import copy
import unittest

from leeroy_dojo.realm.vehicles import verdict


def observed(negative=False):
    return dict(kind="dojo-vehicle-observation", format=1, scenario="leviathan", token="test", state="done",
                failure="", negative=negative, ready=True, human_boarded=True, cleanup=True,
                observation_clock="map_update",
                samples=500 if negative else 800, max_gap_ms=60,
                human_death_ms=0 if negative else 20000, boss_start_health=0 if negative else 1000000,
                boss_least_health=0 if negative else 900000, driver_entries=[0, 0] if negative else [33060, 33109],
                damage_hits=[0, 0] if negative else [10, 15], moving_hits=[0, 0] if negative else [2, 4],
                after_death_hits=[0, 0] if negative else [3, 6], distance=[0, 0] if negative else [90.5, 70.2])


class VehicleGateTest(unittest.TestCase):
    def test_real_effects_and_quiet_negative_control(self):
        for negative in (True, False):
            self.assertTrue(verdict(observed(negative), token="test", negative=negative)["ok"])

    def test_acknowledgement_or_one_successful_driver_is_not_proof(self):
        for key, value in (("ready", False), ("human_boarded", False), ("cleanup", False),
                           ("samples", 0), ("max_gap_ms", 201), ("human_death_ms", 0),
                           ("boss_least_health", 1000000), ("driver_entries", [33060, 33067]),
                           ("damage_hits", [10, 0]), ("moving_hits", [0, 4]),
                           ("after_death_hits", [3, 0]), ("distance", [0, 70])):
            with self.subTest(key=key):
                sample = observed(); sample[key] = value
                self.assertFalse(verdict(sample, token="test")["ok"])

    def test_malformed_and_cross_run_observations_fail_closed(self):
        source = observed()
        for key in source:
            with self.subTest(key=key):
                sample = copy.deepcopy(source); del sample[key]
                self.assertFalse(verdict(sample, token="test")["ok"])
        for key, value in (("samples", True), ("distance", [float("nan"), 90]), ("damage_hits", [True, 5]),
                           ("driver_entries", [33060]), ("failure", "fixture_deadline"), ("token", "different"),
                           ("observation_clock", "world_update")):
            sample = observed(); sample[key] = value
            self.assertFalse(verdict(sample, token="test")["ok"])

    def test_control_with_effects_is_insensitive(self):
        for key in ("driver_entries", "damage_hits", "moving_hits", "after_death_hits", "distance"):
            sample = observed(True); sample[key] = [1, 0]
            self.assertFalse(verdict(sample, token="test", negative=True)["ok"])


def drakes(negative=False):
    sample = observed(negative)
    sample.update(scenario="malygos", driver_entries=[30161, 30161], setup_add_kills=6,
                  heal_events=0 if negative else 5, effective_healing=0 if negative else 2000,
                  dot_samples=0 if negative else 100, field_samples=100, surge_samples=[10, 15],
                  shield_samples=[0, 0] if negative else [5, 6], safe_ring_samples=[0, 0] if negative else [40, 50],
                  field_escapes=[0, 0] if negative else [1, 2], longest_field_ms=[9000, 9000] if negative else [1800, 2200])
    return sample


def reset_probe(negative=False):
    sample = observed(False)
    sample.update(scenario="leviathan-reset", negative=negative, reset_requested=not negative,
                  reset_ready=not negative, reset_reengaged=not negative, reset_old_accessories=12,
                  human_death_ms=0 if negative else 20000,
                  reset_removed_accessories=0 if negative else 12, reset_fresh_accessories=0 if negative else 12)
    return sample


class ResetGateTest(unittest.TestCase):
    def test_reset_and_no_evade_control_require_native_exposure(self):
        for negative in (False, True):
            self.assertTrue(verdict(reset_probe(negative), token="test", scenario="leviathan-reset",
                                    negative=negative)["ok"])

    def test_old_accessories_partial_reset_and_no_second_pull_fail(self):
        for key, value in (("reset_requested", False), ("reset_ready", False), ("reset_reengaged", False),
                           ("reset_old_accessories", 0), ("reset_removed_accessories", 11),
                           ("reset_fresh_accessories", 13), ("reset_fresh_accessories", True),
                           ("damage_hits", [0, 3]), ("after_death_hits", [0, 3])):
            with self.subTest(key=key):
                sample = reset_probe(); sample[key] = value
                self.assertFalse(verdict(sample, token="test", scenario="leviathan-reset")["ok"])
        for key in reset_probe():
            sample = reset_probe(); del sample[key]
            self.assertFalse(verdict(sample, token="test", scenario="leviathan-reset")["ok"], key)

    def test_control_must_not_report_reset(self):
        for key, value in (("reset_requested", True), ("reset_ready", True), ("reset_reengaged", True),
                           ("reset_old_accessories", 0), ("reset_removed_accessories", 1),
                           ("reset_fresh_accessories", 1), ("driver_entries", [0, 0]), ("human_death_ms", 20000)):
            sample = reset_probe(True); sample[key] = value
            self.assertFalse(verdict(sample, token="test", scenario="leviathan-reset", negative=True)["ok"], key)


class DrakeGateTest(unittest.TestCase):
    def test_effects_are_required_and_disabled_strategy_is_sensitive(self):
        for negative in (True, False):
            self.assertTrue(verdict(drakes(negative), token="test", scenario="malygos", negative=negative)["ok"])
        sample = drakes(True); sample["failure"] = "drake_driver_or_mount_lost"
        self.assertTrue(verdict(sample, token="test", scenario="malygos", negative=True)["ok"])

    def test_missing_phase_or_effect_or_wrong_target_fails(self):
        for key, value in (("setup_add_kills", 0), ("driver_entries", [30161, 33060]), ("heal_events", 0),
                           ("effective_healing", 0), ("dot_samples", 0), ("field_samples", 0),
                           ("surge_samples", [0, 0]), ("shield_samples", [5, 0]), ("safe_ring_samples", [0, 50]),
                           ("failure", "drake_driver_or_mount_lost"), ("distance", [0, 80]),
                           ("field_escapes", [0, 0]), ("longest_field_ms", [4001, 2200])):
            with self.subTest(key=key):
                sample = drakes(); sample[key] = value
                self.assertFalse(verdict(sample, token="test", scenario="malygos")["ok"])

    def test_missing_fields_and_unexposed_control_fail(self):
        for key in drakes():
            sample = drakes(); del sample[key]
            self.assertFalse(verdict(sample, token="test", scenario="malygos")["ok"], key)
        for key, value in (("setup_add_kills", 0), ("surge_samples", [0, 0]), ("field_samples", 0),
                           ("heal_events", 1), ("shield_samples", [0, 1]), ("dot_samples", 1),
                           ("damage_hits", [0, 1]), ("failure", "malygos_phase_setup_lost")):
            sample = drakes(True); sample[key] = value
            self.assertFalse(verdict(sample, token="test", scenario="malygos", negative=True)["ok"], key)
