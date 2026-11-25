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
            ".gitignore": "build/\nazerothcore-wotlk/\nmind-data/\nvoice-data/\nactions.log\nstubs/\n.superpowers/\n__pycache__/\n",
            "mind/app.py": "original\n",
            "scripts/common.sh": (SCRIPTS / "common.sh").read_text(encoding="utf-8"),
            "scripts/ac-mind.py": '''import os, sys
from pathlib import Path
root = Path(os.environ["AC_ROOT"])
with (root / "actions.log").open("a") as log: log.write("mind " + sys.argv[1] + "\\n")
sys.exit(int(os.environ.get("FAIL_MIND_" + sys.argv[1].upper(), "0")))
''',
            "scripts/ac-soap.py": "import sys\nsys.exit(0)\n",
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
case "${BUILD_OWNER_CHANGE:-}" in
    commit)
        printf 'owner work\\n' > "$AC_ROOT/mind/owner.py"
        git -C "$AC_ROOT" add mind/owner.py
        git -C "$AC_ROOT" commit -qm 'Owner work during build'
        ;;
    dirty) printf 'owner edit\\n' > "$AC_ROOT/mind/app.py" ;;
esac
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
        # common.sh's server listing (ac_server_processes); by default no server runs.
        self.write(stub / "powershell", '''#!/usr/bin/env bash
if [[ ${PROCESSES_OUTPUT+x} ]]; then printf '%s\\n' "$PROCESSES_OUTPUT";
else printf 'end\\r\\n'; fi
exit "${PROCESSES_FAIL:-0}"
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
        result = subprocess.run([BASH, getattr(self, "runner", SCRIPTS / "ac-release.sh").as_posix(), *args],
                                cwd=self.root, env=dict(self.env, **env), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
        self.assertEqual(result.returncode, rc, result.stdout)
        self.assertNotIn("SECRET_SENTINEL", result.stdout)
        self.assertIn("Rollback", result.stdout)
        return result.stdout

    def publish(self, files, *, parent=None, message="Incoming release"):
        """Put a synthetic commit on origin/main without changing the release branch."""
        branch = self.git("symbolic-ref", "--short", "HEAD")
        self.sequence = getattr(self, "sequence", 0) + 1
        self.git("checkout", "-qb", f"incoming-{self.sequence}", parent or "origin/main")
        for name, content in files.items():
            path = self.root / name
            if content is None:
                path.unlink()
            else:
                self.write(path, content)
        self.git("add", "-A", "--", *files)
        self.git("commit", "-qm", message)
        commit = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", commit)
        self.git("checkout", "-q", branch)
        return commit

    def install_runner(self):
        """Exercise the real entry point inside the checkout it is about to replace."""
        paths = [SCRIPTS / "ac-release.sh", *SCRIPTS.glob("ac-release-*.py")]
        for path in paths:
            self.write(self.root / "scripts" / path.name, path.read_text(encoding="utf-8"))
        self.git("add", "scripts")
        self.git("commit", "-qm", "Install release runner in fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.runner = self.root / "scripts/ac-release.sh"

    PIN = """bool RandomPlayerbotMgr::ProcessBot(Player* bot)
{
    uint32 botId = bot->GetGUID().GetCounter();
    if (_pinKeep && IsPinned(botId))
        return ProcessPinnedBot(bot);
    if (bot->isDead())
        return false;
    // leave group if leader is rndbot
    if (bot->GetGroup())
        return false;
    return true;
}
"""

    def module(self, name):
        repo = self.root / "azerothcore-wotlk/modules" / name
        repo.mkdir(parents=True)
        self.git_at(repo, "init", "-q", "-b", "main")
        for key, value in (("user.name", "Release Test"), ("user.email", "release@example.invalid"),
                           ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.git_at(repo, "config", key, value)
        self.write(repo / "payload.txt", "base\n")
        if name == "mod-playerbots":
            self.write(repo / "src/Bot/RandomPlayerbotMgr.cpp", self.PIN)
        self.git_at(repo, "add", ".")
        self.git_at(repo, "commit", "-qm", "Module base")
        return repo, self.git_at(repo, "rev-parse", "HEAD")

    def series(self, repo, changes):
        source = self.root.parent / (repo.name + "-patch-source")
        shutil.copytree(repo, source)
        patches = {}
        for number, files in enumerate(changes, 1):
            for name, text in files.items():
                self.write(source / name, text)
            self.git_at(source, "add", ".")
            self.git_at(source, "commit", "-qm", f"Module step {number}")
            patches[f"patches/{repo.name}/{number:04d}-step.patch"] = self.git_at(
                source, "format-patch", "-1", "--stdout") + "\n"
        return patches

    def module_original(self, repo, head):
        self.assertEqual(self.git_at(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git_at(repo, "status", "--porcelain"), "")
        self.assertEqual(self.git_at(repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertFalse((repo / ".git/rebase-apply").exists())

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
        self.assertIn("cherry-pick 2 commit(s)", output)
        self.assertIn(self.base, output)
        self.assertIn("pre-release-", output)
        self.assertNotIn("reset --hard", output)
        self.assertFalse(any(line.startswith("  cd ") for line in output.splitlines()))
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
        output = self.run_release(self.single, rc=1)
        self.assertIn("working tree is dirty", output)
        self.assertNotIn("reset --hard", output)
        self.assertFalse(any(line.startswith("  cd ") for line in output.splitlines()))
        self.assertEqual((self.root / "mind/app.py").read_text(), "dirty\n")
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

    def test_a_commit_already_live_is_skipped_without_restarting_services(self):
        self.run_release(self.single)
        before = self.git("rev-parse", "HEAD")
        actions = self.actions()
        snapshots = sorted((self.db.parent / "snapshots").iterdir())
        output = self.run_release(self.single)
        self.assertIn("skip", output.lower())
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.actions(), actions)
        self.assertEqual(sorted((self.db.parent / "snapshots").iterdir()), snapshots)

    def test_conflict_aborts_entire_batch(self):
        self.write(self.root / "mind/app.py", "conflict\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Local change")
        original = self.git("rev-parse", "HEAD")
        output = self.run_release(self.merge, self.single, rc=1)
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

    DOJO = r"F:\azerothcore\dojo-runtime\instances\claude-raids\bin"

    def processes(self, *rows):
        """common.sh's server listing: (pid, image, path) rows, then its end line. PIDs
        are odd: real Windows PIDs are multiples of 4."""
        return "\r\n".join([*("\t".join(row) for row in rows), "end"])

    def test_native_stop_requires_readable_process_absence(self):
        live = str(self.bin / "authserver.exe").upper()
        for env in ({"PROCESSES_OUTPUT": self.processes(("1001", "worldserver.exe", str(self.bin / "worldserver.exe")))},
                    {"PROCESSES_OUTPUT": self.processes(("1003", "AUTHSERVER.EXE", live.replace("\\", "/")))},
                    {"PROCESSES_OUTPUT": self.processes(("1005", "worldserver.exe", ""))},
                    {"PROCESSES_OUTPUT": self.processes(("x", "worldserver.exe", self.DOJO + r"\worldserver.exe"))},
                    {"PROCESSES_OUTPUT": ""}, {"PROCESSES_OUTPUT": "unavailable"},
                    {"PROCESSES_OUTPUT": "\t".join(("2001", "worldserver.exe", self.DOJO + r"\worldserver.exe"))},
                    {"PROCESSES_FAIL": "1"}):
            with self.subTest(env=env):
                self.git("reset", "--hard", self.base)
                output = self.run_release("--native", self.native, rc=2, **env)
                self.assertIn("FAIL: native stop", output)
                self.assertNotIn("build jobs=", self.actions())
                self.assertNotIn("migrate", self.actions())
                # Durable control scripts may exist, but no database or binaries
                # can be copied until native process absence was confirmed.
                snapshots = self.db.parent / "snapshots"
                self.assertEqual(list(snapshots.glob("*.db")), [])
                self.assertEqual(list(snapshots.glob("*-binaries")), [])
                self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")

    def test_real_server_wrapper_cannot_hide_failed_termination(self):
        # Use the real best-effort wrapper, shadowing every Windows process tool.
        self.write(self.root / "scripts/ac-server.sh", (SCRIPTS / "ac-server.sh").read_text())
        self.git("add", "scripts/ac-server.sh")
        self.git("commit", "-qm", "Install real stop wrapper in synthetic fixture")
        for name, body in {
            "taskkill": '''#!/usr/bin/env bash
printf 'failed taskkill %s\\n' "$*" >> "$AC_ROOT/actions.log"
exit 1
''',
            "sleep": "#!/usr/bin/env bash\nexit 0\n",
        }.items():
            path = self.root / "stubs" / name
            self.write(path, body)
            path.chmod(0o755)
        listing = self.processes(("1001", "worldserver.exe", str(self.bin / "worldserver.exe")),
                                 ("1003", "authserver.exe", str(self.bin / "authserver.exe")),
                                 ("2001", "worldserver.exe", self.DOJO + r"\worldserver.exe"))
        output = self.run_release("--native", self.native, rc=2, PROCESSES_OUTPUT=listing)
        self.assertGreaterEqual(self.actions().count("failed taskkill"), 4)
        self.assertIn("failed taskkill //F //PID 1001", self.actions())
        self.assertIn("failed taskkill //F //PID 1003", self.actions())
        self.assertNotIn("2001", self.actions())
        self.assertNotIn("//IM", self.actions())
        self.assertNotIn("build jobs=", self.actions())
        self.assertNotIn("migrate", self.actions())
        self.assertNotIn("release complete", output)
        self.assertIn("stop failed", output)

    def test_native_release_proceeds_while_a_dojo_server_runs(self):
        # 2026-11-24: any worldserver.exe or authserver.exe failed the stop check, a dojo's too.
        dojo = self.processes(("2001", "worldserver.exe", self.DOJO + r"\worldserver.exe"),
                              ("2003", "authserver.exe", self.DOJO + r"\authserver.exe"))
        output = self.run_release("--native", self.native, PROCESSES_OUTPUT=dojo)
        self.assertEqual([line for line in output.splitlines() if line.startswith("FAIL")], [])
        self.assertIn("release complete", output)
        self.assertIn("started binary: new binary", self.actions())

    def test_printed_native_rollback_refuses_surviving_or_unknown_processes(self):
        output = self.run_release("--native", self.native)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        released = self.git("rev-parse", "HEAD")
        for env in ({"PROCESSES_OUTPUT": self.processes(("1001", "worldserver.exe", str(self.bin / "worldserver.exe")))},
                    {"PROCESSES_OUTPUT": self.processes(("1005", "authserver.exe", ""))},
                    {"PROCESSES_OUTPUT": ""}, {"PROCESSES_FAIL": "1"}):
            result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
                env=dict(self.env, **env), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(self.git("rev-parse", "HEAD"), released)
            self.assertEqual(self.actions().count("build jobs=4"), 1)
        # A dojo's servers do not hold the rollback back.
        dojo = self.processes(("2001", "worldserver.exe", self.DOJO + r"\worldserver.exe"))
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
            env=dict(self.env, PROCESSES_OUTPUT=dojo), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()

    def test_printed_native_rollback_respects_release_and_build_locks(self):
        output = self.run_release("--native", self.native)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        released = self.git("rev-parse", "HEAD")
        actions = self.actions()
        release_lock = self.root / ".git/ac-release.lock"
        build_lock = self.root / ".superpowers/live-build.lock"
        for lock in (release_lock, build_lock):
            with self.subTest(lock=lock.name):
                lock.mkdir(parents=True)
                try:
                    result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
                        env=self.env, text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, timeout=30)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertTrue(lock.is_dir(), "must not remove another operation's lock")
                    self.assertEqual(self.git("rev-parse", "HEAD"), released)
                    self.assertEqual(self.actions(), actions)
                    if lock == build_lock:
                        self.assertFalse(release_lock.exists())
                finally:
                    lock.rmdir()
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()
        self.assertFalse(build_lock.exists())

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

    def test_non_runtime_conflicts_take_pinned_main_tip_not_the_selected_commit(self):
        paths = (".github/workflows/a release.yml", "mind/tests/test release.py")
        selected = self.publish({p: "selected PR\n" for p in paths}, parent=self.base)
        self.publish({p: "newest main\n" for p in paths}, parent=selected)
        for path in paths:
            self.write(self.root / path, "private local version\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Private non-runtime files")
        self.run_release(selected)
        for path in paths:
            self.assertEqual((self.root / path).read_text(), "newest main\n")
        self.assertEqual((self.root / "mind/app.py").read_text(), "original\n")

    def test_non_runtime_modify_delete_conflict_keeps_main_deletion(self):
        path = "mind/tests/obsolete test.py"
        seed = self.publish({path: "old\n"}, parent=self.base)
        self.git("cherry-pick", seed)
        selected = self.publish({path: None}, parent=seed)
        self.write(self.root / path, "local modification\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Local test edit")
        self.run_release(selected)
        self.assertFalse((self.root / path).exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_non_runtime_local_deletion_takes_the_main_file(self):
        path = ".github/workflows/restored.yml"
        seed = self.publish({path: "old\n"}, parent=self.base)
        self.git("cherry-pick", seed)
        selected = self.publish({path: "main edit\n"}, parent=seed)
        self.git("rm", path)
        self.git("commit", "-qm", "Private workflow deletion")
        self.run_release(selected)
        self.assertEqual((self.root / path).read_text(), "main edit\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_mixed_runtime_and_non_runtime_conflicts_refuse_before_changes(self):
        files = {".github/workflows/check.yml": "incoming\n", "mind/app.py": "incoming\n"}
        selected = self.publish(files, parent=self.base)
        for path in files:
            self.write(self.root / path, "local\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Private runtime and non-runtime edits")
        before = self.git("rev-parse", "HEAD")
        self.run_release(selected, rc=1)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.actions(), "")
        for path in files:
            self.assertEqual((self.root / path).read_text(), "local\n")

    def test_scripts_require_explicit_opt_in(self):
        selected = self.publish({"scripts/new helper.sh": "#!/bin/sh\nexit 0\n"}, parent=self.base)
        self.assertIn("--allow-scripts", self.run_release(selected, rc=1))
        self.original()
        self.run_release("--allow-scripts", selected)
        self.assertTrue((self.root / "scripts/new helper.sh").exists())

    def test_script_only_release_and_rollback_respect_build_lock(self):
        selected = self.publish({"scripts/new helper.sh": "#!/bin/sh\nexit 0\n"}, parent=self.base)
        build_lock = self.root / ".superpowers/live-build.lock"
        build_lock.mkdir(parents=True)
        try:
            self.run_release("--allow-scripts", selected, rc=1)
            self.assertTrue(build_lock.is_dir())
            self.assertEqual(self.actions(), "")
            self.original()
        finally:
            build_lock.rmdir()
        output = self.run_release("--allow-scripts", selected)
        self.assertFalse(build_lock.exists())
        self.assertNotIn("server ", self.actions())
        self.assertNotIn("build jobs=", self.actions())
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        released, actions = self.git("rev-parse", "HEAD"), self.actions()
        build_lock.mkdir()
        try:
            result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
                env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertTrue(build_lock.is_dir())
            self.assertFalse((self.root / ".git/ac-release.lock").exists())
            self.assertEqual(self.git("rev-parse", "HEAD"), released)
            self.assertEqual(self.actions(), actions)
        finally:
            build_lock.rmdir()
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()
        self.assertFalse(build_lock.exists())
        self.assertNotIn("server ", self.actions())
        self.assertNotIn("build jobs=", self.actions())

    def test_scripts_rename_source_requires_opt_in(self):
        self.git("checkout", "-qb", "move-script", self.base)
        self.git("mv", "scripts/ac-health.sh", "mind/renamed.sh")
        self.git("commit", "-qm", "Move script")
        selected = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", selected)
        self.git("checkout", "-q", "release/test")
        self.assertIn("--allow-scripts", self.run_release(selected, rc=1))
        self.original()

    def test_self_replacement_and_removed_helper_keep_manual_rollback_working(self):
        self.install_runner()
        selected = self.publish({"scripts/ac-release.sh": "#!/bin/sh\nexit 91\n",
                                 "scripts/ac-release-data.py": None,
                                 "mind/new.py": "released\n"}, parent=self.base)
        output = self.run_release("--allow-scripts", selected)
        self.assertIn("release complete", output)
        self.assertEqual(self.runner.read_text(), "#!/bin/sh\nexit 91\n")
        self.assertFalse((self.root / "scripts/ac-release-data.py").exists())
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root, env=self.env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()

    def test_changed_data_helper_cannot_break_failure_rollback(self):
        self.install_runner()
        selected = self.publish({"scripts/ac-release-data.py": "raise RuntimeError('new broken helper')\n",
                                 "mind/new.py": "released\n"}, parent=self.base)
        self.run_release("--allow-scripts", selected, rc=2, FAIL_MIGRATE="1")
        self.original()
        self.assertIn("mind start", self.actions())

    def test_changed_native_stop_script_cannot_block_build_failure_rollback(self):
        selected = self.publish({"scripts/ac-server.sh": "#!/bin/sh\nexit 92\n",
                                 "mind/new.py": "released\n"}, parent=self.base)
        self.run_release("--native", "--allow-scripts", selected, rc=2, FAIL_BUILD="1")
        self.original()
        self.assertEqual(self.actions().count("server stop"), 2)
        self.assertIn("started binary: old binary", self.actions())

    def test_changed_mind_stop_script_cannot_block_migration_failure_rollback(self):
        selected = self.publish({"scripts/ac-mind.py": "raise RuntimeError('new stop is broken')\n",
                                 "mind/new.py": "released\n"}, parent=self.base)
        self.run_release("--allow-scripts", selected, rc=2, FAIL_MIGRATE="1")
        self.original()
        self.assertEqual(self.actions().count("mind stop"), 2)
        self.assertIn("mind start", self.actions())

    def test_printed_rollback_uses_frozen_stop_when_new_stop_is_broken(self):
        selected = self.publish({"scripts/ac-server.sh": '''#!/usr/bin/env bash
if [[ $1 == stop ]]; then exit 92; fi
printf 'new server start\\n' >> "$AC_ROOT/actions.log"
''', "mind/new.py": "released\n"}, parent=self.base)
        output = self.run_release("--native", "--allow-scripts", selected)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root,
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.original()
        self.assertEqual(self.actions().count("server stop"), 2)

    def test_foreign_build_time_commit_is_preserved_and_rollback_refuses(self):
        output = self.run_release("--native", self.native, rc=2,
                                  FAIL_BUILD="1", BUILD_OWNER_CHANGE="commit")
        self.assertIn("checkout rollback refused", output)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Owner work during build")
        self.assertEqual((self.root / "mind/owner.py").read_text(), "owner work\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotIn("server start", self.actions())

    def test_foreign_build_time_edit_is_preserved_and_rollback_refuses(self):
        output = self.run_release("--native", self.native, rc=2,
                                  FAIL_BUILD="1", BUILD_OWNER_CHANGE="dirty")
        self.assertIn("checkout rollback refused", output)
        self.assertEqual((self.root / "mind/app.py").read_text(), "owner edit\n")
        self.assertIn("mind/app.py", self.git("status", "--porcelain"))
        self.assertNotIn("server start", self.actions())

    def test_owned_commit_without_final_journal_checkpoint_can_roll_back(self):
        journal = self.root.parent / "root-plan.json"
        helper = [sys.executable, str(SCRIPTS / "ac-release-git.py")]
        result = subprocess.run([*helper, "plan", str(self.root), self.git("rev-parse", "origin/main"),
                                 str(journal), "0", self.single], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(journal.read_text())
        plan.update(phase="applying", pending=plan["commits"][0])
        journal.write_text(json.dumps(plan))
        self.git("cherry-pick", self.single)  # commit completed before its success checkpoint
        result = subprocess.run([*helper, "rollback", str(self.root), str(journal)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.original()
        self.assertEqual(json.loads(journal.read_text())["phase"], "rolled_back")

    def test_root_commit_hook_failure_aborts_owned_pick_and_restores_checkout(self):
        hook = self.root / ".git/hooks/prepare-commit-msg"
        self.write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.run_release(self.single, rc=2)
        self.original()
        self.assertIn("mind start", self.actions())
        self.assertNotIn("migrate", self.actions())

    def test_root_commit_hook_foreign_same_file_edit_is_preserved(self):
        hook = self.root / ".git/hooks/prepare-commit-msg"
        self.write(hook, '#!/bin/sh\nprintf "owner edit\\n" > mind/app.py\nexit 1\n')
        hook.chmod(0o755)
        output = self.run_release(self.single, rc=2)
        self.assertIn("checkout rollback refused", output)
        self.assertEqual((self.root / "mind/app.py").read_text(), "owner edit\n")
        self.assertTrue((self.root / ".git/CHERRY_PICK_HEAD").exists())
        self.assertNotIn("mind start", self.actions())

    def test_ancestor_commit_is_a_noop(self):
        self.git("reset", "--hard", self.single)
        self.write(self.root / "mind/app.py", "a newer live version\n")
        self.git("add", "mind/app.py")
        self.git("commit", "-qm", "Later live work on the same file")
        before = self.git("rev-parse", "HEAD")
        self.run_release(self.single)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "mind/app.py").read_text(), "a newer live version\n")
        self.assertEqual(self.actions(), "")
        self.assertFalse((self.db.parent / "snapshots").exists())

    def test_duplicate_arguments_and_already_applied_commits_allow_new_work(self):
        self.run_release(self.single)
        before = self.git("rev-parse", "HEAD")
        output = self.run_release(self.single, self.merge, self.merge)
        self.assertIn("skip", output.lower())
        self.assertEqual(self.git("rev-list", "--count", f"{before}..HEAD"), "1")
        self.assertEqual((self.root / "mind/feature.py").read_text(), "feature\n")
        self.assertEqual(self.actions().count("migrate"), 2)

    def test_merge_applied_as_constituent_commits_is_skipped(self):
        first = self.publish({"mind/one.py": "one\n"}, parent=self.base)
        second = self.publish({"mind/two.py": "two\n"}, parent=first)
        self.git("checkout", "-qb", "merge-base", self.base)
        self.write(self.root / "mind/unselected.py", "not selected\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Unselected main commit")
        self.git("merge", "--no-ff", "-qm", "Merge two changes", second)
        selected = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", selected)
        self.git("checkout", "-q", "release/test")
        self.git("cherry-pick", first, second)
        before = self.git("rev-parse", "HEAD")
        self.run_release(selected)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.actions(), "")
        self.assertFalse((self.root / "mind/unselected.py").exists())

    def test_native_patches_apply_in_series_order_and_dry_run_is_readonly(self):
        repo, head = self.module("mod-playerbots")
        patches = self.series(repo, [{"payload.txt": "first\n"}, {"payload.txt": "second\n"}])
        selected = self.publish(patches, parent=self.base)
        self.run_release("--native", "--dry-run", selected)
        self.module_original(repo, head)
        self.original()
        self.assertEqual(self.actions(), "")
        self.run_release("--native", selected)
        self.assertEqual((repo / "payload.txt").read_text(), "second\n")
        self.assertEqual(self.git_at(repo, "log", "--format=%s", "--reverse", f"{head}..HEAD"),
                         "Module step 1\nModule step 2")
        self.assertIn("build jobs=4", self.actions())

    def test_patch_equivalent_module_commit_is_kept_and_only_missing_tail_applies(self):
        repo, _ = self.module("mod-playerbots")
        patches = self.series(repo, [{"payload.txt": "first\n"}, {"payload.txt": "second\n"}])
        self.write(repo / "payload.txt", "first\n")
        self.git_at(repo, "add", ".")
        self.git_at(repo, "commit", "-qm", "Applied locally with another identity")
        already = self.git_at(repo, "rev-parse", "HEAD")
        selected = self.publish(patches, parent=self.base)
        self.run_release("--native", selected)
        self.assertEqual(self.git_at(repo, "rev-list", "--count", f"{already}..HEAD"), "1")
        self.assertEqual((repo / "payload.txt").read_text(), "second\n")

    def test_installed_module_patch_exported_with_other_context_is_not_applied_again(self):
        repo, _ = self.module("mod-playerbots")
        lines = "".join(f"line {i}\n" for i in range(1, 16))
        self.write(repo / "payload.txt", lines)
        self.git_at(repo, "commit", "-qam", "Module lines")
        source = self.root.parent / "playerbots-reexport"
        shutil.copytree(repo, source)
        self.write(source / "payload.txt", lines.replace("line 8\n", "line 8 changed\n"))
        self.git_at(source, "commit", "-qam", "Change line eight")
        patch = self.git_at(source, "format-patch", "-U1", "-1", "--stdout") + "\n"
        self.write(repo / "payload.txt", lines.replace("line 8\n", "line 8 changed\n"))
        self.git_at(repo, "commit", "-qam", "Change line eight")  # installed as a -U3 commit
        installed = self.git_at(repo, "rev-parse", "HEAD")
        ids = [subprocess.run(["git", "patch-id", "--stable"], input=text, capture_output=True, text=True,
                              check=True).stdout.split()[0]
               for text in (patch, self.git_at(repo, "show", "HEAD") + "\n")]
        self.assertNotEqual(*ids)
        selected = self.publish({"patches/mod-playerbots/0001-step.patch": patch}, parent=self.base)
        self.assertIn("apply 0 missing module patch(es)", self.run_release("--native", "--dry-run", selected))
        self.module_original(repo, installed)
        self.run_release("--native", selected)
        self.module_original(repo, installed)

    def test_already_live_root_commit_still_installs_missing_native_patch(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        self.git("cherry-pick", selected)  # the previous manual release updated only the root checkout
        root_head = self.git("rev-parse", "HEAD")
        self.run_release("--native", selected, rc=2, FAIL_BUILD="1")
        self.assertEqual(self.git("rev-parse", "HEAD"), root_head)
        self.module_original(repo, head)
        self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")
        self.run_release("--native", selected)
        self.assertEqual(self.git("rev-parse", "HEAD"), root_head)
        self.assertNotEqual(self.git_at(repo, "rev-parse", "HEAD"), head)
        self.assertEqual((repo / "payload.txt").read_text(), "patched\n")
        self.assertIn("build jobs=4", self.actions())
        actions = self.actions()
        module_head = self.git_at(repo, "rev-parse", "HEAD")
        self.run_release("--native", selected)  # now root AND module are already applied
        self.assertEqual(self.actions(), actions)
        self.assertEqual(self.git("rev-parse", "HEAD"), root_head)
        self.assertEqual(self.git_at(repo, "rev-parse", "HEAD"), module_head)

    def test_failed_native_build_restores_both_module_repositories(self):
        playerbots, pb_head = self.module("mod-playerbots")
        dungeon, dc_head = self.module("mod-dungeon-clear")
        patches = self.series(playerbots, [{"payload.txt": "playerbots patch\n"}])
        patches.update(self.series(dungeon, [{"payload.txt": "dungeon patch\n"}]))
        selected = self.publish(patches, parent=self.base)
        self.run_release("--native", selected, rc=2, FAIL_BUILD="1")
        self.module_original(playerbots, pb_head)
        self.module_original(dungeon, dc_head)
        self.original()
        self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")
        self.assertIn("started binary: old binary", self.actions())

    def test_failed_native_migration_restores_module_repository(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        self.run_release("--native", selected, rc=2, FAIL_MIGRATE="1")
        self.module_original(repo, head)
        self.original()
        self.assertEqual((self.bin / "worldserver.exe").read_text(), "old binary")

    def test_native_git_am_hook_failure_aborts_session_and_restores_checkout(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        hook = repo / ".git/hooks/applypatch-msg"
        self.write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)  # cloned rehearsal has no local hook; only the actual apply fails
        self.run_release("--native", selected, rc=2)
        self.module_original(repo, head)
        self.original()
        self.assertNotIn("build jobs=", self.actions())

    def test_dirty_module_is_refused_before_service_stop(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        self.write(repo / "payload.txt", "private uncommitted work\n")
        self.run_release("--native", selected, rc=1)
        self.assertEqual(self.git_at(repo, "rev-parse", "HEAD"), head)
        self.assertEqual((repo / "payload.txt").read_text(), "private uncommitted work\n")
        self.assertEqual(self.actions(), "")
        self.original()

    def test_foreign_module_git_operation_is_preserved(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        pending = repo / ".git/CHERRY_PICK_HEAD"
        self.write(pending, head + "\n")
        self.run_release("--native", selected, rc=1)
        self.assertEqual(pending.read_text(), head + "\n")
        self.assertEqual(self.git_at(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.actions(), "")
        self.original()

    def test_non_repository_module_cannot_fall_back_to_the_parent_git_repository(self):
        repo, _ = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        git_dir = (repo / ".git").resolve()
        self.assertTrue(git_dir.is_relative_to(self.root.resolve()))
        def remove_readonly(operation, path, error):
            os.chmod(path, 0o700)
            operation(path)
        shutil.rmtree(git_dir, onerror=remove_readonly)
        self.run_release("--native", selected, rc=1)
        self.original()
        self.assertEqual(self.actions(), "")

    def test_misplaced_pin_guard_is_refused_before_module_changes(self):
        repo, head = self.module("mod-playerbots")
        bad_pin = self.PIN.replace("    if (_pinKeep && IsPinned(botId))\n        return ProcessPinnedBot(bot);\n", "")
        bad_pin = bad_pin.replace("    // leave group if leader is rndbot", "    if (_pinKeep && IsPinned(botId))\n        return ProcessPinnedBot(bot);\n    // leave group if leader is rndbot")
        selected = self.publish(self.series(repo, [{"src/Bot/RandomPlayerbotMgr.cpp": bad_pin}]), parent=self.base)
        self.run_release("--native", selected, rc=1)
        self.module_original(repo, head)
        self.original()
        self.assertEqual(self.actions(), "")

    def test_printed_native_rollback_restores_the_original_module_head(self):
        repo, head = self.module("mod-playerbots")
        selected = self.publish(self.series(repo, [{"payload.txt": "patched\n"}]), parent=self.base)
        output = self.run_release("--native", selected)
        command, = (line.strip() for line in output.splitlines() if line.startswith("  cd "))
        result = subprocess.run([BASH, "-s"], input=command + "\n", cwd=self.root, env=self.env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.module_original(repo, head)
        self.original()


if __name__ == "__main__":
    unittest.main(verbosity=2)
