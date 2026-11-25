"""Offline milestones require the named boss, instance and real job, never a request ack."""
import copy
import unittest
from leeroy_dojo.realm.offline import reached


class OfflineMilestoneTest(unittest.TestCase):
    def test_requires_target_encounter_in_owned_running_instance(self):
        obs = dict(token="abc", error="", samples=12, native={"stage": "running",
            "event": {"id": "abc", "instance_id": 123}}, bodies=[
                dict(slot=1, map=649, instance=123, bosses=[3, 0, 0, 0, 0])])
        self.assertTrue(reached(obs, target="toc-beasts", token="abc"))
        for change in ("token", "event", "stage", "map", "instance", "boss", "samples", "error", "missing"):
            bad = copy.deepcopy(obs)
            if change == "token": bad["token"] = "previous"
            elif change == "event": bad["native"]["event"]["id"] = "previous"
            elif change == "stage": bad["native"]["stage"] = "starting"
            elif change == "map": bad["bodies"][0]["map"] = 603
            elif change == "instance": bad["bodies"][0]["instance"] = 99
            elif change == "boss": bad["bodies"][0]["bosses"] = [0, 3, 3, 3, 3]
            elif change == "samples": bad["samples"] = 0
            elif change == "error": bad["error"] = "failed"
            else: bad["bodies"] = []
            bad["native"]["event"]["kills"] = 100
            bad["ack"] = True
            with self.subTest(change=change):
                self.assertFalse(reached(bad, target="toc-beasts", token="abc"))
