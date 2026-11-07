"""The Board tab: forum posts, delivered letters and write-ups from mind.db (boardview) with his
replies under them, its route and gates, and his replies: POST /api/board checked as the mind
checks it, refused in words while nothing can be saved, then on to the mind's board op. Scratch
databases, fake links and invented text only."""
import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from console.rig import FakeMind, http, make_app, running
from leeroy_mind.console import boardview, mindlink
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError

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
            assert v["replies_on"] is False and v["waiting"] == 0
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


def _reply(c, id, kind, parent, state, *, to=1101, text="an invented reply", reason=None,
           answer_post=None, answer_mail=None, ts=NOW - 5):
    c.execute("INSERT INTO board_reply(id,ts,kind,parent,to_guid,text,state,reason,settled,"
              "answer_post,answer_mail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (id, ts, kind, parent, to, text, state, reason,
               None if state == "waiting" else NOW, answer_post, answer_mail))


def test_the_board_shows_replies_under_their_post(db, tmp_path):
    _post(db, 5, NOW - 50, 1102, "member", "beta", "an invented answer")
    _reply(db, 1, "post", 1, "waiting", text=f"{SECRET} waiting")
    _reply(db, 2, "post", 3, "answered", to=1102, answer_post=5)
    _reply(db, 3, "post", 3, "withdrawn", to=1102, ts=NOW - 9)
    _reply(db, 4, "letter", 2, "refused", to=1102, reason="screen_name")
    _reply(db, 5, "letter", 3, "answered", answer_mail=1)
    _reply(db, 6, "post", 4, "posted")
    _reply(db, 7, "letter", 3, "waiting")
    db.commit()
    v = boardview.board_view(db, now=NOW, data_dir=tmp_path)
    assert v["replies_on"] is True and v["waiting"] == 2
    posts = {p["id"]: p for t in v["forum"] for p in t["posts"]}
    shape = {"id", "ts", "text", "state", "reason", "answer_post", "answer_mail"}
    assert all(set(r) == shape for p in posts.values() for r in p["replies"])
    assert [(r["id"], r["state"], r["text"]) for r in posts[1]["replies"]] == [
        (1, "waiting", f"{SECRET} waiting")]
    assert [(r["id"], r["state"]) for r in posts[3]["replies"]] == [(3, "withdrawn"),
                                                                    (2, "answered")]
    assert posts[3]["replies"][1]["answer_post"] == 5
    assert [r["state"] for r in posts[4]["replies"]] == ["posted"] and posts[2]["replies"] == []
    assert posts[5]["answers"] == 2 and posts[3]["answers"] is None and posts[1]["answers"] is None
    letters = {m["id"]: m for m in v["letters"]}
    assert [(r["id"], r["reason"]) for r in letters[2]["replies"]] == [(4, "screen_name")]
    assert [(r["id"], r["answer_mail"]) for r in letters[3]["replies"]] == [(5, 1), (7, None)]
    db.execute("DROP TABLE board_reply")
    v = boardview.board_view(db, now=NOW, data_dir=tmp_path)
    assert v["replies_on"] is False and v["waiting"] == 0
    assert all(p["replies"] == [] and p["answers"] is None for t in v["forum"] for p in t["posts"])
    assert all(m["replies"] == [] for m in v["letters"])


# -- his replies: POST /api/board ----------------------------------------------------------------

SAVED = {"ok": True, "message": "Saved. The guild answers tonight.", "id": 12}
REPLY = {"action": "reply", "kind": "post", "to": 7, "text": "hi"}


class FakeLink:
    """The mind's control endpoint, as the other tabs' tests fake it, with each call's timeout."""

    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        if self.error:
            raise self.error if isinstance(self.error, LinkError) else LinkError(self.error)
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args))
        self.timeouts.append(timeout)
        if self.error:
            raise self.error if isinstance(self.error, LinkError) else LinkError(self.error)
        return self.reply


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def send(app, body, **kw):
    return http(app, "POST", "/api/board", body=body, origin=True, **kw)


def test_a_reply_goes_to_the_minds_board_op(data):
    link = FakeLink(SAVED)

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            told = []
            app.broadcast = lambda kind, d: told.append((kind, d))
            r = await send(app, {"action": "reply", "kind": "post", "to": 7, "text": " hi \n"})
            assert r.status == 200 and r.json() == SAVED
            r = await send(app, {"action": "withdraw", "id": 12})
            assert r.status == 200
            assert [t for t in told if t[0] == "board"] == [
                ("board", {"action": "reply", "id": 12}), ("board", {"action": "withdraw", "id": 12})]
    asyncio.run(go())
    # board_args' answer, cleaned: what the mind checks again
    assert link.calls == [((101, 100), "board", REPLY),
                          ((101, 100), "board", {"action": "withdraw", "id": 12})]
    assert mindlink.BOARD_S == 10.0 and link.timeouts == [mindlink.BOARD_S] * 2


def test_a_reply_needs_the_key_and_the_origin(data):
    link = FakeLink(SAVED)

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/board", body=REPLY)).status == 403  # no Origin
            assert (await http(app, "POST", "/api/board", body=REPLY, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/board", body=REPLY,
                               origin="http://127.0.0.1:1")).status == 403
            assert (await http(app, "POST", "/api/board", body=b"action=reply", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert (await http(app, "POST", "/api/board", body=REPLY, origin=True,
                               host="localhost:1")).status == 403
    asyncio.run(go())
    assert link.calls == []


def test_no_control_refuses_replies(data):
    link = FakeLink(SAVED)
    off = Controls(enabled=False, reason="process control is off (--no-control)",
                   mind=running(data))

    async def go():
        async with served(make_app(data, mind=running(data), controls=off, link=link)) as app:
            for body in (REPLY, {"action": "withdraw", "id": 1}):
                r = await send(app, body)
                assert r.status == 403 and r.json() == {
                    "ok": False, "error": "Replies are off: this console runs with --no-control."}
            assert (await http(app, "GET", "/api/board")).status == 200  # reading stays on
    asyncio.run(go())
    assert link.calls == []


def test_a_reply_with_the_mind_down_says_so(data):
    """Review Focus 2: the mind down, its state unclear, a start or stop of it under way, no link
    or a link that fails: each is said as what it is, and nothing is sent."""
    link = FakeLink(SAVED)

    async def go():
        async with served(make_app(data, mind=FakeMind(data), link=link)) as app:
            r = await send(app, REPLY)
            assert r.status == 409 and r.json() == {
                "ok": False, "error": "The mind is stopped, so nothing can be saved."}
        async with served(make_app(data, mind=FakeMind(data, error="ambiguous family"),
                                   link=link)) as app:
            r = await send(app, REPLY)
            assert r.status == 409 and r.json()["error"] == (
                "The mind's state is unclear, so nothing can be saved.")
        async with served(make_app(data, mind=running(data), link=link)) as app:
            app.controls.busy = ("mind", "restart")
            r = await send(app, REPLY)
            assert r.status == 409 and r.json()["error"] == (
                "A restart of the mind is under way, so nothing can be saved.")
            app.controls.busy = ("voice", "start")  # the voice service holds nothing here
            assert (await send(app, REPLY)).status == 200
        assert len(link.calls) == 1
        async with served(make_app(data, mind=running(data), link=None)) as app:
            app.link = None
            r = await send(app, REPLY)
            assert r.status == 409 and r.json()["error"] == (
                "Nothing was saved: the console has no link to the mind's control endpoint.")
        gone = FakeLink(error="the mind is not listening where control.json says")
        async with served(make_app(data, mind=running(data), link=gone)) as app:
            r = await send(app, REPLY)
            assert r.status == 409 and r.json()["error"] == (
                "Nothing was saved: the mind is not listening where control.json says.")
        silent = FakeLink(error=LinkError("the control endpoint did not answer (TimeoutError)",
                                          unanswered=True))
        async with served(make_app(data, mind=running(data), link=silent)) as app:
            r = await send(app, REPLY)
            assert r.status == 409 and r.json()["error"] == (
                "The control endpoint did not answer (TimeoutError). Look at the Board before "
                "sending it again.")
    asyncio.run(go())


@pytest.mark.parametrize("body, words", [
    ({"action": "reply", "kind": "post", "to": 7, "text": "  "}, "There's nothing to send."),
    ({"action": "reply", "kind": "post", "to": 7, "text": "a" * 201},
     "That's too long for a forum post (200 characters at most)."),
    ({"action": "reply", "kind": "letter", "to": 7, "text": "a" * 401},
     "That's too long for a letter (400 characters at most)."),
    ({"action": "reply", "kind": "letter", "to": 7, "text": "hi \ud800"},
     "That text can't be stored."),
])
def test_a_bad_reply_is_400_with_its_words_before_anything_else(data, body, words):
    link = FakeLink(SAVED)
    off = Controls(enabled=False, reason="process control is off (--no-control)",
                   mind=FakeMind(data))

    async def go():
        async with served(make_app(data, controls=off, link=link)) as app:  # the mind down too
            r = await send(app, body)
            assert r.status == 400 and r.json() == {"ok": False, "error": words}
            for shape in ({"action": "reply", "kind": "say", "to": 7, "text": "hi"},
                          {"action": "withdraw"}, {"action": "withdraw", "id": True}):
                assert (await send(app, shape)).status == 400
            assert (await http(app, "POST", "/api/board", body=b'["reply"]', origin=True)).status == 400
    asyncio.run(go())
    assert link.calls == []


def test_the_minds_refusal_comes_back_409(data):
    async def go():
        for reply, words in (
                ({"ok": False, "error": "You already replied to that. The answer comes tonight."},
                 "You already replied to that. The answer comes tonight."),
                ({"ok": False, "error": "The Realm is off, so nobody would answer. Turn it on in "
                                        "mind.toml ([realm] enabled) and mod_leeroy.conf "
                                        "(Leeroy.Realm.Enable) first."},
                 "The Realm is off, so nobody would answer. Turn it on in mind.toml ([realm] "
                 "enabled) and mod_leeroy.conf (Leeroy.Realm.Enable) first."),
                # A mind from before the board op
                ({"ok": False, "error": "bad request: op is one of status, stop, raid, tune"},
                 "The running mind predates Board replies: restart it after the update."),
                ({"ok": False}, "The mind refused it.")):
            link = FakeLink(reply)
            async with served(make_app(data, mind=running(data), link=link)) as app:
                told = []
                app.broadcast = lambda kind, d: told.append(kind)
                r = await send(app, REPLY)
                assert r.status == 409 and r.json() == {"ok": False, "error": words}
                assert link.calls == [((101, 100), "board", REPLY)]
                assert "board" not in told
    asyncio.run(go())


def test_no_reply_text_reaches_the_console_log(data, caplog):
    caplog.set_level(logging.DEBUG)
    link = FakeLink({"ok": False, "error": "That post is no longer on the forum."})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            await send(app, {**REPLY, "text": f"{SECRET} reply"})
            await send(app, {**REPLY, "text": f"{SECRET} " * 40})
    asyncio.run(go())
    assert SECRET not in caplog.text


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
