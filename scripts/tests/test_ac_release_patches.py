"""The module patch engine against temporary Git repositories only."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ac-release-patches.py"
spec = importlib.util.spec_from_file_location("release_patches", SCRIPT)
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

PIN = """bool RandomPlayerbotMgr::ProcessBot(Player* bot)
{
    if (IsPinned(botId)) return ProcessPinnedBot(bot);
    if (bot->isDead()) return false;
    // leave group if leader is rndbot
    return false;
}
"""


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="leeroy-patch-tests-")
        self.root = Path(self.temporary.name)
        self.modules = self.root / "modules with spaces"
        self.series = self.root / "series"
        self.journal = self.root / "recovery" / "modules.json"
        self.repos = {}
        self.original = {}
        for name in release.MODULES:
            repo = self.modules / name
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q", "-b", "local")
            self.git(repo, "config", "user.name", "Module test")
            self.git(repo, "config", "user.email", "module@example.invalid")
            self.git(repo, "config", "core.autocrlf", "false")
            self.git(repo, "config", "commit.gpgsign", "false")
            self.write(repo / "engine.cpp", "base\n")
            if name == "mod-playerbots":
                self.write(repo / release.PIN_FILE, PIN)
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "Base")
            self.repos[name] = repo
            self.original[name] = self.git(repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def git(repo, *args, data=None):
        r = subprocess.run(["git", "-C", str(repo), *args], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode:
            raise AssertionError((args, r.stderr.decode(errors="replace")))
        return r.stdout.decode().strip()

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def series_for(self, name, texts=("one\n", "two\n"), *, filenames=None):
        author = self.root / (name + "-author")
        self.git(self.repos[name], "clone", "-q", "--config", "core.autocrlf=false", str(self.repos[name]), str(author))
        self.git(author, "config", "user.name", "Patch author")
        self.git(author, "config", "user.email", "author@example.invalid")
        self.git(author, "config", "commit.gpgsign", "false")
        self.git(author, "config", "core.autocrlf", "false")
        entries = []
        for i, text in enumerate(texts, 1):
            self.write(author / "engine.cpp", text)
            self.git(author, "add", ".")
            self.git(author, "commit", "-qm", f"Patch {i}")
            raw = subprocess.check_output(["git", "-C", str(author), "format-patch", "-1", "--stdout", "--full-index"])
            target = self.series / name / (filenames[i-1] if filenames else f"{i:04d}-patch-{i}.patch")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            entries.append(raw)
        return author, entries

    def prepare(self):
        return release.prepare(self.modules, self.series, self.journal)

    def assert_original(self):
        for name, repo in self.repos.items():
            self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.original[name])
            self.assertEqual(self.git(repo, "symbolic-ref", "HEAD"), "refs/heads/local")
            self.assertEqual(self.git(repo, "status", "--porcelain"), "")
            self.assertFalse((repo / ".git/rebase-apply").exists())

    def test_prepare_is_read_only_and_apply_orders_both_series_then_rollback_restores_them(self):
        for name in self.repos:
            self.series_for(name)
        before = {name: (repo / ".git/index").read_bytes() for name, repo in self.repos.items()}
        journal = self.prepare()
        self.assertEqual(release.summary(journal), {"modules": 2, "missing": 4, "applied": 0})
        self.assert_original()
        self.assertEqual(before, {name: (repo / ".git/index").read_bytes() for name, repo in self.repos.items()})
        applied = release.apply(self.journal)
        self.assertEqual(release.summary(applied)["applied"], 4)
        for repo in self.repos.values():
            self.assertEqual((repo / "engine.cpp").read_text(), "two\n")
            self.assertEqual(self.git(repo, "log", "-2", "--format=%s").splitlines(), ["Patch 2", "Patch 1"])
        release.rollback(self.journal)
        self.assert_original()
        release.rollback(self.journal)  # recovery is idempotent
        self.assert_original()

    def test_patch_equivalence_and_partial_prefix_skip_different_commit_hash(self):
        author, entries = self.series_for("mod-playerbots")
        repo = self.repos["mod-playerbots"]
        self.git(repo, "am", data=entries[0])
        self.git(repo, "commit", "--amend", "-qm", "Different local commit")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(self.original["mod-playerbots"], self.git(author, "rev-parse", "HEAD^"))
        journal = self.prepare()
        self.assertEqual(release.summary(journal)["missing"], 1)
        release.apply(self.journal)
        self.assertEqual((repo / "engine.cpp").read_text(), "two\n")
        release.rollback(self.journal)
        self.assert_original()

    def test_ancestor_patches_are_skipped_and_no_series_ignores_absent_modules(self):
        author, _ = self.series_for("mod-playerbots")
        repo = self.repos["mod-playerbots"]
        self.git(repo, "fetch", str(author), "local")
        self.git(repo, "merge", "--ff-only", "FETCH_HEAD")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        missing_root = self.root / "does-not-exist"
        self.assertEqual(release.summary(release.prepare(missing_root, missing_root, self.root / "empty.json"))["modules"], 0)
        self.assertEqual(release.summary(self.prepare())["missing"], 0)
        release.apply(self.journal)
        self.assert_original()

    def test_dirty_tracked_and_untracked_repo_refused(self):
        for filename in ("engine.cpp", "untracked.cpp"):
            with self.subTest(filename=filename):
                self.series_for("mod-playerbots") if not self.series.exists() else None
                self.write(self.repos["mod-playerbots"] / filename, "owner edit\n")
                with self.assertRaisesRegex(release.Refused, "dirty_repository"):
                    self.prepare()
                self.assertFalse(self.journal.exists())
                if filename == "engine.cpp":
                    self.git(self.repos["mod-playerbots"], "checkout", "--", filename)
                else:
                    (self.repos["mod-playerbots"] / filename).unlink()

    def test_later_revert_of_installed_patch_is_preserved(self):
        _, entries = self.series_for("mod-playerbots", ("one\n",))
        repo = self.repos["mod-playerbots"]
        self.git(repo, "am", data=entries[0])
        self.git(repo, "revert", "--no-edit", "HEAD")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        self.assertEqual(release.summary(self.prepare())["missing"], 0)
        release.apply(self.journal)
        self.assertEqual((repo / "engine.cpp").read_text(), "base\n")
        release.rollback(self.journal)
        self.assert_original()

    def test_later_edit_of_installed_patch_is_preserved(self):
        _, entries = self.series_for("mod-playerbots", ("one\n",))
        repo = self.repos["mod-playerbots"]
        self.git(repo, "am", data=entries[0])
        self.write(repo / "engine.cpp", "owner improvement\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Local improvement")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        self.assertEqual(release.summary(self.prepare())["missing"], 0)
        release.apply(self.journal)
        self.assertEqual((repo / "engine.cpp").read_text(), "owner improvement\n")
        release.rollback(self.journal)
        self.assert_original()

    def test_duplicate_series_number_or_content_refused_before_mutation(self):
        self.series_for("mod-playerbots", ("one\n",))
        directory = self.series / "mod-playerbots"
        raw = (directory / "0001-patch-1.patch").read_bytes()
        for name, reason in (("0001-other.patch", "duplicate_patch_number"),
                             ("0002-repeat.patch", "duplicate_patch_content")):
            with self.subTest(name=name):
                duplicate = directory / name
                duplicate.write_bytes(raw)
                with self.assertRaisesRegex(release.Refused, reason):
                    self.prepare()
                duplicate.unlink()
                self.assert_original()

    def test_linked_git_worktree_preserves_original_branch_and_tip(self):
        name = "mod-playerbots"
        self.series_for(name, ("one\n",))
        store = self.root / "stored-module"
        self.repos[name].rename(store)
        self.git(store, "worktree", "add", "-qb", "release-worktree", str(self.repos[name]))
        self.prepare()
        release.apply(self.journal)
        release.rollback(self.journal)
        repo = self.repos[name]
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.original[name])
        self.assertEqual(self.git(repo, "symbolic-ref", "HEAD"), "refs/heads/release-worktree")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_zip_inside_parent_git_repo_is_not_a_module_checkout(self):
        self.series_for("mod-playerbots")
        root = self.root / "parent"
        root.mkdir()
        self.git(root, "init", "-q", "-b", "main")
        (root / "mod-playerbots").mkdir()
        with self.assertRaisesRegex(release.Refused, "not_module_repository"):
            release.prepare(root, self.series, self.journal)

    def test_missing_module_refused(self):
        self.series_for("mod-playerbots")
        with self.assertRaises(release.Refused):
            release.prepare(self.root / "missing", self.series, self.journal)

    def test_unfinished_git_operation_refused(self):
        self.series_for("mod-playerbots")
        (self.repos["mod-playerbots"] / ".git/rebase-apply").mkdir()
        with self.assertRaisesRegex(release.Refused, "git_operation_in_progress"):
            self.prepare()

    def test_pin_guard_missing_misplaced_or_commented_refused(self):
        self.series_for("mod-playerbots", ())
        directory = self.series / "mod-playerbots"
        directory.mkdir(parents=True)
        for body in (PIN.replace("IsPinned(botId)", "Other(botId)"),
                     PIN.replace("    if (IsPinned(botId)) return ProcessPinnedBot(bot);\n", "") + "// IsPinned(botId)\n",
                     PIN.replace("    if (IsPinned(botId))", "    // if (IsPinned(botId))")):
            with self.subTest(body=body):
                self.write(self.repos["mod-playerbots"] / release.PIN_FILE, body)
                with self.assertRaises(release.Refused):
                    release.pin_guard(self.repos["mod-playerbots"])

    def test_bad_resulting_pin_guard_fails_rehearsal_without_touching_real_module(self):
        author, _ = self.series_for("mod-playerbots", ("one\n",))
        self.write(author / release.PIN_FILE, PIN.replace("IsPinned(botId)", "Other(botId)"))
        self.git(author, "add", ".")
        self.git(author, "commit", "-qm", "Bad guard")
        raw = subprocess.check_output(["git", "-C", str(author), "format-patch", "-1", "--stdout"])
        (self.series / "mod-playerbots/0002-bad.patch").write_bytes(raw)
        with self.assertRaisesRegex(release.Refused, "pin_guard_misplaced"):
            self.prepare()
        self.assert_original()
        self.assertFalse(self.journal.exists())

    LINES = "".join(f"line {i}\n" for i in range(1, 16))

    def lined(self, text=LINES):
        """A multi-line file in the real playerbots checkout, and an author clone of it."""
        repo = self.repos["mod-playerbots"]
        self.write(repo / "lines.cpp", text)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Lines")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        author = self.root / "lines-author"
        self.git(repo, "clone", "-q", "--config", "core.autocrlf=false", str(repo), str(author))
        for key, value in (("user.name", "Patch author"), ("user.email", "author@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(author, "config", key, value)
        return repo, author

    def entry(self, author, number, old, new, subject, *export):
        """Commit one change in the author clone; write its export, return the standard one."""
        path = author / "lines.cpp"
        self.write(path, path.read_text(encoding="utf-8").replace(old, new, 1))
        self.git(author, "commit", "-qam", subject)
        show = ["git", "-C", str(author), "format-patch", "-1", "--stdout"]
        target = self.series / "mod-playerbots" / f"{number:04d}-{subject.replace(' ', '-')}.patch"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output([*show, *export]))
        return subprocess.check_output(show)

    def commit_lines(self, repo, text, subject):
        self.write(repo / "lines.cpp", text)
        self.git(repo, "commit", "-qam", subject)
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")

    def file_id(self, repo, number):
        return release.patch_id(repo, next((self.series / "mod-playerbots").glob(f"{number:04d}-*")).read_bytes())

    def test_installed_patch_with_another_patch_id_is_recognised_by_its_content(self):
        repo, author = self.lined()
        self.entry(author, 1, "line 8\n", "line 8 changed\n", "Change eight", "-U1")
        self.commit_lines(repo, (author / "lines.cpp").read_text(encoding="utf-8"), "Installed by hand")
        self.assertNotIn(self.file_id(repo, 1), release.history_ids(repo))
        journal = self.prepare()
        self.assertEqual(release.summary(journal)["missing"], 0)
        self.assert_original()
        release.apply(self.journal)
        self.assert_original()

    def test_installed_patch_with_another_patch_id_and_later_edit_is_matched_by_subject(self):
        repo, author = self.lined()
        standard = self.entry(author, 1, "line 8\n", "line 8 changed\n", "Change eight", "-U1")
        self.git(repo, "am", data=standard)
        self.commit_lines(repo, self.LINES.replace("line 8\n", "line 8 owner\n"), "Owner edit")
        self.assertNotIn(self.file_id(repo, 1), release.history_ids(repo))
        self.assertEqual(release.summary(self.prepare())["missing"], 0)
        self.assert_original()

    def test_missing_patch_after_one_installed_with_another_patch_id_is_applied(self):
        repo, author = self.lined()
        self.entry(author, 1, "line 4\n", "line 4 changed\n", "Change four", "-U1")
        self.commit_lines(repo, (author / "lines.cpp").read_text(encoding="utf-8"), "Installed by hand")
        self.entry(author, 2, "line 12\n", "line 12 changed\n", "Change twelve", "-U1")
        journal = self.prepare()
        self.assertEqual(release.summary(journal)["missing"], 1)
        self.assertEqual([p["name"] for p in journal["modules"][0]["patches"]], ["0002-Change-twelve.patch"])
        self.assert_original()
        self.assertEqual(release.summary(release.apply(self.journal))["applied"], 1)
        self.assertEqual((repo / "lines.cpp").read_text(encoding="utf-8"), (author / "lines.cpp").read_text(encoding="utf-8"))
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s"), "Change twelve")
        release.rollback(self.journal)
        self.assert_original()

    def test_entry_undoing_a_missing_earlier_entry_is_not_taken_as_installed(self):
        repo, author = self.lined()
        self.entry(author, 1, "line 8\n", "line 8 changed\n", "Change eight")
        self.entry(author, 2, "line 8 changed\n", "line 8\n", "Restore eight")
        self.assertEqual(release.summary(self.prepare())["missing"], 2)
        self.assert_original()
        release.apply(self.journal)
        self.assertEqual(self.git(repo, "log", "-2", "--format=%s").splitlines(), ["Restore eight", "Change eight"])
        release.rollback(self.journal)
        self.assert_original()

    def test_patch_that_neither_reverses_nor_applies_still_fails_closed(self):
        repo, author = self.lined()
        self.entry(author, 1, "line 8\n", "line 8 changed\n", "Change eight", "-U1")
        self.commit_lines(repo, self.LINES.replace("line 8\n", "line 8 local\n"), "Local change")
        with self.assertRaisesRegex(release.Refused, r"mod-playerbots: 0001-Change-eight\.patch: patch_apply_failed"):
            self.prepare()
        self.assert_original()
        self.assertFalse(self.journal.exists())

    def test_patch_that_both_reverses_and_applies_is_refused_as_ambiguous(self):
        block = "one\ntwo\nthree\nfour\n"
        repo, author = self.lined("head 1\nhead 2\nhead 3\none\ntwo\nX\nthree\nfour\nmid\n" + block + "tail 1\ntail 2\n")
        self.entry(author, 1, "X\n", "", "Drop X", "-U2")
        with self.assertRaisesRegex(release.Refused, r"mod-playerbots: 0001-Drop-X\.patch: ambiguous_installed_state"):
            self.prepare()
        self.assert_original()
        self.assertFalse(self.journal.exists())

    def test_incompatible_patch_reports_its_series_entry_without_real_changes(self):
        self.series_for("mod-playerbots", ("one\n",))
        repo = self.repos["mod-playerbots"]
        self.write(repo / "engine.cpp", "incompatible local change\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Local change")
        self.original["mod-playerbots"] = self.git(repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(release.Refused, r"mod-playerbots: 0001-patch-1\.patch: patch_apply_failed"):
            self.prepare()
        self.assert_original()
        self.assertFalse(self.journal.exists())

    def test_pending_intent_recovers_commit_before_success_checkpoint(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        original_am = release.am
        def crash(repo, raw):
            original_am(repo, raw)
            raise RuntimeError("interrupted after commit")
        with patch.object(release, "am", side_effect=crash), self.assertRaises(RuntimeError):
            release.apply(self.journal)
        self.assertIsNotNone(release.load(self.journal)["modules"][0]["pending"])
        release.rollback(self.journal)
        self.assert_original()

    def test_rollback_recovers_reset_before_success_checkpoint(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        release.apply(self.journal)
        original_git = release.git
        def crash(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("reset", "--hard"):
                raise RuntimeError("interrupted after reset")
            return result
        with patch.object(release, "git", side_effect=crash), self.assertRaises(RuntimeError):
            release.rollback(self.journal)
        release.rollback(self.journal)
        self.assert_original()

    def test_second_module_failed_am_aborts_own_operation_and_rolls_back_first(self):
        for name in self.repos:
            self.series_for(name, ("one\n",))
        self.prepare()
        original_am = release.am
        def fail(repo, raw):
            if repo.name == "mod-dungeon-clear":
                # A failing applypatch-msg hook leaves the owned clean am session.
                hook = repo / ".git/hooks/applypatch-msg"
                self.write(hook, "#!/bin/sh\nexit 1\n")
                hook.chmod(0o755)
            original_am(repo, raw)
        with patch.object(release, "am", side_effect=fail), self.assertRaisesRegex(
                release.Refused, r"mod-dungeon-clear: 0001-patch-1\.patch: patch_apply_failed"):
            release.apply(self.journal)
        self.assertNotEqual(self.git(self.repos["mod-playerbots"], "rev-parse", "HEAD"), self.original["mod-playerbots"])
        release.rollback(self.journal)
        self.assert_original()

    def test_failed_pre_applypatch_hook_rolls_back_only_exact_owned_staged_tree(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        hook = self.repos["mod-playerbots"] / ".git/hooks/pre-applypatch"
        self.write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaises(release.Refused):
            release.apply(self.journal)
        self.assertNotEqual(self.git(self.repos["mod-playerbots"], "status", "--porcelain"), "")
        release.rollback(self.journal)
        self.assert_original()

    def test_owned_am_with_foreign_unstaged_edit_is_preserved(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        repo = self.repos["mod-playerbots"]
        hook = repo / ".git/hooks/pre-applypatch"
        self.write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaises(release.Refused):
            release.apply(self.journal)
        self.write(repo / "engine.cpp", "owner edit\n")
        with self.assertRaisesRegex(release.Refused, "foreign_changes_prevent_rollback"):
            release.rollback(self.journal)
        self.assertTrue((repo / ".git/rebase-apply").exists())
        self.assertEqual((repo / "engine.cpp").read_text(), "owner edit\n")

    def test_foreign_commit_prevents_all_module_rollbacks(self):
        for name in self.repos:
            self.series_for(name, ("one\n",))
        self.prepare()
        release.apply(self.journal)
        repo = self.repos["mod-dungeon-clear"]
        self.write(repo / "other.cpp", "owner\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Owner work")
        heads = {name: self.git(r, "rev-parse", "HEAD") for name, r in self.repos.items()}
        with self.assertRaisesRegex(release.Refused, "foreign_changes_prevent_rollback"):
            release.rollback(self.journal)
        self.assertEqual(heads, {name: self.git(r, "rev-parse", "HEAD") for name, r in self.repos.items()})

    def test_foreign_dirty_content_is_preserved_on_rollback(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        release.apply(self.journal)
        repo = self.repos["mod-playerbots"]
        self.write(repo / "engine.cpp", "owner edit\n")
        with self.assertRaisesRegex(release.Refused, "foreign_changes_prevent_rollback"):
            release.rollback(self.journal)
        self.assertEqual((repo / "engine.cpp").read_text(), "owner edit\n")

    def test_foreign_am_session_is_preserved(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        release.apply(self.journal)
        folder = self.repos["mod-playerbots"] / ".git/rebase-apply"
        folder.mkdir()
        self.write(folder / "applying", "owner session\n")
        with self.assertRaisesRegex(release.Refused, "foreign_operation_prevents_rollback"):
            release.rollback(self.journal)
        self.assertEqual((folder / "applying").read_text(), "owner session\n")

    def test_changed_branch_refused_before_apply_and_rollback(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        self.git(self.repos["mod-playerbots"], "checkout", "-qb", "other")
        for action in (release.apply, release.rollback):
            with self.assertRaisesRegex(release.Refused, "repository_identity_changed"):
                action(self.journal)

    def test_replaced_git_directory_is_not_treated_as_owned_repository(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        directory = self.repos["mod-playerbots"] / ".git"
        parked = self.root / "original-gitdir"
        directory.rename(parked)
        shutil.copytree(parked, directory)
        for action in (release.apply, release.rollback):
            with self.assertRaisesRegex(release.Refused, "repository_identity_changed"):
                action(self.journal)
        self.assert_original()

    def test_self_contained_journal_can_move_and_source_patches_can_disappear(self):
        self.series_for("mod-playerbots", ("one\n",))
        self.prepare()
        moved = self.root / "durable recovery.json"
        moved.write_bytes(self.journal.read_bytes())
        self.journal.unlink()
        shutil.rmtree(self.series)
        release.apply(moved)
        release.rollback(moved)
        self.assert_original()

    def test_changed_payload_refused_without_changing_module(self):
        self.series_for("mod-playerbots", ("one\n",))
        journal = self.prepare()
        journal["modules"][0]["patches"][0]["sha256"] = "0" * 64
        release.save(self.journal, journal)
        with self.assertRaisesRegex(release.Refused, "patch_payload_changed"):
            release.apply(self.journal)
        release.rollback(self.journal)
        self.assert_original()

    def test_detached_checkout_is_preserved(self):
        self.series_for("mod-playerbots", ("one\n",))
        repo = self.repos["mod-playerbots"]
        self.git(repo, "checkout", "--detach", "-q")
        self.prepare()
        release.apply(self.journal)
        release.rollback(self.journal)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.original["mod-playerbots"])
        self.assertIsNone(release.state(repo)["branch"])


if __name__ == "__main__":
    unittest.main()
