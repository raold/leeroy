"""The raid lineup (D4 Part B): the night's raid lead proposes a lineup for each raid night the
calendar desk posted, the D2 engine argues it in #raid-planning (Part A's stage seeds), and two
hours later she settles it with a post in her name: the code-built roster by role, with the bench.
The guild's own formation (RaidNight._form) then forms from it, which puts it in effect; one that
never formed is talk only once its night is over. While a posted lineup's night is near, the
add-from-guild picker in game holds its roster (native guild_add_roster, Leeroy.GuildAdd.FollowRoster).

Everything here is built from rows: the cast, guild roles, the bench streak, 30-day attendance,
the armory's item levels and the raid buffs each spec brings. He is never in a lineup or on its
bench; he always raids. Logs carry decision ids, counts and codes only."""
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import logging
from uuid import uuid4

from leeroy_mind.guild.comp import (TARGETS, NoTankError, candidates_from, choose_leads, coverage,
                                    select_comp)
from leeroy_mind.guild.store import attendance_counts, bench_streaks, last_led
from . import ledger
from .positions import lineup_positions

log = logging.getLogger(__name__)

LINEUP_AHEAD_S = (6 * 3600, 30 * 3600)   # propose between 30 h and 6 h before a posted raid night
SETTLE_AFTER_S = 2 * 3600                # the lead posts it this long after proposing it
NIGHT_S = 6 * 3600                       # a raid night ends this long after its posted time
ROSTER_AHEAD_S = 6 * 3600                # the picker holds the roster from here to the night's end
BENCH_MAX = 10                           # the bench a lineup posts: the next members by priority
CHANNEL = 'raid-planning'
DAY_S = 86400.0
ROLE_WORDS = (('tank', 'Tanks'), ('healer', 'Healers'), ('damage', 'Damage'))


def subject_of(event_id: int) -> str:
    return f'raid:{event_id}'


def _event_of(subject: str) -> int | None:
    tail = subject.removeprefix('raid:')
    return int(tail) if tail.isdecimal() else None


def _seed(event_id: int) -> int:
    return int.from_bytes(hashlib.sha256(f'decisions:lineup:{event_id}'.encode()).digest()[:8], 'big')


def raid_events(conn, *, since: float, until: float) -> list:
    """The raid nights the calendar desk posted with their time in [since, until]."""
    return conn.execute("SELECT id,title,at FROM realm_calendar WHERE state='posted' AND kind='raid' "
                        'AND at>=? AND at<=? ORDER BY at,id', (since, until)).fetchall()


def _target(conn, now, available):
    """A raid night names no instance: the size and difficulty of his newest raid in the last two
    weeks (culture.loot.target_raid), else 25 with 22 or more available, else 10 (as _form)."""
    from leeroy_mind.culture.loot import target_raid
    target = target_raid(conn, now)
    if target is not None and target.size in TARGETS:
        return target.size, target.heroic
    return (25 if available >= 22 else 10), False


def build_lineup(conn, armory_gear: Mapping[int, int], cal, *, now, members: Mapping, personas: Mapping,
                 human_guid: int, away=frozenset()) -> dict | None:
    """The lineup for a posted raid night: select_comp over the cast (never him) with each member's
    item level from the armory and the raid-buff tiebreak, the night's leads (choose_leads), and
    the bench: the next BENCH_MAX raiders and officers by priority. None when no tank can lead it.
    {"size", "heroic", "lead", "tank", "loot", "members", "bench", "tanks", "healers", "coverage"}"""
    cast = {g: m for g, m in members.items() if g != human_guid and personas.get(g) is not None}
    found = candidates_from(cast, personas, bench_streaks(conn, cast),
                            attendance_counts(conn, cast, now - 30 * DAY_S), away)
    # A night ahead: bodies are pinned online, so a member at 80 who is not away is available
    cands = [replace(c, available=c.guid not in away and cast[c.guid].level >= 80,
                     ilvl=max(0, int(armory_gear.get(c.guid, 0) or 0))) for c in found]
    size, heroic = _target(conn, now, sum(c.available for c in cands))
    try:
        leads = choose_leads(cands, requested=None, human_leads=False, human_guid=human_guid,
                             last_led=last_led(conn))
        required = sorted({leads.tank_guid, leads.lead_guid, leads.loot_guid} - {human_guid})
        comp = select_comp(size, cands, must_include=required, human_in_raid=True,
                           seed=_seed(cal['id']), buff_ties=True)
    except (NoTankError, ValueError):
        return None
    by = {c.guid: c for c in cands}
    return {'size': size, 'heroic': heroic, 'lead': leads.lead_guid, 'tank': leads.tank_guid,
            'loot': leads.loot_guid, 'members': list(comp.members), 'bench': list(comp.bench[:BENCH_MAX]),
            'tanks': [g for g in comp.members if comp.roles[g] == 'tank'],
            'healers': [g for g in comp.members if comp.roles[g] == 'healer'],
            'coverage': coverage(size, [by[g] for g in comp.members])}


def _guids(value, key) -> list[int]:
    items = value.get(key, ())
    return [g for g in items if type(g) is int and g > 0] if isinstance(items, list) else []


def roster_text(value: dict, names: Mapping[int, str], *, title: str, at: datetime) -> str:
    """The lead's post: the roster by role with the bench, built from the lineup's rows."""
    name = lambda g: names.get(g, f'#{g}')  # noqa: E731
    tanks, healers = set(_guids(value, 'tanks')), set(_guids(value, 'healers'))
    members = _guids(value, 'members')
    roles = {'tank': [g for g in members if g in tanks], 'healer': [g for g in members if g in healers],
             'damage': [g for g in members if g not in tanks and g not in healers]}
    size = value.get('size') if type(value.get('size')) is int else len(members) + 1
    head = f"Lineup for {title}, {at:%A %H:%M} ({size}-man{' heroic' if value.get('heroic') is True else ''}):"
    lines = [head] + [f'{words}: ' + (', '.join(name(g) for g in roles[role]) or 'none')
                      for role, words in ROLE_WORDS]
    bench = _guids(value, 'bench')
    lines.append('Bench: ' + (', '.join(name(g) for g in bench) or 'nobody'))
    missing = [code.removeprefix('no_') for code in value.get('coverage', ()) if isinstance(code, str)
               and code.startswith('no_')]
    if missing:
        lines.append('Missing: ' + ', '.join(m.capitalize() for m in missing))
    return '\n'.join(lines)


@dataclass(frozen=True, slots=True)
class FormLineup:
    """Tonight's posted lineup as RaidNight._form takes it: who it seats, in order, and its bench."""
    decision: int
    members: tuple[int, ...]
    bench: frozenset[int]
    tanks: frozenset[int]
    healers: frozenset[int]


def tonight(conn, now: float) -> FormLineup | None:
    """The settled or in-effect lineup of the raid night whose window holds now (from ROSTER_AHEAD_S
    before its posted time to its end): a formation now forms from it."""
    for event in raid_events(conn, since=now - NIGHT_S, until=now + ROSTER_AHEAD_S):
        if not event['at'] - ROSTER_AHEAD_S <= now < event['at'] + NIGHT_S:
            continue
        subject = subject_of(event['id'])
        row = ledger.pending(conn, 'lineup', subject)
        if row is None or row['status'] != 'settled':
            row = ledger.current(conn, 'lineup', subject)
        if row is None:
            continue
        try:
            value = json.loads(row['value'])
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        return FormLineup(row['id'], tuple(_guids(value, 'members')), frozenset(_guids(value, 'bench')),
                          frozenset(_guids(value, 'tanks')), frozenset(_guids(value, 'healers')))
    return None


def formed(store, decision: int, raid_id: int, *, now) -> bool:
    """The guild's formation formed from this lineup: it is in effect, with the raid it formed.
    False when it is no longer settled (in effect already, overruled, ended)."""
    row = ledger.get(store.conn, decision)
    if row is None or row['status'] != 'settled':
        return False
    ledger.take_effect(store, decision, effect_ref={'raid': raid_id}, now=now)
    return True


class LineupRules:
    """The lineup kind: proposed, posted and ended by its tick; in effect once formed from. An
    in-effect lineup formed a raid, which moved bodies: the game cannot undo it, so his overrule
    then stops only its future effect (the picker's roster). Before that, overruling it stops it."""
    kind = 'lineup'

    def __init__(self):
        self._sent = None          # the roster native holds as far as the mind knows; None: unknown
        self._connected = False    # a hello since the bridge last connected
        self._unbuilt = set()      # raid nights no lineup could be built for (logged once each)

    def reversible(self, row) -> bool:
        return False

    async def undo(self, app, row) -> None:
        raise RuntimeError('lineup_not_reversible')

    def on_hello(self, ev) -> None:
        self._connected, self._sent = True, None    # a worldserver start cleared it: send again

    def on_connection(self, connected: bool) -> None:
        if not connected:
            self._connected, self._sent = False, None

    async def tick(self, app, now: float) -> None:
        store = app.store
        await self._propose(app, now)
        self._settle(app, now)
        self._end(store, now)
        await self._roster(app, now)

    async def _propose(self, app, now):
        lo, hi = LINEUP_AHEAD_S
        due = [e for e in raid_events(app.store.conn, since=now + lo, until=now + hi)
               if app.store.conn.execute('SELECT 1 FROM guild_decision WHERE kind=? AND subject=?',
                                         ('lineup', subject_of(e['id']))).fetchone() is None]
        if not due:
            return
        members = dict(app.store.cast())
        personas = {g: p for g in members if (p := app.personas.get(g)) is not None}
        gear = await _gear(app, sorted(g for g in personas if g != app.human_guid))
        from leeroy_mind.nightly.store import active_away
        try:
            away = frozenset(active_away(app.store))
        except Exception:
            away = frozenset()
        for event in due:
            value = build_lineup(app.store.conn, gear, event, now=now, members=members, personas=personas,
                                 human_guid=app.human_guid, away=away)
            if value is None:
                if event['id'] not in self._unbuilt:
                    self._unbuilt.add(event['id'])
                    log.info('lineup_unbuilt event=%d reason=no_tank', event['id'])
                continue
            # Who objects first: the discussion seed shows the first few positions
            positions = sorted(lineup_positions(app.store.conn, value, personas, now=now),
                               key=lambda p: p.side != 'object')
            ident = ledger.propose(app.store, kind='lineup', subject=subject_of(event['id']), value=value,
                                   positions=positions, now=now)
            log.info('lineup_proposed id=%d event=%d members=%d bench=%d lead=%d', ident, event['id'],
                     len(value['members']), len(value['bench']), value['lead'])

    def _settle(self, app, now):
        store = app.store
        server = getattr(getattr(app, 'server', None), 'config', None)
        if server is not None:
            from leeroy_mind.server.clock import in_window
            if in_window(app.clock.local(), server.quiet_window):
                return  # posted when the server is awake
        for row in store.conn.execute("SELECT * FROM guild_decision WHERE kind='lineup' AND status='proposed' "
                                      'AND proposed<=? ORDER BY id', (now - SETTLE_AFTER_S,)).fetchall():
            event = _event(store.conn, row['subject'])
            if event is None or now >= event['at']:
                continue  # its night ends it (_end)
            value = json.loads(row['value'])
            names = {g: n for g, n in store.conn.execute('SELECT guid,name FROM cast_member')}
            text = roster_text(value, names, title=event['title'],
                               at=datetime.fromtimestamp(event['at'], app.clock.local().tzinfo))
            from leeroy_mind.discord import store as dstore
            with store.transaction():
                message = dstore.post(store, channel=CHANNEL, author=value['lead'], origin='mind', text=text,
                                      now=now)
                ledger.settle(store, row['id'], settled_by=value['lead'], message=message, now=now)
            log.info('lineup_posted id=%d message=%d', row['id'], message)

    def _end(self, store, now):
        """A night that is over ends its lineup: never posted, expired; posted but never formed
        from, talk only; formed from, its record kept and no longer live."""
        for row in store.conn.execute("SELECT id,subject,status FROM guild_decision WHERE kind='lineup' "
                                      "AND status IN ('proposed','settled','in_effect') ORDER BY id").fetchall():
            event = _event(store.conn, row['subject'])
            if event is not None and now < event['at'] + NIGHT_S:
                continue
            if row['status'] == 'proposed':
                ledger.expire(store, row['id'], now=now)
            elif row['status'] == 'settled':
                ledger.talk_only(store, row['id'], reason='not_formed', now=now)
            else:
                ledger.retire(store, row['id'], now=now)

    def desired(self, conn, now) -> tuple[tuple[int, ...], tuple[int, ...], int]:
        """The roster the picker should hold now: tonight's lineup members and bench, until the
        night's end; empty without one."""
        found = tonight(conn, now)
        if found is None:
            return (), (), 0
        row = ledger.get(conn, found.decision)
        event = _event(conn, row['subject'])
        until = int((event['at'] + NIGHT_S) * 1000) if event is not None else 0
        return found.members, tuple(sorted(found.bench - set(found.members))), until

    async def _roster(self, app, now):
        guild = getattr(app, 'guild', None)
        if guild is None or not self._connected or not guild.ports.enabled:
            return
        members, bench, until = self.desired(app.store.conn, now)
        want = (members, bench, until)
        if want == self._sent:
            return
        from leeroy_mind.guild.protocol import GuildAddRoster
        command = GuildAddRoster(uuid4().hex, members, bench, until)
        self._sent = want  # sent once per change; a hello sends it again
        try:
            ack = await guild.ports.send(command)
        except Exception as error:
            self._sent = None
            log.warning('guild_add_roster_failed error=%s', type(error).__name__)
            return
        if ack is None or ack.ok is not True:
            error = ''.join(c for c in str(getattr(ack, 'error', '') or 'no_ack')
                            if c.isascii() and (c.isalnum() or c in '_:.-'))[:60]
            log.warning('guild_add_roster_refused error=%s', error or 'none')
            return
        log.info('guild_add_roster_sent members=%d bench=%d', len(members), len(bench))


def _event(conn, subject):
    event = _event_of(subject)
    if event is None:
        return None
    return conn.execute("SELECT id,title,at FROM realm_calendar WHERE id=? AND state='posted' AND kind='raid'",
                        (event,)).fetchone()


async def _gear(app, guids) -> dict[int, int]:
    """Each member's average item level from the armory, off the event loop; {} when unread."""
    guild = getattr(app, 'guild', None)
    armory = getattr(guild, 'armory', None)
    if armory is None or not guids or not getattr(armory, 'available', True):
        return {}
    from leeroy_mind.guild.armory import average_item_level
    try:
        equipped = await asyncio.to_thread(armory.equipped, guids)
    except Exception as error:
        log.warning('lineup_gear_unread error=%s', type(error).__name__)
        return {}
    return {g: round(average_item_level(slots)) for g, slots in equipped.items() if isinstance(slots, dict)}


def lineup_rules() -> LineupRules:
    return LineupRules()
