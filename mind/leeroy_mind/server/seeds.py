"""Conversation subjects from observed guild data. Gathering is SELECT-only."""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import logging
import math
import re
import sqlite3

from leeroy_mind.claims.visibility import audience_class
from leeroy_mind.discord import layout
from leeroy_mind.guild.raids import CATALOG, shown_kills
from leeroy_mind.memory.privacy import STRICTEST, effective_scopes
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import boss_name

SEED_KINDS = ('raid_before', 'raid_after', 'loot', 'life', 'plan', 'calendar', 'birthday',
              'reset', 'weekend', 'rival', 'his_day', 'officer', 'purpose', 'photo', 'joke',
              'wow_event', 'loot_close', 'loot_wish', 'intent', 'decision', 'his_loot', 'meme', 'meme_image', 'day')
# An intent (godmode.intents) is tried again half an hour after an opener that was dropped.
# A decision's stage is argued once (its keys name the stage): never fresh again once used
FRESH_S = {'purpose': 86400.0, 'plan': 7 * 86400.0, 'officer': 7 * 86400.0, 'loot_close': 7 * 86400.0,
           'intent': 1800.0, 'decision': math.inf, 'his_loot': 7 * 86400.0}
FRESH_DEFAULT_S = 3 * 86400.0
USED_KEY = 'server:seed:{key}'
# The kind of subject each seed kind is (rhythm.choose_seed_and_channel draws the subject first,
# then a seed within it, so a kind with many seeds never crowds out the rest; 2026-11-15: officer
# and life seeds were 88% of the openers). A kind missing here is its own subject.
SUBJECTS = {'raid': ('raid_before', 'raid_after', 'reset', 'his_day'),
            'loot': ('loot', 'loot_close', 'loot_wish', 'his_loot'),
            'life': ('life', 'weekend', 'photo', 'day'),
            'meme': ('joke', 'meme', 'meme_image'),
            'rival': ('rival',),
            'officer': ('officer', 'decision'),
            'calendar': ('calendar', 'birthday', 'wow_event'),
            'chat': ('plan', 'purpose')}
_SUBJECT_OF = {kind: subject for subject, kinds in SUBJECTS.items() for kind in kinds}
# A public decision (a lineup, a loot rule, a boss strategy) is the guild's raid or loot
# business, by the channel it is argued in; an officers' one stays officer business.
DECISION_SUBJECTS = {'raid-planning': 'raid', 'strategy': 'raid', 'loot-and-dkp': 'loot'}
# The weekly attendance thread (dropping_off): regulars who missed this many of the last
# ATTENDANCE_WINDOW raids they could have joined after attending ATTENDANCE_REGULAR of the
# ATTENDANCE_BEFORE before them; its fact names at most ATTENDANCE_NAMES of them, and there is
# none when more than ATTENDANCE_MAX_SHARE of the cast would be named (no exodus is invented).
ATTENDANCE_WINDOW, ATTENDANCE_MISSED = 4, 3
ATTENDANCE_BEFORE, ATTENDANCE_REGULAR = 8, 2
ATTENDANCE_NAMES = 6
ATTENDANCE_MAX_SHARE = 0.10
# [talk] recall_diversity (memory/diversity.py; PM log 2026-11-16: "bench" in 61 of 2,869 member
# lines in 7 days, 43 of them in officers' attendance threads): one guild theme can't dominate.
# A topic seed (TOPIC_KINDS: subjects that come round again; raid, loot, calendar, decision,
# intent and culture seeds are each a new event or have their own cooldowns) whose fact is
# SUBJECT_JACCARD or more like one that started a conversation within SUBJECT_S is passed over
# while any other seed is available (`varied`); its receipt (SUBJECT_KEY) holds the fact's words
# as hashes, never text. The officers' weekly attendance thread is the only attendance seed and
# starts at most once in SUBJECT_S guild-wide, whatever the week.
SUBJECT_KEY = 'server:subject:{key}'
SUBJECT_S = 7 * 86400.0
SUBJECT_JACCARD = 0.5
TOPIC_KINDS = frozenset({'life', 'plan', 'purpose', 'officer', 'rival'})
ATTENDANCE_SEED = 'officer:attendance:'
ATTENDANCE_RE = re.compile(r"\b(?:bench(?:ed|es|ing)?|attendance|s[ai]t(?:ting)? out|standby)\b",
                           re.IGNORECASE)
# Seeds the game's openers to the guild may bring up (talk.engine): public, never officers'.
GAME_KINDS = ('raid_before', 'raid_after', 'reset', 'loot', 'life', 'calendar', 'birthday',
              'weekend', 'rival')
log = logging.getLogger(__name__)
_unavailable = set()


def subject_of(kind: str) -> str:
    """The subject a seed kind belongs to (SUBJECTS); an unlisted kind is its own."""
    return _SUBJECT_OF.get(kind, kind)


def seed_subject(seed) -> str:
    """A seed's subject: its kind's (subject_of), except a public decision, which is raid or loot
    business by its channel (DECISION_SUBJECTS)."""
    if seed.kind == 'decision' and not seed.officers_only and seed.channels:
        return DECISION_SUBJECTS.get(seed.channels[0], subject_of(seed.kind))
    return subject_of(seed.kind)


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
        if self.kind in ('life', 'photo', 'group_start', 'loot_wish', 'intent', 'day'):
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
    if row:  # "Raid night tonight at 9pm server time" (when_words: never a bare "03:00")
        from leeroy_mind.realm.calendar_time import when_words
        yield Seed(f'raid_before:{row[0]}', 'raid_before', ('raid-planning', 'general'),
                   f"{' '.join(str(row[1]).split())} {when_words(row[2], c.local)}")


def _raid_after(conn, c):
    for rid, key, size, kills, wipes, lead in conn.execute(
            'SELECT raid_id,raid_key,size,kills,wipes,lead_guid FROM guild_raid '
            'WHERE ended BETWEEN ? AND ? ORDER BY ended DESC LIMIT 16', (c.now-64800, c.now)):
        raid = CATALOG.get(key)
        yield Seed(f'raid_after:{rid}', 'raid_after', ('logs', 'loot-and-dkp', 'strategy', 'general'),
                   f'Last raid: {raid.name if raid else key} {size}, {shown_kills(key, kills)} kills, '
                   f'{wipes} wipes, led by {c.name(lead)}',
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
    for lid, guid, delta, item, class_id, note in conn.execute(
            "SELECT d.id,d.guid,d.delta,d.item_id,m.class,d.note FROM dkp_ledger d "
            'LEFT JOIN cast_member m ON m.guid=d.guid '
            "WHERE d.reason='item' AND d.item_id IS NOT NULL AND d.ts BETWEEN ? AND ? "
            'ORDER BY d.ts DESC,d.id DESC LIMIT 64', (c.now-FRESH_DEFAULT_S, c.now)):
        if guid not in c.cast and guid != c.human:
            continue
        channels = ('loot-and-dkp',)
        if class_id in layout.CLASS_CHANNEL:
            channels += (layout.CLASS_CHANNEL[class_id],)
        # A loot council's award (decisions.loot_rules) is charged nothing and says so
        price = 'by loot council' if note.startswith('council:') else f'for {abs(delta):g} DKP'
        yield Seed(f'loot:{lid}', 'loot', channels,
                   f'{c.name(guid)} won [{_item_name(conn, item)}] {price}', (guid,))


def _life(conn, c):
    # A beat whose memory is not open to everyone (private with him, about her secret: nightly
    # life.private_beat) or no longer active (retired: nightly life-contact) seeds nothing public.
    # Its scope is the effective one (memory.privacy.resolve): as strict as its evidence, at any
    # depth, checked or not; never the stored column alone.
    beats = conn.execute(
        'SELECT b.id,t.guid,b.text FROM life_beat b JOIN life_thread t ON t.id=b.thread_id '
        'WHERE b.night=(SELECT max(night) FROM life_beat WHERE ts<=?) '
        'AND b.ts BETWEEN ? AND ? ORDER BY b.id DESC', (c.now, c.now-FRESH_DEFAULT_S, c.now)).fetchall()
    rows = conn.execute(
        "SELECT m.id,m.owner,m.status,e.value FROM memory m, json_each(m.evidence) e "
        "WHERE m.source='offscreen' AND m.owner IN (SELECT value FROM json_each(?)) "
        "AND e.value IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted({b[1] for b in beats})),
         json.dumps([f'beat:{b[0]}' for b in beats]))).fetchall() if beats else []
    scopes = effective_scopes(conn, [r[0] for r in rows])
    closed = {(r[1], r[3]) for r in rows
              if r[2] != 'active' or scopes.get(r[0], STRICTEST)[0] != 'open'}
    left = 64
    for bid, guid, text in beats:
        if (guid, f'beat:{bid}') in closed:
            continue
        if not left:
            break
        left -= 1
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


def dropping_off(conn, now: float, cast) -> tuple[list[int], int]:
    """The members of `cast` who missed at least ATTENDANCE_MISSED of the last
    ATTENDANCE_WINDOW finished raids they could have joined, after attending at least
    ATTENDANCE_REGULAR of the ATTENDANCE_BEFORE before those (regulars dropping off, not members
    who never raid), in guid order, and how many raids the window holds. A raid with no
    attendance rows (an offline start that failed) counts for nobody; one he called (not
    offline) counts only for its roster and bench, since it records only his group; an offline
    raid counts for everyone. A member on leave (an open away row) is never named. Nobody until
    the window is full, so at least ATTENDANCE_WINDOW + ATTENDANCE_REGULAR raids with attendance
    must have finished before anyone can be named."""
    raids = [(r[0], bool(r[1])) for r in conn.execute(
        'SELECT r.raid_id,r.offline FROM guild_raid r WHERE r.ended IS NOT NULL AND r.ended<=? '
        'AND EXISTS (SELECT 1 FROM guild_attendance a WHERE a.raid_id=r.raid_id) '
        'ORDER BY r.ended DESC,r.raid_id DESC LIMIT ?', (now, ATTENDANCE_WINDOW + ATTENDANCE_BEFORE))]
    if len(raids) < ATTENDANCE_WINDOW:
        return [], len(raids)
    rows = {}
    for rid, guid, status in conn.execute(
            "SELECT raid_id,guid,status FROM guild_attendance WHERE raid_id IN "
            '(SELECT value FROM json_each(?))', (json.dumps([rid for rid, _ in raids]),)):
        rows.setdefault(guid, {})[rid] = status
    away = {r[0] for r in conn.execute('SELECT guid FROM away WHERE returned_at IS NULL '
                                       'AND (returns_at IS NULL OR returns_at>?)', (now,))}
    recent, before = raids[:ATTENDANCE_WINDOW], raids[ATTENDANCE_WINDOW:]
    out = []
    for guid in sorted(cast):
        mine = rows.get(guid, {})
        attended = {rid for rid, status in mine.items() if status in ('raided', 'benched')}
        could = [rid for rid, offline in recent if offline or rid in mine]
        if (guid not in away and len([rid for rid in could if rid not in attended]) >= ATTENDANCE_MISSED
                and len({rid for rid, _ in before} & attended) >= ATTENDANCE_REGULAR):
            out.append(guid)
    return out, len(recent)


def _officer(conn, c):
    # One attendance thread a week for the officers, never one per member (2026-11-15: 49 of
    # 57 #officer-chat openers were "X missed N of the last 4 raids", one seed per member).
    late, window = dropping_off(conn, c.now, c.cast)
    if late and len(late) <= ATTENDANCE_MAX_SHARE * len(c.cast):
        names = [c.name(g) for g in late[:ATTENDANCE_NAMES]]
        more = len(late) - len(names)
        listed = ', '.join(names) + (f' and {more} more' if more else '')
        yield Seed(f'officer:attendance:{c.week}', 'officer', ('officer-chat', 'applications'),
                   f'Attendance this week: {listed} missed {ATTENDANCE_MISSED} or more of the '
                   f'last {window} raids after raiding regularly before', tuple(late), True)
    for guid, in conn.execute('SELECT guid FROM cast_member WHERE rank=? ORDER BY guid', (c.trial_rank,)):
        if guid in c.cast:
            yield Seed(f'officer:trial:{guid}:{c.week}', 'officer', ('applications', 'officer-chat'),
                       f'{c.name(guid)} is a trial member', (guid,), True)


def _purpose(conn, c):
    for channel in layout.CHANNELS:
        if channel.category != 'info':
            yield Seed(f'purpose:{channel.id}:{c.local.date()}', 'purpose', (channel.id,), channel.topic,
                       officers_only=channel.officers_only)


def _intent(conn, c):
    """His pending post, thread and bring_up intents (godmode.intents), each opened by its
    member in its own channel (bring_up without one: general or irl). The fact is his ask in
    her terms, screened when he made it."""
    from leeroy_mind.godmode.intents import BRING_UP_CHANNELS, SEED_WEIGHT, seed_rows
    for ident, member, kind, args in seed_rows(conn, now=c.now):
        if member not in c.cast:
            continue
        channel = args.get('channel')
        channels = (channel,) if channel else BRING_UP_CHANNELS
        definition = layout.channel(channels[0])
        yield Seed(f'intent:{ident}:{kind}', 'intent', channels,
                   args.get('about') or args.get('topic') or '', (member,),
                   officers_only=bool(definition and definition.officers_only), weight=SEED_WEIGHT)


_SOURCES = {'raid_before': _raid_before, 'raid_after': _raid_after, 'loot': _loot, 'life': _life,
            'plan': _plan, 'calendar': _calendar, 'birthday': lambda db, ctx: _calendar(db, ctx, birthday=True),
            'reset': _reset, 'weekend': _weekend, 'rival': _rival, 'his_day': _his_day,
            'officer': _officer, 'purpose': _purpose}


def _names(conn, cast, human) -> dict:
    names = {g: p.character_name for g, p in cast.items()}
    for guid, name in _read('names', lambda: conn.execute('SELECT guid,name FROM cast_member')):
        if guid in cast or guid == human:
            names[guid] = name
    return names


def public_seeds(conn, *, now: float, local: datetime, cast: Mapping[int, Persona],
                 human: int, kinds: Iterable[str] = GAME_KINDS) -> list[Seed]:
    """The SELECT-only seeds of `kinds` that are never officers' business (GAME_KINDS by
    default), for the game's openers to the guild (talk.engine): no officer, culture, decision
    or intent source runs."""
    c = _Context(now, local, cast, frozenset(), human, _names(conn, cast, human), 0)
    return [seed for kind in kinds if kind in _SOURCES and kind not in ('officer', 'purpose')
            for seed in _read(kind, lambda kind=kind: _SOURCES[kind](conn, c)) if not seed.officers_only]


def gather(conn, *, now: float, local: datetime, cast: Mapping[int, Persona],
           officers: frozenset[int], human: int, human_name: str, trial_rank: int,
           culture_enabled: bool = False, culture_store=None, ident=None,
           culture_calendar: bool = False, culture_loot: bool = False,
           intents_enabled: bool = False, decisions_enabled: bool = False,
           culture_memes: bool = False, day_plans: bool = False) -> list[Seed]:
    # human_name is deliberately not a fallback: the persisted character row is the
    # authority; an operator's real name must not become a seed.
    names = _names(conn, cast, human)
    c = _Context(now, local, cast, officers, human, names, trial_rank)
    result = [seed for kind, source in _SOURCES.items() for seed in _read(kind, lambda: source(conn, c))]
    if culture_enabled:
        from leeroy_mind.culture.seeds import culture_seeds
        if culture_store is None or culture_store.conn is not conn:
            raise ValueError("Culture gathering requires the connection's owning Store")
        result += _read("joke", lambda: culture_seeds(culture_store, now=now, local=local,
                                                   personas=cast, human=human, ident=ident))
        from leeroy_mind.culture import seeds as culture
        if culture_calendar:
            result += _read('wow_event', lambda: culture.wow_event_seeds(conn, now=now, local=local, ident=ident))
        if culture_loot:
            close = _read('loot_close', lambda: culture.loot_close_seeds(conn, now=now, cast=cast,
                                                                         names=names, human=human, ident=ident))
            # A close award is that drop's subject; it is never raised twice as plain loot too.
            raised = {'loot:' + seed.key.split(':', 1)[1] for seed in close}
            result = [seed for seed in result if seed.key not in raised] + close
            result += _read('loot_wish', lambda: culture.loot_wish_seeds(conn, now=now, cast=cast,
                                                                         names=names, human=human, ident=ident))
        if culture_memes:  # Part C: text memes, and the night's ready image memes
            for kind, source in (('meme', culture.meme_seeds), ('meme_image', culture.meme_image_seeds)):
                result += _read(kind, lambda source=source: source(culture_store, now=now, local=local,
                                                                   personas=cast, human=human, ident=ident))
    if day_plans:
        from leeroy_mind.godmode.dayrun import seeds
        result += _read('day', lambda: seeds(conn, now, names))
    if intents_enabled:
        result += _read('intent', lambda: _intent(conn, c))
    if decisions_enabled:
        from leeroy_mind.decisions.seeds import decision_seeds, his_loot_seeds
        result += _read('decision', lambda: decision_seeds(conn, now=now, personas=cast, officers=officers))
        his = _read('his_loot', lambda: his_loot_seeds(conn, now=now, names=names, human=human, cast=cast))
        # His claim is that drop's subject; it is never raised twice as plain loot too.
        raised = {'loot:' + seed.key.split(':', 1)[1] for seed in his}
        result = [seed for seed in result if seed.key not in raised] + his
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


def attendance(seed: Seed) -> bool:
    """A seed about attendance or the bench: the officers' weekly thread, or a topic seed whose
    fact names either (a member's life beat, a plan). Never a channel's purpose, a raid, loot,
    calendar or decision seed (a lineup names its bench)."""
    return seed.key.startswith(ATTENDANCE_SEED) or (
        seed.kind in TOPIC_KINDS and seed.kind != 'purpose' and bool(ATTENDANCE_RE.search(seed.fact)))


def subject_words(seed: Seed, names: frozenset[str] = frozenset()) -> frozenset[str]:
    """A seed's subject: its fact's content words (memory.diversity.words), as short hashes."""
    from leeroy_mind.memory.diversity import words
    return frozenset(sha256(w.encode('utf-8')).hexdigest()[:12] for w in words(seed.fact, names))


def note_subject(store, seed: Seed, *, now: float, names: frozenset[str] = frozenset()) -> None:
    """A topic seed's conversation began (its opener was posted): its subject receipt, pruned
    with the other seed receipts after a week (records.prune)."""
    if seed.kind in TOPIC_KINDS:
        store.set_meta(SUBJECT_KEY.format(key=seed.key), json.dumps(
            {'at': now, 'kind': seed.kind, 'words': sorted(subject_words(seed, names))},
            separators=(',', ':')))


def recent_subjects(conn, *, now: float) -> list[frozenset[str]]:
    """The subjects of the topic conversations begun within SUBJECT_S."""
    out = []
    for _, value in _read('subjects', lambda: conn.execute(
            "SELECT key,value FROM meta WHERE key GLOB 'server:subject:*'")):
        try:
            data = json.loads(value)
            at, found = float(data['at']), data['words']
        except (TypeError, ValueError, KeyError):
            continue
        if math.isfinite(at) and now - at < SUBJECT_S and isinstance(found, list):
            out.append(frozenset(w for w in found if isinstance(w, str)))
    return out


def last_attendance(conn) -> float | None:
    """When an officers' attendance thread last began (its seed receipts), None for never."""
    found = []
    for _, value in _read('used', lambda: conn.execute(
            "SELECT key,value FROM meta WHERE key GLOB ?", (USED_KEY.format(key=ATTENDANCE_SEED) + '*',))):
        try:
            found.append(float(value))
        except (TypeError, ValueError):
            continue
    return max((t for t in found if math.isfinite(t)), default=None)


def varied(seeds: Iterable[Seed], *, subjects: list[frozenset[str]], attendance_at: float | None,
           now: float, names: frozenset[str] = frozenset()) -> list[Seed]:
    """The seeds a conversation may start on under [talk] recall_diversity: no attendance seed
    but the officers' weekly one, and that one only when no attendance thread began within
    SUBJECT_S; then a topic seed whose subject is like a recent one's (`subjects`) only while
    nothing else is left."""
    from leeroy_mind.memory.diversity import jaccard
    kept = [seed for seed in seeds if not attendance(seed) or (
        seed.key.startswith(ATTENDANCE_SEED)
        and (attendance_at is None or now - attendance_at >= SUBJECT_S))]
    def spent(seed):
        if seed.kind not in TOPIC_KINDS:
            return False
        mine = subject_words(seed, names)
        return any(jaccard(mine, done) >= SUBJECT_JACCARD for done in subjects)
    new = [seed for seed in kept if not spent(seed)]
    return new if new else kept
