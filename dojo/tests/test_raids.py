"""The runtime gate must reject absent/vacuous evidence, including broken negative controls."""
import copy
import unittest
from leeroy_dojo.realm.raids import parse_status, verdict
from leeroy_dojo.realm.world import WorldError


def observation(scenario, negative=False):
    return dict(format=1, kind="dojo-raid-observation", scenario=scenario, token="abc", control=negative,
                state="done", setup=True, cleanup=True, failure="", map=dict(valithria=631, twins=649, poison=533)[scenario],
                instance=42, samples=500, max_gap_ms=15, combat_observed=True, reach_competitor=True,
                effective_healing=0 if negative else 10000, heal_events=0 if negative else 3,
                light_samples=0 if negative else 8, dark_samples=0 if negative else 12,
                switch_ms=0 if negative else 3500, crossings=3, hazard_samples=100,
                minimum_clearance=1.0 if negative else 3.2, poison_hits=2 if negative else 0)


class RaidVerdictTest(unittest.TestCase):
    def check(self, o):
        return verdict(o, scenario=o["scenario"], token="abc", negative=o["control"])

    def test_positive_and_negative_effects(self):
        for scenario in ("valithria", "twins", "poison"):
            for negative in (False, True):
                self.assertTrue(self.check(observation(scenario, negative))["ok"])

    def test_infrastructure_failures_never_count_as_expected_negative(self):
        for field, value in (("token", "old"), ("state", "cleanup_failed"), ("cleanup", False), ("setup", False),
                             ("failure", "login timeout"), ("instance", 0), ("map", 0), ("samples", 0)):
            for scenario in ("valithria", "twins", "poison"):
                for negative in (False, True):
                    with self.subTest(field=field, scenario=scenario, negative=negative):
                        o = observation(scenario, negative)
                        o[field] = value
                        self.assertFalse(self.check(o)["ok"])

    def test_successful_casts_and_health_setup_do_not_prove_healing(self):
        for changed in ({"heal_events": 0}, {"effective_healing": 0}, {"reach_competitor": False}, {"combat_observed": False}):
            o = observation("valithria") | changed | {"casts": 99, "action_success": True}
            self.assertFalse(self.check(o)["ok"])

    def test_only_one_essence_or_no_switch_fails(self):
        for changed in ({"light_samples": 0}, {"dark_samples": 0}, {"switch_ms": 0}):
            self.assertFalse(self.check(observation("twins") | changed)["ok"])

    def test_poison_standing_still_no_hazard_sparse_sampling_and_hits_fail(self):
        for changed in ({"crossings": 0}, {"hazard_samples": 0}, {"max_gap_ms": 201},
                        {"minimum_clearance": 1.5}, {"poison_hits": 1}):
            self.assertFalse(self.check(observation("poison") | changed)["ok"])

    def test_broken_negative_controls_fail(self):
        for scenario, changed in (("valithria", {"heal_events": 1, "effective_healing": 100}),
                                  ("twins", {"light_samples": 1}), ("poison", {"poison_hits": 0})):
            self.assertFalse(self.check(observation(scenario, True) | changed)["ok"])

    def test_missing_or_invalid_counters_cannot_pass_negative_control(self):
        for key in ("heal_events", "poison_hits", "light_samples", "minimum_clearance"):
            for invalid in (None, -1, float("nan"), True, "0"):
                self.assertFalse(self.check(observation("poison", True) | {key: invalid})["ok"])

    def test_replies_must_have_one_observation(self):
        for text in ("command succeeded", "", 'dojo-raid {}\ndojo-raid {}'):
            with self.assertRaises(WorldError):
                parse_status(text)
        self.assertEqual(parse_status('server prompt\ndojo-raid {"state":"idle"}\n'), {"state": "idle"})


if __name__ == "__main__":
    unittest.main()
