import copy
import json
import random
import unittest
from pathlib import Path

from leeroy_dojo import lifecycle as lc_mod
from leeroy_dojo.lifecycle import Lifecycle, check_info
from leeroy_dojo.protocol import MAX_COPIES, MAX_TEXT_BYTES, RECENT_EPISODES, Bounds, decode_request, encode

FIXTURE = Path(__file__).resolve().parents[2] / "modules" / "mod-dojo" / "tests" / "fixtures" / "dojo_protocol.jsonl"
WORKER = json.loads(FIXTURE.read_bytes().decode("utf-8").split("\n")[0])["worker"]
OPEN = [{"actor": i, "obs": [0.5], "mask": [1, 0, 1]} for i in range(2)]


def recv(lc: Lifecycle, message: dict):
    return lc.handle(decode_request(encode(message), lc.bounds))


def step_done(sim_ms=200, terminated=False):
    return {"elapsed_ms": 200, "substeps": 20, "sim_ms": sim_ms, "observations": copy.deepcopy(OPEN), "events": [],
            "rewards": [{"actor": i, "terminal": 0, "shaping": 0.0} for i in range(2)], "terminated": terminated,
            "truncated": False, "outcome": {"reason": "time_limit", "winner": None} if terminated else None}


def started(complete: bool = False) -> Lifecycle:
    """Session s1 after hello, with episode e1's reset (generation 1, token 1) in flight or, if complete, done."""
    lc = Lifecycle(WORKER)
    lc.connect("s1")
    recv(lc, {"v": 1, "type": "hello", "id": "h1", "protocol": {"min": 1, "max": 1}})
    recv(lc, {"v": 1, "type": "reset", "id": "r1", "session": "s1", "episode": "e1", "decision": 0,
              "scenario_seed": 7})
    if complete:
        lc.complete_reset(1, 1, {"ok": True, "observations": copy.deepcopy(OPEN), "sim_ms": 0})
    return lc


def step(rid: str, decision: int) -> dict:
    return {"v": 1, "type": "step", "id": rid, "session": "s1", "episode": "e1", "decision": decision,
            "actions": [{"actor": 0, "action": 0}, {"actor": 1, "action": 0}]}


class InfoTest(unittest.TestCase):
    def test_accepts_the_fixture_worker(self):
        check_info(WORKER, Bounds())

    def test_refuses_bad_worker_descriptions(self):
        def broken(**change):
            info = copy.deepcopy(WORKER)
            info.update(change)
            return info

        for info in (broken(actions=["bolt", "noop"]), broken(actions=[]), broken(actors=[]),
                     broken(actors=[{"slot": 1, "team": 0}]), broken(actors=[{"slot": i, "team": 0} for i in range(3)]),
                     broken(scenario="has space"), broken(schemas={"observation": "x"}), {**WORKER, "extra": 1}):
            with self.assertRaises(ValueError):
                Lifecycle(info)

    def test_recent_episode_ids_cannot_be_reused(self):
        lc = Lifecycle(WORKER)
        lc.connect("s1")
        recv(lc, {"v": 1, "type": "hello", "id": "h", "protocol": {"min": 1, "max": 1}})

        def reset(n: int, episode: str):
            out = recv(lc, {"v": 1, "type": "reset", "id": f"r{n}", "session": "s1", "episode": episode,
                            "decision": 0, "scenario_seed": 1})
            if out.effects:
                lc.complete_reset(lc.generation, lc.tokens, {"ok": True, "observations": copy.deepcopy(OPEN),
                                                             "sim_ms": 0})
            return out

        for n in range(RECENT_EPISODES):
            self.assertTrue(reset(n, f"e{n}").effects)
        self.assertEqual(reset(1000, "e0").send[0]["code"], "stale_episode")
        self.assertEqual(reset(1001, f"e{RECENT_EPISODES - 1}").send[0]["code"], "stale_episode")
        self.assertTrue(reset(1002, "fresh").effects)
        self.assertTrue(reset(1003, "e0").effects, "beyond the window the id is forgotten, as documented")

    def test_worker_payload_indices_are_exact_integers(self):
        """The reviewer's probe: False, True, 0.0 and 1.0 used to pass for actor slots (C++ always refused them)."""
        for actors in ((False, True), (0.0, 1.0)):
            lc = started()
            out = lc.complete_reset(1, 1, {"ok": True, "observations": [dict(o, actor=a) for o, a in zip(OPEN, actors)],
                                           "sim_ms": 0})
            self.assertEqual((lc.state, out.send[0]["reason"]), ("aborted", "invalid_result"), actors)
        lc = started(complete=True)
        recv(lc, step("s1", 0))
        done = step_done()
        done["rewards"][0]["actor"] = False
        self.assertEqual(lc.complete_step(1, 2, done).send[0]["reason"], "invalid_result")
        for slot in (False, 0.0):
            with self.assertRaises(ValueError):
                Lifecycle({**WORKER, "actors": [{"slot": slot, "team": 0}, {"slot": 1, "team": 1}]})

    def test_an_unencodable_result_aborts_instead_of_raising(self):
        """A worker payload Python cannot encode (a set, bytes, a tuple key) used to escape _fits as a TypeError."""
        for event in ({"kind": "x", "data": {1, 2}}, {"kind": "x", "data": b"raw"}, {"kind": "x", (1, 2): 3}):
            lc = started(complete=True)
            recv(lc, step("s1", 0))
            out = lc.complete_step(1, 2, dict(step_done(), events=[event]))
            self.assertEqual((lc.state, out.send[0]["code"], out.send[0]["reason"]),
                             ("aborted", "aborted", "invalid_result"), event)

    def test_in_flight_copies_are_capped(self):
        """The reviewer's probe: 5000 copies of a step in flight used to produce 5001 step_results."""
        lc = started(complete=True)
        self.assertEqual(len(recv(lc, step("s9", 0)).effects), 1)
        answers = [recv(lc, step("s9", 0)).send for _ in range(5000)]
        self.assertTrue(all(len(a) <= 1 for a in answers))
        self.assertEqual(sum(1 for a in answers if a and a[0]["code"] == "busy"), 5001 - MAX_COPIES)
        done = lc.complete_step(1, 2, step_done())
        self.assertEqual([m["type"] for m in done.send], ["step_result"] * MAX_COPIES)

    def test_a_late_completion_cannot_finish_the_next_step(self):
        """The reviewer's probe: with decision 1 in flight, decision 0's completion (same generation) answered it."""
        lc = started(complete=True)
        recv(lc, step("a0", 0))
        first = lc.tokens
        self.assertEqual(lc.complete_step(lc.generation, first, step_done()).send[0]["id"], "a0")
        recv(lc, step("a1", 1))
        late = lc.complete_step(lc.generation, first, step_done())
        self.assertEqual((late.stale, late.send, lc.in_flight, lc.decision), (True, [], "a1", 1))
        self.assertEqual(lc.complete_step(lc.generation, lc.tokens, step_done()).send[0]["id"], "a1")

    def test_error_messages_are_clipped(self):
        lc = started()
        out = lc.complete_reset(1, 1, {"ok": False, "reason": "x" * 3000 + "\ud800"})   # long, a lone surrogate
        message = out.send[0]["message"]
        self.assertLessEqual(len(message.encode("utf-8")), MAX_TEXT_BYTES)
        encode(out.send[0]).encode("utf-8")
        decoded = decode_request(encode({"v": 1, "type": "status", "id": "q", "k" * 5000: 1}))
        self.assertLessEqual(len(decoded.message.encode("utf-8")), MAX_TEXT_BYTES)

    def test_one_session_at_a_time(self):
        lc = Lifecycle(WORKER)
        lc.connect("s1")
        with self.assertRaises(RuntimeError):
            lc.connect("s2")
        lc.disconnect()
        lc.connect("s2")
        self.assertEqual(lc.session, "s2")


class NoDoubleApplicationTest(unittest.TestCase):
    """Random interleavings of duplicates, conflicts, stale and premature requests, completions and disconnects."""

    def test_random_sequences_never_apply_a_decision_twice(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                self.run_sequence(random.Random(seed), 400)

    def run_sequence(self, rng: random.Random, length: int):
        lc = Lifecycle(WORKER, Bounds(result_cache=3))
        serial = 0
        sent_lines: list[dict] = []
        steps_begun: set[tuple[int, int]] = set()
        resets_begun: dict[str, list[str]] = {}
        pending: list[tuple[str, int, int]] = []
        tokens: set[int] = set()
        last_generation = 0
        episode_counter = 0
        history: list[dict] = []

        def connect():
            nonlocal serial
            serial += 1
            lc.connect(f"s{serial}")
            recv(lc, {"v": 1, "type": "hello", "id": f"h{serial}", "protocol": {"min": 1, "max": 1}})

        connect()
        for _ in range(length):
            choice = rng.random()
            session = lc.session or "s0"
            if choice < 0.08:
                out = lc.disconnect()
                connect()
            elif choice < 0.25 and pending:
                kind, gen, token = pending.pop(rng.randrange(len(pending)))
                forged = rng.random() < 0.2      # a completion naming another operation's token
                if forged:
                    token += rng.randint(1, 3)
                before = lc.in_flight
                if kind == "reset":
                    out = lc.complete_reset(gen, token, {"ok": True, "observations": copy.deepcopy(OPEN), "sim_ms": 0})
                else:
                    out = lc.complete_step(gen, token, step_done(terminated=rng.random() < 0.05))
                if forged:
                    self.assertTrue(out.stale, "a forged token completed an operation")
                if out.stale:
                    self.assertEqual(lc.in_flight, before)
            elif choice < 0.3:
                out = lc.fault()
            elif choice < 0.45 and history:
                message = copy.deepcopy(rng.choice(history))
                if rng.random() < 0.3 and message["type"] == "step":
                    message["actions"][0]["action"] = 2 - message["actions"][0]["action"]
                out = recv(lc, message)
            elif choice < 0.6:
                episode_counter += 1
                episode = f"e{rng.randrange(max(episode_counter // 2, 1)) if rng.random() < 0.2 else episode_counter}"
                message = {"v": 1, "type": "reset", "id": f"r{len(history)}", "session": session,
                           "episode": episode, "decision": 0, "scenario_seed": rng.randrange(100)}
                history.append(message)
                out = recv(lc, message)
            else:
                decision = (lc.decision or 0) + rng.choice((0, 0, 0, 1, -1))
                message = {"v": 1, "type": "step", "id": f"r{len(history)}", "session": session,
                           "episode": lc.episode or "e0", "decision": max(decision, 0),
                           "actions": [{"actor": 0, "action": rng.choice((0, 2))}, {"actor": 1, "action": 0}]}
                history.append(message)
                out = recv(lc, message)

            self.assertIn(lc.state, lc_mod.STATES)
            self.assertGreaterEqual(lc.generation, last_generation)
            last_generation = lc.generation
            if out.stale:
                self.assertEqual(out.send, [])
            for effect in out.effects:
                if effect["effect"] in ("begin_step", "begin_reset"):
                    self.assertNotIn(effect["token"], tokens, "a token was handed out twice")
                    tokens.add(effect["token"])
                if effect["effect"] == "begin_step":
                    key = (effect["generation"], effect["decision"])
                    self.assertNotIn(key, steps_begun, "a decision was applied twice")
                    steps_begun.add(key)
                    pending.append(("step", effect["generation"], effect["token"]))
                elif effect["effect"] == "begin_reset":
                    begun = resets_begun.setdefault(lc.session, [])
                    self.assertNotIn(effect["episode"], begun[-RECENT_EPISODES:],
                                     "a recent episode was reset twice in one session")
                    begun.append(effect["episode"])
                    pending.append(("reset", effect["generation"], effect["token"]))
            sent_lines.extend(out.send)
        for message in sent_lines:
            json.loads(encode(message))


if __name__ == "__main__":
    unittest.main()
