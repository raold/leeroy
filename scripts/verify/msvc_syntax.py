#!/usr/bin/env python3
"""MSVC syntax-only compile (`cl /Zs`) of changed mod-leeroy translation units, as the live build.

CI compiles with clang on Linux; the live release is MSVC with <windows.h> in reach, so a few
failures show up only there. This reads the RelWithDebInfo|x64 compile settings (include
directories, /external:I, defines, language standard, warnings) from the live build's
modules.vcxproj, points every mod-leeroy path at the checkout under test, and runs
`cl /Zs` on each selected file. /Zs parses and type-checks without writing anything, and
the compiler runs in a temporary directory: the build tree is only read.

    msvc_syntax.py --vcxproj FILE --live-module DIR --module DIR [--jobs N] CHANGED...

CHANGED are repository-relative paths below modules/mod-leeroy/src. A changed .cpp is
compiled itself; a changed header through one .cpp that includes it (directly or through
other headers), unless an already selected file covers it. Standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
CONFIG = "'RelWithDebInfo|x64'"
STANDARDS = {"stdcpp14": "/std:c++14", "stdcpp17": "/std:c++17", "stdcpp20": "/std:c++20",
             "stdcpplatest": "/std:c++latest"}
INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
DIAGNOSTIC = re.compile(r"\b(?:fatal )?error [A-Z]+\d+:|Command line error")


class Refused(Exception):
    pass


def norm(path: str) -> str:
    """A comparable form of a Windows path, wherever this runs (the self-test runs on Linux)."""
    return os.path.normcase(os.path.normpath(path.replace(chr(92), "/")))


def remap(path: str, live: str, module: str) -> str:
    """`path` with the live mod-leeroy directory swapped for the one under test."""
    key, base = norm(path), norm(live)
    if key == base:
        return module
    if key.startswith(base + os.sep):
        return str(Path(module) / os.path.relpath(key, base))
    return path


def settings(vcxproj: Path) -> dict[str, str]:
    try:
        root = ET.parse(vcxproj).getroot()
    except (OSError, ET.ParseError) as exc:
        raise Refused(f"cannot read {vcxproj.name}: {type(exc).__name__}") from exc
    for group in root.findall("m:ItemDefinitionGroup", NS):
        if CONFIG in (group.get("Condition") or ""):
            compile_ = group.find("m:ClCompile", NS)
            if compile_ is not None:
                return {child.tag.split("}", 1)[1]: (child.text or "") for child in compile_}
    raise Refused(f"no RelWithDebInfo|x64 ClCompile settings in {vcxproj.name}")


def options(cfg: dict[str, str], live: str, module: str) -> list[str]:
    """The cl arguments MSBuild would pass for these settings, mod-leeroy paths remapped."""
    args = ["/nologo", "/Zs", "/c"]
    standard = cfg.get("LanguageStandard", "")
    args.append(STANDARDS.get(standard, "/std:c++20"))
    if cfg.get("ExceptionHandling", "Sync") == "Sync":
        args.append("/EHsc")
    if cfg.get("RuntimeLibrary") == "MultiThreadedDLL":
        args.append("/MD")
    if cfg.get("UseStandardPreprocessor") == "true":
        args.append("/Zc:preprocessor")
    if cfg.get("ExternalWarningLevel") == "TurnOffAllWarnings":
        args.append("/external:W0")
    args += [f"/wd{w}" for w in cfg.get("DisableSpecificWarnings", "").split(";") if w.strip()]
    args += [f"/we{w}" for w in cfg.get("TreatSpecificWarningsAsErrors", "").split(";") if w.strip()]
    # posix=False keeps backslashes; the quotes come off by hand.
    extra = [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
             for t in shlex.split(cfg.get("AdditionalOptions", "").replace("%(AdditionalOptions)", ""), posix=False)]
    i = 0
    while i < len(extra):
        token = extra[i]
        if token.lower() in ("/external:i", "-external:i") and i + 1 < len(extra):
            args.append("/external:I" + remap(extra[i + 1], live, module))
            i += 2
            continue
        args.append(token)
        i += 1
    for define in cfg.get("PreprocessorDefinitions", "").split(";"):
        define = define.strip()
        if define and not define.startswith("%("):
            args.append("/D" + define)
    seen = set()
    includes = []
    for directory in cfg.get("AdditionalIncludeDirectories", "").split(";"):
        directory = directory.strip()
        if directory and not directory.startswith("%("):
            includes.append(remap(directory, live, module))
    # A directory the branch adds is not in the live project yet.
    for directory in sorted(p for p in (Path(module) / "src").rglob("*") if p.is_dir()):
        includes.append(str(directory))
    for directory in includes:
        if norm(directory) not in seen:
            seen.add(norm(directory))
            args.append("/I" + directory)
    return args


def response_line(arg: str) -> str:
    """One argument as a response-file token: quoted, with embedded quotes escaped."""
    return '"' + arg.replace("\\\"", "\\\\\"").replace('"', '\\"') + '"' if (" " in arg or '"' in arg) else arg


def includes_of(path: Path) -> list[str]:
    try:
        return INCLUDE.findall(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def select(module: Path, changed: list[str]) -> tuple[list[Path], list[str]]:
    """The .cpp files to compile for these changed module files, and the headers left without
    an includer. Headers are matched by file name, as the flat include directories allow."""
    src = module / "src"
    cpps = sorted(src.rglob("*.cpp"))
    headers = {p.name: p for p in src.rglob("*.h")}

    def closure(cpp: Path) -> set[str]:
        seen: set[str] = set()
        todo = [Path(name).name for name in includes_of(cpp)]
        while todo:
            name = todo.pop()
            if name in seen or name not in headers:
                continue
            seen.add(name)
            todo += [Path(inner).name for inner in includes_of(headers[name])]
        return seen

    def pure(cpp: Path) -> bool:
        # src/Lib is the pure layer: no game headers, so no <windows.h> either.
        return (src / "Lib") in cpp.parents

    chosen: list[Path] = []
    covered: set[str] = set()       # headers a chosen game TU (outside src/Lib) reaches
    for name in changed:
        path = module.parent.parent / name
        if path.suffix == ".cpp" and path.is_file() and path not in chosen:
            chosen.append(path)
            if not pure(path):
                covered |= closure(path)
    orphans = []
    for name in changed:
        path = module.parent.parent / name
        if path.suffix not in (".h", ".hpp") or not path.is_file():
            continue
        # The header's own .cpp, plus a game TU that reaches it: a header such as
        # Lib/LeeroyStrings.h breaks only where the game headers bring in <windows.h>.
        own = [c for c in cpps if c.stem == path.stem and path.name in closure(c)]
        for cpp in own:
            if cpp not in chosen:
                chosen.append(cpp)
            if not pure(cpp):
                covered |= closure(cpp)
        if path.name in covered:
            continue
        game = next((c for c in cpps if not pure(c) and path.name in closure(c)), None)
        if game is not None:
            if game not in chosen:
                chosen.append(game)
            covered |= closure(game)
        elif not own:
            orphans.append(name)
    return chosen, orphans


def vcvars_env() -> dict[str, str]:
    script = os.environ.get("VERIFY_VCVARS")
    if not script:
        vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
            "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.is_file():
            raise Refused("vswhere.exe not found (set VERIFY_VCVARS to vcvars64.bat)")
        found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                                "installationPath"], stdout=subprocess.PIPE, text=True).stdout.strip()
        if not found:
            raise Refused("no Visual Studio with the C++ x64 tools")
        script = str(Path(found.splitlines()[0]) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat")
    if not Path(script).is_file():
        raise Refused("vcvars64.bat not found")
    # A string, not a list: Windows passes it to cmd verbatim, as cmd's quoting needs.
    out = subprocess.run(f'cmd /d /s /c ""{script}" >nul 2>&1 && set"', stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, errors="replace").stdout
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("="))
    path = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    # CreateProcess searches the parent's PATH, not env's: name the compiler in full.
    env["VERIFY_CL"] = shutil.which("cl", path=path) or ""
    if not env["VERIFY_CL"] or not any(k.upper() == "INCLUDE" for k in env):
        raise Refused("vcvars64.bat did not set up the compiler environment")
    return env


def compile_one(cpp: Path, rsp: Path, env: dict[str, str], work: str) -> tuple[Path, int, list[str], float]:
    started = time.monotonic()
    result = subprocess.run([env["VERIFY_CL"], f"@{rsp}", str(cpp)], cwd=work, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = [line.strip() for line in result.stdout.splitlines() if DIAGNOSTIC.search(line)]
    return cpp, result.returncode, lines, time.monotonic() - started


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vcxproj", required=True, type=Path)
    parser.add_argument("--live-module", required=True, help="mod-leeroy as the live project names it")
    parser.add_argument("--module", required=True, type=Path, help="mod-leeroy in the checkout under test")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--plan", action="store_true", help="print the selected files and stop")
    parser.add_argument("changed", nargs="*")
    args = parser.parse_args(argv)
    module = args.module.resolve()
    chosen, orphans = select(module, args.changed)
    for name in orphans:
        print(f"note msvc {name}: no .cpp includes it")
    if args.plan or not chosen:
        for cpp in chosen:
            print(f"plan msvc {cpp.relative_to(module.parent.parent).as_posix()}")
        if not chosen:
            print("note msvc: no translation unit to compile")
        return 0
    try:
        cl_args = options(settings(args.vcxproj), args.live_module, str(module))
        env = vcvars_env()
    except Refused as exc:
        print(f"FAIL msvc: {exc}")
        return 1
    failed = 0
    with tempfile.TemporaryDirectory(prefix="leeroy-verify-msvc-") as work:
        rsp = Path(work) / "cl.rsp"
        rsp.write_text("\n".join(response_line(a) for a in cl_args) + "\n", encoding="utf-8")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            jobs = [pool.submit(compile_one, cpp, rsp, env, work) for cpp in chosen]
            for job in jobs:
                cpp, code, lines, seconds = job.result()
                name = cpp.relative_to(module.parent.parent).as_posix()
                if code == 0 and not lines:
                    print(f"ok   msvc {name} ({seconds:.0f} s)")
                    continue
                failed += 1
                print(f"FAIL msvc {name} (exit {code})")
                for line in lines[:20]:
                    print(f"       {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
