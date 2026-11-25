import copy
import unittest

from leeroy_dojo.realm.offline import reached
from leeroy_dojo.realm.offline_naxx import APPROACH_SPAWNS


def sample(control=False):
    states = [3 if i in (3, 4, 5, 6, 7, 8, 10) else 0 for i in range(15)]
    states[0] = 0 if control else 1
    return dict(token="run", error="", samples=100, scripted_wipe=False,
        native=dict(active=not control, cleanup=control, stage="" if control else "running", map=533,
            boss_states=states, event=dict(id="run", instance_id=123, wipes=0, kills=0,
                state="done" if control else "running", reason="dc_unreachable" if control else "")),
        bodies=[dict(slot=1, map=533, instance=123, alive=True, owned=True, pinned=True, bosses=states)],
        naxx_pass=dict(state="ready", control=control, measured_ms=310000, gothik_skipped=not control,
            patchwerk_engaged=not control, skip_ms=0 if control else 1000, engage_ms=0 if control else 300000,
            passes=16 if control else 1, setup_bosses=[3, 4, 5, 6, 7, 8, 10], setup_completed_mask=127,
            setup_boss_deaths=[15956,15953,15952,15954,15936,16011,16061], setup_add_deaths=2,
            setup_trash_deaths=APPROACH_SPAWNS.copy(),
            setup_location="razuvious_room",
            strategies_restored=True))


class NaxxPassTest(unittest.TestCase):
    def test_real_skip_then_pull_within_ten_minutes(self):
        self.assertTrue(reached(sample(), target="naxx-gothik-pass", token="run"))
        changes = [
            ("gate", "gothik_skipped", False), ("gate", "patchwerk_engaged", False),
            ("gate", "skip_ms", 300001), ("gate", "engage_ms", 600001),
            ("gate", "measured_ms", 600001), ("gate", "passes", 0), ("gate", "passes", 2),
            ("gate", "passes", True), ("gate", "control", True),
            ("gate", "setup_bosses", [3, 4, 5, 6, 7, 8, 10, 11]),
            ("gate", "setup_completed_mask", 255), ("gate", "strategies_restored", False),
            ("gate", "setup_boss_deaths", []), ("gate", "setup_add_deaths", 0),
            ("gate", "setup_trash_deaths", []),
            ("gate", "setup_location", "gothik_corridor"),
            ("gate", "setup_trash_deaths", APPROACH_SPAWNS[:-1]),
            ("gate", "setup_trash_deaths", APPROACH_SPAWNS + [97736]),
            ("body", "map", 649), ("body", "instance", 456), ("body", "alive", False),
            ("body", "owned", False), ("body", "pinned", False),
            ("body", "bosses", [3] * 15), ("event", "wipes", 1),
            ("event", "kills", 7), ("event", "kills", True),
            ("native", "stage", "restarting"), ("native", "active", False),
        ]
        for section, key, value in changes:
            obs = sample()
            part = {"gate": obs["naxx_pass"], "body": obs["bodies"][0],
                    "event": obs["native"]["event"], "native": obs["native"]}[section]
            part[key] = value
            with self.subTest(section=section, key=key, value=value):
                self.assertFalse(reached(obs, target="naxx-gothik-pass", token="run"))

    def test_negative_must_actually_stall_and_clean_up(self):
        self.assertTrue(reached(sample(True), target="naxx-gothik-pass-control", token="run"))
        for key, value in (("gothik_skipped", True), ("patchwerk_engaged", True), ("passes", 0),
                           ("skip_ms", 1), ("engage_ms", 1), ("control", False)):
            obs = sample(True)
            obs["naxx_pass"][key] = value
            self.assertFalse(reached(obs, target="naxx-gothik-pass-control", token="run"))
        for reason in ("timeout", "dojo_stop", "boss_wipe_cap", "dc_door_blocked", "cleared_reachable"):
            obs = sample(True)
            obs["native"]["event"]["reason"] = reason
            self.assertFalse(reached(obs, target="naxx-gothik-pass-control", token="run"))
        obs = sample(True)
        obs["native"]["cleanup"] = False
        self.assertFalse(reached(obs, target="naxx-gothik-pass-control", token="run"))

    def test_prerequisite_state_cannot_stand_in_for_skip_or_pull(self):
        for index in (0, 3, 4, 5, 6, 7, 8, 10, 11):
            obs = copy.deepcopy(sample())
            obs["bodies"][0]["bosses"][index] = 3 if index in (0, 11) else 0
            self.assertFalse(reached(obs, target="naxx-gothik-pass", token="run"))
