"""The Board tab (reading): forum posts, delivered letters and write-ups from mind.db (boardview),
its route and gates. Scratch databases and invented text only."""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import boardview

NOW = 1_793_304_000.0
HUMAN = 1100
SECRET = "SECRETTEXT"


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _post(c, id, ts, author, kind, thread, text):
    c.execute("INSERT INTO realm_post(id,ts,author,author_kind,thread,text) VALUES(?,?,?,?,?,?)",
              (id, ts, author, kind, thread, text))


def _mail(c, id, ts, sender, state, items="[]", subject="Hello", body="Body", kind="letter", taken=None):
    c.execute("INSERT INTO realm_mail(id,ts,sender,kind,subject,body,items,state,taken) "
              "VALUES(?,?,?,?,?,?,?,?,?)", (id, ts, sender, kind, subject, body, items, state, taken))


def _event(c, id, kind, start, result, organiser=1101, place="Dalaran", state="ended"):
    c.execute("INSERT INTO realm_event(id,kind,organiser,start,end,place,result,state) VALUES(?,?,?,?,?,?,?,?)",
              (id, kind, organiser, start, start + 1800, place, json.dumps(result), state))


@pytest.fixture
def db(store):
    c = store.conn
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('human_guid',?)", (str(HUMAN),))
    for g in (1101, 1102):
        c.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) "
                  "VALUES(?,?,1,1,0,80,3,0)", (g, f"Member{g}"))
    c.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES(1,'progression','horde','Rival Hall',0)")
    c.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES(9001,1,'Rivalofficer','officer',0)")
    _post(c, 1, NOW - 500, 1101, "member", "alpha", f"{SECRET} first")
    _post(c, 2, NOW - 400, 9001, "actor", "alpha", "second")
    _post(c, 3, NOW - 100, 1102, "member", "beta", "third")
    _post(c, 4, NOW - 900, 1101, "member", "old", "oldest thread")
    _mail(c, 1, NOW - 50, 1101, "ready", subject="hidden")
    _mail(c, 2, NOW - 40, 1102, "sent", subject="Seen", body=f"{SECRET} letter")
    _mail(c, 3, NOW - 30, 1101, "taken", taken=NOW - 10,
          items=json.dumps([{"from": 1101, "item_id": 10, "count": 2},
                            {"from": 1101, "item_id": 11, "count": 1, "name": "Mailed Name"},
                            {"from": 1101, "item_id": 12, "count": 1}]))
    _event(c, 1, "arena", NOW - 3000, {"winner": "cast", "writeup": f"{SECRET} writeup"})
    _event(c, 2, "duel", NOW - 2000, {"winner": "rival"})
    c.commit()
    return c


def _library(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, ilvl INTEGER, "
              "req_level INTEGER, slot INTEGER)")
    c.execute("INSERT INTO item VALUES(10,'Library Blade',4,1,1,1)")
    c.execute("INSERT INTO item VALUES(11,'Library Other',2,1,1,1)")
    c.commit()
    c.close()


def test_threads_letters_and_writeups(db, tmp_path):
    _library(tmp_path / "library.db")
    v = boardview.board_view(db, now=NOW, data_dir=tmp_path)
    assert v["human"] == HUMAN and v["missing"] == [] and v["names"]["1101"] == "Member1101"
    assert [t["thread"] for t in v["forum"]] == ["beta", "alpha", "old"]
    alpha = v["forum"][1]
    assert alpha["last"] == NOW - 400 and [p["id"] for p in alpha["posts"]] == [1, 2]
    assert alpha["posts"][0]["name"] == "Member1101" and alpha["posts"][0]["guild"] is None
    assert alpha["posts"][1]["author_kind"] == "actor"
    assert alpha["posts"][1]["name"] == "Rivalofficer" and alpha["posts"][1]["guild"] == "Rival Hall"
    assert [m["id"] for m in v["letters"]] == [3, 2]
    taken = v["letters"][0]
    assert taken["state"] == "taken" and taken["taken"] == NOW - 10
    assert taken["items"] == [{"id": 10, "name": "Library Blade", "quality": 4, "count": 2},
                              {"id": 11, "name": "Mailed Name", "quality": 2, "count": 1},
                              {"id": 12, "name": "item 12", "quality": 1, "count": 1}]
    assert [w["id"] for w in v["writeups"]] == [1]
    w = v["writeups"][0]
    assert (w["kind"], w["place"], w["organiser"], w["text"]) == ("arena", "Dalaran", 1101, f"{SECRET} writeup")
    assert w["end"] - w["start"] == 1800


def test_the_board_says_when_the_realm_is_off(db, tmp_path):
    assert boardview.board_view(db, now=NOW, data_dir=tmp_path)["realm_on"] is False
    (tmp_path / "mind.toml").write_text("[realm]\nenabled = false\n")
    assert boardview.board_view(db, now=NOW, data_dir=tmp_path)["realm_on"] is False
    (tmp_path / "mind.toml").write_text("[realm]\nenabled = true\n")
    assert boardview.board_view(db, now=NOW, data_dir=tmp_path)["realm_on"] is True
    (tmp_path / "mind.toml").write_text("[realm\n")
    assert boardview.board_view(db, now=NOW, data_dir=tmp_path)["realm_on"] is None


def test_the_lists_are_capped(db, tmp_path):
    for i in range(100, 160):
        _post(db, i, NOW - i, 1101, "member", f"t{i}", "x")
    assert len(boardview.board_view(db, now=NOW, data_dir=tmp_path)["forum"]) == boardview.THREADS == 50
    assert boardview.LETTERS == 50 and boardview.WRITEUPS == 30


def test_board_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.commit()
    c.close()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/board")
            assert r.status == 200
            v = r.json()
            assert {"realm_post", "realm_mail", "realm_event"} <= set(v["missing"])
            assert v["forum"] == [] and v["letters"] == [] and v["writeups"] == []
        finally:
            await app.close()
    asyncio.run(go())


def test_board_route_serves_the_view(db, data):
    async def go():
        app = make_app(data, mind=running(data), clock=lambda: NOW)
        await app.start()
        try:
            r = await http(app, "GET", "/api/board")
            assert r.status == 200 and r.json()["forum"][0]["thread"] == "beta"
            assert (await http(app, "GET", "/api/board", key=False)).status == 403
            assert (await http(app, "GET", "/api/board", host="localhost:1")).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/board")
            assert r.status == 503 and r.json()["ok"] is False
            assert r.json()["error"].startswith("mind.db could not be read")
        finally:
            await app.close()
    asyncio.run(go())


def test_no_letter_or_post_text_reaches_the_log(db, data, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/board")).status == 200
        finally:
            await app.close()
    asyncio.run(go())
    assert SECRET not in caplog.text
