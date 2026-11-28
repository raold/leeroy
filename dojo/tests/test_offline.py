"""Offline milestones require the named boss, instance and real job, never a request ack."""
import copy
import unittest
import json
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from leeroy_dojo.realm.offline import reached, retain_runtime_logs
from leeroy_dojo.realm.offline_fixture import FixtureCheckpoint, validate_training
from leeroy_dojo.realm.world import WorldError


class OfflineMilestoneTest(unittest.TestCase):
    def test_preboot_failure_keeps_no_previous_runtime_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            logs, out = root / "logs", root / "out"
            logs.mkdir()
            out.mkdir()
            for name in ("Server.log", "DungeonClear.log", "Playerbots.log", "WormRoutes.log"):
                (logs / name).write_text("previous trial\n")
            self.assertEqual(retain_runtime_logs(SimpleNamespace(logs=logs), out, None), {})
            self.assertEqual(list(out.iterdir()), [])

    def test_retained_logs_are_fresh_and_hash_the_saved_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            logs, out = root / "logs", root / "out"
            logs.mkdir()
            out.mkdir()
            old, fresh = logs / "DungeonClear.log", logs / "Server.log"
            old.write_bytes(b"old milestone\n")
            fresh.write_bytes(b"new startup\n")
            os.utime(old, (1000, 1000))
            os.utime(fresh, (1002, 1002))
            expected = {"Server.log": hashlib.sha256(fresh.read_bytes()).hexdigest()}
            self.assertEqual(retain_runtime_logs(SimpleNamespace(logs=logs), out, 1001), expected)
            self.assertEqual([p.name for p in out.iterdir()], ["Server.log"])
            self.assertEqual((out / "Server.log").read_bytes(), fresh.read_bytes())

    def test_worm_route_evidence_is_retained_only_from_this_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            logs, out = root / "logs", root / "out"
            logs.mkdir()
            out.mkdir()
            trace = logs / "WormRoutes.log"
            trace.write_bytes(b"worm_move guid=1 move=2 spline=3\n")
            os.utime(trace, (1000, 1000))
            self.assertEqual(retain_runtime_logs(SimpleNamespace(logs=logs), out, 1001), {})
            os.utime(trace, (1002, 1002))
            expected = {"WormRoutes.log": hashlib.sha256(trace.read_bytes()).hexdigest()}
            self.assertEqual(retain_runtime_logs(SimpleNamespace(logs=logs), out, 1001), expected)
            self.assertEqual((out / trace.name).read_bytes(), trace.read_bytes())

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


class OfflineCheckpointTest(unittest.TestCase):
    @staticmethod
    def trained_fixture():
        return dict(fixture_preparation_version=2, trained_ranks=[
            dict(slot=slot, base=11366, expected=42891, known=True) for slot in (6, 12)])

    def test_training_requires_current_actual_rank_evidence_for_both_mages(self):
        validate_training(self.trained_fixture())
        cases = [{}, dict(fixture_preparation_version=1), dict(fixture_preparation_version=True)]
        for change in ("unknown", "rank_one", "missing_mage", "duplicate", "invalid_slot"):
            fixture = self.trained_fixture()
            if change == "unknown": fixture["trained_ranks"][0]["known"] = False
            elif change == "rank_one": fixture["trained_ranks"][0]["expected"] = 11366
            elif change == "missing_mage": fixture["trained_ranks"].pop()
            elif change == "duplicate": fixture["trained_ranks"].append(dict(fixture["trained_ranks"][0]))
            else: fixture["trained_ranks"][0]["slot"] = True
            cases.append(fixture)
        for fixture in cases:
            with self.subTest(fixture=fixture), self.assertRaises(WorldError):
                validate_training(fixture)

    def test_untrained_checkpoint_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            layout = SimpleNamespace(secrets=Path(folder), instance="test",
                schemas={kind: "dojo_" + kind for kind in FixtureCheckpoint.KINDS})
            checkpoint = FixtureCheckpoint(layout)
            checkpoint.root.mkdir()
            checkpoint.receipt.write_text(json.dumps(dict(format=1, instance="test",
                schemas=dict(layout.schemas), files={}, fixture={})))
            with patch.object(checkpoint, "_client"), patch("leeroy_dojo.realm.offline_fixture.subprocess.run") as execute:
                with self.assertRaisesRegex(WorldError, "predates trained"):
                    checkpoint.restore()
                execute.assert_not_called()

    def test_live_world_refuses_before_database_access(self):
        with patch("leeroy_dojo.realm.offline_fixture.world.running", return_value=True), \
                patch("leeroy_dojo.realm.offline_fixture.mysql.Client") as database:
            checkpoint = FixtureCheckpoint(SimpleNamespace(secrets=Path("unused")))
            with self.assertRaisesRegex(WorldError, "stopped"):
                checkpoint.restore()
            database.assert_not_called()

    def test_changed_or_foreign_checkpoint_imports_nothing(self):
        for changed in ("auth", "characters", "playerbots", "instance", "schema"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                layout = SimpleNamespace(secrets=Path(folder), instance="test",
                    schemas={kind: "dojo_" + kind for kind in FixtureCheckpoint.KINDS})
                checkpoint = FixtureCheckpoint(layout)
                checkpoint.root.mkdir()
                files = {}
                for kind in checkpoint.KINDS:
                    path = checkpoint.root / f"{kind}.sql"
                    path.write_text("SELECT 1;", encoding="utf-8")
                    files[kind] = checkpoint.digest(path)
                data = dict(format=1, instance="test", schemas=dict(layout.schemas), files=files,
                    fixture=self.trained_fixture())
                if changed == "instance": data["instance"] = "somewhere-else"
                elif changed == "schema": data["schemas"]["characters"] = "foreign_characters"
                else: (checkpoint.root / f"{changed}.sql").write_text("SELECT 2;", encoding="utf-8")
                checkpoint.receipt.write_text(json.dumps(data), encoding="utf-8")
                with patch.object(checkpoint, "_client"), \
                        patch("leeroy_dojo.realm.offline_fixture.subprocess.run") as execute:
                    with self.assertRaises(WorldError):
                        checkpoint.restore()
                    execute.assert_not_called()
