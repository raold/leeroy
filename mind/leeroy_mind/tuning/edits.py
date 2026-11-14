"""Godmode edits: the owner's changes to a member's mind, as tuning overlays with receipts in the
same change log as dials and nudges (D6 Part A, tasks 1 and 2).

Storage follows nudges: ``meta`` holds each edit's current projection under
``tuning:edit:value:<id>`` and append-only before/after receipts under
``tuning:edit:change:<change>``, both written in the transaction of their ``tuning_change`` row.
An edit's id is the change that made it. ``changes.undo`` and ``changes.revert_to`` fold these
receipts in, so ``tuning undo N`` and ``revert N`` cover every kind.

Two families:

- overlays (trait, feeling, mood, hangouts, skill, activities) are a lens, never a write: the
  stored rows stay the night's truth, read by every writer and the night. Talk-facing readers
  alone see it: traits, hangouts, skill and activities on the effective persona for talk
  (tuning.effective), feelings and moods through Store.get_relationship/get_state(lens=True)
  and the raw readers (feeling_overlay, mood_overlay). Dropping the overlay is the whole undo,
  and leaves what the night would have produced without it.
- table edits (life_event, memory_implant, memory_erase, feud_start, feud_end) write through the
  tables the night owns, and their projection keeps what undo needs to put them back:
  an implanted memory is rejected, an erased one gets its old status back, a feud start is
  removed (or ended when the night already cited it), a feud end reopens it, a life event's
  thread is removed (or resolved when it already has beats, their memories archived) and the
  thread it replaced is active again.

Content rules hold: the age, looks and name paths are never edited, text passes the board
cleaning, the real-name screen, the explicit screens and the romance-between-members screen,
and a trait value must still make a valid persona. Nothing here logs text or calls a model."""
import json
import logging
import random
from dataclasses import dataclass, field
from types import MappingProxyType

from leeroy_mind.storage.rows import REL_RANGES

log = logging.getLogger(__name__)
CURRENT = "tuning:edit:value:"
RECEIPT = "tuning:edit:change:"
EDIT_KINDS = ("trait", "feeling", "mood", "life_event", "memory_implant", "memory_erase",
              "feud_start", "feud_end")
BEHAVIOUR_KINDS = ("hangouts", "skill", "activities")
KINDS = EDIT_KINDS + BEHAVIOUR_KINDS
OVERLAY_KINDS = frozenset({"trait", "feeling", "mood", "hangouts", "skill", "activities"})
PERSONA_KINDS = frozenset({"trait", "hangouts", "skill", "activities"})
MAX_ACTIVE = 128
MOOD_SECONDS = (3600, 14 * 86400)
FEELING_DIMS = ("affinity", "trust", "attraction")
OFFSET_LIMIT = {"affinity": 200.0, "trust": 100.0, "attraction": 100.0}
SKILLS = {"star": "clutch", "middle": "solid", "fire": "sloppy"}
HANGOUT_MAX = 3
ACTIVITIES = ("city", "inn", "fishing", "gathering", "camp", "duel_practice", "holiday",
              "offline", "questing")
MEMORY_KINDS = ("event", "observation", "story", "reflection", "conversation", "plan")
SCOPES = ("open", "private", "secret")
TITLE_MAX = 80
MEMORY_MAX = 200
_TRAITS = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")
# The persona paths a trait edit may change: personality, interests and habits. Never the
# identity (age, name), the looks, the photo, the romance stance or the character's name.
TRAIT_PATHS = frozenset({
    *(f"big_five.{t}.score" for t in _TRAITS), *(f"big_five.{t}.behaviour" for t in _TRAITS),
    "attachment", "humour", "likes", "dislikes", "values", "opinions", "speech_habits",
    "habits", "game.wants", "game.motivation", "game.thinks_as", "game.favourite_spot"})
_REFUSED_PATHS = (("identity.age", "age"), ("identity.real_name", "name"),
                  ("character_name", "name"), ("appearance", "looks"), ("photo", "looks"),
                  ("identity", "identity"), ("romance", "romance"))
_PATH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_.")


class EditRefused(ValueError):
    """A refusal with a code (logs and tests) and words (the console)."""

    def __init__(self, code: str, words: str):
        super().__init__(words)
        self.code = code


def _tune_error(code, words):
    from .changes import TuneError
    err = TuneError(words)
    err.code = code
    return err


# -- the shape check (the endpoint's) ------------------------------------------------------------

def _int_id(v) -> bool:
    return type(v) is int and 0 < v < 1 << 32


def _num(v, lo, hi) -> bool:
    return type(v) in (int, float) and v == v and lo <= v <= hi


def _texts(v, n, cap) -> bool:
    return isinstance(v, list) and len(v) <= n and all(isinstance(x, str) and len(x) <= cap for x in v)


def _kind_args(kind, a) -> dict:
    bad = ValueError(f"A {kind.replace('_', ' ')} edit's arguments are not right.")
    if not isinstance(a, dict) or not _int_id(a.get("member")):
        raise ValueError("An edit names a member by guid.")
    keys = set(a)

    def exact(required, optional=()):
        if not set(required) <= keys <= set(required) | set(optional):
            raise bad

    if kind == "trait":
        exact({"member", "path", "value"})
        p = a["path"]
        if not (isinstance(p, str) and 1 <= len(p) <= 60 and set(p) <= _PATH_CHARS):
            raise bad
        if len(json.dumps(a["value"])) > 2000:
            raise bad
    elif kind == "feeling":
        exact({"member", "other"}, set(FEELING_DIMS) | {"romance_stage"})
        if not _int_id(a["other"]) or not keys & (set(FEELING_DIMS) | {"romance_stage"}):
            raise bad
        if any(not _num(a[d], -OFFSET_LIMIT[d], OFFSET_LIMIT[d]) for d in FEELING_DIMS if d in a):
            raise bad
        if "romance_stage" in a and not isinstance(a["romance_stage"], str):
            raise bad
    elif kind == "mood":
        exact({"member", "offset", "seconds"})
        if not _num(a["offset"], -1, 1) or type(a["seconds"]) is not int \
                or not MOOD_SECONDS[0] <= a["seconds"] <= MOOD_SECONDS[1]:
            raise ValueError("A mood edit moves mood by -1 to 1 for 1 hour to 14 days.")
    elif kind == "life_event":
        exact({"member", "title"})
        if not isinstance(a["title"], str):
            raise bad
    elif kind == "memory_implant":
        exact({"member", "text"}, {"kind", "importance", "valence", "days_ago", "actors", "scope"})
        if (not isinstance(a["text"], str) or a.get("kind", "event") not in MEMORY_KINDS
                or type(a.get("importance", 6)) is not int or not 1 <= a.get("importance", 6) <= 10
                or not _num(a.get("valence", 0.0), -1, 1)
                or type(a.get("days_ago", 0)) is not int or not 0 <= a.get("days_ago", 0) <= 3650
                or not isinstance(a.get("actors", []), list) or len(a.get("actors", [])) > 8
                or not all(_int_id(g) for g in a.get("actors", []))
                or a.get("scope", "open") not in SCOPES):
            raise bad
    elif kind == "memory_erase":
        exact({"member", "memory"})
        if not _int_id(a["memory"]):
            raise bad
    elif kind in ("feud_start", "feud_end"):
        exact({"member", "other"})
        if not _int_id(a["other"]) or a["other"] == a["member"]:
            raise bad
    elif kind == "hangouts":
        exact({"member"}, {"channels", "rooms"})
        for name in ("channels", "rooms"):
            v = a.get(name, {})
            if (not isinstance(v, dict) or len(v) > 64
                    or not all(isinstance(k, str) and len(k) <= 40 and type(w) is int
                               and 0 <= w <= HANGOUT_MAX for k, w in v.items())):
                raise ValueError("Hangouts weigh channels and rooms 0 (avoids) to 3.")
        if not a.get("channels") and not a.get("rooms"):
            raise bad
    elif kind == "skill":
        exact({"member", "skill"})
        if a["skill"] not in SKILLS:
            raise ValueError("Skill is star, middle or fire.")
    elif kind == "activities":
        exact({"member"}, {"activities", "with"})
        acts, mates = a.get("activities", []), a.get("with", [])
        if (not isinstance(acts, list) or len(acts) > len(ACTIVITIES)
                or any(x not in ACTIVITIES for x in acts) or not _texts(acts, 9, 20)
                or not isinstance(mates, list) or len(mates) > 6
                or not all(_int_id(g) for g in mates) or not (acts or mates)):
            raise ValueError("Activities come from the day-plan list; partners are up to 6 guids.")
    return dict(a)


def parse_args(args: dict) -> dict:
    """The endpoint's exact shape check for tune action "edit"; refusal words never echo
    submitted text."""
    verb = args.get("verb")
    common = {"action", "verb"}
    if verb in ("set", "preview"):
        if set(args) != common | {"kind", "args"} or args.get("kind") not in KINDS:
            raise ValueError("An edit needs a kind (" + ", ".join(KINDS) + ") and its arguments.")
        return {"action": "edit", "verb": verb, "kind": args["kind"],
                "args": _kind_args(args["kind"], args["args"])}
    if verb == "retire" and set(args) == common | {"id"} and _int_id(args["id"]):
        return dict(args)
    if verb == "list" and set(args) == common:
        return dict(args)
    raise ValueError("Edit actions are set, retire, list and preview; choose a valid edit id.")


# -- the content screens -------------------------------------------------------------------------

def screen_text(text, *, cap, pic, human, ages=True):
    """The cleaned text, or EditRefused: board cleaning and length, real names, explicit
    wording, romance between members and, with `ages`, anything that codes someone as young
    (a romance near such words always). Never echoes the text."""
    from leeroy_mind.board import text_problem
    from leeroy_mind.personas.screens import ROMANCE_RE, check_romance, has_word, names_real_name
    from leeroy_mind.selfies.kind import EXPLICIT_RE, WEAR_RE
    from leeroy_mind.selfies.scene import SEXUAL_RE
    from leeroy_mind.selfies.style import AGE_RE
    if not isinstance(text, str):
        raise EditRefused("text", "Text is required.")
    problem, cleaned = text_problem("post", text)
    if problem:
        raise EditRefused("text", problem)
    if len(cleaned) > cap:
        raise EditRefused("text", f"Keep it to {cap} characters.")
    names = [getattr(human, "real_name", ""), *(p.identity.real_name for p in pic.personas.values())]
    if (human is not None and names_real_name(cleaned, human)) or any(
            has_word(cleaned, n) for n in names if n):
        raise EditRefused("real_name", "Use character names or nicknames, not real names.")
    if EXPLICIT_RE.search(cleaned) or WEAR_RE.search(cleaned) or SEXUAL_RE.search(cleaned):
        raise EditRefused("explicit", "Edits stay non-explicit; explicit wording lives only in "
                                      "your own prompt overlays.")
    if AGE_RE.search(cleaned) and (ages or ROMANCE_RE.search(cleaned)):
        raise EditRefused("age", "Nothing may make anyone read as young.")
    if human is not None:
        characters = [m.name for g, m in pic.cast.items() if g != pic.human]
        try:
            check_romance({"edit": cleaned}, human, (), characters=characters)
        except ValueError:
            raise EditRefused("romance_members", "Romance aims only at your character, never "
                                                 "between members.") from None
    return cleaned


def _cast_member(pic, guid, words="Choose a cast member with a stored persona, other than you."):
    if guid == pic.human or guid not in pic.cast or guid not in pic.personas:
        raise EditRefused("member", words)
    return pic.personas[guid]


def _woman(p) -> bool:
    return p.identity.player_gender == "female"


def _trait_value(pic, human, member, path, value):
    """The value as the persona will read it, or EditRefused."""
    from leeroy_mind.personas.schema import (PersonaError, persona_from_dict, persona_to_dict,
                                             validate_persona)
    if path not in TRAIT_PATHS:
        for prefix, code in _REFUSED_PATHS:
            if path == prefix or path.startswith(prefix + "."):
                raise EditRefused(code, "Age, looks, names, identity and romance stance are never "
                                        "edited.")
        raise EditRefused("path", "That part of a persona is not editable here.")
    p = _cast_member(pic, member)
    value = _cleaned(value, lambda s: screen_text(s, cap=300, pic=pic, human=human))
    data = persona_to_dict(p)
    node = data
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    try:
        q = persona_from_dict(data)
    except (PersonaError, ValueError, TypeError, KeyError):
        raise EditRefused("invalid", "That value does not fit this part of a persona.") from None
    if validate_persona(q):
        raise EditRefused("invalid", "That value is out of range for this part of a persona.")
    return _get(persona_to_dict(q), keys)


def _cleaned(v, clean):
    """`v` with every string screened and replaced by its cleaned form (Board cleaning)."""
    if isinstance(v, str):
        return clean(v)
    if isinstance(v, dict):
        return {k: _cleaned(x, clean) for k, x in v.items()}
    if isinstance(v, list):
        return [_cleaned(x, clean) for x in v]
    return v


def _get(d, keys):
    for k in keys:
        d = d[k]
    return d


def checked(kind, a, *, pic, human, conn) -> dict:
    """The normalized arguments for `kind`, or EditRefused. Reads the live picture (cast,
    stored personas, the owner's guid) and, for the table kinds, the rows they need."""
    member = a["member"]
    if kind in ("feud_start", "feud_end"):
        for g in (member, a["other"]):
            _cast_member(pic, g, "A feud is between two cast members, never you.")
        return {"member": min(member, a["other"]), "other": max(member, a["other"])}
    p = _cast_member(pic, member)
    if kind == "trait":
        return {"member": member, "path": a["path"],
                "value": _trait_value(pic, human, member, a["path"], a["value"])}
    if kind == "feeling":
        other = a["other"]
        if other != pic.human:
            _cast_member(pic, other, "Feelings are toward you or another cast member.")
        out = {"member": member, "other": other}
        out.update({d: float(a[d]) for d in FEELING_DIMS if d in a and a[d]})
        romance = "attraction" in out or "romance_stage" in a
        if romance and other != pic.human:
            raise EditRefused("romance_members", "Romance aims only at your character, never "
                                                 "between members.")
        if romance and not _woman(p):
            raise EditRefused("men_friends", "Men stay friends: no attraction or romance stage.")
        if "romance_stage" in a:
            from leeroy_mind.arc.ladder import FOND_CAP, FULL_ARC, GATED
            stage = a["romance_stage"]
            stance = p.romance.stance
            if stage not in GATED:
                raise EditRefused("ladder", "A romance stage is one on the ladder, none to planning.")
            if stance == "none" and stage != "none" or (
                    stance not in FULL_ARC and GATED.index(stage) > GATED.index(FOND_CAP)):
                raise EditRefused("stance", "Her attitude toward you does not reach that stage.")
            out["romance_stage"] = stage
        if len(out) == 2:
            raise EditRefused("empty", "Nothing to change.")
        return out
    if kind == "mood":
        return {"member": member, "offset": float(a["offset"]), "seconds": a["seconds"]}
    if kind == "life_event":
        return {"member": member,
                "title": screen_text(a["title"], cap=TITLE_MAX, pic=pic, human=human, ages=False)}
    if kind == "memory_implant":
        actors = sorted(set(a.get("actors", [])))
        if any(g != pic.human and g not in pic.cast for g in actors):
            raise EditRefused("member", "A memory's people are you or cast members.")
        return {"member": member,
                "text": screen_text(a["text"], cap=MEMORY_MAX, pic=pic, human=human, ages=False),
                "kind": a.get("kind", "event"), "importance": a.get("importance", 6),
                "valence": float(a.get("valence", 0.0)), "days_ago": a.get("days_ago", 0),
                "actors": actors, "scope": a.get("scope", "open")}
    if kind == "memory_erase":
        row = conn.execute("SELECT owner,status FROM memory WHERE id=?", (a["memory"],)).fetchone()
        if row is None or row[0] != member:
            raise EditRefused("memory", "That memory is not one of this member's.")
        if row[1] != "active":
            raise EditRefused("memory", "Only an active memory can be erased.")
        return {"member": member, "memory": a["memory"]}
    if kind == "hangouts":
        from leeroy_mind.discord import layout
        from leeroy_mind.voice.rooms import BY_ID
        channels = a.get("channels", {})
        rooms = a.get("rooms", {})
        if any(layout.channel(c) is None or layout.channel(c).category == "info" for c in channels):
            raise EditRefused("channel", "Choose the server's own conversation channels.")
        if any(r not in BY_ID for r in rooms):
            raise EditRefused("room", "Choose existing voice rooms.")
        return {"member": member, "channels": dict(sorted(channels.items())),
                "rooms": dict(sorted(rooms.items()))}
    if kind == "skill":
        return {"member": member, "skill": a["skill"]}
    if kind == "activities":
        mates = sorted(set(a.get("with", [])))
        if any(g == member or g not in pic.cast or g == pic.human for g in mates):
            raise EditRefused("member", "Partners are other cast members.")
        return {"member": member, "activities": sorted(set(a.get("activities", []))),
                "with": mates}
    raise EditRefused("kind", "Unknown edit kind.")


# -- projections and receipts --------------------------------------------------------------------

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _meta(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) "
                     "DO UPDATE SET value=excluded.value", (key, _json(value)))


def values(conn) -> dict[int, dict]:
    """Every edit's current projection, retired ones included (history and revert)."""
    out = {}
    for key, value in conn.execute("SELECT key,value FROM meta WHERE key GLOB ?",
                                   (CURRENT + "[0-9]*",)):
        try:
            out[int(key[len(CURRENT):])] = json.loads(value)
        except ValueError:
            continue
    return out


def receipt(conn, change) -> list:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (RECEIPT + str(change),)).fetchone()
    return [] if row is None else json.loads(row[0])


def live(proj) -> bool:
    """An edit whose effects stand (expiry aside)."""
    return proj is not None and proj.get("retired") is None


def in_force(proj, now) -> bool:
    return live(proj) and (proj.get("ends") is None or now is None or now < proj["ends"])


# -- table effects -------------------------------------------------------------------------------

@dataclass
class Ctx:
    """What table effects need. `lenient` (a revert or a retire): a table edit the night moved
    since is left as the night left it and counted in `moved`, instead of refusing everything.
    `going`: the edits this write turns off, so a later one going too never blocks."""
    store: object
    now: float
    human: int
    dial: int = 5
    ident: object = None
    lenient: bool = False
    moved: list = field(default_factory=list)
    going: set = field(default_factory=set)

    def changed(self, proj, words):
        if not self.lenient:
            raise _tune_error("changed", words)
        self.moved.append(proj["id"])


def _apply(ctx, proj) -> dict:
    """Make a table edit's change; returns what undo needs."""
    kind, a, conn = proj["kind"], proj["args"], ctx.store.conn
    if kind == "memory_implant":
        return {"memory": _implant(ctx, proj)}
    if kind == "memory_erase":
        row = conn.execute("SELECT status FROM memory WHERE id=?", (a["memory"],)).fetchone()
        if row is None or row[0] != "active":
            raise _tune_error("changed", "That memory is no longer active.")
        conn.execute("UPDATE memory SET status='archived' WHERE id=?", (a["memory"],))
        return {"status_before": row[0]}
    if kind == "feud_start":
        return _feud_start(ctx, a)
    if kind == "feud_end":
        return _feud_end(ctx, a)
    if kind == "life_event":
        return _life_event(ctx, proj)
    return {}


def _reverse(ctx, proj) -> None:
    """Undo a table edit's change exactly, or refuse in words when it moved since."""
    kind, a, fx, conn = proj["kind"], proj["args"], proj.get("effects") or {}, ctx.store.conn
    if kind == "memory_implant" and fx.get("memory"):
        conn.execute("UPDATE memory SET status='rejected' WHERE id=?", (fx["memory"],))
        _archive_derived(conn, fx["memory"])
    elif kind == "memory_erase":
        row = conn.execute("SELECT status FROM memory WHERE id=?", (a["memory"],)).fetchone()
        if row is not None and row[0] != "archived":
            return ctx.changed(proj, "That memory changed since it was erased: undo the later "
                                     "change first, or revert.")
        if row is not None:
            conn.execute("UPDATE memory SET status=? WHERE id=?", (fx.get("status_before", "active"),
                                                                   a["memory"]))
    elif kind == "feud_start" and fx.get("feud"):
        row = conn.execute("SELECT ended FROM feud WHERE id=?", (fx["feud"],)).fetchone()
        if row is not None and row[0] is None:
            if _feud_cited(conn, fx["feud"]):
                conn.execute("UPDATE feud SET ended=? WHERE id=?", (ctx.now, fx["feud"]))
            else:
                conn.execute("DELETE FROM feud WHERE id=?", (fx["feud"],))
        _restore_arc(conn, a, fx.get("arc"), mine=fx.get("arc_left"))
    elif kind == "feud_end" and fx.get("feud"):
        row = conn.execute("SELECT ended FROM feud WHERE id=?", (fx["feud"],)).fetchone()
        other = conn.execute("SELECT 1 FROM feud WHERE a=? AND b=? AND ended IS NULL",
                             (a["member"], a["other"])).fetchone()
        if row is None or row[0] != fx.get("ended") or other is not None:
            return ctx.changed(proj, "That pair's feud changed since: undo the later change "
                                     "first, or revert.")
        conn.execute("UPDATE feud SET ended=NULL WHERE id=?", (fx["feud"],))
        _restore_arc(conn, a, fx.get("arc"), mine=fx.get("arc_left"))
    elif kind == "life_event" and fx.get("thread"):
        _undo_life(ctx, proj)


def _implant(ctx, proj) -> int:
    from leeroy_mind.memory.privacy import derive_scope, with_request
    from leeroy_mind.storage.rows import MemoryRow
    a, store = proj["args"], ctx.store
    key = f"owner:godmode:{proj['id']}"
    evidence = (key,)
    when = int(ctx.now) - a["days_ago"] * 86400
    scope_with = ctx.human if a["scope"] != "open" else 0
    derived = derive_scope(store.conn, a["member"], evidence)
    scope, scope_with = with_request(derived, a["scope"], scope_with)
    row = MemoryRow(id=None, owner=a["member"], kind=a["kind"], text=a["text"], t_event=when,
                    t_created=int(ctx.now), t_accessed=int(ctx.now), access_n=0,
                    actors=tuple(a["actors"]), place=None, boss_id=None, raid_id=None,
                    importance=a["importance"], valence=a["valence"], source="owner",
                    evidence=evidence, key=key, scope=scope, scope_with=scope_with)
    return store.upsert_keyed_memory(row)


DERIVED_MAX = 500


def _archive_derived(conn, memory):
    """Archive the active memories built on `memory` (citing memory:<id>, at any depth up to
    DERIVED_MAX), so nothing she recalls still rests on an implant taken back."""
    frontier, seen = [memory], {memory}
    while frontier and len(seen) < DERIVED_MAX:
        refs = json.dumps([f"memory:{m}" for m in frontier])
        found = [r[0] for r in conn.execute(
            "SELECT DISTINCT m.id FROM memory m, json_each(m.evidence) e "
            "WHERE e.value IN (SELECT value FROM json_each(?))", (refs,))]
        frontier = [m for m in found if m not in seen][:DERIVED_MAX - len(seen)]
        seen.update(frontier)
    derived = sorted(seen - {memory})
    if derived:
        conn.execute("UPDATE memory SET status='archived' WHERE status='active' AND id IN "
                     "(SELECT value FROM json_each(?))", (json.dumps(derived),))


def _feud_cited(conn, feud) -> bool:
    ref = f"feud:{feud}"
    return conn.execute("SELECT 1 FROM memory m, json_each(m.evidence) e WHERE e.value=? LIMIT 1",
                        (ref,)).fetchone() is not None


def _arc(conn, a):
    from leeroy_mind.nightly.storyteller import arc_key
    key = arc_key("feud", a["member"], a["other"])
    row = conn.execute("SELECT id,tension,opened,last_beat,status FROM story_arc WHERE key=?",
                       (key,)).fetchone()
    return key, (None if row is None else list(row))


def _restore_arc(conn, a, before, *, mine):
    key, now_row = _arc(conn, a)
    if now_row is None or (mine is not None and now_row != mine):
        return  # the night moved the arc since: its story is its own
    if before is None:
        conn.execute("DELETE FROM story_arc WHERE key=?", (key,))
    else:
        conn.execute("UPDATE story_arc SET tension=?,opened=?,last_beat=?,status=? WHERE key=?",
                     (before[1], before[2], before[3], before[4], key))


def _feud_start(ctx, a) -> dict:
    conn = ctx.store.conn
    if conn.execute("SELECT 1 FROM feud WHERE a=? AND b=? AND ended IS NULL",
                    (a["member"], a["other"])).fetchone():
        raise EditRefused("feud_open", "They are already feuding.")
    key, before = _arc(conn, a)
    fid = conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (?,?,?,?,?)",
                       (a["member"], a["other"], ctx.now, "owner", "[]")).lastrowid
    conn.execute("INSERT INTO story_arc(kind,key,tension,opened,last_beat,status) "
                 "VALUES ('feud',?,0.5,?,?,'open') ON CONFLICT(key) DO UPDATE SET "
                 "tension=0.5,opened=excluded.opened,last_beat=excluded.last_beat,status='open'",
                 (key, ctx.now, ctx.now))
    return {"feud": fid, "arc": before, "arc_left": _arc(conn, a)[1]}


def _feud_end(ctx, a) -> dict:
    conn = ctx.store.conn
    row = conn.execute("SELECT id FROM feud WHERE a=? AND b=? AND ended IS NULL",
                       (a["member"], a["other"])).fetchone()
    if row is None:
        raise EditRefused("no_feud", "They are not feuding.")
    key, before = _arc(conn, a)
    conn.execute("UPDATE feud SET ended=? WHERE id=?", (ctx.now, row[0]))
    conn.execute("UPDATE story_arc SET status='resolved',last_beat=? WHERE key=? AND kind='feud'",
                 (ctx.now, key))
    return {"feud": row[0], "ended": ctx.now, "arc": before, "arc_left": _arc(conn, a)[1]}


def _life_event(ctx, proj) -> dict:
    from leeroy_mind.nightly.life import ensure_thread
    from leeroy_mind.nightly.wowlife import refresh_on_mind
    a, store = proj["args"], ctx.store
    conn, guid = store.conn, a["member"]
    state = store.get_state(guid)
    prior = [list(r) for r in conn.execute(
        "SELECT id,status,resolved FROM life_thread WHERE guid=? AND status='active' ORDER BY id",
        (guid,))]
    for tid, _, _ in prior:
        conn.execute("UPDATE life_thread SET status='resolved',resolved=? WHERE id=?",
                     (ctx.now, tid))
    tid = ensure_thread(store, guid, a["title"], ctx.now, random.Random(proj["id"]), dial=ctx.dial)
    refresh_on_mind(store, guid, dial=ctx.dial)
    left = store.get_state(guid)
    return {"thread": tid, "at": ctx.now, "prior": prior,
            "state_before": [state.life_thread, state.updated],
            "state_left": [left.life_thread, left.updated]}


def _undo_life(ctx, proj) -> None:
    from dataclasses import replace
    from leeroy_mind.nightly.wowlife import refresh_on_mind
    a, fx, store = proj["args"], proj["effects"], ctx.store
    conn, guid, tid = store.conn, a["member"], fx["thread"]
    for later, v in values(conn).items():
        if (later != proj["id"] and later not in ctx.going and live(v)
                and v.get("kind") == "life_event" and v["member"] == guid
                and any(p[0] == tid for p in (v.get("effects") or {}).get("prior", ()))):
            raise _tune_error("changed", f"A later life event (edit #{later}) replaced this one's "
                                         "thread: undo that first, or revert.")
    beats = [r[0] for r in conn.execute("SELECT id FROM life_beat WHERE thread_id=?", (tid,))]
    if beats:
        conn.execute("UPDATE life_thread SET status='resolved',resolved=coalesce(resolved,?) "
                     "WHERE id=?", (ctx.now, tid))
        refs = [f"beat:{b}" for b in beats]
        conn.execute("UPDATE memory SET status='archived' WHERE status='active' AND id IN "
                     "(SELECT m.id FROM memory m, json_each(m.evidence) e WHERE m.owner=? "
                     "AND e.value IN (SELECT value FROM json_each(?)))", (guid, json.dumps(refs)))
    else:
        conn.execute("DELETE FROM life_thread WHERE id=?", (tid,))
    if conn.execute("SELECT 1 FROM life_thread WHERE guid=? AND status='active'",
                    (guid,)).fetchone() is None:
        for pid, status, resolved in fx.get("prior", []):
            conn.execute("UPDATE life_thread SET status=?,resolved=? WHERE id=? AND guid=? "
                         "AND status='resolved' AND resolved=?",
                         (status, resolved, pid, guid, fx.get("at")))
    state = store.get_state(guid)
    if [state.life_thread, state.updated] == fx.get("state_left"):
        before = fx.get("state_before") or ["", 0]
        store.put_state(replace(state, life_thread=before[0], updated=before[1]))
    else:
        refresh_on_mind(store, guid, dial=ctx.dial)


# -- writing ------------------------------------------------------------------------------------

def write_moves(ctx, change, moves) -> list:
    """Apply moves (id, before, after): retirements and removals first, newest first, then
    (re)activations, oldest first; table effects run on each transition, and the receipt keeps
    the projections they left. Returns the moves as written."""
    if not moves:
        return []
    conn = ctx.store.conn
    off = sorted((m for m in moves if live(m["before"]) and not live(m["after"])),
                 key=lambda m: -m["id"])
    ctx.going = {m["id"] for m in off}
    on = sorted((m for m in moves if not live(m["before"]) and live(m["after"])),
                key=lambda m: m["id"])
    rest = [m for m in moves if m not in off and m not in on]
    out = []
    for m in off:
        if m["before"]["kind"] not in OVERLAY_KINDS:
            _reverse(ctx, m["before"])
        out.append(m)
    for m in on:
        after = dict(m["after"])
        if after["kind"] not in OVERLAY_KINDS:
            after["effects"] = _apply(ctx, after)
        out.append({**m, "after": after})
    out.extend(rest)
    out.sort(key=lambda m: m["id"])
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (RECEIPT + str(change), _json(out)))
    for m in out:
        _meta(conn, CURRENT + str(m["id"]), m["after"])
    return out


def undo_moves(conn, change) -> list:
    """The moves that put back what `change` did to edits; TuneError when one moved since."""
    current = values(conn)
    out = []
    for r in receipt(conn, change):
        if current.get(r["id"]) != r["after"]:
            raise _tune_error("changed", "That edit changed since: undo its later change first, "
                                         "or revert.")
        out.append({"id": r["id"], "before": r["after"], "after": r["before"]})
    return out


def revert_moves(conn, to_change) -> list:
    """The moves that bring every edit back to where it stood right after `to_change`."""
    then = {}
    for (value,) in conn.execute(
            "SELECT m.value FROM tuning_change c JOIN meta m ON m.key=? || c.id "
            "WHERE c.id<=? ORDER BY c.id", (RECEIPT, to_change)):
        for r in json.loads(value):
            then[r["id"]] = r["after"]
    current = values(conn)
    out = []
    for n in sorted(set(current) | set(then)):
        before, after = current.get(n), then.get(n)
        if live(before) and live(after):
            continue  # standing then and now: its rows are what they are
        if before != after:
            out.append({"id": n, "before": before, "after": after})
    return out


def _ctx(store, now, human, dial, ident):
    return Ctx(store, now, human, dial, ident)


def set_edit(store, args, *, human, source, now, pic=None, dial=5) -> dict:
    """Check one edit and write it as one tuning_change; returns its receipt (change, id, kind).
    `args` is the endpoint's {kind, args}; `human` the owner's HumanIdentity (the screens)."""
    from . import changes as C
    if pic is None:
        pic = _picture(store)
    with store.transaction():
        conn = store.conn
        kind = args["kind"]
        a = checked(kind, args["args"], pic=pic, human=human, conn=conn)
        if sum(in_force(v, now) for v in values(conn).values()) >= MAX_ACTIVE:
            raise EditRefused("cap", f"There are already {MAX_ACTIVE} edits in force. Retire one "
                                     "first.")
        change = C._new_change(conn, now, source, f"edit {kind} set")
        proj = {"id": change, "kind": kind, "member": a["member"], "args": a, "created": now,
                "ends": now + a["seconds"] if kind == "mood" else None, "retired": None,
                "author": "owner"}
        out = write_moves(_ctx(store, now, pic.human, dial, human), change,
                          [{"id": change, "before": None, "after": proj}])
        C._stamp(conn, change)
        return {"change": change, "id": change, "kind": kind,
                "effects": out[0]["after"].get("effects", {}), "rows": 1}


def retire(store, id, *, source, now, human=0, dial=5) -> dict:
    """Retire an edit: an overlay stops reaching her, a table edit is put back as undo would."""
    from . import changes as C
    with store.transaction():
        conn = store.conn
        before = values(conn).get(id)
        if before is None:
            raise EditRefused("missing", "There is no such edit.")
        if before["retired"] is not None:
            return {"change": None, "id": id, "kind": before["kind"], "rows": 0}
        change = C._new_change(conn, now, source, f"edit {before['kind']} retired")
        ctx = Ctx(store, now, human, dial, None, lenient=True)
        write_moves(ctx, change, [{"id": id, "before": before, "after": {**before, "retired": now}}])
        C._stamp(conn, change)
        return {"change": change, "id": id, "kind": before["kind"], "rows": 1,
                "moved": bool(ctx.moved)}


def preview(store, args, *, human, now, pic=None) -> dict:
    """A dry run: the checks and screens, and what the edit would touch, never written and
    never a model call."""
    if pic is None:
        pic = _picture(store)
    with store.transaction():
        a = checked(args["kind"], args["args"], pic=pic, human=human, conn=store.conn)
    touches = {"trait": "her persona as read (overlay)", "feeling": "her feelings (overlay)",
               "mood": "her mood (overlay)", "hangouts": "where she hangs out (overlay)",
               "skill": "her play style (overlay)", "activities": "her day plans (overlay)",
               "life_event": "a new life thread", "memory_implant": "a new memory",
               "memory_erase": "one memory archived", "feud_start": "a new feud",
               "feud_end": "an open feud ended"}[args["kind"]]
    return {"ok": True, "kind": args["kind"], "member": a["member"], "touches": touches}


def _picture(store):
    from leeroy_mind.personas.io import load_persona
    from . import changes as C
    return C.picture(store, {g: p for g in store.persona_versions()
                             if (p := load_persona(store, g)) is not None})


# -- reading the overlays -----------------------------------------------------------------------

@dataclass(frozen=True)
class MemberEdits:
    """A member's persona overlays in force: traits (path -> value, newest wins), skill
    (star/middle/fire), hangout weights and day-plan inputs."""
    traits: object = field(default_factory=lambda: MappingProxyType({}))
    skill: str | None = None
    channels: object = field(default_factory=lambda: MappingProxyType({}))
    rooms: object = field(default_factory=lambda: MappingProxyType({}))
    activities: tuple = ()
    partners: tuple = ()

    def channel_weight(self, channel: str) -> float:
        return float(self.channels.get(channel, 1))

    def room_weight(self, room: str) -> float:
        return float(self.rooms.get(room, 1))

    @property
    def empty(self) -> bool:
        return not (self.traits or self.skill or self.channels or self.rooms or self.activities
                    or self.partners)


NO_EDITS = MemberEdits()


def load(conn, now=None) -> tuple:
    """The persona-overlay edits in force (trait, hangouts, skill, activities), oldest first."""
    found = [kv for kind in sorted(PERSONA_KINDS) for kv in _live_of(conn, kind)]
    return tuple(v for _, v in sorted(found, key=lambda kv: kv[0]) if in_force(v, now))


def member_edits(edits, guid) -> MemberEdits:
    traits, skill, channels, rooms, acts, mates = {}, None, {}, {}, (), ()
    for e in edits:
        if e["member"] != guid:
            continue
        a = e["args"]
        if e["kind"] == "trait":
            traits[a["path"]] = a["value"]
        elif e["kind"] == "skill":
            skill = a["skill"]
        elif e["kind"] == "hangouts":
            channels.update(a.get("channels", {}))
            rooms.update(a.get("rooms", {}))
        elif e["kind"] == "activities":
            acts, mates = tuple(a.get("activities", ())), tuple(a.get("with", ()))
    if not (traits or skill or channels or rooms or acts or mates):
        return NO_EDITS
    return MemberEdits(MappingProxyType(traits), skill, MappingProxyType(channels),
                       MappingProxyType(rooms), acts, mates)


def trait_overlay(conn, guid, now=None) -> dict:
    """Persona path -> value for the member's trait edits in force (whitelisted paths only)."""
    return dict(member_edits(load(conn, now), guid).traits)


_MALFORMED: set[str] = set()


def _live_of(conn, kind):
    """The unretired projections of one kind, oldest first. SQLite filters by kind and
    retirement, so a lens reader parses only what can be in force; a malformed projection is
    skipped and logged once (by id), never failing the read."""
    try:
        rows = conn.execute(
            "SELECT key,value FROM meta WHERE key GLOB ? AND json_valid(value) "
            "AND json_extract(value,'$.kind')=? AND json_extract(value,'$.retired') IS NULL",
            (CURRENT + "[0-9]*", kind)).fetchall()
    except Exception:
        return []
    out = []
    for key, value in rows:
        try:
            v = json.loads(value)
            int(v["member"]), v["args"]["member"]
            out.append((int(key[len(CURRENT):]), v))
        except (ValueError, KeyError, TypeError):
            if key not in _MALFORMED:
                _MALFORMED.add(key)
                log.warning("godmode_edit_malformed key=%s", key[len(CURRENT):][:12])
    return sorted(out, key=lambda kv: kv[0])


def feeling_overlay(conn, now=None) -> dict:
    """(a, b) -> offsets on affinity, trust and attraction (summed over the edits in force), and
    "romance_stage" from the newest edit that sets one. A lens: only talk-facing readers fold it
    in (Store.get_relationship(lens=True), tie_kind, tease, the server snapshot, the console)."""
    out = {}
    for _, v in _live_of(conn, "feeling"):
        try:
            a = v["args"]
            row = out.setdefault((int(a["member"]), int(a["other"])), {})
            for d in FEELING_DIMS:
                if d in a:
                    row[d] = row.get(d, 0.0) + float(a[d])
            if "romance_stage" in a:
                row["romance_stage"] = str(a["romance_stage"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def mood_parts(conn) -> dict:
    """guid -> ((offset, ends), ...) for every unretired mood edit; each counts only before its
    own end."""
    out = {}
    for _, v in _live_of(conn, "mood"):
        try:
            out.setdefault(int(v["member"]), []).append((float(v["args"]["offset"]),
                                                          float(v["ends"])))
        except (KeyError, TypeError, ValueError):
            continue
    return {g: tuple(parts) for g, parts in out.items()}


def mood_overlay(conn, guid, now) -> float:
    """The member's mood offset in force now: the sum over her mood edits not yet ended
    (unclamped; fold_mood clamps the folded mood once); 0.0 when none."""
    return sum(off for off, ends in mood_parts(conn).get(guid, ()) if now < ends)


def fold_mood(stored: float, offset: float) -> float:
    """Her mood through the lens: the stored mood plus the offset in force, clamped to [-1, 1]
    once. Every lens reader folds with this (Store.get_state(lens=True), culture.tease)."""
    return max(-1.0, min(1.0, stored + offset))


def feeling(offsets, base: float, dim: str) -> float:
    lo, hi = REL_RANGES[dim]
    return max(lo, min(hi, base + offsets.get(dim, 0.0)))


def apply_feelings(row: dict, offsets: dict | None) -> dict:
    """A raw relationship row (dict) with an overlay's offsets folded in."""
    if not offsets:
        return row
    out = dict(row)
    for d in FEELING_DIMS:
        if d in offsets:
            out[d] = feeling(offsets, float(row.get(d, 0.0)), d)
    if "romance_stage" in offsets:
        out["romance_stage"] = offsets["romance_stage"]
    return out


# -- what the page and the CLI show --------------------------------------------------------------

def _state(proj):
    return "none" if proj is None else "retired" if proj.get("retired") is not None else "active"


def summary(conn, change) -> list:
    """A change's edit moves without text: id, kind, member, other and the state move."""
    out = []
    for r in receipt(conn, change):
        p = r["after"] or r["before"] or {}
        out.append({"id": r["id"], "kind": p.get("kind"), "member": p.get("member"),
                    "other": (p.get("args") or {}).get("other"),
                    "from": _state(r["before"]), "to": _state(r["after"])})
    return out


def view(conn, *, now, member=None, limit=30) -> dict:
    current = values(conn)
    active = [v for v in current.values() if in_force(v, now)
              and (member is None or v["member"] == member
                   or (v["args"].get("other") == member))]
    history = []
    for row in conn.execute("SELECT c.id,c.ts,c.label,c.undoes FROM tuning_change c "
                            "JOIN meta m ON m.key=? || c.id ORDER BY c.id DESC LIMIT ?",
                            (RECEIPT, limit)):
        history.append({"change": row[0], "ts": row[1], "label": row[2], "undoes": row[3],
                        "edits": summary(conn, row[0])})
    return {"active": sorted(active, key=lambda v: v["id"], reverse=True), "history": history}
