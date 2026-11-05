"""The reset acceptance's own logic, without a server: its schedule, and the checks it applies to each completed reset
(old-generation records, stock actions, the manifest's expectation and the reference)."""

import copy
import json
import unittest
from pathlib import Path

from leeroy_dojo import actions, observation
from leeroy_dojo.realm import reset
from leeroy_dojo.scenario import load_manifest

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "dojo" / "scenarios" / "frost_mirror_small_v1.json"


def make_run():
    manifest = load_manifest(SCENARIO).data
    kit = [{"spell_id": k["spell_id"], "name": k["name"], "target": t, "facing": f}
           for k, t, f in zip(manifest["loadout"]["kit"], ("enemy", "enemy", "self"), (True, True, False))]
    run = reset.Run.__new__(reset.Run)
    run.manifest = manifest
    run.catalog = actions.build_catalog(kit)
    run.schema = observation.build_schema(run.catalog)
    run.context = {"class_id": 8, "level": 80, "tree_points": [0, 0, 71], "perceptual_delay_ms": 100,
                   "kit": [{"name": "frostbolt", "base_cooldown_ms": 0}, {"name": "ice_lance", "base_cooldown_ms": 0},
                           {"name": "frost_nova", "base_cooldown_ms": 25000}]}
    run.results = []
    return run


def ready_record(run, generation=5, previous=4, sees=(False, False)):
    initial = []
    for slot in (0, 1):
        values = observation.expected_initial(run.schema, run.catalog, run.manifest, slot, run.context,
                                              enemy_visible=sees[slot], start_offset_ms=10)
        initial.append({"actor": slot, "generation": generation, "age_ms": 100, "start_offset_ms": 10,
                        "values": values, "mask": "1" * len(run.catalog.entries)})
    clean = {"auras": [1, 2], "cooldowns": [], "health": 16523}
    return {"generation": generation, "previous_generation": previous, "previous_first_action_id": 10,
            "began_seq": 100,
            "status": {"phase": "running", "generation": generation, "first_action_id": 20, "result": "ready"},
            "evidence": {"generation": generation, "initial": initial,
                         "clean": {"actors": [dict(clean), dict(clean)], "sees": list(sees), "problems": []}},
            "trace": []}


def trace(seq, action_id=0, producer="executor", kind="command", outcome="executed"):
    return {"seq": seq, "action_id": action_id, "producer": producer, "kind": kind, "outcome": outcome, "detail": ""}


class ScheduleTest(unittest.TestCase):
    def test_a_hundred_resets_cover_every_case(self):
        plan = reset.schedule(100)
        self.assertEqual(len(plan), 100)
        for case in [c for c in reset.CYCLE if c] + list(reset.TENTH):
            self.assertIn(case, plan)
        self.assertGreaterEqual(plan.count("death"), 10)
        self.assertGreaterEqual(plan.count("disconnect"), 10)
        self.assertGreaterEqual(plan.count("arena_fault"), 2)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.run = make_run()

    def test_a_clean_reset_passes(self):
        record = ready_record(self.run)
        self.assertEqual(self.run.check(record, None, None, None, []), [])
        reference = copy.deepcopy(record)
        self.assertEqual(self.run.check(record, reference, None, None, []), [])

    def test_the_manifest_expectation_is_checked(self):
        record = ready_record(self.run)
        record["evidence"]["initial"][0]["values"][self.run.schema.index("self.mana")] = 0.9
        problems = self.run.check(record, None, None, None, [])
        self.assertTrue(any("self.mana" in p for p in problems), problems)

    def test_an_old_generation_record_after_the_reset_fails_it(self):
        record = ready_record(self.run)
        record["trace"] = [trace(90, 12), trace(101, 12, kind="command", outcome="dropped"), trace(102, 15)]
        problems = self.run.check(record, None, None, None, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("action 15", problems[0])

    def test_a_stock_record_fails_it(self):
        record = ready_record(self.run)
        record["trace"] = [trace(105, 0, producer="stock", kind="cast", outcome="observed")]
        self.assertTrue(self.run.check(record, None, None, None, []))

    def test_a_difference_from_the_reference_fails_it(self):
        record = ready_record(self.run)
        reference = copy.deepcopy(record)
        record["evidence"]["clean"]["actors"][1]["auras"] = [1, 2, 3]
        problems = self.run.check(record, reference, None, None, [])
        self.assertEqual(problems, ["actor 1 clean state auras: [1, 2, 3] (reference [1, 2])"])

    def test_a_generation_that_skips_fails_it(self):
        record = ready_record(self.run, generation=7, previous=5)
        self.assertTrue(any("jumped" in p for p in self.run.check(record, None, None, None, [])))

    def test_a_first_observation_inside_the_delay_fails_it(self):
        record = ready_record(self.run)
        record["evidence"]["initial"][1]["age_ms"] = 60
        self.assertTrue(any("60 ms old" in p for p in self.run.check(record, None, None, None, [])))

    def test_not_ready_is_reported(self):
        record = ready_record(self.run)
        record["status"]["phase"] = "failed"
        self.assertEqual(len(self.run.check(record, None, None, None, [])), 1)


class LeakTest(unittest.TestCase):
    def test_a_pending_command_must_be_dropped(self):
        decision = {"requested": [{"action": 5, "action_id": 30}, {"action": 0, "action_id": 31}]}
        record = {"began_seq": 10, "trace": [trace(11, 30, producer="episode", outcome="dropped")]}
        self.assertEqual(reset.leaked(record, {"case": "delayed_action", "decision": decision}), [])
        record["trace"].append(trace(12, 30))
        self.assertEqual(len(reset.leaked(record, {"case": "delayed_action", "decision": decision})), 1)
        record = {"began_seq": 10, "trace": []}
        self.assertEqual(len(reset.leaked(record, {"case": "delayed_action", "decision": decision})), 1)

    def test_longest_run(self):
        self.assertEqual(reset._longest_run([{"ok": True}, {"ok": False}, {"ok": True}, {"ok": True}]), 2)


class LinesTest(unittest.TestCase):
    def test_prefixed_json_lines(self):
        text = 'noise\ndojo-episode {"phase": "running"}\ndojo-episode-reset {"generation": 2}\n'
        self.assertEqual(reset._lines("dojo-episode", text), [{"phase": "running"}])
        self.assertEqual(reset._lines("dojo-episode-reset", text), [json.loads('{"generation": 2}')])


if __name__ == "__main__":
    unittest.main()
