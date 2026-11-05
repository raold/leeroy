"""Read bounded, repository-local Git revision evidence without executing Git.

Missing, malformed, escaped or linked metadata is unknown, never stable evidence.
External worktree metadata requires Git's exact backlink and standard layout.
"""

import json
from pathlib import Path
import re

MODULES = ("mod-playerbots", "mod-dungeon-clear")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _inside(path, root):
    path = Path(path).absolute()
    # Reject links/junctions before resolution, including every ancestor.
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("linked Git metadata")
        if part == root:
            break
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("external Git metadata")
    return resolved


def _read(path, root, limit=8192):
    path = _inside(path, root)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversize Git metadata")
    return data.decode("ascii").strip()


def _head(repo, root):
    git = _inside(repo / ".git", root)
    metadata_root = root
    if git.is_file():
        text = _read(git, root)
        if not text.startswith("gitdir: ") or "\n" in text:
            raise ValueError("invalid gitdir")
        target = (repo / text[8:]).absolute()
        try:
            git = _inside(target, root)
        except ValueError:
            # A standard external linked worktree can prove its association with
            # this exact supplied checkout. Never accept an arbitrary gitdir or
            # follow its arbitrary commondir. Only fixed Git metadata is read.
            if target.parent.name != "worktrees" or target.parent.parent.name != ".git":
                raise ValueError("external gitdir is not a linked worktree") from None
            for part in (target, *target.parents):
                if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                    raise ValueError("linked external metadata")
            git = target.resolve()
            metadata_root = git.parent.parent
            backlink = _read(git / "gitdir", metadata_root)
            if Path(backlink).absolute() != (repo / ".git").absolute():
                raise ValueError("worktree backlink mismatch")
            if _read(git / "commondir", metadata_root) != "../..":
                raise ValueError("invalid external common directory")
    common = git
    if (git / "commondir").exists():
        common = _inside(git / _read(git / "commondir", metadata_root), metadata_root)
    head = _read(git / "HEAD", metadata_root)
    if _SHA.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        raise ValueError("invalid HEAD")
    ref = head[5:]
    if (not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref)
            or any(p in ("", ".", "..") for p in ref.split("/"))):
        raise ValueError("invalid ref")
    for folder in dict.fromkeys((git, common)):
        path = folder / ref
        if path.exists():
            sha = _read(path, metadata_root)
            if not _SHA.fullmatch(sha):
                raise ValueError("invalid revision")
            return sha
    for line in _read(common / "packed-refs", metadata_root, 2 * 1024 * 1024).splitlines():
        if line.startswith(("#", "^")):
            continue
        parts = line.split(" ")
        if len(parts) == 2 and parts[1] == ref and _SHA.fullmatch(parts[0]):
            return parts[0]
    raise ValueError("missing revision")


def revisions(core_dir: Path) -> dict[str, str]:
    root = Path(core_dir).absolute()
    result = {}
    for module in MODULES:
        try:
            result[module] = _head(root / "modules" / module, root)
        except (OSError, ValueError, UnicodeError):
            pass
    return result


def known(revs):
    return (isinstance(revs, dict) and set(revs) == set(MODULES)
            and all(isinstance(v, str) and _SHA.fullmatch(v) for v in revs.values()))


def drift_due(conn, now_revs) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='tg_revs'").fetchone()
    try:
        old = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        old = None
    return not known(now_revs) or not known(old) or old != now_revs
