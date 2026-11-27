"""A feature enablement receipt needs observed game effects, including exposure."""
import unittest

from leeroy_dojo.realm.raids import verdict


def evidence(scenario, negative=False):
    return dict(format=1, kind="dojo-raid-observation", scenario=scenario, token="abc", control=negative,
                state="done", setup=True, cleanup=True, failure="", map=631, instance=42, samples=500,
                max_gap_ms=15, reach_competitor=True, effective_healing=0 if negative else 10000,
                heal_events=0 if negative else 3, light_samples=0, dark_samples=0, switch_ms=0,
                crossings=0, hazard_samples=100, minimum_clearance=0, poison_hits=0,
                room_samples=100, harvest_samples=100, strangulate_samples=100,
                escape_ms=0 if negative else 2200, outside_samples=0 if negative else 100,
                inside_samples=10, late_hits=0, encounter_samples=100, defile_hits=4 if negative else 1,
                final_hazard_clearance=-5.0 if negative else 4.0)


class LichKingEvidenceTest(unittest.TestCase):
    def check(self, o):
        return verdict(o, scenario=o["scenario"], token="abc", negative=o["control"])["ok"]

    def test_controls_and_observed_effects(self):
        for scenario in ("lk-terenas", "lk-defile"):
            for negative in (True, False):
                self.assertTrue(self.check(evidence(scenario, negative)))

    def test_cast_success_does_not_prove_a_terenas_heal(self):
        for changed in ({"heal_events": 0}, {"effective_healing": 0}, {"room_samples": 0},
                        {"reach_competitor": False}, {"encounter_samples": 0}):
            with self.subTest(changed=changed):
                self.assertFalse(self.check(evidence("lk-terenas") | changed | {"action_success": True}))
        self.assertFalse(self.check(evidence("lk-terenas", True) | {"heal_events": 1}))

    def test_defile_needs_real_exposure_prompt_escape_and_no_later_damage(self):
        for changed in ({"inside_samples": 0}, {"hazard_samples": 0}, {"escape_ms": 0},
                        {"escape_ms": 4001}, {"outside_samples": 0}, {"late_hits": 1},
                        {"final_hazard_clearance": 0}, {"final_hazard_clearance": float("nan")},
                        {"max_gap_ms": 201}, {"encounter_samples": 0}):
            with self.subTest(changed=changed):
                self.assertFalse(self.check(evidence("lk-defile") | changed))
        self.assertFalse(self.check(evidence("lk-defile", True) | {"defile_hits": 0}))

    def test_teleport_alone_cannot_certify_a_harvest_soul_entry(self):
        for negative in (True, False):
            for field in ("harvest_samples", "strangulate_samples"):
                for value in (None, True, -1, 0, 1.5, "1"):
                    with self.subTest(negative=negative, field=field, value=value):
                        self.assertFalse(self.check(evidence("lk-terenas", negative) | {field: value}))

    def test_invalid_counters_never_pass_either_control(self):
        for scenario in ("lk-terenas", "lk-defile"):
            for negative in (True, False):
                for field in ("room_samples", "escape_ms", "defile_hits", "encounter_samples"):
                    for value in (None, True, -1, 1.5, "1"):
                        with self.subTest(scenario=scenario, negative=negative, field=field, value=value):
                            self.assertFalse(self.check(evidence(scenario, negative) | {field: value}))


if __name__ == "__main__":
    unittest.main()
