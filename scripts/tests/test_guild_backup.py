"""Synthetic-only guild backup regression tests. No installed service imports."""

from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "guild_backup.py"
spec = importlib.util.spec_from_file_location("guild_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class GuildBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guild-backup-self-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "install with spaces"
        self.mind = self.install / "mind-data"
        self.voice = self.install / "voice-data"
        self.destination = self.root / "pretend NAS"
        for path in (self.mind, self.voice, self.destination):
            path.mkdir(parents=True)
        for name in backup.DATABASES:
            with closing(sqlite3.connect(self.mind / name)) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO sample VALUES (1, 'invented fixture')")
        self.put("personas/1234/card.toml", b"name = 'Fixture'\n")
        self.put("personas/1234/versions/1.toml", b"revision = 1\n")
        self.put("voices/1234.ref.wav", b"synthetic reference")
        self.put("selfies/1234/sets/1/c0.jpg", b"synthetic picture" * 100)
        self.put("selfies/1234/chosen.jpg", b"synthetic picture" * 100)
        self.put("realm/plans/example.json", b"{}")
        for name in backup.VOICE_FILES:
            (self.voice / name).write_text("# invented\n", encoding="utf-8")
        self.config = self.root / "backup.toml"
        self.config.write_text("destination = " + json.dumps(str(self.destination)) + "\n", encoding="utf-8")
        # Explicit data paths override any developer's live LEEROY_* environment.
        self.args = ["--root", str(self.install), "--config", str(self.config),
                     "--mind-data", str(self.mind), "--voice-data", str(self.voice)]

    def put(self, name, content):
        target = self.mind / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *self.args, *args],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def store(self):
        return backup.open_store(self.destination, create=True)

    def latest(self):
        store, owner = self.store()
        return backup.list_sets(store, owner)[0][0][1]

    def fake_set(self, when, files=None):
        store, owner = self.store()
        name = "guild-" + when.strftime("%Y%m%dT%H%M%S%fZ") + "-aabbccdd"
        path = store / "sets" / name
        path.mkdir()
        files = files or {"mind-data/" + n: backup.digest(self.mind / n) for n in backup.DATABASES}
        data = backup.json_bytes(backup.metadata(owner, set_id=name, created_utc=when.isoformat(), files=files))
        backup.write_new(path / "manifest.json", data)
        backup.write_new(path / backup.SET_MARKER, backup.json_bytes(backup.metadata(
            owner, set_id=name, manifest_sha256=hashlib.sha256(data).hexdigest())))
        return path

    @contextmanager
    def writer(self):
        ready, stop = self.root / "ready", self.root / "stop"
        code = """
import sqlite3, sys, time
from pathlib import Path
db = sqlite3.connect(sys.argv[1], timeout=5)
db.execute('PRAGMA journal_mode=WAL')
Path(sys.argv[2]).touch()
while not Path(sys.argv[3]).exists():
    with db:
        db.execute("INSERT INTO sample(value) VALUES ('synthetic live write')")
    time.sleep(.002)
db.close()
"""
        writer = subprocess.Popen([sys.executable, "-c", code, str(self.mind / "mind.db"), str(ready), str(stop)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            self.assertIsNone(writer.poll())
            yield writer
        finally:
            stop.touch()
            try:
                writer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                writer.terminate()
                writer.wait(timeout=10)

    def test_roundtrip_with_live_sqlite_writer_and_game_dumps(self):
        game = self.install / "db_backup" / "manual_20260102_030405"
        game.mkdir(parents=True)
        for db in backup.GAME_DATABASES:
            (game / f"{db}.sql").write_text("-- invented dump\n", encoding="utf-8")
        with self.writer() as writer:
            self.run_cli("--with-db")
            self.assertIsNone(writer.poll())
            self.run_cli("--restore-test")
        output = self.root / "restored"
        self.run_cli("--restore-to", str(output))
        with closing(sqlite3.connect(output / "mind-data" / "mind.db")) as db, db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertGreater(db.execute("SELECT count(*) FROM sample").fetchone()[0], 1)
        manifest = self.latest()
        self.assertEqual(set(manifest["files"]), {
            p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()})
        for name, entry in manifest["files"].items():
            self.assertEqual(backup.digest(output / name), entry)
        self.assertEqual(len(list((output / "game-db").rglob("*.sql"))), 4)

    def test_exclusions_never_capture_control_keys_models_or_sidecars(self):
        excluded = ["control.json", "console.key", "nested/control.json", "nested/console.key",
                    "logs/private.log", "scratch/temp", "snapshots/old.db", "models/weights",
                    "cache/copy", "caches/copy", "nested/__pycache__/x", "mind.lock",
                    "nested/file.db-wal", "nested/file.db-shm", "nested/file.db-journal",
                    "selfies/1234/sets/2.part/c0.jpg", "incomplete.part"]
        for name in excluded:
            self.put(name, b"NEVER_COPY_SENTINEL")
        for name in ("models/weights", "cache/clip", "logs/private", "console.key", "control.json"):
            path = self.voice / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"NEVER_COPY_SENTINEL")
        result = self.run_cli()
        manifest = self.latest()
        self.assertTrue(all("mind-data/" + name not in manifest["files"] for name in excluded))
        self.assertEqual({name for name in manifest["files"] if name.startswith("voice-data/")},
                         {"voice-data/" + name for name in backup.VOICE_FILES})
        store, _ = self.store()
        for obj in (store / "objects").rglob("*"):
            if obj.is_file():
                self.assertNotIn(b"NEVER_COPY_SENTINEL", obj.read_bytes())
        self.assertNotIn("NEVER_COPY_SENTINEL", result.stdout + result.stderr)

    def test_incremental_reuses_unchanged_objects_without_hardlinks(self):
        self.run_cli()
        store, _ = self.store()
        before = {p: p.stat().st_mtime_ns for p in (store / "objects").rglob("*") if p.is_file()}
        self.run_cli()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        self.assertEqual(len(before), len([p for p in (store / "objects").rglob("*") if p.is_file()]))
        with closing(sqlite3.connect(self.mind / "mind.db")) as db, db:
            db.execute("INSERT INTO sample(value) VALUES ('second')")
        self.assertIn("1 new objects", self.run_cli().stdout)

    def test_file_changes_during_database_capture_fail_before_publication(self):
        stage = self.root / "stage"
        stage.mkdir()
        original = backup.sqlite_snapshot

        def change(source, target):
            original(source, target)
            self.put("personas/1234/card.toml", os.urandom(20))

        with patch.object(backup, "sqlite_snapshot", side_effect=change):
            with self.assertRaisesRegex(backup.BackupError, "Source files changed"):
                backup.capture(stage, self.mind, self.voice, None)

    def test_changed_source_retries_and_publishes_only_stable_capture(self):
        original = backup.sqlite_snapshot
        calls = []

        def change_once(source, target):
            original(source, target)
            if not calls:
                self.put("personas/1234/card.toml", b"stable after first attempt")
            calls.append(source)

        with patch.object(backup, "sqlite_snapshot", side_effect=change_once), redirect_stdout(io.StringIO()):
            self.assertEqual(backup.main(self.args), 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.latest()["files"]["mind-data/personas/1234/card.toml"],
                         backup.digest(self.mind / "personas/1234/card.toml"))

    def test_new_file_during_capture_is_detected(self):
        stage = self.root / "stage"
        stage.mkdir()
        original = backup.sqlite_snapshot

        def change(source, target):
            original(source, target)
            self.put("selfies/new.jpg", b"new")

        with patch.object(backup, "sqlite_snapshot", side_effect=change):
            with self.assertRaises(backup.BackupError):
                backup.capture(stage, self.mind, self.voice, None)

    def test_unreadable_subdirectory_cannot_be_silently_omitted(self):
        def unreadable_walk(_root, *, followlinks, onerror):
            onerror(PermissionError("synthetic unreadable subtree"))
            return iter(())

        with patch.object(backup.os, "walk", side_effect=unreadable_walk):
            with self.assertRaises(PermissionError):
                backup.inventory(self.mind, self.voice, None)

    def test_resumable_transfer_validates_prefix(self):
        source = self.put("large.jpg", b"invented" * (backup.BLOCK // 2))
        entry = backup.digest(source)
        store, _ = self.store()
        target = backup.object_path(store, entry["sha256"])
        target.parent.mkdir()
        part = target.with_name(target.name + ".part")
        for prefix in (source.read_bytes()[:12345], b"wrong prefix", source.read_bytes() + b"too long"):
            part.write_bytes(prefix)
            self.assertTrue(backup.transfer(source, store, entry))
            self.assertEqual(backup.digest(target), entry)
            self.assertFalse(part.exists())
            target.unlink()

    def test_interrupted_copy_resumes_and_never_publishes_a_set(self):
        source = self.put("large.jpg", b"invented" * backup.BLOCK)
        entry = backup.digest(source)
        store, owner = self.store()
        with patch.object(backup.os, "fsync", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                backup.transfer(source, store, entry)
        self.assertEqual(backup.list_sets(store, owner)[0], [])
        self.assertTrue(backup.transfer(source, store, entry))
        self.assertEqual(backup.digest(backup.object_path(store, entry["sha256"])), entry)

    def test_corrupt_existing_object_aborts_without_pruning(self):
        self.run_cli()
        store, owner = self.store()
        sets = list((store / "sets").iterdir())
        entry = next(iter(self.latest()["files"].values()))
        backup.object_path(store, entry["sha256"]).write_bytes(b"corrupt")
        result = subprocess.run([sys.executable, str(SCRIPT), *self.args], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((store / "sets").iterdir()), sets)
        with self.assertRaises(backup.BackupError):
            backup.restore(store, owner)

    def test_failed_publication_keeps_previous_newest_restorable(self):
        self.run_cli()
        store, owner = self.store()
        original = backup.write_new

        def fail_marker(path, data):
            if path.name == backup.SET_MARKER:
                raise OSError("synthetic interruption")
            original(path, data)

        with patch.object(backup, "write_new", side_effect=fail_marker), redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main(self.args), 1)
        sets, foreign = backup.list_sets(store, owner)
        self.assertEqual(len(sets), 1)
        self.assertTrue(foreign)
        self.assertGreater(backup.restore(store, owner), 2)

    def test_restore_refuses_existing_output_and_removes_failed_partial(self):
        self.run_cli()
        store, owner = self.store()
        output = self.root / "restore"
        output.mkdir()
        sentinel = output / "leave alone"
        sentinel.write_bytes(b"foreign")
        with self.assertRaises(backup.BackupError):
            backup.restore(store, owner, output)
        self.assertEqual(sentinel.read_bytes(), b"foreign")
        entry = next(iter(self.latest()["files"].values()))
        backup.object_path(store, entry["sha256"]).write_bytes(b"bad")
        with self.assertRaises(backup.BackupError):
            backup.restore(store, owner, self.root / "new restore")
        self.assertFalse((self.root / "new restore").exists())
        self.assertFalse(list(self.root.glob("leeroy-guild-restore-*")))

    def test_manifest_tampering_and_unsafe_names_are_rejected(self):
        self.run_cli()
        store, owner = self.store()
        path = backup.list_sets(store, owner)[0][0][0]
        manifest_path = path / "manifest.json"
        good = manifest_path.read_bytes()
        manifest_path.write_bytes(good.replace(b"invented-not-present", b"x") + b" ")
        with self.assertRaises(backup.BackupError):
            backup.read_set(path, owner)
        manifest_path.write_bytes(good)
        for name in ("../escape", "/absolute", "mind-data/../escape", "mind-data/x:stream",
                     "mind-data/control.json", "mind-data/console.key", "mind-data/a\\b",
                     "mind-data/a/./b", "mind-data/trailing.", "mind-data/logs/no",
                     "mind-data/NUL.txt", "mind-data/bad?.jpg"):
            self.assertFalse(backup.valid_payload_name(name), name)

    def test_metadata_corruption_is_rejected_before_set_is_published(self):
        store, owner = self.store()
        original = backup.write_new

        def corrupt_manifest(path, data):
            original(path, data + b" " if path.name == "manifest.json" else data)

        with patch.object(backup, "write_new", side_effect=corrupt_manifest), redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main(self.args), 1)
        self.assertEqual(backup.list_sets(store, owner)[0], [])
        self.assertFalse(any(backup.SET_RE.fullmatch(p.name) for p in (store / "sets").iterdir()))

    def test_sqlite_corruption_detected_even_with_matching_manifest_hash(self):
        self.run_cli()
        store, owner = self.store()
        sets, _ = backup.list_sets(store, owner)
        path, manifest = sets[0]
        invalid = self.root / "bad sqlite"
        invalid.write_bytes(b"not a sqlite database")
        entry = backup.digest(invalid)
        backup.transfer(invalid, store, entry)
        manifest["files"]["mind-data/mind.db"] = entry
        data = backup.json_bytes(manifest)
        (path / "manifest.json").write_bytes(data)
        marker = backup.read_json(path / backup.SET_MARKER)
        marker["manifest_sha256"] = hashlib.sha256(data).hexdigest()
        (path / backup.SET_MARKER).write_bytes(backup.json_bytes(marker))
        with self.assertRaises(sqlite3.DatabaseError):
            backup.restore(store, owner)

    def test_retention_keeps_daily_and_weekly_buckets(self):
        now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        for days in range(70):
            self.fake_set(now - timedelta(days=days))
        # Multiple same-day runs do not consume the daily count.
        self.fake_set(now - timedelta(hours=1))
        store, owner = self.store()
        sets, _ = backup.list_sets(store, owner)
        keep = backup.retained_names(sets, 7, 8)
        self.assertEqual(len(keep), 14)  # newest weekly also occupies one daily slot
        self.assertEqual(backup.prune(store, owner, 7, 8), 57)
        left = backup.list_sets(store, owner)[0]
        self.assertEqual({p.name for p, _ in left}, keep)

    def test_retention_preserves_foreign_dirs_wrong_markers_links_and_extra_files(self):
        now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.fake_set(now)
        old = self.fake_set(now - timedelta(days=1))
        foreign = self.fake_set(now - timedelta(days=2))
        (foreign / backup.SET_MARKER).write_text('{}', encoding="utf-8")
        extra = self.fake_set(now - timedelta(days=3))
        (extra / "foreign.txt").write_bytes(b"keep")
        store, owner = self.store()
        manual = store / "sets" / "manual_20260101_010101"
        manual.mkdir()
        (manual / "keep").write_bytes(b"keep")
        outside = self.destination / "older-backups"
        outside.mkdir()
        (outside / "keep").write_bytes(b"keep")
        self.assertEqual(backup.prune(store, owner, 1, 0), 1)
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in (foreign, extra, manual, outside)))

    def test_retention_collects_only_unreferenced_objects_from_pruned_owned_sets(self):
        store, owner = self.store()
        now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        shared = backup.digest(self.mind / "mind.db")
        backup.transfer(self.mind / "mind.db", store, shared)
        oldfile = self.put("old.jpg", b"old")
        old = backup.digest(oldfile)
        backup.transfer(oldfile, store, old)
        orphanfile = self.put("orphan.jpg", b"orphan")
        orphan = backup.digest(orphanfile)
        backup.transfer(orphanfile, store, orphan)
        base = {"mind-data/" + n: shared for n in backup.DATABASES}
        self.fake_set(now, base)
        self.fake_set(now - timedelta(days=1), {**base, "mind-data/old.jpg": old})
        backup.prune(store, owner, 1, 0)
        self.assertTrue(backup.object_path(store, shared["sha256"]).exists())
        self.assertFalse(backup.object_path(store, old["sha256"]).exists())
        self.assertTrue(backup.object_path(store, orphan["sha256"]).exists())

    def test_unknown_set_disables_object_collection(self):
        self.run_cli()
        store, owner = self.store()
        (store / "sets" / "foreign").mkdir()
        before = {p for p in (store / "objects").rglob("*") if p.is_file()}
        self.fake_set(datetime(2026, 1, 1, tzinfo=timezone.utc))
        backup.prune(store, owner, 1, 0)
        self.assertTrue(all(p.exists() for p in before))

    def test_symlinks_and_windows_junctions_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"NEVER_COPY_SENTINEL")
        link = self.mind / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(backup.BackupError):
                backup.inventory(self.mind, self.voice, None)
            with self.assertRaises(backup.BackupError):
                backup.safe_path(link / "secret")
        finally:
            # Remove only the generated link itself; never recurse through it.
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()
        self.assertTrue((outside / "secret").exists())

    def test_destination_cannot_overlap_source(self):
        self.config.write_text("destination = " + json.dumps(str(self.mind)) + "\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main(self.args), 1)
        self.assertFalse((self.mind / backup.STORE).exists())

    def test_restore_target_cannot_be_in_live_source(self):
        self.run_cli()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main([*self.args, "--restore-to", str(self.mind / "new")]), 1)
        self.assertFalse((self.mind / "new").exists())

    def test_unowned_store_is_not_adopted(self):
        (self.destination / backup.STORE).mkdir()
        with self.assertRaises(FileNotFoundError):
            self.store()
        self.assertEqual(list((self.destination / backup.STORE).iterdir()), [])

    def test_configuration_validation_and_errors_do_not_leak_contents(self):
        for value in ('destination = "PRIVATE_SENTINEL"\n',
                      'destination = "PRIVATE_SENTINEL"\nkeep_daily = -1\n',
                      'destination = "PRIVATE_SENTINEL"\npassword = "PRIVATE_SENTINEL"\n',
                      'PRIVATE_SENTINEL = [ malformed'):
            self.config.write_text(value, encoding="utf-8")
            out = io.StringIO()
            with redirect_stderr(out):
                self.assertEqual(backup.main(self.args), 1)
            self.assertNotIn("PRIVATE_SENTINEL", out.getvalue())

    def test_missing_or_incomplete_newest_game_set_fails(self):
        game = self.install / "db_backup"
        older = game / "manual_20260101_000000"
        older.mkdir(parents=True)
        for db in backup.GAME_DATABASES:
            (older / f"{db}.sql").write_bytes(b"-- invented")
        newer = game / "manual_20260102_000000"
        newer.mkdir()
        self.assertEqual(backup.newest_game_set(game), newer)
        with self.assertRaises(FileNotFoundError):
            backup.inventory(self.mind, self.voice, newer)
        (newer / "acore_auth.err").write_bytes(b"PRIVATE_SENTINEL")
        with self.assertRaisesRegex(backup.BackupError, "dump errors"):
            backup.inventory(self.mind, self.voice, newer)

    def test_store_lock_blocks_second_process_and_recovers_after_exit(self):
        store, _ = self.store()
        with backup.store_lock(store):
            result = subprocess.run([sys.executable, str(SCRIPT), *self.args], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("busy", result.stderr)
        self.run_cli()


if __name__ == "__main__":
    unittest.main(verbosity=2)
