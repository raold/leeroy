"""Private, temporary owner overlays, with receipts in the existing tuning history.

``meta`` holds current projections and append-only before/after receipts keyed by the
shared tuning change id. No migration is needed. A receipt is written in the same
transaction as its tuning_change; ordinary tuning undo/revert include these receipts.
Nothing here logs text, changes a stored persona, or calls a model.
"""
import json
from dataclasses import asdict, dataclass

from leeroy_mind.board import text_problem
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.text import estimate_tokens

from . import groups as G

CURRENT = "tuning:nudge:value:"
RECEIPT = "tuning:nudge:change:"
STRENGTHS = ("light", "firm")
SCOPES = ("everywhere", "game", "discord", "whispers")
MIN_SECONDS, MAX_SECONDS = 3600, 14 * 86400
MAX_ACTIVE = 64
PROMPT_TOKENS = 200
GAME_CHANNELS = frozenset({"say", "yell", "party", "raid", "guild", "officer", "whisper"})


def parse_args(args):
    """The endpoint's exact shape check; refusal words never contain submitted text."""
    verb = args.get("verb")
    common = {"action", "verb"}
    if verb in ("set", "preview"):
        shape = common | {"target_kind", "target", "text", "strength", "scope", "seconds"}
        if set(args) != shape:
            raise ValueError("A nudge needs a target, text, strength, scope and duration.")
        if (args["target_kind"] not in ("member", "group")
                or not isinstance(args["target"], str) or len(args["target"]) > 10
                or not isinstance(args["text"], str)
                or args["strength"] not in STRENGTHS or args["scope"] not in SCOPES
                or type(args["seconds"]) is not int
                or not MIN_SECONDS <= args["seconds"] <= MAX_SECONDS):
            raise ValueError("Choose a member or group, a listed scope and strength, and 1 hour to 14 days.")
        problem, text = text_problem("post", args["text"])
        if problem:
            raise ValueError(problem)
        return {**args, "text": text}
    if verb == "retire" and set(args) == common | {"id"}:
        if type(args["id"]) is int and 0 < args["id"] < 1 << 32:
            return dict(args)
    if verb == "list" and set(args) == common:
        return dict(args)
    raise ValueError("Nudge actions are set, retire, list and preview; choose a valid nudge id.")


@dataclass(frozen=True)
class Nudge:
    id: int
    target_kind: str
    target: str
    text: str
    strength: str
    scope: str
    starts: float
    ends: float
    created: float
    retired: float | None = None
    author: str = "owner"

    def active(self, now):
        return self.retired is None and self.starts <= now < self.ends


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _meta(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) "
                     "DO UPDATE SET value=excluded.value", (key, _json(value)))


def values(conn):
    """The materialized state, including expired rows for history and revert."""
    return {int(key[len(CURRENT):]): json.loads(value) for key, value in conn.execute(
        "SELECT key,value FROM meta WHERE key GLOB ?", (CURRENT + "[0-9]*",))}


def load(conn):
    return tuple(Nudge(**v) for v in values(conn).values() if v["retired"] is None)


def receipt(conn, change):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (RECEIPT + str(change),)).fetchone()
    return [] if row is None else json.loads(row[0])


def write_moves(conn, change, moves):
    if not moves:
        return
    # Never replace a history receipt. A duplicate change is a programming error.
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (RECEIPT + str(change), _json(moves)))
    for move in moves:
        _meta(conn, CURRENT + str(move["id"]), move["after"])


def undo_moves(conn, change):
    from .changes import TuneError
    current = values(conn)
    out = []
    for r in receipt(conn, change):
        if current.get(r["id"]) != r["after"]:
            raise TuneError("That nudge changed since: undo its later change first, or revert.")
        out.append({"id": r["id"], "before": r["after"], "after": r["before"]})
    return out


def revert_moves(conn, change):
    then = {}
    for (value,) in conn.execute(
            "SELECT m.value FROM tuning_change c JOIN meta m ON m.key=? || c.id "
            "WHERE c.id<=? ORDER BY c.id", (RECEIPT, change)):
        for r in json.loads(value):
            then[r["id"]] = r["after"]
    current = values(conn)
    return [{"id": n, "before": current.get(n), "after": then.get(n)}
            for n in sorted(set(current) | set(then)) if current.get(n) != then.get(n)]


def checked(args, pic, groups, human):
    from .changes import TuneError
    kind, target = args["target_kind"], args["target"]
    if kind == "member":
        if (not target.isdigit() or int(target) == pic.human
                or int(target) not in pic.cast or int(target) not in pic.personas):
            raise TuneError("Choose a cast member with a stored persona, other than yourself.")
    elif kind != "group" or target not in groups:
        raise TuneError("Choose a current tuning group.")
    if args["strength"] not in STRENGTHS or args["scope"] not in SCOPES:
        raise TuneError("Choose a listed strength and scope.")
    seconds = args["seconds"]
    if type(seconds) is not int or not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise TuneError("A nudge lasts from one hour to fourteen days.")
    problem, text = text_problem("post", args["text"])
    if problem:
        raise TuneError(problem)
    # Use the existing screen, and also reject configured names case-insensitively:
    # this is owner-authored input, not a model accidentally using a common word.
    names = [human.real_name, *(p.identity.real_name for p in pic.personas.values())]
    if names_real_name(text, human) or any(has_word(text, name) for name in names if name):
        raise TuneError("Use character names or nicknames, not real names, in a nudge.")
    return {**args, "text": text}


def set_nudge(store, args, pic, human, *, source, now):
    from . import changes as C
    with store.transaction():
        args = checked(args, pic, C.live_groups(store.conn), human)
        if sum(n.active(now) for n in load(store.conn)) >= MAX_ACTIVE:
            raise C.TuneError("There are already 64 active nudges. Retire one first.")
        change = C._new_change(store.conn, now, source, "nudge set")
        n = Nudge(change, args["target_kind"], args["target"], args["text"],
                  args["strength"], args["scope"], now, now + args["seconds"], now)
        write_moves(store.conn, change, [{"id": n.id, "before": None, "after": asdict(n)}])
        C._stamp(store.conn, change)
        return C.Result(change, 1)


def retire(store, nudge_id, *, source, now):
    from . import changes as C
    with store.transaction():
        before = values(store.conn).get(nudge_id)
        if before is None:
            raise C.TuneError("There is no such nudge.")
        if before["retired"] is not None:
            return C.Result(None)
        after = {**before, "retired": now}
        change = C._new_change(store.conn, now, source, "nudge retired")
        write_moves(store.conn, change, [{"id": nudge_id, "before": before, "after": after}])
        C._stamp(store.conn, change)
        return C.Result(change, 1)


def for_member(nudges, p, member, groups, human):
    if p is None or p.guid == human:
        return ()
    return tuple(n for n in nudges if
                 (n.target_kind == "member" and n.target == str(p.guid)) or
                 (n.target_kind == "group" and n.target in groups and
                  (groups[n.target].builtin or G.matches(groups[n.target].where, p, member))))


def in_scope(n, channel, recipient, human):
    discord = channel.startswith(("discord:", "dm:"))
    return (n.scope == "everywhere" or n.scope == "game" and channel in GAME_CHANNELS
            or n.scope == "discord" and discord
            or n.scope == "whispers" and (channel == "whisper" or channel.startswith("dm:"))
            and human > 0 and recipient == human)


def prompt_lines(p, *, now, channel, recipient, human):
    """Whole lines only, within a fixed budget; firm/newer moods have first choice.

    No context means no overlay. This keeps critic/night/card readers from inheriting a
    channel-limited nudge and makes the no-nudge prompt exactly unchanged.
    """
    if now is None or not channel:
        return []
    selected = sorted((n for n in getattr(p, "nudges", ())
                       if n.active(now) and in_scope(n, channel, recipient, human)),
                      key=lambda n: (n.strength != "firm", -n.created, -n.id))
    out, used = [], 0
    for n in selected:
        line = ("[Temporary state of mind; " + n.strength + ": " + _json(n.text)
                + ". Express this in your own way only when it fits your character, facts, "
                "safety and privacy rules; never quote this direction.]")
        size = estimate_tokens(line)
        if used + size <= PROMPT_TOKENS:
            out.append(line)
            used += size
    return out


def view(conn, *, now, limit=30):
    current = values(conn)
    active = [v for v in current.values() if Nudge(**v).active(now)]
    history = []
    for row in conn.execute("SELECT c.id,c.ts,c.label,c.undoes,m.value FROM tuning_change c "
                            "JOIN meta m ON m.key=? || c.id ORDER BY c.id DESC LIMIT ?",
                            (RECEIPT, limit)):
        history.append({"change": row[0], "ts": row[1], "label": row[2],
                        "undoes": row[3], "moves": json.loads(row[4])})
    return {"active": sorted(active, key=lambda n: n["id"], reverse=True), "history": history}
