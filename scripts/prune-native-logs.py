#!/usr/bin/env python3
"""Prune native timestamp rotations older than 14 days; never prune active logs."""
import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

ROTATION = re.compile(r"(.+\.log)\.(\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\Z")


def prune(directory: Path, days: int = 14, *, now: datetime | None = None) -> int:
    if type(days) is not int or days < 1:
        raise ValueError("retention days must be a positive integer")
    # AppenderFile uses the server's local wall clock in the rotation filename.
    # mtime may predate the rotation (or change during a restore), so it is not
    # evidence of when this archive was made. Keep the exact cutoff and future
    # timestamps, including a clock rollback; invalid dates are also retained.
    cutoff = (now if now is not None else datetime.now()) - timedelta(days=days)
    directory = directory.resolve(strict=True)
    removed = 0
    for path in directory.iterdir():
        match = ROTATION.fullmatch(path.name)
        if match:
            try:
                rotated = datetime.strptime(match[2], "%Y-%m-%d %H-%M-%S")
            except ValueError:
                continue
            if rotated >= cutoff:
                continue
            # Individual children only: never follow a link or recurse into a directory.
            if path.is_symlink() or path.resolve().parent != directory or not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass  # Another startup may have pruned the same rotation.
            except PermissionError:
                pass  # A still-open native log must not prevent startup.
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--days", type=int, default=14, help="positive retention days (default: 14)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be a positive integer")
    print(f"native_log_rotations_pruned count={prune(args.directory, args.days)} days={args.days}")
