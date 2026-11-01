"""The voice brief (Task 19): what the casting reads of the cast, `voices brief` (read-only, N1),
and the watch that sends a spec file's change to the service. The invented cast only."""
import hashlib
import json
import os
import sqlite3

import pytest

from conftest import make_persona, seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import latest_version, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.voice import wire
from leeroy_mind.voice.brief import BRIEF_KEYS, SpecWatch, write_brief
from leeroy_mind.wow import CLASSES, RACES
from voice.conftest import write_spec

TEN = {"guid", "name", "gender", "accent", "age", "descriptors", "speed", "guild_role",
       "raid_role", "core"}


def put(store, guid, name, race=1, cls=1, **over):
    save_persona(store, make_persona(guid, name, **over), source="seed", ts=1002.0,
                 race=RACES[race], class_name=CLASSES[cls])


def seeded(store):
    """101-105 as P1b seeds them (men, UK), then Belra a British woman and Dunna an American
    one; Beklotu has a persona and no cast row (she left)."""
    seed_test_cast(store)
    put(store, 102, "Belra", 4, 11, player_gender="female", accent="en-gb", region="UK",
        voice={"base": ["quick", "low"], "speed": 1.1}, guild_role="officer", raid_role="healer")
    put(store, 104, "Dunna", 3, 2, player_gender="female", accent="en-us", region="US",
        core=False)
    put(store, 110, "Beklotu", 4, 11, player_gender="female")
    return store


def test_brief_has_no_private_text(store, tmp_path):
    seeded(store)
    path = write_brief(store, tmp_path / "out")
    assert path == tmp_path / "out" / "brief.json"
    text = path.read_text(encoding="utf-8")
    entries = json.loads(text)
    assert set(BRIEF_KEYS) == TEN
    assert [e["guid"] for e in entries] == [101, 102, 103, 104, 105]  # Beklotu left
    assert all(set(e) == TEN for e in entries)
    belra = next(e for e in entries if e["guid"] == 102)
    assert belra == {"guid": 102, "name": "Belra", "gender": "female", "accent": "en-gb",
                     "age": 34, "descriptors": ["quick", "low"], "speed": 1.1,
                     "guild_role": "officer", "raid_role": "healer", "core": True}
    for private in ("Sam Carter", "Jo Mercer", "Chris Dale", "Pat Reeve", "Alex Finch",
                    "once lost a bet", "ok pulling in 5", "warehouse", "stance", "Beklotu",
                    "secret", "romance"):
        assert private not in text


def test_brief_is_only_the_cast_with_a_persona(store, tmp_path):
    seeded(store)
    store.upsert_cast([Member(guid=120, name="Nopers", race=1, class_=1, gender=0, level=80,
                              rank=3, online=False, map=0, zone=0, spec_tab=-1)], 1003.0)
    entries = json.loads(write_brief(store, tmp_path).read_text(encoding="utf-8"))
    assert 120 not in {e["guid"] for e in entries}


def build_db(path, clock):
    s = Store(open_db(path), clock=clock)
    seeded(s)
    s.close()


def test_brief_cli(tmp_path, clock, capsys):
    db = tmp_path / "mind.db"
    build_db(db, clock)
    # two women in the cast: Belra (en-gb) and Dunna (en-us); Beklotu is not in it
    assert main(["voices", "brief", "--db", str(db), "--out", str(tmp_path / "voices")]) == 0
    assert (tmp_path / "voices" / "brief.json").is_file()
    out = capsys.readouterr().out
    assert "5 members" in out
    assert "British women: 1 of 2 (50%)" in out
    assert "Belra" not in out


def test_brief_out_defaults_to_the_data_dir(tmp_path, clock):
    root = tmp_path / "data"
    root.mkdir()
    build_db(root / "mind.db", clock)
    assert main(["voices", "brief", "--data-dir", str(root)]) == 0
    assert (root / "voices" / "brief.json").is_file()


def test_brief_never_migrates(tmp_path, clock, capsys):
    db = tmp_path / "mind.db"
    build_db(db, clock)
    conn = sqlite3.connect(db)
    for table in ("voice_heard", "voice_line", "voice_presence"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute(f"PRAGMA user_version = {latest_version() - 1}")
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), os.listdir(tmp_path))
    assert main(["voices", "brief", "--db", str(db), "--out", str(tmp_path / "v")]) == 0
    assert (tmp_path / "v" / "brief.json").is_file()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before[0]
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() - 1
    conn.close()
    assert not (tmp_path / "mind.lock").exists()
    assert sorted(set(os.listdir(tmp_path)) - set(before[1])) == ["v"]


def test_brief_without_a_database_is_one_line(tmp_path, capsys):
    assert main(["voices", "brief", "--db", str(tmp_path / "nope.db"),
                 "--out", str(tmp_path / "v")]) == 1
    assert "nope.db" in capsys.readouterr().err
    assert not (tmp_path / "nope.db").exists() and not (tmp_path / "v").exists()


# -- the watch ----------------------------------------------------------------------------------

def sent_voices(vapp):
    return [m for m in vapp.sent if isinstance(m, wire.VoiceDef)]


def ticks(vapp):
    return next(t for t in vapp.ext.tickers if isinstance(t, SpecWatch))


def settle(vapp, seconds):
    from conftest import run
    run(vapp.clock.advance(seconds, step=seconds))


def test_spec_change_resent(vapp):
    watch = ticks(vapp)
    watch()  # the baseline
    assert not sent_voices(vapp)
    path = vapp.app.data_dir / "voices" / "103.json"
    old = path.stat().st_mtime_ns
    write_spec(vapp.app.data_dir, 103, "Coros", speed=1.1)
    os.utime(path, ns=(old + 5_000_000_000, old + 5_000_000_000))
    settle(vapp, 29)
    watch()
    assert not sent_voices(vapp)  # not due yet
    settle(vapp, 2)
    watch()
    [msg] = sent_voices(vapp)
    assert msg.guid == 103 and msg.spec["speed"] == 1.1
    assert vapp.ext.specs[103]["speed"] == 1.1
    settle(vapp, 31)
    watch()
    assert len(sent_voices(vapp)) == 1  # nothing changed since: nothing sent


def test_new_spec_file_picked_up(vapp):
    watch = ticks(vapp)
    watch()
    assert 106 not in vapp.ext.specs
    write_spec(vapp.app.data_dir, 106, "Fenn")
    settle(vapp, 31)
    watch()
    [msg] = sent_voices(vapp)
    assert msg.guid == 106 and msg.name == "Fenn"
    assert 106 in vapp.ext.specs  # begin_session's roll reads these: a member for tomorrow's comms


def test_watch_survives_a_broken_folder(vapp, monkeypatch):
    watch = ticks(vapp)
    watch()
    from leeroy_mind.voice import brief

    real, broken = brief.load_specs, [True]

    def flaky(*a, **k):
        if broken[0]:
            raise OSError("disk gone")
        return real(*a, **k)
    monkeypatch.setattr(brief, "load_specs", flaky)
    write_spec(vapp.app.data_dir, 107, "Gale")
    settle(vapp, 31)
    watch()  # does not raise
    assert not sent_voices(vapp)
    broken[0] = False
    settle(vapp, 31)
    watch()
    assert [m.guid for m in sent_voices(vapp)] == [107]


def test_watch_is_a_ticker_of_an_enabled_voice(vapp):
    assert any(isinstance(t, SpecWatch) for t in vapp.ext.tickers)
