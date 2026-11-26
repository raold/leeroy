#!/usr/bin/env python3
"""Journalled local module patches for ac-release.sh; standard library only.

prepare reads the real module checkouts and rehearses in temporary clones. apply
is called only after the release runner has confirmed that native services are
stopped. The self-contained journal and this helper must be retained for manual
recovery. No command discovers an installation or starts a service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

MODULES = ("mod-playerbots", "mod-dungeon-clear", "mod-individual-progression")
PIN_FILE = "src/Bot/RandomPlayerbotMgr.cpp"
OPERATIONS = ("rebase-apply", "rebase-merge", "CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD", "sequencer")


class Refused(Exception):
    pass


def git(repo: Path, *args: str, data: bytes | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", **(env or {})))
    if check and result.returncode:
        raise Refused(f"git_{args[0].replace('-', '_')}_failed")
    return result


def value(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git(repo, *args, env=env).stdout.decode("utf-8", "surrogateescape").strip()


def same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def state(repo: Path) -> dict:
    if not repo.is_dir() or not same_path(value(repo, "rev-parse", "--show-toplevel"), repo):
        raise Refused("not_module_repository")
    gitdir = Path(value(repo, "rev-parse", "--absolute-git-dir")).resolve()
    identity = gitdir.stat()
    branch = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if branch.returncode not in (0, 1):
        raise Refused("branch_unreadable")
    return {"path": str(repo.resolve()), "gitdir": str(gitdir),
            "gitdir_identity": [identity.st_dev, identity.st_ino],
            "branch": branch.stdout.decode().strip() or None,
            "head": value(repo, "rev-parse", "--verify", "HEAD")}


def clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout


def operations(info: dict) -> list[str]:
    return [name for name in OPERATIONS if (Path(info["gitdir"]) / name).exists()]


def patch_id(repo: Path, patch: bytes) -> str:
    rows = git(repo, "patch-id", "--stable", data=patch).stdout.decode().splitlines()
    if len(rows) != 1 or not re.fullmatch(r"[0-9a-f]{40,64} [0-9a-f]{40,64}", rows[0]):
        raise Refused("not_one_nonempty_patch")
    return rows[0].split()[0]


def history_ids(repo: Path) -> set[str]:
    # Stream history, rather than holding potentially large playerbots diffs in memory.
    # No 300-commit bound: a previously applied patch may have been rebased years ago.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryFile() as errors:
        log = subprocess.Popen(["git", "-C", str(repo), "log", "--no-merges", "-p", "HEAD"],
                               stdout=subprocess.PIPE, stderr=errors, env=env)
        assert log.stdout is not None
        ids = subprocess.Popen(["git", "-C", str(repo), "patch-id", "--stable"], stdin=log.stdout,
                               stdout=subprocess.PIPE, stderr=errors, env=env)
        log.stdout.close()
        output, _ = ids.communicate()
        if ids.returncode or log.wait():
            raise Refused("patch_history_unreadable")
    return {line.split()[0] for line in output.decode().splitlines() if line.split()}


def mail(clone: Path, raw: bytes, work: Path) -> tuple[str, bytes]:
    """The commit subject and diff that git am takes from a format-patch file."""
    info = git(clone, "mailinfo", str(work / "message"), str(work / "diff"), data=raw).stdout
    rows = info.decode("utf-8", "surrogateescape").splitlines()
    subject = next((row[len("Subject: "):] for row in rows if row.startswith("Subject: ")), "")
    return subject, (work / "diff").read_bytes()


def subjects(clone: Path) -> dict[str, list[str]]:
    rows = git(clone, "log", "--no-merges", "--format=%H %s", "HEAD").stdout
    found: dict[str, list[str]] = {}
    for row in rows.decode("utf-8", "surrogateescape").splitlines():
        sha, _, subject = row.partition(" ")
        found.setdefault(subject, []).append(sha)
    return found


def applied_tree(clone: Path, start: str, diff: bytes, index: dict[str, str], *flags: str) -> str | None:
    # Apply to a committed tree in a private index of the rehearsal clone: never a
    # working tree, its line-ending conversion or the real checkout. Compare whole
    # trees, since git apply --check only warns about a mismatched file mode.
    git(clone, "read-tree", start, env=index)
    if git(clone, "apply", "--cached", *flags, data=diff, check=False, env=index).returncode:
        return None
    return value(clone, "write-tree", env=index)


def same_change(clone: Path, sha: str, diff: bytes, index: dict[str, str]) -> bool:
    # The patch is this commit when it turns the commit's parent into exactly the
    # commit's tree, whatever context width or format it was exported with.
    parents = value(clone, "rev-list", "--parents", "-n", "1", sha).split()
    return (len(parents) == 2 and applied_tree(clone, parents[1], diff, index)
            == value(clone, "rev-parse", f"{sha}^{{tree}}"))


def label(patch: dict) -> str:
    # Report the offending series entry, never Git's unbounded error payload.
    # Unusual filenames get a content identifier instead of terminal controls.
    name = patch["name"]
    return name if re.fullmatch(r"[0-9]{4}-[A-Za-z0-9_.-]{1,180}\.patch", name) else patch["sha256"][:12]


def pin_guard(repo: Path) -> None:
    try:
        code = (repo / PIN_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refused("pin_guard_unreadable") from exc
    start = re.search(r"^bool RandomPlayerbotMgr::ProcessBot\(Player\* bot\)", code, re.M)
    if not start:
        raise Refused("pin_guard_missing")
    # Match the established operator check, confined to the function's first closing
    # top-level brace so a later method cannot accidentally satisfy it.
    body = code[start.end():].split("\n}", 1)[0]
    uncommented = re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m[0]), body, flags=re.S)
    guard = uncommented.find("IsPinned(botId)")
    death = uncommented.find("bot->isDead()")
    leave = body.find("leave group if leader is rndbot")
    if not 0 <= guard < death < leave:
        raise Refused("pin_guard_misplaced")


def save(path: Path, journal: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # This file is outside every checkout. A committed patch with an interrupted
    # post-commit save is recoverable through the previously flushed pending intent.
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(journal, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path: Path) -> dict:
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("version") != 1 or not isinstance(journal.get("modules"), list):
        raise Refused("invalid_journal")
    names = [module.get("name") for module in journal["modules"]]
    if len(set(names)) != len(names) or any(name not in MODULES for name in names):
        raise Refused("invalid_journal_modules")
    return journal


def summary(journal: dict) -> dict:
    return {"modules": len(journal["modules"]),
            "missing": sum(len(m["patches"]) for m in journal["modules"]),
            "applied": sum(m["applied"] for m in journal["modules"])}


def patch_bytes(patch: dict) -> bytes:
    raw = base64.b64decode(patch["data"], validate=True)
    if hashlib.sha256(raw).hexdigest() != patch["sha256"]:
        raise Refused("patch_payload_changed")
    return raw


def am(repo: Path, raw: bytes) -> None:
    # No three-way conflict files: a failed application remains clean and can be
    # distinguished from unrelated working-tree edits before rollback.
    git(repo, "-c", "commit.gpgSign=false", "am", "--no-3way", data=raw)


def apply_entry(repo: Path, module: str, patch: dict) -> None:
    raw = patch_bytes(patch)
    try:
        am(repo, raw)
    except Refused as exc:
        raise Refused(f"{module}: {label(patch)}: patch_apply_failed") from exc


def prepare(modules_root: Path, series_root: Path, path: Path) -> dict:
    if path.exists():
        raise Refused("journal_already_exists")
    journal = {"version": 1, "modules": []}
    # Validate all real repositories before rehearsal, and recheck after it; none
    # of prepare's Git mutations target them.
    for name in MODULES:
        series = series_root / name
        files = sorted(series.glob("*.patch")) if series.is_dir() else []
        if not files:
            continue
        if series.is_symlink() or any(p.is_symlink() or not re.fullmatch(r"[0-9]{4}-.+\.patch", p.name) for p in files):
            raise Refused(f"{name}: invalid_patch_series")
        numbers = [p.name[:4] for p in files]
        if len(set(numbers)) != len(numbers):
            raise Refused(f"{name}: duplicate_patch_number")
        repo = (modules_root / name).resolve()
        info = state(repo)
        if operations(info):
            raise Refused(f"{name}: git_operation_in_progress")
        if not clean(repo):
            raise Refused(f"{name}: dirty_repository")
        module = dict(info, name=name, expected=info["head"], applied=0, pending=None, patches=[])
        seen: set[str] | None = None
        for file in files:
            raw = file.read_bytes()
            first = raw.splitlines()[0] if raw else b""
            match = re.fullmatch(rb"From ([0-9a-f]{40}|[0-9a-f]{64}) Mon Sep 17 00:00:00 2001", first)
            if not match:
                raise Refused(f"{name}: not_format_patch")
            ident = patch_id(repo, raw)
            ancestor = git(repo, "merge-base", "--is-ancestor", match[1].decode(), "HEAD", check=False)
            known = ancestor.returncode == 0
            if not known:
                if seen is None:
                    seen = history_ids(repo)
                known = ident in seen
            if known:
                # Installed means present in history. Preserve later local edits
                # and intentional reverts; replay requires a new series entry.
                continue
            if any(p["patch_id"] == ident for p in module["patches"]):
                raise Refused(f"{name}: duplicate_patch_content")
            module["patches"].append({"name": file.name, "patch_id": ident,
                                      "sha256": hashlib.sha256(raw).hexdigest(),
                                      "data": base64.b64encode(raw).decode("ascii")})
        journal["modules"].append(module)
    with tempfile.TemporaryDirectory(prefix="leeroy-patch-plan-") as temporary:
        for module in journal["modules"]:
            real = Path(module["path"])
            if not module["patches"]:
                if module["name"] == "mod-playerbots":
                    pin_guard(real)
                continue
            clone = Path(temporary) / module["name"]
            git(real, "clone", "--quiet", "--shared", "--no-checkout", str(real), str(clone))
            git(clone, "config", "user.name", "Release rehearsal")
            git(clone, "config", "user.email", "release@example.invalid")
            git(clone, "config", "commit.gpgSign", "false")
            git(clone, "config", "core.autocrlf", "false")
            git(clone, "checkout", "--quiet", "--detach", module["head"])
            work = Path(temporary) / (module["name"] + "-check")
            work.mkdir()
            index = {"GIT_INDEX_FILE": str(work / "index")}
            history = subjects(clone)  # the real checkout's history, before any rehearsal commit
            candidates, module["patches"] = module["patches"], []
            for patch in candidates:
                subject, diff = mail(clone, patch_bytes(patch), work)
                if any(same_change(clone, sha, diff, index) for sha in history.get(subject, ())):
                    continue  # the same change in history under another patch-id
                # Its whole change is already in the checkout when undoing it and
                # redoing it gives back exactly the rehearsal tip. Checked in series
                # order, so an entry that undoes a missing earlier one is not skipped.
                tip = value(clone, "rev-parse", "HEAD^{tree}")
                before = applied_tree(clone, tip, diff, index, "--reverse")
                if before and applied_tree(clone, before, diff, index) == tip:
                    if applied_tree(clone, tip, diff, index) not in (None, tip):
                        raise Refused(f"{module['name']}: {label(patch)}: ambiguous_installed_state")
                    continue
                module["patches"].append(patch)
                apply_entry(clone, module["name"], patch)
                patch["tree"] = value(clone, "rev-parse", "HEAD^{tree}")
                patch["result_patch_id"] = patch_id(clone, git(clone, "show", "--format=", "--binary", "HEAD").stdout)
            if module["name"] == "mod-playerbots":
                pin_guard(clone)
    for module in journal["modules"]:
        current = state(Path(module["path"]))
        if any(current[key] != module[key] for key in ("path", "gitdir", "gitdir_identity", "branch", "head")) or operations(current) or not clean(Path(module["path"])):
            raise Refused(f"{module['name']}: changed_during_prepare")
    save(path, journal)
    return journal


def identity(module: dict) -> dict:
    current = state(Path(module["path"]))
    if any(current[key] != module[key] for key in ("path", "gitdir", "gitdir_identity", "branch")):
        raise Refused(f"{module['name']}: repository_identity_changed")
    return current


def owned_head(module: dict, current: dict) -> bool:
    if current["head"] == module["expected"]:
        return True
    if module.get("rolling_back") and current["head"] == module["head"]:
        return True  # reset completed before its final journal checkpoint
    if module["pending"] is None:
        return False
    # git am may have committed before its success checkpoint was flushed.
    repo = Path(module["path"])
    patch = module["patches"][module["pending"]]
    parents = value(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    return (parents[1:] == [module["expected"]]
            and value(repo, "rev-parse", "HEAD^{tree}") == patch["tree"]
            and patch_id(repo, git(repo, "show", "--format=", "--binary", "HEAD").stdout) == patch["result_patch_id"])


def owned_am(module: dict, current: dict) -> bool:
    if module["pending"] is None or operations(current) != ["rebase-apply"]:
        return False
    amdir = Path(current["gitdir"]) / "rebase-apply"
    try:
        # git am keeps its original tip in ORIG_HEAD, unlike git rebase's
        # rebase-apply/orig-head. abort-safety also binds this exact session tip.
        original = value(Path(module["path"]), "rev-parse", "--verify", "ORIG_HEAD")
        abort_safety = (amdir / "abort-safety").read_text().strip()
        applying = (amdir / "applying").exists()
        raw = (amdir / "patch").read_bytes()
        patch = module["patches"][module["pending"]]
        expected = (patch["patch_id"], patch["result_patch_id"])
        return (original == module["expected"] and abort_safety == current["head"]
                and applying and patch_id(Path(module["path"]), raw) in expected)
    except (OSError, Refused):
        return False


def clean_or_owned_index(module: dict, current: dict) -> bool:
    repo = Path(module["path"])
    if clean(repo):
        return True
    # pre-applypatch or commit can fail after Git staged our complete patch.
    # Only discard that exact rehearsed tree, with no additional working edits.
    if not owned_am(module, current):
        return False
    if git(repo, "diff", "--quiet", check=False).returncode:
        return False
    if git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout:
        return False
    tree = git(repo, "write-tree", check=False)
    return (tree.returncode == 0 and tree.stdout.decode().strip()
            == module["patches"][module["pending"]]["tree"])


def apply(path: Path) -> dict:
    journal = load(path)
    # Check every checkout before the first mutation, even one with no new patch.
    for module in journal["modules"]:
        current = identity(module)
        if module["pending"] is not None or module.get("rolling_back"):
            raise Refused(f"{module['name']}: interrupted_apply_requires_rollback")
        if current["head"] != module["expected"] or operations(current) or not clean(Path(module["path"])):
            raise Refused(f"{module['name']}: changed_before_apply")
    for module in journal["modules"]:
        repo = Path(module["path"])
        for index in range(module["applied"], len(module["patches"])):
            current = identity(module)
            if current["head"] != module["expected"] or operations(current) or not clean(repo):
                raise Refused(f"{module['name']}: changed_before_patch")
            module["pending"] = index
            save(path, journal)
            apply_entry(repo, module["name"], module["patches"][index])
            current = identity(module)
            if not owned_head(module, current) or not clean(repo) or operations(current):
                raise Refused(f"{module['name']}: unexpected_applied_state")
            module["expected"] = current["head"]
            module["applied"] = index + 1
            module["pending"] = None
            save(path, journal)
        if module["name"] == "mod-playerbots":
            pin_guard(repo)
    return journal


def rollback(path: Path) -> dict:
    journal = load(path)
    # Refuse before resetting anything if another actor changed either checkout.
    # A failed commit may leave precisely our rehearsed patch staged. Unrelated
    # edits are never ours to discard, even during an owned am operation.
    for module in journal["modules"]:
        current = identity(module)
        if not owned_head(module, current) or not clean_or_owned_index(module, current):
            raise Refused(f"{module['name']}: foreign_changes_prevent_rollback")
        if operations(current) and not owned_am(module, current):
            raise Refused(f"{module['name']}: foreign_operation_prevents_rollback")
    for module in reversed(journal["modules"]):
        repo = Path(module["path"])
        current = identity(module)
        if not owned_head(module, current) or not clean_or_owned_index(module, current):
            raise Refused(f"{module['name']}: changed_during_rollback")
        if operations(current):
            if not owned_am(module, current):
                raise Refused(f"{module['name']}: foreign_operation_prevents_rollback")
            git(repo, "am", "--abort")
        module["rolling_back"] = True
        save(path, journal)
        git(repo, "reset", "--hard", module["head"])
        module["expected"] = module["head"]
        module["pending"] = None
        module["applied"] = 0
        module["rolling_back"] = False
        save(path, journal)
    return journal


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[0] == "prepare":
            result = prepare(*(Path(arg) for arg in argv[1:]))
        elif len(argv) == 2 and argv[0] in ("apply", "rollback", "summary"):
            action = {"apply": apply, "rollback": rollback, "summary": load}[argv[0]]
            result = action(Path(argv[1]))
        else:
            raise Refused("usage: prepare MODULES_ROOT SERIES_ROOT JOURNAL | apply/rollback/summary JOURNAL")
        print(json.dumps(summary(result), sort_keys=True))
        return 0
    except (Refused, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        print(f"FAIL: release patches: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
