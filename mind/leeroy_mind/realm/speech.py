"""The light minds' speech (Task 13, I15, PR12): rival officers' rationed, screened lines from
the capitals, the gankers' emotes, and the officers' places.

A speaking guild is an active Horde rival guild of the progression or drama role; its officers
are its active realm_actor officers, never him, a roster member or anyone he ever kicked. At an
eligible moment (maybe_speak; the tick offers one in about twelve minutes, MOMENT_CHANCE) one
officer says one line when every rule holds:

- he is online, the screen is ready (it fails closed: without the blocklist or the guild's name
  this part sends nothing at all), and his raid is not in combat, wipe_silence or wipe_debrief;
- a path is open, by the realm's own move records (realm_actor.place), never a guess:
  Trade, while he stands where the Horde City channel opens. The officer must stand in such a
  city too, which may be another than his: the channel is one per faction. With none there, one
  officer is first moved to a city place in another capital (uc_bank or tb_bank, else org_bank)
  and speaks at a later moment.
  General, elsewhere: the officer stands in his zone; else a move to a city place in his zone
  comes first.
  A whisper to him, at most once a day across the realm.
  Say and yell are never rival paths: where an actor stands within a zone is unknown (M33);
- rationing counts sent lines only: LIMITS' 2 an hour per guild, 6 overall, 180 s apart, and
  never a third channel line in a row since his or a member's last channel line.

answer(ev) gives his Trade line naming a rival guild or officer one answer from that guild, and
gz(fact) a grudging gz after a server-wide kill, once per fact. Both keep the hourly caps and
raid silence (gz the three-in-a-row rule too) but not the 180 s gap: an answer that waited
would answer nothing. Both need an officer whose record already says a Trade city.
boast(guild, fact) is the race's hook after a benchmark loss: that guild boasts in Trade once,
rationed as a gz, at the first tick within a day that allows it.
A rival's Trade line is one of last night's beats for its guild (night.beat_lines_in_unit) while
one is left unsaid, screened again; otherwise, and for every other line, it is composed by one
BACKGROUND call on the live model (ports.llm_line, from a spawned child: a tick never awaits a
model), repaired as the pipeline repairs, then screened. A failed screen drops the line
(realm_line_dropped id=<n> reason=<r>) and tries once more, composed. Every line is
stored in realm_line; one that passes goes out once, as RealmSay through the DirectLedger (owner
"speech", ref line:<id>), and its row follows the ledger: sent, refused or unknown, never sent
again. A refusal rests the officer a while; not_in_city also clears his place record.

The gankers never call the model: emote_for picks spit or laugh over a corpse they made and wave
at a duel, sent as RealmEmote through the DirectLedger.

Officers' places: every realm_move that comes back done is recorded with set_actor_place. Rival
officers are placed in a capital at each hello, once an hour while he is offline and after his
raid night ends, through the action queue (owner realm_event); the body ledger alone refuses a
held body (owner ruling 1, 2026-11-04), and the next officer is tried. The guild's name, which
the screen needs, is refreshed once per hello (ruling 5) once the blocklist exists.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import random
import re
import uuid

from leeroy_mind.prompts import load_prompt
from leeroy_mind.protocol import event_type
from leeroy_mind.talk.filters import repair
from leeroy_mind.talk.items import defuse, fold
from leeroy_mind.text import truncate_words
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import protocol, store as realm_store
from .action_policy import decode
from .action_store import TERMINAL
from .config import LIMITS
from .places import Places, PlacesError, places_path, trade_cities
from .provenance import public_fact_in_unit
from .screen import Screen

log = logging.getLogger(__name__)

OWNER = "speech"  # the DirectLedger owner of every line and emote
MOVE_OWNER = "realm_event"  # the body ledger owner of the officers' moves
FACTION = "horde"
SPEAKING_ROLES = ("progression", "drama")
CAPITALS = ("uc_bank", "tb_bank", "org_bank")
SILENT_PHASES = frozenset({"combat", "wipe_silence", "wipe_debrief"})
RIVAL_KINDS = ("rival", "gz", "answer")
EVENTS = ("channel_chat", "realm_kill", "pvp_kill", "duel_end", "realm_job", "hello", "logout")
MOMENT_S = 60
MOMENT_CHANCE = 1 / 12
REST_S = 600  # an officer native refused (or a move of his did) rests this long
MOVE_TIMEOUT_S = 600  # a move with no outcome by then no longer holds up its guild
OFFLINE_PASS_S = 3600
BEAT_FRESH_S = 2 * LIMITS.day_s  # a night's beats are for the day after it
OFFICER_CARD_TOKENS, GUILD_CARD_TOKENS = 120, 150
_CARD_BYTES = 64 * 1024
_REMEMBER = 256
_REASON = re.compile(r"[^a-z0-9_]+")
# Refusals of a move that say nothing about the officer: he is tried again.
_NOT_THEIRS = frozenset({"owner_dropped", "stale_context", "queue_closed", "realm_off", "disabled",
                         "paced", "transport_capacity", "not_enrolled", "stale_identity"})

RIVAL_PROMPT = (
    "You write one line of chat for an officer of a rival guild on a World of Warcraft server, "
    "in Wrath of the Lich King, in 2009.\n"
    "Write it the way players type in Trade chat: one short line, no quotation marks, no name tag.\n"
    "Mock the play, never a person or a group of people. Never name another guild. No flirting. "
    "Nobody quits or leaves a guild.\n"
    "Write only the line.")
MEMBER_PROMPT = (
    "You write one line of Trade chat for a member of a guild on a World of Warcraft server, "
    "in Wrath of the Lich King, in 2009.\n"
    "One short line, the way players type in Trade, no quotation marks, no name tag.\n"
    "Mock the play, never a person or a group of people. Never name a guild. No flirting. "
    "Nobody quits or leaves a guild.\n"
    "Write only the line.")


@dataclass(frozen=True, slots=True)
class Officer:
    guid: int
    name: str
    guild: int
    guild_name: str
    place: str  # the realm's own move record; "" when none


@dataclass(frozen=True, slots=True)
class SpokenLine:
    """A line the realm sent, as a member's reaction sees it (talk.react)."""
    id: int
    kind: str
    guid: int
    name: str
    guild: int
    guild_name: str
    channel: str
    text: str
    zone: int


@dataclass(frozen=True, slots=True)
class _Spec:
    kind: str  # realm_line.kind: rival, gz, answer or member
    speaker_kind: str  # the screen's: officer or member
    guid: int
    name: str
    guild: int
    guild_name: str
    channel: str
    to_guid: int
    zone: int
    situation: str
    fact: object = None
    sketch: str = ""
    line: str = ""  # a line written already (a night's beat): screened again, never composed


@dataclass(frozen=True, slots=True)
class _Ration:
    """Sent rival lines of the last day, as (guild, channel, ts), and the last member line."""
    now: float
    lines: tuple
    member_at: float | None

    def _hour(self):
        return [line for line in self.lines if self.now - line[2] < LIMITS.hour_s]

    def realm_ok(self):
        return len(self._hour()) < LIMITS.rival_realm_lines_per_hour

    def guild_ok(self, guild):
        return sum(g == guild for g, _, _ in self._hour()) < LIMITS.rival_guild_lines_per_hour

    def spaced(self):
        return all(self.now - ts >= LIMITS.rival_line_gap_s for _, _, ts in self.lines)

    def run_ok(self, since):
        marks = [t for t in (since, self.member_at) if t is not None]
        start = max(marks) if marks else None
        run = sum(1 for _, channel, ts in self.lines
                  if channel != "whisper" and (start is None or ts > start))
        return run < LIMITS.rival_consecutive_lines

    def whisper_ok(self):
        return (sum(channel == "whisper" for _, channel, _ in self.lines)
                < LIMITS.rival_human_whispers_per_day)

    def order(self, guilds):
        """Guilds that spoke least this hour first, then the one silent longest."""
        hour = self._hour()

        def key(guild):
            mine = [ts for g, _, ts in hour if g == guild[0]]
            return len(mine), max(mine, default=0.0), guild[0]
        return sorted(guilds, key=key)


def _reason(text, fallback):
    if type(text) is not str:
        return fallback
    cleaned = _REASON.sub("_", text.lower()).strip("_")[:64]
    return cleaned or fallback


def _line_state(ledger, ack):
    """realm_line's (state, reason) for the line's DirectLedger state."""
    if ledger is None:
        return "refused", "unsent"  # Pruned: only a refused or abandoned intent ever is.
    state, reason, _ = ledger
    if state == "acked":
        return "sent", None
    if state in ("prepared", "dispatched"):
        return state, None
    if state == "unknown":
        return "unknown", _reason(reason, "transport_unknown")
    if state == "refused" and ack is not None and ack.ok is False:
        return "refused", _reason(ack.error, "native_refused")
    return "refused", _reason(reason, "unsent")


def _item_links(app, text):
    """A screened line as realm_line keeps it and as RealmSay sends it: the guild's item links
    (talk/items.py; rival officers link at level 80). RealmSay is not cleaned on the way out,
    so its text is folded as native will clean it (items.fold) and only then gets placeholders,
    and those only once native has them (ItemLinker.wire)."""
    items = getattr(getattr(app, "engine", None), "items", None)
    if items is None:
        text = defuse(text)
        return text, fold(text)
    text = items.canonical(text, CAST_MIN_LEVEL)
    return text, items.wire(text, CAST_MIN_LEVEL)


def _one_line(text):
    for line in text.splitlines():
        line = line.strip().strip('"“”').strip()
        if line:
            return line
    return ""


def _card(directory, stem, tokens):
    """A rival card from mind-data/realm/cards (<stem>.json or <stem>.txt), cut to `tokens`."""
    for suffix in (".json", ".txt"):
        path = directory / f"{stem}{suffix}"
        try:
            if path.stat().st_size > _CARD_BYTES:
                return ""
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return ""
        if suffix == ".json":
            try:
                data = json.loads(raw)
            except ValueError:
                return ""
            if type(data) is not dict:
                return ""
            raw = "; ".join(
                f"{key}: {', '.join(str(v) for v in value) if type(value) is list else value}"
                for key, value in data.items() if type(key) is str and value not in (None, "", []))
        return truncate_words(" ".join(raw.split()), tokens * 4)
    return ""


class Speech:
    """The realm's part for the light minds' speech; see the module's docstring."""

    name = "speech"

    def __init__(self, realm, *, rng=None):
        self.realm = realm
        self._rng = rng or random.Random()
        self.moment_chance = MOMENT_CHANCE
        self._places = None
        self._screen = None
        self._composing = None
        self._inflight = None
        self._rest = {}
        self._moving = {}
        self._wanted = set()
        self._pass_epoch = 0
        self._name_epoch = 0
        self._offline_pass = None
        self._raid = ""
        self._break = None
        self._next_moment = 0.0
        self._gz = OrderedDict()
        self._answered = OrderedDict()
        self._boasts = OrderedDict()  # fact id -> (guild, fact, until), None once said or given up
        self._noted = set()

    # -- what it reads -----------------------------------------------------------------------

    @property
    def places(self):
        """The places beside the live module configs (else the repo's dist), loaded once."""
        if self._places is None:
            cfg = getattr(getattr(self.realm.app, "guild", None), "cfg", None)
            try:
                self._places = Places.load(places_path(getattr(cfg, "worldserver_conf", None)))
            except PlacesError:
                self._note("realm_speech_places_unavailable")
                self._places = Places({})
        return self._places

    @property
    def screen(self):
        if self._screen is None:
            ports = self.realm.ports
            self._screen = Screen(self.realm.app.data_dir, ports.guild_name, ports.human)
        return self._screen

    def _note(self, event):
        if event not in self._noted:
            self._noted.add(event)
            log.warning(event)

    def _live(self):
        context = self.realm.action_context()
        return context if context.enabled and context.connected and context.presence_known else None

    def _store(self):
        return self.realm.app.store

    def _zone_of(self, place):
        found = self.places.get(place) if place else None
        return found.zone if found is not None else None

    def _his_zone(self):
        try:
            zone = self.realm.ports.human_zone()
        except Exception:
            return None
        return zone if type(zone) is int and zone > 0 else None

    def _open(self):
        """He is online and his raid is not in a silent phase (an unreadable phase is one)."""
        ports = self.realm.ports
        if ports.human_online() is not True:
            return False
        try:
            phase = str(ports.raid_phase())
        except Exception:
            return False
        return phase not in SILENT_PHASES

    def _may_talk(self):
        return self._composing is None and self.screen.ready and self._open()

    def _guilds(self):
        store = self._store()
        with store.transaction():
            rows = store.conn.execute(
                "SELECT id,name FROM realm_guild WHERE status='active' AND faction=? AND role IN (?,?) "
                "ORDER BY id", (FACTION, *SPEAKING_ROLES)).fetchall()
        return [(row[0], row[1]) for row in rows if type(row[0]) is int and type(row[1]) is str]

    def _excluded(self):
        store = self._store()
        human = self.realm.ports.human().guid
        with store.transaction():
            roster = {row[0] for row in store.conn.execute("SELECT guid FROM cast_member")}
        return self.realm.ledger.actor_exclusions() | roster | {human}

    def _officers(self, now, guild=None):
        """The guild's (or every speaking guild's) officers who are not resting, by guid."""
        excluded = self._excluded()
        store = self._store()
        with store.transaction():
            rows = store.conn.execute(
                "SELECT a.guid,a.name,a.guild,g.name,a.place FROM realm_actor a JOIN realm_guild g "
                "ON g.id=a.guild WHERE a.status='active' AND a.role='officer' AND g.status='active' "
                "AND g.faction=? AND g.role IN (?,?) ORDER BY a.guid", (FACTION, *SPEAKING_ROLES)).fetchall()
        return [Officer(row[0], row[1], row[2], row[3], row[4] or "") for row in rows
                if type(row[0]) is int and row[0] not in excluded and self._rest.get(row[0], 0.0) <= now
                and (guild is None or row[2] == guild)]

    def _in_city(self, officer):
        return self._zone_of(officer.place) in trade_cities(FACTION)

    def _ration(self, now):
        store = self._store()
        with store.transaction():
            rows = store.conn.execute(
                "SELECT guild,channel,ts FROM realm_line WHERE state='sent' AND kind IN (?,?,?) AND ts>? "
                "ORDER BY ts", (*RIVAL_KINDS, now - LIMITS.day_s)).fetchall()
            member = store.conn.execute(
                "SELECT max(ts) FROM realm_line WHERE state='sent' AND kind='member'").fetchone()[0]
        return _Ration(now, tuple((row[0], row[1], float(row[2])) for row in rows),
                       None if member is None else float(member))

    def _veil(self, text):
        """His guild's name never reaches a rival's prompt: the screen would drop the line."""
        name = self.realm.ports.guild_name()
        if not name:
            return text
        return re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "his guild", text, flags=re.IGNORECASE)

    # -- the tick ------------------------------------------------------------------------------

    async def tick(self):
        context = self._live()
        if context is None:
            return
        now = self.realm.ports.now()
        self._refresh_name(context)
        self._reconcile()
        if not self.screen.ready:
            return
        self._passes(context, now)
        self._position(now)
        self._boast_due(now)
        if now >= self._next_moment:
            self._next_moment = now + MOMENT_S
            if self._rng.random() < self.moment_chance:
                self.maybe_speak(now)

    def _refresh_name(self, context):
        """Once per hello (connection), from an owned child, once the blocklist exists."""
        if context.connection_epoch == self._name_epoch or not self.screen.path.is_file():
            return
        guild, ports = context.guild, self.realm.ports
        if self.realm.spawn(lambda: ports.refresh_guild_name(guild)) is not None:
            self._name_epoch = context.connection_epoch

    def _reconcile(self):
        """Bring each line still prepared or dispatched up to its ledger's state (a send that was
        cancelled, or one an earlier process left behind)."""
        if self.realm.part("direct") is None:
            return
        store = self._store()
        with store.transaction():
            ids = [row[0] for row in store.conn.execute(
                "SELECT id FROM realm_line WHERE kind IS NOT NULL AND state IN ('prepared','dispatched') "
                "ORDER BY id LIMIT 32")]
        for line_id in ids:
            if line_id != self._inflight:
                self._settle(line_id)

    # -- the officers' places --------------------------------------------------------------------

    def _want_all(self):
        self._wanted = {guild for guild, _ in self._guilds()}

    def _passes(self, context, now):
        ports = self.realm.ports
        if context.connection_epoch != self._pass_epoch:
            self._pass_epoch = context.connection_epoch
            self._want_all()
        try:
            state = ports.raid_state()
        except Exception:
            state = self._raid
        if state == "ENDED" and self._raid != "ENDED":
            self._want_all()
        self._raid = state
        if ports.human_online() is True:
            self._offline_pass = None
        elif self._offline_pass is None or now - self._offline_pass >= OFFLINE_PASS_S:
            self._offline_pass = now
            self._want_all()

    def _capital(self):
        """A city place in a capital other than his: uc_bank or tb_bank, else org_bank."""
        zone = self._his_zone()
        found = [self.places.get(name) for name in CAPITALS]
        found = [p for p in found if p is not None and p.kind == "city"]
        return next((p for p in found if p.zone != zone), found[0] if found else None)

    def _move(self, guild, officer, place, now):
        cmd = protocol.RealmMove(uuid.uuid4().hex, officer.guid, place.name)
        try:
            self.realm.queue.submit(cmd, owner=MOVE_OWNER)
        except Exception as error:
            log.debug("realm_speech_move_refused guid=%d error=%s", officer.guid, type(error).__name__)
            self._rest[officer.guid] = now + REST_S
            return False
        self._moving[guild] = (officer.guid, cmd.id, now)
        return True

    def _poll_moves(self, now):
        for guild, (guid, command_id, since) in tuple(self._moving.items()):
            record = self.realm.action_store.get(command_id)
            if record is not None and record.state not in TERMINAL and record.state != "conflict":
                if now - since < MOVE_TIMEOUT_S:
                    continue
            del self._moving[guild]
            if record is not None and record.result_ok is True:
                self._wanted.discard(guild)
            elif record is None or record.result_reason not in _NOT_THEIRS:
                self._rest[guid] = now + REST_S

    def _position(self, now):
        self._poll_moves(now)
        for guild in sorted(self._wanted):
            if guild in self._moving:
                continue
            officers = self._officers(now, guild)
            target = self._capital()
            if not officers or target is None or any(self._in_city(o) for o in officers):
                self._wanted.discard(guild)
                continue
            self._move(guild, officers[0], target, now)

    def _place_officer(self, guild, now):
        if guild in self._moving:
            return False
        officers, target = self._officers(now, guild), self._capital()
        return bool(officers) and target is not None and self._move(guild, officers[0], target, now)

    # -- a rival's line --------------------------------------------------------------------------

    def maybe_speak(self, now):
        """One eligible moment. 'trade', 'general' or 'whisper' when a line is being composed,
        'moving' when an officer is first sent to a place, None when a rule says no."""
        if self._live() is None or not self._may_talk():
            return None
        ration = self._ration(now)
        if not ration.realm_ok() or not ration.spaced():
            return None
        zone = self._his_zone()
        trade = zone in trade_cities(FACTION)
        for guild, guild_name in ration.order(self._guilds()):
            if not ration.guild_ok(guild):
                continue
            officers = self._officers(now, guild)
            if not officers:
                continue
            if trade:
                if not ration.run_ok(self._break):
                    return None
                speaker = next((o for o in officers if self._in_city(o)), None)
                if speaker is not None:
                    return "trade" if self._rival(speaker, "trade", "rival", (
                        "Say one line in Trade, as your guild's officer would: your raids, "
                        "recruiting, gear, or the news above."), line=self._beat(guild, now)) else None
                if self._place_officer(guild, now):
                    return "moving"
                continue
            local = next((o for o in officers if zone is not None and self._zone_of(o.place) == zone), None)
            cities = self.places.by_kind("city", zone=zone) if zone is not None else []
            if local is not None or cities:
                if not ration.run_ok(self._break):
                    return None
                if local is not None:
                    return "general" if self._rival(local, "general", "rival", (
                        "Say one line in General chat, as your guild's officer would.")) else None
                if guild not in self._moving and self._move(guild, officers[0], cities[0], now):
                    return "moving"
                continue
            if ration.whisper_ok():
                return "whisper" if self._rival(officers[0], "whisper", "rival", (
                    "Whisper one line to that player, as a rival officer would: a jab at his "
                    "guild's progress, or a dare.")) else None
        return None

    def answer(self, ev):
        """His Trade line naming a rival guild or officer: one answer from that guild."""
        ports = self.realm.ports
        if (type(ev) is not protocol.ChannelChat or ev.channel != "trade" or ev.via_bridge
                or ev.from_guid != ports.human().guid):
            return False
        key = (ev.ts, ev.text)
        if key in self._answered or self._live() is None or not self._may_talk():
            return False
        now = ports.now()
        named = self._named(ev.text, now)
        if named is None:
            return False
        guild, officers = named
        ration = self._ration(now)
        if not ration.realm_ok() or not ration.guild_ok(guild):
            return False
        speaker = next((o for o in officers if self._in_city(o)), None)
        if speaker is None or not self._rival(speaker, "trade", "answer", (
                f'That player just said in Trade: "{self._veil(ev.text)}". Answer him in Trade, one line.')):
            return False
        self._remember(self._answered, key)
        return True

    def _named(self, text, now):
        """(guild, its officers, the named one first) for the first rival his text names."""
        folded, best = text.casefold(), None
        officers = self._officers(now)
        for guild, name in self._guilds():
            names = [(name, None)] + [(o.name, o.guid) for o in officers if o.guild == guild]
            for word, guid in names:
                match = re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", folded) if word else None
                if match is not None and (best is None or match.start() < best[0]):
                    best = (match.start(), guild, guid)
        if best is None:
            return None
        _, guild, guid = best
        mine = [o for o in officers if o.guild == guild]
        return guild, sorted(mine, key=lambda o: o.guid != guid)

    def gz(self, fact):
        """A grudging gz in Trade after a server-wide kill, once per fact, from a speaking guild
        the server told."""
        fact_id = getattr(fact, "id", None)
        if type(fact_id) is not int or fact_id in self._gz:
            return False
        if self._live() is None or not self._may_talk():
            return False
        now = self.realm.ports.now()
        ration = self._ration(now)
        if not ration.realm_ok() or not ration.run_ok(self._break):
            return False
        store = self._store()
        with store.transaction():
            told = {row[0] for row in store.conn.execute(
                "SELECT guild FROM realm_knows WHERE fact=? AND how='server'", (fact_id,))}
        for guild, _ in ration.order(self._guilds()):
            if guild not in told or not ration.guild_ok(guild):
                continue
            speaker = next((o for o in self._officers(now, guild) if self._in_city(o)), None)
            if speaker is None:
                continue
            situation = (f'The whole server was just told: "{self._veil(fact.text)}". '
                         "Give them a grudging gz in Trade, one line.")
            if self._rival(speaker, "trade", "gz", situation, fact=fact):
                self._remember(self._gz, fact_id)
                return True
            return False
        return False

    def boast(self, guild, fact):
        """The race's hook after a benchmark loss: that guild boasts in Trade, once per fact, at
        the first tick within a day whose rules allow it (rationed as a gz). Never awaits."""
        fact_id = getattr(fact, "id", None)
        if type(guild) is not int or type(fact_id) is not int or fact_id in self._boasts:
            return False
        self._boasts[fact_id] = (guild, fact, self.realm.ports.now() + LIMITS.day_s)
        while len(self._boasts) > _REMEMBER:
            self._boasts.popitem(last=False)
        return True

    def _boast_due(self, now):
        for fact_id, pending in tuple(self._boasts.items()):
            if pending is None:
                continue
            guild, fact, until = pending
            outcome = "never" if now > until else self._boast_now(guild, fact, now)
            if outcome != "wait":
                self._boasts[fact_id] = None
            if outcome != "never":
                return  # One at a time: started, or nothing may be said now.

    def _boast_now(self, guild, fact, now):
        if guild not in {g for g, _ in self._guilds()}:
            return "never"  # Not a guild that speaks.
        if self._live() is None or not self._may_talk():
            return "wait"
        ration = self._ration(now)
        if not ration.realm_ok() or not ration.run_ok(self._break) or not ration.guild_ok(guild):
            return "wait"
        speaker = next((o for o in self._officers(now, guild) if self._in_city(o)), None)
        if speaker is None:
            return "wait"
        situation = (f"That player's guild was just told: \"{self._veil(fact.text)}\". "
                     "Boast about it in Trade, one line.")
        return "started" if self._rival(speaker, "trade", "rival", situation, fact=fact) else "wait"

    def _beat(self, guild, now):
        """The next of the guild's beats from the newest night (night.beat_lines_in_unit) that
        no realm_line has said or dropped yet; "" when none is left or they are stale."""
        from .night import beat_lines_in_unit
        store = self._store()
        try:
            with store.transaction():
                for fact in beat_lines_in_unit(store, guild, human=self.realm.ports.human()):
                    if now - fact.ts > BEAT_FRESH_S:
                        continue
                    if store.conn.execute("SELECT 1 FROM realm_line WHERE guild=? AND text=? AND ts>=? LIMIT 1",
                                          (guild, fact.text, fact.ts)).fetchone() is None:
                        return fact.text
        except Exception as error:  # A night's lines are a bonus: composing still works.
            log.debug("realm_speech_beats_unavailable error=%s", type(error).__name__)
        return ""

    def member_trade(self, guid, name, sketch, line):
        """A member's own Trade answer to a rival line, for talk; stored only in realm_line."""
        context = self._live()
        if context is None or not self._may_talk():
            return False
        situation = (f'{line.name} from <{line.guild_name}> just said in Trade: "{self._veil(line.text)}". '
                     "Answer in Trade in a few words, your way.")
        return self._start(_Spec("member", "member", guid, name, context.guild, "", "trade", 0, 0,
                                 situation, sketch=sketch))

    def _rival(self, officer, channel, kind, situation, *, fact=None, line=""):
        to_guid = self.realm.ports.human().guid if channel == "whisper" else 0
        zone = self._zone_of(officer.place) or 0
        return self._start(_Spec(kind, "officer", officer.guid, officer.name, officer.guild,
                                 officer.guild_name, channel, to_guid, zone, situation, fact=fact,
                                 line=line))

    @staticmethod
    def _remember(seen, key):
        seen[key] = True
        while len(seen) > _REMEMBER:
            seen.popitem(last=False)

    # -- composing and sending -----------------------------------------------------------------

    def _start(self, spec):
        if self._composing is not None:
            return False
        task = self.realm.spawn(lambda: self._deliver(spec))
        if task is None:
            return False
        self._composing = task
        task.add_done_callback(self._composed)
        return True

    def _composed(self, task):
        if self._composing is task:
            self._composing = None

    def _messages(self, spec):
        app, ports = self.realm.app, self.realm.ports
        if spec.kind == "member":
            system = load_prompt("realm_member", app.data_dir, MEMBER_PROMPT)
            user = [f"You play {spec.name}{', ' + spec.sketch if spec.sketch else ''}.", spec.situation]
        else:
            system = load_prompt("realm_rival", app.data_dir, RIVAL_PROMPT)
            cards = ports.realm_dir() / "cards"
            user = [f"You are {spec.name}, an officer of <{spec.guild_name}>."]
            if officer := _card(cards, str(spec.guid), OFFICER_CARD_TOKENS):
                user.append(f"About you: {officer}")
            if guild := _card(cards, f"guild-{spec.guild}", GUILD_CARD_TOKENS):
                user.append(f"Your guild: {guild}")
            try:
                facts = tuple(self.realm.ledger.known(spec.guild))
            except Exception as error:
                log.debug("realm_speech_facts_unavailable error=%s", type(error).__name__)
                facts = ()
            if facts:
                user.append("What your guild knows:\n" + "\n".join(f"- {self._veil(f.text)}" for f in facts))
            user.append(f"A player of another guild you may run into: "
                        f"{ports.human().describe(include_real_name=False)}.")
            user.append(spec.situation)
        user.append("Write the line only.")
        return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user)}]

    async def compose(self, spec):
        """One line for the spec from the live model (one BACKGROUND call, LIMITS' 20 s
        deadline), repaired as the pipeline repairs; None when the model gives nothing."""
        text = await self.realm.ports.llm_line(self._messages(spec),
                                               deadline_s=LIMITS.rival_line_deadline_s)
        if text is None:
            return None
        return repair(_one_line(text), speaker=spec.name) or None

    async def _deliver(self, spec):
        written = spec.line
        for _ in range(2):  # One retry after a failed screen, composed.
            text, written = written or await self.compose(spec), ""
            if text is None:
                return
            reason = self.screen.check(text, speaker_kind=spec.speaker_kind)
            if reason is None:
                await self._send(spec, text)
                return
            line_id = self._insert(spec, text, state="dropped", reason=reason)
            log.info("realm_line_dropped id=%d reason=%s", line_id, reason)

    def _insert(self, spec, text, *, state, reason=None, command_id=None):
        store = self._store()
        with store.transaction():
            return store.conn.execute(
                "INSERT INTO realm_line(ts,guid,guild,channel,text,command_id,kind,state,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (self.realm.ports.now(), spec.guid, spec.guild, spec.channel,
                                               text, command_id, spec.kind, state, reason)).lastrowid

    async def _send(self, spec, text):
        realm, store = self.realm, self._store()
        text, wire = _item_links(realm.app, text)
        direct, context = realm.part("direct"), self._live()
        if direct is None or context is None or not self._open():
            self._insert(spec, text, state="dropped", reason="silenced")
            return
        cmd = protocol.RealmSay(uuid.uuid4().hex, spec.guid, spec.channel, wire, spec.to_guid)
        with store.transaction():
            line_id = self._insert(spec, text, state="prepared", command_id=cmd.id)
            prepared = direct.prepare(cmd, owner=OWNER, ref=f"line:{line_id}", context=context,
                                      expects_job=False)
            if prepared is None:
                store.conn.execute("UPDATE realm_line SET state='dropped',command_id=NULL,reason='not_live' "
                                   "WHERE id=?", (line_id,))
        if prepared is None:
            return
        self._inflight, ack = line_id, None
        try:
            ack = await realm.ports.send(cmd)
        except Exception as error:  # The ledger records the outcome; a line is never resent.
            log.debug("realm_line_unsent id=%d error=%s", line_id, type(error).__name__)
        finally:
            self._inflight = None
            try:
                direct.abandon_unsent(cmd)  # Only a send that never reached its barrier.
            except Exception as error:
                log.warning("realm_line_close_failed id=%d error=%s", line_id, type(error).__name__)
            state = self._settle(line_id, ack)
        self._after(spec, line_id, text, state, ack)

    def _settle(self, line_id, ack=None):
        direct = self.realm.part("direct")
        if direct is None:
            return None
        state, reason = _line_state(direct.state(OWNER, f"line:{line_id}"), ack)
        if state != "prepared":
            store = self._store()
            with store.transaction():
                store.conn.execute("UPDATE realm_line SET state=?,reason=? WHERE id=? "
                                   "AND state IN ('prepared','dispatched')", (state, reason, line_id))
        return state

    def _after(self, spec, line_id, text, state, ack):
        now = self.realm.ports.now()
        if state == "sent":
            if spec.kind == "member":
                self._break = now
            elif spec.channel == "trade":
                talk = self.realm.part("talk")
                if talk is not None:
                    try:
                        talk.react(spec.fact, SpokenLine(line_id, spec.kind, spec.guid, spec.name, spec.guild,
                                                         spec.guild_name, spec.channel, text, spec.zone))
                    except Exception as error:
                        log.warning("realm_reaction_failed error=%s", type(error).__name__)
            return
        if spec.speaker_kind != "officer" or state != "refused" or ack is None or ack.ok is not False:
            return
        if ack.error in ("not_in_city", "not_in_channel"):
            try:
                realm_store.set_actor_place(self._store().conn, spec.guid, "", now)
            except ValueError:
                pass
        self._rest[spec.guid] = now + REST_S

    # -- events ----------------------------------------------------------------------------------

    async def on_event(self, ev):
        name = event_type(ev)
        try:
            if name == "channel_chat":
                self._on_channel(ev)
            elif name == "realm_job":
                self._on_job(ev)
            elif name == "realm_kill":
                fact = self._kill_fact(ev)
                if fact is not None:
                    self.gz(fact)
            elif name in ("pvp_kill", "duel_end"):
                self._on_gank(ev)
            elif name == "logout" and getattr(ev, "human", False) is True:
                self._offline_pass = None
        except Exception as error:  # Never the Realm's event failure.
            log.warning("realm_speech_event_failed type=%s error=%s", name, type(error).__name__)

    def _on_channel(self, ev):
        if type(ev) is not protocol.ChannelChat:
            return
        ports = self.realm.ports
        if ev.from_guid == ports.human().guid and not ev.via_bridge:
            self._break = ports.now()
            self.answer(ev)
            return
        store = self._store()
        with store.transaction():
            member = store.conn.execute("SELECT 1 FROM cast_member WHERE guid=?", (ev.from_guid,)).fetchone()
        if member is not None:
            self._break = ports.now()

    def _on_job(self, ev):
        """Any realm_move that came back done moves its actor's place record."""
        if (type(ev) is not protocol.RealmJob or ev.kind != "realm_move" or ev.state != "done"
                or ev.pvp_owner):
            return
        record = self.realm.action_store.get(ev.id)
        if record is None or record.kind != "realm_move":
            return
        try:
            cmd, _ = decode(record.payload)
        except (ValueError, TypeError, KeyError):
            return
        if cmd.guid != ev.guid:
            return
        try:
            realm_store.set_actor_place(self._store().conn, cmd.guid, cmd.place, self.realm.ports.now())
        except ValueError:
            pass  # Not a realm actor: a cast member's move.

    def _kill_fact(self, ev):
        store = self._store()
        with store.transaction():
            row = store.conn.execute("SELECT fact FROM realm_kill WHERE kill_id=?", (ev.kill_id,)).fetchone()
            if row is None or row[0] is None:
                return None
            read = public_fact_in_unit(store, row[0], human=self.realm.ports.human())
        return read[0] if type(read) is tuple else None

    # -- the gankers -----------------------------------------------------------------------------

    def _gankers(self):
        excluded = self._excluded()
        store = self._store()
        with store.transaction():
            rows = store.conn.execute(
                "SELECT a.guid FROM realm_actor a JOIN realm_guild g ON g.id=a.guild WHERE a.status='active' "
                "AND a.role='ganker' AND g.status='active'").fetchall()
        return {row[0] for row in rows if type(row[0]) is int and row[0] not in excluded}

    def emote_for(self, ev):
        """spit or laugh over a corpse a ganker made, wave at a ganker's duel; never the model."""
        if type(ev) is protocol.PvpKill:
            return self._rng.choice(("spit", "laugh")) if ev.killer in self._gankers() else None
        if type(ev) is protocol.DuelEnd:
            gankers = self._gankers()
            return "wave" if ev.winner in gankers or ev.loser in gankers else None
        return None

    def _on_gank(self, ev):
        context, direct = self._live(), self.realm.part("direct")
        if context is None or direct is None or not self.screen.ready:
            return
        emote = self.emote_for(ev)
        if emote is None:
            return
        if type(ev) is protocol.PvpKill:
            guid, target = ev.killer, ev.victim
        else:
            guid = ev.winner if ev.winner in self._gankers() else ev.loser
            target = ev.loser if guid == ev.winner else ev.winner
        cmd = protocol.RealmEmote(uuid.uuid4().hex, guid, emote, target)
        if direct.prepare(cmd, owner=OWNER, ref=f"emote:{event_type(ev)}:{ev.ts}:{guid}", context=context,
                          expects_job=False) is None:
            return
        ports = self.realm.ports

        async def send():
            try:
                await ports.send(cmd)
            except Exception as error:
                log.debug("realm_emote_unsent error=%s", type(error).__name__)

        def close(_task=None):
            try:
                direct.abandon_unsent(cmd)
            except Exception as error:
                log.warning("realm_emote_close_failed error=%s", type(error).__name__)

        task = self.realm.spawn(send)
        if task is None:
            close()
        else:
            task.add_done_callback(close)


def attach(realm):
    """The speech part on its events. No I/O: places, the screen and every read wait for the
    Realm's own tick."""
    return Speech(realm), EVENTS
