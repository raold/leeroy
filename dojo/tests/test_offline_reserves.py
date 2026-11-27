"""The reserve must actually enter the owned group and the same running instance."""
import copy
import unittest

from leeroy_dojo.realm.offline import reached


class ReserveMilestoneTest(unittest.TestCase):
    def test_receipt_needs_the_benched_actor_not_a_full_group_ack(self):
        members = [dict(guid=i, present=True, alive=True, map=649, instance=123) for i in (*range(1, 10), 11)]
        bodies = [dict(slot=i, guid=i, map=649, instance=123, alive=True, owned=True, grouped=True, master=50)
                  for i in (*range(1, 10), 11)]
        obs = dict(token="abc", error="", samples=100, bodies=bodies,
                   native=dict(stage="running", stage_ms=5000, elected=1, leader=1, bodies=members,
                               event=dict(id="abc", instance_id=123)))
        self.assertTrue(reached(obs, target="offline-reserves", token="abc"))
        for change in ("member_missing", "wrong_member", "duplicate", "absent", "dead", "wrong_instance",
                       "unowned", "ungrouped", "no_master", "short", "bad_timer", "wrong_leader"):
            bad = copy.deepcopy(obs)
            if change == "member_missing": bad["native"]["bodies"].pop()
            elif change == "wrong_member": bad["native"]["bodies"][-1]["guid"] = 10
            elif change == "duplicate": bad["native"]["bodies"][-1] = bad["native"]["bodies"][0]
            elif change == "absent": bad["native"]["bodies"][-1]["present"] = False
            elif change == "dead": bad["native"]["bodies"][-1]["alive"] = False
            elif change == "wrong_instance": bad["native"]["bodies"][-1]["instance"] = 999
            elif change == "unowned": bad["bodies"][-1]["owned"] = False
            elif change == "ungrouped": bad["bodies"][-1]["grouped"] = False
            elif change == "no_master": bad["bodies"][-1]["master"] = 0
            elif change == "short": bad["native"]["stage_ms"] = 4999
            elif change == "bad_timer": bad["native"]["stage_ms"] = None
            else: bad["native"]["elected"] = 2
            with self.subTest(change=change):
                self.assertFalse(reached(bad, target="offline-reserves", token="abc"))


if __name__ == "__main__":
    unittest.main()
