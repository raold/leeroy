import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from leeroy_dojo.realm import campaign, raids, vehicles, yogg
from leeroy_dojo.realm.layout import LayoutError
from test_raids import observation as raid_observed
from test_vehicle_gate import drakes
from test_yogg_gate import observed


class CampaignTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = SimpleNamespace(data=Path(self.temp.name))
        self.source = dict(revisions={"source": "candidate"}, worldserver_sha256="binary",
                           layout_sha256="layout", configs={"worldserver.conf": "config"})
        self.running = patch.object(campaign.world, "running", return_value=False).start()
        self.provenance = patch.object(campaign.raids, "provenance", side_effect=lambda _: copy.deepcopy(self.source)).start()
        self.addCleanup(patch.stopall)
        self.calls = 0

    def child(self, layout, gate, log):
        self.calls += 1
        out = layout.data / "runs" / f"child-{self.calls}"
        out.mkdir()
        cases = []
        for negative in (True, False):
            mode = "negative" if negative else "normal"
            if gate == "yogg":
                o = observed(negative)
                o["illusion"] = (self.calls - 1) % 3 + 1
                family, name, validate = "yogg", mode, yogg.verdict
                options = {}
            else:
                o = raid_observed(gate, negative) if gate == "poison" else drakes(negative)
                family, name = ("raid" if gate == "poison" else "vehicle"), f"{gate}-{mode}"
                validate = raids.verdict if gate == "poison" else vehicles.verdict
                options = {"scenario": gate}
            o["token"] = f"token-{self.calls}-{negative}"
            cases.append(dict(name=name, source=copy.deepcopy(self.source), observation=o,
                              **validate(o, token=o["token"], negative=negative, **options)))
        report = dict(kind=f"dojo-{family}-acceptance", format=1, ok=True, stopped=True,
                      source=copy.deepcopy(self.source), out=str(out), cases=cases)
        return self.save(report)

    def save(self, report):
        (Path(report["out"]) / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    def run_campaign(self, factory=None):
        with patch.object(campaign, "_execute", side_effect=factory or self.child):
            return campaign.run(self.layout, gate="yogg", runs=3, log=lambda _: None)

    def test_requires_three_complete_pairs_and_actual_room_coverage(self):
        result = self.run_campaign()
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls, 3)
        self.assertEqual(result["illusion_counts"], {"1": 1, "2": 1, "3": 1})
        for row in result["runs"]:
            self.assertEqual(row["report_sha256"], raids.fingerprint(Path(row["out"]) / "report.json"))

    def test_raid_and_vehicle_pairs_have_the_same_campaign_requirements(self):
        with patch.object(campaign, "_execute", side_effect=self.child):
            for gate in ("poison", "malygos"):
                result = campaign.run(self.layout, gate=gate, runs=3, log=lambda _: None)
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(result["runs"]), 3)

    def test_one_room_repeated_does_not_certify_all_yogg_rooms(self):
        def same(*args):
            report = self.child(*args)
            for case in report["cases"]:
                case["observation"]["illusion"] = 1
            return self.save(report)
        result = self.run_campaign(same)
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls, 3)
        self.assertEqual(result["illusion_counts"], {"1": 3, "2": 0, "3": 0})

    def test_behavior_and_plumbing_failures_stop_without_retry(self):
        for field, value, category in (("brain_hits", 0, "behavior_problems"),
                                       ("max_gap_ms", 201, "plumbing_problems"),
                                       ("cleanup", False, "plumbing_problems")):
            with self.subTest(field=field):
                before = self.calls
                def failed(*args):
                    report = self.child(*args)
                    case = report["cases"][1]
                    case["observation"][field] = value
                    case.update(yogg.verdict(case["observation"], token=case["observation"]["token"]))
                    report["ok"] = False
                    return self.save(report)
                result = self.run_campaign(failed)
                self.assertFalse(result["ok"])
                self.assertEqual(self.calls, before + 1)
                self.assertTrue(result[category])
                self.assertTrue(Path(result["runs"][0]["out"]).is_dir())

    def test_source_binary_config_and_layout_changes_fail(self):
        for key in self.source:
            with self.subTest(key=key):
                def changed(*args):
                    report = self.child(*args)
                    self.source[key] = {"changed": True} if isinstance(self.source[key], dict) else "changed"
                    return report
                result = self.run_campaign(changed)
                self.assertFalse(result["ok"])
                self.assertIn("run 1: candidate changed during run", result["plumbing_problems"])

    def test_old_receipt_and_duplicate_native_tokens_fail(self):
        result = self.run_campaign()
        old = json.loads((Path(result["runs"][0]["out"]) / "report.json").read_text())
        result = self.run_campaign(lambda *_: old)
        self.assertFalse(result["ok"])
        self.assertTrue(any("reused" in p for p in result["plumbing_problems"]))
        def duplicate(*args):
            report = self.child(*args)
            report["cases"][1]["observation"]["token"] = report["cases"][0]["observation"]["token"]
            return self.save(report)
        self.assertFalse(self.run_campaign(duplicate)["ok"])

    def test_pass_boolean_cannot_hide_bad_evidence_or_missing_control(self):
        for mutation in (lambda r: r["cases"][1]["observation"].update(brain_hits=0),
                         lambda r: r["cases"].pop(0), lambda r: r.update(stopped=False)):
            def forged(*args):
                report = self.child(*args)
                mutation(report)
                return self.save(report)
            self.assertFalse(self.run_campaign(forged)["ok"])

    def test_refuses_single_runs_and_does_not_adopt_a_running_realm(self):
        with self.assertRaises(LayoutError):
            campaign.run(self.layout, gate="yogg", runs=1)
        self.running.return_value = True
        with self.assertRaises(campaign.world.WorldError):
            self.run_campaign()


class FailureClassificationTest(unittest.TestCase):
    def test_raid_and_vehicle_report_both_cadence_and_behavior(self):
        sample = raid_observed("poison")
        sample.update(max_gap_ms=201, poison_hits=1)
        result = raids.verdict(sample, scenario="poison", token=sample["token"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["plumbing_problems"])
        self.assertTrue(result["behavior_problems"])
        sample = drakes()
        sample.update(max_gap_ms=201, damage_hits=[0, 0])
        result = vehicles.verdict(sample, scenario="malygos", token=sample["token"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["plumbing_problems"])
        self.assertTrue(result["behavior_problems"])
