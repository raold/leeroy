"""Intents: strings that are nudges (D6 Part A, task 3). He gives a member an intent; she carries
it out in her own words and way, or it is reported back to him as not taken with a reason code.

Rows live in godmode_intent (migration strings). Part A runs the Discord kinds through D2:

- post and thread: an `intent` seed in the server engine with her as the opener, taken when its
  line is sent (a thread opens on it under his name for it);
- bring_up: the same seed, and also taken when a Discord line of hers in its scope mentions the
  topic's words (moves.topics, one or more); it expires otherwise;
- react: done by the server's Reactor with the named emoji;
- dm_him: a DM opener on D2's DM path, taken when it is sent (or when her DM to him mentions it).

The game kinds (come_to_me, join_group, follow, duel_me, emote, sit) are the native strings of
Part B; until then they are refused here (`not_yet`). Group DMs are never an intent's place: no
seed, card line or mention counts there. Nothing here logs text: ids, kinds and codes only."""
import json
import logging
import re

from leeroy_mind.logs import log as log_event

log = logging.getLogger(__name__)

DISCORD_KINDS = ("post", "thread", "react", "dm_him", "bring_up")
GAME_KINDS = ("come_to_me", "join_group", "follow", "duel_me", "emote", "sit")   # executed by Task 4
KINDS = DISCORD_KINDS + GAME_KINDS
STATUSES = ("pending", "taken", "not_taken", "expired", "cancelled")
DUE_S = 24 * 3600.0
GAME_DUE_S = 600.0
PER_MEMBER = 3
MAX_PENDING = 32
ABOUT_MAX = 200
TOPIC_MAX = 120
SEED_WEIGHT = 4.0
BRING_UP_CHANNELS = ("general", "irl")
# A carried-out attempt that came to nothing (a dropped draft, an opener that never named the
# topic) waits BACKOFF_S, doubling, and after MAX_TRIES the intent is reported not taken.
BACKOFF_S = 300.0
MAX_TRIES = 4
TRIES_KEY = "godmode:intent:tries:{id}"
DM_FLOOR_S = 600.0  # never right after an opener of her own
LOVE = frozenset({"❤️", "😍", "😊", "🫂", "💕", "😘", "🥰"})
_ARGS = {"post": ({"channel", "about"}, set()), "thread": ({"channel", "name", "about"}, set()),
         "react": ({"message", "emoji"}, set()), "dm_him": ({"about"}, set()),
         "bring_up": ({"topic"}, {"channel"})}


class IntentRefused(ValueError):
    """A refusal with a code (the log, the reply) and words (the console)."""

    def __init__(self, code: str, words: str):
        super().__init__(words)
        self.code = code


def parse(args) -> dict:
    """The endpoint's exact shape check for the `string` op; never echoes submitted text."""
    if not isinstance(args, dict):
        raise ValueError("A string action is create, cancel or list.")
    action = args.get("action")
    if action == "list" and set(args) <= {"action", "member"}:
        m = args.get("member")
        if m is None or type(m) is int and 0 < m < 1 << 32:
            return dict(args)
    if action == "cancel" and set(args) == {"action", "id"}:
        if type(args["id"]) is int and 0 < args["id"] < 1 << 32:
            return dict(args)
    if action == "create" and set(args) == {"action", "member", "kind", "args"}:
        m, kind, a = args["member"], args["kind"], args["args"]
        if type(m) is not int or not 0 < m < 1 << 32 or kind not in KINDS or not isinstance(a, dict):
            raise ValueError("A string names a member, a kind and its arguments.")
        if kind in _ARGS:
            need, may = _ARGS[kind]
            if not need <= set(a) <= need | may:
                raise ValueError("That string's arguments are: " + ", ".join(sorted(need | may)) + ".")
            for k in ("channel", "about", "name", "emoji", "topic"):
                if k in a and (not isinstance(a[k], str) or len(a[k]) > 1000):
                    raise ValueError("A string's text fields are text.")
            if "message" in a and (type(a["message"]) is not int or not 0 < a["message"] < 1 << 63):
                raise ValueError("A reaction names a message id.")
        return dict(args)
    raise ValueError("A string action is create {member, kind, args}, cancel {id} or list.")


def _row(r) -> dict:
    out = dict(zip(("id", "member", "kind", "args", "status", "reason", "created", "due",
                    "settled", "change"), r))
    out["args"] = json.loads(out["args"])
    return out


_COLS = "id,member,kind,args,status,reason,created,due,settled,change"


def get(conn, ident) -> dict | None:
    r = conn.execute(f"SELECT {_COLS} FROM godmode_intent WHERE id=?", (ident,)).fetchone()
    return None if r is None else _row(r)


def waiting(conn, ident) -> bool:
    """Whether the intent is still pending (not cancelled, settled or gone)."""
    row = conn.execute("SELECT status FROM godmode_intent WHERE id=?", (ident,)).fetchone()
    return row is not None and row[0] == "pending"


def pending(conn, member=None) -> list[dict]:
    sql = f"SELECT {_COLS} FROM godmode_intent WHERE status='pending'"
    params = ()
    if member is not None:
        sql += " AND member=?"
        params = (member,)
    try:
        return [_row(r) for r in conn.execute(sql + " ORDER BY id", params)]
    except Exception:
        return []  # an older mind.db without the table: no intents


def listing(conn, *, member=None, limit=50) -> list[dict]:
    sql = f"SELECT {_COLS} FROM godmode_intent"
    params = []
    if member is not None:
        sql += " WHERE member=?"
        params.append(member)
    return [_row(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]


def _officers(pic):
    return {g for g, p in pic.personas.items() if p.play.guild_role == "officer"}


def _channel(pic, member, channel):
    from leeroy_mind.discord import layout
    ch = layout.channel(channel)
    if ch is None or ch.category == "info":
        raise IntentRefused("channel", "Choose one of the server's conversation channels.")
    if member not in layout.audience(channel, cast=pic.cast, officers=_officers(pic), human=pic.human):
        raise IntentRefused("audience", "She cannot read that channel.")
    return channel


def create(store, *, member, kind, args, human, now, pic=None, server=True) -> int:
    """Check, screen and store one intent; its id. `human` is his HumanIdentity (the screens),
    `server` whether the D2 engine runs (the Discord kinds need it)."""
    from leeroy_mind.tuning import edits as E
    if pic is None:
        pic = E._picture(store)
    if kind in GAME_KINDS:
        raise IntentRefused("not_yet", "Strings in the game come with the native strings update.")
    if kind not in DISCORD_KINDS:
        raise IntentRefused("kind", "Unknown string.")
    if not server:
        raise IntentRefused("server_off", "Discord strings need the guild Discord's engine "
                                          "([server] enabled).")
    try:
        E._cast_member(pic, member)
    except E.EditRefused as e:
        raise IntentRefused(e.code, str(e)) from None

    def text(value, cap):
        try:
            return E.screen_text(value, cap=cap, pic=pic, human=human, ages=False)
        except E.EditRefused as e:
            raise IntentRefused(e.code, str(e)) from None

    a = {}
    if kind in ("post", "thread"):
        a["channel"] = _channel(pic, member, args["channel"])
        a["about"] = text(args["about"], ABOUT_MAX)
        if kind == "thread":
            from leeroy_mind.discord.text import MAX_THREAD_NAME, clean_name
            a["name"] = text(clean_name(args["name"]), MAX_THREAD_NAME)
    elif kind == "bring_up":
        a["topic"] = text(args["topic"], TOPIC_MAX)
        # one place to bring it up: his channel, or one of the everyday ones by her guid
        a["channel"] = _channel(pic, member, args.get("channel")
                                or BRING_UP_CHANNELS[member % len(BRING_UP_CHANNELS)])
    elif kind == "dm_him":
        a["about"] = text(args["about"], ABOUT_MAX)
    elif kind == "react":
        from leeroy_mind.discord.text import is_one_emoji
        if not is_one_emoji(args["emoji"]):
            raise IntentRefused("emoji", "A reaction is one emoji.")
        row = store.conn.execute("SELECT author,channel,deleted FROM discord_message WHERE id=?",
                                 (args["message"],)).fetchone()
        if row is None or row[2] is not None:
            raise IntentRefused("message", "That message is not there.")
        if row[0] == member:
            raise IntentRefused("message", "She does not react to her own message.")
        from leeroy_mind.discord import layout
        if member not in layout.audience(row[1], cast=pic.cast, officers=_officers(pic),
                                         human=pic.human):
            raise IntentRefused("audience", "She cannot read that message.")
        a = {"message": args["message"], "emoji": args["emoji"]}
    with store.transaction():
        conn = store.conn
        open_ = conn.execute("SELECT count(*), sum(member=?) FROM godmode_intent "
                             "WHERE status='pending'", (member,)).fetchone()
        if (open_[1] or 0) >= PER_MEMBER or (open_[0] or 0) >= MAX_PENDING:
            raise IntentRefused("cap", f"At most {PER_MEMBER} waiting strings a member and "
                                       f"{MAX_PENDING} in all.")
        due = now + (GAME_DUE_S if kind in GAME_KINDS else DUE_S)
        ident = conn.execute("INSERT INTO godmode_intent(member,kind,args,status,created,due) "
                             "VALUES (?,?,?,'pending',?,?)",
                             (member, kind, json.dumps(a, ensure_ascii=False, sort_keys=True),
                              now, due)).lastrowid
    log_event("godmode_intent_created", id=ident, member=member, kind=kind)
    return ident


def settle(store, id, *, status, reason=None, now) -> bool:
    """Settle a pending intent once; False when it was settled already."""
    if status not in STATUSES[1:]:
        raise ValueError("an intent settles taken, not_taken, expired or cancelled")
    with store.transaction():
        done = store.conn.execute("UPDATE godmode_intent SET status=?,reason=?,settled=? "
                                  "WHERE id=? AND status='pending'",
                                  (status, reason, now, id)).rowcount == 1
    if done:
        row = get(store.conn, id)
        log_event("godmode_intent_settled", id=id, kind=row["kind"] if row else "?",
                  status=status, **({"reason": reason} if reason else {}))
    return done


def _tie(conn, a, b):
    from leeroy_mind.server.state import tie_kind
    return tie_kind(conn, a, b)


def fit(conn, intent, personas, *, now) -> tuple[bool, str]:
    """Whether she can still carry it out: (True, "ok"), or (False, a reason code) when she is
    away, gone, at feud with whom it touches, her stance makes it absurd, or it contradicts a
    claim of hers. Cheap SELECTs and lexical checks only; no model."""
    member, kind, a = intent["member"], intent["kind"], intent["args"]
    p = personas.get(member) if personas is not None else None
    if p is None:
        return False, "gone"
    if conn.execute("SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL "
                    "AND (returns_at IS NULL OR returns_at>?) LIMIT 1", (member, now)).fetchone():
        return False, "away"
    if kind == "react":
        row = conn.execute("SELECT author,deleted FROM discord_message WHERE id=?",
                           (a["message"],)).fetchone()
        if row is None or row[1] is not None:
            return False, "deleted"
        tie = _tie(conn, member, row[0])
        if tie == "feud":
            return False, "feud"
        if tie == "rival" and a["emoji"] in LOVE:
            return False, "stance"
        return True, "ok"
    human = int((conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone() or ["0"])[0] or 0)
    if kind == "dm_him" and human:
        rel = conn.execute("SELECT affinity FROM relationship WHERE a=? AND b=?",
                           (member, human)).fetchone()
        from leeroy_mind.tuning.edits import apply_feelings, feeling_overlay
        affinity = apply_feelings({"affinity": rel[0] if rel else 0.0},
                                  feeling_overlay(conn).get((member, human)))["affinity"]
        if affinity <= -30:
            return False, "stance"
    words = a.get("about") or a.get("topic") or ""
    reason = _about_people(conn, member, words, now)
    if reason:
        return False, reason
    if _contradicts(conn, member, words, now):
        return False, "claims"
    return True, "ok"


def _about_people(conn, member, text, now):
    """Warm words about someone she feuds with, or loving ones about a rival, are not hers."""
    from leeroy_mind.claims.stance import polarity
    if polarity(text) <= 0:
        return ""
    for guid, name in conn.execute("SELECT guid,name FROM cast_member WHERE guid<>?", (member,)):
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I):
            tie = _tie(conn, member, guid)
            if tie == "feud":
                return "feud"
            if tie == "rival":
                return "stance"
    return ""


def _contradicts(conn, member, text, now) -> bool:
    """A lexical reversal of one of her live claims (claims.stance), without a model."""
    if not text:
        return False
    try:
        from leeroy_mind.claims.stance import assertions, reversal, subject_matches
        from leeroy_mind.claims.store import live_claims
        claims = live_claims(conn, member, now=now)
    except Exception:
        return False
    for clause in assertions(text) or [text]:
        for claim in claims:
            if (subject_matches(claim.subject, claim.text, clause, 0.0)
                    and reversal(claim.kind, claim.text, clause, claim.polarity)):
                return True
    return False


def mentions(topic: str, text: str) -> bool:
    """Her line names the topic: one or more of its words (moves.topics) in it."""
    from leeroy_mind.talk.moves import topics
    words = topics(topic)
    folded = " ".join((text or "").casefold().split())
    return any(re.search(rf"(?<!\w){re.escape(w)}", folded) for w in words)


def instruction(intent) -> str:
    """Her own plan, in her terms: never an order and never his words as her line."""
    kind, a = intent["kind"], intent["args"]
    if kind == "post":
        return f"You'd meant to post here about {a['about']}."
    if kind == "thread":
        return f"You'd meant to start a thread here about {a['about']}."
    if kind == "bring_up":
        return f"You'd meant to bring up {a['topic']}."
    if kind == "dm_him":
        return f"You'd meant to message him about {a['about']}."
    return ""


def in_place(intent, cid, *, to_guid=0, human=0) -> bool:
    """Whether channel id `cid` is where she carries this intent out: a post and a bring_up in
    their channel (a bring_up in her DM with him too), a dm_him in her DM with him."""
    kind, a, guid = intent["kind"], intent["args"], intent["member"]
    dm = cid == f"dm:{guid}" and to_guid in (0, human)
    if kind == "post":
        return a["channel"] == cid
    if kind == "bring_up":
        return a.get("channel") == cid or dm
    return kind == "dm_him" and dm


def card_lines(conn, guid, channel, *, to_guid=0, human=0) -> list[str]:
    """Her pending intents as her own plans ("You meant to ..."), only where she would carry
    them out (in_place). Never in a group DM, never a thread or a reaction."""
    from leeroy_mind.discord.channels import channel_id, is_discord, is_group
    if not is_discord(channel) or is_group(channel):
        return []
    cid = channel_id(channel)
    out = []
    for i in pending(conn, guid):
        kind, a = i["kind"], i["args"]
        if not in_place(i, cid, to_guid=to_guid, human=human):
            continue
        if kind == "post":
            out.append(f"You meant to post here about {a['about']}.")
        elif kind == "bring_up":
            out.append(f"You meant to bring up {a['topic']}.")
        elif kind == "dm_him":
            out.append(f"You meant to message him about {a['about']}.")
    return out[:2]


def tries(conn, ident) -> tuple[int, float]:
    """(attempts that came to nothing, the earliest time to try again)."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (TRIES_KEY.format(id=ident),)).fetchone()
    try:
        v = json.loads(row[0]) if row else {}
        return int(v.get("n", 0)), float(v.get("next", 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0, 0.0


def may_try(conn, ident, now) -> bool:
    return now >= tries(conn, ident)[1]


def tried(store, ident, *, now, reason="dropped") -> bool:
    """One attempt came to nothing: back off (BACKOFF_S, doubling), and after MAX_TRIES settle
    it not taken with `reason`. True when it was settled now."""
    with store.transaction():
        n = tries(store.conn, ident)[0] + 1
        store.set_meta(TRIES_KEY.format(id=ident),
                       json.dumps({"n": n, "next": now + BACKOFF_S * 2 ** (n - 1)}))
    if n >= MAX_TRIES:
        return settle(store, ident, status="not_taken", reason=reason, now=now)
    return False


def seed_rows(conn, *, now):
    """(id, member, kind, args) of the pending intents the server engine opens (post, thread,
    bring_up), oldest first."""
    return [(i["id"], i["member"], i["kind"], i["args"]) for i in pending(conn)
            if i["kind"] in ("post", "thread", "bring_up") and i["due"] > now
            and may_try(conn, i["id"], now)]
