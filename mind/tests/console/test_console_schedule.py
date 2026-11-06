"""scripts/ac-console-schedule.ps1: the logon task that keeps the console running hidden, for
the tailnet. Read as text (and parsed by PowerShell where there is one); nothing here installs,
removes or queries a task."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ac-console-schedule.ps1"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="ascii")  # ASCII: Windows PowerShell 5.1 reads it as ANSI


def test_it_offers_install_remove_and_status(source):
    for switch in ("$Install", "$Remove", "$Status"):
        assert f"[switch] {switch}" in source
    assert "DefaultParameterSetName = 'Status'" in source


def test_it_runs_this_checkouts_console_hidden_with_a_stable_key_and_the_tailnet_name(source):
    assert "'mind\\.venv\\Scripts\\pythonw.exe'" in source
    assert "-m leeroy_mind console" in source
    for option in ("--root", "--data-dir", "--key-file", "--allow-host"):
        assert option in source
    assert "'console.key'" in source and "fractal.pug-nessie.ts.net" in source
    assert not re.search(r"[A-Za-z]:\\\\?azerothcore", source)  # paths come from the checkout


def test_it_starts_at_logon_only_while_he_is_logged_on_and_restarts_on_failure(source):
    assert "-AtLogOn" in source
    assert "-LogonType Interactive" in source and "-RunLevel Limited" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-RestartCount" in source and "-RestartInterval" in source
    assert "[TimeSpan]::Zero" in source  # no time limit: it runs for the whole session
    # nothing catches its output: the start line holds the key, and goes nowhere hidden
    assert "Out-File" not in source and "Start-Transcript" not in source


@pytest.mark.skipif(not (shutil.which("powershell") or shutil.which("pwsh")),
                    reason="no PowerShell here")
def test_powershell_parses_it():
    shell = shutil.which("powershell") or shutil.which("pwsh")
    script = str(SCRIPT).replace("'", "''")
    probe = ("$e = $null; $null = [System.Management.Automation.Language.Parser]::ParseFile("
             f"'{script}', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", probe],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "0", out.stdout + out.stderr
