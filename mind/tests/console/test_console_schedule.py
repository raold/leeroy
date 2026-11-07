"""scripts/ac-console-schedule.ps1: the logon task that keeps the console running hidden, for
the tailnet. Its -Show builds the task -Install would register, in memory (New-ScheduledTask
registers nothing), and prints it: that definition is what is checked here, on a scratch
checkout. Nothing installs, removes or queries a task, and the tailnet name is a made-up one."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ac-console-schedule.ps1"
NAME = "pc.example-tailnet.ts.net"
SHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="ascii")  # ASCII: Windows PowerShell 5.1 reads it as ANSI


def powershell(*args, timeout=60):
    return subprocess.run([SHELL, "-NoProfile", "-NonInteractive", *args], capture_output=True,
                          text=True, timeout=timeout)


def test_it_names_no_tailnet(source):
    assert not re.search(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", source.replace("<pc>.<tailnet>", ""))


def test_nothing_catches_its_output(source):
    # the start line holds the key, and goes nowhere hidden
    assert "Out-File" not in source and "Start-Transcript" not in source
    assert not re.search(r"[A-Za-z]:\\\\?azerothcore", source)  # paths come from the checkout


@pytest.mark.skipif(SHELL is None, reason="no PowerShell here")
def test_powershell_parses_it():
    script = str(SCRIPT).replace("'", "''")
    out = powershell("-Command",
                     "$e = $null; $null = [System.Management.Automation.Language.Parser]::"
                     f"ParseFile('{script}', [ref]$null, [ref]$e); $e.Count")
    assert out.returncode == 0 and out.stdout.strip() == "0", out.stdout + out.stderr


@pytest.mark.skipif(SHELL is None, reason="no PowerShell here")
def test_install_and_show_need_the_tailnet_name():
    script = str(SCRIPT).replace("'", "''")
    out = powershell("-Command", f"$c = Get-Command '{script}'; foreach ($s in 'Install', 'Show', "
                     "'Remove', 'Status') { $p = $c.Parameters['AllowHost'].ParameterSets[$s]; "
                     "\"$s=$(if ($p) { $p.IsMandatory } else { 'none' })\" }")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["Install=True", "Show=True", "Remove=none", "Status=none"]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / "mind" / ".venv" / "Scripts").mkdir(parents=True)
    (root / "mind" / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")  # never run
    (tmp_path / "mind-data").mkdir()
    return root


def show(root, *args):
    out = powershell("-File", str(SCRIPT), "-Show", *args, "-Root", str(root))
    return out, (json.loads(out.stdout) if out.returncode == 0 else None)


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler's cmdlets are Windows-only")
def test_the_task_it_would_register(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    data = tmp_path / "mind-data"
    out, task = show(checkout, "-AllowHost", NAME, "-DataDir", str(data))
    assert task is not None, out.stdout + out.stderr
    key = tmp_path / "appdata" / "leeroy" / "console.key"
    assert task["TaskName"] == "leeroy-console"
    assert task["Execute"] == str(checkout / "mind" / ".venv" / "Scripts" / "pythonw.exe")
    assert task["WorkingDirectory"] == str(checkout / "mind")
    assert task["Arguments"] == (f'-m leeroy_mind console --root "{checkout}" --data-dir "{data}" '
                                 f'--key-file "{key}" --allow-host {NAME}')
    assert not key.parent.exists()  # -Show makes nothing
    kinds = {t["Kind"]: t for t in task["Triggers"]}
    assert set(kinds) == {"MSFT_TaskLogonTrigger", "MSFT_TaskTimeTrigger"}
    assert kinds["MSFT_TaskLogonTrigger"]["UserId"] == task["Principal"]["UserId"]
    assert kinds["MSFT_TaskTimeTrigger"]["Repetition"] == "PT5M"
    assert task["Principal"]["LogonType"] == "Interactive"
    assert task["Principal"]["RunLevel"] == "Limited"
    settings = task["Settings"]
    assert settings["MultipleInstances"] == "IgnoreNew"
    assert settings["ExecutionTimeLimit"] == "PT0S"  # no limit: the default ends it at 72 h
    assert (settings["RestartCount"], settings["RestartInterval"]) == (3, "PT1M")
    assert settings["Priority"] == 4  # normal: the default 7 is below normal
    assert settings["StartWhenAvailable"] is True
    assert settings["DisallowStartIfOnBatteries"] is False
    assert settings["StopIfGoingOnBatteries"] is False


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler's cmdlets are Windows-only")
def test_a_named_key_file_and_a_bad_name(checkout, tmp_path):
    key = tmp_path / "keys" / "console.key"
    out, task = show(checkout, "-AllowHost", NAME, "-DataDir", str(tmp_path / "mind-data"),
                     "-KeyFile", str(key))
    assert task is not None and f'--key-file "{key}"' in task["Arguments"], out.stderr
    for bad in ("evil", "pc example.ts.net", "pc.ts.net/x", "https://pc.ts.net"):
        out, task = show(checkout, "-AllowHost", bad, "-DataDir", str(tmp_path / "mind-data"))
        assert task is None and "not a host name" in out.stderr, bad
    out, task = show(tmp_path / "nowhere", "-AllowHost", NAME)
    assert task is None and "install root not found" in out.stderr
