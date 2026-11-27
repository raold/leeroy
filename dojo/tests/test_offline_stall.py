import unittest

from leeroy_dojo.realm.offline_stall import reached_stall


def observation():
    return {"native_stall": dict(state="held", released=True, limit_ms=600000, held_ms=599990,
                                 samples=30000, max_gap_ms=30),
            "native": dict(active=False, cleanup=True, map=649,
                           event=dict(state="done", reason="stalled", kills=0, wipes=0))}


class NativeStallTest(unittest.TestCase):
    def test_real_native_stop_after_the_full_plateau(self):
        self.assertTrue(reached_stall(observation()))

    def test_missing_evidence_is_not_a_pass(self):
        for section in ("native_stall", "native"):
            for key in observation()[section]:
                sample = observation(); del sample[section][key]
                self.assertFalse(reached_stall(sample), key)

    def test_early_stop_timeout_operator_stop_and_unreleased_hold_fail(self):
        for key, value in (("held_ms", 597999), ("held_ms", 610001), ("limit_ms", 60000),
                           ("samples", 999), ("samples", True), ("max_gap_ms", 201), ("released", False)):
            sample = observation(); sample["native_stall"][key] = value
            self.assertFalse(reached_stall(sample), key)
        for key, value in (("reason", "stopped"), ("reason", "timeout"), ("state", "failed"),
                           ("kills", 1), ("wipes", 1), ("kills", False)):
            sample = observation(); sample["native"]["event"][key] = value
            self.assertFalse(reached_stall(sample), key)
