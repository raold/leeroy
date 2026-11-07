"""His Board replies (leeroy_mind.board): the text rule (the shared fixture board_reply_text.jsonl,
which the page's boardmodel.mjs reads too), the op's strict arguments (endpoint.board_args), the
desk that stores a reply waiting for the night, and the two calls the night makes. Scratch
databases and invented text only."""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import board
from leeroy_mind.endpoint import BadRequest, board_args
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_793_304_000.0
SECRET = "INVENTEDREPLYTEXT"
MIGRATE = "This mind.db predates Board replies: stop the mind and run db migrate."
REALM_OFF = ("The Realm is off, so nobody would answer. Turn it on in mind.toml ([realm] enabled) "
             "and mod_leeroy.conf (Leeroy.Realm.Enable) first.")
SAVED = "Saved. The guild answers tonight."
AGAIN = "You already replied to that. The answer comes tonight."
NOT_IN_BOX = "That letter isn't in your mailbox."


def rows(name: str) -> list[dict]:
    return [json.loads(t) for t in (FIXTURES / name).read_text(encoding="utf-8").split("\n")
            if t.strip()]


def reply(kind="post", to=7, text="see you thursday"):
    return {"action": "reply", "kind": kind, "to": to, "text": text}


# -- the text and the arguments ------------------------------------------------------------------

def test_reply_text_rules():
    assert (board.POST_MAX, board.LETTER_MAX, board.RAW_MAX) == (200, 400, 1024)
    table = rows("board_reply_text.jsonl")
    assert {"plain", "spaces", "pipes", "bell", "emoji", "a200", "a201", "a400", "a401", "empty",
            "blank", "only_pipes"} <= {r["name"] for r in table}
    for row in table:
        assert board.clean_reply(row["text"]) == row["clean"], row["name"]
        for kind in ("post", "letter"):
            ok = row[f"{kind}_ok"]
            try:
                args = board_args(reply(kind, 7, row["text"]))
            except BadRequest:
                assert not ok, (row["name"], kind)
            else:
                assert ok and args["text"] == row["clean"], (row["name"], kind)
    emoji = next(r for r in table if r["name"] == "emoji")
    assert len(board.clean_reply(emoji["text"])) == 4  # code points, as the page counts them


def test_board_args_are_exact():
    assert board_args(reply(text="  see  you\nthursday ")) == {
        "action": "reply", "kind": "post", "to": 7, "text": "see you thursday"}
    assert board_args(reply("letter", (1 << 63) - 1, "hi")) == {
        "action": "reply", "kind": "letter", "to": (1 << 63) - 1, "text": "hi"}
    assert board_args({"action": "withdraw", "id": 3}) == {"action": "withdraw", "id": 3}
    for bad in ({**reply(), "extra": 1}, {"action": "reply", "kind": "post", "text": "hi"},
                {**reply(), "kind": "say"}, {**reply(), "to": 0}, {**reply(), "to": -1},
                {**reply(), "to": True}, {**reply(), "to": "7"}, {**reply(), "to": 1 << 63},
                {**reply(), "to": 7.0}, {**reply(), "text": 5}, {**reply(), "text": None},
                {"action": "withdraw"}, {"action": "withdraw", "id": True},
                {"action": "withdraw", "id": 0}, {"action": "withdraw", "id": 3, "kind": "post"},
                {"action": "edit", "id": 3}, {}, [], "reply", None):
        with pytest.raises(BadRequest):
            board_args(bad)
    with pytest.raises(BadRequest, match=r"^That text can't be stored\.$"):
        board_args(reply(text="\ud800"))
    with pytest.raises(BadRequest, match=r"^That text can't be stored\.$"):
        board_args(reply("letter", text="hi \udc00 there"))
    with pytest.raises(BadRequest, match=r"^There's nothing to send\.$"):
        board_args(reply(text=" | \u0007 "))
    with pytest.raises(BadRequest, match=r"^That's too long for a forum post \(200 characters at "
                                         r"most\)\.$"):
        board_args(reply(text=" " * (board.RAW_MAX + 1)))  # the raw box first, before any clean
    with pytest.raises(BadRequest, match=r"^That's too long for a letter \(400 characters at "
                                         r"most\)\.$"):
        board_args(reply("letter", text="a" * (board.RAW_MAX + 1)))
    assert board_args(reply("letter", text=" " * (board.RAW_MAX - 1) + "a"))["text"] == "a"


# -- the desk ------------------------------------------------------------------------------------

class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def now(self):
        return self.t


class App:
    """What the desk reads of the mind: its store and clock, whether it is stopping, the Realm."""

    def __init__(self, store, *, realm=True):
        self.store, self.clock, self.stopping = store, Clock(), False
        self.realm = SimpleNamespace(enabled=realm) if realm is not None else None


def ask(desk, args):
    return asyncio.run(desk.handle(args))


def _member(c, guid, name, *, persona=True):
    c.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) "
              "VALUES(?,?,1,1,1,80,3,0)", (guid, name))
    if persona:
        c.execute("INSERT INTO persona_core(guid,json,card,card_tokens,version,source,updated) "
                  "VALUES(?,'{}','card',1,1,'seed',0)", (guid,))


def _seed(store):
    c = store.conn
    _member(c, 1101, "Inventa")
    _member(c, 1102, "Nopersona", persona=False)
    c.execute("INSERT INTO realm_post(id,ts,author,author_kind,thread,text) "
              "VALUES(7,?,1101,'member','alpha','an invented post')", (NOW - 60,))
    c.execute("INSERT INTO realm_post(id,ts,author,author_kind,thread,text) "
              "VALUES(8,?,9001,'actor','alpha','a rival line')", (NOW - 50,))
    for id, sender, state in ((1, 1101, "ready"), (2, 1101, "taken"), (3, 1102, "sent"),
                              (4, 9999, "sent"), (5, 1101, "failed"), (6, 1101, "sent")):
        c.execute("INSERT INTO realm_mail(id,ts,sender,kind,subject,body,state) "
                  "VALUES(?,?,?,'letter','Hello','An invented letter',?)", (id, NOW - 30, sender, state))
    c.commit()


@pytest.fixture
def seeded(store):
    _seed(store)
    return store


def _row(store, id):
    return store.conn.execute("SELECT * FROM board_reply WHERE id = ?", (id,)).fetchone()


def test_a_forum_reply_is_stored_waiting(seeded):
    app = App(seeded)
    desk = board.BoardDesk(app)
    out = ask(desk, board_args(reply(text="  see you   thursday ")))
    assert out == {"ok": True, "message": SAVED, "id": out["id"]} and out["id"] >= 1
    r = _row(seeded, out["id"])
    assert (r["kind"], r["parent"], r["to_guid"], r["text"], r["state"]) == (
        "post", 7, 1101, "see you thursday", "waiting")
    assert r["ts"] == app.clock.now() == NOW and r["settled"] is None and r["reason"] is None
    assert r["night"] is None and r["fact"] is None and r["answer_post"] is None
    rival = ask(desk, reply(to=8, text="you wish"))  # a rival officer's post: its author
    assert rival["ok"] is True and _row(seeded, rival["id"])["to_guid"] == 9001
    assert ask(desk, reply(to=99)) == {"ok": False, "error": "That post is no longer on the forum."}


def test_a_letter_reply_needs_a_delivered_letter_from_a_member_with_a_persona(seeded):
    desk = board.BoardDesk(App(seeded))
    for id in (1, 4, 5, 99):  # written but not delivered, a sender not in the cast, failed, absent
        assert ask(desk, reply("letter", id, "thanks")) == {"ok": False, "error": NOT_IN_BOX}, id
    assert ask(desk, reply("letter", 3, "thanks")) == {
        "ok": False, "error": "Nopersona has no persona, so they can't write back."}
    out = ask(desk, reply("letter", 2, "a" * 400))
    assert out["ok"] is True
    r = _row(seeded, out["id"])
    assert (r["kind"], r["parent"], r["to_guid"], len(r["text"])) == ("letter", 2, 1101, 400)
    assert ask(desk, reply("letter", 6, "thanks"))["ok"] is True  # sent, not yet taken
    assert seeded.conn.execute("SELECT count(*) FROM board_reply").fetchone()[0] == 2


def test_one_waiting_reply_per_post_or_letter(seeded):
    desk = board.BoardDesk(App(seeded))
    first = ask(desk, reply(text="first"))
    assert first["ok"] is True
    assert ask(desk, reply(text="second")) == {"ok": False, "error": AGAIN}
    assert ask(desk, reply("letter", 7, "x")) == {"ok": False, "error": NOT_IN_BOX}  # kind apart
    assert ask(desk, reply("letter", 2, "letter one"))["ok"] is True
    assert ask(desk, reply("letter", 2, "letter two")) == {"ok": False, "error": AGAIN}
    assert ask(desk, reply(to=8, text="another post"))["ok"] is True
    # The index holds it even past the desk's own check
    with pytest.raises(Exception, match="UNIQUE"):
        seeded.conn.execute("INSERT INTO board_reply(ts,kind,parent,to_guid,text,state) "
                            "VALUES(1,'post',7,1101,'x','waiting')")
    seeded.conn.rollback()
    # Once the night has taken it, he may reply again
    with seeded.transaction():
        assert board.settle(seeded.conn, first["id"], state="posted", night="2026-11-07", now=NOW)
    assert ask(desk, reply(text="third"))["ok"] is True


def test_the_realm_off_refuses_with_its_words(seeded):
    for realm in (False, None):
        desk = board.BoardDesk(App(seeded, realm=realm))
        assert ask(desk, reply()) == {"ok": False, "error": REALM_OFF}
    off = App(seeded)
    off.realm.enabled = 1  # only the flag itself counts
    assert ask(board.BoardDesk(off), reply()) == {"ok": False, "error": REALM_OFF}
    assert seeded.conn.execute("SELECT count(*) FROM board_reply").fetchone()[0] == 0
    # The order: stopping first, then the Realm, then the parent
    stopping = App(seeded, realm=False)
    stopping.stopping = True
    assert ask(board.BoardDesk(stopping), reply(to=99)) == {"ok": False,
                                                            "error": "The mind is stopping."}
    assert ask(board.BoardDesk(App(seeded, realm=False)), reply(to=99))["error"] == REALM_OFF


def test_withdraw_only_while_waiting(seeded):
    app = App(seeded)
    desk = board.BoardDesk(app)
    a, b = ask(desk, reply(text="one"))["id"], ask(desk, reply(to=8, text="two"))["id"]
    app.clock.t += 60
    assert ask(desk, {"action": "withdraw", "id": a}) == {"ok": True, "message": "Withdrawn.",
                                                          "id": a}
    r = _row(seeded, a)
    assert r["state"] == "withdrawn" and r["settled"] == NOW + 60
    assert ask(desk, {"action": "withdraw", "id": a}) == {
        "ok": False, "error": "That reply was already taken up by the night."}
    with seeded.transaction():
        assert board.settle(seeded.conn, b, state="answered", night="2026-11-07", now=NOW,
                            answer_post=8)
    assert ask(desk, {"action": "withdraw", "id": b})["error"] == (
        "That reply was already taken up by the night.")
    assert ask(desk, {"action": "withdraw", "id": 999}) == {"ok": False,
                                                            "error": "There is no such reply."}
    app.realm.enabled = False  # a waiting reply can be taken back with the Realm off
    c = ask(board.BoardDesk(App(seeded)), reply(text="three"))["id"]
    assert ask(desk, {"action": "withdraw", "id": c})["ok"] is True
    app.stopping = True
    assert ask(desk, {"action": "withdraw", "id": c}) == {"ok": False,
                                                          "error": "The mind is stopping."}


def test_settle_and_waiting_for_the_night(seeded):
    app = App(seeded)
    desk = board.BoardDesk(app)
    ids = []
    for to in (7, 8):
        ids.append(ask(desk, reply(to=to, text=f"post {to}"))["id"])
        app.clock.t += 1
    ids.append(ask(desk, reply("letter", 2, "a letter"))["id"])
    assert [r["id"] for r in board.waiting(seeded.conn, "post", 5)] == ids[:2]
    assert [r["id"] for r in board.waiting(seeded.conn, "post", 1)] == ids[:1]
    assert [r["id"] for r in board.waiting(seeded.conn, "letter", 5)] == ids[2:]
    with seeded.transaction():
        assert board.settle(seeded.conn, ids[0], state="refused", night="2026-11-07", now=NOW,
                            reason="screen_name") is True
        assert board.settle(seeded.conn, ids[0], state="answered", night="2026-11-07",
                            now=NOW) is False  # changes only a waiting row
        assert board.settle(seeded.conn, 999, state="posted", night="2026-11-07", now=NOW) is False
        assert board.settle(seeded.conn, ids[2], state="answered", night="2026-11-07", now=NOW,
                            answer_mail=6) is True
    r = _row(seeded, ids[0])
    assert (r["state"], r["reason"], r["night"], r["settled"]) == ("refused", "screen_name",
                                                                    "2026-11-07", NOW)
    assert _row(seeded, ids[2])["answer_mail"] == 6
    assert [r["id"] for r in board.waiting(seeded.conn, "post", 5)] == ids[1:2]
    with pytest.raises(ValueError):
        board.settle(seeded.conn, ids[1], state="waiting", night="x", now=NOW)


def test_a_reply_survives_a_mind_restart(seeded, clock):
    path = Path(seeded.conn.execute("PRAGMA database_list").fetchone()["file"])
    saved = ask(board.BoardDesk(App(seeded)), reply(text="before the restart"))
    seeded.close()
    store = Store(open_db(path), clock=clock)
    try:
        desk = board.BoardDesk(App(store))  # a fresh desk on the same store
        got = board.waiting(store.conn, "post", 3)
        assert [(r["id"], r["text"], r["state"]) for r in got] == [
            (saved["id"], "before the restart", "waiting")]
        assert ask(desk, reply(text="again")) == {"ok": False, "error": AGAIN}
    finally:
        store.close()
    seeded.conn = open_db(path)  # the fixture closes what it holds


def test_an_older_mind_db_says_migrate(tmp_path, monkeypatch, clock):
    from test_migrations import _db_at
    from leeroy_mind.storage import db as db_mod
    number = next(v for v, p in db_mod._migration_files() if p.stem.endswith("_board_reply"))
    conn = _db_at(tmp_path / "mind.db", number - 1, tmp_path, monkeypatch)
    store = Store(conn, clock=clock)
    try:
        desk = board.BoardDesk(App(store))
        assert ask(desk, reply()) == {"ok": False, "error": MIGRATE}
        assert ask(desk, {"action": "withdraw", "id": 1}) == {"ok": False, "error": MIGRATE}
        stopping = App(store)
        stopping.stopping = True
        assert ask(board.BoardDesk(stopping), reply())["error"] == "The mind is stopping."
    finally:
        conn.close()


def test_the_log_never_carries_the_text(seeded, caplog):
    desk = board.BoardDesk(App(seeded))
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        saved = ask(desk, reply(text=f"{SECRET} one"))
        ask(desk, reply(text=f"{SECRET} two"))
        ask(desk, reply("letter", 3, f"{SECRET} three"))
        ask(desk, {"action": "withdraw", "id": saved["id"]})
    assert SECRET not in caplog.text and "Nopersona" not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("board_reply")]
    assert lines == ["board_reply: kind=post outcome=saved",
                     "board_reply: kind=post outcome=waiting",
                     "board_reply: kind=letter outcome=no_persona",
                     "board_reply: kind=post outcome=withdrawn"]
