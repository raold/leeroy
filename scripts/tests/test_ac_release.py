"""End-to-end release tests; never discovers or uses an installed service."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
BASH = os.environ.get("AC_RELEASE_TEST_BASH", "bash")


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="leeroy-release-tests-")
        cls.template = Path(cls.temp.name) / "template"
        cls.template.mkdir()
        cls.git_at(cls.template, "init", "-q", "-b", "main")
        cls.git_at(cls.template, "config", "user.name", "Release Test")
        cls.git_at(cls.template, "config", "user.email", "release@example.invalid")
        cls.git_at(cls.template, "config", "core.autocrlf", "false")
        cls.git_at(cls.template, "config", "commit.gpgsign", "false")
        files = {
            ".gitignore": "build/\nmind-data/\nvoice-data/\nactions.log\nstubs/\n.superpowers/\n__pycache__/\n",
            "mind/app.py": "original\n",
            "scripts/common.sh": (SCRIPTS / "common.sh").read_text(encoding="utf-8"),
            "scripts/ac-mind.py": '''import os, sys
from pathlib import Path
root = Path(os.environ["AC_ROOT"])
with (root / "actions.log").open("a") as log: log.write("mind " + sys.argv[1] + "\\n")
sys.exit(int(os.environ.get("FAIL_MIND_" + sys.argv[1].upper(), "0")))
''',
            "mind/leeroy_mind/__init__.py": "",
            "mind/leeroy_mind/__main__.py": '''import os, sqlite3, sys
from pathlib import Path
root = Path(os.environ["AC_ROOT"])
with (root / "actions.log").open("a") as log: log.write("migrate\\n")
with sqlite3.connect(root / "mind-data/mind.db") as db:
    db.execute("INSERT INTO marker VALUES ('migrated')")
sys.exit(int(os.environ.get("FAIL_MIGRATE", "0")))
''',
            "scripts/ac-server.sh": '''#!/usr/bin/env bash
printf 'server %s\\n' "$1" >> "$AC_ROOT/actions.log"
if [[ $1 == start ]]; then
    printf 'started binary: %s\\n' "$(cat "$AC_ROOT/build/bin/RelWithDebInfo/worldserver.exe")" >> "$AC_ROOT/actions.log"
fi
''',
            "scripts/ac-build.sh": '''#!/usr/bin/env bash
printf 'build jobs=%s\\n' "$AC_JOBS" >> "$AC_ROOT/actions.log"
printf 'new binary' > "$AC_ROOT/build/bin/RelWithDebInfo/worldserver.exe"
printf 'new dll' > "$AC_ROOT/build/bin/RelWithDebInfo/new.dll"
exit "${FAIL_BUILD:-0}"
''',
            "scripts/ac-health.sh": '''#!/usr/bin/env bash
printf 'health\\n' >> "$AC_ROOT/actions.log"
echo 'ok: synthetic health'
exit "${FAIL_HEALTH:-0}"
''',
        }
        for name, text in files.items():
            cls.write(cls.template / name, text)
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Base")
        cls.base = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.git_at(cls.template, "checkout", "-qb", "feature")
        cls.write(cls.template / "mind/feature.py", "feature\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Feature")
        cls.git_at(cls.template, "checkout", "-q", "main")
        cls.write(cls.template / "mind/main.py", "main\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Main")
        cls.git_at(cls.template, "merge", "--no-ff", "-qm", "Merge feature", "feature")
        cls.merge = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.write(cls.template / "mind/app.py", "released\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Single")
        cls.single = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.write(cls.template / ".gitattributes", "*.ttf binary\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Attributes")
        cls.attributes = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.write(cls.template / "modules/native.cpp", "native\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Native")
        cls.native = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.write(cls.template / "voice/feature.py", "voice change\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Voice")
        cls.voice = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.write(cls.template / "outside.txt", "outside\n")
        cls.git_at(cls.template, "add", ".")
        cls.git_at(cls.template, "commit", "-qm", "Disallowed")
        cls.disallowed = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.git_at(cls.template, "mv", "outside.txt", "mind/moved.txt")
        cls.git_at(cls.template, "commit", "-qm", "Disallowed rename source")
        cls.rename = cls.git_at(cls.template, "rev-parse", "HEAD")
        cls.git_at(cls.template, "update-ref", "refs/remotes/origin/main", "HEAD")
        cls.git_at(cls.template, "checkout", "-qb", "release/test", cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    @staticmethod
    def git_at(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE).strip()

    def git(self, *args):
        return self.git_at(self.root, *args)

    def setUp(self):
        self.root = Path(self.temp.name) / self._testMethodName / "install with spaces"
        shutil.copytree(self.template, self.root)
        self.bin = self.root / "build/bin/RelWithDebInfo"
        for name in ("worldserver.exe", "authserver.exe", "library.dll"):
            self.write(self.bin / name, "old binary")
        self.write(self.bin / "configs/worldserver.conf", 'CharacterDatabaseInfo = "127.0.0.1;3306;fake;SECRET_SENTINEL;acore_characters"\n')
        self.db = self.root / "mind-data/mind.db"
        self.db.parent.mkdir()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE guild_raid(raid_id INTEGER, ended REAL); CREATE TABLE meta(key TEXT, value TEXT); CREATE TABLE marker(value TEXT); INSERT INTO marker VALUES ('original');")
        stub = self.root / "stubs"
        self.write(stub / "mysql", '''#!/usr/bin/env bash
if [[ -n ${MYSQL_PAUSE:-} ]]; then
    touch "$MYSQL_PAUSE.ready"
    while [[ ! -e $MYSQL_PAUSE.go ]]; do sleep 0.05; done
fi
printf '%s\\n' "${OWNER_ONLINE-0}"
exit "${MYSQL_FAIL:-0}"
''')
        self.write(stub / "tasklist", '''#!/usr/bin/env bash
if [[ ${TASKLIST_OUTPUT+x} ]]; then printf '%s\\n' "$TASKLIST_OUTPUT";
else printf '%s\\n' '"System","4","Services","0","8 K"'; fi
exit "${TASKLIST_FAIL:-0}"
''')
        self.write(stub / "schtasks", '''#!/usr/bin/env bash
if [[ $1 == //Query ]]; then exit "${TASK_MISSING:-0}"; fi
printf 'console %s\\n' "$1" >> "$AC_ROOT/actions.log"
exit "${FAIL_CONSOLE:-0}"
''')
        for path in stub.iterdir():
            path.chmod(0o755)
        self.env = dict(os.environ, AC_ROOT=self.root.as_posix(),
                        AC_RELEASE_PYTHON=Path(sys.executable).as_posix(),
                        AC_RELEASE_MYSQL=(stub / "mysql").as_posix(),
                        LEEROY_MIND_DATA=self.db.parent.as_posix(),
                        LEEROY_VOICE_DATA=(self.root / "voice-data").as_posix(),
                        PATH=str(stub) + os.pathsep + os.environ["PATH"])

    def run_release(self, *args, rc=0, **env):
        result = subprocess.run([BASH, (SCRIPTS / "ac-release.sh").as_posix(), *args],
                                cwd=self.root, env=dict(self.env, **env), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
        self.assertEqual(result.returncode, rc, result.stdout)
        self.assertNotIn("SECRET_SENTINEL", result.stdout)
        self.assertIn("Rollback", result.stdout)
        return result.stdout

    def actions(self):
        path = self.root / "actions.log"
        return path.read_text() if path.exists() else ""

    def original(self):
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".git/CHERRY_PICK_HEAD").exists())
        self.assertFalse((self.root / ".git/ac-release.lock").exists())
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM marker").fetchall(), [("original",)])

    def test_dry_run_is_readonly(self):
        before = self.db.read_bytes()
        output = self.run_release("--dry-run", self.merge, self.single)
        self.assertIn("Plan: cherry-pick 2 commit(s)", output)
        self.assertIn(self.base, output)
        self.assertIn("pre-release-", output)
        self.assertEqual(self.actions(), "")
        self.assertFalse((self.db.parent / "snapshots").exists())
        self.assertEqual(self.db.read_bytes(), before)
        self.original()

    def test_owner_refusals(self):
        for env in ({"OWNER_ONLINE": "1"}, {"MYSQL_FAIL": "1"}, {"OWNER_ONLINE": ""}):
            self.assertIn("FAIL: owner", self.run_release(self.single, rc=1, **env))
        self.assertEqual(self.actions(), "")
        self.original()

    def test_branches_and_dirty_tree(self):
        self.git("checkout", "-q", "main")
        self.assertIn("found main", self.run_release(self.single, rc=1))
        self.git("checkout", "-q", "--detach", self.base)
        self.assertIn("found detached", self.run_release(self.single, rc=1))
        self.git("checkout", "-q", "release/test")
        self.write(self.root / "mind/app.py", "dirty\n")
        self.assertIn("working tree is dirty", self.run_release(self.single, rc=1))
        self.assertEqual(self.actions(), "")

    def test_raid_refusals_and_explicit_exceptions(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO guild_raid VALUES (2, NULL)")
            db.execute("INSERT INTO meta VALUES ('guild:offline:owned', ?)",
                       (json.dumps({"raid_id": 3, "phase": "running"}),))
        output = self.run_release(self.single, rc=1)
        self.assertIn("open guild raid 2", output)
        self.assertIn("open offline raid 3", output)
        self.run_release("--allow-raid", "2", self.single, rc=1)
        output = self.run_release("--allow-raid", "2", "--allow-raid", "3", "--dry-run", self.single)
        self.assertIn("allowed raid IDs: 2 3", output)
        self.assertIn("allowing guild raid 2", output)
        self.assertIn("allowing offline raid 3", output)
        self.original()

    def test_bad_raid_state_fails_closed(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO meta VALUES ('guild:offline:owned', 'PRIVATE_BROKEN_JSON')")
        output = self.run_release(self.single, rc=1)
        self.assertNotIn("PRIVATE_BROKEN_JSON", output)
        self.assertIn("raid state unavailable", output)
        self.assertEqual(self.actions(), "")

    def test_allowed_raids_release_and_terminal_owned_raid(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO guild_raid VALUES (2, NULL)")
            db.execute("INSERT INTO meta VALUES ('guild:offline:owned', ?)",
                       (json.dumps({"raid_id": 3, "phase": "complete"}),))
        output = self.run_release("--allow-raid", "2", self.single)
        self.assertIn("allowed raid IDs: 2", output)
        self.assertIn("release complete", output)

    def test_invalid_arguments_and_existing_release_lock(self):
        for args in ((), ("--allow-raid",), ("--allow-raid", "0", self.single), ("--unknown", self.single)):
            self.run_release(*args, rc=1)
        (self.root / ".git/ac-release.lock").mkdir()
        self.assertIn("checkout lock", self.run_release(self.single, rc=1))
        self.assertTrue((self.root / ".git/ac-release.lock").exists())
        self.assertEqual(self.actions(), "")

    def test_build_lock(self):
        self.write(self.root / ".superpowers/live-build.lock", "someone else")
        self.assertIn("build lock exists", self.run_release("--native", self.native, rc=1))
        self.assertEqual((self.root / ".superpowers/live-build.lock").read_text(), "someone else")
        self.original()

    def test_commits_and_paths(self):
        self.run_release("does-not-exist", rc=1)
        self.write(self.root / "mind/local.py", "local\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Local only")
        output = self.run_release("HEAD", rc=1)
        self.assertIn("not on origin/main", output)
        self.git("reset", "--hard", self.base)
        self.assertIn("outside.txt", self.run_release(self.disallowed, rc=1))
        self.assertIn("outside.txt", self.run_release(self.rename, rc=1))
        self.assertIn("requires --native", self.run_release(self.native, rc=1))
        self.run_release("--native", "--dry-run", self.native)
        self.original()

    def test_gitattributes_is_an_allowed_path(self):
        self.assertIn("allowed paths", self.run_release("--dry-run", self.attributes))

    def test_merge_and_single_success(self):
        output = self.run_release(self.merge, self.single)
        self.assertEqual((self.root / "mind/feature.py").read_text(), "feature\n")
        self.assertEqual((self.root / "mind/app.py").read_text(), "released\n")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertIn("release complete", output)
        self.assertEqual(self.actions().splitlines(), ["mind stop", "migrate", "mind start", "console //End", "console //Run", "health"])
        snapshot, = (self.db.parent / "snapshots").glob("*.db")
        with closing(sqlite3.connect(snapshot)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM marker").fetchall(), [("original",)])

    def test_a_commit_already_live_is_refused_before_any_change(self):
        self.run_release(self.single)
        before = self.git("rev-parse", "HEAD")
        output = self.run_release(self.single, rc=1)
        self.assertIn("already on the live branch", output)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_conflict_aborts_entire_batch(self):
        self.write(self.root / "mind/app.py", "conflict\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Local change")
        original = self.git("rev-parse", "HEAD")
        output = self.run_release(self.merge, self.single, rc=2)
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".git/CHERRY_PICK_HEAD").exists())
        self.assertFalse((self.root / "mind/feature.py").exists())
        self.assertIn(original, output)
        self.assertEqual(self.actions(), "")

    def test_printed_rollback_command_executes_with_spaced_paths(self):
        output = self.run_release(self.single)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root, env=self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()
        self.assertEqual(self.actions().count("console //Run"), 2)

    def test_wal_commits_are_checked_and_snapshotted(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO guild_raid VALUES (9, NULL)")
            db.execute("INSERT INTO marker VALUES ('wal')")
            db.commit()
            self.assertIn("open guild raid 9", self.run_release(self.single, rc=1))
            self.run_release("--allow-raid", "9", self.single)
            snapshot, = (self.db.parent / "snapshots").glob("*.db")
            with closing(sqlite3.connect(snapshot)) as saved:
                self.assertEqual(saved.execute("SELECT value FROM marker").fetchall(), [("original",), ("wal",)])

    def test_failed_build_restores_old_binaries(self):
        output = self.run_release("--native", self.native, rc=2, FAIL_BUILD="1")
        self.assertIn("old services restarted", output)
        self.assertIn("AC_JOBS=4", output)
        self.assertIn("build jobs=4", self.actions())
        self.assertIn("started binary: old binary", self.actions())
        self.assertFalse((self.bin / "new.dll").exists())
        self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")
        self.assertFalse((self.root / ".superpowers/live-build.lock").exists())
        self.assertNotIn("migrate", self.actions())
        self.original()

    def test_failed_migration_restores_database(self):
        self.run_release(self.single, rc=2, FAIL_MIGRATE="1")
        self.assertIn("migrate", self.actions())
        self.assertIn("mind start", self.actions())
        self.original()

    def test_failed_native_migration_restores_database_and_binaries(self):
        self.run_release("--native", self.native, rc=2, FAIL_MIGRATE="1")
        self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")
        self.assertIn("started binary: old binary", self.actions())
        self.original()

    def test_native_success_and_missing_console(self):
        output = self.run_release("--native", self.native, TASK_MISSING="1")
        self.assertIn("scheduled task is absent", output)
        self.assertIn("started binary: new binary", self.actions())
        self.assertLess(self.actions().index("build jobs=4"), self.actions().index("migrate"))
        self.assertLess(self.actions().index("migrate"), self.actions().index("server start"))

    def test_unhealthy_release_keeps_checkout_and_instructions(self):
        output = self.run_release(self.single, rc=2, FAIL_HEALTH="1")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertIn("post-release health", output)
        self.assertEqual(self.actions().count("mind stop"), 1)

    def test_stop_failure_leaves_recovery_instructions(self):
        output = self.run_release(self.single, rc=2, FAIL_MIND_STOP="1")
        self.assertIn("stop failed", output)
        self.assertNotIn("migrate", self.actions())
        self.assertNotIn("mind start", self.actions())

    def test_native_stop_requires_readable_process_absence(self):
        for env in ({"TASKLIST_OUTPUT": '"worldserver.exe","12","Console","1","100 K"'},
                    {"TASKLIST_OUTPUT": '"AUTHSERVER.EXE","13","Console","1","100 K"'},
                    {"TASKLIST_OUTPUT": ""}, {"TASKLIST_OUTPUT": "unavailable"},
                    {"TASKLIST_FAIL": "1"}):
            with self.subTest(env=env):
                self.git("reset", "--hard", self.base)
                output = self.run_release("--native", self.native, rc=2, **env)
                self.assertIn("FAIL: native stop", output)
                self.assertNotIn("build jobs=", self.actions())
                self.assertNotIn("migrate", self.actions())
                self.assertFalse((self.db.parent / "snapshots").exists())
                self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")

    def test_real_server_wrapper_cannot_hide_failed_termination(self):
        # Use the real best-effort wrapper, shadowing every Windows process tool.
        self.write(self.root / "scripts/ac-server.sh", (SCRIPTS / "ac-server.sh").read_text())
        self.git("add", "scripts/ac-server.sh")
        self.git("commit", "-qm", "Install real stop wrapper in synthetic fixture")
        for name, body in {
            "tasklist": '''#!/usr/bin/env bash
printf '%s\\n' '"worldserver.exe","12","Console","1","100 K"' '"authserver.exe","13","Console","1","100 K"'
''',
            "taskkill": '''#!/usr/bin/env bash
printf 'failed taskkill\\n' >> "$AC_ROOT/actions.log"
exit 1
''',
            "sleep": "#!/usr/bin/env bash\nexit 0\n",
        }.items():
            path = self.root / "stubs" / name
            self.write(path, body)
            path.chmod(0o755)
        output = self.run_release("--native", self.native, rc=2)
        self.assertGreaterEqual(self.actions().count("failed taskkill"), 4)
        self.assertNotIn("build jobs=", self.actions())
        self.assertNotIn("migrate", self.actions())
        self.assertNotIn("release complete", output)
        self.assertIn("stop failed", output)

    def test_printed_native_rollback_refuses_surviving_or_unknown_processes(self):
        output = self.run_release("--native", self.native)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        released = self.git("rev-parse", "HEAD")
        for env in ({"TASKLIST_OUTPUT": '"worldserver.exe","12","Console","1","100 K"'},
                    {"TASKLIST_FAIL": "1"}):
            result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
                env=dict(self.env, **env), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(self.git("rev-parse", "HEAD"), released)
            self.assertEqual(self.actions().count("build jobs=4"), 1)
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()

    def test_preflight_serializes_checkout_snapshot_with_other_releases(self):
        pause = self.root / "stubs/preflight"
        ready, proceed = Path(str(pause) + ".ready"), Path(str(pause) + ".go")
        process = subprocess.Popen([BASH, (SCRIPTS / "ac-release.sh").as_posix(), self.single],
            cwd=self.root, env=dict(self.env, MYSQL_PAUSE=pause.as_posix()), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            output = self.run_release(self.merge, rc=1)
            self.assertIn("checkout lock", output)
            self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
            self.assertEqual(self.actions(), "")
        finally:
            proceed.write_text("continue")
            output, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("release complete", output)
        self.assertEqual(self.actions().count("migrate"), 1)

    def test_voice_changes_refuse_a_running_service_without_changing_its_lock(self):
        path = self.root / "voice-data/voice.lock"
        path.parent.mkdir()
        with path.open("w+b") as fh:
            fh.write(b"lock"); fh.flush(); fh.seek(0)
            before = path.stat().st_mtime_ns
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                for args in (("--dry-run", self.voice), (self.voice,)):
                    output = self.run_release(*args, rc=1)
                    self.assertIn("voice service to be stopped first", output)
                    self.assertEqual(self.actions(), "")
                    self.original()
                self.assertEqual(path.stat().st_mtime_ns, before)
            finally:
                if os.name == "nt":
                    fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        self.assertEqual(path.read_bytes(), b"lock")
        self.assertIn("release complete", self.run_release(self.voice))

    def test_voice_changes_with_no_service_do_not_create_or_enable_one(self):
        output = self.run_release(self.voice)
        self.assertIn("voice service stopped for voice changes", output)
        self.assertFalse((self.root / "voice-data").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
