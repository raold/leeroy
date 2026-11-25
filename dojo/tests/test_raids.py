"""The runtime gate must reject absent/vacuous evidence, including broken negative controls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from leeroy_dojo.realm.raids import encounter_verdict, parse_encounter, parse_status, provenance, verdict
from leeroy_dojo.realm.layout import LayoutError
from leeroy_dojo.realm.world import WorldError


def observation(scenario, negative=False):
    return dict(format=1, kind="dojo-raid-observation", scenario=scenario, token="abc", control=negative,
                state="done", setup=True, cleanup=True, failure="", map=dict(valithria=631, twins=649, poison=533)[scenario],
                instance=42, samples=500, max_gap_ms=15, combat_observed=True, reach_competitor=True,
                effective_healing=0 if negative else 10000, heal_events=0 if negative else 3,
                light_samples=0 if negative else 8, dark_samples=0 if negative else 12,
                switch_ms=0 if negative else 3500, crossings=3, hazard_samples=100,
                minimum_clearance=1.0 if negative else 3.2, poison_hits=2 if negative else 0)


class RaidVerdictTest(unittest.TestCase):
    def check(self, o):
        return verdict(o, scenario=o["scenario"], token="abc", negative=o["control"])

    def test_positive_and_negative_effects(self):
        for scenario in ("valithria", "twins", "poison"):
            for negative in (False, True):
                self.assertTrue(self.check(observation(scenario, negative))["ok"])

    def test_infrastructure_failures_never_count_as_expected_negative(self):
        for field, value in (("token", "old"), ("state", "cleanup_failed"), ("cleanup", False), ("setup", False),
                             ("failure", "login timeout"), ("instance", 0), ("map", 0), ("samples", 0)):
            for scenario in ("valithria", "twins", "poison"):
                for negative in (False, True):
                    with self.subTest(field=field, scenario=scenario, negative=negative):
                        o = observation(scenario, negative)
                        o[field] = value
                        self.assertFalse(self.check(o)["ok"])

    def test_successful_casts_and_health_setup_do_not_prove_healing(self):
        for changed in ({"heal_events": 0}, {"effective_healing": 0}, {"reach_competitor": False}, {"combat_observed": False}):
            o = observation("valithria") | changed | {"casts": 99, "action_success": True}
            self.assertFalse(self.check(o)["ok"])

    def test_only_one_essence_or_no_switch_fails(self):
        for changed in ({"light_samples": 0}, {"dark_samples": 0}, {"switch_ms": 0}):
            self.assertFalse(self.check(observation("twins") | changed)["ok"])

    def test_poison_standing_still_no_hazard_sparse_sampling_and_hits_fail(self):
        for changed in ({"crossings": 0}, {"hazard_samples": 0}, {"max_gap_ms": 201},
                        {"minimum_clearance": 1.5}, {"poison_hits": 1}):
            self.assertFalse(self.check(observation("poison") | changed)["ok"])

    def test_broken_negative_controls_fail(self):
        for scenario, changed in (("valithria", {"heal_events": 1, "effective_healing": 100}),
                                  ("twins", {"light_samples": 1}), ("poison", {"poison_hits": 0})):
            self.assertFalse(self.check(observation(scenario, True) | changed)["ok"])

    def test_missing_or_invalid_counters_cannot_pass_negative_control(self):
        for key in ("heal_events", "poison_hits", "light_samples", "minimum_clearance"):
            for invalid in (None, -1, float("nan"), True, "0"):
                self.assertFalse(self.check(observation("poison", True) | {key: invalid})["ok"])

    def test_replies_must_have_one_observation(self):
        for text in ("command succeeded", "", 'dojo-raid {}\ndojo-raid {}'):
            with self.assertRaises(WorldError):
                parse_status(text)
        self.assertEqual(parse_status('server prompt\ndojo-raid {"state":"idle"}\n'), {"state": "idle"})


def encounter_observation(difficulty="10h", mode="after", outcome="done"):
    healer = dict(healer=True, is_heal=True, level=80, deaths=0, dream_entries=0, dream_ms=0, in_reach_ms=290000,
                  healing_on_her=2000000, mana_cooldowns=3, mana_potions=1, max_twisted_nightmares=0,
                  max_emerald_vigor=0)
    members = [dict(healer, name=f"h{i}") for i in range(3)]
    members += [dict(name=f"d{i}", healer=False, is_heal=False, level=80, deaths=0) for i in range(7)]
    return dict(format=1, kind="dojo-valithria-encounter", token="abc", difficulty=difficulty, mode=mode,
                focus=mode == "after", state="done", setup=True, cleanup=True, failure="", outcome=outcome,
                instance=7, map_difficulty={"10n": 0, "10h": 2}[difficulty], engaged=True, pull_ms=300000,
                samples=1200, start_hp=50.0, hp=100.0 if outcome == "done" else 63.0,
                peak_hp=100.0 if outcome == "done" else 68.0, members=members)


class EncounterVerdictTest(unittest.TestCase):
    def check(self, o):
        return encounter_verdict(o, token="abc", difficulty=o["difficulty"], mode=o["mode"])

    def test_a_kill_needs_her_own_done_state_and_full_health(self):
        for difficulty in ("10n", "10h"):
            for mode in ("before", "after"):
                result = self.check(encounter_observation(difficulty, mode))
                self.assertTrue(result["ok"] and result["killed"])
                self.assertEqual(result["summary"]["healing_on_her"], 6000000)
        for outcome in ("wipe", "reset", "timeout"):
            result = self.check(encounter_observation(outcome=outcome))
            self.assertTrue(result["ok"])
            self.assertFalse(result["killed"])
        self.assertFalse(self.check(encounter_observation() | {"peak_hp": 92.0})["killed"])

    def test_setup_and_harness_failures_are_never_a_kill(self):
        for field, value in (("token", "old"), ("state", "cleanup_failed"), ("cleanup", False), ("setup", False),
                             ("failure", "setup deadline"), ("instance", 0), ("map_difficulty", 0),
                             ("engaged", False), ("samples", 3), ("start_hp", 80.0), ("outcome", ""),
                             ("focus", False), ("kind", "dojo-raid-observation")):
            with self.subTest(field=field):
                result = self.check(encounter_observation() | {field: value})
                self.assertFalse(result["ok"])
                self.assertFalse(result["killed"])

    def test_the_roster_must_be_ten_with_three_level_80_healers(self):
        o = encounter_observation()
        for members in (o["members"][:9], o["members"][1:] + [dict(o["members"][5])],
                        [dict(m, level=79) if i == 0 else m for i, m in enumerate(o["members"])],
                        [dict(m, is_heal=False) if i == 1 else m for i, m in enumerate(o["members"])]):
            result = self.check(o | {"members": members})
            self.assertFalse(result["ok"] or result["killed"])

    def test_portal_trips_and_nightmare_stacks_are_summarised(self):
        o = encounter_observation(mode="before", outcome="wipe")
        o["members"][1] |= dict(dream_entries=3, dream_ms=55000, max_twisted_nightmares=14, deaths=1)
        summary = self.check(o)["summary"]
        self.assertEqual((summary["portal_entries"], summary["dream_s"]), (3, 55.0))
        self.assertEqual((summary["max_twisted_nightmares"], summary["healer_deaths"]), (14, 1))

    def test_encounter_replies_must_have_one_observation(self):
        for text in ("command succeeded", "", 'dojo-encounter {}\ndojo-encounter {}'):
            with self.assertRaises(WorldError):
                parse_encounter(text)
        self.assertEqual(parse_encounter('x\ndojo-encounter {"state":"idle"}\n'), {"state": "idle"})


class RaidProvenanceTest(unittest.TestCase):
    def test_reverted_sources_do_not_certify_old_dirty_builds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "modules").mkdir()
            for name in ("worldserver.conf", "modules/playerbots.conf", "modules/mod_dojo.conf", "modules/mod_leeroy.conf", "layout.json"):
                (root / name).write_text("fixture")
            (root / "worldserver.exe").write_bytes(b"fixture binary")
            revs = dict.fromkeys(("source", "core", "playerbots"), "a" * 40)
            configured = dict(raid_acceptance=True, variant="training", clean_revisions=revs,
                              **{k: {"head": v} for k, v in revs.items()})
            linked = dict(exit_code=0, problems=[], clean_revisions=revs, **revs,
                          artifacts={"worldserver.exe": {"sha256": hashlib.sha256(b"fixture binary").hexdigest()}})
            layout = SimpleNamespace(roots=dict.fromkeys((*revs, "build"), str(root)), bin=root,
                                     build_bin=root, configs=root, path=root / "layout.json")
            def git(argv, **kwargs):
                # Trees are now clean even for the earlier, unattested dirty build.
                return "" if "status" in argv else "a" * 40
            with patch("leeroy_dojo.realm.raids.subprocess.check_output", side_effect=git):
                for missing in ("configure", "link", "pending", None):
                    cfg, link = copy.deepcopy(configured), copy.deepcopy(linked)
                    if missing == "configure":
                        cfg.pop("clean_revisions")
                    elif missing == "link":
                        link.pop("clean_revisions")
                    pending = root / "dojo-build.pending.json"
                    if missing == "pending":
                        pending.write_text("{}")
                    else:
                        pending.unlink(missing_ok=True)
                    (root / "dojo-configure.json").write_text(json.dumps(cfg))
                    (root / "dojo-build.json").write_text(json.dumps(link))
                    with self.subTest(missing=missing):
                        if missing:
                            with self.assertRaises(LayoutError):
                                provenance(layout)
                        else:
                            self.assertEqual(provenance(layout)["revisions"], revs)


if __name__ == "__main__":
    unittest.main()
