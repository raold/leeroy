"""The Realm tab: the rival guilds read from mind.db (realmview), its route and gates. Scratch
databases and synthetic rows only, invented names."""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import realmview
from leeroy_mind.realm import race

NOW = 1_793_304_000.0
DAY = 86400
OWN = 7
SECRET = "SECRETLINE"
BOSS = (631, 0, 1)
assert BOSS in race.race_keys()


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _guild(c, id, role, name, faction="horde", status="active"):
    c.execute("INSERT INTO realm_guild(id,role,faction,name,added,status) VALUES(?,?,?,?,0,?)",
              (id, role, faction, name, status))


def _actor(c, guid, guild, name, role, place=""):
    c.execute("INSERT INTO realm_actor(guid,guild,name,role,added,place) VALUES(?,?,?,?,0,?)",
              (guid, guild, name, role, place))


def _progress(c, guild, key, state, pct=100, killed_at=None, source="rule"):
    c.execute("INSERT INTO realm_progress(guild,map,boss_id,difficulty,state,best_pct,killed_at,source,"
              "updated) VALUES(?,?,?,?,?,?,?,?,0)", (guild, *key, state, pct, killed_at, source))


def _line(c, id, ts, guid, guild, text, kind="rival", state="sent"):
    reason = "no" if state == "refused" else None
    c.execute("INSERT INTO realm_line(id,ts,guid,guild,channel,text,kind,state,command_id,reason) "
              "VALUES(?,?,?,?,'Trade',?,?,?,?,?)",
              (id, ts, guid, guild, text, kind, state, f"{id:032x}", reason))


def _guild_kill(c, key, end):
    c.execute("INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,start_ms,end_ms,"
              "members,assisted,boss_entry,upd_mean_ms,kind,complete,scoreable,received) "
              "VALUES('p1','c',?,7,?,?,25,'killed',?,?,25,0,1,1,'his',1,1,?)",
              (key[0], key[1], key[2], int((end - 300) * 1000), int(end * 1000), end))


@pytest.fixture
def db(store):
    c = store.conn
    c.execute("INSERT INTO meta(key,value) VALUES('realm:guild_name',?)",
              (json.dumps({"guild": OWN, "name": "Our Guild"}),))
    _guild(c, OWN, "progression", "Our Guild")
    _guild(c, 1, "progression", "Rival One")
    _guild(c, 2, "drama", "Rival Two")
    _guild(c, 3, "gankers", "Rival Three", "alliance")
    _guild(c, 4, "drama", "Retired Rivals", status="retired")
    _actor(c, 201, 1, "Offa", "officer", "Orgrimmar")
    _actor(c, 202, 1, "Ganka", "ganker")
    _actor(c, 203, 1, "Gankb", "ganker")
    _actor(c, 204, 1, "Applya", "applicant")
    _actor(c, 301, 3, "Offb", "officer")
    for guid, role, ilvl in ((1, "tank", 240), (2, "healer", 250), (3, "melee", 251)):
        c.execute("INSERT INTO realm_raider(guild,guid,class,role,avg_ilvl,checked) VALUES(1,?,1,?,?,0)",
                  (guid, role, ilvl))
    _guild_kill(c, BOSS, NOW - 10 * DAY)
    _progress(c, 1, BOSS, "killed", 0, NOW - 7 * DAY)
    _progress(c, 2, BOSS, "progressing", 40)
    _progress(c, 1, (631, 1, 1), "progressing", 70)
    _progress(c, OWN, BOSS, "killed", 0, NOW)
    c.commit()
    return c


def test_the_rivals_their_people_and_progress(db):
    v = realmview.realm_view(db, now=NOW)
    assert v["missing"] == [] and v["own"] == {"guild": OWN, "name": "Our Guild"}
    assert [r["id"] for r in v["rivals"]] == [1, 2, 3]
    one = v["rivals"][0]
    assert (one["name"], one["role"], one["faction"]) == ("Rival One", "progression", "horde")
    assert one["officers"] == [{"guid": 201, "name": "Offa", "place": "Orgrimmar"}]
    assert (one["gankers"], one["applicants"], one["kills"]) == (2, 1, 1)
    assert one["raiders"] == {"tank": 1, "healer": 1, "melee": 1, "ranged": 0, "ilvl": 247}
    assert v["rivals"][2]["faction"] == "alliance"
    assert v["rivals"][1]["raiders"]["ilvl"] == 0 and v["rivals"][1]["officers"] == []


def test_the_race_puts_the_guilds_kill_first(db):
    v = realmview.realm_view(db, now=NOW)
    first = v["race"][0]
    assert (first["map"], first["boss"], first["difficulty"]) == BOSS
    assert first["guild_at"] == NOW - 10 * DAY and first["mode"] == race.mode_label(631, 1)
    assert first["boss_name"]
    assert first["rivals"]["1"] == {"state": "killed", "best_pct": 0, "killed_at": NOW - 7 * DAY,
                                    "source": "rule"}
    assert first["rivals"]["2"]["state"] == "progressing" and str(OWN) not in first["rivals"]
    second = v["race"][1]
    assert second["boss"] == 1 and second["guild_at"] is None
    assert len(v["race"]) == 2


def test_only_sent_rival_lines_are_shown(db):
    _line(db, 1, NOW - 100, 201, 1, "older")
    _line(db, 2, NOW - 50, 201, 1, "newer")
    _line(db, 3, NOW - 40, 201, 1, "member said", kind="member")
    _line(db, 4, NOW - 30, 201, 1, "was refused", state="refused")
    v = realmview.realm_view(db, now=NOW)
    assert [(x["text"], x["name"], x["guild"], x["channel"]) for x in v["lines"]] == [
        ("newer", "Offa", "Rival One", "Trade"), ("older", "Offa", "Rival One", "Trade")]


def test_the_lines_are_capped(db):
    for i in range(60):
        _line(db, 10 + i, NOW - i, 201, 1, f"line {i}")
    v = realmview.realm_view(db, now=NOW)
    assert len(v["lines"]) == realmview.LINES_MAX == 50 and v["lines"][0]["text"] == "line 0"


def test_canon_and_the_weeks_poach(db):
    for a, g, ts, st in ((4576, 1100, 5, "granted"), (2186, 1101, 9, "granted"), (3117, 1100, 1, "planned")):
        db.execute("INSERT INTO realm_canon(achievement,guid,reason,ts,state) VALUES(?,?,'',?,?)",
                   (a, g, ts, st))
    db.execute("INSERT INTO meta(key,value) VALUES('realm:poach:week',?)", (json.dumps(
        {"period": "2026-W45", "guild": 1, "officer": 201, "member": 1101, "ts": 5.0, "text": SECRET,
         "grievance": ["x"]}),))
    v = realmview.realm_view(db, now=NOW)
    assert [(c["achievement"], c["name"], c["guid"]) for c in v["canon"]] == [
        (2186, "achievement 2186", 1101), (4576, "Fall of the Lich King", 1100)]
    assert v["poach"] == {"period": "2026-W45", "guild": 1, "officer": 201, "member": 1101, "ts": 5.0,
                          "officer_name": "Offa", "guild_name": "Rival One"}
    assert SECRET not in json.dumps(v)


def test_no_poach_marker_is_none(db):
    assert realmview.realm_view(db, now=NOW)["poach"] is None


def test_realm_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE cast_member(guid INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/realm")
            assert r.status == 200
            v = r.json()
            assert "realm_guild" in v["missing"] and "realm_progress" in v["missing"]
            assert v["rivals"] == [] and v["race"] == [] and v["lines"] == [] and v["canon"] == []
            assert v["own"] is None and v["poach"] is None
        finally:
            await app.close()
    asyncio.run(go())


def test_realm_route_serves_the_view(db, data):
    async def go():
        app = make_app(data, mind=running(data), clock=lambda: NOW)
        await app.start()
        try:
            r = await http(app, "GET", "/api/realm")
            assert r.status == 200 and [x["id"] for x in r.json()["rivals"]] == [1, 2, 3]
            assert (await http(app, "GET", "/api/realm", key=False)).status == 403
            assert (await http(app, "GET", "/api/realm", host="localhost:1")).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/realm")
            assert r.status == 503 and r.json()["ok"] is False
            assert r.json()["error"].startswith("mind.db could not be read")
        finally:
            await app.close()
    asyncio.run(go())


def test_nothing_from_the_realm_reaches_the_log(db, data, caplog):
    _line(db, 1, NOW - 100, 201, 1, SECRET)
    db.commit()
    caplog.set_level(logging.DEBUG)

    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/realm")).status == 200
        finally:
            await app.close()
    asyncio.run(go())
    assert SECRET not in caplog.text
