"""Voice diagnostics read temporary data and use fake connection probes, never live services."""
import hashlib
import json
import socket

import pytest

from conftest import seed_test_cast
from leeroy_mind.__main__ import _doctor_voice_service, main
from leeroy_mind.config import load_config
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM


@pytest.fixture
def data(tmp_path, clock, monkeypatch):
    root = tmp_path / "mind-data"
    root.mkdir()
    (root / "mind.toml").write_text("[bridge]\nport = 39001\n", encoding="utf-8")
    store = Store(open_db(root / "mind.db"), clock=clock)
    seed_test_cast(store)
    with store.transaction():
        store.conn.execute("DELETE FROM cast_member WHERE guid IN (104, 105)")
    store.close()
    cfg = load_config(root / "mind.toml")
    (root / "mod_ollama_chat.conf").write_text(
        f'OllamaChat.Model = "{cfg.models.live}"\nOllamaChat.NumCtx = 4096\n', encoding="utf-8")
    voice = tmp_path / "voice-data"
    voice.mkdir()
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(voice))
    return root, voice


class Connection:
    def close(self):
        pass


def run_doctor(root, capsys):
    cfg = load_config(root / "mind.toml")
    llm = FakeLLM(lambda model, messages: "hey all",
                  models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
    code = main(["doctor", "--data-dir", str(root), "--ollama-conf",
                 str(root / "mod_ollama_chat.conf")], make_llm=lambda cfg: llm)
    return code, capsys.readouterr().out


def enable(root, port=39002):
    with (root / "mind.toml").open("a", encoding="utf-8") as f:
        f.write(f"\n[voice]\nenabled = true\nport = {port}\n")


@pytest.mark.parametrize("invalid", ['{"guid": 999}', '{malformed'])
def test_reports_spec_count(data, monkeypatch, capsys, invalid):
    root, _ = data
    folder = root / "voices"
    folder.mkdir()
    for guid in (101, 102, 104):  # 104 still has a persona but is no longer in the cast.
        (folder / f"{guid}.json").write_text(json.dumps({"guid": guid}), encoding="utf-8")
    (folder / "103.json").write_text(invalid, encoding="utf-8")
    (folder / "brief.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: Connection())
    before = hashlib.sha256((root / "mind.db").read_bytes()).digest()
    code, output = run_doctor(root, capsys)
    assert code == 0, output
    assert "voice specs: 2 of 3 cast members" in output
    assert "voice service: disabled" in output
    assert hashlib.sha256((root / "mind.db").read_bytes()).digest() == before


def test_service_down_is_not_a_failure(data, monkeypatch, capsys):
    root, _ = data
    enable(root)
    calls = []

    def connect(address, **kwargs):
        calls.append(address)
        if address[1] == 39002:
            raise ConnectionRefusedError()
        assert address[1] == 39001
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    code, output = run_doctor(root, capsys)
    assert code == 0, output
    assert "voice specs: 0 of 3 cast members" in output
    assert "voice service: not running on 127.0.0.1:39002" in output
    assert ("127.0.0.1", 39002) in calls


def test_service_follows_voice_data_and_reports_endpoint_mismatch(data, monkeypatch):
    root, voice = data
    enable(root)
    (voice / "voice.toml").write_text('[mind]\nhost = "127.0.0.1"\nport = 39003\n',
                                    encoding="utf-8")
    calls = []

    def connect(address, **kwargs):
        calls.append((address, kwargs["timeout"]))
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    lines = _doctor_voice_service(root)
    assert calls == [(("127.0.0.1", 39003), 1.0)]
    assert lines == [(None, "voice endpoint mismatch: mind uses 127.0.0.1:39002; "
                           "service uses 127.0.0.1:39003"),
                     (True, "voice service: listening on 127.0.0.1:39003")]


def test_disabled_voice_never_probes(data, monkeypatch):
    root, _ = data

    def forbidden(*args, **kwargs):
        pytest.fail("disabled voice must not probe any port")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert _doctor_voice_service(root) == [(None, "voice service: disabled")]


@pytest.mark.parametrize("host", ['[]', '{ bad = true }'])
def test_malformed_mind_voice_host_is_a_note(data, monkeypatch, host):
    root, _ = data
    enable(root)
    with (root / "mind.toml").open("a", encoding="utf-8") as f:
        f.write(f"host = {host}\n")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid voice host must not probe any port")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert _doctor_voice_service(root) == [(None, "voice service: not checked (TypeError)")]


@pytest.mark.parametrize("config", ["[mind]\nport = true", '[mind]\nhost = "example.com"',
                                    "[mind]\nport = 0", "[mind]\nport = 70000", "mind = 5",
                                    "[broken"])
def test_invalid_service_configuration_never_probes(data, monkeypatch, config):
    root, voice = data
    enable(root)
    (voice / "voice.toml").write_text(config, encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration must not probe any port")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    lines = _doctor_voice_service(root)
    assert len(lines) == 1 and lines[0][0] is None
    assert "voice service: invalid configuration" in lines[0][1]
    assert "example.com" not in lines[0][1]
