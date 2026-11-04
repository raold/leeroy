import unittest

from leeroy_dojo import fake_game as g
from leeroy_dojo.lifecycle import check_info
from leeroy_dojo.protocol import Bounds


def commands(decision: int, a: int, b: int = g.NOOP) -> list[dict[str, int]]:
    return [{"actor": 0, "action": a, "action_id": decision * 2}, {"actor": 1, "action": b, "action_id": decision * 2 + 1}]


def run(game: g.FakeDuel, plan: dict[int, tuple[int, int]], decisions: int) -> list[dict]:
    results = []
    for d in range(decisions):
        a, b = plan.get(d, (g.NOOP, g.NOOP))
        results.append(game.step(commands(d, a, b)))
        if results[-1]["terminated"]:
            break
    return results


def events(results, kind):
    return [e for r in results for e in r["events"] if e["kind"] == kind]


class FakeDuelTest(unittest.TestCase):
    def setUp(self):
        self.game = g.FakeDuel()
        self.reset = self.game.reset(1)

    def test_info_is_a_valid_worker_description(self):
        check_info(g.info(), Bounds())
        self.assertEqual(g.info()["actions"][0], "noop")

    def test_reset_observes_full_actors_with_noop_open(self):
        self.assertTrue(self.reset["ok"])
        for o in self.reset["observations"]:
            self.assertEqual(len(o["obs"]), len(g.OBSERVATION_FIELDS))
            self.assertEqual(o["mask"], [1, 0, 1, 1, 1])
            self.assertEqual(o["obs"][0], 1.0)

    def test_a_bolt_lands_after_delay_plus_cast_and_names_its_origin(self):
        results = run(self.game, {0: (g.BOLT, g.NOOP)}, 5)
        start = events(results, "cast_start")
        self.assertEqual([(e["t_ms"], e["action_id"]) for e in start], [(150, 0)])
        hit = events(results, "damage")
        self.assertEqual([(e["t_ms"], e["action_id"], e["target"]) for e in hit], [(750, 0, 1)])
        self.assertIn(hit[0]["amount"], range(g.BOLT_DAMAGE[0], g.BOLT_DAMAGE[1] + 1))
        self.assertTrue(results[1]["observations"][1]["obs"][7], "the enemy sees the cast")

    def test_noop_keeps_a_pending_command_and_a_new_one_supersedes_it(self):
        # With a 150 ms delay inside 200 ms decisions a command never outlives its decision, so park one by hand.
        self.assertEqual(self.game.step(commands(0, g.NOOP))["events"], [])
        self.game.actors[0].pending = g._Pending(g.LANCE, 100, 10_000)
        step = self.game.step(commands(1, g.NOOP))
        self.assertEqual(step["events"], [])
        self.assertEqual(self.game.actors[0].pending.action_id, 100)
        self.assertEqual(step["observations"][0]["obs"][5], 1.0, "the pending command is part of self observation")
        step = self.game.step(commands(2, g.MOVE))
        self.assertEqual(events([step], "superseded"),
                         [{"kind": "superseded", "t_ms": 400, "actor": 0, "action_id": 100, "by": 4}])
        self.assertEqual([e["action_id"] for e in events([step], "moved")], [4])

    def test_rejections_name_their_reason(self):
        results = run(self.game, {0: (g.LANCE, g.STOP_CAST), 1: (g.LANCE, g.NOOP)}, 2)
        rejected = events(results, "rejected")
        self.assertIn({"kind": "rejected", "t_ms": 150, "actor": 1, "action_id": 1, "reason": "not_casting"}, rejected)
        self.assertIn({"kind": "rejected", "t_ms": 350, "actor": 0, "action_id": 2, "reason": "gcd"}, rejected)
        self.game.actors[1].mana = 0
        step = self.game.step(commands(2, g.NOOP, g.LANCE))
        self.assertEqual(events([step], "rejected")[0]["reason"], "no_mana")

    def test_stop_cast_and_movement_cancel_a_cast(self):
        results = run(self.game, {0: (g.BOLT, g.NOOP), 1: (g.STOP_CAST, g.NOOP)}, 4)
        cancelled = events(results, "cast_cancelled")
        self.assertEqual([(e["action_id"], e["reason"]) for e in cancelled], [(0, "stop_cast")])
        self.assertEqual(events(results, "damage"), [])
        game = g.FakeDuel()
        game.reset(1)
        results = run(game, {0: (g.BOLT, g.NOOP), 1: (g.MOVE, g.NOOP)}, 4)
        self.assertEqual([e["reason"] for e in events(results, "cast_cancelled")], ["moved"])

    def test_death_ends_the_episode_with_zero_sum_rewards(self):
        self.game.actors[1].hp = 3
        step = self.game.step(commands(0, g.LANCE))
        self.assertTrue(step["terminated"])
        self.assertEqual(step["outcome"], {"reason": "death", "winner": 0})
        self.assertEqual([r["terminal"] for r in step["rewards"]], [1, -1])
        self.assertEqual(step["elapsed_ms"], 150)
        self.assertEqual(step["substeps"], 15)
        self.assertAlmostEqual(sum(r["shaping"] for r in step["rewards"]), 0.0)
        self.assertEqual(self.game.step(commands(1, g.NOOP)), {"fault": "no episode is running"})

    def test_simultaneous_death_is_a_draw(self):
        self.game.actors[0].hp = self.game.actors[1].hp = 3
        step = self.game.step(commands(0, g.LANCE, g.LANCE))
        self.assertEqual(step["outcome"], {"reason": "simultaneous_death", "winner": None})
        self.assertEqual([r["terminal"] for r in step["rewards"]], [0, 0])

    def test_time_limit_is_a_draw_and_shaping_returns_to_zero(self):
        game = g.FakeDuel(time_limit_ms=1000)
        game.reset(3)
        results = run(game, {0: (g.LANCE, g.NOOP)}, 10)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[-1]["outcome"], {"reason": "time_limit", "winner": None})
        total = sum(r["rewards"][0]["shaping"] for r in results)
        self.assertAlmostEqual(total, 0.0, places=5)   # potential shaping telescopes to Phi(terminal) = 0

    def test_same_seed_and_commands_replay_exactly(self):
        plan = {d: (g.BOLT if d % 4 == 0 else g.LANCE, g.LANCE) for d in range(60)}
        games = {seed: g.FakeDuel() for seed in (42, 43)}
        twin = g.FakeDuel()
        for seed, game in games.items():
            game.reset(seed)
        twin.reset(42)
        first = run(games[42], plan, 60)
        self.assertEqual(first, run(twin, plan, 60))
        self.assertNotEqual([e["amount"] for e in events(first, "damage")],
                            [e["amount"] for e in events(run(games[43], plan, 60), "damage")])

    def test_reset_does_not_rewind_the_worker_clock(self):
        self.game.step(commands(0, g.NOOP))
        again = self.game.reset(1)
        self.assertEqual(again["sim_ms"], 200)
        step = self.game.step(commands(0, g.BOLT))
        self.assertEqual(step["sim_ms"], 400)
        self.assertEqual(events([step], "cast_start")[0]["t_ms"], 150, "event times are episode-relative")

    def test_policies_only_choose_open_actions(self):
        game = g.FakeDuel()
        game.reset(9)
        observations = game.observations()
        for _ in range(400):
            actions = [g.POLICIES[name](o["obs"], o["mask"]) for name, o in zip(("caster", "lancer"), observations)]
            for o, action in zip(observations, actions):
                self.assertEqual(o["mask"][action], 1)
            step = game.step([{"actor": i, "action": a, "action_id": i} for i, a in enumerate(actions)])
            if step["terminated"]:
                break
            observations = step["observations"]
        self.assertTrue(step["terminated"])


if __name__ == "__main__":
    unittest.main()
