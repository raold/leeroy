import unittest

from leeroy_dojo.realm.yogg import verdict


def observed(negative=False):
    team = [False, False, True, False, False, True, False, True, True]
    return dict(kind="dojo-yogg-observation", format=1, token="test", state="done", failure="",
                negative=negative, ready=True, cleanup=True, portals=4, initial_sanity_members=9,
                samples=500, max_gap_ms=60, native_spell_teleports=0, native_charge_samples=0, native_constrictor_attachments=0,
                observation_clock="map_update", illusion=1, max_below=0 if negative else 4,
                max_healers_below=0 if negative else 1, healer_entrants=0 if negative else 1, min_healers_above=2, tank_below_samples=0,
                influence_deaths=0 if negative else 8, brain_hits=0 if negative else 10,
                brain_start_health=1000000, brain_least_health=1000000 if negative else 900000,
                exit_remaining_ms=0 if negative else 8000,
                entered=[False] * 9 if negative else team.copy(), exited=[False] * 9 if negative else team.copy(),
                portal_casts=[0] * 9 if negative else [int(v) for v in team],
                exit_casts=[0] * 9 if negative else [int(v) for v in team], minimum_sanity=[80] * 9)


class YoggGateTest(unittest.TestCase):
    def test_actual_team_and_sensitive_control(self):
        for negative in (True, False):
            self.assertTrue(verdict(observed(negative), token="test", negative=negative)["ok"])

    def test_missing_fields_and_cross_run_receipts_fail(self):
        for key in observed():
            sample = observed(); del sample[key]
            self.assertFalse(verdict(sample, token="test")["ok"], key)
        self.assertFalse(verdict(observed(), token="different")["ok"])

    def test_roles_actual_effects_and_deadline_are_all_required(self):
        for key, value in (("ready", False), ("cleanup", False), ("max_below", 5), ("max_healers_below", 0),
                           ("min_healers_above", 0), ("healer_entrants", 2), ("tank_below_samples", 1), ("influence_deaths", 0),
                           ("brain_hits", 0), ("brain_least_health", 1000000), ("exit_remaining_ms", 999),
                           ("portal_casts", [0] * 9), ("exit_casts", [0] * 9), ("exited", [False] * 9),
                           ("minimum_sanity", [0] + [80] * 8), ("max_gap_ms", 201), ("illusion", 0)):
            with self.subTest(key=key):
                sample = observed(); sample[key] = value
                self.assertFalse(verdict(sample, token="test")["ok"])

    def test_unexposed_or_insensitive_control_fails(self):
        for key, value in (("portals", 0), ("initial_sanity_members", 0), ("samples", True),
                           ("entered", [True] + [False] * 8), ("brain_hits", 1), ("influence_deaths", 1),
                           ("failure", "yogg_actor_dead_lost_or_charmed"), ("native_spell_teleports", True),
                           ("native_spell_teleports", -1), ("native_charge_samples", True),
                           ("native_charge_samples", -1), ("native_constrictor_attachments", True),
                           ("native_constrictor_attachments", -1), ("observation_clock", "world_update")):
            sample = observed(True); sample[key] = value
            self.assertFalse(verdict(sample, token="test", negative=True)["ok"], key)
