"""Conversation subjects from observed guild data. Gathering is SELECT-only."""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
import sqlite3

from leeroy_mind.claims.visibility import audience_class
from leeroy_mind.discord import layout
from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import boss_name

SEED_KINDS = ('raid_before', 'raid_after', 'loot', 'life', 'plan', 'calendar', 'birthday',
              'reset', 'weekend', 'rival', 'his_day', 'officer', 'purpose', 'photo', 'joke', 'decision')
# A decision's stage is argued once (its keys name the stage): never fresh again once used
FRESH_S = {'purpose': 86400.0, 'plan': 7 * 86400.0, 'officer': 7 * 86400.0, 'decision': math.inf}
FRESH_DEFAULT_S = 3 * 86400.0
USED_KEY = 'server:seed:{key}'
log = logging.getLogger(__name__)
_unavailable = set()


@dataclass(frozen=True, slots=True)
class Seed:
    key: str
    kind: str
    channels: tuple[str, ...]
    fact: str
    about: tuple[int, ...] = ()
    officers_only: bool = False
    weight: float = 1.0
    witnesses: tuple[int, ...] = ()

    @property
    def openers(self) -> tuple[int, ...] | None:
        """Who already knows a subject before any of it is posted in the channel."""
        if self.kind in ('life', 'photo', 'group_start'):
            return self.about
        if self.kind == 'his_day':
            return self.witnesses
        return None


@dataclass
class _Context:
    now: float
    local: datetime
    cast: Mapping[int, Persona]
    officers: frozenset[int]
    human: int
    names: dict
    trial_rank: int

    def name(self, guid):
        return self.names.get(guid, f'#{guid}')

    @property
    def week(self):
        year, week, _ = self.local.isocalendar()
        return f'{year}-W{week:02}'


def _read(kind, source):
    try:
        return list(source())
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith(('no such table', 'no such column')):
            raise
        if kind not in _unavailable:
            _unavailable.add(kind)
            log.warning('server_seed_unavailable kind=%s', kind)
        return []


def _raid_before(conn, c):
    row = conn.execute("SELECT id,title,at FROM realm_calendar WHERE state='posted' AND kind='raid' "
                       'AND at>=? AND at<? ORDER BY at,id LIMIT 1', (c.now, c.now+43200)).fetchone()
    if row:
        at = datetime.fromtimestamp(row[2], c.local.tzinfo)
        yield Seed(f'raid_before:{row[0]}', 'raid_before', ('raid-planning', 'general'),
                   f'Raid night: {row[1]} at {at:%H:%M}')


def _raid_after(conn, c):
    for rid, key, size, kills, wipes, lead in conn.execute(
            'SELECT raid_id,raid_key,size,kills,wipes,lead_guid FROM guild_raid '
            'WHERE ended BETWEEN ? AND ? ORDER BY ended DESC LIMIT 16', (c.now-64800, c.now)):
        raid = CATALOG.get(key)
        yield Seed(f'raid_after:{rid}', 'raid_after', ('logs', 'loot-and-dkp', 'strategy', 'general'),
                   f'Last raid: {raid.name if raid else key} {size}, {kills} kills, {wipes} wipes, led by {c.name(lead)}',
                   (lead,) if lead in c.cast else ())


def _item_name(conn, item):
    # The caller may attach its configured library read-only before gathering. Never open
    # a second database here or guess the live installation's data directory.
    try:
        row = conn.execute('SELECT name FROM library.item WHERE id=?', (item,)).fetchone()
        if row:
            return row[0]
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith(('no such table', 'no such column')):
            raise
    return f'Item {item}'


def _loot(conn, c):
    for lid, guid, delta, item, class_id in conn.execute(
            "SELECT d.id,d.guid,d.delta,d.item_id,m.class FROM dkp_ledger d "
            'LEFT JOIN cast_member m ON m.guid=d.guid '
            "WHERE d.reason='item' AND d.item_id IS NOT NULL AND d.ts BETWEEN ? AND ? "
            'ORDER BY d.ts DESC,d.id DESC LIMIT 64', (c.now-FRESH_DEFAULT_S, c.now)):
        if guid not in c.cast and guid != c.human:
            continue
        channels = ('loot-and-dkp',)
        if class_id in layout.CLASS_CHANNEL:
            channels += (layout.CLASS_CHANNEL[class_id],)
        yield Seed(f'loot:{lid}', 'loot', channels,
                   f'{c.name(guid)} won [{_item_name(conn, item)}] for {abs(delta):g} DKP', (guid,))


def _life(conn, c):
    for bid, guid, text in conn.execute(
            'SELECT b.id,t.guid,b.text FROM life_beat b JOIN life_thread t ON t.id=b.thread_id '
            'WHERE b.night=(SELECT max(night) FROM life_beat WHERE ts<=?) '
            'AND b.ts BETWEEN ? AND ? ORDER BY b.id DESC LIMIT 64', (c.now, c.now-FRESH_DEFAULT_S, c.now)):
        if guid in c.cast:
            yield Seed(f'life:{bid}', 'life', ('irl', 'general', 'food'), text, (guid,))


def _plan(conn, c):
    for cid, guid, text, channel, readers, frm in conn.execute(
            'SELECT c.id,c.guid,c.text,l.channel,l.audience,l.from_guid FROM member_claim c '
            'JOIN chat_line l ON l.id=c.source_line '
            "WHERE c.kind='plan' AND c.superseded_by IS NULL AND (c.expires IS NULL OR c.expires>?) "
            'AND c.last_seen<=? ORDER BY c.last_seen DESC,c.id DESC LIMIT 128', (c.now, c.now)):
        if guid not in c.cast or frm != guid:
            continue
        try:
            audience = json.loads(readers) if readers is not None else None
            if audience is not None and (not isinstance(audience, list) or any(type(g) is not int for g in audience)):
                continue
        except (TypeError, ValueError):
            continue
        if audience_class(channel, readers=audience) != 'public':
            continue
        # The source must have reached the whole cast, not merely carry a public label.
        if audience is not None and not set(c.cast).issubset(audience):
            continue
        if channel != 'guild' and audience is None:
            continue
        yield Seed(f'plan:{cid}:{c.week}', 'plan', ('general', 'lfg-and-alts', 'professions-and-gold'), text, (guid,))


def _calendar(conn, c, *, birthday=False):
    for eid, kind, title, at in conn.execute(
            "SELECT id,kind,title,at FROM realm_calendar WHERE state='posted' "
            'AND at BETWEEN ? AND ? ORDER BY at,id LIMIT 64', (c.now, c.now+7*86400)):
        if (kind == 'birthday') != birthday or kind == 'raid':
            continue
        seed_kind = 'birthday' if birthday else 'calendar'
        day = datetime.fromtimestamp(at, c.local.tzinfo)
        private = kind == 'meeting'
        yield Seed(f'{seed_kind}:{eid}', seed_kind, ('officer-chat',) if private else ('general',),
                   f'{title} on {day:%A}', officers_only=private)


def _reset(conn, c):
    if c.local.weekday() == 1 and c.local.hour >= 9:
        yield Seed(f'reset:{c.local.date()}', 'reset', ('raid-planning', 'general'), 'Weekly reset today')


def _weekend(conn, c):
    if c.local.weekday() >= 5:
        yield Seed(f'weekend:{c.local.date()}', 'weekend', ('irl', 'food', 'general'), "It's the weekend")


def _rival(conn, c):
    # Rival prose is generated; report only its observed public event, not its invented
    # interpretation. Private lines never supply even a public subject.
    for lid, guild, channel in conn.execute(
            'SELECT l.id,g.name,l.channel FROM realm_line l JOIN realm_guild g ON g.id=l.guild '
            "WHERE l.kind='rival' AND l.state='sent' AND l.ts BETWEEN ? AND ? "
            'ORDER BY l.ts DESC,l.id DESC LIMIT 50', (c.now-7*86400, c.now)):
        if channel in {'trade', 'general', 'lfg', 'recruitment', 'say', 'yell'}:
            yield Seed(f'rival:{lid}', 'rival', ('general',),
                       f'{guild} was active in {channel} this week; nothing more is known')


def _his_day(conn, c):
    session = conn.execute('SELECT id FROM session WHERE started<=? AND coalesce(ended,?)>=? '
                           'ORDER BY started DESC,id DESC LIMIT 1', (c.now, c.now, c.now-64800)).fetchone()
    if not session:
        return
    for eid, kind, guid, raw in conn.execute(
            "SELECT id,type,guid,payload FROM game_event WHERE session_id=? AND type IN ('boss','loot_won') "
            'AND ts BETWEEN ? AND ? ORDER BY ts DESC,id DESC LIMIT 64', (session[0], c.now-64800, c.now)):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            members = data.get('members')
            if not isinstance(members, list) or any(type(g) is not int for g in members):
                continue
            witnesses = tuple(sorted(set(members) & (set(c.cast) - {c.human})))
            if not witnesses:
                continue
            if (kind == 'boss' and data.get('state') == 'killed'
                    and c.human in members and type(data.get('map')) is int
                    and type(data.get('boss_id')) is int and isinstance(data.get('boss_name', ''), str)):
                name = boss_name(data.get('map', 0), data.get('boss_id', 0), data.get('boss_name', ''))
                fact = f'{c.name(c.human)} killed {name} tonight'
            elif kind == 'loot_won' and guid == c.human and type(data.get('item_id')) is int:
                fact = f"{c.name(c.human)} won [{_item_name(conn, data['item_id'])}] tonight"
            else:
                continue
            yield Seed(f'his_day:{eid}', 'his_day', ('general',), fact, (c.human,), witnesses=witnesses)
        except (TypeError, ValueError):
            continue


def _officer(conn, c):
    raids = [r[0] for r in conn.execute('SELECT raid_id FROM guild_raid WHERE ended IS NOT NULL AND ended<=? '
                                      'ORDER BY ended DESC,raid_id DESC LIMIT 4', (c.now,))]
    if raids:
        attended = {}
        for rid, guid in conn.execute("SELECT raid_id,guid FROM guild_attendance WHERE status IN ('raided','benched') "
                                     'AND raid_id IN (SELECT value FROM json_each(?))', (json.dumps(raids),)):
            attended.setdefault(guid, set()).add(rid)
        for guid in c.cast:
            missed = len(raids)-len(attended.get(guid, ()))
            if missed:
                yield Seed(f'officer:attendance:{raids[0]}:{guid}:{c.week}', 'officer', ('officer-chat', 'applications'),
                           f'{c.name(guid)} missed {missed} of the last {len(raids)} raids', (guid,), True)
    for guid, in conn.execute('SELECT guid FROM cast_member WHERE rank=? ORDER BY guid', (c.trial_rank,)):
        if guid in c.cast:
            yield Seed(f'officer:trial:{guid}:{c.week}', 'officer', ('applications', 'officer-chat'),
                       f'{c.name(guid)} is a trial member', (guid,), True)


def _purpose(conn, c):
    for channel in layout.CHANNELS:
        if channel.category != 'info':
            yield Seed(f'purpose:{channel.id}:{c.local.date()}', 'purpose', (channel.id,), channel.topic,
                       officers_only=channel.officers_only)


def gather(conn, *, now: float, local: datetime, cast: Mapping[int, Persona],
           officers: frozenset[int], human: int, human_name: str, trial_rank: int,
           culture_enabled: bool = False, culture_store=None, ident=None,
           decisions_enabled: bool = False) -> list[Seed]:
    names = {g: p.character_name for g, p in cast.items()}
    for guid, name in _read('names', lambda: conn.execute('SELECT guid,name FROM cast_member')):
        if guid in cast or guid == human:
            names[guid] = name
    # human_name is deliberately not a fallback: the persisted character row is the
    # authority; an operator's real name must not become a seed.
    c = _Context(now, local, cast, officers, human, names, trial_rank)
    sources = {'raid_before': _raid_before, 'raid_after': _raid_after, 'loot': _loot, 'life': _life,
               'plan': _plan, 'calendar': _calendar, 'birthday': lambda db, ctx: _calendar(db, ctx, birthday=True),
               'reset': _reset, 'weekend': _weekend, 'rival': _rival, 'his_day': _his_day,
               'officer': _officer, 'purpose': _purpose}
    result = [seed for kind, source in sources.items() for seed in _read(kind, lambda: source(conn, c))]
    if culture_enabled:
        from leeroy_mind.culture.seeds import culture_seeds
        if culture_store is None or culture_store.conn is not conn:
            raise ValueError("Culture gathering requires the connection's owning Store")
        result += _read("joke", lambda: culture_seeds(culture_store, now=now, local=local,
                                                   personas=cast, human=human, ident=ident))
    if decisions_enabled:
        from leeroy_mind.decisions.seeds import decision_seeds
        result += _read('decision', lambda: decision_seeds(conn, now=now, personas=cast, officers=officers))
    return result


def fresh(conn, seeds: Iterable[Seed], *, now: float) -> list[Seed]:
    used = dict(_read('used', lambda: conn.execute("SELECT key,value FROM meta WHERE key GLOB 'server:seed:*'")))
    out = []
    for seed in seeds:
        try:
            at = float(used.get(USED_KEY.format(key=seed.key), '-inf'))
        except (TypeError, ValueError):
            at = -math.inf
        if not math.isfinite(at) or now-at >= FRESH_S.get(seed.kind, FRESH_DEFAULT_S):
            out.append(seed)
    return out


def mark_used(store, seed: Seed, *, now: float) -> None:
    store.set_meta(USED_KEY.format(key=seed.key), str(now))
