"""The arc's state. His play days are the arc's clock: a stage waits on the nights he played,
never on the calendar, so a week away pauses every arc."""
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

from leeroy_mind.clock import night_of
from leeroy_mind.storage.store import Store

_TICK = timedelta(microseconds=1)


def play_days(store: Store, since: float, until: float, tz: tzinfo) -> int:
    """How many nights (clock.night_of in `tz`, 06:00 to 06:00) he played between `since` and
    `until`: the distinct nights his sessions overlap inside that span. An open session runs to
    `until`; a session ends where it ended, so one that stops at 06:00 never counts the next
    night. Two sessions in one night count it once."""
    return len(_play_nights(store, since, until, tz))


def play_days_after(store: Store, changed: float, until: float, tz: tzinfo) -> int:
    """The play days of the span that opens at a stage change: play_days(changed, until) minus
    the night the change fell in, when he had already played in it before the change, so a play
    night split by a change counts once (in the span before it, never in both)."""
    start = datetime.combine(date.fromisoformat(night_of(datetime.fromtimestamp(changed, tz))),
                             time(6), tz).timestamp()
    return len(_play_nights(store, changed, until, tz) - _play_nights(store, start, changed, tz))


def _play_nights(store: Store, since: float, until: float, tz: tzinfo) -> set[date]:
    if until <= since:
        return set()
    with store.transaction():
        rows = store.conn.execute(
            "SELECT started, ended FROM session WHERE started < ? AND (ended IS NULL OR ended > ?)",
            (until, since)).fetchall()
    nights: set[date] = set()
    for started, ended in rows:
        lo, hi = max(since, started), min(until, until if ended is None else ended)
        if hi <= lo:
            continue
        day = date.fromisoformat(night_of(datetime.fromtimestamp(lo, tz)))
        last = date.fromisoformat(night_of(datetime.fromtimestamp(hi, tz) - _TICK))
        while day <= last:
            nights.add(day)
            day += timedelta(days=1)
    return nights


EVENT_CAP = 12


@dataclass(frozen=True)
class Arc:
    """One romance_arc row: the plan she floated and how far it got, when she reached planning,
    her weekends and last meetup, the after-weekend milestone, her partner at home, how it
    ended and the newest EVENT_CAP (kind, ts) events. Times are mind-clock unix seconds."""
    guid: int
    human: int
    plan: dict = field(default_factory=dict)
    plan_state: str = "none"
    plan_at: float | None = None
    planning_since: float | None = None
    weekends: int = 0
    last_meetup: float | None = None
    milestone: int = 0
    milestone_at: float | None = None
    partner: str = "none"
    ended: str | None = None
    events: tuple[tuple[str, float], ...] = ()
    updated: float = 0.0


def get_arc(store: Store, guid: int, human: int) -> Arc:
    with store.transaction():
        r = store.conn.execute("SELECT * FROM romance_arc WHERE guid = ? AND human = ?",
                               (guid, human)).fetchone()
    if r is None:
        return Arc(guid, human)
    return Arc(guid, human, json.loads(r["plan"]), r["plan_state"], r["plan_at"],
               r["planning_since"], r["weekends"], r["last_meetup"], r["milestone"],
               r["milestone_at"], r["partner"], r["ended"],
               tuple((k, float(t)) for k, t in json.loads(r["events"])), r["updated"])


def put_arc(store: Store, arc: Arc) -> None:
    with store.transaction():
        store.conn.execute(
            "INSERT OR REPLACE INTO romance_arc(guid, human, plan, plan_state, plan_at, "
            "planning_since, weekends, last_meetup, milestone, milestone_at, partner, ended, "
            "events, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (arc.guid, arc.human, json.dumps(arc.plan, sort_keys=True), arc.plan_state,
             arc.plan_at, arc.planning_since, arc.weekends, arc.last_meetup, arc.milestone,
             arc.milestone_at, arc.partner, arc.ended,
             json.dumps([[k, t] for k, t in arc.events]), arc.updated))


def add_event(arc: Arc, kind: str, ts: float) -> Arc:
    return replace(arc, events=(*arc.events, (kind, ts))[-EVENT_CAP:])


def end_romance(store: Store, guid: int, human: int, *,
                by: Literal["he_ended", "found_out", "owner"], now: float) -> bool:
    """Her romance with him becomes history: stage ex (a rel_delta "<stage>->ex"), the arc row
    ended `by` with its plan cleared, and her open away row closed (returned at `now`).
    False, and nothing written, when it is already ex."""
    with store.transaction():
        rel = store.get_relationship(guid, human)
        if rel.romance_stage == "ex":
            return False
        store.put_relationship(replace(rel, romance_stage="ex"))
        store.add_rel_delta(guid, human, "romance_stage", -1, f"{rel.romance_stage}->ex", [],
                            now, applied=True)
        put_arc(store, replace(get_arc(store, guid, human), plan={}, plan_state="none",
                               plan_at=None, ended=by, updated=now))
        if store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='away'").fetchone():
            store.conn.execute("UPDATE away SET returned_at = ? WHERE guid = ? "
                               "AND returned_at IS NULL", (now, guid))
    return True
