"""`doctor` checks the women's notes toward the human against their attitude (fix wave G2;
dress rehearsal 2, judge C): 19 notes still said "no romantic interest" for women whose
attitude is fond, drawn or lust (they predate the owner's attitude mix of 2026-10-30), and her
prompt then read her attitude next to its denial. A lust or drawn woman's note that
denies it fails; a fond woman's is a note. Names and the phrase only, never the note."""
import socket
from datetime import datetime, timezone
from pathlib import Path

from conftest import HUMAN, make_persona, seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.config import load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.notes import cold_notes, denies
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM

ME = HumanIdentity(HUMAN, "Testmage", "Testy", "", True, pronouns="he")
WOMEN = {  # guid: (name, stance, note in her persona, note on her row toward him)
    106: ("Fenna", "lust", "Likes Testy's raid calls, no romantic interest though.", None),
    107: ("Gilda", "drawn", None, "Keeps interactions professional, mostly about loot."),
    108: ("Hestia", "fond", "Friendly in raid. Nothing more than that.", None),
    109: ("Iska", "lust", "Wants Testy and is not subtle about it.", None),
    110: ("Jorun", "none", "Thinks the GM runs a tidy raid; no romantic interest.", None),
    111: ("Kelda", "drawn", "Sends him a winking emote after every boss.", None),
}


def _cast(store: Store) -> None:
    seed_test_cast(store)
    store.upsert_cast([Member(guid=g, name=n, race=5, class_=8, gender=1, level=80, rank=3,
                              online=False, map=0, zone=0, spec_tab=-1)
                       for g, (n, *_) in WOMEN.items()]
                      + [Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0,
                                level=80, rank=0, online=False, map=0, zone=0, spec_tab=-1)],
                      1.0)
    for g, (name, stance, note, row) in WOMEN.items():
        rels = [{"other": "@human", "kind": "friend", "note": note}] if note else []
        p = make_persona(g, name, player_gender="female", stance=stance, relationships=rels)
        save_persona(store, p, source="seed", ts=1.0, race="Undead", class_name="Mage")
        store.put_relationship(Relationship(a=g, b=HUMAN, notes=row if row is not None
                                            else (note or "")))


def _setup(root: Path, port: int, women: bool = True) -> Path:
    (root / "mind.toml").write_text(f'[bridge]\nport = {port}\n\n[human]\nnickname = "Testy"\n'
                                    'pronouns = "he"\n', encoding="utf-8")
    s = Store(open_db(root / "mind.db"), clock=FakeClock(datetime(2026, 10, 29,
                                                                 tzinfo=timezone.utc)))
    try:
        if women:
            _cast(s)
        else:
            seed_test_cast(s)
            s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0,
                                  level=80, rank=0, online=False, map=0, zone=0, spec_tab=-1)],
                          1.0)
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


def test_denies_finds_the_notes_that_say_she_is_not_interested():
    for note in ("Thinks the GM's loot rules are fair; no romantic interest.",
                 "Not romantic about it at all.", "No romantic feelings for him, honestly.",
                 "Nothing more than raid buddies.", "Holds a professional distance in whispers.",
                 "Keeps things professional in raid.", "Their relationship as professional colleagues.",
                 "Polite, with a lot of emotional distance.", "Just friends."):
        assert denies(note), note
    for note in ("Wants Testy, badly.", "Shy around him, but lights up when he logs in.",
                 "Wants nothing more than to get him alone.",
                 "Would like nothing more than a night with him.",
                 "Not interested in anyone else.",
                 "Keeps her distance from the others, never from him.",
                 "More than just friends, and he knows it.",
                 "Professional about raids, flirty about everything else.",
                 "Romantic about him and not shy about it.", "More than a friend, and he knows."):
        assert denies(note) is None, note


def test_cold_notes_reads_her_row_and_her_persona(store):
    _cast(store)
    from leeroy_mind.personas.io import load_persona
    personas = [load_persona(store, g) for g in WOMEN]
    got = {(c.name, c.stance, c.where) for c in cold_notes(store, personas, ME)}
    assert got == {("Fenna", "lust", "row"), ("Fenna", "lust", "persona"),
                   ("Gilda", "drawn", "row"), ("Hestia", "fond", "row"),
                   ("Hestia", "fond", "persona")}


def test_doctor_fails_a_lust_or_drawn_woman_whose_note_denies_it(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1])
        code, out = _doctor(tmp_path, capsys)
    assert code == 1
    fail = [x for x in out if x.startswith("FAIL")]
    assert len(fail) == 1 and "notes toward Testy deny" in fail[0]
    assert "lust Fenna (106): \"no romantic\"" in fail[0]
    assert "drawn Gilda (107): \"keeps interactions professional\"" in fail[0]
    note = [x for x in out if x.startswith("note") and "notes toward" in x]
    assert len(note) == 1 and "fond Hestia (108): \"nothing more\"" in note[0]
    text = "\n".join(out)
    assert all(n not in text for n in ("Iska", "Jorun", "Kelda"))
    assert "raid calls" not in text and "than that" not in text  # never the note


def test_doctor_reads_a_relative_data_dir(tmp_path, capsys, monkeypatch):
    """`doctor --data-dir mind-data` from the folder above it: mind.db's read-only URI needs
    the absolute path (found running this wave's checks on a copy)."""
    root = tmp_path / "mind-data"
    root.mkdir()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(root, listener.getsockname()[1], women=False)
        monkeypatch.chdir(tmp_path)
        cfg = load_config(root / "mind.toml")
        llm = FakeLLM(lambda model, messages: "hey all",
                      models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
        code = main(["doctor", "--data-dir", "mind-data", "--ollama-conf",
                     str(root / "mod_ollama_chat.conf")], make_llm=lambda c: llm)
    out = capsys.readouterr().out
    assert code == 0 and "ok   mind.db schema v" in out, out


def test_doctor_is_ok_when_every_note_fits(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _setup(tmp_path, listener.getsockname()[1], women=False)
        code, out = _doctor(tmp_path, capsys)
    assert code == 0
    assert any(x.startswith("ok   notes toward Testy fit every woman's attitude") for x in out)
