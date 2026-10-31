"""`doctor` reports the prompt overlays in mind-data/prompts against the caps their prompts cut
them to (prompts lens, minor 6): a long overlay loses its end, so the owner sees it before the
guild does. Names and sizes only: an overlay's text is never printed."""
import socket
from datetime import datetime, timezone
from pathlib import Path

from conftest import HUMAN, seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.config import load_config
from leeroy_mind.personas.generate import OVERLAY_TOKEN_CAP
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.romance import OVERLAY_CAP_TOKENS
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM

SECRET = "OVERLAYTEXT"  # a word only the overlays hold: it must never be printed


def _setup(root: Path, port: int) -> Path:
    (root / "mind.toml").write_text(f"[bridge]\nport = {port}\n", encoding="utf-8")
    s = Store(open_db(root / "mind.db"), clock=FakeClock(datetime(2026, 10, 29,
                                                                 tzinfo=timezone.utc)))
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                              level=80, rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
    finally:
        s.close()
    cfg = load_config(root / "mind.toml")
    conf = root / "mod_ollama_chat.conf"
    conf.write_text(f'OllamaChat.Model = "{cfg.models.live}"\nOllamaChat.NumCtx = 4096\n',
                    encoding="utf-8")
    return conf


def _doctor(root: Path, capsys) -> tuple[int, list[str]]:
    cfg = load_config(root / "mind.toml")
    llm = FakeLLM(lambda model, messages: "hey all",
                  models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
    code = main(["doctor", "--data-dir", str(root), "--ollama-conf",
                 str(root / "mod_ollama_chat.conf")], make_llm=lambda c: llm)
    return code, capsys.readouterr().out.splitlines()


def _words(tokens: int) -> str:
    """About `tokens` estimated tokens (4 characters each) of overlay text."""
    return " ".join([SECRET[:3].lower()] * tokens)


def test_doctor_shows_each_overlay_against_its_cap(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "romance_explicit.txt").write_text(
            f"{SECRET} " + _words(OVERLAY_CAP_TOKENS + 40), encoding="utf-8")
        (prompts / "romance_flirty.txt").write_text(f"{SECRET} " + _words(50),
                                                    encoding="utf-8-sig")
        (prompts / "persona_rules.txt").write_text(f"{SECRET} " + _words(300), encoding="utf-8")
        (prompts / "romance_warm.txt").write_text("  \n", encoding="utf-8")
        (prompts / "romance-explicit.txt").write_text(f"{SECRET} typo", encoding="utf-8")
        (prompts / "notes.md").write_text(f"{SECRET} not an overlay", encoding="utf-8")
        code, out = _doctor(tmp_path, capsys)
    assert code == 0, out  # an overlay is a note, never a FAIL
    lines = [line for line in out if "prompt overlay" in line]
    assert all(line.startswith("ok") for line in lines) and SECRET not in "\n".join(out)
    by_name = {line.split("prompt overlay ", 1)[1].split(":", 1)[0]: line for line in lines}
    assert set(by_name) == {"persona_rules", "romance-explicit.txt", "romance_explicit",
                            "romance_flirty", "romance_warm"}
    explicit = by_name["romance_explicit"]
    assert f"over its cap of {OVERLAY_CAP_TOKENS}" in explicit and "the end is cut" in explicit
    assert f"(cap {OVERLAY_CAP_TOKENS})" in by_name["romance_flirty"]
    assert f"(cap {OVERLAY_TOKEN_CAP})" in by_name["persona_rules"]
    assert "blank" in by_name["romance_warm"]
    assert "no prompt reads it" in by_name["romance-explicit.txt"]
    assert "romance_explicit.txt" in by_name["romance-explicit.txt"]  # what it should be


def test_doctor_says_when_an_overlay_is_not_utf8(tmp_path, capsys):
    """Part F review, Minor 7: the owner sees it in doctor, not only in the log."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "romance_flirty.txt").write_bytes(f"{SECRET} keep it light".encode("utf-16"))
        quote = chr(0x201C)
        (prompts / "romance_suggestive.txt").write_bytes(
            f"{SECRET} {quote}soft{quote}".encode("cp1252"))
        (prompts / "romance_warm.txt").write_text(f"{SECRET} kind", encoding="utf-8")
        code, out = _doctor(tmp_path, capsys)
    assert code == 0, out
    lines = {line.split("prompt overlay ", 1)[1].split(":", 1)[0]: line
             for line in out if "prompt overlay" in line}
    assert "not UTF-8: read as UTF-16" in lines["romance_flirty"]
    assert "(cap 200)" in lines["romance_flirty"]  # its real size, not the garbled one
    assert "not UTF-8: 2 characters could not be read" in lines["romance_suggestive"]
    assert "UTF" not in lines["romance_warm"] and SECRET not in "\n".join(out)


def test_an_overlay_that_cannot_be_read_fails(tmp_path, capsys, monkeypatch):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        (tmp_path / "prompts").mkdir()
        locked = tmp_path / "prompts" / "romance_flirty.txt"
        locked.write_text("x", encoding="utf-8")
        for name in ("read_bytes", "read_text"):
            real = getattr(Path, name)

            def read(self, *a, _real=real, **kw):  # an editor holding the file mid-save
                if self == locked:
                    raise PermissionError(13, "Permission denied")
                return _real(self, *a, **kw)
            monkeypatch.setattr(Path, name, read)
        try:
            code, out = _doctor(tmp_path, capsys)
        finally:
            monkeypatch.undo()
    fails = [line for line in out if line.startswith("FAIL")]
    assert code == 1 and len(fails) == 1 and "romance_flirty: cannot be read" in fails[0]


def test_doctor_knows_the_whisper_overlay(tmp_path, capsys):
    """Fix wave G1 (item 5): the explicit band in a whisper reads its own overlay, capped like
    the others; the doctor reports it like them."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_text(
            f"{SECRET} " + _words(OVERLAY_CAP_TOKENS + 10), encoding="utf-8")
        code, out = _doctor(tmp_path, capsys)
    line, = [x for x in out if "prompt overlay" in x]
    assert code == 0 and line.startswith("ok") and SECRET not in "\n".join(out)
    assert "romance_explicit_whisper:" in line and "no prompt reads it" not in line
    assert f"over its cap of {OVERLAY_CAP_TOKENS}" in line


def test_no_overlays_no_lines(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        (tmp_path / "prompts").mkdir()
        code, out = _doctor(tmp_path, capsys)
    assert code == 0 and not [line for line in out if "prompt overlay" in line]
