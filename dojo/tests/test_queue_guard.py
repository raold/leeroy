import unittest

from leeroy_dojo.realm import queue_guard, raids
from test_raids import observation


def queue_observation():
    return dict(kind="dojo-queue-observation", format=1, token="abc", state="done", cleanup=True,
                failure="", control_queued=True, control_left=True, max_gap_ms=20,
                cases=[dict(case=name, attempts=17, samples=500, queued_samples=0,
                            map=36 if name == "dungeon" else 0, instance=7 if name == "dungeon" else 0,
                            group=8 if name in ("raid", "pinned_party") else 0,
                            pinned_self=name == "pinned_solo", pinned_peer=name == "pinned_party")
                       for name in ("raid", "pinned_solo", "pinned_party", "dungeon")])


class QueueProofTest(unittest.TestCase):
    def test_control_and_every_guard_are_required(self):
        self.assertTrue(queue_guard.verdict(queue_observation(), token="abc")["ok"])
        for field, value in (("control_queued", False), ("control_left", False), ("cleanup", False),
                             ("max_gap_ms", 201), ("token", "old"), ("cases", [])):
            with self.subTest(field=field):
                self.assertFalse(queue_guard.verdict(queue_observation() | {field: value}, token="abc")["ok"])

    def test_attempts_are_not_queue_or_group_proof(self):
        for slot in range(4):
            for field, value in (("attempts", 0), ("samples", 0), ("queued_samples", 1),
                                 ("pinned_self", None), ("pinned_peer", None), ("case", "raid")):
                if slot == 0 and field == "case":
                    continue
                o = queue_observation()
                o["cases"][slot][field] = value
                self.assertFalse(queue_guard.verdict(o, token="abc")["ok"])
        for slot in (0, 2):
            o = queue_observation(); o["cases"][slot]["group"] = 0
            self.assertFalse(queue_guard.verdict(o, token="abc")["ok"])


def fire_observation(negative=False):
    return observation("poison", negative) | dict(scenario="trigger-flame", map=603,
        trigger_aura_samples=200, trigger_hits=2 if negative else 0, inside_samples=2,
        outside_samples=0 if negative else 100, escape_ms=0 if negative else 900,
        late_hits=0, trigger_radius=3., trigger_displacement=0. if negative else 5.,
        final_hazard_clearance=-3. if negative else 2.)


class TriggerProofTest(unittest.TestCase):
    def check(self, o):
        return raids.verdict(o, scenario="trigger-flame", token="abc", negative=o["control"])

    def test_real_damage_control_and_escape_can_pass(self):
        for negative in (False, True):
            self.assertTrue(self.check(fire_observation(negative))["ok"])

    def test_stationary_vacuous_late_reentering_and_sparse_proofs_fail(self):
        for changed in ({"trigger_aura_samples": 0}, {"trigger_displacement": 0.}, {"inside_samples": 0},
                        {"escape_ms": 4001}, {"outside_samples": 0}, {"late_hits": 1},
                        {"max_gap_ms": 201}, {"final_hazard_clearance": 0.}, {"reach_competitor": False}):
            self.assertFalse(self.check(fire_observation() | changed)["ok"])
        self.assertFalse(self.check(fire_observation(True) | {"trigger_hits": 0})["ok"])
        for key in ("trigger_radius", "trigger_hits", "inside_samples", "trigger_displacement"):
            for value in (None, True, "0", float("nan")):
                self.assertFalse(self.check(fire_observation() | {key: value})["ok"])

    def test_coldflame_requires_escape_before_the_native_area_expires(self):
        o = fire_observation() | dict(scenario="coldflame", map=631)
        def check(value):
            return raids.verdict(value, scenario="coldflame", token="abc", negative=False)
        self.assertTrue(check(o)["ok"])
        for changed in ({"escape_ms": 2001}, {"trigger_aura_samples": 0}, {"outside_samples": 0},
                        {"failure": "native_trigger_disappeared"}, {"trigger_displacement": 0.}):
            self.assertFalse(check(o | changed)["ok"])
