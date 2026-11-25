import copy
import unittest

from leeroy_dojo.realm.offline import reached


def observation():
    return dict(token="run", error="", samples=100, scripted_wipe=True, restarting_seen=True, restarts_at_wipe=0,
                bodies=[dict(slot=i, guid=i) for i in range(1, 11)],
                native=dict(stage="running", stage_ms=5000, restarts=1, driver=True, leader=1, elected=1,
                            event=dict(id="run", instance_id=123),
                            bodies=[dict(guid=i, alive=True, present=True, map=603, instance=123) for i in range(1, 11)]))


class RestartGateTest(unittest.TestCase):
    def test_tank_swap_requires_actual_razorscale_role_change_and_restoration(self):
        sample = observation() | dict(wipe_boss=2, main_tank_at_wipe=2, main_tank=1)
        self.assertTrue(reached(sample, target="ulduar-tank-restart", token="run"))
        for key, value in (("wipe_boss", 3), ("main_tank_at_wipe", 1), ("main_tank_at_wipe", 0),
                           ("main_tank_at_wipe", None), ("main_tank", 2), ("main_tank", None),
                           ("scripted_wipe", False), ("restarting_seen", False)):
            with self.subTest(key=key, value=value):
                self.assertFalse(reached(sample | {key: value}, target="ulduar-tank-restart", token="run"))

    def test_requires_the_real_post_wipe_restart_and_all_actual_bodies(self):
        self.assertTrue(reached(observation(), target="ulduar-restart", token="run"))
        for change in ("no_wipe", "not_seen", "no_restart", "two_restarts", "restarting", "short", "driver_lost",
                       "election_changed", "actor_missing", "duplicate_actor", "wrong_actor", "dead", "absent",
                       "wrong_map", "wrong_instance", "bad_count", "bad_leader", "bad_before"):
            sample = copy.deepcopy(observation())
            native = sample["native"]
            if change == "no_wipe": sample["scripted_wipe"] = False
            elif change == "not_seen": sample["restarting_seen"] = False
            elif change == "no_restart": native["restarts"] = 0
            elif change == "two_restarts": native["restarts"] = 2
            elif change == "restarting": native["stage"] = "restarting"
            elif change == "short": native["stage_ms"] = 4999
            elif change == "driver_lost": native["driver"] = False
            elif change == "election_changed": native["elected"] = 2
            elif change == "actor_missing": native["bodies"].pop()
            elif change == "duplicate_actor": native["bodies"][-1] = native["bodies"][0]
            elif change == "wrong_actor": native["bodies"][-1]["guid"] = 999
            elif change == "dead": native["bodies"][-1]["alive"] = False
            elif change == "absent": native["bodies"][-1]["present"] = False
            elif change == "wrong_map": native["bodies"][-1]["map"] = 0
            elif change == "wrong_instance": native["bodies"][-1]["instance"] = 999
            elif change == "bad_count": native["restarts"] = True
            elif change == "bad_leader": native["leader"] = native["elected"] = None
            elif change == "bad_before": sample["restarts_at_wipe"] = -1
            with self.subTest(change=change):
                self.assertFalse(reached(sample, target="ulduar-restart", token="run"))
