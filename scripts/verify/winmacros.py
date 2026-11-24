#!/usr/bin/env python3
"""Flag identifiers that <windows.h> turns into macros, in C++ lines a diff adds.

The live build is MSVC with <windows.h> reachable from the game headers; minwindef.h and
rpcndr.h define near, far (empty), small (char) and others. A field named `near` compiles
with clang on Linux (CI) and fails only in the native release. Comments, string and
character literals are blanked first, so prose such as "the nearest boss" is never flagged.

    winmacros.py --base REF [--repo DIR]   scan lines added in REF...HEAD under modules/*/src/
    winmacros.py --all FILE...             scan whole files (self-test, audits)

Prints one line per finding and exits 1 when there is any; standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MACROS = ("near", "far", "small", "IN", "OUT", "OPTIONAL", "ERROR", "DELETE", "interface")
TOKEN = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(MACROS) + r")(?![A-Za-z0-9_])")
# Code the game TUs compile: a module's src/. Its tests and tools never see windows.h.
SOURCE = re.compile(r"^modules/[^/]+/src/.+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|inl|ipp)$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
RAW_OPEN = re.compile(r'(?:u8|u|U|L)?R"([^()\\ \t\n]{0,16})\(')


def blank(text: str) -> str:
    """The text with comments and literals replaced by spaces, newlines kept, so line numbers
    stay valid. Handles //, /* */, "..." and '...' with escapes, raw strings and line splices."""
    out = list(text)
    i, n = 0, len(text)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = i
            while j < n and text[j] != "\n":
                j += 2 if text[j] == "\\" and j + 1 < n and text[j + 1] == "\n" else 1
            wipe(i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            wipe(i, j)
            i = j
        elif c in "uULR":
            raw = RAW_OPEN.match(text, i)
            if raw and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                close = text.find(")" + raw.group(1) + '"', raw.end())
                j = n if close < 0 else close + len(raw.group(1)) + 2
                wipe(i, j)
                i = j
            else:
                j = i
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                i = max(j, i + 1)
        elif c.isdigit():
            # A whole number, digit separators (1'000'000) included: not a character literal.
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_.'"
                             or (text[j] in "+-" and text[j - 1] in "eEpP")):
                j += 1
            i = j
        elif c == '"' or c == "'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            wipe(i, j + 1)
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)


def findings(text: str, lines: set[int] | None = None) -> list[tuple[int, str]]:
    """(line, macro) for each flagged token; `lines` limits the scan to those 1-based lines.
    Preprocessor #include/#pragma/#define/#undef lines are skipped: a header name is not an
    identifier, and a deliberate #define or #undef of one of these macros is the point."""
    found = []
    for number, line in enumerate(blank(text).splitlines(), 1):
        if lines is not None and number not in lines:
            continue
        if re.match(r"\s*#\s*(?:include|pragma|define|undef)\b", line):
            continue
        for match in TOKEN.finditer(line):
            found.append((number, match.group(1)))
    return found


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.decode("utf-8", "replace")


def added_lines(repo: Path, base: str, path: str) -> set[int]:
    lines: set[int] = set()
    for row in git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...HEAD", "--", path).splitlines():
        hunk = HUNK.match(row)
        if hunk:
            start, count = int(hunk.group(1)), int(hunk.group(2) or "1")
            lines.update(range(start, start + count))
    return lines


def scan_diff(repo: Path, base: str) -> int:
    names = git(repo, "diff", "--name-only", "--diff-filter=AMR", "-z", f"{base}...HEAD").split("\0")
    hits = 0
    files = [name for name in names if SOURCE.match(name)]
    for name in files:
        text = git(repo, "show", f"HEAD:{name}")
        for number, macro in findings(text, added_lines(repo, base, name)):
            print(f"{name}:{number}: '{macro}' is a <windows.h> macro under MSVC; rename it")
            hits += 1
    print(f"scanned {len(files)} changed C++ file(s), {hits} finding(s)")
    return 1 if hits else 0


def scan_files(paths: list[str]) -> int:
    hits = 0
    for name in paths:
        text = Path(name).read_text(encoding="utf-8", errors="replace")
        for number, macro in findings(text):
            print(f"{name}:{number}: '{macro}' is a <windows.h> macro under MSVC; rename it")
            hits += 1
    return 1 if hits else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--all", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    if args.all:
        return scan_files(args.all)
    if not args.base:
        parser.error("--base or --all is required")
    try:
        return scan_diff(Path(args.repo), args.base)
    except subprocess.CalledProcessError as exc:
        print(f"git failed: {' '.join(exc.cmd[3:5])}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
