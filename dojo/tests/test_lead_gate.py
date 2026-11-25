"""A lead command acknowledgement cannot substitute for walking and restoration."""
import unittest
from leeroy_dojo.realm.lead import verdict


def evidence(negative=False):
    return dict(kind="dojo-lead-observation", format=1, token="abc", state="done", ready=True,
                cleanup=True, restored=True, failure="off" if negative else "", refused_off=negative,
                samples=0 if negative else 100, follow_samples=0 if negative else 30,
                wait_ms=0 if negative else 3000, elapsed_ms=1000 if negative else 30000,
                leader_distance=0 if negative else 55.0, follower_distance=0 if negative else 43.0,
                resumed_distance=0 if negative else 10.5, waited=not negative, max_gap_ms=30)


class LeadReceiptTest(unittest.TestCase):
    def test_complete_effects_and_disabled_control(self):
        for negative in (False, True):
            self.assertTrue(verdict(evidence(negative), token="abc", negative=negative)["ok"])

    def test_every_effect_is_required_even_with_successful_ack(self):
        for changed in ({"leader_distance": 0}, {"follower_distance": 0}, {"follow_samples": 0},
                        {"wait_ms": 0}, {"waited": False}, {"resumed_distance": 0}, {"restored": False},
                        {"cleanup": False}, {"ready": False}, {"state": "observe"}, {"token": "old"}, {"max_gap_ms": 201}):
            with self.subTest(changed=changed):
                self.assertFalse(verdict(evidence() | changed | {"ack": True}, token="abc")["ok"])

    def test_infrastructure_failure_cannot_pass_as_disabled(self):
        for changed in ({"failure": "fixture_guild_identity_mismatch"}, {"failure": ""}, {"refused_off": False},
                        {"ready": False}, {"restored": False}, {"cleanup": False}):
            with self.subTest(changed=changed):
                self.assertFalse(verdict(evidence(True) | changed, token="abc", negative=True)["ok"])

    def test_malformed_measurements_fail_both_cases(self):
        for negative in (False, True):
            for field in ("samples", "wait_ms", "leader_distance", "resumed_distance"):
                for value in (None, True, -1, float("nan"), "20"):
                    with self.subTest(negative=negative, field=field, value=value):
                        self.assertFalse(verdict(evidence(negative) | {field: value}, token="abc", negative=negative)["ok"])


if __name__ == "__main__":
    unittest.main()
