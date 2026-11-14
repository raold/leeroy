"""Who argues what, by code. A position is a pure function of the guild's rows (DKP, attendance,
the bench, wipes) and the member's persona (agreeableness, the argumentative and drama dials,
guild role, how she thinks), so the same inputs always give the same side, and what a member
argued, voted and later says cannot drift. Never parsed from model text.

Officers vote on a poll once, each at a time drawn 10 minutes to 6 hours after it opens (the same
draw after a restart), while their Discord presence is online or idle; the vote is the side of the
position fixed when the poll opened, with its `why` stored."""
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import sqlite3

from leeroy_mind.tuning.effective import dials_of
from .polls import open_polls, options_of, positions_of, vote, voters_of

log = logging.getLogger(__name__)
DAY_S = 86400.0
ATTENDANCE_DAYS = 30
VOTE_AFTER_S = (600.0, 6 * 3600.0)
LOW_ATTENDANCE = .5
REGULAR = .75
HIGH = 7  # a dial or trait at or above this pulls
LOW = 3   # agreeableness at or below this argues


@dataclass(frozen=True, slots=True)
class Position:
    guid: int
    side: str
    why: str  # a code: "high_dkp", "benched_twice", "needs_flask", ...

    def as_dict(self) -> dict:
        return {'guid': self.guid, 'side': self.side, 'why': self.why}


@dataclass(frozen=True, slots=True)
class Facts:
    """One member's standing, from rows: DKP balance and place among the members with a balance,
    finished raids attended in the last 30 days, and the bench in the last three raids."""
    balance: float = 0.0
    place: int = 0          # 1 is the most DKP; 0 without a ledger row
    ranked: int = 0         # members with a ledger row
    attended: int = 0
    raids: int = 0          # finished raids in the last 30 days
    benched: int = 0        # benched in the last `last` raids
    last: int = 0

    @property
    def share(self) -> float | None:
        return self.attended / self.raids if self.raids else None

    @property
    def top(self) -> bool:
        return self.place > 0 and self.balance > 0 and self.place <= max(1, math.ceil(self.ranked / 3))


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(('no such table', 'no such column')):
            return []
        raise


def member_facts(conn, guids: Iterable[int], *, now: float) -> dict[int, Facts]:
    guids = sorted({g for g in guids if type(g) is int})
    balances = {g: b for g, b in _rows(conn, 'SELECT guid,sum(delta) FROM dkp_ledger WHERE ts<=? GROUP BY guid',
                                       (now,))}
    ordered = sorted(balances.values(), reverse=True)
    raids = [r[0] for r in _rows(conn, 'SELECT raid_id FROM guild_raid WHERE ended IS NOT NULL AND ended<=? '
                                       'AND started>=? ORDER BY ended DESC,raid_id DESC',
                                 (now, now - ATTENDANCE_DAYS * DAY_S))]
    last = [r[0] for r in _rows(conn, 'SELECT raid_id FROM guild_raid WHERE ended IS NOT NULL AND ended<=? '
                                      'ORDER BY ended DESC,raid_id DESC LIMIT 3', (now,))]
    status = {}
    wanted = sorted(set(raids) | set(last))
    if wanted:
        for rid, guid, state in _rows(conn, 'SELECT raid_id,guid,status FROM guild_attendance '
                                            'WHERE raid_id IN (SELECT value FROM json_each(?))', (json.dumps(wanted),)):
            status[rid, guid] = state
    out = {}
    for g in guids:
        balance = float(balances.get(g, 0.0))
        place = 1 + sum(b > balance for b in ordered) if g in balances else 0
        out[g] = Facts(balance, place, len(ordered),
                       sum(status.get((r, g)) == 'raided' for r in raids), len(raids),
                       sum(status.get((r, g)) == 'benched' for r in last), len(last))
    return out


def facts_text(why: str, facts: Facts) -> str:
    """The data behind a position, in words built from the rows ("DKP 340, rank 1 of 5")."""
    if why == 'high_dkp':
        return f'DKP {facts.balance:g}, rank {facts.place} of {facts.ranked}'
    if why in ('low_attendance', 'regular_raider', 'not_raiding'):
        return f'{facts.attended} of {facts.raids} raids in {ATTENDANCE_DAYS} days'
    if why in ('benched_twice', 'benched', 'accepts_bench'):
        return f'benched {facts.benched} of the last {facts.last}'
    return ''


def _agreeable(p) -> int:
    return p.big_five.agreeableness.score


def _theorycrafter(p) -> bool:
    return p.game.thinks_as == 'theorycrafter' or dials_of(p).level('theorycraft') >= HIGH


def _stirs(p) -> bool:
    return _agreeable(p) <= LOW or dials_of(p).level('drama') >= HIGH


def loot_rule_positions(conn, officers, personas, *, now) -> list[Position]:
    """Each officer's loot mode: a high DKP balance keeps DKP; low attendance prefers a council;
    a theorycrafter wants gear priority; an argumentative or dramatic officer wants a change."""
    members = [g for g in sorted(officers) if g in personas]
    facts = member_facts(conn, members, now=now)
    out = []
    for g in members:
        p, f = personas[g], facts[g]
        if f.top:
            out.append(Position(g, 'dkp', 'high_dkp'))
        elif f.share is not None and f.share < LOW_ATTENDANCE:
            out.append(Position(g, 'loot_council', 'low_attendance'))
        elif _theorycrafter(p):
            out.append(Position(g, 'gear_priority', 'theorycrafter'))
        elif _stirs(p):
            out.append(Position(g, 'loot_council', 'wants_change'))
        else:
            out.append(Position(g, 'dkp', 'status_quo'))
    return out


def lineup_positions(conn, lineup: dict, personas, *, now) -> list[Position]:
    """Members in the lineup support it; a benched member argues for a spot when she was benched
    twice of the last three raids, or is argumentative; otherwise she accepts the bench."""
    members = [g for g in lineup.get('members', ()) if type(g) is int and g in personas]
    bench = [g for g in lineup.get('bench', ()) if type(g) is int and g in personas and g not in members]
    facts = member_facts(conn, bench, now=now)
    out = [Position(g, 'support', 'in_lineup') for g in members]
    for g in bench:
        if facts[g].benched >= 2:
            out.append(Position(g, 'object', 'benched_twice'))
        elif _stirs(personas[g]):
            out.append(Position(g, 'object', 'benched'))
        else:
            out.append(Position(g, 'support', 'accepts_bench'))
    return out


def bank_positions(conn, request: dict, officers, personas) -> list[Position]:
    """Each officer on a member's bank request ({"guid", "at", ...}): a regular raider is helped,
    one who has not raided is refused, a careful officer keeps stock."""
    requester, at = request.get('guid'), request.get('at')
    if type(requester) is not int or type(at) not in (int, float) or not math.isfinite(at):
        return []
    f = member_facts(conn, [requester], now=float(at))[requester]
    out = []
    for g in sorted(officers):
        if g not in personas or g == requester:
            continue
        if f.share is not None and f.share >= REGULAR:
            out.append(Position(g, 'approve', 'regular_raider'))
        elif not f.attended:
            out.append(Position(g, 'refuse', 'not_raiding'))
        elif personas[g].big_five.conscientiousness.score >= HIGH:
            out.append(Position(g, 'refuse', 'keeps_stock'))
        else:
            out.append(Position(g, 'approve', 'helps_raiders'))
    return out


def strategy_positions(conn, context: str, proposal: dict, cast, personas) -> list[Position]:
    """Each raider on a strategy change for a context, after its wipes since proposal["since_ms"]:
    theorycrafters back a change, argumentative members resist it, the rest follow the wipes."""
    since = proposal.get('since_ms', 0)
    since = since if type(since) is int and since >= 0 else 0
    wipes = (_rows(conn, "SELECT count(*) FROM tg_pull WHERE context=? AND outcome='wiped' AND start_ms>=?",
                   (context, since)) or [(0,)])[0][0]
    out = []
    for g in sorted(cast):
        p = personas.get(g)
        if p is None:
            continue
        if _theorycrafter(p) or p.game.thinks_as == 'lore':
            out.append(Position(g, 'support', 'theorycrafter'))
        elif _agreeable(p) <= LOW:
            out.append(Position(g, 'object', 'contrarian'))
        elif wipes >= 2:
            out.append(Position(g, 'support', 'wiped_twice'))
        else:
            out.append(Position(g, 'object', 'status_quo'))
    return out


def vote_at(poll: int, member: int, opened: float, closes: float) -> float:
    """When this member votes on this poll: a fixed draw 10 minutes to 6 hours after it opens, and
    before it closes."""
    lo, hi = opened + VOTE_AFTER_S[0], min(opened + VOTE_AFTER_S[1], closes - 1)
    if hi <= lo:
        return opened + (closes - opened) / 2
    digest = hashlib.sha256(f'decisions:vote:{poll}:{member}'.encode()).digest()
    return lo + (hi - lo) * int.from_bytes(digest[:8], 'big') / 2 ** 64


def cast_votes(store, *, now, present: Callable[[int], bool]) -> int:
    """Each member with a position on an open poll votes its side once its time has come and while
    present(guid) (online or idle). The number of votes cast."""
    cast = 0
    for row in open_polls(store.conn):
        if now >= row['closes']:
            continue
        voted = {r[0] for r in store.conn.execute('SELECT member FROM guild_poll_vote WHERE poll=?', (row['id'],))}
        voters, options = voters_of(row), options_of(row)
        for p in positions_of(row):
            g = p['guid']
            if (g in voted or g not in voters or p['side'] not in options
                    or now < vote_at(row['id'], g, row['opened'], row['closes']) or not present(g)):
                continue
            cast += vote(store, row['id'], g, p['side'], why=p['why'], now=now)
    return cast


def positions_by_guid(positions: Iterable[Mapping]) -> dict[int, Mapping]:
    return {p['guid']: p for p in positions if isinstance(p, Mapping) and type(p.get('guid')) is int}
