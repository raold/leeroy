"""The Social tab: the guild's drama read from mind.db (socialview), its route and gates, and that
none of it reaches the console's log. Scratch databases and synthetic rows only."""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from conftest import _base_persona_dict
from console.rig import http, make_app, running
from leeroy_mind.console import socialview, views

NOW = 1_793_304_000.0
SECRET = "SECRETTEXT"
HUMAN = 1100


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _cast(c, guid, name):
    c.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) "
              "VALUES(?,?,1,1,0,80,3,0)", (guid, name))


def _rel(c, a, b, stage="none", affinity=0.0, attraction=0.0):
    c.execute("INSERT INTO relationship(a,b,affinity,attraction,romance_stage) VALUES(?,?,?,?,?)",
              (a, b, affinity, attraction, stage))


@pytest.fixture
def db(store):
    c = store.conn
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('human_guid',?)", (str(HUMAN),))
    for g in range(1101, 1108):
        _cast(c, g, f"Member{g}")
    c.execute("INSERT INTO feud(id,a,b,started,ended,cause) VALUES(1,1101,1102,100,NULL,?)",
              (f"{SECRET} loot",))
    c.execute("INSERT INTO feud(id,a,b,started,ended,cause) VALUES(2,1103,1104,50,60,'old')")
    c.execute("INSERT INTO jealousy(holder,target,kind,level,updated) VALUES(1105,1101,'envy',0.6,10)")
    c.execute("INSERT INTO jealousy(holder,target,kind,level,updated) VALUES(1106,1101,'rivalry',0,10)")
    _rel(c, 1101, 1102, "whispers")
    _rel(c, 1102, 1101, "banter")
    _rel(c, 1105, HUMAN, "none", 5, 40)
    _rel(c, 1107, HUMAN, "notices", 2, 10)
    c.execute("INSERT INTO attraction_state(guid,stance,version,together_since,bids_7d,updated) "
              "VALUES(1105,'open',1,NULL,2,0)")
    c.execute("INSERT INTO rumor(id,root,about,canonical,created,last_told,status) "
              "VALUES(1,'r1',1103,?,5,9,'live')", (f"{SECRET} rumour",))
    c.execute("INSERT INTO rumor(id,root,about,canonical,created,last_told,status) "
              "VALUES(2,'r2',1103,'faded one',5,9,'faded')")
    for g, s in ((1101, "spreader"), (1102, "spreader"), (1104, "stifler")):
        c.execute("INSERT INTO rumor_state(rumor,guid,state,updated) VALUES(1,?,?,0)", (g, s))
    for g in (1101, 1102, 1105):
        c.execute("INSERT INTO knows(root,guid,how,learned) VALUES('r1',?,'told',0)", (g,))
    c.execute("INSERT INTO guild_mood(night,value,reasons) VALUES('2026-10-28',0.25,?)",
              (json.dumps(["a good raid"]),))
    c.execute("INSERT INTO pad_mood(guid,p,a,d,updated) VALUES(1101,0.5,0.5,0.5,0)")
    c.execute("INSERT INTO pad_mood(guid,p,a,d,updated) VALUES(1102,-0.5,0.5,0.5,0)")
    c.execute("INSERT INTO pad_mood(guid,p,a,d,updated) VALUES(1103,0.4,0.2,0.1,0)")
    c.commit()
    return c


def test_social_reads_the_drama(db):
    v = socialview.social_view(db, now=NOW)
    assert v["human"] == HUMAN and v["missing"] == []
    assert v["feuds"] == [{"id": 1, "a": 1101, "b": 1102, "started": 100, "cause": f"{SECRET} loot"}]
    assert v["jealousy"] == [{"holder": 1105, "target": 1101, "kind": "envy", "level": 0.6,
                              "updated": 10}]
    assert v["romances"] == [{"a": 1101, "b": 1102, "ab": "whispers", "ba": "banter"}]
    assert [r["id"] for r in v["rumours"]] == [1]
    r = v["rumours"][0]
    assert (r["about"], r["spreaders"], r["stiflers"], r["knowers"]) == (1103, [1101, 1102], [1104], 3)
    assert r["text"] == f"{SECRET} rumour"
    assert v["mood"]["value"] == 0.25 and v["mood"]["reasons"] == ["a good raid"]
    assert sum(v["octants"].values()) == 3 and len(v["octants"]) == 2
    assert v["names"]["1101"] == "Member1101"


def test_toward_you_lists_only_members_with_a_feeling(db):
    you = socialview.social_view(db, now=NOW)["you"]
    assert [y["guid"] for y in you] == [1105, 1107]
    assert you[0]["stance"] == "open" and you[0]["attraction"] == 40 and you[0]["bids_7d"] == 2
    assert you[1]["stance"] is None and you[1]["romance_stage"] == "notices"


def test_romances_go_furthest_stage_first(db):
    _rel(db, 1103, 1104, "invitation")
    _rel(db, 1104, 1103, "none")
    _rel(db, 1106, 1107, "notices")
    ab = [(r["a"], r["b"]) for r in socialview.social_view(db, now=NOW)["romances"]]
    assert ab == [(1103, 1104), (1101, 1102), (1106, 1107)]


def test_jealousy_is_highest_first_and_capped(db):
    for i in range(socialview.JEALOUSY_MAX + 5):
        db.execute("INSERT INTO jealousy(holder,target,kind,level,updated) VALUES(?,?,'envy',?,0)",
                   (2000 + i, 1101, 1 + i))
    j = socialview.social_view(db, now=NOW)["jealousy"]
    assert len(j) == socialview.JEALOUSY_MAX and j[0]["level"] > j[-1]["level"]


def test_names_fall_back_to_a_stored_persona(db):
    db.execute("INSERT INTO persona_core(guid,json,card,card_tokens,version,source,updated) "
               "VALUES(1999,?,'c',1,1,'seed',0)", (json.dumps(_base_persona_dict(1999, "Leaver")),))
    n = views.names(db)
    assert n[1101] == "Member1101" and n[1999] == "Leaver"


def test_view_helpers(db):
    assert "feud" in views.tables(db)
    assert views.rows(db, "SELECT * FROM nonesuch") == []
    assert views.rows(db, "SELECT guid FROM cast_member WHERE guid = ?", (1101,))[0]["guid"] == 1101
    db.execute("INSERT INTO meta(key,value) VALUES('k',?)", (json.dumps({"a": 1}),))
    db.execute("INSERT INTO meta(key,value) VALUES('bad','{')")
    assert views.meta_json(db, "k") == {"a": 1}
    assert views.meta_json(db, "bad") is None and views.meta_json(db, "absent") is None


def test_social_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE cast_member(guid INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/social")
            assert r.status == 200
            v = r.json()
            assert v["missing"] == list(socialview.SOCIAL_TABLES)
            assert v["mood"] is None
            for k in ("you", "feuds", "jealousy", "romances", "rumours"):
                assert v[k] == []
        finally:
            await app.close()
    asyncio.run(go())


def test_social_route_serves_the_view(db, data):
    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            r = await http(app, "GET", "/api/social")
            assert r.status == 200 and r.json()["feuds"][0]["id"] == 1
        finally:
            await app.close()
    asyncio.run(go())


def test_social_needs_the_key_and_the_host(data):
    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/social", key=False)).status == 403
            assert (await http(app, "GET", "/api/social", host="localhost:1")).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/social")
            assert r.status == 503 and r.json()["ok"] is False
            assert r.json()["error"].startswith("mind.db could not be read")
        finally:
            await app.close()
    asyncio.run(go())


def test_nothing_from_the_drama_reaches_the_log(db, data, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/social")).status == 200
        finally:
            await app.close()
    asyncio.run(go())
    assert SECRET not in caplog.text


def test_rows_reads_a_missing_table_as_empty_but_raises_other_errors(db, monkeypatch):
    assert views.rows(db, "SELECT nonesuch FROM cast_member") == []
    with pytest.raises(sqlite3.OperationalError):
        views.rows(db, "SELEC bad")
