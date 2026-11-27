import copy
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from leeroy_dojo.realm.offline import configure_full_raid_features, reached
from leeroy_dojo.realm.layout import read_conf


def terminal(target):
    ulduar = target != "toc-full"
    states = [3] * (14 if ulduar else 5)
    if target == "ulduar-route":
        states = [3 if i in (1, 2, 3, 5, 6) else 0 for i in range(14)]
    return dict(token="run", error="", samples=100, scripted_wipe=False,
                toc_twins=dict(enabled=True, active_samples=50, essence_seen=[True] * 10),
                native=dict(active=False, cleanup=True, map=603 if ulduar else 649,
                            boss_states=states, event=dict(id="run", instance_id=123, state="done",
                            reason="cleared_reachable" if target == "ulduar-route" else "cleared",
                            kills=states.count(3))))


class FullOfflineTest(unittest.TestCase):
    def test_full_toc_pins_features_before_boot_from_an_older_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            layout = SimpleNamespace(configs=Path(folder))
            path = layout.configs / "modules/playerbots.conf"
            path.parent.mkdir()
            original = "Playerbots.TwinValkyrInteractions = 0\nPlayerbots.TocWormSupport = 0\nPlayerbots.ReactDelay = 100\n"
            path.write_text(original)
            self.assertEqual(configure_full_raid_features(layout, "toc-beasts"), {})
            self.assertEqual(path.read_text(), original)
            features = configure_full_raid_features(layout, "toc-full")
            self.assertEqual(features, {"Playerbots.TwinValkyrInteractions": "1", "Playerbots.TocWormSupport": "1"})
            config = read_conf(path)
            self.assertEqual({key: config[key] for key in features}, features)
            self.assertEqual(config["Playerbots.ReactDelay"], "100")

    def test_full_toc_requires_enabled_native_essence_effects_for_all_members(self):
        for change in ("missing", "off", "no_samples", "boolean_samples", "no_effect", "short_roster", "integer_effect"):
            sample = terminal("toc-full")
            if change == "missing": del sample["toc_twins"]
            elif change == "off": sample["toc_twins"]["enabled"] = False
            elif change == "no_samples": sample["toc_twins"]["active_samples"] = 0
            elif change == "boolean_samples": sample["toc_twins"]["active_samples"] = True
            elif change == "no_effect": sample["toc_twins"]["essence_seen"][3] = False
            elif change == "short_roster": sample["toc_twins"]["essence_seen"].pop()
            else: sample["toc_twins"]["essence_seen"][0] = 1
            with self.subTest(change=change):
                self.assertFalse(reached(sample, target="toc-full", token="run"))

    def test_full_completion_requires_terminal_and_encounter_evidence(self):
        for target in ("toc-full", "ulduar-full", "ulduar-route"):
            self.assertTrue(reached(terminal(target), target=target, token="run"))
            for change in ("first_boss", "wrong_count", "boolean_state", "active", "cleanup", "wipe_cap",
                           "cleanup_failed", "low_kills", "boolean_kills", "wrong_map", "forced_wipe",
                           "wrong_token", "no_instance"):
                sample = copy.deepcopy(terminal(target))
                native = sample["native"]
                if change == "first_boss": native["boss_states"][2] = 0
                elif change == "wrong_count": native["boss_states"].pop()
                elif change == "boolean_state": native["boss_states"][0] = True
                elif change == "active": native["active"] = True
                elif change == "cleanup": native["cleanup"] = False
                elif change == "wipe_cap": native["event"]["reason"] = "boss_wipe_cap"
                elif change == "cleanup_failed": native["event"]["state"] = "failed"
                elif change == "low_kills": native["event"]["kills"] = 1
                elif change == "boolean_kills": native["event"]["kills"] = True
                elif change == "wrong_map": native["map"] = 0
                elif change == "forced_wipe": sample["scripted_wipe"] = True
                elif change == "wrong_token": native["event"]["id"] = "old"
                else: native["event"]["instance_id"] = 0
                with self.subTest(target=target, change=change):
                    self.assertFalse(reached(sample, target=target, token="run"))

    def test_partial_route_cannot_pass_the_full_raid(self):
        sample = terminal("ulduar-route")
        self.assertFalse(reached(sample, target="ulduar-full", token="run"))
        sample["native"]["event"]["reason"] = "cleared"
        sample["native"]["event"]["kills"] = 14
        self.assertFalse(reached(sample, target="ulduar-full", token="run"))
        for state in (1, 2, 4):
            sample = terminal("ulduar-route")
            sample["native"]["boss_states"][0] = state
            self.assertFalse(reached(sample, target="ulduar-route", token="run"))
