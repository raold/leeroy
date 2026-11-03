"""Run the headless regression rehearsal in explicitly selected Python environments.

No dependencies are installed. Child pytest processes use the worktree's existing guarded
test fixtures. Every run retains synthetic-only data and output under --out, outside the
checkout. This is a cross-service regression test, not real device or deployment acceptance.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--mind-python", required=True, type=Path)
    parser.add_argument("--voice-python", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path,
                        help="parent of run artifacts, outside the source checkout")
    args = parser.parse_args()
    worktree = args.worktree.resolve(strict=True)
    here = Path(__file__).resolve().parent
    output_root = args.out.resolve()
    if output_root.is_relative_to(worktree):
        parser.error("--out must be outside --worktree so runtime artifacts cannot be committed")
    for project in ("mind", "voice"):
        assert (worktree / project / f"leeroy_{project}" / "__init__.py").is_file()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = output_root / f"{stamp}-{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True)
    processes, outputs = {}, []
    summary = {"passed": False, "run": str(root), "worktree": str(worktree),
               "synthetic_only": True, "live_device_acceptance": False,
               "limitations": ["No copied/live database migration rehearsal",
                               "No real model, microphone, playback device, GPU or Ollama",
                               "No WoW focus, elevation or Windows ducking acceptance"]}

    def start(project, python):
        # Resolving a POSIX venv's python symlink would bypass that virtual environment.
        executable = python.expanduser().absolute()
        assert executable.is_file(), f"missing {project} interpreter: {executable}"
        environment = os.environ.copy()
        # pytest's autouse fixtures select fresh scratch runtime directories.
        for key in ("LEEROY_MIND_DATA", "LEEROY_VOICE_DATA", "PYTEST_ADDOPTS"):
            environment.pop(key, None)
        environment.update(REHEARSAL_WORKTREE=str(worktree), REHEARSAL_RUN=str(root),
                           PYTHONPATH=os.pathsep.join(map(str, (worktree / project / "tests",
                                                                worktree / project, here))),
                           PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        output = (root / f"{project}-pytest.log").open("w", encoding="utf-8")
        outputs.append(output)
        command = [str(executable), "-B", "-m", "pytest", "-q", "--tb=short",
                   "-p", "no:cacheprovider", "-p", "conftest", "-c",
                   str(worktree / project / "pyproject.toml"), "--basetemp",
                   str(root / f"{project}-tmp"), str(here / f"test_{project}_leg.py")]
        process = subprocess.Popen(command, cwd=worktree / project, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes[project] = process

    try:
        start("voice", args.voice_python)
        deadline = time.monotonic() + 35
        while not (root / "voice-ready.json").exists():
            assert processes["voice"].poll() is None, "voice exited before readiness; see voice-pytest.log"
            assert time.monotonic() < deadline, "voice readiness timeout"
            time.sleep(0.05)
        start("mind", args.mind_python)
        deadline = time.monotonic() + 120
        while any(p.poll() is None for p in processes.values()):
            failed = {name: p.returncode for name, p in processes.items()
                      if p.poll() is not None and p.returncode != 0}
            assert not failed, f"child failures: {failed}; see pytest logs"
            assert time.monotonic() < deadline, "rehearsal timeout"
            time.sleep(0.05)
        codes = {name: p.returncode for name, p in processes.items()}
        summary["exit_codes"] = codes
        assert all(code == 0 for code in codes.values()), f"child failures: {codes}; see pytest logs"
        ports = []
        for project in ("mind", "voice"):
            result = json.loads((root / f"{project}-result.json").read_text(encoding="utf-8"))
            assert result["passed"]
            summary[project] = result
            ports.extend(result["ports"])
        assert len(ports) == len(set(ports)) == 4
        assert not set(ports) & {7878, 7879, 7880, 11434, 3306}
        for port in ports:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            except OSError:
                pass
            else:
                connection.close()
                raise AssertionError(f"temporary port still listening after child exit: {port}")
        summary.update(passed=True, ports_closed=True, processes_exited=True)
    except BaseException as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        forced = []
        if any(p.poll() is None for p in processes.values()):
            (root / "abort.json").write_text(json.dumps({"reason": summary.get("error", "stop")}),
                                            encoding="utf-8")
            # Let the sibling's guarded wait unwind its real service before terminating it.
            cleanup_deadline = time.monotonic() + 10
            while any(p.poll() is None for p in processes.values()) and time.monotonic() < cleanup_deadline:
                time.sleep(0.05)
        for name, process in processes.items():
            if process.poll() is None:
                forced.append(name)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for output in outputs:
            output.close()
        summary["forced_teardown"] = forced
        summary["child_exit_codes"] = {name: p.returncode for name, p in processes.items()}
        (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "summary": str(root / "summary.json"),
                      "error": summary.get("error")}, indent=2), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
