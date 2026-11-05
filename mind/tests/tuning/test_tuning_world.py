"""T5: world dials stored in mind.db override mind.toml wherever the mind reads it, at once,
and the doctor notes an override that shadows the file."""
import socket
from datetime import datetime, timezone

from conftest import HUMAN, seed_test_cast
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.tuning import changes as C

CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)


def _seed(root, *sets):
    s = Store(open_db(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(HUMAN))
        if sets:
            pic = C.picture(s, {g: load_persona(s, g) for g in s.persona_versions()})
            C.apply_sets(s, [C.Set(*x) for x in sets], pic, source="cli", now=2.0)
    finally:
        s.close()


def _app(root):
    return MindApp(load_config(root / "mind.toml"), root, llm=FakeLLM(lambda m, msg: "ok"),
                   clock=FakeClock(CLOCK))


def test_the_app_starts_with_the_overrides_and_keeps_them_across_a_reload(tmp_path):
    (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 4\n", encoding="utf-8")
    _seed(tmp_path, ("world", "", "chattiness", 9), ("world", "", "jealousy", 1))
    app = _app(tmp_path)
    try:
        assert (app.cfg.talk.chattiness, app.cfg.romance.jealousy) == (9, 1)
        assert app.engine.budget._c == 9 and app.engine.asides.jealousy == 1
        assert app.file_cfg.talk.chattiness == 4
        (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 2\n", encoding="utf-8")
        app._reload_config()
        assert app.cfg.talk.chattiness == 9 and app.file_cfg.talk.chattiness == 2
    finally:
        app.store.close()


def test_without_overrides_the_config_is_the_file_itself(tmp_path):
    (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 4\n", encoding="utf-8")
    _seed(tmp_path)
    app = _app(tmp_path)
    try:
        assert app.cfg == app.file_cfg
    finally:
        app.store.close()


def _doctor(root, capsys):
    from leeroy_mind.__main__ import main
    cfg = load_config(root / "mind.toml")
    llm = FakeLLM(lambda model, messages: "hey all",
                  models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
    code = main(["doctor", "--data-dir", str(root), "--ollama-conf",
                 str(root / "mod_ollama_chat.conf")], make_llm=lambda c: llm)
    return code, capsys.readouterr().out.splitlines()


def _toml(root, port):
    (root / "mind.toml").write_text(f'[bridge]\nport = {port}\n\n[human]\nnickname = "Testy"\n'
                                    'pronouns = "he"\n', encoding="utf-8")
    cfg = load_config(root / "mind.toml")
    (root / "mod_ollama_chat.conf").write_text(
        f'OllamaChat.Model = "{cfg.models.live}"\nOllamaChat.NumCtx = 4096\n', encoding="utf-8")


def test_the_doctor_notes_an_override_that_shadows_mind_toml(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _toml(tmp_path, listener.getsockname()[1])
        _seed(tmp_path, ("world", "", "romance", 6), ("member", "102", "talkative", 8))
        code, out = _doctor(tmp_path, capsys)
    notes = [x for x in out if "tuning" in x]
    assert any(x.startswith("note") and "romance 6 overrides mind.toml's 3" in x for x in notes)
    assert any("1 member value" in x for x in notes)
    assert code == 0


def test_the_doctor_says_nothing_of_tuning_when_none_is_set(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _toml(tmp_path, listener.getsockname()[1])
        _seed(tmp_path)
        _, out = _doctor(tmp_path, capsys)
    assert not [x for x in out if "tuning" in x]


def test_the_doctor_knows_the_tuning_overlays(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _toml(tmp_path, listener.getsockname()[1])
        _seed(tmp_path)
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "tuning_humour_high.txt").write_text("a line\n", encoding="utf-8")
        _, out = _doctor(tmp_path, capsys)
    line = next(x for x in out if "tuning_humour_high" in x)
    assert "no prompt reads it" not in line and "(cap 200)" in line
