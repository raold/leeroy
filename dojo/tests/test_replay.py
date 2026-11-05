"""Replaying recorded episodes without a server, and the G1 report's reconciliation and audit, on synthetic recordings
in the native worker's shape."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo import actions, observation, outcome, policies, replay
from leeroy_dojo.realm import g1
from leeroy_dojo.recording import EpisodeRecorder, decision_record

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "modules" / "mod-dojo" / "tests" / "fixtures"
KIT = json.loads((FIXTURES / "action_cases.json").read_text(encoding="utf-8"))["kit"]


def setup() -> tuple[actions.Catalog, observation.Schema]:
    catalog = actions.build_catalog(KIT)
    return catalog, observation.build_schema(catalog)


def header(catalog, schema, specs) -> dict:
    return {"schemas": {"observation": f"{observation.SCHEMA}:{schema.sha256}", "action": f"{actions.SCHEMA}:{catalog.sha256}"},
            "actions": [policies.wire_name(e.name) for e in catalog.entries], "catalog_kit": catalog.kit,
            "actors": [{"slot": 0, "team": 0}, {"slot": 1, "team": 1}], "policies": specs}


def record_episode(root: Path, episode: str, *, decisions: int = 6, ending: str = "time_limit", tamper=None) -> Path:
    """Two policies stepped over synthetic observations, recorded as the native worker's step results would be."""
    catalog, schema = setup()
    players = [policies.Noop(schema, catalog, 0), policies.RandomLegal(schema, catalog, 9)]
    for p in players:
        p.reset(episode)
    specs = [{"actor": i, **p.describe()} for i, p in enumerate(players)]
    rec = EpisodeRecorder(root, episode, {**header(catalog, schema, specs), "episode": episode})
    def obs_for(k: int) -> list[dict]:
        out = []
        for slot in (0, 1):
            values = [0.0] * schema.size
            values[schema.index("self.alive")] = 1.0
            values[schema.index("self.health")] = round(1 - 0.1 * k * slot, 6)
            mask = [1 if (j + k + slot) % 4 else 0 for j in range(len(catalog.entries))]
            mask[0] = 1
            out.append({"actor": slot, "obs": values, "mask": mask})
        return out
    observations = obs_for(0)
    for k in range(decisions):
        last = k == decisions - 1
        chosen = [players[o["actor"]].act(o["obs"], o["mask"]) for o in observations]
        if tamper and tamper[0] == k:
            chosen[tamper[1]] = tamper[2]
        nxt = obs_for(k + 1)
        events = [{"kind": "command", "producer": "protocol", "outcome": "queued", "actor": 1,
                   "action_id": 2 * k + 1, "executor_id": 100 + k, "t_ms": 200 * k}] if chosen[1] else []
        if last and ending == "death":
            events.append({"kind": "death", "actor": 1, "t_ms": 200 * k + 10})
        o = None
        if last:
            o = {"reason": ending, "winner": 0 if ending == "death" else None}
        terminal = [0, 0] if not o or o["winner"] is None else [1, -1]
        result = {"id": f"s{k}", "decision": k, "next_decision": k + 1, "sim_ms": 1000 + 200 * (k + 1),
                  "elapsed_ms": 200, "substeps": 20,
                  "requested": [{"actor": s, "action": chosen[s], "action_id": 2 * k + s} for s in (0, 1)],
                  "observations": nxt, "events": events,
                  "rewards": [{"actor": s, "terminal": terminal[s], "shaping": 0.0} for s in (0, 1)],
                  "terminated": last, "truncated": False, "outcome": o}
        rec.add(decision_record(observations, result, 1000))
        observations = nxt
    rec.finish("terminated", reason=ending, winner=0 if ending == "death" else None, decisions=decisions)
    return rec.dir


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-replay-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_recording_replays_and_its_policies_reproduce_every_request(self):
        path = record_episode(self.tmp, "ep-a")
        report = replay.replay_episode(path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["reproduced"]["decisions"], report["reproduced"]["of"])
        self.assertEqual(report["reproduced"]["of"], 12)
        self.assertEqual(report["outcome"], "terminated")
        self.assertTrue(report["timeline"])

    def test_a_request_the_policy_would_not_make_is_caught(self):
        catalog, _ = setup()
        path = record_episode(self.tmp, "ep-b", tamper=(2, 0, catalog.index("none/forward/keep")))
        report = replay.replay_episode(path)
        self.assertFalse(report["ok"])
        self.assertTrue(report["reproduced"]["differing"])

    def test_a_masked_request_is_a_problem(self):
        catalog, _ = setup()
        # Decision 3's mask for actor 0 closes every index j with (j + 3) % 4 == 0, such as 1.
        path = record_episode(self.tmp, "ep-c", tamper=(3, 0, 1))
        report = replay.replay_episode(path)
        self.assertTrue(any("which its mask closed" in p for p in report["problems"]), report["problems"])

    def test_a_death_outcome_needs_its_death_event(self):
        path = record_episode(self.tmp, "ep-d", ending="death")
        report = replay.replay_episode(path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["counts"][1]["deaths"], 1)
        self.assertEqual(report["returns"][0]["terminal"], 1)

    def test_an_aborted_episode_is_excluded(self):
        catalog, schema = setup()
        rec = EpisodeRecorder(self.tmp, "ep-e", header(catalog, schema, []))
        rec.finish("aborted", reason="worker fault", decisions=0)
        report = replay.replay_episode(rec.dir)
        self.assertTrue(report["ok"])
        self.assertFalse(report["usable"])
        self.assertIn("excluded", report)

    def test_a_schema_the_worker_did_not_publish_is_refused(self):
        path = record_episode(self.tmp, "ep-f")
        h = json.loads((path / "header.json").read_text(encoding="utf-8"))
        h["schemas"]["observation"] = f"{observation.SCHEMA}:{'0' * 64}"
        _, _, problems = replay.rebuild(h)
        self.assertTrue(problems)


class CountTest(unittest.TestCase):
    def test_damage_deaths_and_completed_casts(self):
        catalog, _ = setup()
        bolt = catalog.index("frostbolt/hold/toward")
        lance = catalog.index("ice_lance/hold/toward")
        records = [{"actors": [{"actor": 0, "action": bolt, "action_id": 0}, {"actor": 1, "action": lance, "action_id": 1}],
                    "events": [
                        {"kind": "command", "producer": "protocol", "outcome": "queued", "actor": 0, "action_id": 0},
                        {"kind": "command", "producer": "executor", "outcome": "executed", "actor": 1, "action_id": 1,
                         "detail": "face toward ok; cast ice_lance ok: cast"},
                        {"kind": "command", "producer": "executor", "outcome": "executed", "actor": 0, "action_id": 0,
                         "detail": "face toward ok; cast frostbolt ok: casting"},
                        {"kind": "damage", "actor": 1, "target": 0, "amount": 900}]},
                   {"actors": [{"actor": 0, "action": 0, "action_id": 2}, {"actor": 1, "action": 0, "action_id": 3}],
                    "events": [{"kind": "cast", "producer": "executor", "outcome": "completed", "actor": 0, "action_id": 0},
                               {"kind": "damage", "actor": 0, "target": 1, "amount": 3000},
                               {"kind": "death", "actor": 1}]}]
        # A command rejected for its movement still cast its instant.
        records[1]["events"].append({"kind": "command", "producer": "executor", "outcome": "rejected", "actor": 1,
                                     "action_id": 3, "detail": "cast frost_nova ok: cast; move forward_right FAILED: rooted"})
        records[1]["events"].append({"kind": "command", "producer": "executor", "outcome": "rejected", "actor": 1,
                                     "action_id": 3, "detail": "cast ice_lance FAILED: not_ready; move left ok: 1.75 yd"})
        counts = replay.count_events(records, catalog)
        self.assertEqual(counts[1]["casts"], {"frost_nova": 1, "ice_lance": 1})
        self.assertEqual(counts[0]["casts"], {"frostbolt": 1})
        self.assertEqual((counts[0]["damage_done"], counts[0]["damage_taken"]), (3000, 900))
        self.assertEqual((counts[1]["deaths"], counts[1]["hits"]), (1, 1))


class ReconcileTest(unittest.TestCase):
    def score(self, done, deaths, kb, casts):
        core = {"actors": [{"actor": i, "damage_done": done[i], "deaths": deaths[i], "killing_blows": kb[i]} for i in (0, 1)]}
        dojo = [{"actor": i, "damage_done": done[i], "deaths": deaths[i], "casts": casts[i]} for i in (0, 1)]
        return {"core": core, "dojo": dojo}

    def test_matching_counts_reconcile(self):
        catalog, _ = setup()
        bolt_id = str(next(k["spell_id"] for k in KIT if actions.ability_name(k["name"]) == "frostbolt"))
        bolt = catalog.index("frostbolt/hold/toward")
        records = [{"decision": 0, "outcome": {"reason": "death", "winner": 0},
                    "actors": [{"actor": 0, "action": bolt, "action_id": 0, "reward": {"terminal": 1, "shaping": -0.02}},
                               {"actor": 1, "action": 0, "action_id": 1, "reward": {"terminal": -1, "shaping": 0.02}}],
                    "events": [{"kind": "command", "producer": "protocol", "outcome": "queued", "actor": 0, "action_id": 0},
                               {"kind": "cast", "producer": "executor", "outcome": "completed", "actor": 0, "action_id": 0},
                               {"kind": "damage", "actor": 0, "target": 1, "amount": 500}, {"kind": "death", "actor": 1}]}]
        bounds = [{"decision": 0, "standings": [{"team": 0, "alive": True, "health": 1.0}, {"team": 1, "alive": True, "health": 0.6}]},
                  {"decision": 1, "standings": [{"team": 0, "alive": True, "health": 1.0}, {"team": 1, "alive": False, "health": 0}]}]
        result = {"outcome": {"reason": "death", "winner": 0},
                  "score": self.score([500, 0], [0, 1], [1, 0], [{bolt_id: 1}, {}])}
        got = g1.reconcile(result, catalog, records, bounds)
        self.assertTrue(got["ok"], got["problems"])
        self.assertEqual(got["shaping"]["decisions"], 1)
        # Phi went from 0.4 to 0 (terminal): -0.02 for team 0.
        self.assertAlmostEqual(outcome.potential([outcome.Standing(0, True, 1), outcome.Standing(1, True, 0.6)]), 0.4)

    def test_a_mismatch_is_reported(self):
        catalog, _ = setup()
        result = {"outcome": {"reason": "death", "winner": 0}, "score": self.score([500, 0], [0, 1], [1, 0], [{}, {}])}
        result["score"]["dojo"][0]["damage_done"] = 400
        got = g1.reconcile(result, catalog, None, None)
        self.assertFalse(got["ok"])
        result = {"outcome": {"reason": "death", "winner": 0}, "score": self.score([0, 0], [0, 0], [0, 0], [{}, {}])}
        self.assertFalse(g1.reconcile(result, catalog, None, None)["ok"])


class ResultsFilesTest(unittest.TestCase):
    def test_the_plans_evidence_files_name_the_gate_and_inputs(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-g1-"))
        try:
            report = {"inputs": {"scenario": {"id": "s", "sha256": "0" * 64}}, "reset_reliability": {"ok": True},
                      "hello": {"session": "s1"}, "receipt": {"port": 1},
                      "episodes": [{"name": "m", "reset": {"generation": 3}, "decisions": 9, "outcome": None,
                                    "endpoint": {"counts": {"steps": 9}}},
                                   {"name": "stock", "stock": True, "reset": {"generation": 4}}]}
            g1.results_files(tmp, report)
            resets = json.loads((tmp / "reset-results.json").read_text(encoding="utf-8"))
            protocol = json.loads((tmp / "protocol-results.json").read_text(encoding="utf-8"))
            self.assertEqual((resets["gate"], protocol["gate"]), ("G1", "G1"))
            self.assertEqual(resets["inputs"], report["inputs"])
            self.assertEqual([r["generation"] for r in resets["this_run"]], [3, 4])
            self.assertEqual([e["episode"] for e in protocol["episodes"]], ["m"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AuditTest(unittest.TestCase):
    def test_stock_in_a_controlled_episode_and_foreign_commands_fail(self):
        catalog, _ = setup()
        ok = {"episode": "e", "trace": [{"producer": "executor", "kind": "command", "action_id": 5, "origin": "step:3:0"}]}
        summary, _ = g1.audit(ok, [], catalog)
        self.assertTrue(summary["ok"], summary["problems"])
        bad = {"episode": "e", "trace": ok["trace"] + [{"producer": "stock", "kind": "cast", "action_id": 0}]}
        self.assertFalse(g1.audit(bad, [], catalog)[0]["ok"])
        foreign = {"episode": "e", "trace": [{"producer": "executor", "kind": "command", "action_id": 5,
                                              "origin": "console:1"}]}
        self.assertFalse(g1.audit(foreign, [], catalog)[0]["ok"])
        stock = {"episode": "s", "stock": True, "trace": [{"producer": "stock (released)", "kind": "cast", "action_id": 0}]}
        self.assertTrue(g1.audit(stock, None, catalog)[0]["ok"])

    def test_each_request_gets_its_final_outcome(self):
        catalog, _ = setup()
        bolt = catalog.index("frostbolt/hold/toward")
        records = [{"decision": 0, "actors": [{"actor": 0, "action": bolt, "action_id": 0}, {"actor": 1, "action": 0, "action_id": 1}],
                    "events": [{"kind": "command", "producer": "protocol", "outcome": "queued", "action_id": 0},
                               {"kind": "command", "producer": "executor", "outcome": "executed", "action_id": 0,
                                "exec_t_ms": 150}]}]
        summary, lines = g1.audit({"episode": "e", "trace": []}, records, catalog)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(lines[0]["outcome"], "executed")


if __name__ == "__main__":
    unittest.main()
