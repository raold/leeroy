"""WoW grounding for raid and guild turns ([talk] wow_grounding, default true; owner, 2026-11-20:
the live model changed from Orion to leeroy-dark-scarlett-vision, whose private talk is better and
whose WoW talk is worse). The prompt gets facts and her own earlier lines instead of leaning on the
model's game knowledge, in three sections after the awareness card, in this order of priority:

- the card (`card_lines`, CARD_TOKENS with its heading): her class, spec (the talent tree with the
  most points, as native reports it) and role; with a guild raid running or ended within
  RECENT_RAID_S, its instance and difficulty, the boss being fought or the last one with up to
  two playbook lines for her role (raidplay's `explain`, whole, never cut), the last wipe's top
  causes (the training ground's tg_ tables and stored spell names, as `training hurt` reads
  them), her loot and her DKP; otherwise the guild's next raid night and its last kills. Facts
  only, from stored rows: a line whose source is missing is left out, never guessed;
- her example lines (`exemplars`, EXEMPLAR_TOKENS): two or three of her own lines from before the
  model switch ([talk] wow_exemplars_before), typed on the guild's public game channels (guild,
  raid, party) or its public WoW Discord channels (officer chat too on an officers' turn of an
  officer), ranked by the owner's thumbs, then the learn rewards, then the owner's replies. Each
  is screened: no names, links, stock or slop phrase (guards/phrases.py), no model talk, no later
  era, no line a memory of narrower scope cites (memory.privacy, #372). The guards are told
  which were shown (Draft.exemplars): a draft that copies one is `phrase_exemplar`;
- the glossary (`glossary`, GLOSSARY_TOKENS): fixed raid words, and the guild's own active slang
  from the lexicon, screened as the lexicon is (its era and everyday-word screens, its origin's
  scope) and never a game name.

Only on WoW turns (`wow_turn`): game guild, officer, party and raid chat, the guild's raiding,
WoW, class, schedule, announcement and officer Discord channels, and the raid, role, class,
loot, strategy and officers' voice rooms. Never a whisper, a DM, a group DM, say or yell or any
other channel, so those prompts are byte-identical with it on. Nothing here writes, calls a
model or logs text; the reads are bounded and read-only."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field, replace

from leeroy_mind.text import estimate_tokens, has_meta, strip_wow_markup, word_jaccard

CARD_TOKENS = 120
GLOSSARY_TOKENS = 40
EXEMPLAR_TOKENS = 60
RECENT_RAID_S = 24 * 3600
OPEN_RAID_S = 12 * 3600
EXEMPLAR_MIN_AGE_S = 6 * 3600  # her history, never the conversation going on (own_recent's)
EXEMPLARS_MAX, EXEMPLAR_POOL = 3, 9  # shown, and the best of hers they rotate through by day
EXEMPLAR_WORDS = (4, 18)
EXEMPLAR_CHARS = (15, 110)
EXEMPLAR_SCAN = 400  # her newest lines read before the switch
EXEMPLAR_JACCARD = 0.5  # two shown lines never this alike
EXPLAINS = 2
SLANG_TERMS = 4

CARD_HEAD = "Raid and guild (facts; keep to them, never invent more):"
EXEMPLAR_HEAD = "How you talk in raid/guild (your own past lines):"
GLOSSARY = ("Raid words: pull (start a fight), wipe (all dead), trash (mobs before a boss), "
            "cds (cooldowns), brez, oom, bis, ms/os, dkp, gz.")
SLANG_HEAD = " Guild slang: "

GAME = frozenset({"guild", "officer", "party", "raid", "raid_warning"})
OFFICER_CHANNELS = frozenset({"officer", "voice:officers", "discord:officer-chat"})
# The voice rooms whose talk is the raid's or the guild's business (voice/rooms.py)
ROOMS = frozenset({"raid", "raid2", "tanks", "healers", "casters", "melee", "loot_dkp", "lore",
                   "officers", "warriors", "paladins", "hunters", "rogues", "priests",
                   "death_knights", "shamans", "mages", "warlocks", "druids"})
# Example lines come from the guild's public typed channels only
EXEMPLAR_GAME = ("guild", "raid", "party")
EXEMPLAR_OFFICER = ("officer", "discord:officer-chat")


def _discord_wow() -> frozenset[str]:
    from leeroy_mind.discord import layout
    return frozenset(c.id for c in layout.CHANNELS
                     if c.category in ("raiding", "wow")
                     or c.id in ("raid-schedule", "announcements", "officer-chat"))


DISCORD = _discord_wow()
EXEMPLAR_DISCORD = tuple(sorted(f"discord:{c}" for c in DISCORD if c != "officer-chat"))


def wow_turn(channel: str) -> bool:
    """A turn the grounding is for (the module docstring): never a private or group one."""
    if not isinstance(channel, str):
        return False
    if channel in GAME:
        return True
    if channel.startswith("voice:"):
        return channel.removeprefix("voice:") in ROOMS
    if channel.startswith("discord:"):
        return channel.removeprefix("discord:") in DISCORD
    return False


def medium(channel: str) -> str:
    return "discord" if channel.startswith("discord:") else "game"


@dataclass(frozen=True)
class Speaker:
    """What the loop knows of her, captured before the read: class and talent tab as native last
    reported them (0 / -1 unknown), her persona's raid role, whether she is an officer, his guid
    and the names a line may show."""
    guid: int
    cls: int = 0
    spec_tab: int = -1
    raid_role: str | None = None
    officer: bool = False
    human: int = 0
    names: dict[int, str] = field(default_factory=dict)
    # the names her example lines must not hold (members', his), folded
    protect: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grounding:
    card: tuple[str, ...] = ()
    exemplars: tuple[str, ...] = ()
    glossary: str = ""

    def sections(self) -> list[str]:
        """The rendered sections, highest priority first (the trim drops from the end)."""
        out = []
        if self.card:
            out.append(_section(CARD_HEAD, self.card))
        if self.exemplars:
            out.append(_section(EXEMPLAR_HEAD, self.exemplars))
        if self.glossary:
            out.append(self.glossary)
        return out


def _section(head: str, lines) -> str:
    return head + "\n" + "\n".join("- " + x for x in lines)


class _Reader:
    """Plain SELECTs; a table or column an older mind.db lacks reads as nothing."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def rows(self, sql: str, args=()) -> list[sqlite3.Row | tuple]:
        try:
            cur = self.conn.execute(sql, args)
            keys = [c[0] for c in cur.description]
            return [dict(zip(keys, r)) for r in cur.fetchall()]
        except sqlite3.OperationalError as e:
            if str(e).startswith(("no such table:", "no such column:")):
                return []
            raise


def _json(raw, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return default
    return value if isinstance(value, type(default)) else default


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        n, unit = max(1, s // 60), "minute"
    elif s < 86400:
        n, unit = s // 3600, "hour"
    else:
        n, unit = s // 86400, "day"
    return f"{n} {unit}{'s' if n != 1 else ''}"


def _ordinal(n: int) -> str:
    tail = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{tail}"


def _difficulty_words(size: int, heroic: bool) -> str:
    return f"{size}{' heroic' if heroic else ''}"


# -- the card -----------------------------------------------------------------------------------

def _role(sp: Speaker) -> tuple[str, str]:
    """(her role in words, her playbook role) from her class, talent tab and raid role."""
    from leeroy_mind.guild.roles import role_of
    from leeroy_mind.raidlead.briefs import playbook_role
    role = role_of(sp.cls, sp.spec_tab, sp.raid_role)
    book = playbook_role(role, sp.cls, sp.spec_tab)
    words = role if role in ("tank", "healer") else f"{book} damage"
    return words, book


def _you(sp: Speaker) -> str | None:
    from leeroy_mind.guild.roles import spec_name
    from leeroy_mind.wow import CLASSES
    cls = CLASSES.get(sp.cls)
    if not cls:
        return None
    spec = spec_name(sp.cls, sp.spec_tab) if sp.spec_tab >= 0 else None
    words, _ = _role(sp)
    who = f"{spec.title()} {cls}" if spec else cls
    return f"You: {who}, {words}."


def _recent_raid(r: _Reader, now: float):
    # a row still open after OPEN_RAID_S is a raid nobody closed (health fails one past 6 h),
    # never one running now
    rows = r.rows("SELECT raid_id,started,ended,raid_key,size,heroic,kills,wipes,lead_guid,tank_guid "
                  "FROM guild_raid "
                  "WHERE started<=? AND ((ended IS NULL AND started>=?) OR ended>=?) "
                  "ORDER BY (ended IS NULL) DESC,started DESC,raid_id DESC LIMIT 1",
                  (now, now - OPEN_RAID_S, now - RECENT_RAID_S))
    return rows[0] if rows else None


def _raid_line(r: _Reader, raid, sp: Speaker, now: float) -> str | None:
    from leeroy_mind.guild.raids import CATALOG
    known = CATALOG.get(raid["raid_key"])
    if known is None or type(raid["size"]) is not int:
        return None
    what = f"{known.name} {_difficulty_words(raid['size'], bool(raid['heroic']))}"
    tally = (f"{raid['kills']} kill{'s' if raid['kills'] != 1 else ''}, "
             f"{raid['wipes']} wipe{'s' if raid['wipes'] != 1 else ''}")
    seat = r.rows("SELECT status FROM guild_attendance WHERE raid_id=? AND guid=?",
                  (raid["raid_id"], sp.guid))
    you = ({"raided": "; you're in it", "benched": "; you're on the bench"}
           .get(seat[0]["status"], "") if seat else "")
    if raid["ended"] is None:
        return f"Raid now: {what}, {tally} so far{you}."
    you = you.replace("you're in it", "you were in it").replace("you're on", "you were on")
    return f"Last raid ({_ago(now - raid['ended'])} ago): {what}, {tally}{you}."


def _meta(r: _Reader, key: str):
    """A meta value as guild/loot.py's _put stores it (JSON of any type); None when absent or
    unreadable."""
    rows = r.rows("SELECT value FROM meta WHERE key=?", (key,))
    try:
        return json.loads(rows[0]["value"]) if rows else None
    except (TypeError, ValueError, RecursionError):
        return None


def _raid_members(r: _Reader, raid) -> set[int]:
    """Who the guild raid holds: its stored plan's members, its attendance and its lead and
    tank (guild/session.py)."""
    plan = _meta(r, f"guild:raid:{raid['raid_id']}:plan")
    members = set(plan.get("members") or ()) if isinstance(plan, dict) else set()
    members |= {x["guid"] for x in r.rows("SELECT guid FROM guild_attendance WHERE raid_id=? "
                                          "AND status='raided'", (raid["raid_id"],))}
    members |= {raid.get("lead_guid"), raid.get("tank_guid")}
    return {g for g in members if type(g) is int and g > 0}


def _boss(r: _Reader, raid, now: float):
    """(map, boss index, state, at) of the guild raid's newest boss event, or None: an event in
    the instance the raid is bound to (meta guild:raid:<id>:instance, as guild/session.py binds
    it), or, before it is bound, one whose members include the raid's; another group's fight on
    the same map in the same hours is never the guild's."""
    from leeroy_mind.guild.raids import CATALOG
    known = CATALOG.get(raid["raid_key"])
    if known is None:
        return None
    bound = _meta(r, f"guild:raid:{raid['raid_id']}:instance")
    bound = bound if type(bound) is int and bound > 0 else None
    members = _raid_members(r, raid) if bound is None else set()
    if bound is None and not members:
        return None
    end = raid["ended"] if raid["ended"] is not None else now
    start = max(raid["started"], now - RECENT_RAID_S)
    rows = r.rows("SELECT ts,payload FROM game_event WHERE type='boss' AND ts BETWEEN ? AND ? "
                  "ORDER BY ts DESC,id DESC LIMIT 64", (start, end))
    for row in rows:
        ev = _json(row["payload"], {})
        if (ev.get("map") != known.map_id or type(ev.get("boss_id")) is not int
                or ev.get("state") not in ("engaged", "killed", "wiped")):
            continue
        if bound is not None and ev.get("instance_id") != bound:
            continue
        if bound is None and not members & {g for g in ev.get("members") or () if type(g) is int}:
            continue
        return known.map_id, ev["boss_id"], ev["state"], row["ts"]
    return None


def _boss_lines(r: _Reader, raid, sp: Speaker, now: float) -> tuple[str | None, list[str]]:
    from leeroy_mind.wow import boss_name
    found = _boss(r, raid, now)
    if found is None:
        return None, []
    map_id, boss_id, state, _ = found
    name = boss_name(map_id, boss_id)
    if name == "the boss":
        return None, []
    head = (f"Fighting now: {name}." if state == "engaged" and raid["ended"] is None
            else f"Last boss: {name}, {'killed' if state == 'killed' else 'a wipe'}.")
    return head, _explains(map_id, boss_id, raid, sp, name)


def _explains(map_id: int, boss_id: int, raid, sp: Speaker, name: str) -> list[str]:
    from leeroy_mind.raidlead.briefs import _pick, difficulty
    try:
        from leeroy_mind import raidplay
        books = raidplay.load()
    except Exception:
        return []
    diff = difficulty(raid["size"], bool(raid["heroic"]))
    book = books.playbook_for(map_id, boss_id, diff) if diff is not None else None
    if book is None:
        return []
    _, role = _role(sp)
    out = []
    for m in _pick(book, books.raids[map_id].difficulties, (role,))[:EXPLAINS]:
        text = " ".join(m.explain.split())
        if text:
            out.append(f"{name}, {m.name}: {text if text[-1:] in '.!?' else text + '.'}")
    return out


def _wipe_line(r: _Reader, raid, sp: Speaker, now: float) -> str | None:
    """The guild raid's newest complete wiped pull within RECENT_RAID_S (tg_pull.raid_id is the
    guild raid's; his raids and offline raids only, never a benchmark's or another's): its
    killing spells and its failed mechanics, his own left out (training/hurt.py's reading)."""
    from leeroy_mind.training.hurt import MELEE, stored_names
    from leeroy_mind.wow import boss_name
    pulls = r.rows("SELECT id,map,boss_id,end_ms FROM tg_pull WHERE outcome='wiped' AND complete=1 "
                   "AND raid_id=? AND kind IN ('his','offline') AND end_ms>=? "
                   "ORDER BY end_ms DESC,id DESC LIMIT 1",
                   (raid["raid_id"], int((now - RECENT_RAID_S) * 1000)))
    if not pulls:
        return None
    pull = pulls[0]
    try:
        names = stored_names(r.conn)
    except sqlite3.Error:
        names = {}
    deaths = r.rows("SELECT spell,count(*) AS n FROM tg_death WHERE pull=? AND guid<>? "
                    "GROUP BY spell ORDER BY n DESC,spell LIMIT 2", (pull["id"], sp.human))
    causes = []
    for d in deaths:
        spell = d["spell"] if type(d["spell"]) is int else 0
        label = MELEE if spell == 0 else names.get(spell)
        if label:
            causes.append(f"{label} ({d['n']})")
    failed = r.rows("SELECT key,sum(count) AS n FROM tg_failure WHERE pull=? AND count>0 AND guid<>? "
                    "GROUP BY key ORDER BY n DESC,key LIMIT 1", (pull["id"], sp.human))
    missed = ""
    if failed and isinstance(failed[0]["key"], str):
        missed = _mechanic_name(failed[0]["key"])
    if not causes and not missed:
        return None
    what = [*(["most deaths to " + ", ".join(causes)] if causes else []),
            *([f"missed {missed}"] if missed else [])]
    return (f"Last wipe ({boss_name(pull['map'], pull['boss_id'])}, "
            f"{_ago(now - pull['end_ms'] / 1000)} ago): " + "; ".join(what) + ".")


def _mechanic_name(key: str) -> str:
    try:
        from leeroy_mind import raidplay
        m = raidplay.mechanic(key)
    except Exception:
        m = None
    if m is not None:
        return m.name
    return " ".join(key.replace("_", " ").split())[:60]


def _loot_line(r: _Reader, raid, sp: Speaker) -> str | None:
    """Her own part in the raid's loot (guild:loot:<raid>:* receipts): an item she won, else one
    she wanted that went to someone else."""
    prefix = f"guild:loot:{raid['raid_id']}:"
    rows = r.rows("SELECT key,value FROM meta WHERE key>=? AND key<? ORDER BY key",
                  (prefix, prefix[:-1] + ";"))
    won, lost = [], []
    for row in rows:
        rec = _json(row["value"], {})
        item = rec.get("name")
        if not isinstance(item, str) or not item.strip() or "|" in item:
            continue
        item = item.strip()[:60]
        award = rec.get("award") if isinstance(rec.get("award"), dict) else {}
        winner, paid = award.get("guid"), award.get("paid")
        if winner == sp.guid:
            won.append((item, paid))
            continue
        wanted = any(isinstance(b, dict) and b.get("guid") == sp.guid and b.get("wanted") is True
                     for b in rec.get("bidders") or ())
        if (award.get("runner_up") == sp.guid or wanted) and type(winner) is int:
            lost.append((item, sp.names.get(winner, "someone")))
    if won:
        item, paid = won[-1]
        price = f" for {paid:g} DKP" if type(paid) in (int, float) and math.isfinite(paid) else ""
        return f"Your loot: you won {item}{price}."
    if lost:
        item, who = lost[-1]
        return f"Your loot: {item} went to {who}; you wanted it."
    return None


def _dkp_line(r: _Reader, sp: Speaker) -> str | None:
    rows = r.rows("WITH b AS (SELECT guid,sum(delta) AS balance FROM dkp_ledger GROUP BY guid) "
                  "SELECT balance,1+(SELECT count(*) FROM b o WHERE o.balance>b.balance) AS place,"
                  "(SELECT count(*) FROM b) AS n FROM b WHERE guid=?", (sp.guid,))
    if not rows or type(rows[0]["balance"]) not in (int, float):
        return None
    row = rows[0]
    return f"Your DKP: {row['balance']:g}, {_ordinal(row['place'])} of {row['n']}."


def _next_raid_line(r: _Reader, sp: Speaker, now: float) -> str | None:
    rows = r.rows("SELECT id,at,event_id FROM realm_calendar WHERE kind='raid' AND state='posted' "
                  "AND at>=? ORDER BY at,id LIMIT 1", (now,))
    if not rows:
        return None
    row = rows[0]
    you = ""
    if type(row["event_id"]) is int:
        rsvp = r.rows("SELECT status FROM realm_rsvp WHERE event=? AND guid=?",
                      (row["event_id"], sp.guid))
        if rsvp and isinstance(rsvp[0]["status"], str):
            status = " ".join(rsvp[0]["status"].replace("_", " ").split())[:24]
            you = f"; you: {status}" if status else ""
    return f"Next raid night: in {_ago(row['at'] - now)}{you}."


def _last_kills_line(r: _Reader, now: float) -> str | None:
    from leeroy_mind.guild.raids import CATALOG
    rows = r.rows("SELECT raid_key,size,heroic,kills,started,ended FROM guild_raid "
                  "WHERE kills>0 AND started<=? ORDER BY started DESC,raid_id DESC LIMIT 1", (now,))
    if not rows:
        return None
    row = rows[0]
    known = CATALOG.get(row["raid_key"])
    if known is None or type(row["size"]) is not int:
        return None
    when = row["ended"] if row["ended"] is not None else row["started"]
    return (f"Last kills: {known.name} {_difficulty_words(row['size'], bool(row['heroic']))}, "
            f"{row['kills']} boss{'es' if row['kills'] != 1 else ''}, {_ago(now - when)} ago.")


def card_lines(conn: sqlite3.Connection, sp: Speaker, now: float, *,
               budget: int = CARD_TOKENS) -> list[str]:
    """Her card's lines in display order, fitted whole into `budget` with CARD_HEAD: lines are
    taken by priority and one that does not fit is left out, never cut."""
    r = _Reader(conn)
    you = _you(sp)
    raid = _recent_raid(r, now)
    order: list[tuple[int, str]] = []  # (priority, line), display order
    if you:
        order.append((0, you))
    if raid is not None:
        head = _raid_line(r, raid, sp, now)
        boss, explains = _boss_lines(r, raid, sp, now)
        if head:
            order.append((1, head))
        if boss:
            order.append((2, boss))
        for i, x in enumerate(explains):
            order.append((3 if i == 0 else 8, x))
        wipe = _wipe_line(r, raid, sp, now)
        if wipe:
            order.append((4, wipe))
        loot = _loot_line(r, raid, sp)
        if loot:
            order.append((5, loot))
        dkp = _dkp_line(r, sp)
        if dkp:
            order.append((6, dkp))
    else:
        nxt = _next_raid_line(r, sp, now)
        if nxt:
            order.append((1, nxt))
        kills = _last_kills_line(r, now)
        if kills:
            order.append((2, kills))
        dkp = _dkp_line(r, sp)
        if dkp:
            order.append((3, dkp))
    chosen: set[int] = set()
    for i, _ in sorted(enumerate(order), key=lambda x: (x[1][0], x[0])):
        trial = [line for j, (_, line) in enumerate(order) if j in chosen or j == i]
        if estimate_tokens(_section(CARD_HEAD, trial)) <= budget:
            chosen.add(i)
    return [line for j, (_, line) in enumerate(order) if j in chosen]


# -- the glossary -------------------------------------------------------------------------------

def _slang(conn: sqlite3.Connection) -> list[str]:
    """The guild's own active slang, most used first, screened as the lexicon screens it: its era
    (nightly.lexicon.era_ok), an everyday word is never slang, no model talk, no name of the game,
    and nothing whose origin rests on a private or restricted line (frozen_origin's reading)."""
    from leeroy_mind.guards.phrases import _words, game_names
    from leeroy_mind.memory.privacy import derive_scope, restricted_channels
    from leeroy_mind.nightly.lexicon import era_ok
    from leeroy_mind.talk.everyday import everyday_reason
    r = _Reader(conn)
    rows = r.rows("SELECT id,term,originator,origin_ref FROM lexicon WHERE kind='slang' "
                  "AND status='active' ORDER BY uses DESC,id LIMIT 40")
    games = game_names()
    out = []
    for row in rows:
        term = " ".join(strip_wow_markup(row["term"] or "").split())
        words = term.casefold().split()
        if (not term or len(term) > 24 or not era_ok(term) or has_meta(term) or "|" in term
                or _words(term) in games or (len(words) == 1 and everyday_reason(words[0]))):
            continue
        saved = r.rows("SELECT value FROM meta WHERE key=?", (f"nightly:lexicon-sources:{row['id']}",))
        extra = _json(saved[0]["value"], []) if saved else []
        if not all(isinstance(x, str) for x in extra):
            continue
        refs = list(dict.fromkeys([row["origin_ref"], *extra]))
        try:
            if (derive_scope(conn, row["originator"], refs).scope != "open"
                    or restricted_channels(conn, {row["id"]: refs})):
                continue
        except sqlite3.Error:
            continue
        out.append(term.casefold())
        if len(out) >= SLANG_TERMS:
            break
    return out


def glossary(conn: sqlite3.Connection, *, budget: int = GLOSSARY_TOKENS) -> str:
    """GLOSSARY, then as many of the guild's own slang terms as fit `budget`."""
    text = GLOSSARY
    terms = []
    for term in _slang(conn):
        trial = GLOSSARY + SLANG_HEAD + ", ".join([*terms, term]) + "."
        if estimate_tokens(trial) <= budget:
            terms.append(term)
    if terms:
        text = GLOSSARY + SLANG_HEAD + ", ".join(terms) + "."
    return text if estimate_tokens(text) <= budget else ""


# -- her example lines --------------------------------------------------------------------------

_LINK = re.compile(r"\[|\]|\{\{|https?://|www\.|\|", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    id: int
    ts: float
    channel: str
    text: str
    score: float


def candidates(conn: sqlite3.Connection, sp: Speaker, *, officer_turn: bool, before: float,
               now: float) -> list[Candidate]:
    """Her screened example lines, best first (the module docstring): thumbs, then rewards,
    then his replies, then the newest."""
    from leeroy_mind.guards import phrases
    from leeroy_mind.nightly.lexicon import era_ok
    r = _Reader(conn)
    channels = [*EXEMPLAR_GAME, *EXEMPLAR_DISCORD]
    if officer_turn and sp.officer:
        channels += list(EXEMPLAR_OFFICER)
    cutoff = min(before, now - EXEMPLAR_MIN_AGE_S)
    marks = ",".join("?" * len(channels))
    rows = r.rows(
        f"SELECT id,ts,channel,text FROM chat_line WHERE from_guid=? AND origin='mind' AND ts<? "
        f"AND channel IN ({marks}) ORDER BY ts DESC,id DESC LIMIT ?",
        (sp.guid, cutoff, *channels, EXEMPLAR_SCAN))
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    blob = json.dumps(ids)
    rewards = {x["line_id"]: x["value"] for x in r.rows(
        "SELECT line_id,value FROM talk_reward WHERE line_id IN (SELECT value FROM json_each(?))",
        (blob,))}
    thumbs, replied = {}, {}
    for x in r.rows("SELECT line_id,kind,source,value FROM talk_signal WHERE line_id IN "
                    "(SELECT value FROM json_each(?)) AND kind IN ('explicit','replied',"
                    "'thread_continued')", (blob,)):
        if x["kind"] == "explicit" and x["source"] == "explicit":
            thumbs[x["line_id"]] = x["value"]
        elif x["value"] and x["value"] > 0:
            replied[x["line_id"]] = replied.get(x["line_id"], 0) + 1
    templates = {x["line_id"] for x in r.rows(
        "SELECT line_id FROM talk_recipe WHERE model_id='none' AND line_id IN "
        "(SELECT value FROM json_each(?))", (blob,))}
    narrow = _narrow_lines(conn, ids)
    names = sorted({n for n in sp.protect if n}, key=len, reverse=True)
    named = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)")
             if names else None)
    out = []
    for row in rows:
        lid = row["id"]
        thumb, reward = thumbs.get(lid, 0), rewards.get(lid, 0)
        if lid in templates or lid in narrow or (thumb or 0) < 0 or (reward or 0) < 0:
            continue
        text = " ".join(strip_wow_markup(row["text"] or "").split())
        words = text.split()
        if not (EXEMPLAR_WORDS[0] <= len(words) <= EXEMPLAR_WORDS[1]
                and EXEMPLAR_CHARS[0] <= len(text) <= EXEMPLAR_CHARS[1]):
            continue
        if _LINK.search(text) or has_meta(text) or not era_ok(text) or "\n" in text:
            continue
        if named is not None and named.search(text.casefold()):
            continue
        if phrases._stock_spans(phrases.tokens(text)):
            continue
        score = 2.0 * (thumb or 0) + (reward or 0) + 0.1 * replied.get(lid, 0)
        out.append(Candidate(lid, row["ts"], row["channel"], text, score))
    return sorted(out, key=lambda c: (-c.score, -c.ts, -c.id))


def _narrow_lines(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """The lines a memory cites whose effective scope (memory.privacy, #372) is not open: never
    an example, though the line itself was public."""
    from leeroy_mind.memory.privacy import effective_scopes
    try:
        cites = conn.execute("SELECT src,dst FROM memory_cite WHERE kind='chat' AND dst IN "
                             "(SELECT value FROM json_each(?))", (json.dumps(ids),)).fetchall()
    except sqlite3.OperationalError as e:
        if str(e).startswith("no such table:"):
            return set()
        raise
    if not cites:
        return set()
    scopes = effective_scopes(conn, {src for src, _ in cites})
    return {dst for src, dst in cites if scopes.get(src, ("secret", 0))[0] != "open"}


def exemplars(pool: list[Candidate], channel: str, now: float, *,
              budget: int = EXEMPLAR_TOKENS) -> list[str]:
    """Up to EXEMPLARS_MAX of her `pool`, fitted whole into `budget` with EXEMPLAR_HEAD: those of
    the turn's medium first, the best EXEMPLAR_POOL rotating by the day, none two alike."""
    want = medium(channel)
    ranked = sorted(pool, key=lambda c: (medium(c.channel) != want,))[:EXEMPLAR_POOL]
    if not ranked:
        return []
    day = int(now // 86400)
    start = (day * EXEMPLARS_MAX) % len(ranked)
    ordered = ranked[start:] + ranked[:start]
    shown: list[str] = []
    for c in ordered:
        if len(shown) >= EXEMPLARS_MAX:
            break
        if any(word_jaccard(c.text, s) >= EXEMPLAR_JACCARD for s in shown):
            continue
        if estimate_tokens(_section(EXEMPLAR_HEAD, [*shown, c.text])) <= budget:
            shown.append(c.text)
    return shown


def build(conn: sqlite3.Connection, sp: Speaker, channel: str, now: float, *, before: float,
          pool: list[Candidate] | None = None) -> tuple[Grounding, list[Candidate]]:
    """The turn's grounding and her example pool (read here unless `pool` is given, so a caller
    can keep it a while: it only changes with the night's rewards). Her class and talent tab
    come from the stored roster (cast_member) when the loop had none for her."""
    if not sp.cls:
        roster = _Reader(conn).rows("SELECT class,spec_tab FROM cast_member WHERE guid=?",
                                    (sp.guid,))
        if roster and type(roster[0]["class"]) is int:
            spec = roster[0]["spec_tab"]
            sp = replace(sp, cls=roster[0]["class"], spec_tab=spec if type(spec) is int else -1)
    if pool is None:
        pool = candidates(conn, sp, officer_turn=channel in OFFICER_CHANNELS, before=before,
                          now=now)
    return Grounding(tuple(card_lines(conn, sp, now)), tuple(exemplars(pool, channel, now)),
                     glossary(conn)), pool


__all__ = ["CARD_HEAD", "CARD_TOKENS", "Candidate", "EXEMPLAR_HEAD", "EXEMPLAR_TOKENS", "GLOSSARY",
           "GLOSSARY_TOKENS", "Grounding", "Speaker", "build", "candidates", "card_lines",
           "exemplars", "glossary", "medium", "wow_turn"]
