"""Doctor names the cast members with no stored persona; it reads temporary data only."""
import hashlib
import socket

import pytest

from conftest import seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Member
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM


def member(guid, name, *, level=80, rank=4):
    return Member(guid=guid, name=name, race=2, class_=1, gender=0, level=level, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


@pytest.fixture
def root(tmp_path, clock, monkeypatch):
    root = tmp_path / "mind-data"
    root.mkdir()
    (root / "mind.toml").write_text("[bridge]\nport = 39001\n", encoding="utf-8")
    store = Store(open_migrated(root / "mind.db"), clock=clock)
    seed_test_cast(store)
    store.upsert_cast([member(1, "Testmage", rank=0)], 1000.0)
    store.close()
    cfg = load_config(root / "mind.toml")
    (root / "mod_ollama_chat.conf").write_text(
        f'OllamaChat.Model = "{cfg.models.live}"\nOllamaChat.NumCtx = 4096\n', encoding="utf-8")
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(tmp_path / "voice-data"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: Closed())
    return root


class Closed:
    def close(self):
        pass


def doctor(root, capsys):
    cfg = load_config(root / "mind.toml")
    llm = FakeLLM(lambda model, messages: "hey all",
                  models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
    code = main(["doctor", "--data-dir", str(root), "--ollama-conf",
                 str(root / "mod_ollama_chat.conf")], make_llm=lambda cfg: llm)
    return code, capsys.readouterr().out


def add_cast(root, clock, *members):
    store = Store(open_migrated(root / "mind.db"), clock=clock)
    store.upsert_cast(list(members), 2000.0)
    store.close()


def test_a_full_cast_prints_no_line(root, capsys):
    code, out = doctor(root, capsys)
    assert code == 0, out
    assert "personas 5:" in out and "without a persona" not in out


def test_new_cast_members_are_a_note_with_the_remedy(root, clock, capsys):
    """Two joined the cast after generation; one below 80 is not cast; the human needs none."""
    add_cast(root, clock, member(108, "Mirvo"), member(106, "Ladja"),
             member(107, "Orrin", level=67))
    before = hashlib.sha256((root / "mind.db").read_bytes()).digest()
    code, out = doctor(root, capsys)
    assert code == 0, out  # a note, not a failure: the mind runs, they are only silent
    line = next(x for x in out.splitlines() if "without a persona" in x)
    assert line.startswith("note ")
    assert ("cast without a persona 2: Ladja (106), Mirvo (108); the mind never voices "
            "them until `personas generate` (with the mind stopped) writes theirs") in line
    assert hashlib.sha256((root / "mind.db").read_bytes()).digest() == before
