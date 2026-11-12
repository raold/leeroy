#!/usr/bin/env python3
"""Prune AppenderFile timestamp rotations in one directory, keeping ten per log."""
import argparse
import re
from collections import defaultdict
from pathlib import Path

ROTATION = re.compile(r"(.+\.log)\.(\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\Z")


def prune(directory: Path, keep: int = 10) -> int:
    directory = directory.resolve(strict=True)
    groups = defaultdict(list)
    for path in directory.iterdir():
        match = ROTATION.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            groups[match[1]].append(path)
    removed = 0
    for paths in groups.values():
        for path in sorted(paths, key=lambda p: p.name, reverse=True)[keep:]:
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
    args = parser.parse_args()
    print(f"native_log_rotations_pruned count={prune(args.directory)}")
