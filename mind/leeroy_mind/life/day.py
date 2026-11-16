"""Her day plan: every minute of her local day in one block (asleep, work, commute, home duty,
gym, out, free, gaming), built from her persona's job, rhythm, household and night_owl, with
one drawn event a day. Pure and deterministic; README.md has the tables."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from ..personas.schema import Persona
from .seed import unit

DAY_MIN = 1440
LATE_SHIFT_MIN = 120


class Block(StrEnum):
    ASLEEP = "asleep"
    WORK = "work"
    COMMUTE = "commute"
    HOME_DUTY = "home_duty"
    GYM = "gym"
    OUT = "out"
    FREE = "free"
    GAMING = "gaming"


class JobShape(StrEnum):
    DAY = "day"
    EARLY = "early"
    LATE = "late"
    NIGHTS = "nights"
    ROTATING = "rotating"
    STUDENT = "student"
    REMOTE = "remote"
    HOME = "home"


class DayEvent(StrEnum):
    NONE = "none"
    LATE_SHIFT = "late_shift"
    SICK_KID = "sick_kid"
    NIGHT_OUT = "night_out"
    BAD_DAY = "bad_day"


def _bounded(pattern: str) -> re.Pattern:
    return re.compile(rf"\b(?:{pattern})\b")


# first match wins, in this order; "raid nights" and "late nights" are no night shift
_JOB_PATTERNS = (
    (JobShape.NIGHTS, _bounded(r"night shifts?|works? nights|on nights|overnights?|graveyard")),
    (JobShape.ROTATING, _bounded(r"\d+\s*on\s*,?\s*\d+\s*off|\d+\s*(?:days|weeks)\s*on|rotating"
                                 r"|rota|12[- ]hour|on call")),
    (JobShape.EARLY, _bounded(r"early shifts?|early starts?|[4-6]\s*a\.?m\.?\s*starts?"
                              r"|starts? at [4-6]")),
    (JobShape.LATE, _bounded(r"late shifts?|evening shifts?|swing shifts?|afternoon shifts?")),
    (JobShape.STUDENT, _bounded(r"student|studying|uni|college|university|lectures?")),
    (JobShape.REMOTE, _bounded(r"wfh|works? from home|from home|freelanc\w*|self[- ]employed")),
    (JobShape.HOME, _bounded(r"stay[- ]at[- ]home|unemployed|between jobs|retired")),
)
_KIDS = _bounded(r"kids?|sons?|daughters?|bab(?:y|ies)|toddlers?|child(?:ren)?|twins|stepkids?")
_NO_KIDS = _bounded(r"no kids")
# a word start only, so "gyms" and "5km" count and "uplifting", "recycling" and "25k" do not
_GYM = re.compile(r"\b(?:gym|climbing|lifting|crossfit|yoga|swimming|cycling|marathon|5k)")


@dataclass(frozen=True, slots=True)
class _Hours:
    work: tuple[int, int]                    # from the shift day's midnight; may pass 1440
    commutes: tuple[tuple[int, int], ...]    # the second one starts at the work's end
    wake: int | None                         # on a workday; None: NIGHTS (its own rule)


def _hm(h: int, m: int = 0) -> int:
    return h * 60 + m


_HOURS = {
    JobShape.DAY: _Hours((_hm(9), _hm(17)), ((_hm(8, 15), _hm(9)), (_hm(17), _hm(17, 45))), _hm(7, 30)),
    JobShape.EARLY: _Hours((_hm(6), _hm(14, 30)), ((_hm(5, 30), _hm(6)), (_hm(14, 30), _hm(15))),
                           _hm(4, 45)),
    JobShape.LATE: _Hours((_hm(14), _hm(22, 30)), ((_hm(13, 30), _hm(14)), (_hm(22, 30), _hm(23))),
                          _hm(10)),
    JobShape.NIGHTS: _Hours((_hm(22), _hm(30)), ((_hm(21, 30), _hm(22)), (_hm(30), _hm(30, 30))), None),
    JobShape.ROTATING: _Hours((_hm(7), _hm(19)), ((_hm(6, 30), _hm(7)), (_hm(19), _hm(19, 30))),
                              _hm(5, 45)),
    JobShape.STUDENT: _Hours((_hm(10), _hm(15)), (), _hm(9)),
    JobShape.REMOTE: _Hours((_hm(9, 30), _hm(17)), (), _hm(8, 30)),
}
SLEEP_MIN, OWL_SLEEP_MIN = 450, 390          # the night before a workday
NIGHTS_SLEEP = (_hm(7), _hm(14, 30))         # the morning after a night shift
GYM_DAYS = (0, 2, 4)                         # Mon, Wed, Fri
GYM_MIN = 60
KIDS_MORNING = (_hm(7), _hm(8, 15))          # weekdays
KIDS_EVENING = (_hm(17, 45), _hm(20))        # every day
GAMING_EVENING = (_hm(18), DAY_MIN)
GAMING_WEEKEND = (_hm(13), _hm(18))
NIGHT_OUT = (_hm(19, 30), _hm(23, 30))
GAMING_SHARE, GAMING_DAY_SHARE = 0.75, 0.5


@dataclass(frozen=True, slots=True)
class Span:
    start: int   # minutes from her local midnight
    end: int     # exclusive
    block: Block


@dataclass(frozen=True, slots=True)
class DayPlan:
    guid: int
    day: date
    job: JobShape
    kids: bool
    event: DayEvent
    spans: tuple[Span, ...]

    @property
    def workday(self) -> bool:
        """Her job's schedule has her working (a night shift starting) on this date."""
        return _workday(self.job, self.guid, self.day)

    def block_at(self, minute: int) -> Block:
        if not 0 <= minute < DAY_MIN:
            raise ValueError("minute must be 0-1439")
        for span in self.spans:
            if span.start <= minute < span.end:
                return span.block
        raise ValueError("plan does not cover the minute")


def job_shape(p: Persona) -> JobShape:
    text = f"{p.real_life.job} {p.real_life.rhythm}".casefold()
    for shape, pattern in _JOB_PATTERNS:
        if pattern.search(text):
            return shape
    return JobShape.DAY


def has_kids(p: Persona) -> bool:
    text = p.real_life.household.casefold()
    return bool(_KIDS.search(text)) and not _NO_KIDS.search(text)


def _gym(p: Persona) -> bool:
    rl = p.real_life
    return bool(_GYM.search(f"{rl.job} {rl.rhythm} {rl.life_thread}".casefold()))


def _workday(job: JobShape, guid: int, day: date) -> bool:
    if job is JobShape.HOME:
        return False
    if job is JobShape.NIGHTS:
        return day.weekday() in {(k + guid) % 7 for k in range(4)}
    if job is JobShape.ROTATING:
        return (day.toordinal() + guid) % 8 < 4
    return day.weekday() < 5


def _day_off_sleep(owl: bool, kids: bool) -> tuple[int, int]:
    if owl:
        return _hm(3), _hm(9, 30) if kids else _hm(11)
    return 0, _hm(7) if kids else _hm(8, 30)


def _morning_sleep(job: JobShape, guid: int, day: date, owl: bool, kids: bool) -> tuple[int, int]:
    if job is JobShape.NIGHTS:
        if _workday(job, guid, day - timedelta(days=1)):
            return NIGHTS_SLEEP
        return _day_off_sleep(owl, kids)
    if _workday(job, guid, day):
        wake = _HOURS[job].wake
        return max(0, wake - (OWL_SLEEP_MIN if owl else SLEEP_MIN)), wake
    return _day_off_sleep(owl, kids)


def _evening_sleep(job: JobShape, guid: int, day: date, owl: bool) -> tuple[int, int] | None:
    """The part before midnight of the next workday's sleep."""
    if job in (JobShape.NIGHTS, JobShape.HOME) or not _workday(job, guid, day + timedelta(days=1)):
        return None
    start = _HOURS[job].wake - (OWL_SLEEP_MIN if owl else SLEEP_MIN)
    return (DAY_MIN + start, DAY_MIN) if start < 0 else None


@dataclass(slots=True)
class _Shift:
    work: tuple[int, int]
    commutes: list[tuple[int, int]]
    starts_today: bool


def _shifts(job: JobShape, guid: int, day: date) -> list[_Shift]:
    """Yesterday's and today's shifts in today's minutes (a night shift runs past midnight)."""
    if job is JobShape.HOME:
        return []
    hours, out = _HOURS[job], []
    for back in (1, 0):
        if not _workday(job, guid, day - timedelta(days=back)):
            continue
        off = -back * DAY_MIN
        out.append(_Shift((hours.work[0] + off, hours.work[1] + off),
                          [(s + off, e + off) for s, e in hours.commutes], back == 0))
    return out


def _ends_today(shift: _Shift) -> bool:
    return 0 < shift.work[1] <= DAY_MIN


def _late_shift(shift: _Shift) -> None:
    end = shift.work[1]
    shift.work = (shift.work[0], end + LATE_SHIFT_MIN)
    shift.commutes = [(s + LATE_SHIFT_MIN, e + LATE_SHIFT_MIN) if s == end else (s, e)
                      for s, e in shift.commutes]


def _event(guid: int, day: date, *, work_ends_today: bool, kids: bool,
           event: DayEvent | None) -> DayEvent:
    if event is None:
        u = unit("event", guid, day)
        event = (DayEvent.LATE_SHIFT if u < .04 else DayEvent.SICK_KID if u < .07 else
                 DayEvent.NIGHT_OUT if u < .11 else DayEvent.BAD_DAY if u < .17 else DayEvent.NONE)
    if event is DayEvent.LATE_SHIFT and not work_ends_today:
        return DayEvent.NONE
    if event is DayEvent.SICK_KID and not kids:
        return DayEvent.NONE
    return DayEvent(event)


def _paint(minutes: list[Block], span: tuple[int, int], block: Block) -> None:
    start, end = max(0, span[0]), min(DAY_MIN, span[1])
    if start < end:
        minutes[start:end] = [block] * (end - start)


def _spans(minutes: list[Block]) -> tuple[Span, ...]:
    out, start = [], 0
    for i in range(1, DAY_MIN + 1):
        if i == DAY_MIN or minutes[i] is not minutes[start]:
            out.append(Span(start, i, minutes[start]))
            start = i
    return tuple(out)


def _build(p: Persona, day: date, event: DayEvent | None, *, carry: bool) -> DayPlan:
    guid, owl = p.guid, p.real_life.night_owl
    job, kids = job_shape(p), has_kids(p)
    shifts = _shifts(job, guid, day)
    event = _event(guid, day, work_ends_today=any(_ends_today(s) for s in shifts), kids=kids,
                   event=event)
    if event is DayEvent.LATE_SHIFT:
        for shift in shifts:
            if _ends_today(shift):
                _late_shift(shift)
    work, duty = [], []
    for shift in shifts:
        own = event is DayEvent.SICK_KID and shift.starts_today
        (duty if own else work).append((Block.WORK, shift.work))
        (duty if own else work).extend((Block.COMMUTE, c) for c in shift.commutes)
    morning = _morning_sleep(job, guid, day, owl, kids)

    minutes = [Block.FREE] * DAY_MIN
    if unit("game", guid, day) < GAMING_SHARE:
        _paint(minutes, GAMING_EVENING, Block.GAMING)
    if day.weekday() >= 5 and unit("game_day", guid, day) < GAMING_DAY_SHARE:
        _paint(minutes, GAMING_WEEKEND, Block.GAMING)
    if carry and morning[0] > 0:   # last night's gaming runs on to a bedtime past midnight
        yesterday = _build(p, day - timedelta(days=1), None, carry=False)
        if yesterday.block_at(DAY_MIN - 1) is Block.GAMING:
            _paint(minutes, (0, morning[0]), Block.GAMING)
    if kids:
        if day.weekday() < 5:
            _paint(minutes, KIDS_MORNING, Block.HOME_DUTY)
        _paint(minutes, KIDS_EVENING, Block.HOME_DUTY)
    for _, span in duty:
        _paint(minutes, span, Block.HOME_DUTY)
    if _gym(p) and day.weekday() in GYM_DAYS:
        ends = [min(DAY_MIN, e) for block, (s, e) in work
                if block is Block.COMMUTE and s < DAY_MIN and e > 0]
        start = max(GAMING_EVENING[0], max(ends, default=0))
        _paint(minutes, (start, start + GYM_MIN), Block.GYM)
    for block in (Block.COMMUTE, Block.WORK):
        for b, span in work:
            if b is block:
                _paint(minutes, span, block)
    _paint(minutes, morning, Block.ASLEEP)
    evening = _evening_sleep(job, guid, day, owl)
    if evening:
        _paint(minutes, evening, Block.ASLEEP)
    if job is JobShape.NIGHTS:   # a night shift is painted again over the sleep around it
        for block in (Block.COMMUTE, Block.WORK):
            for b, span in work:
                if b is block:
                    _paint(minutes, span, block)
    if event is DayEvent.NIGHT_OUT:
        _paint(minutes, NIGHT_OUT, Block.OUT)
    return DayPlan(guid, day, job, kids, event, _spans(minutes))


def day_plan(p: Persona, day: date, *, event: DayEvent | None = None) -> DayPlan:
    """Her plan for one local date. `event` None draws the day's event; an event the day cannot
    have (a late shift with no shift ending today, a sick kid without kids) reads as none."""
    return _build(p, day, event, carry=True)


def block_at(p: Persona, her_local: datetime) -> tuple[Block, DayEvent]:
    plan = day_plan(p, her_local.date())
    return plan.block_at(her_local.hour * 60 + her_local.minute), plan.event
