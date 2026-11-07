"""The Bank tab: the mind's bank snapshot (control op `bank`) merged with names, item names from
library.db and the realm_bank ledger from mind.db. The mind, its link and the probes are fakes;
no MySQL, no live data. Synthetic rows only."""
import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import endpoint
from leeroy_mind.console import bankview, mindlink
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError
from console.rig import http, make_app, running

SNAPSHOT = {"ok": True, "read_at": 1793304000.5, "guild": 21, "money": 1234567,
            "tabs": [{"tab": 0, "name": "Potions", "slots": [[0, 2589, 20], [14, 4306, 5]]},
                     {"tab": 1, "name": "", "slots": [[97, 777777, 1]]}],
            "log": [[1793304000, 4, 1101, 50000], [1793303000, 5, 1102, 10000]]}
SECRET = "SECRETTEXT"


class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        if self.error:
            raise LinkError(self.error)
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args))
        self.timeouts.append(timeout)
        if self.error:
            raise LinkError(self.error)
        return self.reply


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def db(store, data):
    c = store.conn
    for g in (1101, 1102):
        c.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) "
                  "VALUES(?,?,1,1,0,80,3,0)", (g, f"Member{g}"))
    for i in range(bankview.LEDGER_MAX + 5):
        c.execute("INSERT INTO realm_bank(ts,dir,reason,guid,item_id,count,money) "
                  "VALUES(?,?,?,?,?,?,?)", (100 + i, "in" if i % 2 else "out", f"{SECRET} stock",
                                            1101, 2589, 3, 0))
    c.commit()
    lib = sqlite3.connect(data / "library.db")
    lib.execute("CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, ilvl INTEGER, "
                "req_level INTEGER, slot TEXT)")
    lib.execute("INSERT INTO item VALUES(2589,'Linen Cloth',1,5,1,'')")
    lib.execute("INSERT INTO item VALUES(4306,'Silk Cloth',2,5,1,'')")
    lib.execute("INSERT INTO item VALUES(555,'Unrelated',5,5,1,'')")
    lib.commit()
    lib.close()
    return c


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_the_bank_tab_merges_the_minds_snapshot_with_names_and_the_ledger(db, data):
    link = FakeLink(SNAPSHOT)

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "GET", "/api/bank")
            assert r.status == 200
            return r.json()
    v = asyncio.run(go())
    assert link.calls == [((101, 100), "bank", None)]
    assert mindlink.BANK_S == endpoint.BANK_READ_S + 5.0 and link.timeouts == [mindlink.BANK_S]
    assert v["why"] is None and v["missing"] == []
    assert v["bank"]["money"] == 1234567 and v["bank"]["tabs"][0]["name"] == "Potions"
    assert v["bank"]["read_at"] == 1793304000.5 and v["bank"]["guild"] == 21
    assert v["names"]["1101"] == "Member1101"
    assert v["items"]["2589"] == {"name": "Linen Cloth", "quality": 1}
    assert v["items"]["4306"]["name"] == "Silk Cloth" and "555" not in v["items"]
    assert "777777" not in v["items"]  # not in the library: the page says "Item #777777"
    assert len(v["ledger"]) == bankview.LEDGER_MAX
    assert v["ledger"][0]["ts"] > v["ledger"][-1]["ts"]
    assert set(v["ledger"][0]) == {"ts", "dir", "reason", "guid", "item_id", "count", "money"}


def test_the_bank_tab_says_why_with_the_mind_down(db, data):
    link = FakeLink(SNAPSHOT)

    async def go():
        async with served(make_app(data, link=link)) as app:
            return (await http(app, "GET", "/api/bank")).json()
    v = asyncio.run(go())
    assert v["bank"] is None and v["why"].startswith("The bank is read through the mind, which is ")
    assert link.calls == [] and len(v["ledger"]) == bankview.LEDGER_MAX  # the ledger shows anyway


def test_an_older_mind_is_named(db, data):
    link = FakeLink({"ok": False, "error": "bad request: op is one of status, stop, raid, tune"})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            return (await http(app, "GET", "/api/bank")).json()
    v = asyncio.run(go())
    assert v["bank"] is None
    assert v["why"] == "The running mind predates the Bank tab: restart it after the update."


def test_the_minds_words_and_a_broken_link_come_through(db, data):
    async def ask(link):
        async with served(make_app(data, mind=running(data), link=link)) as app:
            return (await http(app, "GET", "/api/bank")).json()
    v = asyncio.run(ask(FakeLink({"ok": False, "error": "The game database didn't answer (QueryError)."})))
    assert v["why"] == "The game database didn't answer (QueryError)." and v["bank"] is None
    v = asyncio.run(ask(FakeLink(error="the mind is not listening where control.json says")))
    assert v["why"] == "The mind is not listening where control.json says." and v["bank"] is None
    v = asyncio.run(ask(None))
    assert v["bank"] is None and "no link" in v["why"]


def test_a_malformed_snapshot_is_not_shown(db, data):
    bad = dict(SNAPSHOT, tabs="nope")

    async def go():
        async with served(make_app(data, mind=running(data), link=FakeLink(bad))) as app:
            return (await http(app, "GET", "/api/bank")).json()
    v = asyncio.run(go())
    assert v["bank"] is None and v["why"]


def test_no_control_still_reads_the_bank(db, data):
    link = FakeLink(SNAPSHOT)
    off = Controls(enabled=False, mind=running(data), voice=SimpleNamespace(act=lambda a: None))

    async def go():
        async with served(make_app(data, mind=running(data), controls=off, link=link)) as app:
            return (await http(app, "GET", "/api/bank")).json()
    assert asyncio.run(go())["bank"]["money"] == 1234567


def test_bank_on_an_older_mind_db_says_not_set_up(tmp_path):
    c = sqlite3.connect(tmp_path / "mind.db")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE cast_member(guid INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

    async def go():
        async with served(make_app(tmp_path, mind=running(tmp_path), link=FakeLink(SNAPSHOT))) as app:
            return (await http(app, "GET", "/api/bank")).json()
    v = asyncio.run(go())
    assert v["missing"] == ["realm_bank"] and v["ledger"] == []
    assert v["bank"]["money"] == 1234567 and v["items"] == {}  # no library.db: no names


def test_an_unreadable_library_gives_no_names_not_an_error(db, data):
    (data / "library.db").unlink()
    (data / "library.db").write_bytes(b"not a database")

    async def go():
        async with served(make_app(data, mind=running(data), link=FakeLink(SNAPSHOT))) as app:
            return await http(app, "GET", "/api/bank")
    r = asyncio.run(go())
    assert r.status == 200 and r.json()["items"] == {}


def test_bank_needs_the_key_and_the_host(db, data):
    async def go():
        async with served(make_app(data, mind=running(data), link=FakeLink(SNAPSHOT))) as app:
            assert (await http(app, "GET", "/api/bank", key=False)).status == 403
            assert (await http(app, "GET", "/api/bank", host="localhost:1")).status == 403
            assert (await http(app, "POST", "/api/bank", origin=True, body={})).status == 405
    asyncio.run(go())


def test_an_unreadable_mind_db_is_503(tmp_path):
    (tmp_path / "mind.db").mkdir()

    async def go():
        async with served(make_app(tmp_path, mind=running(tmp_path), link=FakeLink(SNAPSHOT))) as app:
            return await http(app, "GET", "/api/bank")
    r = asyncio.run(go())
    assert r.status == 503 and r.json()["error"].startswith("mind.db could not be read")


def test_nothing_from_the_bank_reaches_the_log(db, data, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        async with served(make_app(data, mind=running(data), link=FakeLink(SNAPSHOT))) as app:
            assert (await http(app, "GET", "/api/bank")).status == 200
    asyncio.run(go())
    for word in (SECRET, "Potions", "Linen Cloth", "Member1101", "1234567"):
        assert word not in caplog.text
