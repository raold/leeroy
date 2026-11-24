#!/usr/bin/env python3
"""Prove a branch's mind.db migrations on a copy, never on the database they came from.

    migrate_check.py numbering --repo DIR --base REF
        the branch's new migration files against the merge target's: no number taken twice,
        no hole (the mind refuses to open a database whose migrations skip a number)
    migrate_check.py live --source DB --mind DIR
        SQLite online backup of DB (opened read-only) to a temporary directory, the branch's
        `db migrate` on the copy, PRAGMA integrity_check, then the copy is deleted
    migrate_check.py fixture --base-mind DIR --mind DIR
        a fresh database at the base code's schema, then the same migrate and checks

DIR is a checkout's mind/ folder; its code runs with that folder first on sys.path, in a child
process of this interpreter (the mind's venv). Prints counts, versions and sizes, never a row.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MIGRATIONS = "mind/leeroy_mind/storage/migrations"
NUMBERED = re.compile(r"^(\d{4})_.*\.sql$")


class Failed(Exception):
    pass


def numbers(names: list[str]) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for name in names:
        match = NUMBERED.match(name)
        if match:
            found.setdefault(int(match.group(1)), []).append(name)
    return found


def git_names(repo: Path, ref: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", f"{ref}:{MIGRATIONS}"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return out.stdout.split() if out.returncode == 0 else []


def numbering(repo: Path, base: str) -> str:
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"{base}^{{commit}}"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise Failed(f"unknown base {base}")
    base_names = git_names(repo, base)
    head_names = git_names(repo, "HEAD")
    merge_base = subprocess.run(["git", "-C", str(repo), "merge-base", base, "HEAD"],
                                stdout=subprocess.PIPE, text=True).stdout.strip()
    fork_names = set(git_names(repo, merge_base)) if merge_base else set()
    added = [name for name in head_names if name not in fork_names]
    merged = numbers(sorted(set(base_names) | set(added)))
    taken = {n: names for n, names in merged.items() if len(names) > 1}
    if taken:
        raise Failed("migration number taken on the merge target, renumber after rebasing: "
                     + ", ".join(f"{n:04d} ({' / '.join(names)})" for n, names in sorted(taken.items())))
    top = max(merged, default=0)
    holes = sorted(set(range(1, top + 1)) - set(merged))
    if holes:
        raise Failed("merged numbering skips " + ", ".join(f"{n:04d}" for n in holes)
                     + ": the mind refuses to open such a database")
    return f"{len(added)} new migration(s), merged numbering 0001-{top:04d} without a hole"


def latest(mind: Path) -> int:
    return max(numbers([p.name for p in (mind / "leeroy_mind" / "storage" / "migrations").iterdir()]), default=0)


def child_env(mind: Path, data: Path) -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=str(mind), PYTHONDONTWRITEBYTECODE="1", LEEROY_MIND_DATA=str(data))
    env.pop("PYTHONHOME", None)
    return env


def run_code(mind: Path, data: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=mind, env=child_env(mind, data),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")


def same_code(mind: Path, data: Path) -> None:
    """The child imports leeroy_mind from `mind`, not from an installed or editable copy."""
    out = run_code(mind, data, "-c", "import leeroy_mind; print(leeroy_mind.__file__)")
    where = out.stdout.strip().splitlines()[-1:] or [""]
    if out.returncode or Path(mind / "leeroy_mind") not in Path(where[0]).resolve().parents:
        raise Failed("the child would not import leeroy_mind from the checkout under test")


def version(db: Path) -> int:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def migrate(mind: Path, data: Path) -> None:
    same_code(mind, data)
    out = run_code(mind, data, "-m", "leeroy_mind", "db", "--data-dir", str(data), "migrate")
    if out.returncode:
        raise Failed(f"db migrate exited {out.returncode}: {last_error(out.stdout)}")


def last_error(output: str) -> str:
    """The failure's last line when its kind cannot carry row data (SQLite's own errors and the
    migrator's refusals name tables, columns and versions), else the exception class alone: a
    data step's error could quote a stored value."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "no output"
    line = lines[-1]
    kind = re.match(r"^([A-Za-z_][\w.]*)(?::|$)", line)
    if not kind:
        return line[:200]
    if kind.group(1).startswith("sqlite3.") or kind.group(1) == "RuntimeError":
        return line[:200]
    return kind.group(1)


def integrity(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        try:
            import sqlite_vec  # the vec0 tables check through their module when it is there
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.Error):
            pass
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    if rows != ["ok"]:
        raise Failed(f"integrity_check reported {len(rows)} problem(s)")


def checked(db: Path, mind: Path, before: int) -> str:
    integrity(db)
    after, want = version(db), latest(mind)
    if after != want:
        raise Failed(f"copy is at v{after} after migrate, the branch's code is v{want}")
    return f"v{before} -> v{after}, integrity ok"


def live(source: Path, mind: Path, scratch: Path | None) -> str:
    if not source.is_file():
        raise Failed(f"no database at {source}")
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="leeroy-verify-db-", dir=scratch))
    try:
        data = work / "data"
        data.mkdir()
        copy = data / "mind.db"
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)  # one step: a single read transaction, never blocks the writer
            finally:
                dst.close()
        finally:
            src.close()
        before = version(copy)
        size = copy.stat().st_size / 1e9
        migrate(mind, data)
        result = checked(copy, mind, before)
        return f"live mind.db copy ({size:.1f} GB) {result} in {time.monotonic() - started:.0f} s"
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"note: could not delete the temporary copy at {work}", file=sys.stderr)


def fixture(base_mind: Path, mind: Path, scratch: Path | None) -> str:
    work = Path(tempfile.mkdtemp(prefix="leeroy-verify-db-", dir=scratch))
    try:
        data = work / "data"
        data.mkdir()
        db = data / "mind.db"
        same_code(base_mind, data)
        out = run_code(base_mind, data, "-c", "import sys; from pathlib import Path; "
                       "from leeroy_mind.storage.db import open_db; open_db(Path(sys.argv[1])).close()", str(db))
        if out.returncode:
            raise Failed("the base code could not create its schema")
        before = version(db)
        migrate(mind, data)
        return "fixture at the base schema " + checked(db, mind, before)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("numbering")
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--base", required=True)
    p = sub.add_parser("live")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--mind", type=Path, required=True)
    p.add_argument("--scratch", type=Path)
    p = sub.add_parser("fixture")
    p.add_argument("--base-mind", type=Path, required=True)
    p.add_argument("--mind", type=Path, required=True)
    p.add_argument("--scratch", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "numbering":
            print(numbering(args.repo, args.base))
        elif args.action == "live":
            print(live(args.source, args.mind.resolve(), args.scratch))
        else:
            print(fixture(args.base_mind.resolve(), args.mind.resolve(), args.scratch))
        return 0
    except Failed as exc:
        print(exc)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__} while checking")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
