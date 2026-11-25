"""Offline milestones require the named boss, instance and real job, never a request ack."""
import copy
import unittest
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from leeroy_dojo.realm.offline import reached
from leeroy_dojo.realm.offline_fixture import FixtureCheckpoint
from leeroy_dojo.realm.world import WorldError


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


class OfflineCheckpointTest(unittest.TestCase):
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
                data = dict(format=1, instance="test", schemas=dict(layout.schemas), files=files, fixture={})
                if changed == "instance": data["instance"] = "somewhere-else"
                elif changed == "schema": data["schemas"]["characters"] = "foreign_characters"
                else: (checkpoint.root / f"{changed}.sql").write_text("SELECT 2;", encoding="utf-8")
                checkpoint.receipt.write_text(json.dumps(data), encoding="utf-8")
                with patch.object(checkpoint, "_client"), \
                        patch("leeroy_dojo.realm.offline_fixture.subprocess.run") as execute:
                    with self.assertRaises(WorldError):
                        checkpoint.restore()
                    execute.assert_not_called()
