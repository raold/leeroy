"""The rival guilds (plan Task 14): who they are, their actors' cards, the data step that makes
them real, and the weekly top-up that keeps them at raid size.

The plan (``realm plan rivals --progression-name <name>``; reads the armory and mind.db, asks the
live model, writes only its plan file) picks three bot guilds. Horde: the one with the most
level-80 members is the progression rival, renamed to the name he gives (local data), and the
next is the drama farm. Alliance: the one with the most is the gankers. Each grows to 25-30
level-80 members with guildless level-80 random bots of its faction at an average item level of
200 or more, chosen as a raid: two tanks and six healers first (by class, wow.class_roles), the
rest damage. Its actors, the leader and 2-3 more of the highest rank at 80 (the gankers: 2-3
level-80 members, the leader first), stay at 12 or fewer of native's 16. Never anyone on guild
21's roster, anyone he ever kicked (taken back or not), a former member, or him
(ledger.actor_exclusions). Each actor gets a card from the live model, screened, at most 120
estimated tokens; a card that fails is rewritten once, then dropped.

The step "rivals" (steps.py) applies his approved plan in the running mind. It first writes the
realm_guild, realm_actor and realm_raider rows and the cards, then sends, each item once:
1. the rename and 2. the actor set (the whole set the rows hold), through the direct ledger
   (Progress.send);
3. one guild_grow per guild through the action queue, whose body ledger decides.
The actor set goes before the grows: native refuses a grow into a guild its realm guild set does
not name (RealmGuild.cpp, GrowAdmission: "refused_guild"), and only an actor set names them. A
grow's command id comes from the plan's hash, the guild and the attempt, so after a restart the
action journal says what became of it: one that may have reached native is never sent again,
one that never left goes as the next attempt. A grow with a held body waits for the body ledger
without spending an attempt.

The weekly top-up, from each Sunday once the step has been applied a day, reads the armory on a
worker thread (never the Realm's loop) and refills each rival below 25 level-80 members by the
same rules. It only adds: realm_raider rows, then one guild_grow, logged
``realm_rivals_topped guild=<id> added=<n>``. It counts who is in the guild, so after a grow
that ended unknown it fills the gap with others: that grow's bodies stay held and are never picked.
"""
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import math
import os
import random
import re
import uuid

from leeroy_mind.bodies import RANKS
from leeroy_mind.config import load_config
from leeroy_mind.human import human_identity
from leeroy_mind.library import mysql
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import EXTRACT, JSONJobFailed, chat_json
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.filters import repair
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import CLASSES, class_roles, faction_known

from . import protocol
from .action_store import ABANDON_RECEIPT, ABANDONED, ActionCapacity, ActionUnavailable
from .actions import ActionRejected
from .armory import GUILD_ID, RealmArmoryUnavailable
from .ledger import actor_exclusions_in_unit
from .ports import GUILD_NAME_KEY, armory_from_config
from .screen import Screen
from .steps import StepError, StepPending

log = logging.getLogger(__name__)

STEP = "rivals"
VERSION = 1
ROLES = ("progression", "drama", "gankers")
FACTION = {"progression": "horde", "drama": "horde", "gankers": "alliance"}
ACTOR_ROLE = {"progression": "officer", "drama": "officer", "gankers": "ganker"}
PERSONALITY = {
    "progression": "the try-hard progression guild that is always second to the same guild",
    "drama": "a social guild that farms drama in Trade and General",
    "gankers": "world PvP gankers who camp flight paths and Wintergrasp",
}
GROW_TO = (25, 30)
TOPUP_TO = 25
GROW_MAX = 30  # one guild_grow's bound (action_policy, native)
MIN_ILVL = 200
TANKS, HEALERS = 2, 6
ACTORS_MAX = 12
CARD_TOKENS = 120
GROW_OWNER = "realm_day"
GROW_ATTEMPTS = 8
TOPUP_KEY = "realm:rivals:topped"
TOPUP_SETTLE_S = 86400
TOPUP_RETRY_S = 600
RAIDER_ROLES = frozenset({"tank", "healer", "melee", "ranged"})
CARD_FIELDS = ("name", "role", "temperament", "habits", "want", "view_of_guild")
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "role": {"type": "string"}, "temperament": {"type": "string"},
        "habits": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
        "want": {"type": "string"}, "view_of_guild": {"type": "string"}},
    "required": list(CARD_FIELDS),
}
# The member list this plan needs and the armory does not have yet (Wave 4 may move it there).
MEMBER_QUERIES = {
    "guild_members": (
        "SELECT gm.guid, c.name, c.race, c.class, c.level, gm.rank, c.account FROM guild_member gm "
        "JOIN characters c ON c.guid = gm.guid WHERE gm.guildid = {guild_id:d} ORDER BY gm.rank, gm.guid"),
    "random_bot_accounts": (
        "SELECT id FROM account WHERE username LIKE 'RNDBOT%' AND id IN ({ids}) ORDER BY id"),
}
_MELEE = frozenset({1, 2, 4, 6})  # Warrior, Paladin, Rogue, Death Knight
_RANGED = frozenset({3, 5, 8, 9})  # Hunter, Priest, Mage, Warlock; a Shaman or Druid is either
_GUILD_NAME = re.compile(r"[A-Za-z]+(?: [A-Za-z]+)*")
_DIGITS = re.compile(r"[0-9]+")
_U32 = 0xFFFFFFFF
_PENDING = frozenset({None, "prepared", "dispatched"})
_WAITING = frozenset({"pending", "waiting", "sent"})


# -- the armory -------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GuildMember:
    guid: int
    name: str
    race: int
    class_id: int
    level: int
    rank: int

    @property
    def faction(self) -> str | None:
        return faction_known(self.race)


def _unavailable():
    return RealmArmoryUnavailable("armory evidence unavailable")


def _number(value, low, high) -> int:
    if type(value) is not str or _DIGITS.fullmatch(value) is None or not low <= int(value) <= high:
        raise _unavailable()
    return int(value)


class RivalArmory:
    """The realm armory (synchronous, read-only) plus the guild member list the plan needs."""

    def __init__(self, armory):
        self.armory = armory

    def bot_guilds(self):
        return self.armory.bot_guilds()

    def guildless_80s(self, faction):
        return self.armory.guildless_80s(faction)

    def guild_name(self, guild_id):
        return self.armory.guild_name(guild_id)

    def _rows(self, db, name, **values):
        sql = MEMBER_QUERIES[name].format(**values)
        try:
            mysql.check_select(sql)
            return mysql.query(db, sql, self.armory.mysql_exe, timeout=10, max_ms=5000)
        except mysql.QueryError:
            raise RealmArmoryUnavailable("armory query unavailable") from None

    def guild_members(self, guild_id) -> list[GuildMember]:
        """The guild's members on random-bot accounts, by rank then guid."""
        if type(guild_id) is not int or not 0 < guild_id <= _U32:
            raise ValueError("guild id must be a positive uint32 integer")
        found = []
        for row in self._rows(self.armory.chars, "guild_members", guild_id=guild_id):
            if len(row) != 7 or type(row[1]) is not str or not row[1]:
                raise _unavailable()
            race = _number(row[2], 1, 255)
            if faction_known(race) is None:
                raise _unavailable()
            found.append((GuildMember(_number(row[0], 1, _U32), row[1], race, _number(row[3], 1, 255),
                                      _number(row[4], 0, 255), _number(row[5], 0, 255)),
                          _number(row[6], 0, _U32)))
        accounts = sorted({account for _, account in found if account})
        bots = set()
        for start in range(0, len(accounts), 500):
            ids = ",".join(map(str, accounts[start:start + 500]))
            for row in self._rows(self.armory.auth, "random_bot_accounts", ids=ids):
                if len(row) != 1:
                    raise _unavailable()
                bots.add(_number(row[0], 1, _U32))
        return [m for m, account in found if account in bots]


# -- the plan ---------------------------------------------------------------------------------

def check_progression_name(name) -> str:
    if type(name) is not str or not 2 <= len(name) <= 24 or _GUILD_NAME.fullmatch(name) is None:
        raise StepError("the progression name must be 2-24 letters, words split by single spaces")
    return name


def _eligible(candidates, faction, excluded):
    """Guildless 80s of the faction at item level 200 or more, nobody excluded, best first."""
    seen, out = set(), []
    for c in candidates:
        if (c.faction != faction or c.guid in excluded or c.guid in seen or c.class_id not in CLASSES
                or not c.avg_ilvl >= MIN_ILVL):
            continue
        seen.add(c.guid)
        out.append(c)
    return sorted(out, key=lambda c: (-c.avg_ilvl, c.guid))


def _damage(class_id, rng) -> str:
    if class_id in _MELEE:
        return "melee"
    if class_id in _RANGED:
        return "ranged"
    return rng.choice(("melee", "ranged"))


def pick_raid(pool, need, taken, rng, *, tanks=TANKS, healers=HEALERS) -> list[dict]:
    """`need` raiders from the (best-first) pool, nobody in `taken`: first the tanks (pure tank
    classes before hybrids), then the healers (pure healers first), then damage. Adds the picks
    to `taken`."""
    free = [c for c in pool if c.guid not in taken]
    roles = {c.guid: class_roles(c.class_id) for c in free}
    tank = sorted((c for c in free if "tank" in roles[c.guid]),
                  key=lambda c: ("healer" in roles[c.guid], -c.avg_ilvl, c.guid))[:max(0, min(tanks, need))]
    used = {c.guid for c in tank}
    heal = sorted((c for c in free if c.guid not in used and "healer" in roles[c.guid]),
                  key=lambda c: ("tank" in roles[c.guid], -c.avg_ilvl, c.guid))[:max(0, min(healers, need - len(tank)))]
    used |= {c.guid for c in heal}
    rest = [c for c in free if c.guid not in used][:max(0, need - len(tank) - len(heal))]
    picked = ([(c, "tank") for c in tank] + [(c, "healer") for c in heal]
              + [(c, _damage(c.class_id, rng)) for c in rest])
    taken.update(c.guid for c, _ in picked)
    return [{"guid": c.guid, "name": c.name, "class": c.class_id, "role": role,
             "avg_ilvl": math.floor(c.avg_ilvl)} for c, role in picked]


def _pick_actors(role, guild, members, excluded, rng) -> list[dict]:
    faction, seen, eligible = FACTION[role], set(), []
    for m in sorted(members, key=lambda m: (m.guid != guild.leader_guid, m.rank, m.guid)):
        if (m.level != 80 or m.faction != faction or m.guid in excluded or m.guid in seen
                or m.class_id not in CLASSES):
            continue
        seen.add(m.guid)
        eligible.append(m)
    n = rng.randint(2, 3) if role == "gankers" else rng.randint(3, 4)
    return [{"guid": m.guid, "name": m.name, "class": m.class_id, "rank": m.rank,
             "role": ACTOR_ROLE[role]} for m in eligible[:n]]


def plan_rivals(armory, cast_guids, former, *, progression_name, rng, human_guid=0) -> dict:
    """The rivals plan's payload, cards aside. `cast_guids` is guild 21's whole roster, `former`
    the actor exclusions (every former member and every guid he ever kicked)."""
    name = check_progression_name(progression_name)
    excluded = frozenset(cast_guids) | frozenset(former) | {human_guid}
    guilds = [g for g in armory.bot_guilds() if g.guild_id != GUILD_ID]

    def ranked(faction):
        return sorted((g for g in guilds if g.faction == faction),
                      key=lambda g: (-g.count(level=80, faction=faction), g.guild_id))
    horde, alliance = ranked("horde"), ranked("alliance")
    if len(horde) < 2 or not alliance:
        raise StepError("not enough bot guilds for the three rivals")
    chosen = (("progression", horde[0]), ("drama", horde[1]), ("gankers", alliance[0]))
    if any(g.name.casefold() == name.casefold() and g is not horde[0] for g in guilds):
        raise StepError("another guild already has the progression name")
    pools, taken, out = {}, set(), []
    for role, guild in chosen:
        faction = FACTION[role]
        if faction not in pools:
            pools[faction] = _eligible(armory.guildless_80s(faction), faction, excluded)
        level80 = guild.count(level=80, faction=faction)
        target = rng.randint(*GROW_TO)
        need = max(0, min(GROW_MAX, target - level80))
        raiders = pick_raid(pools[faction], need, taken, rng)
        actors = _pick_actors(role, guild, armory.guild_members(guild.guild_id), excluded, rng)
        out.append({"role": role, "guild_id": guild.guild_id, "faction": faction,
                    "name": name if role == "progression" else guild.name, "old_name": guild.name,
                    "level80": level80, "target": target, "short": max(0, need - len(raiders)),
                    "actors": actors, "raiders": raiders})
    return {"version": VERSION, "guilds": out}


def _uint(value) -> int:
    if type(value) is not int or not 0 < value <= _U32:
        raise ValueError("invalid guid")
    return value


def _label(value, high) -> str:
    if type(value) is not str or not 0 < len(value) <= high or value != value.strip() or not value.isprintable():
        raise ValueError("invalid name")
    return value


def _check_card(card):
    if type(card) is not dict or set(card) != set(CARD_FIELDS):
        raise ValueError("invalid card")
    habits = card["habits"]
    if type(habits) is not list or not 2 <= len(habits) <= 3:
        raise ValueError("invalid card")
    for text in (card["name"], card["role"], card["temperament"], *habits, card["want"], card["view_of_guild"]):
        _label(text, 400)


def parse_plan(payload) -> dict:
    """The approved payload, checked: ValueError when it is not a rivals plan of this version."""
    if type(payload) is not dict or payload.get("version") != VERSION:
        raise ValueError("not a rivals plan")
    guilds = payload.get("guilds")
    if type(guilds) is not list or [g.get("role") if type(g) is dict else None for g in guilds] != list(ROLES):
        raise ValueError("invalid rivals")
    ids, seen, actors = set(), set(), set()
    for g in guilds:
        gid = _uint(g.get("guild_id"))
        if gid == GUILD_ID or gid in ids or g.get("faction") != FACTION[g["role"]]:
            raise ValueError("invalid rival guild")
        ids.add(gid)
        name, old = _label(g.get("name"), 24), _label(g.get("old_name"), 24)
        if g["role"] != "progression" and name != old:
            raise ValueError("invalid rival name")
        if type(g.get("actors")) is not list or type(g.get("raiders")) is not list or len(g["raiders"]) > GROW_MAX:
            raise ValueError("invalid rival members")
        for a in g["actors"]:
            if (type(a) is not dict or a.get("role") != ACTOR_ROLE[g["role"]] or a.get("class") not in CLASSES
                    or _uint(a.get("guid")) in seen):
                raise ValueError("invalid actor")
            _label(a.get("name"), 64)
            seen.add(a["guid"])
            actors.add(a["guid"])
        for r in g["raiders"]:
            if (type(r) is not dict or r.get("role") not in RAIDER_ROLES or r.get("class") not in CLASSES
                    or type(r.get("avg_ilvl")) is not int or not 0 <= r["avg_ilvl"] <= 0xFFFF
                    or _uint(r.get("guid")) in seen):
                raise ValueError("invalid raider")
            _label(r.get("name"), 64)
            seen.add(r["guid"])
    if len(actors) > ACTORS_MAX:
        raise ValueError("too many actors")
    cards = payload.get("cards", {})
    if type(cards) is not dict or not all(type(k) is str and k.isdecimal() and int(k) in actors for k in cards):
        raise ValueError("invalid cards")
    for card in cards.values():
        _check_card(card)
    return payload


# -- the cards ----------------------------------------------------------------------------------

def card_text(card) -> str:
    return " ".join([card["name"], card["role"], card["temperament"], *card["habits"], card["want"],
                     card["view_of_guild"]])


def _card_messages(guild, actor):
    kind = "an officer" if actor["role"] == "officer" else "a ganker"
    return [
        {"role": "system", "content": (
            "You write character cards for members of rival guilds on a World of Warcraft: Wrath "
            "of the Lich King server. A card guides one character's short chat lines. Every field "
            "is a few words; the whole card stays under 80 words.")},
        {"role": "user", "content": (
            f"{actor['name']} is a level 80 {CLASSES.get(actor['class'], 'adventurer')} and {kind} "
            f"of <{guild['name']}>, a {guild['faction'].title()} guild: {PERSONALITY[guild['role']]}.\n"
            "Fields: name (theirs), role (their place in the guild), temperament, habits (2 or 3 "
            "speech habits), want (one thing they want), view_of_guild (what they think of the "
            "guild everyone measures themselves against; call it \"that guild\", never by a name).")},
    ]


def _card_field(value, key) -> str:
    if type(value) is not str:
        raise ValueError(f"{key} must be a string")
    text = " ".join(repair(value).split())
    if not text or not text.isprintable():
        raise ValueError(f"{key} must be plain, non-empty text")
    return text


def _card_check(actor, screen):
    def validate(data):
        habits = data.get("habits")
        if type(habits) is not list or not 2 <= len(habits) <= 3:
            raise ValueError("habits must be a list of 2 or 3 short strings")
        card = {"name": actor["name"], "role": _card_field(data.get("role"), "role"),
                "temperament": _card_field(data.get("temperament"), "temperament"),
                "habits": [_card_field(h, "habits") for h in habits],
                "want": _card_field(data.get("want"), "want"),
                "view_of_guild": _card_field(data.get("view_of_guild"), "view_of_guild")}
        fields = [(key, card[key]) for key in ("role", "temperament", "want", "view_of_guild")]
        for key, text in fields + [("habits", h) for h in card["habits"]]:
            reason = screen.check(text, speaker_kind="officer")
            if reason is not None:  # A reason code only: never the text or a name.
                raise ValueError(f"the card's {key} was refused ({reason}); write it again without that")
        if estimate_tokens(card_text(card)) > CARD_TOKENS:
            raise ValueError("the card is too long; keep the whole card under 80 words")
        return card
    return validate


async def write_cards(plan, llm, *, model, screen) -> dict[str, dict]:
    """One card per actor, by guid: one JSON call on the live model (P1b's EXTRACT options,
    think=False), screened field by field and at most CARD_TOKENS estimated tokens. A card that
    fails is rewritten once, then dropped. With the screen not ready nothing is asked."""
    if not screen.ready:
        log.warning("realm_rivals_cards_unscreened")
        return {}
    cards = {}
    for guild in plan["guilds"]:
        for actor in guild["actors"]:
            try:
                cards[str(actor["guid"])] = await chat_json(
                    llm, model, _card_messages(guild, actor), CARD_SCHEMA, EXTRACT, _card_check(actor, screen))
            except JSONJobFailed:
                log.info("realm_rivals_card_dropped guid=%d", actor["guid"])
    return cards


# -- the command line -------------------------------------------------------------------------

def _open_armory(root):
    armory = armory_from_config(root / "mind.toml")
    return None if armory is None else RivalArmory(armory)


def _open_llm(cfg):
    from leeroy_mind.llm.client import OllamaClient
    return OllamaClient(cfg.ollama.url)


def _progression_arg(argv):
    if len(argv) == 2 and argv[0] == "--progression-name":
        return argv[1]
    if len(argv) == 1 and argv[0].startswith("--progression-name="):
        return argv[0].split("=", 1)[1]
    return None


def _human_guid(conn) -> int:
    """Who the mind takes for him: meta human_guid, else the roster's lowest rank-0 guid."""
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    value = row[0] if row is not None else None
    if type(value) is str and len(value) <= 10 and value.isascii() and value.isdecimal() and 0 < int(value) <= _U32:
        return int(value)
    row = conn.execute("SELECT guid FROM cast_member WHERE rank=0 ORDER BY guid LIMIT 1").fetchone()
    return row[0] if row is not None else 0


def _cached_guild_name(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (GUILD_NAME_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row is not None else None
    except (TypeError, ValueError):
        value = None
    name = value.get("name") if type(value) is dict else None
    return name if type(name) is str and 0 < len(name) <= 24 and name == name.strip() and name.isprintable() else ""


def _summary(path, plan, cards):
    guilds = [{"role": g["role"], "guild_id": g["guild_id"], "name": g["name"], "old_name": g["old_name"],
               "level80": g["level80"], "target": g["target"], "adding": len(g["raiders"]),
               "short": g["short"], "actors": [a["name"] for a in g["actors"]]} for g in plan["guilds"]]
    dropped = [a["name"] for g in plan["guilds"] for a in g["actors"] if str(a["guid"]) not in cards]
    return {"plan": path.name, "guilds": guilds, "cards": len(cards), "dropped": dropped}


def plan_cli(ctx) -> int:
    """realm plan rivals --progression-name <name>: a dry run. It reads mind.db (read-only) and the
    armory, asks the live model for the cards, prints the counts and names and writes the plan
    file; nothing else is written. The dispatcher refuses it inside the night's window."""
    name = _progression_arg(ctx.argv)
    if name is None:
        print("realm: usage: realm plan rivals --progression-name <name>")
        return 1
    check_progression_name(name)
    cfg = load_config(ctx.root / "mind.toml")
    with closing(ctx.connect()) as conn:
        if conn.execute("SELECT 1 FROM realm_guild WHERE status='active' LIMIT 1").fetchone() is not None:
            print("realm: rivals already chosen; the weekly top-up keeps them at raid size")
            return 1
        store = Store(conn, clock=ctx.clock)
        human = _human_guid(conn)
        with store.transaction():
            roster = frozenset(row[0] for row in conn.execute("SELECT guid FROM cast_member"))
            former = actor_exclusions_in_unit(store, human_guid=human)
        identity = human_identity(cfg, store, human)
        guild_name = _cached_guild_name(conn)
    armory = _open_armory(ctx.root)
    if armory is None:
        print("realm: rivals plan refused: the armory is unavailable")
        return 1
    plan = plan_rivals(armory, roster, former, progression_name=name, rng=random.Random(), human_guid=human)
    guild_name = guild_name or armory.guild_name(GUILD_ID)
    screen = Screen(ctx.root, lambda: guild_name, lambda: identity)
    if not screen.ready:
        print("realm: rivals plan refused: the screen is not ready (blocklist or guild name missing)")
        return 1
    try:
        cards = asyncio.run(write_cards(plan, _open_llm(cfg), model=cfg.models.live, screen=screen))
    except LLMError:
        print("realm: rivals plan refused: the live model is unavailable")
        return 1
    path = ctx.write_plan(STEP, {**plan, "cards": cards})
    print(json.dumps(_summary(path, plan, cards), sort_keys=True, indent=2, ensure_ascii=False))
    return 0


# -- the running mind's part ------------------------------------------------------------------

def grow_id(key: str, guild_id: int, attempt: int) -> str:
    """The command id of one guild_grow attempt: the same in every process."""
    return hashlib.sha256(f"rivals:grow:{key}:{guild_id}:{attempt}".encode("utf-8")).hexdigest()[:32]


def week_key(local) -> str:
    """The Sunday that starts this local week."""
    day = local.date()
    return (day - timedelta(days=(day.weekday() + 1) % 7)).isoformat()


def _ready(context) -> bool:
    return context.enabled is True and context.connected is True and context.presence_known is True


def _state(sent):
    return sent[0] if type(sent) is tuple else sent


def _read_for_topup(armory_of, guilds):
    """On a worker thread: each rival's level-80 count and the pools of the factions short."""
    armory = armory_of()
    if armory is None:
        return None
    bots = {g.guild_id: g for g in armory.bot_guilds()}
    counts = {gid: bots[gid].count(level=80, faction=faction) for gid, faction, _ in guilds
              if gid in bots and bots[gid].faction == faction}
    short = sorted({faction for gid, faction, _ in guilds if counts.get(gid, TOPUP_TO) < TOPUP_TO})
    return counts, {faction: armory.guildless_80s(faction) for faction in short}


class Rivals:
    """The step "rivals" and the weekly top-up, as a Realm part."""

    name = "rivals"

    def __init__(self, realm):
        self.realm = realm
        self._rng = random.Random()
        self._topping = None
        self._next_look = 0.0
        self._done = None

    async def on_event(self, ev):
        """No event types of its own."""

    # -- shared --------------------------------------------------------------------------------

    def _excluded(self, store, human) -> frozenset[int]:
        roster = {row[0] for row in store.conn.execute("SELECT guid FROM cast_member")}
        return actor_exclusions_in_unit(store, human_guid=human) | roster | {human}

    def _grow(self, key, guild_id, guids) -> str:
        """One guild_grow of (key, guild), as its journal tells it: acked, unknown, gave_up or
        none; pending (in flight), waiting (not enrolled) or sent (enrolled now). The prune keeps
        every grow row; a released one's meta receipt answers even without its row."""
        realm, latest = self.realm, None
        for attempt in range(GROW_ATTEMPTS):
            command_id = grow_id(key, guild_id, attempt)
            if realm.app.store.get_meta(ABANDON_RECEIPT + command_id) is not None:
                return "unknown"
            record = realm.action_store.get(command_id)
            if record is not None:
                latest = attempt, record
        attempt = 0
        if latest is not None:
            attempt, record = latest
            if record.state in ("prepared", "dispatched"):
                return "pending"
            if record.state in ("unknown", "conflict") or record.result_reason == ABANDONED:
                return "unknown"  # It may have reached native: never sent again, even once released.
            if record.state == "completed" and record.result_ok is True:
                return "acked"
            attempt += 1  # It never left, or native refused it before doing anything.
            if attempt >= GROW_ATTEMPTS:
                return "gave_up"
        if not guids:
            return "none"
        if not realm.bodies.may_move_all(GROW_OWNER, guids, rank=RANKS[GROW_OWNER]):
            return "waiting"
        try:
            realm.queue.submit(protocol.GuildGrow(grow_id(key, guild_id, attempt), guild_id, tuple(guids)),
                               owner=GROW_OWNER)
        except (ActionRejected, ActionCapacity, ActionUnavailable) as error:
            log.debug("realm_rivals_grow_deferred guild=%d error=%s", guild_id, type(error).__name__)
            return "waiting"
        return "sent"

    # -- the step ------------------------------------------------------------------------------

    def _conflict(self, conn, plan) -> bool:
        planned = {(g["guild_id"], g["role"]) for g in plan["guilds"]}
        ids = {gid for gid, _ in planned}
        return any((gid in ids and ((gid, role) not in planned or status != "active"))
                   or (gid not in ids and status == "active")
                   for gid, role, status in conn.execute("SELECT id, role, status FROM realm_guild"))

    def _write_rows(self, conn, plan, excluded, now):
        kept = {}
        for g in plan["guilds"]:
            actors = [a for a in g["actors"] if a["guid"] not in excluded]
            raiders = [r for r in g["raiders"] if r["guid"] not in excluded]
            kept[g["guild_id"]] = actors, raiders
            conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(id) DO NOTHING", (g["guild_id"], g["role"], g["faction"], g["name"], now))
            for a in actors:
                conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(guid) DO NOTHING", (a["guid"], g["guild_id"], a["name"], a["role"], now))
            for r in raiders:
                conn.execute("INSERT INTO realm_raider(guild,guid,class,role,avg_ilvl,checked) VALUES "
                             "(?,?,?,?,?,?) ON CONFLICT(guild,guid) DO NOTHING",
                             (g["guild_id"], r["guid"], r["class"], r["role"], r["avg_ilvl"], now))
        return kept

    def _write_cards(self, cards, guids, guilds=()):
        """The actors' cards (cards/<guid>.json) and, where none is there yet, each guild's own
        (cards/guild-<id>.json: its name, its part in the realm and its faction), as speech
        reads them. A guild card already there, his own words perhaps, is kept."""
        directory = self.realm.ports.realm_dir() / "cards"
        directory.mkdir(parents=True, exist_ok=True)
        files = [(directory / f"{guid}.json", cards[str(guid)], True) for guid in guids if str(guid) in cards]
        files += [(directory / f"guild-{g['guild_id']}.json",
                   {"name": g["name"], "role": PERSONALITY[g["role"]], "faction": g["faction"]}, False)
                  for g in guilds]
        for path, card, renew in files:
            text = json.dumps(card, sort_keys=renew, indent=2, ensure_ascii=False) + "\n"
            if path.is_file() and (not renew or path.read_text(encoding="utf-8") == text):
                continue
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

    async def _rename(self, plan, progress):
        guild = plan["guilds"][0]
        if guild["name"] == guild["old_name"]:
            return "none"
        state = _state(await progress.send(0, protocol.GuildRename(uuid.uuid4().hex, guild["guild_id"],
                                                                   guild["name"])))
        if state == "refused":  # Never done: the row keeps the name the guild still has.
            store = self.realm.app.store
            with store.transaction():
                store.conn.execute("UPDATE realm_guild SET name=? WHERE id=? AND name=?",
                                   (guild["old_name"], guild["guild_id"], guild["name"]))
        return state

    async def _actors(self, progress):
        cmd = self.realm.actor_set()
        if cmd is None:
            return "invalid"
        if not cmd.guids and not cmd.guilds:
            return "none"
        return _state(await progress.send(1, cmd))

    async def apply(self, payload, progress):
        """The approved plan, in the running mind (Steps calls it only while the Realm is live)."""
        try:
            plan = parse_plan(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("realm_rivals_plan_invalid")
            return {"refused": "rivals_plan_invalid"}
        realm = self.realm
        store, human, now = realm.app.store, realm.ports.human().guid, realm.app.clock.now()
        with store.transaction():
            conflict = self._conflict(store.conn, plan)
            if not conflict:  # The rows come first, before anything is sent.
                kept = self._write_rows(store.conn, plan, self._excluded(store, human), now)
        if conflict:
            log.warning("realm_rivals_already_chosen")
            return {"refused": "rivals_already_chosen"}
        self._write_cards(plan.get("cards", {}), [a["guid"] for actors, _ in kept.values() for a in actors],
                          plan["guilds"])
        rename = await self._rename(plan, progress)
        if rename in _PENDING:
            raise StepPending("rename")
        actors = await self._actors(progress)
        if actors in _PENDING:
            raise StepPending("actors")
        grows = {}
        for g in plan["guilds"]:
            guids = [r["guid"] for r in kept[g["guild_id"]][1]]
            # Without native's realm guild set every grow would be refused: none is sent.
            grows[str(g["guild_id"])] = (self._grow(progress.sha256, g["guild_id"], guids)
                                         if actors == "acked" else "skipped")
        if any(state in _WAITING for state in grows.values()):
            raise StepPending("grow")
        log.info("realm_rivals_applied rename=%s actors=%s", rename, actors)
        return {"rename": rename, "actors": actors, "grow": grows,
                "actors_kept": sum(len(a) for a, _ in kept.values()),
                "raiders_kept": sum(len(r) for _, r in kept.values())}

    # -- the weekly top-up ---------------------------------------------------------------------

    async def tick(self):
        """The top-up's cheap look; the read is a spawned child. Never the Realm loop's failure."""
        try:
            self._look()
        except Exception as error:
            self._next_look = self.realm.app.clock.now() + TOPUP_RETRY_S
            log.warning("realm_rivals_topup_failed error=%s", type(error).__name__)

    def _look(self):
        realm = self.realm
        now = realm.app.clock.now()
        if self._topping is not None or now < self._next_look or not _ready(realm.action_context()):
            return
        key = week_key(realm.app.clock.local())
        if self._done == key:
            return
        self._next_look = now + TOPUP_RETRY_S
        store = realm.app.store
        with store.transaction():
            marker = store.conn.execute("SELECT value FROM meta WHERE key=?", (TOPUP_KEY,)).fetchone()
            applied = store.conn.execute("SELECT max(applied) FROM realm_step WHERE name=? AND applied IS NOT NULL",
                                         (STEP,)).fetchone()[0]
            guilds = tuple((row[0], row[1], row[2]) for row in store.conn.execute(
                "SELECT id, faction, role FROM realm_guild WHERE status='active' ORDER BY id"))
        if marker is not None and marker[0] == json.dumps(key):
            self._done = key
            return
        if applied is None or now - applied < TOPUP_SETTLE_S or not guilds:
            return
        if any(r.kind == "guild_grow" and r.state in ("prepared", "dispatched") for r in realm.action_store.records()):
            return  # Its members are still joining: their count would say they are missing.
        task = realm.spawn(lambda: self._top_up(key, guilds))
        if task is not None:
            self._topping = task
            task.add_done_callback(self._topped)

    def _topped(self, task):
        self._topping = None

    async def _top_up(self, key, guilds):
        realm = self.realm
        try:
            read = await asyncio.to_thread(_read_for_topup, realm.ports.armory, guilds)
        except Exception as error:  # The armory's own errors carry no configuration or data.
            log.warning("realm_rivals_topup_unavailable error=%s", type(error).__name__)
            return
        if read is None:
            log.warning("realm_rivals_topup_unavailable error=no_armory")
            return
        if not realm.enabled or not _ready(realm.action_context()):
            return
        counts, pools = read
        store, human, now = realm.app.store, realm.ports.human().guid, realm.app.clock.now()
        with store.transaction():
            excluded = self._excluded(store, human)
            roles = {}
            for gid, role in store.conn.execute("SELECT guild, role FROM realm_raider"):
                roles.setdefault(gid, []).append(role)
        try:
            # An unresolved action keeps its manifest held for good (a grow that ended unknown,
            # say): none of it is picked, or the grow would wait on it forever.
            held = {guid for hold in realm.action_store.holds() for guid in hold.guids}
        except ActionUnavailable:
            log.warning("realm_rivals_topup_unavailable error=ActionUnavailable")
            return
        taken, plans = held, []
        for gid, faction, _ in guilds:
            if gid not in counts or counts[gid] >= TOPUP_TO:
                continue
            have = roles.get(gid, [])
            picked = pick_raid(_eligible(pools.get(faction, ()), faction, excluded),
                               min(GROW_MAX, TOPUP_TO - counts[gid]), taken, self._rng,
                               tanks=TANKS - have.count("tank"), healers=HEALERS - have.count("healer"))
            if picked:
                plans.append((gid, picked))
        with store.transaction():  # Rows first; a top-up only ever adds.
            for gid, picked in plans:
                for r in picked:
                    store.conn.execute(
                        "INSERT INTO realm_raider(guild,guid,class,role,avg_ilvl,checked) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(guild,guid) DO NOTHING", (gid, r["guid"], r["class"], r["role"], r["avg_ilvl"], now))
        complete = True
        for gid, picked in plans:
            state = self._grow(f"topup:{key}", gid, [r["guid"] for r in picked])
            if state == "sent":
                log.info("realm_rivals_topped guild=%d added=%d", gid, len(picked))
            elif state == "waiting":
                complete = False
        if complete:
            store.set_meta(TOPUP_KEY, json.dumps(key))
            self._done = key


def attach(realm):
    """The rivals part, with its step registered on the steps part: no I/O."""
    part = Rivals(realm)
    steps = realm.part("steps")
    if steps is None:
        log.warning("realm_rivals_no_steps")
    else:
        steps.register(STEP, part.apply)
    return part, ()
