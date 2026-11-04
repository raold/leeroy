"""Bounded, header-last telemetry assembly and replay-safe pull persistence.

This module does not attach handlers. Connection changes must call
``Ledger.on_connection`` before accepting another connection's parts. A complete
record proves receipt of the advertised parts, not more precise measurements
than the header's individual provenance labels permit.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import logging
import re

from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.guild.roles import role_of

from . import protocol as p, store


log = logging.getLogger(__name__)
MAX_MEMBERS, MAX_DEATHS, MAX_SECONDS = 40, 80, 1200
MAX_TRACKS = (MAX_MEMBERS + 1) * (MAX_SECONDS // 2)
MAX_POINTS = (MAX_MEMBERS + 1) * MAX_SECONDS
MAX_PENDING, MAX_CLOSED = 8, 1024
PART_TTL = 600
_I64, _U32 = (1 << 63) - 1, (1 << 32) - 1
_ID = re.compile(r"(0|[1-9][0-9]{0,18})-(0|[1-9][0-9]{0,9})-(0|[1-9][0-9]{0,9})\Z")
_CONTEXT = re.compile(r"(0|[1-9][0-9]{0,9}):(0|[1-9][0-9]{0,9}):([0-3])\Z")
_ARRAYS = ("xs", "ys", "o", "hp", "power", "target")


def _int(value, low=0, high=_I64):
    return type(value) is int and low <= value <= high


def _ident(value):
    match = _ID.fullmatch(value) if type(value) is str else None
    if match:
        parts = tuple(map(int, match.groups()))
        if parts[0] <= _I64 and 0 < parts[1] <= _U32 and parts[2] <= _U32:
            return parts
    return None


def context_key(map_id, boss_id, difficulty):
    if not (_int(map_id, 0, _U32) and _int(boss_id, 0, _U32) and _int(difficulty, 0, 3)):
        raise ValueError("invalid training context")
    return f"{map_id}:{boss_id}:{difficulty}"


def combat_role(cls, spec_tab, raid_role, *, tank=False):
    if tank:
        return "tank"
    role = role_of(cls, spec_tab, raid_role)
    # A blood build is not proof of tanking; neither is a rival's unlabelled
    # protection/feral build. The recorder supplies their observed tank flag.
    if role == "tank" and (raid_role is None or cls == 6 and raid_role != "tank"):
        role = "damage"
    if role in {"tank", "healer"}:
        return role
    if raid_role in {"melee", "ranged"}:
        return raid_role
    melee = cls in {4, 6} or cls == 1 and spec_tab in {0, 1, 2} or (
        cls == 2 and spec_tab in {1, 2}) or cls in {7, 11} and spec_tab == 1
    return "melee" if melee else "ranged"


@dataclass(frozen=True)
class PullRecord:
    header: p.Pull
    members: tuple[p.PullMember, ...]
    deaths: tuple[p.PullDeath, ...]
    tracks: dict[tuple[int, int], dict[str, list[int]]]
    casts: tuple[p.TrackCast, ...]
    complete: bool


@dataclass(frozen=True)
class PullContext:
    kind: str
    lead_guid: int
    raid_id: int | None
    card_id: int | None
    arm_id: int | None
    change: str
    covariates: dict[str, int | str]


@dataclass
class _Parts:
    received: float
    wire_ts: int | None = None
    members: dict = field(default_factory=dict)
    deaths: dict = field(default_factory=dict)
    tracks: dict = field(default_factory=dict)
    entities: set = field(default_factory=set)
    cast_chunks: int = 0
    points: int = 0
    invalid: bool = False


def _header_ok(h):
    return (_ident(h.id) == (h.start_ms, h.instance_id, h.boss_id)
        and _int(h.start_ms) and _int(h.instance_id, 1, _U32) and _int(h.boss_id, 0, _U32)
        and _int(h.ts) and _int(h.map, 0, _U32) and _int(h.difficulty, 0, 3)
        and h.size == (25 if h.difficulty & 1 else 10) and type(h.size) is int
        and _int(h.end_ms, h.start_ms) and _int(h.members, 0, MAX_MEMBERS)
        and _int(h.deaths, 0, MAX_DEATHS) and _int(h.tracks, 0, MAX_TRACKS)
        and h.outcome in {"killed", "wiped", "aborted"} and h.source in {"tracked", "benchmark"}
        and type(h.assisted) is bool and _int(h.boss_entry, 0, _U32) and _int(h.upd_mean_ms, 0, _U32)
        and all(type(v) is str and len(v) <= 128 for v in (
            h.damage_basis, h.healing_basis, h.spell_basis, h.interrupt_basis)))


def _member_ok(m):
    if not (_int(m.ts) and _int(m.guid, 1, _U32) and m.class_ in {1, 2, 3, 4, 5, 6, 7, 8, 9, 11}
            and type(m.class_) is int and _int(m.spec_tab, -1, 2) and type(m.tank) is bool
            and _int(m.ilvl, 0, 65535) and all(_int(getattr(m, key)) for key in (
                "damage", "damage_boss", "healing", "overheal", "taken", "deaths", "interrupts", "active_ms"))):
        return False
    lists = ((m.done, p.SpellTotal, 33), (m.taken_by, p.TakenTotal, 33),
             (m.debuffs, p.DebuffTotal, 24), (m.casts, p.CastTotal, 48))
    for rows, cls, limit in lists:
        if type(rows) is not tuple or len(rows) > limit:
            return False
        seen = set()
        for row in rows:
            if type(row) is not cls or any(not _int(getattr(row, key)) for key in cls.__dataclass_fields__):
                return False
            if row.spell > _U32 or getattr(row, "source", 0) > _U32:
                return False
            key = (row.spell, getattr(row, "source", 0))
            if key in seen:
                return False
            seen.add(key)
    return m.damage_boss <= m.damage


def _death_ok(d):
    if not (_int(d.ts) and _int(d.guid, 1, _U32) and _int(d.at_ms, 0, _U32)
            and _int(d.spell, 0, _U32) and _int(d.source, 0, _U32)
            and _int(d.x, -(1 << 31), (1 << 31) - 1) and _int(d.y, -(1 << 31), (1 << 31) - 1)
            and type(d.hits) is tuple and len(d.hits) <= 8):
        return False
    previous = -1
    for hit in d.hits:
        if (type(hit) is not p.LastHit or not _int(hit.at_ms, max(0, previous), d.at_ms)
                or not _int(hit.spell, 0, _U32) or not _int(hit.source, 0, _U32)
                or not _int(hit.amount) or not _int(hit.hp, 0, _U32)):
            return False
        previous = hit.at_ms
    return True


def _track_ok(t):
    if not (_int(t.ts) and _int(t.who, 0, _U32) and _int(t.entry, 0, _U32)
            and _int(t.from_s, 0, MAX_SECONDS - 1) and all(type(getattr(t, k)) is tuple for k in _ARRAYS)
            and type(t.casts) is tuple and len(t.casts) <= 64):
        return False
    n = len(t.xs)
    if n > 600 or t.from_s + n > MAX_SECONDS or len(t.ys) != n or len(t.hp) != n:
        return False
    if t.who:
        if t.entry or len(t.power) != n or t.o or t.target or t.casts or not n:
            return False
    elif len(t.o) != n or len(t.target) != n or t.power or not n and (not t.casts or t.from_s):
        return False
    bounds = {"xs": (-(1 << 31), (1 << 31) - 1), "ys": (-(1 << 31), (1 << 31) - 1),
              "o": (-(1 << 15), (1 << 15) - 1), "hp": (0, 100), "power": (0, 100), "target": (0, _U32)}
    if any(not _int(value, *bounds[key]) for key in _ARRAYS for value in getattr(t, key)):
        return False
    return all(type(c) is p.TrackCast and _int(c.spell, 1, _U32)
               and _int(c.at_s, 0, MAX_SECONDS) for c in t.casts)


class PullAssembler:
    """The final header seals a pull, even when incomplete. No late repair.

    Sparse tracks contain explicit ``at_s`` indices alongside each nonempty
    array. Never interpret list positions as seconds without those indices.
    Identical duplicate chunks are conservatively invalid, not counted twice.
    """

    MAX_PENDING = MAX_PENDING
    MAX_TRACKS = MAX_TRACKS
    MAX_POINTS = MAX_POINTS

    def __init__(self, clock):
        self.clock = clock
        self._parts = OrderedDict()
        self._closed = OrderedDict()

    def _finish(self, ident):
        self._parts.pop(ident, None)
        self._closed[ident] = None
        while len(self._closed) > MAX_CLOSED:
            self._closed.popitem(last=False)

    def reset(self):
        count = len(self._parts)
        self._parts.clear()
        if count:
            log.info("tg_parts_reset count=%d", count)
        return count

    def expire(self, now):
        expired = [key for key, value in self._parts.items() if now - value.received >= PART_TTL]
        for key in expired:
            self._finish(key)
        if expired:
            log.info("tg_parts_expired count=%d", len(expired))
        return len(expired)

    def add(self, ev):
        if type(ev) not in (p.Pull, p.PullMember, p.PullDeath, p.PullTrack):
            return None
        ident = ev.id if type(ev) is p.Pull else ev.pull
        if not _ident(ident) or ident in self._closed:
            return None
        self.expire(self.clock.now())
        if ident in self._closed:
            return None
        parts = self._parts.get(ident)
        if type(ev) is p.Pull:
            if not _header_ok(ev):
                self._finish(ident)
                log.warning("tg_invalid_header")
                return None
            result = self._assemble(ev, parts or _Parts(self.clock.now()))
            self._finish(ident)
            return result
        if parts is None:
            if len(self._parts) >= self.MAX_PENDING:
                self._finish(next(iter(self._parts)))
                log.warning("tg_parts_capacity")
            parts = self._parts[ident] = _Parts(self.clock.now())
        if parts.wire_ts is None:
            parts.wire_ts = ev.ts
        elif parts.wire_ts != ev.ts:
            parts.invalid = True
        if type(ev) is p.PullMember:
            if not _member_ok(ev) or ev.guid in parts.members or len(parts.members) >= MAX_MEMBERS:
                parts.invalid = True
            else:
                parts.members[ev.guid] = ev
        elif type(ev) is p.PullDeath:
            key = (ev.guid, ev.at_ms)
            if not _death_ok(ev) or key in parts.deaths or len(parts.deaths) >= MAX_DEATHS:
                parts.invalid = True
            else:
                parts.deaths[key] = ev
        else:
            key = (ev.who, ev.entry, ev.from_s)
            entity = (ev.who, ev.entry)
            if (not _track_ok(ev) or key in parts.tracks or len(parts.tracks) >= self.MAX_TRACKS
                    or entity not in parts.entities and len(parts.entities) >= MAX_MEMBERS + 1
                    or not ev.who and any(who == 0 and entry != ev.entry for who, entry in parts.entities)
                    or ev.casts and parts.cast_chunks
                    or parts.points + len(ev.xs) > self.MAX_POINTS
                    or sum(x.points for x in self._parts.values()) + len(ev.xs) > self.MAX_POINTS * 2
                    or sum(len(x.tracks) for x in self._parts.values()) >= self.MAX_TRACKS * 2):
                parts.invalid = True
            else:
                parts.tracks[key] = ev
                parts.entities.add(entity)
                parts.cast_chunks += bool(ev.casts)
                parts.points += len(ev.xs)
        return None

    @staticmethod
    def _assemble(h, parts):
        complete = not parts.invalid and parts.wire_ts in (None, h.ts) and (
            len(parts.members), len(parts.deaths), len(parts.tracks)) == (
            h.members, h.deaths, h.tracks)
        duration = h.end_ms - h.start_ms
        members = tuple(parts.members[g] for g in sorted(parts.members))
        deaths = tuple(parts.deaths.values())
        if any(m.active_ms > duration for m in members):
            complete = False
        if any(d.guid not in parts.members or d.at_ms > duration for d in deaths):
            complete = False
        tracks, casts, cast_chunks = {}, [], 0
        for key in sorted(parts.tracks):
            t = parts.tracks[key]
            owner = (t.who, t.entry)
            data = tracks.setdefault(owner, {"at_s": [], **{k: [] for k in _ARRAYS}})
            times = list(range(t.from_s, t.from_s + len(t.xs)))
            if (t.who and t.who not in parts.members or not t.who and t.entry != h.boss_entry
                    or times and times[-1] * 1000 >= duration
                    or data["at_s"] and times and times[0] <= data["at_s"][-1]):
                complete = False
            # An overlap is invalid and omitted rather than rewriting a first observation.
            if data["at_s"] and times and times[0] <= data["at_s"][-1]:
                continue
            data["at_s"].extend(times)
            for name in _ARRAYS:
                data[name].extend(getattr(t, name))
            if t.casts:
                cast_chunks += 1
                casts.extend(t.casts)
        if cast_chunks > 1 or len(casts) > 64 or any(c.at_s * 1000 > duration for c in casts):
            complete = False
        return PullRecord(h, members, deaths, tracks, tuple(sorted(casts, key=lambda c: (c.at_s, c.spell))), complete)


class Ledger:
    def __init__(self, ports):
        self.ports = ports
        self.assembler = PullAssembler(ports.clock)
        self.after_store = []
        self._run = None
        self._ready = OrderedDict()

    def set_run(self, run):
        self._run = run

    def _run_id(self):
        return getattr(getattr(self._run, "session", None), "raid_id", None)

    def _get(self, key, default=None):
        row = self.ports.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return default

    def _put(self, key, value):
        with self.ports.conn:
            self.ports.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (key, json.dumps(value, separators=(",", ":"), allow_nan=False)))

    def set_in_play(self, context, *, card_id=None, arm_id=None, change=""):
        if (not _CONTEXT.fullmatch(context) or any(v is not None and not _int(v, 1) for v in (card_id, arm_id))
                or type(change) is not str or len(change) > 5000):
            raise ValueError("invalid in-play context")
        raid_id = self._run_id()
        if not _int(raid_id, 1):
            return
        key = f"tg:in-play:{raid_id}:{context}"
        history = self._get(key, [])
        if not isinstance(history, list):
            raise ValueError("invalid saved in-play context")
        now = int(self.ports.clock.now() * 1000)
        history = [item for item in history if item["at_ms"] != now]
        history.append(dict(at_ms=now, card_id=card_id, arm_id=arm_id, change=change))
        self._put(key, sorted(history, key=lambda item: item["at_ms"])[-64:])

    def mark_unscoreable(self, context):
        if not _CONTEXT.fullmatch(context):
            raise ValueError("invalid training context")
        raid_id = self._run_id()
        if _int(raid_id, 1):
            self._put(f"tg:unscoreable:{raid_id}:{context}", {"from_ms": int(self.ports.clock.now() * 1000)})

    def first_pull_ts(self):
        value = self._get("tg_first_pull")
        return float(value) if type(value) in (int, float) else None

    def expire(self, now=None):
        return self.assembler.expire(self.ports.clock.now() if now is None else now)

    async def on_connection(self, connected):
        # No part received on one socket can complete another socket's suffix.
        self.assembler.reset()

    def _matches(self, h, map_id, size, heroic, started, ended, raid_id):
        bound = self._get(f"guild:raid:{raid_id}:instance")
        return (h.map == map_id and h.size == size and (h.difficulty >= 2) == heroic
                and started * 1000 <= h.start_ms and (ended is None or h.start_ms < ended * 1000)
                and (bound is None or type(bound) is int and bound == h.instance_id))

    def _raid(self, h):
        if h.source == "benchmark":
            return "benchmark", 0, None, False
        run = self._run
        if run is not None:
            row = self.ports.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (run.session.raid_id,)).fetchone()
            raid = CATALOG.get(row["raid_key"]) if row else None
            if (raid and self._matches(h, raid.map_id, row["size"], bool(row["heroic"]),
                    row["started"], row["ended"], row["raid_id"])
                    and run.session.ctx.map_id == raid.map_id
                    and (run.session.ctx.instance_id is None or run.session.ctx.instance_id == h.instance_id)):
                return ("his" if run.human_in_raid else "offline", row["lead_guid"], row["raid_id"],
                        row["ended"] is None)
        old = self.ports.raid_at(h.start_ms)
        if old and self._matches(h, old.map_id, old.size, old.heroic, old.started, old.ended, old.raid_id):
            return "offline" if old.offline else "his", old.lead_guid, old.raid_id, False
        return "other", 0, None, False

    def _context(self, rec):
        h = rec.header
        kind, lead, raid_id, live = self._raid(h)
        # A still-open raid may also have an hours-old backlog. Today's persona
        # choices and habits cannot be presented as historical observations.
        live = live and abs(int(self.ports.clock.now() * 1000) - h.ts) <= 60_000
        cast = self.ports.cast()
        roles = {}
        for member in rec.members:
            persona = self.ports.persona(member.guid) if live and member.guid in cast else None
            raid_role = persona.play.raid_role if persona is not None else None
            roles[member.guid] = combat_role(member.class_, member.spec_tab, raid_role, tank=member.tank)
        tanks, healers = sum(v == "tank" for v in roles.values()), sum(v == "healer" for v in roles.values())
        covariates = dict(ilvl=round(sum(m.ilvl for m in rec.members) / len(rec.members)) if rec.members else 0,
            tanks=tanks, healers=healers, comp=f"{tanks}-{healers}-{len(roles) - tanks - healers}",
            human_in_raid=int(kind == "his"), assisted=int(h.assisted),
            drunk=sum(self.ports.drunk(g) for g in roles if g in cast) if live else "unknown",
            flask_skips=len(set(roles) & self.ports.flask_skips()) if live else "unknown",
            state_basis="receipt_time" if live else "historical_unavailable",
            role_basis="recorded_and_receipt_persona" if live else "recorded_tank_and_spec")
        context = context_key(h.map, h.boss_id, h.difficulty)
        history = self._get(f"tg:in-play:{raid_id}:{context}", []) if raid_id is not None else []
        eligible = [item for item in history if item["at_ms"] <= h.start_ms]
        chosen = max(eligible, key=lambda item: item["at_ms"], default={})
        token = None
        if raid_id is not None:
            key = f"tg:unscoreable:{raid_id}:{context}"
            row = self.ports.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            value = self._get(key)
            if row and isinstance(value, dict) and _int(value.get("from_ms")) and value["from_ms"] <= h.start_ms:
                token = (key, row[0])
        return (PullContext(kind, lead, raid_id, chosen.get("card_id"), chosen.get("arm_id"),
                            chosen.get("change", ""), covariates), roles, token)

    async def on_event(self, ev):
        if type(ev) not in (p.Pull, p.PullMember, p.PullDeath, p.PullTrack):
            return
        ident = ev.id if type(ev) is p.Pull else ev.pull
        if not _ident(ident):
            return
        if self.ports.conn.execute("SELECT 1 FROM tg_pull WHERE id=?", (ident,)).fetchone():
            return
        ready = self._ready.get(ident)
        if ready is not None:
            if type(ev) is not p.Pull or ev != ready[0].header:
                return
        else:
            rec = self.assembler.add(ev)
            if rec is None:
                return
            # Sealing is irreversible; retain the record before fallible port
            # and database context reads as well as before the write itself.
            ready = (rec, None, None, None, self.ports.clock.now())
            if len(self._ready) >= MAX_PENDING:
                self._ready.popitem(last=False)
                log.warning("tg_store_retry_capacity")
            self._ready[ident] = ready
        rec, ctx, roles, token, received = ready
        if ctx is None:
            ctx, roles, token = self._context(rec)
            self._ready[ident] = (rec, ctx, roles, token, received)
        inserted = store.put_pull(self.ports.conn, rec, ctx, roles, received=received,
            first_pull_ts=received, consume_unscoreable=token)
        self._ready.pop(ident, None)
        if not inserted:
            return
        for callback in tuple(self.after_store):
            try:
                await callback(rec, ctx)
            except Exception:
                # Do not log raw bridge data, callback text, or private persona fields.
                log.warning("tg_after_store_failed")
