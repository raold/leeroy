"""His Board replies: the owner's answer, from the guild console, to a Realm forum post or a letter
in his mailbox. The console's one write: POST /api/board, then the control endpoint's `board` op
(endpoint.board_args checks and cleans its arguments), then BoardDesk.handle, which stores the
reply in mind.db's board_reply `waiting`. The Realm night takes waiting replies up (waiting) and
settles each (settle): an answer, his post standing unanswered, a refusal by its screen, or
nothing when he withdrew it first. A night whose answer fails (the screen refuses it, or the model
gives none) leaves it waiting; answer_failed counts those nights, and on the ANSWER_NIGHTS-th the
night settles it refused, reason NO_ANSWER.

The text rule, which the page's boardmodel.mjs applies too (the shared fixture
tests/fixtures/board_reply_text.jsonl): the box holds at most RAW_MAX characters; cleaning drops
"|" and control characters and collapses whitespace (the chat-line rule, clean_say_text); then
nothing left, or more than POST_MAX (a forum post) or LETTER_MAX (a letter) characters, is refused,
never cut. A string that does not encode as UTF-8 (a lone surrogate) cannot be stored.

Logs carry the kind and the outcome only: never the text, a post, a letter or a name."""
import json
import sqlite3

from leeroy_mind.logs import log as log_event
from leeroy_mind.protocol import clean_say_text

POST_MAX = 200  # a forum fact's limit
LETTER_MAX = 400
RAW_MAX = 1024  # what the console's box may hold before it is cleaned
KINDS = ("post", "letter")
SETTLED = ("answered", "posted", "refused", "withdrawn")
ANSWER_NIGHTS = 3  # nights a reply's answer may fail before it is settled refused, NO_ANSWER
NO_ANSWER = "no_answer"

STOPPING = "The mind is stopping."
NO_TABLE = "This mind.db predates Board replies: stop the mind and run db migrate."
REALM_OFF = ("The Realm is off, so nobody would answer. Turn it on in mind.toml ([realm] enabled) "
             "and mod_leeroy.conf (Leeroy.Realm.Enable) first.")
POST_GONE = "That post is no longer on the forum."
NOT_IN_BOX = "That letter isn't in your mailbox."
NO_PERSONA = "{name} has no persona, so they can't write back."
WAITING = "You already replied to that. The answer comes tonight."
EMPTY = "There's nothing to send."
TOO_LONG = {"post": "That's too long for a forum post (200 characters at most).",
            "letter": "That's too long for a letter (400 characters at most)."}
NOT_UTF8 = "That text can't be stored."
SAVED = "Saved. The guild answers tonight."
WITHDRAWN = "Withdrawn."
NOT_WAITING = "That reply was already taken up by the night."
NO_SUCH = "There is no such reply."


def clean_reply(text: str) -> str:
    """Control characters and "|" dropped, whitespace collapsed to single spaces, none at
    either end."""
    return clean_say_text(text)


def text_problem(kind: str, text: str) -> tuple[str | None, str]:
    """(None, the cleaned text) when a reply of this kind may hold text, else (its refusal's
    words, "")."""
    if len(text) > RAW_MAX:
        return TOO_LONG[kind], ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return NOT_UTF8, ""
    cleaned = clean_reply(text)
    if not cleaned:
        return EMPTY, ""
    if len(cleaned) > (POST_MAX if kind == "post" else LETTER_MAX):
        return TOO_LONG[kind], ""
    return None, cleaned


class Refused(Exception):
    """A refusal: its text is the console's words, outcome what the log says."""

    def __init__(self, outcome: str, words: str, *, kind: str | None = None):
        super().__init__(words)
        self.outcome, self.kind = outcome, kind


class BoardDesk:
    """The running mind's side of the `board` op (endpoint.ControlEndpoint.board, which runs one
    at a time): his reply stored waiting, or a waiting one withdrawn. Its arguments are
    endpoint.board_args' answer, already checked and cleaned."""

    def __init__(self, app):
        self.app = app

    async def handle(self, args: dict) -> dict:
        """Checked in this order: the mind stopping, the table, the Realm on (a reply only), what
        it answers, one waiting reply per post or letter. {"ok": True, "message", "id"} or
        {"ok": False, "error"}, in the console's words."""
        kind = args.get("kind")
        try:
            kind, message, reply_id = self._handle(args)
        except Refused as e:
            log_event("board_reply", kind=e.kind or kind or "none", outcome=e.outcome)
            return {"ok": False, "error": str(e)}
        finally:
            store = getattr(self.app, "store", None)
            if store is not None and not store.in_unit:
                store.conn.commit()  # end a read outside a unit, so it never pins a snapshot
        log_event("board_reply", kind=kind, outcome="saved" if args["action"] == "reply"
                  else "withdrawn")
        return {"ok": True, "message": message, "id": reply_id}

    def _handle(self, args: dict) -> tuple[str, str, int]:
        if self.app.stopping:
            raise Refused("stopping", STOPPING)
        conn = self.app.store.conn
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'board_reply'").fetchall():
            raise Refused("no_table", NO_TABLE)
        if args["action"] == "reply":
            return args["kind"], SAVED, self._reply(conn, args)
        return self._withdraw(conn, args["id"]), WITHDRAWN, args["id"]

    def _realm_on(self) -> bool:
        realm = getattr(self.app, "realm", None)
        return realm is not None and getattr(realm, "enabled", None) is True

    def _reply(self, conn, args: dict) -> int:
        kind, parent, text = args["kind"], args["to"], args["text"]
        if not self._realm_on():
            raise Refused("realm_off", REALM_OFF)
        store = self.app.store
        with store.transaction():
            if kind == "post":
                row = conn.execute("SELECT author FROM realm_post WHERE id = ?",
                                   (parent,)).fetchone()
                if row is None or not (type(row[0]) is int and 1 <= row[0] <= 0xFFFFFFFF):
                    raise Refused("post_gone", POST_GONE)
                to = row[0]
            else:
                from leeroy_mind.personas.tiers import cast_sql
                row = conn.execute(
                    "SELECT m.sender, c.name, EXISTS(SELECT 1 FROM persona_core p "
                    "WHERE p.guid = m.sender AND " + cast_sql(conn,"p.guid") + ") FROM realm_mail m JOIN cast_member c "
                    "ON c.guid = m.sender WHERE m.id = ? AND m.state IN ('sent','taken')",
                    (parent,)).fetchone()
                if row is None or not (type(row[0]) is int and 1 <= row[0] <= 0xFFFFFFFF):
                    raise Refused("letter_gone", NOT_IN_BOX)
                if not row[2]:
                    raise Refused("no_persona", NO_PERSONA.format(name=row[1]))
                to = row[0]
            if conn.execute("SELECT 1 FROM board_reply WHERE kind = ? AND parent = ? "
                            "AND state = 'waiting'", (kind, parent)).fetchall():
                raise Refused("waiting", WAITING)
            try:
                cur = conn.execute(
                    "INSERT INTO board_reply(ts, kind, parent, to_guid, text, state) "
                    "VALUES (?, ?, ?, ?, ?, 'waiting')",
                    (self.app.clock.now(), kind, parent, to, text))
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e):
                    raise Refused("waiting", WAITING) from None
                raise
        return cur.lastrowid

    def _withdraw(self, conn, reply_id: int) -> str:
        """The withdrawn reply's kind. The night may have taken it up meanwhile: settle changes
        only a waiting row, so a reply is never both answered and withdrawn."""
        with self.app.store.transaction():
            row = conn.execute("SELECT kind FROM board_reply WHERE id = ?",
                               (reply_id,)).fetchone()
            if row is None:
                raise Refused("no_such", NO_SUCH)
            if not settle(conn, reply_id, state="withdrawn", night=None,
                          now=self.app.clock.now()):
                raise Refused("not_waiting", NOT_WAITING, kind=row[0])
        return row[0]


# -- for the Realm night --------------------------------------------------------------------------

def waiting(conn, kind: str, limit: int) -> list[sqlite3.Row]:
    """The waiting replies of one kind, oldest first, at most limit of them. A read only: inside
    the caller's own unit, or followed by its commit."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    return conn.execute("SELECT * FROM board_reply WHERE state = 'waiting' AND kind = ? "
                        "ORDER BY ts, id LIMIT ?", (kind, max(0, int(limit)))).fetchall()


def _failed_key(reply_id: int) -> str:
    return f"board:reply:{reply_id}:failed"


def answer_failed(conn, reply_id: int, night: str) -> int:
    """Count `night` once among the nights this reply's answer failed (meta
    board:reply:<id>:failed, the nights as a JSON list; an unreadable one starts again) and
    return how many there are. Inside the caller's unit; it never commits."""
    key = _failed_key(reply_id)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        nights = json.loads(row[0]) if row is not None else []
    except (TypeError, ValueError):
        nights = []
    if not (isinstance(nights, list) and all(isinstance(n, str) for n in nights)):
        nights = []
    if night not in nights:
        nights.append(night)
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(nights)))
    return len(nights)


def settle(conn, reply_id: int, *, state: str, night: str | None, now: float,
           reason: str | None = None, fact: int | None = None, answer_post: int | None = None,
           answer_mail: int | None = None) -> bool:
    """Settle one waiting reply: True when it was waiting and is now `state` (the nights its
    answer failed are forgotten), False when it was not (taken up already, withdrawn, or no such
    reply), which changes nothing. Inside the caller's unit; it never commits."""
    if state not in SETTLED:
        raise ValueError(f"state is one of {', '.join(SETTLED)}")
    cur = conn.execute(
        "UPDATE board_reply SET state = ?, night = ?, settled = ?, reason = ?, fact = ?, "
        "answer_post = ?, answer_mail = ? WHERE id = ? AND state = 'waiting'",
        (state, night, now, reason, fact, answer_post, answer_mail, reply_id))
    if cur.rowcount != 1:
        return False
    conn.execute("DELETE FROM meta WHERE key = ?", (_failed_key(reply_id),))
    return True
