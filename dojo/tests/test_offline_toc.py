import unittest

from leeroy_dojo.realm.offline_toc import reached_intro, reached_anub


def observed():
    return {"toc_intro": dict(state="ready", native_spawn=True, initially_blocked=True, emerged=True,
                             engaged=True, strategies_restored=True, setup_bosses=[0, 1, 2, 3],
                             samples=1000, max_gap_ms=30, trigger_samples=40,
                             measured_ms=12000, emerge_ms=11000, engage_ms=12000),
            "native": dict(stage="running", map=649, boss_states=[3, 3, 3, 3, 1], event=dict(kills=0, wipes=0))}


class TocIntroTest(unittest.TestCase):
    def test_real_surface_and_pull_after_setup(self):
        self.assertTrue(reached_intro(observed()))

    def test_every_required_fact_is_present(self):
        for key in observed()["toc_intro"]:
            sample = observed()
            del sample["toc_intro"][key]
            self.assertFalse(reached_intro(sample), key)

    def test_fake_setup_credit_missing_exposure_and_late_effects_fail(self):
        for key, value in (("max_gap_ms", 201), ("samples", 19), ("trigger_samples", 0),
                           ("native_spawn", False), ("initially_blocked", False), ("emerged", False),
                           ("engaged", False), ("strategies_restored", False), ("emerge_ms", 0),
                           ("engage_ms", 10000), ("measured_ms", 120001), ("samples", True)):
            sample = observed(); sample["toc_intro"][key] = value
            self.assertFalse(reached_intro(sample), key)
        for key, value in (("map", 631), ("stage", "setup"), ("boss_states", [3] * 5)):
            sample = observed(); sample["native"][key] = value
            self.assertFalse(reached_intro(sample), key)
        for key in ("kills", "wipes"):
            for value in (1, False, None):
                sample = observed(); sample["native"]["event"][key] = value
                self.assertFalse(reached_intro(sample), key)


def completed_anub():
    sample = observed()
    sample["scripted_wipe"] = False
    sample["toc_intro"].update(complete_encounter=True, grounded_spheres=2, stopped_spikes=1,
                               boss_start_health=4183500, boss_least_health=0, measured_ms=350000)
    sample["native"].update(active=False, cleanup=True, stage="", boss_states=[3] * 5,
                            event=dict(state="done", reason="cleared", kills=1, wipes=0))
    return sample


class TocAnubTest(unittest.TestCase):
    def test_requires_real_kill_and_native_frost_and_spike_effects(self):
        self.assertTrue(reached_anub(completed_anub()))
        self.assertFalse(reached_anub(observed()))
        self.assertFalse(reached_intro(completed_anub()))

    def test_missing_measurement_is_not_a_pass(self):
        for key in completed_anub()["toc_intro"]:
            sample = completed_anub(); del sample["toc_intro"][key]
            self.assertFalse(reached_anub(sample), key)

    def test_prepared_kills_casts_without_effects_wipes_and_gaps_fail(self):
        for key, value in (("grounded_spheres", 0), ("stopped_spikes", 0), ("complete_encounter", False),
                           ("boss_least_health", 4183500), ("boss_start_health", 0), ("max_gap_ms", 201),
                           ("max_gap_ms", 0), ("measured_ms", 900001), ("engage_ms", 120001),
                           ("stopped_spikes", True), ("grounded_spheres", -1)):
            sample = completed_anub(); sample["toc_intro"][key] = value
            self.assertFalse(reached_anub(sample), key)
        for key, value in (("kills", 5), ("kills", 0), ("wipes", 1), ("reason", "boss_wipe_cap"),
                           ("state", "progress")):
            sample = completed_anub(); sample["native"]["event"][key] = value
            self.assertFalse(reached_anub(sample), key)
        for key, value in (("active", True), ("cleanup", False), ("boss_states", [3, 3, 3, 3, 1])):
            sample = completed_anub(); sample["native"][key] = value
            self.assertFalse(reached_anub(sample), key)
        sample = completed_anub(); sample["scripted_wipe"] = True
        self.assertFalse(reached_anub(sample))
