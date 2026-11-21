#!/usr/bin/env python3
"""Plan/apply a release in Git; no application imports or service connections."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class Refused(RuntimeError):
    pass


def git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true"),
                            timeout=180)
    if check and result.returncode:
        raise Refused(f"git {args[0]} failed")
    return result


def text(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.decode("utf-8").strip()


def clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain", "--untracked-files=normal").stdout


def nonruntime(path: str) -> bool:
    return path.startswith((".github/", "mind/tests/"))


def unmerged(repo: Path) -> list[str]:
    return [os.fsdecode(p) for p in git(repo, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split(b"\0") if p]


def picking(repo: Path) -> bool:
    return git(repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False).returncode == 0


def other_operation(repo: Path) -> bool:
    return (git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
            or any(Path(text(repo, "rev-parse", "--path-format=absolute", "--git-path", name)).exists()
                   for name in ("rebase-apply", "rebase-merge", "sequencer", "REVERT_HEAD")))


def empty_pick(repo: Path) -> bool:
    return (picking(repo) and not unmerged(repo)
            and git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0
            and git(repo, "diff", "--quiet", check=False).returncode == 0)


def pick(repo: Path, sha: str, main: str) -> bool:
    """Use Git's actual application result, including an already-applied merge's combined diff.

    A patch-id in old history is insufficient: it may have subsequently been reverted.
    Only a confirmed empty cherry-pick is skipped; other errors remain errors.
    """
    if git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode == 0:
        return False  # Preserve any newer changes on top of an installed original commit.
    parents = text(repo, "rev-list", "--parents", "-n", "1", sha).split()
    args = ["cherry-pick", *( ["-m", "1"] if len(parents) > 2 else []), sha]
    if git(repo, *args, check=False).returncode == 0:
        return True
    conflicts = unmerged(repo)
    if any(not nonruntime(path) for path in conflicts):
        raise Refused(f"runtime cherry-pick conflict: {sha}")
    for path in conflicts:
        if git(repo, "cat-file", "-e", f"{main}:{path}", check=False).returncode == 0:
            git(repo, "restore", f"--source={main}", "--staged", "--worktree", "--", path)
        else:
            git(repo, "rm", "-f", "--ignore-unmatch", "--", path)
    if empty_pick(repo):
        git(repo, "cherry-pick", "--skip")
        if not clean(repo):
            raise Refused("empty cherry-pick left an unclean checkout")
        return False
    if conflicts and picking(repo) and not unmerged(repo):
        git(repo, "cherry-pick", "--continue")
        if clean(repo):
            return True
    raise Refused(f"cherry-pick failed: {sha}")


def save(path: Path, value: dict) -> None:
    pending = path.with_suffix(path.suffix + ".tmp")
    with pending.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def plan(root: Path, main: str, output: Path, native: bool, commits: list[str]) -> None:
    pre = text(root, "rev-parse", "HEAD")
    branch = text(root, "symbolic-ref", "HEAD")
    if not clean(root):
        raise Refused("working tree changed during preflight")
    rows = []
    with tempfile.TemporaryDirectory(prefix="ac-release-plan.") as temporary:
        stage = Path(temporary) / "checkout"
        result = subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(root), str(stage)],
                                capture_output=True, timeout=180)
        if result.returncode:
            raise Refused("cannot create isolated release plan")
        git(stage, "config", "user.name", "Release plan")
        git(stage, "config", "user.email", "release-plan@example.invalid")
        git(stage, "config", "commit.gpgsign", "false")
        git(stage, "config", "core.autocrlf", "false")
        git(stage, "checkout", "--quiet", "--detach", pre)
        for sha in commits:
            changed = pick(stage, sha, main)
            rows.append({"sha": sha, "changed": changed,
                         "tree": text(stage, "rev-parse", "HEAD^{tree}")})
            print(f"{'planned' if changed else 'already on the live branch; skipping'}: {sha}", flush=True)
        missing = 0
        if native:
            helper = Path(__file__).with_name("ac-release-patches.py")
            result = subprocess.run([sys.executable, str(helper), "prepare",
                str(root / "azerothcore-wotlk/modules"), str(stage / "patches"),
                str(output.with_name("modules.json"))], capture_output=True, text=True, timeout=600)
            if result.returncode:
                # The patch helper emits only bounded paths, IDs and failure codes.
                raise Refused(result.stderr.strip() or "module patch preflight failed")
            missing = json.loads(result.stdout)["missing"]
        voice = any(row["changed"] and git(stage, "diff-tree", "-r", "--no-commit-id", "--name-only",
                    row["sha"] + "^1", row["sha"], "--", "voice/").stdout for row in rows)
        save(output, {"pre": pre, "branch": branch, "main": main, "commits": rows,
                      "missing": missing, "voice": bool(voice), "phase": "prepared",
                      "expected": pre, "pending": None})


def apply(root: Path, path: Path) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if (text(root, "rev-parse", "HEAD") != plan["pre"]
            or text(root, "symbolic-ref", "HEAD") != plan["branch"] or not clean(root)
            or picking(root) or other_operation(root) or plan["phase"] != "prepared"):
        raise Refused("checkout changed after release preflight")
    plan["phase"] = "applying"
    save(path, plan)
    for row in plan["commits"]:
        if row["changed"]:
            plan["pending"] = row
            save(path, plan)
            if not pick(root, row["sha"], plan["main"]):
                raise Refused("release application differs from its plan")
        if text(root, "rev-parse", "HEAD^{tree}") != row["tree"] or not clean(root):
            raise Refused("release tree differs from its plan")
        plan["expected"] = text(root, "rev-parse", "HEAD")
        plan["pending"] = None
        save(path, plan)
    plan["phase"] = "applied"
    save(path, plan)


def rollback(root: Path, path: Path) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if text(root, "symbolic-ref", "HEAD") != plan["branch"] or other_operation(root):
        raise Refused("foreign checkout operation; rollback refused")
    head = text(root, "rev-parse", "HEAD")
    if plan["phase"] in ("rolling_back", "rolled_back") and head == plan["pre"] and clean(root):
        plan["phase"] = "rolled_back"
        save(path, plan)
        return
    pending = plan["pending"]
    owned_commit = (pending and text(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [plan["expected"]]
                    and text(root, "rev-parse", "HEAD^{tree}") == pending["tree"])
    if head != plan["expected"] and not owned_commit:
        raise Refused("foreign checkout commit; rollback refused")
    if picking(root):
        if not pending or text(root, "rev-parse", "CHERRY_PICK_HEAD") != pending["sha"]:
            raise Refused("foreign cherry-pick; rollback refused")
        staged = git(root, "write-tree", check=False)
        if (staged.returncode or staged.stdout.decode().strip() != pending["tree"]
                or git(root, "diff", "--quiet", check=False).returncode
                or git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout):
            raise Refused("foreign checkout edits; rollback refused")
        git(root, "cherry-pick", "--abort")
    if not clean(root):
        raise Refused("foreign checkout edits; rollback refused")
    plan["phase"] = "rolling_back"
    plan["expected"] = text(root, "rev-parse", "HEAD")
    plan["pending"] = None
    save(path, plan)
    git(root, "reset", "--hard", plan["pre"])
    plan["phase"] = "rolled_back"
    plan["expected"] = plan["pre"]
    save(path, plan)


def cleanup(path: Path) -> None:
    # This is the private mktemp directory created by the shell entry point, never a
    # checkout, data directory, module or caller-computed deletion target.
    resolved = path.resolve(strict=True)
    if (path.is_symlink() or not resolved.name.startswith("ac-release.")
            or (resolved / ".release-runtime").read_text() != "leeroy-release-runtime\n"):
        raise Refused("not an owned release runtime")
    shutil.rmtree(resolved)


def freeze_controls(root: Path, output: Path) -> None:
    """Retain the service-stop code and its imports across a scripts/runtime update."""
    server = (root / "scripts/ac-server.sh").read_text(encoding="utf-8")
    if "$AC_ROOT/scripts/" in server or "${AC_ROOT}/scripts/" in server:
        raise Refused("install the bundled relocatable ac-server.sh before using the new release tool")
    scripts = output / "scripts"
    scripts.mkdir(parents=True)
    for name in ("common.sh", "ac-server.sh", "ac-mind.py", "ac-soap.py"):
        source = root / "scripts" / name
        if source.exists():
            shutil.copy2(source, scripts / name)
    package = root / "mind/leeroy_mind"
    if package.is_dir():
        shutil.copytree(package, output / "mind/leeroy_mind", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def main(args: list[str]) -> int:
    try:
        match args:
            case ["plan", root, revision, output, native, *commits]:
                plan(Path(root), revision, Path(output), native == "1", commits)
            case ["apply", root, path]:
                apply(Path(root), Path(path))
            case ["rollback", root, path]:
                rollback(Path(root), Path(path))
            case ["stats", path]:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                values = (sum(row["changed"] for row in value["commits"]), value["missing"], int(value["voice"]))
                sys.stdout.buffer.write((" ".join(map(str, values)) + "\n").encode("ascii"))
            case ["freeze-controls", root, output]:
                freeze_controls(Path(root), Path(output))
            case ["cleanup", path]:
                cleanup(Path(path))
            case _:
                raise Refused("invalid release Git helper arguments")
    except (Refused, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc if isinstance(exc, Refused) else type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
