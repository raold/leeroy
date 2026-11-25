import copy
import unittest

from leeroy_dojo.realm.offline import reached


def observation():
    return dict(token="run", error="", samples=1000,
        native=dict(stage="running", driver=True, leader=15, elected=15, event=dict(id="run", instance_id=42, wipes=0)),
        bodies=[dict(slot=i, guid=14+i, owned=True, pinned=True, map=631, instance=42, bosses=[3]*11) for i in range(1, 11)],
        valithria_frontier=dict(state="ready", setup_strategies_restored=True, measured_ms=20000,
            heals=5, healing=30000, start_health=500000, peak_health=1000000, setup_completed_mask=0))


class ValithriaFrontierTest(unittest.TestCase):
    def test_native_done_needs_real_healing_after_explicit_preparation(self):
        obs = observation()
        self.assertTrue(reached(obs, target="icc-valithria-frontier", token="run"))
        for key, value in (("state", "arriving"), ("setup_strategies_restored", False), ("measured_ms", 0),
                           ("heals", 1), ("healing", 0), ("start_health", 0),
                           ("peak_health", 500000), ("setup_completed_mask", 1)):
            bad = copy.deepcopy(obs)
            bad["valithria_frontier"][key] = value
            with self.subTest(key=key):
                self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))
        for key in obs["valithria_frontier"]:
            bad = copy.deepcopy(obs)
            del bad["valithria_frontier"][key]
            with self.subTest(missing=key):
                self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))
        bad = copy.deepcopy(obs)
        bad["bodies"][0]["bosses"][10] = 1
        self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))

    def test_driver_and_complete_owned_roster_are_required(self):
        for key, value in (("driver", False), ("elected", 16), ("leader", None)):
            bad = observation()
            bad["native"][key] = value
            with self.subTest(native=key):
                self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))
        for key, value in (("owned", False), ("pinned", False), ("guid", 15), ("instance", 43)):
            bad = observation()
            bad["bodies"][1][key] = value
            with self.subTest(actor=key):
                self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))
        bad = observation()
        bad["bodies"].pop()
        self.assertFalse(reached(bad, target="icc-valithria-frontier", token="run"))

    def test_original_full_route_check_does_not_use_frontier_claims(self):
        obs = observation()
        obs["bodies"][0]["bosses"][10] = 0
        self.assertFalse(reached(obs, target="icc-valithria", token="run"))

    def test_clear_after_wiping_cannot_reuse_frontier_evidence(self):
        for value in (1, 2, True, None):
            obs = observation()
            obs["native"]["event"]["wipes"] = value
            with self.subTest(wipes=value):
                self.assertFalse(reached(obs, target="icc-valithria-frontier", token="run"))


if __name__ == "__main__":
    unittest.main()
