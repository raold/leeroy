"""The selfie doctor lines read mind.toml and relay a faked quick check, never a real worker."""
import pytest

from leeroy_mind import __main__ as m


def toml(tmp_path, body):
    (tmp_path / "mind.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_doctor_notes_disabled_and_fails_without_the_venv(tmp_path):
    assert m._doctor_selfies(toml(tmp_path, "")) == [
        (None, "selfies: disabled ([selfies] enabled = false)")]
    missing = tmp_path / "nowhere" / "python.exe"
    lines = m._doctor_selfies(toml(
        tmp_path, f'[selfies]\nenabled = true\npython = "{missing.as_posix()}"\n'))
    assert lines == [(False, f"selfies: no worker venv at {missing} (see selfie/README.md)")]


def test_doctor_relays_the_quick_check(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    seen = {}

    def quick(py, data):
        seen["args"] = (py, data)
        return [(True, "data dir x"), (None, "model y missing"), (False, "model z bad")]
    monkeypatch.setattr(m, "_quick_doctor", quick)
    lines = m._doctor_selfies(toml(
        tmp_path, f'[selfies]\nenabled = true\npython = "{python.as_posix()}"\n'))
    assert lines == [(True, "selfies: data dir x"), (None, "selfies: model y missing"),
                     (False, "selfies: model z bad")]
    assert seen["args"][0] == python


def test_quick_doctor_parses_the_worker_lines_and_survives_a_timeout(tmp_path, monkeypatch):
    import subprocess
    class Done:
        returncode = 1
        stdout = "ok data dir x\nnote a\nFAIL b\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert m._quick_doctor(tmp_path / "p", tmp_path) == [
        (True, "data dir x"), (None, "a"), (False, "b")]

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 30)
    monkeypatch.setattr(subprocess, "run", boom)
    assert m._quick_doctor(tmp_path / "p", tmp_path)[0][0] is False


def test_doctor_notes_the_adapter_until_snaps_are_on(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    monkeypatch.setattr(m, "_quick_doctor", lambda py, data: [
        (True, "data dir x"), (None, "ipadapter: not fetched (fetch-models --only ipadapter)")])
    head = f'[selfies]\nenabled = true\npython = "{python.as_posix()}"\n'
    note = "selfies: identity adapter not fetched (fetch-models --only ipadapter)"
    assert m._doctor_selfies(toml(tmp_path, head)) == [(True, "selfies: data dir x"), (None, note)]
    assert m._doctor_selfies(toml(tmp_path, head + "snaps = true\n")) == [
        (True, "selfies: data dir x"), (False, note)]
    monkeypatch.setattr(m, "_quick_doctor", lambda py, data: [(True, "ipadapter: 3 files ok")])
    assert m._doctor_selfies(toml(tmp_path, head + "snaps = true\n")) == [
        (True, "selfies: ipadapter: 3 files ok")]


def test_flux_doctor_checks_selected_weights_and_required_identity(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    def quick(py, data, *, backend):
        assert backend == "flux1-dev"
        return [(None, f"{name}: not fetched") for name in
                ("flux1-dev", "nsfw", "flux-ipadapter", "flux-clip")]
    monkeypatch.setattr(m, "_quick_doctor", quick)
    head = f'[selfies]\nenabled=true\nbackend="flux1-dev"\npython="{python.as_posix()}"\n'
    assert [ok for ok, _ in m._doctor_selfies(toml(tmp_path, head))] == [False, False, None, None]
    for setting in ("snaps", "identity_sets"):
        assert all(ok is False for ok, _ in m._doctor_selfies(
            toml(tmp_path, head + f"{setting}=true\n")))


def test_quick_doctor_passes_backend_without_loading_models(tmp_path, monkeypatch):
    import subprocess
    def run(command, **kwargs):
        assert command[-2:] == ["--backend", "flux1-dev"]
        assert "--quick" in command
        return subprocess.CompletedProcess(command, 0, "ok checked\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert m._quick_doctor(tmp_path / "p", tmp_path, backend="flux1-dev") == [(True, "checked")]
