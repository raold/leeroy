"""Romantic threads, phase 1: the recorder (`[drama] threads = "record"`, owner 2026-11-17).

It records what would pull two members together and changes nothing. A *pull* is a directed
number a -> b (0-100) for a man and a woman of the cast that only recorded events move. Here
it is a shadow: `pull_shadow`, never `relationship.attraction`, and no member's prompt, card,
scene, relationship row or memory reads or is written from it.

- **Events** (`pull_event`, one row per fact with its kind, weight, factor, scope and evidence
  id): raids together with him (`raided`) and without him (`grouped_away`), standing at a kill
  (`kill_alive`, `first_kill` for the guild's first of that boss) or still standing when a wipe
  was called (`wipe_stood`), a warm accepted scene (`warm_scene`), someone setting a rumour
  about her straight or siding with her in a feud (`took_side`), being agreed with by name in
  chat (`heard`), someone answering a taunt aimed at her (`defended`), a line of his she
  reacted to in a Discord conversation both spoke in (`banter`), a late voice room together
  (`voice_late`), a close DKP award (`dkp_close`, weight 0: an outcome) and respect after one
  (`dkp_respect`, once per pair and award).
- **Cost, not ban.** The gains of a member taken at home (`couples.taken`) are multiplied by
  TAKEN_FACTOR. A woman's gains are damped by her stance toward the owner (STANCE_FACTOR), and
  a thread of a woman whose arc with him is at late_nights or later is marked with the ceiling
  `acknowledged`.
- **Decay.** HALF_LIFE nights without an event halve a pull.
- **Would-be threads.** A pull of OPEN_PULL with affinity OPEN_AFFINITY would open a thread on
  a seeded draw (OPEN_P a night) when the caps allow (THREADS_MAX live, TAKEN_MAX with a taken
  member, one per member); the label (`shape`) and what kept a candidate out (`thread_why`) are
  stored with the shadow row. A would-be thread fades when its pull falls under FADE_PULL.
- **Privacy.** Every event carries the scope of its evidence (public, pair, restricted,
  private). A pull moves only by what its holder could have witnessed: a line on a channel
  whose stored audience leaves her out, or a restricted channel with no stored audience, counts
  for nothing, and whispers and DMs (which are with the owner) are never read. `public_pull`
  is the part public evidence alone would have built. Nothing here is shown to a member.

The stage and `record_night` write only `pull_event`, `pull_shadow` and `pull_night`, inside
the caller's transaction. Chat text is read to detect a reply and never stored or logged; logs
and rows carry ids, counts and codes.
"""
import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from leeroy_mind.discord.channels import (CHANNEL_PREFIX, DM_PREFIX, GROUP_PREFIX, is_private,
                                          is_restricted)
from leeroy_mind.logs import log
from leeroy_mind.storage.rows import ROMANCE_STAGES

STAGE = "pulls"
DAY_S = 86400.0
HALF_LIFE = 21.0
DECAY = 0.5 ** (1.0 / HALF_LIFE)
OPEN_PULL = 35.0
OPEN_AFFINITY = 20.0
OPEN_P = 0.25
FADE_PULL = 10.0
MUTUAL_PULL = 25.0   # the other's pull from which it is mutual
TRIANGLE_PULL = 20.0  # a third member's pull toward either that makes a triangle
SLOW_NIGHTS = 20     # nights from the first event to the opening that make a slow burn
THREADS_MAX = 3      # live threads guild-wide (phase 2 starts here)
TAKEN_MAX = 1        # of those, threads with a taken member
NIGHT_CAP = 8.0      # what one directed pair's pull can take in a night
TAKEN_FACTOR = 0.6
STANCE_FACTOR = {"fond": 0.8, "drawn": 0.5, "lust": 0.3}
EX_COOLDOWN_DAYS = 30.0
SHAPES = ("unrequited", "mutual_secret", "mutual_open", "triangle", "slow_burn")
# kind: (weight, how many a directed pair can take a night)
KINDS = {
    "raided": (1.0, 1), "grouped_away": (1.5, 1), "kill_alive": (1.0, 2), "first_kill": (2.0, 2),
    "wipe_stood": (0.5, 1), "warm_scene": (2.0, 1), "took_side": (4.0, 1), "heard": (2.0, 1),
    "defended": (4.0, 1), "banter": (2.0, 1), "voice_late": (1.0, 1), "dkp_close": (0.0, 1),
    "dkp_respect": (3.0, 1),
}
SCOPES = ("public", "pair", "restricted", "private")
WHY = ("draw", "affinity", "feud", "ex", "pair_open", "cap_member", "cap_live", "cap_taken")
RAID_MIN_S = 600.0       # a raid that neither ran this long nor met a boss was no evening together
REPLY_S = 300.0          # a reply answers a line this recent on its channel
RECENT_LINES = 8
BANTER_S = 1800.0        # she spoke in the conversation within this of the line she reacted to
VOICE_MIN_S = 1800.0
VOICE_LONG_S = 5400.0
VOICE_SKIP = frozenset({"afk", "penalty_box"})
VOICE_CROWD = 6          # more members than this in a room that night is a crowd, not company
LATE_FROM, LATE_TO = time(22, 0), time(4, 0)
RESPECT_DAYS = 7.0
SOUR_EMOJI = frozenset({"\U0001f44e", "\U0001f620", "\U0001f621", "\U0001f644", "\U0001f92e",
                        "\U0001f595", "\U0001f612"})
US_ZONE = ZoneInfo("America/New_York")
UK_ZONE = ZoneInfo("Europe/London")
# Her stages toward the owner from which a thread of hers never passes `acknowledged`.
HIS_LATE = frozenset(ROMANCE_STAGES[ROMANCE_STAGES.index("late_nights"):ROMANCE_STAGES.index("ex")])
MEMBER_ACTIVE = ("notices", "banter", "whispers", "late_nights", "confession")  # couples.ACTIVE

AGREE_RE = re.compile(
    r"\b(?:agreed?|exactly|good point|fair point|so true|true that|you(?:'| a)?re right|"
    r"(?:is|was|she'?s|he'?s) right|well said|same here|seconded|what \w+ said|nailed it|"
    r"spot on|couldn'?t agree more|with (?:you|her|him) on (?:this|that))\b|(?<!\S)\+1(?!\S)",
    re.IGNORECASE)
DISAGREE_RE = re.compile(
    r"\b(?:dis-?agree|don'?t agree|do not agree|not right|isn'?t right|wrong|nope|nah)\b",
    re.IGNORECASE)
TAUNT_RE = re.compile(
    r"\b(?:is|was|you'?re|ur|such an?|what an?|total|absolute|bloody|fucking)\s+"
    r"(?:so\s+|really\s+|just\s+|a\s+|an\s+|the\s+)*(?:useless|stupid|dumb|idiot|noob|scrub|bad|"
    r"terrible|trash|garbage|worthless|annoying|pathetic|clown|joke|carried|dead ?weight|"
    r"liability|worst)\b|\b(?:l2p|learn to play|git gud|uninstall|stfu|shut up|nobody asked)\b",
    re.IGNORECASE)
DEFEND_RE = re.compile(
    r"\b(?:leave (?:him|her|them|\w+) alone|lay off|back off|knock it off|cut it out|be nice|"
    r"play nice|not cool|uncalled for|not fair|ease up|go easy|give (?:him|her|them|\w+) a break|"
    r"stop (?:it|that|picking)|that'?s enough|did(?:n'?t| not) deserve|"
    r"(?:doing|did|does|is|was) (?:fine|great|well|good))\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    a: int        # whose pull would move
    b: int        # toward whom
    scope: str
    ref: str
    weight: float | None = None  # None: the kind's


@dataclass(frozen=True)
class Cast:
    """Who the recorder reads tonight: the cast without the owner and without a member on a
    break, with what each event's factors need. Names are for spotting a named reply only."""
    human: int
    members: frozenset
    gender: dict
    taken: frozenset
    stance: dict   # a woman's attitude toward the owner: fond, drawn, lust
    region: dict
    names: dict    # guid -> character name, the owner's included


def build_cast(human: int, members, persona, names: dict) -> Cast:
    """`persona(guid)` is the night's stored persona (never the owner's godmode lens)."""
    from leeroy_mind.drama.couples import taken
    gender, took, stance, region = {}, set(), {}, {}
    kept = []
    for g in sorted(members):
        if g == human:
            continue
        p = persona(g)
        if p is None:
            continue
        kept.append(g)
        gender[g] = p.identity.player_gender
        region[g] = p.identity.region
        if taken(p):
            took.add(g)
        if gender[g] == "female" and p.romance.stance in STANCE_FACTOR:
            stance[g] = p.romance.stance
    return Cast(human=human, members=frozenset(kept), gender=gender, taken=frozenset(took),
                stance=stance, region=region, names=dict(names))


def cast_of(ctx) -> Cast:
    names = {g: m.name for g, m in ctx.store.cast().items()}
    return build_cast(ctx.human, ctx.members(), ctx.persona, names)


def cast_from_store(store, human: int) -> Cast:
    """The cast as a night would find it, read from the store alone (the replay tool)."""
    from leeroy_mind.nightly.runner import NightPersonas
    from leeroy_mind.nightly.store import active_away
    from leeroy_mind.personas.io import PersonaCache
    from leeroy_mind.wow import CAST_MIN_LEVEL
    roster = store.cast()
    personas = NightPersonas(PersonaCache(store))
    away = active_away(store)
    members = [g for g in store.persona_versions()
               if g != human and g in roster and roster[g].level >= CAST_MIN_LEVEL and g not in away]
    return build_cast(human, members, personas.get, {g: m.name for g, m in roster.items()})


def scope_of(channel: str) -> str:
    if is_private(channel):
        return "private"
    return "restricted" if is_restricted(channel) else "public"


def _object(text) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pairs(kind, who, scope, ref, weight=None):
    who = sorted(set(who))
    for a in who:
        for b in who:
            if a != b:
                yield Event(kind, a, b, scope, ref, weight)


# -- the sources -----------------------------------------------------------------------------

def _raids(conn, window, cast):
    for r in conn.execute(
            "SELECT raid_id, offline, started, ended, kills, wipes FROM guild_raid "
            "WHERE started >= ? AND started < ? ORDER BY raid_id", window).fetchall():
        ran = (r["ended"] if r["ended"] is not None else window[1]) - r["started"]
        if r["kills"] + r["wipes"] <= 0 and ran < RAID_MIN_S:
            continue
        who = [x["guid"] for x in conn.execute(
            "SELECT guid FROM guild_attendance WHERE raid_id = ? AND status = 'raided'",
            (r["raid_id"],))]
        yield from _pairs("grouped_away" if r["offline"] else "raided", who, "public",
                          f"raid:{r['raid_id']}")


def _bosses(conn, window, cast):
    """Who stood at each kill, and who was still standing when a wipe was called: the fight's
    members without those whose last word in it was a death. A kill or wipe with no stored
    engage in the window proves nothing and is passed over."""
    pulls = {}
    for row in conn.execute(
            "SELECT id, type, session_id, payload FROM game_event WHERE ts >= ? AND ts < ? "
            "AND type IN ('boss', 'death', 'resurrect') ORDER BY ts, id", window).fetchall():
        p = _object(row["payload"])
        try:
            if row["type"] == "boss":
                key = (row["session_id"], p["map"], p["instance_id"], p["boss_id"])
                if p["state"] in ("started", "engaged"):
                    pulls[key] = (set(p["members"]), set())
                elif p["state"] in ("killed", "wiped"):
                    pull = pulls.pop(key, None)
                    if pull is None:
                        continue
                    alive = set(p["members"]) - pull[1]
                    ref = f"event:{row['id']}"
                    if p["state"] == "wiped":
                        yield from _pairs("wipe_stood", alive, "public", ref)
                        continue
                    first = conn.execute(
                        "SELECT 1 FROM game_event WHERE type = 'boss' AND id < ? "
                        "AND json_extract(payload, '$.state') = 'killed' "
                        "AND json_extract(payload, '$.map') = ? "
                        "AND json_extract(payload, '$.boss_id') = ? LIMIT 1",
                        (row["id"], p["map"], p["boss_id"])).fetchone() is None
                    yield from _pairs("first_kill" if first else "kill_alive", alive, "public", ref)
            else:
                guid = p["guid"]
                for key, (members, dead) in pulls.items():
                    if key[0] == row["session_id"] and guid in members:
                        (dead.add if row["type"] == "death" else dead.discard)(guid)
        except (KeyError, TypeError):
            continue


def _scenes(conn, night, window, cast):
    """Tonight's accepted scenes: a warm one (its applied changes sum above zero) draws both,
    and one that set a rumour straight is the corrector taking the side of whom it is about."""
    scenes = {r["id"]: r for r in conn.execute(
        "SELECT id, a, b, volition, ref FROM encounter WHERE night = ? AND status = 'done' "
        "ORDER BY id", (night,)).fetchall()}
    if not scenes:
        return
    totals = Counter()
    for r in conn.execute(
            "SELECT delta, evidence FROM rel_delta WHERE ts >= ? AND reason LIKE 'encounter%' "
            "AND reason NOT LIKE '%not applied%' AND dim IN ('affinity', 'trust', 'respect')",
            (int(window[0]),)):
        try:
            first = json.loads(r["evidence"])[0]
        except (TypeError, ValueError, IndexError):
            continue
        if isinstance(first, str) and first.startswith("encounter:") and first[10:].isdecimal():
            totals[int(first[10:])] += r["delta"]
    for ident, s in scenes.items():
        ref = f"encounter:{ident}"
        if totals.get(ident, 0) > 0:
            yield from _pairs("warm_scene", (s["a"], s["b"]), "pair", ref)
        rule = s["ref"] or ""
        if s["volition"] == "correct" and rule.startswith("rule:rumor:") and rule[11:].isdecimal():
            about = conn.execute("SELECT about FROM rumor WHERE id = ?", (int(rule[11:]),)).fetchone()
            if about is not None and about["about"] not in (s["a"], cast.human):
                # She was not in the scene: it reaches her second-hand, never as a public fact.
                yield Event("took_side", about["about"], s["a"], "restricted", ref)


def _feuds(conn, window, cast):
    for r in conn.execute("SELECT id, sides FROM feud WHERE started >= ? AND started < ? "
                          "ORDER BY id", window).fetchall():
        for ally, siders in _object(r["sides"]).items():
            if not (isinstance(ally, str) and ally.isdecimal() and isinstance(siders, list)):
                continue
            for s in siders:
                if type(s) is int and s != int(ally):
                    yield Event("took_side", int(ally), s, "public", f"feud:{r['id']}")


def _named(cast):
    """guid -> a matcher for that member's name as a word (names of three letters or more)."""
    return {g: re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE)
            for g, n in cast.names.items() if isinstance(n, str) and len(n) >= 3}


def _sees(audience, guid, scope) -> bool:
    """Whether `guid` could have read the line. A stored audience decides; without one a public
    line is everyone's and a restricted one is nobody's (fail closed)."""
    if audience is None:
        return scope == "public"
    try:
        return guid in json.loads(audience)
    except (TypeError, ValueError):
        return False


def _chat(conn, window, cast):
    """A member agreeing by name with a line another member just said (`heard`: the one agreed
    with), and a member answering a taunt aimed at another (`defended`: the one it was aimed
    at). Whispers and DMs are with the owner and are never read; the gainer must have been
    able to read the reply. The text is matched here and goes nowhere."""
    named = _named(cast)
    recent: dict[str, list] = {}
    for row in conn.execute(
            "SELECT id, ts, channel, from_guid, text, audience, origin FROM chat_line "
            "WHERE ts >= ? AND ts < ? ORDER BY ts, id", window).fetchall():
        channel, x, text = row["channel"], row["from_guid"], row["text"] or ""
        if is_private(channel):
            continue
        scope = scope_of(channel)
        before = [p for p in recent.get(channel, ()) if row["ts"] - p[0] <= REPLY_S]
        if row["origin"] == "mind" and x in cast.members:
            ref = f"chat:{row['id']}"
            agrees = AGREE_RE.search(text) and not DISAGREE_RE.search(text)
            defends = DEFEND_RE.search(text)
            done = set()
            for _, y, their_text in before:
                if (agrees and y != x and y in cast.members and ("heard", y) not in done
                        and y in named and named[y].search(text)
                        and _sees(row["audience"], y, scope)):
                    done.add(("heard", y))
                    yield Event("heard", y, x, scope, ref)
                if not (defends and y != x and TAUNT_RE.search(their_text)):
                    continue
                for target, match in named.items():
                    if (target in (x, y) or target not in cast.members
                            or ("defended", target) in done or not match.search(their_text)):
                        continue
                    answers = (match.search(text)
                               or (y in named and named[y].search(text)))
                    if answers and _sees(row["audience"], target, scope):
                        done.add(("defended", target))
                        yield Event("defended", target, x, scope, ref)
        before.append((row["ts"], x, text))
        recent[channel] = before[-RECENT_LINES:]


def _talk_channel(channel: str) -> str:
    return channel if channel.startswith((DM_PREFIX, GROUP_PREFIX)) else CHANNEL_PREFIX + channel


def _banter(conn, window, cast):
    """A member's reaction to another member's Discord line in a conversation she spoke in
    herself (the same channel and thread, within BANTER_S of that line)."""
    for r in conn.execute(
            "SELECT r.message, r.member, r.emoji, m.author, m.channel, m.thread, m.ts AS said "
            "FROM discord_reaction r JOIN discord_message m ON m.id = r.message "
            "WHERE r.ts >= ? AND r.ts < ? AND m.origin = 'mind' AND m.deleted IS NULL "
            "AND r.member != m.author ORDER BY r.ts, r.message, r.member", window).fetchall():
        channel = _talk_channel(r["channel"])
        if is_private(channel) or r["emoji"] in SOUR_EMOJI:
            continue
        if r["member"] not in cast.members or r["author"] not in cast.members:
            continue
        spoke = conn.execute(
            "SELECT 1 FROM discord_message WHERE channel = ? AND author = ? AND origin = 'mind' "
            "AND deleted IS NULL AND thread IS ? AND ts BETWEEN ? AND ? LIMIT 1",
            (r["channel"], r["member"], r["thread"], r["said"] - BANTER_S,
             r["said"] + BANTER_S)).fetchone()
        if spoke is not None:
            yield Event("banter", r["member"], r["author"], scope_of(channel),
                        f"discord:{r['message']}")


def _late_spans(window, zone):
    """The stretches of the window that are late (LATE_FROM to LATE_TO) on `zone`'s clock."""
    spans, day = [], datetime.fromtimestamp(window[0], zone).date() - timedelta(days=1)
    while True:
        start = datetime.combine(day, LATE_FROM, zone).timestamp()
        if start >= window[1]:
            return spans
        end = datetime.combine(day + timedelta(days=1), LATE_TO, zone).timestamp()
        lo, hi = max(start, window[0]), min(end, window[1])
        if hi > lo:
            spans.append((lo, hi))
        day += timedelta(days=1)


def _voice(conn, window, cast, notes=None):
    """Two members in one voice room late on the holder's own clock, VOICE_MIN_S or more over
    the night. A room that held more than VOICE_CROWD members in the night's late hours is a
    crowd (the hall while a raid runs, the evening's silent seats) and counts for nobody. The
    officers' room and the private rooms are restricted evidence."""
    rooms: dict[str, list] = {}
    for r in conn.execute(
            "SELECT id, guid, room, since, until FROM voice_presence WHERE since < ? "
            "AND coalesce(until, ?) > ? ORDER BY id", (window[1], window[1], window[0])).fetchall():
        if r["guid"] not in cast.members or r["room"] in VOICE_SKIP:
            continue
        if r["until"] is None and r["since"] < window[0] - DAY_S:
            continue  # a row nobody closed is no evidence of a night in the room
        lo = max(r["since"], window[0])
        hi = min(r["until"] if r["until"] is not None else window[1], window[1])
        if hi > lo:
            rooms.setdefault(r["room"], []).append((r["guid"], lo, hi, r["id"]))
    late = {"UK": _late_spans(window, UK_ZONE), "US": _late_spans(window, US_ZONE)}
    any_late = late["UK"] + late["US"]
    seconds: dict[tuple, list] = {}
    for room, rows in sorted(rooms.items()):
        there = {g for g, lo, hi, _ in rows if any(min(hi, e) > max(lo, s) for s, e in any_late)}
        if len(there) > VOICE_CROWD:
            if notes is not None:
                notes["voice_crowd"] += 1
            continue
        channel = f"voice:{room}"
        scope = "restricted" if room.startswith("private") or is_restricted(channel) else "public"
        for i, (a, alo, ahi, aid) in enumerate(rows):
            for b, blo, bhi, bid in rows[i + 1:]:
                lo, hi = max(alo, blo), min(ahi, bhi)
                if a == b or hi <= lo:
                    continue
                for x, y, ident in ((a, b, aid), (b, a, bid)):
                    spans = late["UK" if cast.region.get(x) == "UK" else "US"]
                    got = sum(max(0.0, min(hi, e) - max(lo, s)) for s, e in spans)
                    if got > 0:
                        slot = seconds.setdefault((x, y), [0.0, 0.0, ident])
                        slot[0] += got
                        slot[1] += got if scope == "public" else 0.0
                        slot[2] = min(slot[2], ident)
    for (x, y), (total, public, ident) in sorted(seconds.items()):
        if total >= VOICE_MIN_S:
            yield Event("voice_late", x, y, "public" if public >= VOICE_MIN_S else "restricted",
                        f"voice:{ident}", 2.0 if total >= VOICE_LONG_S else 1.0)


def _dkp(conn, window, cast):
    """A close award (the runner-up lost by a little) is recorded as an outcome that moves
    nothing; respect either gained for the other within RESPECT_DAYS of it is worth a pull,
    once per pair and award."""
    from leeroy_mind.culture.origins import loot_record
    for row in conn.execute("SELECT key FROM meta WHERE key GLOB 'guild:loot:*' ORDER BY key").fetchall():
        found = loot_record(conn, "loot:close_loot:" + row["key"])
        if found is None:
            continue
        record, when = found
        award = record["award"]
        winner, runner = award["guid"], award.get("runner_up")
        if award.get("close") is not True or type(runner) is not int or runner == winner:
            continue
        ref = "loot:" + row["key"]
        if window[0] <= when < window[1]:
            yield from _pairs("dkp_close", (winner, runner), "public", ref)
        if not window[0] - RESPECT_DAYS * DAY_S <= when < window[1]:
            continue
        for x, y in ((runner, winner), (winner, runner)):
            if conn.execute("SELECT 1 FROM pull_event WHERE kind = 'dkp_respect' AND a = ? "
                            "AND b = ? AND ref = ?", (x, y, ref)).fetchone():
                continue
            if conn.execute(
                    "SELECT 1 FROM rel_delta WHERE a = ? AND b = ? AND dim = 'respect' "
                    "AND delta > 0 AND reason NOT LIKE '%not applied%' AND ts > ? AND ts <= ? "
                    "LIMIT 1", (x, y, when, when + RESPECT_DAYS * DAY_S)).fetchone():
                yield Event("dkp_respect", x, y, "public", ref)


def collect(conn, night: str, window, cast: Cast, notes=None) -> list[Event]:
    """Every raw event of the night, in a stable order; no factor, cap or pairing rule yet.
    `notes` counts what a source passed over whole (a crowded voice room)."""
    out = []
    for source in (_raids(conn, window, cast), _bosses(conn, window, cast),
                   _scenes(conn, night, window, cast), _feuds(conn, window, cast),
                   _chat(conn, window, cast), _banter(conn, window, cast),
                   _voice(conn, window, cast, notes), _dkp(conn, window, cast)):
        out.extend(source)
    return out


# -- factors, the ledger and the shadow --------------------------------------------------------

def factor(cast: Cast, a: int) -> float:
    """What a gain of `a` is multiplied by: the household cost and, for a woman, her stance
    toward the owner."""
    return ((TAKEN_FACTOR if a in cast.taken else 1.0)
            * STANCE_FACTOR.get(cast.stance.get(a), 1.0))


def _nights(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def decayed(pull: float, idle_nights: int) -> float:
    """A pull after `idle_nights` nights without an event."""
    return pull * DECAY ** max(0, idle_nights)


def _ledger(conn, night, events, cast, now):
    """The night's rows: man-woman pairs of the cast only, each kind's cap, the factors, the
    night cap. Returns ({(a, b): [amount, public amount, events]}, kinds, scopes, skipped)."""
    gains, kinds, scopes, skipped = {}, Counter(), Counter(), Counter()
    seen, per_kind = set(), Counter()
    for e in events:
        if e.a == e.b or e.kind not in KINDS or e.scope not in SCOPES:
            continue
        if e.a not in cast.members or e.b not in cast.members:
            skipped["not_cast"] += 1
            continue
        if {cast.gender.get(e.a), cast.gender.get(e.b)} != {"female", "male"}:
            skipped["same_sex"] += 1
            continue
        key = (e.a, e.b, e.kind, e.ref)
        if key in seen:
            continue
        seen.add(key)
        if per_kind[(e.a, e.b, e.kind)] >= KINDS[e.kind][1]:
            skipped["kind_cap"] += 1
            continue
        weight = KINDS[e.kind][0] if e.weight is None else e.weight
        mult = factor(cast, e.a)
        slot = gains.setdefault((e.a, e.b), [0.0, 0.0, 0])
        amount = min(weight * mult, max(0.0, NIGHT_CAP - slot[0]))
        if weight > 0 and amount <= 0:
            skipped["night_cap"] += 1
            continue
        per_kind[(e.a, e.b, e.kind)] += 1
        conn.execute(
            "INSERT INTO pull_event(night, a, b, kind, weight, factor, amount, scope, ref, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (night, e.a, e.b, e.kind, weight, mult, amount, e.scope, e.ref, now))
        kinds[e.kind] += 1
        scopes[e.scope] += 1
        if amount > 0:
            slot[0] += amount
            slot[1] += amount if e.scope == "public" else 0.0
            slot[2] += 1
    return {k: v for k, v in gains.items() if v[2]}, kinds, scopes, skipped


def _shadow(conn, night, gains):
    """Decay every stored pull for its nights without an event, then add tonight's gains."""
    rows = {(r["a"], r["b"]): r for r in conn.execute("SELECT * FROM pull_shadow").fetchall()}
    for pair in sorted(set(rows) | set(gains)):
        row, gain = rows.get(pair), gains.get(pair)
        if row is None:
            conn.execute(
                "INSERT INTO pull_shadow(a, b, pull, public_pull, events, first_night, "
                "last_event_night, updated_night) VALUES (?,?,?,?,?,?,?,?)",
                (*pair, min(100.0, gain[0]), min(100.0, gain[1]), gain[2], night, night, night))
            continue
        idle = _nights(row["updated_night"], night) - (1 if gain else 0)
        pull, public = decayed(row["pull"], idle), decayed(row["public_pull"], idle)
        if gain:
            pull, public = min(100.0, pull + gain[0]), min(100.0, public + gain[1])
        conn.execute(
            "UPDATE pull_shadow SET pull = ?, public_pull = ?, events = events + ?, "
            "last_event_night = ?, updated_night = ? WHERE a = ? AND b = ?",
            (pull, public, gain[2] if gain else 0,
             night if gain else row["last_event_night"], night, *pair))


# -- would-be threads --------------------------------------------------------------------------

def shape_of(*, reverse: float, taken: bool, third: bool, nights_to_open: int) -> str:
    """The label of a thread from tonight's facts: a triangle when a third person is in it,
    else mutual (secret when either is taken at home), else a slow burn, else unrequited."""
    if third:
        return "triangle"
    if reverse >= MUTUAL_PULL:
        return "mutual_secret" if taken else "mutual_open"
    return "slow_burn" if nights_to_open >= SLOW_NIGHTS else "unrequited"


def admit(candidates, live, *, threads_max=THREADS_MAX, taken_max=TAKEN_MAX):
    """Which waiting candidates the caps would let open, rarest shape among the live threads
    first, then the stronger pull. `candidates` and `live` are dicts with a, b, pull, shape,
    taken. Returns (opened, {(a, b): why})."""
    live = list(live)
    opened, why = [], {}
    counts = Counter(t["shape"] for t in live)
    for c in sorted(candidates, key=lambda c: (counts[c["shape"]], -c["pull"], c["a"], c["b"])):
        busy = {g for t in live for g in (t["a"], t["b"])}
        if c["a"] in busy or c["b"] in busy:
            why[(c["a"], c["b"])] = "cap_member"
        elif c["taken"] and sum(1 for t in live if t["taken"]) >= taken_max:
            why[(c["a"], c["b"])] = "cap_taken"
        elif len(live) >= threads_max:
            why[(c["a"], c["b"])] = "cap_live"
        else:
            live.append(c)
            opened.append(c)
            counts[c["shape"]] += 1
    return opened, why


def _one(conn, sql, params, default=0.0):
    row = conn.execute(sql, params).fetchone()
    return default if row is None or row[0] is None else row[0]


def _threads(conn, night, cast, now, rng):
    """The would-be threads after tonight's pulls: fade, relabel, then let the caps admit."""
    rows = {(r["a"], r["b"]): dict(r) for r in conn.execute("SELECT * FROM pull_shadow").fetchall()}
    strong = [(k, r["pull"]) for k, r in rows.items() if r["pull"] >= TRIANGLE_PULL]
    late = {g for g in cast.members if cast.gender.get(g) == "female" and _one(
        conn, "SELECT romance_stage FROM relationship WHERE a = ? AND b = ?",
        (g, cast.human), "none") in HIS_LATE}
    marks = ",".join("?" * len(MEMBER_ACTIVE))

    def couple_with_other(g, other):
        return conn.execute(
            f"SELECT 1 FROM relationship WHERE a = ? AND b NOT IN (?, ?, ?) "
            f"AND romance_stage IN ({marks}) LIMIT 1",
            (g, cast.human, other, g, *MEMBER_ACTIVE)).fetchone() is not None

    def facts(a, b):
        r = rows[(a, b)]
        third = (any({x, y} & {a, b} and {x, y} != {a, b} for (x, y), _ in strong)
                 or bool({a, b} & late) or couple_with_other(a, b) or couple_with_other(b, a))
        taken = a in cast.taken or b in cast.taken
        since = r["thread_night"] if r["thread_state"] == "open" and r["thread_night"] else night
        return {"a": a, "b": b, "pull": r["pull"], "taken": taken,
                "ceiling": "acknowledged" if {a, b} & late else "",
                "shape": shape_of(reverse=rows.get((b, a), {}).get("pull", 0.0), taken=taken,
                                  third=third, nights_to_open=_nights(r["first_night"], since))}

    def put(pair, state, why, f, thread_night):
        conn.execute(
            "UPDATE pull_shadow SET thread_state = ?, thread_why = ?, shape = ?, ceiling = ?, "
            "taken = ?, thread_night = ? WHERE a = ? AND b = ?",
            (state, why, f["shape"] if f else "", f["ceiling"] if f else "",
             int(f["taken"]) if f else 0, thread_night, *pair))

    live, faded = [], 0
    for pair, r in sorted(rows.items()):
        if r["thread_state"] != "open":
            continue
        gone = pair[0] not in cast.members or pair[1] not in cast.members
        if r["pull"] < FADE_PULL or gone:
            put(pair, "faded", "", None, night)
            r["thread_state"] = "faded"
            faded += 1
        else:
            f = facts(*pair)
            put(pair, "open", "", f, r["thread_night"])
            live.append(f)
    open_pairs = {frozenset((t["a"], t["b"])) for t in live}
    feuding = {frozenset((r["a"], r["b"])) for r in conn.execute(
        "SELECT a, b FROM feud WHERE ended IS NULL")}
    exes = {frozenset((r["woman"], r["partner"])) for r in conn.execute(
        "SELECT woman, partner FROM breakup WHERE ts > ?", (now - EX_COOLDOWN_DAYS * DAY_S,))}
    waiting, candidates, threshold = Counter(), [], 0
    for pair, r in sorted(rows.items()):
        if r["thread_state"] == "open":
            continue
        a, b = pair
        if r["pull"] < OPEN_PULL or a not in cast.members or b not in cast.members:
            if r["thread_state"] == "waiting":
                put(pair, "none", "", None, None)
            continue
        threshold += 1
        f = facts(a, b)
        both = frozenset(pair)
        if both in open_pairs:
            why = "pair_open"
        elif both in feuding:
            why = "feud"
        elif both in exes:
            why = "ex"
        elif _one(conn, "SELECT affinity FROM relationship WHERE a = ? AND b = ?", pair) < OPEN_AFFINITY:
            why = "affinity"
        elif rng(a, b).random() >= OPEN_P:
            why = "draw"
        else:
            candidates.append(f)
            continue
        waiting[why] += 1
        put(pair, "waiting", why, f, r["thread_night"] if r["thread_state"] == "waiting" else night)
    # The stronger direction of a pair holds its thread; the other waits behind it.
    best = {}
    for c in candidates:
        key = frozenset((c["a"], c["b"]))
        if key not in best or c["pull"] > best[key]["pull"]:
            best[key] = c
    opened, refused = admit(sorted(best.values(), key=lambda c: (c["a"], c["b"])), live)
    won = {(c["a"], c["b"]) for c in opened}
    for c in candidates:
        pair = (c["a"], c["b"])
        if pair in won:
            put(pair, "open", "", c, night)
            log("thread_would_open", a=c["a"], b=c["b"], shape=c["shape"], taken=int(c["taken"]))
            continue
        why = refused.get(pair) or refused.get(pair[::-1]) or "pair_open"
        waiting[why] += 1
        was = rows[pair]
        put(pair, "waiting", why, c, was["thread_night"] if was["thread_state"] == "waiting" else night)
    live += opened
    return {"threshold": threshold, "opened": len(opened),
            "open": len(live), "open_taken": sum(1 for t in live if t["taken"]),
            "waiting": dict(sorted(waiting.items())), "faded": faded,
            "shapes": dict(sorted(Counter(t["shape"] for t in live).items())),
            "ceilings": sum(1 for t in live if t["ceiling"])}


def record_night(store, night: str, window, cast: Cast, *, now: float | None = None) -> dict:
    """Record one night inside the caller's transaction: the ledger rows, the shadow pulls and
    the would-be threads. A night already recorded, or one before the newest recorded night,
    is left alone. Returns the night's summary (counts only)."""
    conn = store.conn
    done = conn.execute("SELECT summary FROM pull_night WHERE night = ?", (night,)).fetchone()
    if done is not None:
        return json.loads(done["summary"])
    newest = conn.execute("SELECT max(night) FROM pull_night").fetchone()[0]
    if newest is not None and newest > night:
        log("pulls_night_skipped", night=night, reason="out_of_order")
        return {"skipped": "out_of_order"}
    now = store.clock.now() if now is None else now
    window = (float(window[0]), float(window[1]))
    notes = Counter()
    events = collect(conn, night, window, cast, notes)
    gains, kinds, scopes, skipped = _ledger(conn, night, events, cast, now)
    skipped.update(notes)
    _shadow(conn, night, gains)

    def rng(a, b):
        return random.Random(f"{night}|{STAGE}|open|{a}|{b}")

    summary = {"events": sum(kinds.values()), "kinds": dict(sorted(kinds.items())),
               "scopes": dict(sorted(scopes.items())), "skipped": dict(sorted(skipped.items())),
               "pairs": len(gains), "members": len(cast.members), "taken": len(cast.taken),
               **_threads(conn, night, cast, now, rng)}
    conn.execute("INSERT INTO pull_night(night, ts, summary) VALUES (?,?,?)",
                 (night, now, json.dumps(summary, sort_keys=True)))
    log("pulls_night", night=night, events=summary["events"], pairs=summary["pairs"],
        open=summary["open"], opened=summary["opened"], faded=summary["faded"],
        waiting=sum(summary["waiting"].values()))
    return summary


async def stage_pulls(ctx) -> None:
    """The night's model-free unit, after `encounters`. Nothing at all unless the night's
    switch says record."""
    from leeroy_mind.drama.config import threads_mode
    if threads_mode(ctx) != "record" or ctx.done(STAGE, "all"):
        return
    cast = cast_of(ctx)
    ctx.commit(STAGE, "all", lambda store: record_night(store, ctx.night, ctx.window, cast,
                                                        now=ctx.clock.now()))


def attach(app):
    """Startup only: anything but `threads = "record"` adds no stage."""
    from leeroy_mind.drama.config import load_drama_config
    if load_drama_config(app.data_dir / "mind.toml").threads != "record":
        return None
    app.night_runner.add_stage(STAGE, stage_pulls, after="encounters", model=None)
    return True
