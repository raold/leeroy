"""Deterministic DKP math and weekly decay. Scheduling uses naive local time."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import DkpConfig
from .store import _insert_ledger, balances


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    ts: float
    guid: int
    delta: float
    reason: str
    raid_id: int | None = None
    item_id: int | None = None
    note: str = ""


def kill_entries(guids, raid_id, ts, cfg: DkpConfig) -> list[LedgerEntry]:
    return [LedgerEntry(ts, guid, cfg.kill, "kill", raid_id) for guid in dict.fromkeys(guids)]


def attendance_entries(raided, benched, raid_id, ts, cfg: DkpConfig) -> list[LedgerEntry]:
    raided, benched = tuple(dict.fromkeys(raided)), tuple(dict.fromkeys(benched))
    if set(raided) & set(benched):
        raise ValueError("attendance cannot be both raided and benched")
    return ([LedgerEntry(ts, guid, cfg.attendance, "attendance", raid_id) for guid in raided]
            + [LedgerEntry(ts, guid, cfg.standby, "standby", raid_id) for guid in benched])


def _local(dt: datetime):
    if dt.tzinfo is not None:
        raise ValueError("DKP scheduling requires naive local datetimes")


def _boundary(now: datetime, weekday: int, hour: int) -> datetime:
    _local(now)
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("weekday must be an integer 0-6")
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("hour must be an integer 0-23")
    candidate = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return candidate if candidate <= now else candidate - timedelta(days=7)


def decay_boundaries(last: datetime | None, now: datetime,
                     weekday: int, hour: int) -> list[datetime]:
    boundary = _boundary(now, weekday, hour)
    if last is None:
        return []
    _local(last)
    found = []
    while boundary > last:
        found.append(boundary)
        boundary -= timedelta(days=7)
    return list(reversed(found))


def decay_entries(balances, times, rate, ts) -> list[LedgerEntry]:
    if type(times) is not int or times < 0:
        raise ValueError("decay times must be a nonnegative integer")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate < 1:
        raise ValueError("decay rate must be in [0, 1)")
    entries = []
    for guid, balance in sorted(balances.items()):
        for _ in range(times):
            if not balance:
                break
            delta = -round(balance * rate, 1)
            if delta:
                entries.append(LedgerEntry(ts, guid, delta, "decay"))
            balance += delta
    return entries


def apply_due_decay(conn, now: datetime, cfg: DkpConfig) -> int:
    """Apply all missed weekly boundaries once, committing entries and receipt together.

    The initial baseline is the latest boundary, not the time of this call.
    Moving a clock backwards never moves that receipt backwards.
    """
    latest = _boundary(now, cfg.decay_weekday, cfg.decay_hour)
    with conn:
        row = conn.execute("SELECT value FROM meta WHERE key='dkp_last_decay'").fetchone()
        last = datetime.fromisoformat(row[0]) if row is not None else None
        if last is None:
            times = []
            receipt = latest
        else:
            times = decay_boundaries(last, now, cfg.decay_weekday, cfg.decay_hour)
            if not times:
                return 0
            receipt = times[-1]
            _insert_ledger(conn, decay_entries(balances(conn), len(times), cfg.decay,
                                               now.timestamp()))
        conn.execute("INSERT INTO meta(key,value) VALUES ('dkp_last_decay',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (receipt.isoformat(),))
        return len(times)


def bid(balance: float, price: int, gain: int | None, wanted: bool) -> float:
    pct = min(200, 100 + 5 * (gain if gain is not None else 0)) + (50 if wanted else 0)
    return float(min(max(balance, 0), math.ceil(price * pct / 100)))


@dataclass(frozen=True, slots=True)
class Bidder:
    guid: int
    balance: float
    gain: int | None
    wanted: bool


@dataclass(frozen=True, slots=True)
class Award:
    guid: int
    paid: float
    bid: float
    priority: int
    runner_up: int | None
    runner_up_bid: float | None
    close: bool


def award(price, bidders, *, human_claimed: bool, human_guid: int) -> Award | None:
    ranked = []
    seen = set()
    for person in bidders:
        if person.guid in seen:
            raise ValueError("duplicate bidder guid")
        seen.add(person.guid)
        if person.guid == human_guid or person.gain is None and not person.wanted:
            continue
        priority = (0 if person.wanted else 1) if person.gain is not None else 2
        amount = bid(person.balance, price, person.gain, person.wanted)
        ranked.append((priority, -amount, -person.balance, person.guid, amount))
    ranked.sort()
    if human_claimed:
        runner = ranked[0] if ranked else None
        return Award(human_guid, float(price), float(price), -1,
                     runner[3] if runner else None, runner[4] if runner else None, False)
    if not ranked:
        return None
    best, runner = ranked[0], ranked[1] if len(ranked) > 1 else None
    priority, _, _, guid, amount = best
    same_priority = runner is not None and runner[0] == priority
    paid = min(amount, max(min(price, amount), runner[4] + 1 if same_priority else 0))
    close = bool(same_priority and runner[4] >= 0.9 * amount and amount > 0)
    return Award(guid, float(paid), amount, priority, runner[3] if runner else None,
                 runner[4] if runner else None, close)
