"""Romantic threads, phase 1: the recorder (`[drama] threads = "record"`, owner 2026-11-17).

It records what would pull two members together and changes nothing. A *pull* is a directed
number a -> b (0-100) for a man and a woman of the cast that only recorded events move. Here
it is a shadow: `pull_shadow`, never `relationship.attraction`, and no member's prompt, card,
scene, relationship row or memory reads or is written from it.

- **Events** (`pull_event`, one row per fact with its kind, weight, factor, likeness, scope and
  evidence id). Two families:
  - *presence* (PRESENCE): a raid together with him (`raided`) or without him (`grouped_away`),
    standing at a kill (`kill_alive`, `first_kill`) or at a wipe's end (`wipe_stood`). Presence
    saturates: a pair's first presence event of a night counts whole, later ones at
    PRESENCE_LATER, and presence can give a pair at most PRESENCE_CAP a night. A thread never
    opens on presence alone (OPEN_SOCIAL).
  - *social*: his 5-man party, a small public activity (`partied`), a warm accepted scene
    (`warm_scene`), setting a rumour about her straight to her face or siding with her in a feud
    (`took_side`), being agreed with by name (`heard`), a taunt at her answered (`defended`), a
    Discord reply (`answered` for the one replied to, `replied` for the replier), talking back
    and forth (`talked`), being named (`mentioned`), a reaction to the other's line (`reacted`;
    `banter` when she spoke in that conversation too), both speaking in one banter riff
    (`riffed`), a joke or tease aimed at the other (`joked`), a compliment (`complimented`),
    speaking well of the other by name (`spoke_well` for the one spoken of, `praised` for the
    speaker), tonight's admiration or gratitude toward the other (`admired`), a small voice room
    together (`voice_room`, `voice_late` late at night), a close DKP award (`dkp_close`, weight
    0: an outcome) and respect after one (`dkp_respect`).
- **Cost, not ban.** The gains of a member taken at home (`couples.taken`) are multiplied by
  TAKEN_FACTOR; a woman's by her stance toward the owner (STANCE_FACTOR), the two never below
  FACTOR_FLOOR together. A thread of a woman whose arc with him is at late_nights or later is
  marked with the ceiling `acknowledged`.
- **Likeness.** A pair's gains are multiplied by 0.8-1.25 from what their cards share (region,
  night rhythm, humour, what they do outside raids, likes) and a small seeded draw.
- **Decay.** HALF_LIFE nights without an event halve a pull; its public part halves over
  HALF_LIFE nights without a public event and its social part without a social one.
- **Would-be threads.** A pull of OPEN_PULL (OPEN_SOCIAL of it social) with affinity
  OPEN_AFFINITY or more both ways would open a thread on a seeded draw (OPEN_P a night) when
  the caps allow (THREADS_MAX live, TAKEN_MAX with a taken member, one per member,
  OPENS_PER_NIGHT a night, the rarest shape first); the label (`shape`) and what kept a
  candidate out (`thread_why`) are stored with the shadow row. A member couple counts as a
  third person from `banter` on (two who only notice each other do not). A would-be thread
  fades when its pull falls under FADE_PULL.
- **Privacy.** Only public evidence is read: public chat channels, public Discord channels,
  public voice rooms, guild raids and kills. Whispers, DMs, group DMs, the officers' channels
  and room, and the private, interview and penalty-box voice rooms are never read, and a line
  whose stored audience leaves the holder out (or that was stored without one) moves nothing of
  hers. A scene is between the two (scope `pair`). A feeling of the holder's own that the other
  never saw (an admiration, respect gained) is `private`, and an admiration is read only when
  every memory it rests on reads open by its effective scope (memory.privacy.effective_scopes).
  `public_pull` is the part public evidence alone would have built. Nothing here is shown to a
  member.
- **The gate for anything shown.** Whatever a later phase shows beyond the owner's console must
  gate on `public_pull` (what the guild could know) or on `pull_event.scope` (`pair` for what
  the two share), never on `pull`, `thread_state` or `shape`: those come from the whole pull,
  and the would-be threads of the live replay rest mostly on pair and private evidence.
  "Public" here is "not private or restricted": a party, raid, say or voice-room line and
  presence are public though only those there saw them, so a "what the guild knows" view must
  not read them as known to all.

The stage and `record_night` write only `pull_event`, `pull_shadow` and `pull_night`, inside
the caller's transaction. Chat text is read to detect a reply or a name and never stored or
logged; logs and rows carry ids, counts and codes.
"""
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from leeroy_mind.discord.channels import (CHANNEL_PREFIX, DM_PREFIX, GROUP_PREFIX, is_discord,
                                          is_public)
from leeroy_mind.logs import log
from leeroy_mind.storage.rows import ROMANCE_STAGES
from leeroy_mind.voice.rooms import PRIVATE

STAGE = "pulls"
DAY_S = 86400.0
HALF_LIFE = 21.0
DECAY = 0.5 ** (1.0 / HALF_LIFE)
OPEN_PULL = 35.0
OPEN_SOCIAL = 12.0   # of that pull, what must have come from social events
OPEN_AFFINITY = 0.0  # not disliking each other; the liking is the pull's social part
OPEN_P = 0.5
FADE_PULL = 10.0
MUTUAL_PULL = 20.0   # the other's pull from which it is mutual
TRIANGLE_PULL = 25.0  # a third member's pull toward either that makes a triangle
SLOW_NIGHTS = 20     # nights from the first event to the opening that make a slow burn
THREADS_MAX = 3      # live threads guild-wide (phase 2 starts here)
TAKEN_MAX = 1        # of those, threads with a taken member
OPENS_PER_NIGHT = 1
NIGHT_CAP = 12.0     # what one directed pair's pull can take in a night
PRESENCE_CAP = 2.0   # of that, from presence events
PRESENCE_LATER = 0.25  # a pair's presence events after its first of the night
TAKEN_FACTOR = 0.6
STANCE_FACTOR = {"fond": 0.9, "drawn": 0.8, "lust": 0.6}
FACTOR_FLOOR = 0.4
LIKENESS_MIN, LIKENESS_MAX = 0.8, 1.25
EX_COOLDOWN_DAYS = 30.0
SHAPES = ("unrequited", "mutual_secret", "mutual_open", "triangle", "slow_burn")
PRESENCE = frozenset({"raided", "grouped_away", "kill_alive", "first_kill", "wipe_stood"})
# kind: (weight, how many a directed pair can take a night)
KINDS = {
    "raided": (1.0, 1), "grouped_away": (1.5, 1), "kill_alive": (1.0, 2), "first_kill": (2.0, 2),
    "wipe_stood": (0.5, 1),
    "partied": (4.0, 1), "warm_scene": (10.0, 1), "took_side": (12.0, 1), "heard": (7.5, 1),
    "defended": (12.0, 1), "answered": (5.0, 3), "replied": (4.0, 3), "talked": (2.5, 2),
    "mentioned": (4.0, 2), "reacted": (2.5, 2), "banter": (6.0, 1), "riffed": (6.0, 1),
    "joked": (4.0, 1), "complimented": (7.5, 1), "spoke_well": (9.0, 1), "praised": (5.0, 1),
    "admired": (5.0, 1), "voice_room": (5.0, 1), "voice_late": (7.5, 1), "dkp_close": (0.0, 1),
    "dkp_respect": (7.5, 1),
}
SCOPES = ("public", "pair", "restricted", "private")
WHY = ("draw", "affinity", "social", "feud", "ex", "pair_open", "cap_member", "cap_taken",
       "cap_live", "cap_night")
RAID_MIN_S = 600.0       # a raid that neither ran this long nor met a boss was no evening together
REPLY_S = 300.0          # a chat reply answers a line this recent on its channel
RECENT_LINES = 8
DISCORD_REPLY_S = 600.0  # a Discord reply counts within this of the message it answers
TALK_S = 300.0           # two members' messages this close in a channel are talking
BANTER_S = 1800.0        # she spoke in the conversation within this of the line she reacted to
RIFF_S = 600.0           # banter lines this close on one channel are one riff
MOVE_LINE_S = 60.0       # a scored move's own chat line is this close to it
PARTY_MAX = 5
VOICE_MIN_S = 1200.0
VOICE_LATE_S = 1800.0
# Voice rooms that are never public (learn reads them the same way): the private rooms, the
# officers', applicant interviews and the penalty box. Neither their lines nor their seats count.
NON_PUBLIC_ROOMS = frozenset({*PRIVATE, "officers", "interview", "penalty_box"})
VOICE_SKIP = NON_PUBLIC_ROOMS | {"afk"}
VOICE_ROOM_MAX = 4       # more members than this in a room that night is a crowd, not company
LATE_FROM, LATE_TO = time(22, 0), time(4, 0)
RESPECT_DAYS = 7.0
JOKE_MOVES = frozenset({"joke", "tease", "callback"})
WARM_MOVES = frozenset({"compliment", "petname"})
WARM_FEELINGS = frozenset({"admiration", "gratitude"})
SOUR_EMOJI = frozenset({"\U0001f44e", "\U0001f620", "\U0001f621", "\U0001f644", "\U0001f92e",
                        "\U0001f595", "\U0001f612"})
US_ZONE = ZoneInfo("America/New_York")
UK_ZONE = ZoneInfo("Europe/London")
# Her stages toward the owner from which a thread of hers never passes `acknowledged`.
HIS_LATE = frozenset(ROMANCE_STAGES[ROMANCE_STAGES.index("late_nights"):ROMANCE_STAGES.index("ex")])

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
    break, with what each event's factors need. Names are for spotting a named line only."""
    human: int
    members: frozenset
    gender: dict
    taken: frozenset
    stance: dict   # a woman's attitude toward the owner: fond, drawn, lust
    region: dict
    names: dict    # guid -> character name, the owner's included
    traits: dict = field(default_factory=dict)  # guid -> (region, night owl, humour, activities, likes)


def build_cast(human: int, members, persona, names: dict) -> Cast:
    """`persona(guid)` is the night's stored persona (never the owner's godmode lens)."""
    from leeroy_mind.drama.couples import taken
    gender, took, stance, region, traits = {}, set(), {}, {}, {}
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
        traits[g] = (p.identity.region, bool(p.real_life.night_owl), p.humour,
                     frozenset(p.game.outside_raids),
                     frozenset(" ".join(x.casefold().split()) for x in p.likes))
        if taken(p):
            took.add(g)
        if gender[g] == "female" and p.romance.stance in STANCE_FACTOR:
            stance[g] = p.romance.stance
    return Cast(human=human, members=frozenset(kept), gender=gender, taken=frozenset(took),
                stance=stance, region=region, names=dict(names), traits=traits)


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


def _readable(channel) -> bool:
    """A channel the recorder may read: public (never a whisper, a DM, a group DM, the officers'
    or a Discord channel the layout lacks) and no voice room of NON_PUBLIC_ROOMS."""
    if not (isinstance(channel, str) and is_public(channel)):
        return False
    if not channel.startswith("voice:"):
        return True
    room = channel.removeprefix("voice:")
    return room not in NON_PUBLIC_ROOMS and not room.startswith("private")


def _sees(audience, guid) -> bool:
    """Whether `guid` could have read a line on a readable channel: its stored audience decides
    (a party, raid, say or voice-room line reaches only those there). A line stored without one
    is an unknown audience, which proves nobody read it."""
    if audience is None:
        return False
    try:
        readers = json.loads(audience)
    except (TypeError, ValueError):
        return False
    return isinstance(readers, list) and guid in readers


def _named(cast):
    """guid -> a matcher for that member's name as a word (names of three letters or more),
    written as a name is, capitalised ("Rose", "ROSE"), or as an @-mention in any case: a name
    that is also a common word ("rose", "will") written in lower case names nobody."""
    out = {}
    for g, n in cast.names.items():
        if not isinstance(n, str) or len(n) < 3:
            continue
        head, rest = re.escape(n[0].upper()), re.escape(n[1:])
        out[g] = re.compile(rf"(?<!\w)(?:@(?i:{re.escape(n)})|{head}(?i:{rest}))(?!\w)")
    return out


# -- presence ----------------------------------------------------------------------------------

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


def _parties(conn, window, cast):
    """His 5-man party: a `group` event that is no raid, of PARTY_MAX or fewer. (Battlegrounds
    are not read: the cast's random-bot queues put hundreds of members a night in one.)"""
    for row in conn.execute(
            "SELECT id, type, payload FROM game_event WHERE ts >= ? AND ts < ? "
            "AND type = 'group' ORDER BY ts, id", window).fetchall():
        p = _object(row["payload"])
        members = p.get("members")
        if not isinstance(members, list) or not all(type(g) is int for g in members):
            continue
        if p.get("raid") is False and 2 <= len(members) <= PARTY_MAX:
            yield from _pairs("partied", members, "public", f"event:{row['id']}")


# -- scenes, feuds, feelings ---------------------------------------------------------------------

def _scenes(conn, night, window, cast):
    """Tonight's accepted scenes: a warm one (its applied changes sum above zero) draws both,
    and one that set a rumour about the other straight, to her face, is the corrector taking her
    side. A rumour set straight to someone else never reaches its subject (nothing stores that
    she learned of it), so it moves nothing."""
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
            if about is not None and about["about"] == s["b"] and s["b"] != cast.human:
                yield Event("took_side", s["b"], s["a"], "pair", ref)


def _feuds(conn, window, cast):
    for r in conn.execute("SELECT id, sides FROM feud WHERE started >= ? AND started < ? "
                          "ORDER BY id", window).fetchall():
        for ally, siders in _object(r["sides"]).items():
            if not (isinstance(ally, str) and ally.isdecimal() and isinstance(siders, list)):
                continue
            for s in siders:
                if type(s) is int and s != int(ally):
                    yield Event("took_side", int(ally), s, "public", f"feud:{r['id']}")


def _feelings(conn, night, cast):
    """Tonight's reflection left her admiring or grateful to another member, on memories that
    all read open by their effective scope (memory.privacy: a memory stored open that rests on a
    private or secret one reads as that, and a feeling resting on any such memory is never
    read). The feeling is hers alone and the other never saw it: scope `private`."""
    from leeroy_mind.memory.privacy import effective_scopes
    for r in conn.execute(
            "SELECT id, guid, target, type, intensity, evidence FROM emotion WHERE night = ? "
            "AND source = 'reflect' AND target > 0 ORDER BY id", (night,)).fetchall():
        if r["type"] not in WARM_FEELINGS or r["target"] == cast.human:
            continue
        try:
            refs = json.loads(r["evidence"])
        except (TypeError, ValueError):
            continue
        ids = [int(x[7:]) for x in refs if isinstance(x, str) and x.startswith("memory:")
               and x[7:].isdecimal()] if isinstance(refs, list) else []
        if not ids or len(ids) != len(refs):
            continue
        read = effective_scopes(conn, ids)
        if len(read) == len(set(ids)) and all(s == ("open", 0) for s in read.values()):
            yield Event("admired", r["guid"], r["target"], "private", f"emotion:{r['id']}",
                        KINDS["admired"][0] * (0.5 + 0.25 * r["intensity"]))


# -- chat and Discord ----------------------------------------------------------------------------

def _chat(conn, window, cast):
    """On readable channels only, and only for a member who could read the line (its stored
    audience): a member agreeing by name with a line another member just said (`heard`: the one
    agreed with), a member answering a taunt aimed at another (`defended`: the one it was aimed
    at), and, on the game's channels, a member naming another (`mentioned`: the one named;
    Discord's names are _discord's). The text is matched here and goes nowhere."""
    named = _named(cast)
    recent: dict[str, list] = {}
    for row in conn.execute(
            "SELECT id, ts, channel, from_guid, text, origin, audience FROM chat_line "
            "WHERE ts >= ? AND ts < ? ORDER BY ts, id", window).fetchall():
        channel, x, text = row["channel"], row["from_guid"], row["text"] or ""
        if not _readable(channel):
            continue
        before = [p for p in recent.get(channel, ()) if row["ts"] - p[0] <= REPLY_S]
        if row["origin"] == "mind" and x in cast.members:
            ref = f"chat:{row['id']}"
            agrees = AGREE_RE.search(text) and not DISAGREE_RE.search(text)
            defends = DEFEND_RE.search(text)
            done = set()
            for _, y, their_text in before:
                if (agrees and y != x and y in cast.members and ("heard", y) not in done
                        and y in named and named[y].search(text)
                        and _sees(row["audience"], y)):
                    done.add(("heard", y))
                    yield Event("heard", y, x, "public", ref)
                if not (defends and y != x and TAUNT_RE.search(their_text)):
                    continue
                for target, match in named.items():
                    if (target in (x, y) or target not in cast.members
                            or ("defended", target) in done or not match.search(their_text)
                            or not _sees(row["audience"], target)):
                        continue
                    if match.search(text) or (y in named and named[y].search(text)):
                        done.add(("defended", target))
                        yield Event("defended", target, x, "public", ref)
            if not is_discord(channel):
                for y, match in named.items():
                    if (y != x and y in cast.members and ("heard", y) not in done
                            and ("defended", y) not in done and match.search(text)
                            and _sees(row["audience"], y)):
                        yield Event("mentioned", y, x, "public", ref)
        before.append((row["ts"], x, text))
        recent[channel] = before[-RECENT_LINES:]


def _talk_channel(channel: str) -> str:
    return channel if channel.startswith((DM_PREFIX, GROUP_PREFIX)) else CHANNEL_PREFIX + channel


def _discord(conn, window, cast):
    """Members' messages on public Discord channels: a reply to another member's message within
    DISCORD_REPLY_S (`answered` for the one replied to, `replied` for the replier), a message
    right after another member's in the channel (`talked`, both), and a message naming another
    member (`mentioned`: the one named)."""
    named = _named(cast)
    last: dict[tuple, tuple] = {}
    public: dict[str, bool] = {}
    for m in conn.execute(
            "SELECT m.id, m.channel, m.thread, m.author, m.text, m.ts, p.author AS to_author, "
            "p.origin AS to_origin, p.ts AS to_ts FROM discord_message m "
            "LEFT JOIN discord_message p ON p.id = m.reply_to "
            "WHERE m.ts >= ? AND m.ts < ? AND m.origin = 'mind' AND m.deleted IS NULL "
            "ORDER BY m.ts, m.id", window).fetchall():
        channel = m["channel"]
        if channel not in public:
            public[channel] = _readable(_talk_channel(channel))
        if not public[channel]:
            continue
        x, ref = m["author"], f"discord:{m['id']}"
        said = set()
        if (m["to_author"] and m["to_origin"] == "mind" and m["to_author"] != x
                and m["ts"] - m["to_ts"] <= DISCORD_REPLY_S):
            said.add(m["to_author"])
            yield Event("answered", m["to_author"], x, "public", ref)
            yield Event("replied", x, m["to_author"], "public", ref)
        key = (channel, m["thread"])
        before = last.get(key)
        if before and before[0] != x and before[0] not in said and m["ts"] - before[1] <= TALK_S:
            said.add(before[0])
            yield Event("talked", x, before[0], "public", ref)
            yield Event("talked", before[0], x, "public", ref)
        last[key] = (x, m["ts"])
        text = m["text"] or ""
        for y, match in named.items():
            if y != x and y not in said and y in cast.members and match.search(text):
                yield Event("mentioned", y, x, "public", ref)


def _reactions(conn, window, cast):
    """A member's reaction to another member's public Discord line: `banter` when she spoke in
    that conversation herself (the same channel and thread, within BANTER_S), else `reacted`."""
    for r in conn.execute(
            "SELECT r.message, r.member, r.emoji, m.author, m.channel, m.thread, m.ts AS said "
            "FROM discord_reaction r JOIN discord_message m ON m.id = r.message "
            "WHERE r.ts >= ? AND r.ts < ? AND m.origin = 'mind' AND m.deleted IS NULL "
            "AND r.member != m.author ORDER BY r.ts, r.message, r.member", window).fetchall():
        if not _readable(_talk_channel(r["channel"])) or r["emoji"] in SOUR_EMOJI:
            continue
        if r["member"] not in cast.members or r["author"] not in cast.members:
            continue
        spoke = conn.execute(
            "SELECT 1 FROM discord_message WHERE channel = ? AND author = ? AND origin = 'mind' "
            "AND deleted IS NULL AND thread IS ? AND ts BETWEEN ? AND ? LIMIT 1",
            (r["channel"], r["member"], r["thread"], r["said"] - BANTER_S,
             r["said"] + BANTER_S)).fetchone()
        yield Event("banter" if spoke is not None else "reacted", r["member"], r["author"],
                    "public", f"discord:{r['message']}")


def _riffs(conn, window, cast):
    """Two members speaking in one banter riff (the `[banter]` threads: lines the talk pipeline
    sent as banter, RIFF_S or less apart on one readable channel), both ways (`riffed`). Each
    spoke in it, so each heard the other."""
    riffs, last = [], {}
    for r in conn.execute(
            "SELECT id, ts, guid, channel FROM utterance WHERE ts >= ? AND ts < ? "
            "AND why LIKE 'banter:%' AND dropped IS NULL ORDER BY ts, id", window).fetchall():
        if not _readable(r["channel"]) or r["guid"] not in cast.members:
            continue
        held = last.get(r["channel"])
        if held is None or r["ts"] - held[0] > RIFF_S:
            held = [r["ts"], r["id"], set()]
            riffs.append(held)
        held[0] = r["ts"]
        held[2].add(r["guid"])
        last[r["channel"]] = held
    for _, first, who in riffs:
        yield from _pairs("riffed", who, "public", f"utterance:{first}")


def _moves(conn, window, cast):
    """What the talk pipeline already scored about a member's public line at another member: a
    joke, tease or callback (`joked`, both) or a compliment (`complimented`: the one it was
    for). The one it was at must have been able to read it: the speaker's own line on that
    channel, within MOVE_LINE_S, carries its audience; a move whose line cannot be found proves
    nobody heard it and counts for nothing."""
    for r in conn.execute(
            "SELECT id, ts, guid, counterpart, channel, main FROM talk_move "
            "WHERE ts >= ? AND ts < ? AND counterpart > 0 AND counterpart != guid ORDER BY id",
            window).fetchall():
        if not _readable(r["channel"]) or r["main"] not in JOKE_MOVES | WARM_MOVES:
            continue
        line = conn.execute(
            "SELECT audience FROM chat_line WHERE from_guid = ? AND channel = ? "
            "AND origin = 'mind' AND ts BETWEEN ? AND ? ORDER BY abs(ts - ?), id LIMIT 1",
            (r["guid"], r["channel"], r["ts"] - MOVE_LINE_S, r["ts"] + MOVE_LINE_S,
             r["ts"])).fetchone()
        if line is None or not _sees(line["audience"], r["counterpart"]):
            continue
        ref = f"move:{r['id']}"
        if r["main"] in JOKE_MOVES:
            yield from _pairs("joked", (r["guid"], r["counterpart"]), "public", ref)
        elif r["main"] in WARM_MOVES:
            yield Event("complimented", r["counterpart"], r["guid"], "public", ref)


def _claims(conn, window, cast):
    """A good opinion a member voiced about another by name on a public channel, as the claims
    table scored it: `spoke_well` for the one spoken of, `praised` for the speaker."""
    by_name = {n.casefold(): g for g, n in cast.names.items()
               if isinstance(n, str) and g in cast.members}
    for r in conn.execute(
            "SELECT c.id, c.guid, c.subject, l.channel, l.audience FROM member_claim c "
            "JOIN chat_line l ON l.id = c.source_line WHERE c.first_seen >= ? AND c.first_seen < ? "
            "AND c.kind = 'opinion' AND c.polarity = 1 AND c.source = 'chat' ORDER BY c.id",
            window).fetchall():
        about = by_name.get(" ".join((r["subject"] or "").casefold().split()))
        if (about is None or about == r["guid"] or not _readable(r["channel"])
                or not _sees(r["audience"], about)):
            continue
        ref = f"claim:{r['id']}"
        yield Event("spoke_well", about, r["guid"], "public", ref)
        yield Event("praised", r["guid"], about, "public", ref)


# -- voice and DKP ---------------------------------------------------------------------------------

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
    """Two members in one public voice room for VOICE_MIN_S or more over the night
    (`voice_room`), or VOICE_LATE_S of it late on the holder's own clock (`voice_late`). A room
    that held more than VOICE_ROOM_MAX members that night is a crowd (the hall while a raid
    runs, the evening's silent seats) and counts for nobody; the officers' and the private
    rooms are never read."""
    rooms: dict[str, list] = {}
    for r in conn.execute(
            "SELECT id, guid, room, since, until FROM voice_presence WHERE since < ? "
            "AND coalesce(until, ?) > ? ORDER BY id", (window[1], window[1], window[0])).fetchall():
        if (r["guid"] not in cast.members or r["room"] in VOICE_SKIP
                or r["room"].startswith("private")):
            continue
        if r["until"] is None and r["since"] < window[0] - DAY_S:
            continue  # a row nobody closed is no evidence of a night in the room
        lo = max(r["since"], window[0])
        hi = min(r["until"] if r["until"] is not None else window[1], window[1])
        if hi > lo:
            rooms.setdefault(r["room"], []).append((r["guid"], lo, hi, r["id"]))
    late = {"UK": _late_spans(window, UK_ZONE), "US": _late_spans(window, US_ZONE)}
    seconds: dict[tuple, list] = {}
    for room, rows in sorted(rooms.items()):
        if len({g for g, _, _, _ in rows}) > VOICE_ROOM_MAX:
            if notes is not None:
                notes["voice_crowd"] += 1
            continue
        for i, (a, alo, ahi, aid) in enumerate(rows):
            for b, blo, bhi, bid in rows[i + 1:]:
                lo, hi = max(alo, blo), min(ahi, bhi)
                if a == b or hi <= lo:
                    continue
                for x, y, ident in ((a, b, aid), (b, a, bid)):
                    spans = late["UK" if cast.region.get(x) == "UK" else "US"]
                    slot = seconds.setdefault((x, y), [0.0, 0.0, ident])
                    slot[0] += hi - lo
                    slot[1] += sum(max(0.0, min(hi, e) - max(lo, s)) for s, e in spans)
                    slot[2] = min(slot[2], ident)
    for (x, y), (total, lately, ident) in sorted(seconds.items()):
        if lately >= VOICE_LATE_S:
            yield Event("voice_late", x, y, "public", f"voice:{ident}")
        elif total >= VOICE_MIN_S:
            yield Event("voice_room", x, y, "public", f"voice:{ident}")


def _dkp(conn, window, cast):
    """A close award (the runner-up lost by a little) is recorded as an outcome that moves
    nothing; respect either gained for the other within RESPECT_DAYS of it, and before this
    night's window ends (a replay never reads a later night's deltas), is worth a pull, once per
    pair and award. The respect is the holder's own, which the other never saw: `private`."""
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
                    "AND ts < ? LIMIT 1",
                    (x, y, when, when + RESPECT_DAYS * DAY_S, window[1])).fetchone():
                yield Event("dkp_respect", x, y, "private", ref)


def _mirrored(conn, chat) -> set:
    """{(a, b, "discord:<message>")} for each `heard` or `defended` _chat found on a Discord
    message's own chat line (meta discord:line:<message>): that message already counted for the
    pair, so _discord's events of the same pair for it are not counted again."""
    lines: dict[str, list] = {}
    for e in chat:
        if e.ref.startswith("chat:") and e.kind in ("heard", "defended"):
            lines.setdefault(e.ref[5:], []).append(e)
    if not lines:
        return set()
    out = set()
    for key, value in conn.execute(
            "SELECT key, value FROM meta WHERE key GLOB 'discord:line:*' "
            "AND value IN (SELECT value FROM json_each(?))", (json.dumps(sorted(lines)),)):
        for e in lines.get(str(value), ()):
            out.add((e.a, e.b, "discord:" + key[len("discord:line:"):]))
    return out


def collect(conn, night: str, window, cast: Cast, notes=None) -> list[Event]:
    """Every raw event of the night, in a stable order; no factor, cap or pairing rule yet.
    `notes` counts what a source passed over whole (a crowded voice room). One Discord message
    counts once for a pair: a `heard` or `defended` on its chat line, else _discord's events."""
    chat = list(_chat(conn, window, cast))
    counted = _mirrored(conn, chat)
    discord = (e for e in _discord(conn, window, cast) if (e.a, e.b, e.ref) not in counted)
    out = []
    for source in (_raids(conn, window, cast), _bosses(conn, window, cast),
                   _parties(conn, window, cast), _scenes(conn, night, window, cast),
                   _feuds(conn, window, cast), _feelings(conn, night, cast),
                   chat, discord,
                   _reactions(conn, window, cast), _riffs(conn, window, cast),
                   _moves(conn, window, cast),
                   _claims(conn, window, cast), _voice(conn, window, cast, notes),
                   _dkp(conn, window, cast)):
        out.extend(source)
    return out


# -- factors, the ledger and the shadow --------------------------------------------------------

def factor(cast: Cast, a: int) -> float:
    """What a gain of `a` is multiplied by: the household cost and, for a woman, her stance
    toward the owner, never below FACTOR_FLOOR together."""
    return max(FACTOR_FLOOR, (TAKEN_FACTOR if a in cast.taken else 1.0)
               * STANCE_FACTOR.get(cast.stance.get(a), 1.0))


def likeness(cast: Cast, a: int, b: int) -> float:
    """How alike two members' cards are, the same for both directions, LIKENESS_MIN to
    LIKENESS_MAX: region, night rhythm, humour, what they do outside raids, likes, and a small
    draw seeded by the pair."""
    ta, tb = cast.traits.get(a), cast.traits.get(b)
    if ta is None or tb is None:
        return 1.0
    lo, hi = min(a, b), max(a, b)
    acts, likes = len(ta[3] & tb[3]), len(ta[4] & tb[4])
    value = (1.0 + 0.05 * (ta[0] == tb[0]) + 0.05 * (ta[1] == tb[1]) + 0.04 * (ta[2] == tb[2])
             + 0.05 * min(2, acts) + 0.04 * min(2, likes)
             - 0.08 * (acts == 0) - 0.06 * (ta[0] != tb[0] and ta[1] != tb[1])
             + random.Random(f"likeness|{lo}|{hi}").uniform(-0.04, 0.04))
    return round(max(LIKENESS_MIN, min(LIKENESS_MAX, value)), 3)


def _nights(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def decayed(pull: float, idle_nights: int) -> float:
    """A pull after `idle_nights` nights without an event."""
    return pull * DECAY ** max(0, idle_nights)


def _ledger(conn, night, events, cast, now):
    """The night's rows: man-woman pairs of the cast only, each kind's cap, presence's
    saturation, the factors, the night cap. Returns ({(a, b): [amount, public amount, social
    amount, events]}, kinds, scopes, skipped)."""
    gains, kinds, scopes, skipped = {}, Counter(), Counter(), Counter()
    seen, per_kind, presence, alike = set(), Counter(), {}, {}
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
        pair = (e.a, e.b)
        if per_kind[(e.a, e.b, e.kind)] >= KINDS[e.kind][1]:
            skipped["kind_cap"] += 1
            continue
        weight = KINDS[e.kind][0] if e.weight is None else e.weight
        mult = factor(cast, e.a)
        both = (min(pair), max(pair))
        if both not in alike:
            alike[both] = likeness(cast, *both)
        slot = gains.setdefault(pair, [0.0, 0.0, 0.0, 0])
        amount = weight * mult * alike[both]
        if e.kind in PRESENCE:
            had = presence.setdefault(pair, [0, 0.0])
            if had[0]:
                weight *= PRESENCE_LATER
                amount *= PRESENCE_LATER
            amount = min(amount, max(0.0, PRESENCE_CAP - had[1]))
        amount = min(amount, max(0.0, NIGHT_CAP - slot[0]))
        if weight > 0 and amount <= 0:
            skipped["presence_cap" if e.kind in PRESENCE else "night_cap"] += 1
            continue
        if e.kind in PRESENCE:
            presence[pair][0] += 1
            presence[pair][1] += amount
        per_kind[(e.a, e.b, e.kind)] += 1
        conn.execute(
            "INSERT INTO pull_event(night, a, b, kind, weight, factor, likeness, amount, scope, "
            "ref, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (night, e.a, e.b, e.kind, weight, mult, alike[both], amount, e.scope, e.ref, now))
        kinds[e.kind] += 1
        scopes[e.scope] += 1
        if amount > 0:
            slot[0] += amount
            slot[1] += amount if e.scope == "public" else 0.0
            slot[2] += 0.0 if e.kind in PRESENCE else amount
            slot[3] += 1
    return {k: v for k, v in gains.items() if v[3]}, kinds, scopes, skipped


def _shadow(conn, night, gains):
    """Decay every stored pull for its nights without an event, then add tonight's gains. The
    public and the social part each decay by their own nights without a gain of their kind, so
    a pair that only meets in private scenes keeps no public part it no longer earns."""
    rows = {(r["a"], r["b"]): r for r in conn.execute("SELECT * FROM pull_shadow").fetchall()}
    for pair in sorted(set(rows) | set(gains)):
        row, gain = rows.get(pair), gains.get(pair)
        if row is None:
            conn.execute(
                "INSERT INTO pull_shadow(a, b, pull, public_pull, social_pull, events, "
                "first_night, last_event_night, updated_night) VALUES (?,?,?,?,?,?,?,?,?)",
                (*pair, min(100.0, gain[0]), min(100.0, gain[1]), min(100.0, gain[2]), gain[3],
                 night, night, night))
            continue
        gap = _nights(row["updated_night"], night)
        part = gain[:3] if gain else (0.0, 0.0, 0.0)
        values = [min(100.0, decayed(row[c], gap - (1 if g > 0 else 0)) + g)
                  for c, g in zip(("pull", "public_pull", "social_pull"), part)]
        conn.execute(
            "UPDATE pull_shadow SET pull = ?, public_pull = ?, social_pull = ?, "
            "events = events + ?, last_event_night = ?, updated_night = ? WHERE a = ? AND b = ?",
            (*values, gain[3] if gain else 0,
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


def admit(candidates, live, *, threads_max=None, taken_max=None, opens_max=None):
    """Which waiting candidates the caps would let open, rarest shape among the live threads
    first (counted again after each opening), then the stronger pull. `candidates` and `live`
    are dicts with a, b, pull, shape, taken. Returns (opened, {(a, b): why})."""
    threads_max = THREADS_MAX if threads_max is None else threads_max
    taken_max = TAKEN_MAX if taken_max is None else taken_max
    opens_max = OPENS_PER_NIGHT if opens_max is None else opens_max
    live = list(live)
    opened, why = [], {}
    counts = Counter(t["shape"] for t in live)
    pool = list(candidates)
    while pool:
        c = min(pool, key=lambda c: (counts[c["shape"]], -c["pull"], c["a"], c["b"]))
        pool.remove(c)
        busy = {g for t in live for g in (t["a"], t["b"])}
        if c["a"] in busy or c["b"] in busy:
            why[(c["a"], c["b"])] = "cap_member"
        elif c["taken"] and sum(1 for t in live if t["taken"]) >= taken_max:
            why[(c["a"], c["b"])] = "cap_taken"
        elif len(live) >= threads_max:
            why[(c["a"], c["b"])] = "cap_live"
        elif len(opened) >= opens_max:
            why[(c["a"], c["b"])] = "cap_night"
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
    from leeroy_mind.drama.couples import ACTIVE
    # a member couple past noticing each other makes the other of it a third
    couple = tuple(s for s in ACTIVE if s != "notices")
    rows = {(r["a"], r["b"]): dict(r) for r in conn.execute("SELECT * FROM pull_shadow").fetchall()}
    strong = [k for k, r in rows.items() if r["pull"] >= TRIANGLE_PULL]
    late = {g for g in cast.members if cast.gender.get(g) == "female" and _one(
        conn, "SELECT romance_stage FROM relationship WHERE a = ? AND b = ?",
        (g, cast.human), "none") in HIS_LATE}
    marks = ",".join("?" * len(couple))

    def couple_with_other(g, other):
        return conn.execute(
            f"SELECT 1 FROM relationship WHERE a = ? AND b NOT IN (?, ?, ?) "
            f"AND romance_stage IN ({marks}) LIMIT 1",
            (g, cast.human, other, g, *couple)).fetchone() is not None

    def affinity(a, b):
        return _one(conn, "SELECT affinity FROM relationship WHERE a = ? AND b = ?", (a, b))

    def facts(a, b):
        r = rows[(a, b)]
        third = (any(x not in (a, b) and y in (a, b) for x, y in strong)
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
        elif r["social_pull"] < OPEN_SOCIAL:
            why = "social"
        elif min(affinity(a, b), affinity(b, a)) < OPEN_AFFINITY:
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

    def tops(gender):
        return round(max((r["pull"] for r in conn.execute("SELECT a, pull FROM pull_shadow")
                          if cast.gender.get(r["a"]) == gender), default=0.0), 1)

    summary = {"events": sum(kinds.values()), "kinds": dict(sorted(kinds.items())),
               "scopes": dict(sorted(scopes.items())), "skipped": dict(sorted(skipped.items())),
               "pairs": len(gains), "members": len(cast.members), "taken": len(cast.taken),
               **_threads(conn, night, cast, now, rng)}
    summary["at"] = {str(t): conn.execute("SELECT count(*) FROM pull_shadow WHERE pull >= ?",
                                          (t,)).fetchone()[0] for t in (10, 20, 35)}
    summary["max"] = {"men": tops("male"), "women": tops("female")}
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


def attach_night(runner, settings) -> bool:
    """`nightly run`'s own runner: the stage when that night's settings say record."""
    if getattr(settings, "drama_threads", "off") != "record":
        return False
    runner.add_stage(STAGE, stage_pulls, after="encounters", model=None)
    return True
