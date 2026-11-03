"""`python -m leeroy_voice doctor` and `fetch-models` on the test's own data dir
(LEEROY_VOICE_DATA, set by the guard): config, decision and the pinned models."""
import dataclasses
import hashlib
import urllib.error

import pytest

from fakes import FakeProbes
from leeroy_voice import __main__ as cli
from leeroy_voice import models
from leeroy_voice.__main__ import main

ONNX, VOICES = b"a small stand-in for the model", b"a small stand-in for the voices"


def lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def fake_machine(monkeypatch):
    """The doctor's checks of the machine are test_cli.py's: here they find nothing wrong."""
    monkeypatch.setattr(cli, "PROBES", FakeProbes())


@pytest.fixture
def small_kokoro(monkeypatch, tmp_path):
    """The kokoro pin over two small files, kept by a listening kit in tmp; no network."""
    def refuse(*args, **kw):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(models, "_urlopen", refuse)
    monkeypatch.setattr(models, "_snapshot_download", refuse)
    pin = models.MODEL_PINS["kokoro"]
    monkeypatch.setitem(models.MODEL_PINS, "kokoro", dataclasses.replace(
        pin, sha256={"kokoro-v1.0.onnx": sha(ONNX), "voices-v1.0.bin": sha(VOICES)}))
    lab = tmp_path / "repo" / pin.lab
    lab.mkdir(parents=True)
    (lab / "kokoro-v1.0.onnx").write_bytes(ONNX)
    (lab / "voices-v1.0.bin").write_bytes(VOICES)
    monkeypatch.setattr(models, "lab_dirs", lambda: [tmp_path / "repo"])
    return lab


def test_doctor_on_the_defaults(live_guard, capsys):
    assert not live_guard.data_dir.exists()
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert out[0].startswith("ok   python 3.12")
    assert any(line.startswith("ok   voice.toml: none") for line in out)
    assert "ok   decision: kokoro, swap false, uk mix false" in out
    assert "note kokoro: not fetched (fetch-models --only kokoro)" in out
    assert all(line[:4] in ("ok  ", "note") for line in out)
    assert not live_guard.data_dir.exists()  # the doctor writes nothing


def test_doctor_reads_the_local_files(live_guard, capsys):
    live_guard.data_dir.mkdir()
    (live_guard.data_dir / "voice.toml").write_text('[sound]\nduck_db = 6\n', encoding="utf-8")
    (live_guard.data_dir / "decision.toml").write_text(
        'decided = "2026-11-01"\nprosody_swap = true\nuk_timbre_mix = true\n', encoding="utf-8")
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert any(line.startswith("ok   voice.toml: ") and "valid" in line for line in out)
    assert "ok   decision: kokoro, swap true, uk mix true" in out


def test_doctor_fails_on_bad_files(live_guard, capsys):
    live_guard.data_dir.mkdir()
    (live_guard.data_dir / "voice.toml").write_text('[mind]\nhost = "0.0.0.0"\n', encoding="utf-8")
    (live_guard.data_dir / "decision.toml").write_text('engine = "orpheus"\n', encoding="utf-8")
    assert main(["doctor"]) == 1
    out = lines(capsys)
    assert any(line.startswith("FAIL voice.toml:") and "mind.host" in line for line in out)
    assert any(line.startswith("FAIL decision:") and "engine" in line for line in out)


def test_fetch_then_doctor(live_guard, capsys, small_kokoro):
    """fetch-models --only kokoro copies the kit's files and prints their check; the doctor
    then reports both files' sha256 as pinned."""
    assert main(["fetch-models", "--only", "kokoro"]) == 0
    out = lines(capsys)
    want = [f"ok   kokoro: kokoro-v1.0.onnx sha256 {sha(ONNX)} as pinned",
            f"ok   kokoro: voices-v1.0.bin sha256 {sha(VOICES)} as pinned"]
    assert out[-2:] == want
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert all(line in out for line in want)
    assert "note parakeet: not fetched (fetch-models --only parakeet)" in out


def test_doctor_fails_on_a_wrong_model(live_guard, capsys, small_kokoro):
    assert main(["fetch-models", "--only", "kokoro"]) == 0
    capsys.readouterr()
    (models.model_dir(live_guard.data_dir, "kokoro") / "voices-v1.0.bin").write_bytes(b"changed")
    assert main(["doctor"]) == 1
    out = lines(capsys)
    assert (f"FAIL kokoro: voices-v1.0.bin sha256 {sha(b'changed')}, not the pinned {sha(VOICES)}"
            in out)


def test_fetch_models_refuses_an_unknown_name(live_guard, capsys, small_kokoro):
    assert main(["fetch-models", "--only", "kokoro,orpheus"]) == 1
    assert "orpheus" in capsys.readouterr().err
    assert not live_guard.data_dir.exists()


def test_fetch_models_offline_is_one_line(live_guard, capsys, small_kokoro, monkeypatch):
    """No kit and no network: one line on stderr, no traceback, exit 1."""
    for f in small_kokoro.iterdir():
        f.unlink()

    def unreachable(address, *, timeout):
        raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    monkeypatch.setattr(models, "_urlopen", unreachable)
    assert main(["fetch-models", "--only", "kokoro"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("fetch-models: kokoro: kokoro-v1.0.onnx")
    assert "URLError" in err[0]
