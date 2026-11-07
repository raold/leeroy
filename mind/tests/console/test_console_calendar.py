"""The Calendar tab: the Realm's calendar desk read from mind.db (calendarview), its route and
gates. Scratch databases and synthetic rows only."""
import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import calendarview

NOW = 1_793_304_000.0
DAY = 86400
HUMAN = 1100
SECRET = "SECRETTEXT"


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _event(c, id, kind, at, title, state="posted", event_id=None, creator=1101):
    c.execute("INSERT INTO realm_calendar(id,kind,creator,at,title,ref,updated,event_id,state) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (id, kind, creator, at, title, f"{kind}:{id}", 0, event_id, state))


def _rsvp(c, event, guid, status):
    c.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES(?,?,?,0)", (event, guid, status))


def _update(c, id, state="acked"):
    n = c.execute("SELECT count(*) FROM realm_direct").fetchone()[0]
    c.execute("INSERT INTO realm_direct(command_id,owner,ref,kind,payload_sha256,guild,human,process_id,"
              "expects_job,state,reason,prepared_ms,dispatched_ms,settled_ms) "
              "VALUES(?, 'calendar', ?, 'calendar_update', ?, 1, 1, 'p', 0, ?, ?, 1000, 1000, 1000)",
              (f"{n:032x}", f"update:{id}", "0" * 64, state, None if state == "acked" else "no"))


@pytest.fixture
def db(store):
    c = store.conn
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('human_guid',?)", (str(HUMAN),))
    for g in range(1101, 1106):
        c.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) "
                  "VALUES(?,?,1,1,0,80,3,0)", (g, f"Member{g}"))
    _event(c, 1, "raid", NOW + 2 * DAY, f"{SECRET} night", event_id=501)
    for g in (1101, 1102, 1103):
        _rsvp(c, 501, g, "signed_up")
    _rsvp(c, 501, 1104, "tentative")
    _rsvp(c, 501, HUMAN, "signed_up")
    _event(c, 2, "meeting", NOW + 3 * DAY, "Officer meeting", event_id=502)
    _event(c, 3, "raid", NOW + 2 * DAY, "failed one", state="failed")
    _event(c, 4, "raid", NOW + 20 * DAY, "too far")
    _event(c, 5, "raid", NOW - 2 * DAY, "missed night", event_id=505)
    _update(c, 5)
    _event(c, 6, "raid", NOW - 3 * DAY, "refused update", event_id=506)
    _update(c, 6, "refused")
    _event(c, 7, "raid", NOW - 40 * DAY, "long ago", event_id=507)
    _update(c, 7)
    _event(c, 8, "raid", NOW - 1800, "under way", state="pending")
    c.commit()
    return c


def test_upcoming_events_with_their_signups(db):
    v = calendarview.calendar_view(db, now=NOW)
    assert v["human"] == HUMAN and v["missing"] == []
    assert [e["id"] for e in v["upcoming"]] == [8, 1, 2]
    e = v["upcoming"][1]
    assert (e["kind"], e["state"], e["creator"], e["at"]) == ("raid", "posted", 1101, NOW + 2 * DAY)
    assert e["signups"] == {"signed_up": [1101, 1102, 1103], "tentative": [1104],
                            "declined": [], "other": []}
    assert e["you"] == "signed_up"
    assert v["upcoming"][2]["you"] is None and v["upcoming"][2]["signups"]["signed_up"] == []
    assert v["names"]["1101"] == "Member1101"


def test_an_unknown_status_lands_in_other(db):
    _rsvp(db, 502, 1105, "maybe")
    _rsvp(db, 502, 1104, "declined")
    s = calendarview.calendar_view(db, now=NOW)["upcoming"][2]["signups"]
    assert s["other"] == [1105] and s["declined"] == [1104]


def test_missed_raid_nights_come_from_acked_updates(db):
    v = calendarview.calendar_view(db, now=NOW)
    assert v["missed"] == [{"id": 5, "at": NOW - 2 * DAY, "title": "missed night", "text": "no raid"}]


def test_a_missed_night_he_came_to_says_missed(db):
    db.execute("INSERT INTO session(started,ended) VALUES(?,?)", (NOW - 2 * DAY - 60, NOW - 2 * DAY + 60))
    assert calendarview.calendar_view(db, now=NOW)["missed"][0]["text"] == "missed"


def test_calendar_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE cast_member(guid INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/calendar")
            assert r.status == 200
            v = r.json()
            assert "realm_calendar" in v["missing"] and "realm_rsvp" in v["missing"]
            assert v["upcoming"] == [] and v["missed"] == []
        finally:
            await app.close()
    asyncio.run(go())


def test_calendar_route_serves_the_view(db, data):
    async def go():
        app = make_app(data, mind=running(data), clock=lambda: NOW)
        await app.start()
        try:
            r = await http(app, "GET", "/api/calendar")
            assert r.status == 200 and r.json()["upcoming"][1]["id"] == 1
            assert (await http(app, "GET", "/api/calendar", key=False)).status == 403
            assert (await http(app, "GET", "/api/calendar", host="localhost:1")).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/calendar")
            assert r.status == 503 and r.json()["ok"] is False
            assert r.json()["error"].startswith("mind.db could not be read")
        finally:
            await app.close()
    asyncio.run(go())


def test_nothing_from_the_calendar_reaches_the_log(db, data, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/calendar")).status == 200
        finally:
            await app.close()
    asyncio.run(go())
    assert SECRET not in caplog.text
