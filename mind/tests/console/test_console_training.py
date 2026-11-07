"""The Training tab: the training ground read from mind.db (trainview), its route and gates, and
that none of it reaches the console's log. Scratch databases and synthetic rows only."""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import trainview

from training.test_tg_report import START, add as _add

DAY = 86400
NOW = START + 40 * DAY
SECRET = "SECRETTEXT"


class _Fake:
    def __init__(self, conn):
        self.conn = conn


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _gate(c, phase, ms):
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tg_gate',?)",
              (json.dumps({"phase": phase, "raids": 2, "verdict_ms": ms}),))


@pytest.fixture
def db(store):
    c = store.conn
    for i in range(6):
        _add(_Fake(c), i, 2)
    c.execute("INSERT INTO tg_card(id,context,version,status,origin,directives,call,reason,created,changed)"
              " VALUES(1,'631:0:1',1,'champion','stock','[]','stack',?,10,20)", (f"{SECRET} reason",))
    c.execute("INSERT INTO tg_card(id,context,version,status,origin,directives,call,reason,created,changed)"
              " VALUES(2,'631:0:1',2,'candidate','coach','[]','spread','r2',11,21)")
    c.execute("INSERT INTO tg_trial(id,scope,lever,metric,status,opened) VALUES(1,'631:0:1','x','raid','open',5)")
    c.execute("INSERT INTO tg_trial(id,scope,lever,metric,status,opened,closed) VALUES(2,'631:0:1','y','raid','promoted',3,4)")
    c.execute("INSERT INTO tg_arm(id,trial,card,champion,status) VALUES(1,1,1,1,'live')")
    c.execute("INSERT INTO tg_arm(id,trial,card,champion,status) VALUES(2,1,2,0,'live')")
    _gate(c, "passed", 0.7)
    c.commit()
    return c


def test_days_until_tactics_may_adapt(db):
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tg_first_pull',?)", (str(NOW - 10 * DAY),))
    v = trainview.training_view(db, now=NOW)
    assert v["setup"] and v["days_left"] == 18 and v["days_observed"] == pytest.approx(10)
    assert v["since"] == NOW - 10 * DAY and v["four_weeks"] is False
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tg_first_pull',?)", (str(NOW - 39 * DAY),))
    assert trainview.training_view(db, now=NOW)["days_left"] == 0
    db.execute("DELETE FROM meta WHERE key='tg_first_pull'")
    v = trainview.training_view(db, now=NOW)
    assert v["days_left"] is None and v["days_observed"] is None and v["since"] is None


def test_the_gate_and_cards_per_boss(db):
    v = trainview.training_view(db, now=NOW)
    assert v["gate"] == {"phase": "passed", "raids": 2, "verdict_ms": 0.7, "passed": True}
    _gate(db, "passed", 1.2)  # the gate's own limit is configurable: its phase is the verdict
    assert trainview.training_view(db, now=NOW)["gate"]["passed"] is True
    _gate(db, "failed", 0.9)
    assert trainview.training_view(db, now=NOW)["gate"]["passed"] is False
    _gate(db, "ab", 0.1)
    assert trainview.training_view(db, now=NOW)["gate"]["passed"] is False
    group = v["cards"]["631:0:1"]
    assert group["boss"] == "Lord Marrowgar" and group["mode"] == "25-man"
    assert [c["version"] for c in group["cards"]] == [1, 2]
    assert set(group["cards"][0]) == {"id", "version", "status", "origin", "call", "reason", "created", "changed"}
    assert group["cards"][0]["status"] == "champion" and group["cards"][0]["call"] == "stack"


def test_trials_pulls_and_checklist(db):
    v = trainview.training_view(db, now=NOW)
    assert v["pulls"]["total"] == 6 and sum(v["pulls"]["by_kind"].values()) == 6
    assert isinstance(v["pulls"]["week_by_kind"], dict)
    assert [t["id"] for t in v["trials"]] == [1, 2]
    t = v["trials"][0]
    assert t["scope"] == "631:0:1" and t["status"] == "open" and t["arms"] == 2 and t["observations"] == 0
    assert (t["boss"], t["mode"]) == ("Lord Marrowgar", "25-man")  # named as the cards are
    assert v["trials_open"] == 1
    assert len(v["checklist"]) == 6 and v["checklist"][3]["done"] is True and v["checklist"][0]["done"] is False
    assert v["evidence"]["schema"] == "available" and v["week"].startswith("2026-W")


def test_trials_are_capped(db):
    for i in range(10, 10 + trainview.TRIALS + 5):
        db.execute("INSERT INTO tg_trial(id,scope,lever,metric,status,opened) VALUES(?,'631:0:1','z','raid','open',?)", (i, i))
    assert len(trainview.training_view(db, now=NOW)["trials"]) == trainview.TRIALS


def test_training_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.commit()
    c.close()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/training")
            assert r.status == 200
            v = r.json()
            assert v["setup"] is False and v["why"] == "this mind.db has no training ground tables."
        finally:
            await app.close()
    asyncio.run(go())


def test_training_route_serves_the_view(db, data):
    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            r = await http(app, "GET", "/api/training")
            assert r.status == 200 and r.json()["setup"] is True
            assert r.json()["gate"]["passed"] is True
        finally:
            await app.close()
    asyncio.run(go())


def test_training_needs_the_key_and_the_host(data):
    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/training", key=False)).status == 403
            assert (await http(app, "GET", "/api/training", host="localhost:1")).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/training")
            assert r.status == 503 and r.json()["ok"] is False
            assert r.json()["error"].startswith("mind.db could not be read")
        finally:
            await app.close()
    asyncio.run(go())


def test_nothing_from_training_reaches_the_log(db, data, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            assert (await http(app, "GET", "/api/training")).status == 200
        finally:
            await app.close()
    asyncio.run(go())
    assert SECRET not in caplog.text
