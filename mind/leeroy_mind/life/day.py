"""Her day plan: every minute of her local day in one block (asleep, work, commute, home duty,
gym, out, free, gaming), built from her persona's job, rhythm, household and night_owl, with
one drawn event a day. Pure and deterministic; README.md has the tables."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta
from enum import StrEnum

from ..personas.schema import Persona
from .seed import unit

DAY_MIN = 1440
LATE_SHIFT_MIN = 120
PLAN_CACHE = 1024   # plans kept: two a member (today's, and yesterday's for the late evening)


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
    EVENING = "evening"   # bar and kitchen work: evenings, Wednesday to Sunday
    PART = "part"         # part-time, half days: weekday mornings
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


_APOS = "['\u2019\u02bc]?"   # typed or phone apostrophe, or none

# first match wins, in this order; "raid nights", "late nights" and "plays on nights" are no night
# shift. A shape may name text that rules it out (a teacher's classes are no study).
_JOB_PATTERNS = (
    (JobShape.NIGHTS, _bounded(rf"night[- ]?shifts?|third[- ]shifts?|work(?:s|ing)?(?: on)? nights"
                               rf"|(?:i{_APOS}m|she{_APOS}s|is|was|been) on nights"
                               r"|overnights?|graveyard"), None),
    (JobShape.ROTATING, _bounded(r"\d+\s*on\s*,?\s*\d+\s*off|\d+\s*(?:days|weeks)\s*on|rotating"
                                 r"|rota|12[- ]hour|on call"), None),
    (JobShape.EARLY, _bounded(r"early shifts?|early starts?|[4-6]\s*a\.?m\.?\s*starts?"
                              r"|starts? at [4-6]"), None),
    (JobShape.LATE, _bounded(r"late shifts?|evening shifts?|swing shifts?|afternoon shifts?"), None),
    # only what she does: an employer named "... College", a degree she holds, a course she
    # teaches or a job at a university is no study
    (JobShape.STUDENT, _bounded(r"student(?! loans?)|studying"
                                r"|(?:attends?|attending|has|skips?|skipping|goes to|going to) lectures?"
                                r"|(?:takes?|taking|attends?|attending|night|evening) classes"
                                rf"|(?:back|off|goes|going|still|is|she{_APOS}s|been|starts?|starting)"
                                r" (?:to|at|in) (?:uni|university|college)"
                                r"|(?:uni|college) (?:classes|courses?|lectures?|coursework|assignments?)"
                                r"|doing an? (?:[\w-]+ ){0,2}degree"),
     _bounded(r"teach\w*|tutor\w*|lecturer|professor|instructor|gives? lectures?"
              r"|works? (?:at|for) (?:the |a )?(?:uni|university|college)")),
    (JobShape.REMOTE, _bounded(r"wfh|works? from home|from home|freelanc\w*|self[- ]employed"), None),
    (JobShape.HOME, _bounded(r"stay[- ]at[- ]home|unemployed|between jobs|retired"), None),
)
# [talk] coherence (set by LifeService.configure from mind.toml, read at his login): the readings
# below. Off, a card reads exactly as the table above reads it.
COHERENT = True

# Self-play audit, 2026-11-17: every job the table did not name worked 09:00-17:00 and gamed
# 18:00-24:00, so a bartender on "evenings and weekends" was at work at 14:45 and gaming through
# her shift, a trainer with "early mornings for clients" and a half-day worker likewise, and a
# paralegal on maternity leave was "at work". With COHERENT, in this order: leave (she has the
# job and is not at it), the working hours her card states outright (_clock_shape: a card whose
# job says "third shift" and whose rhythm says "4am to 1pm work" has one clock, the stated one),
# the table above, then the words below. Leave, the hours and the words below are read only in
# clauses about work (_work_clauses): never in one about raiding, playing, being online, the gym
# or sleep ("raids 8pm to 11pm", "early mornings at the gym", "opens the guild bank").
_CLAUSE_SPLIT = re.compile(r"[,;.()]|\s+-\s+|\b(?:and|then|but|while|before|after which)\b")
_LEISURE = _bounded(r"raids?|raiding|plays?|playing|played|games?|gaming|wow|guild|online|logs? on"
                    r"|logging on|logged on|gym|sleeps?|sleeping|asleep|in bed|bedtime|naps?"
                    r"|after work|before work|off work|days? off|weekends? off|free time|hobby"
                    r"|stream\w*|discord")
_PAST = _bounded(r"last (?:year|month|summer|winter|spring)|years? ago|months? ago|used to|was on"
                 r"|were on|back from|came back|returned from|after (?:her|his|their|my) ")
_LEAVE = _bounded(r"(?:maternity|mat|parental|paternity|medical|sick|long[- ]term) leave"
                  r"|on (?:extended |unpaid )?leave|signed off (?:work|sick)|sabbatical"
                  r"|furlough(?:ed)?|laid off|out of work")
_EXTRA_PATTERNS = (
    (JobShape.NIGHTS, _bounded(r"(?:nurse|ward|hospital|a&e|icu|porter|security|guard|warehouse)"
                               r"\W+(?:\w+\W+){0,3}?on nights"
                               r"|nights (?:at|on) the (?:hospital|ward|warehouse|depot|plant)")),
    (JobShape.EARLY, _bounded(r"early mornings?|opening shifts?|morning shifts?"
                              r"|opens (?:up|the)|up (?:at|before) (?:dawn|[4-6])|baker|bakery"
                              r"|milk round|postie|postal round")),
    # bar, pub, club and kitchen work, and anyone whose card says evenings and weekends
    (JobShape.EVENING, _bounded(r"bartend\w*|barmaid|barman|barback|bar (?:staff|work|manager)"
                                r"|(?:behind|tends?|tending|runs?) (?:a |the )?bar"
                                r"|works? (?:at|in) (?:a |the )?(?:bar|pub|club|nightclub|restaurant)"
                                r"|pub (?:landlady|landlord|manager)|evenings and weekends"
                                r"|weekends and evenings|closing shifts?|works? evenings"
                                r"|evening work|chef|line cook|waits? tables"
                                r"|waitress(?:es|ing)? (?:evenings|nights)|bouncer|door staff")),
    (JobShape.LATE, _bounded(r"second[- ]shifts?|works? lates")),
    (JobShape.HOME, _bounded(r"full[- ]time (?:mum|mom|mother|dad|father|parent|carer)"
                             r"|housewife|homemaker")),
    (JobShape.PART, _bounded(r"part[- ]time|half[- ]days?|mornings only|school hours|term[- ]time"
                             r"|(?:two|three|2|3) (?:days|mornings) a week")),
)
# she works weekends (her days off fall in the week): "works weekends", "weekend shifts"
_WEEKENDS = _bounded(r"works? (?:most |every |on |at )?weekends|weekend shifts?|weekends too"
                     r"|evenings and weekends|weekends and evenings|saturdays? and sundays?")
# The hours her card states outright, in a clause about work: "4am to 1pm work", "works 2pm-10pm",
# "shift 22:00 to 06:00", "works 9 to 5".
_AMPM = r"(?:a\.?m\.?|p\.?m\.?)"
_CLOCK = re.compile(
    rf"(?<![\d:])(\d{{1,2}})(?::(\d{{2}}))?\s*({_AMPM})?\s*(?:to|until|till|til|-|–)\s*"
    rf"(\d{{1,2}})(?::(\d{{2}}))?\s*({_AMPM})?(?![\d:])")
_CLOCK_WORK = _bounded(r"works?|working|worked|shifts?|job|on call|on site|opens?|teach\w*"
                       r"|clients|patients|at the (?:office|shop|plant|depot|clinic|hospital"
                       r"|studio|bar|site|school)")


def _work_clauses(text: str) -> list[str]:
    """The clauses of her job and rhythm that are about work: every clause but those about
    raiding, playing, being online, the gym or sleep."""
    return [c for c in (x.strip() for x in _CLAUSE_SPLIT.split(text)) if c
            and not _LEISURE.search(c)]


def _minute(hour: str, minute: str | None, half: str | None) -> int | None:
    h, m = int(hour), int(minute or 0)
    if half:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if half.casefold().startswith("p") else 0)
    return h * 60 + m if h < 24 and m < 60 else None


def _clock_shape(clauses: list[str], *, bare: bool = False) -> JobShape | None:
    """The shape of the working hours her card states outright, read only in a work clause
    that has a word for working in it (works, shift, on call, opens, teaches, clients...); None
    when it states none. Hours with am, pm or minutes are read first and win over every word
    on the card; `bare` hours ("works 9 to 5", a day that ends after it starts) are read only
    once the table of words has said nothing, so "night shifts, 7 to 7" stays nights.
    By the hour she starts: 03:00-07:29 early, 07:30-10:59 day (a morning of five hours or
    less: part), 11:00-15:59 late, 16:00-18:59 evening, else nights."""
    for clause in clauses:
        if not _CLOCK_WORK.search(clause):
            continue
        for m in _CLOCK.finditer(clause):
            start, end = _minute(m[1], m[2], m[3]), _minute(m[4], m[5], m[6])
            if start is None or end is None:
                continue
            plain = not m[3] and not m[6] and m[2] is None and m[5] is None
            if plain is not bare:
                continue
            if plain:
                if not (1 <= int(m[1]) <= 12 and 1 <= int(m[4]) <= 12):
                    continue
                if end <= start:  # "9 to 5": the afternoon
                    end += 12 * 60
                elif start < _hm(6):  # "1 to 9": an afternoon shift, never 1am
                    start, end = start + 12 * 60, end + 12 * 60
            length = (end - start) % DAY_MIN
            if not 120 <= length <= 14 * 60:
                continue
            start %= DAY_MIN
            if _hm(3) <= start < _hm(7, 30):
                return JobShape.EARLY
            if _hm(7, 30) <= start < _hm(11):
                return JobShape.PART if length <= 300 else JobShape.DAY
            if _hm(11) <= start < _hm(16):
                return JobShape.LATE
            if _hm(16) <= start < _hm(19):
                return JobShape.EVENING
            return JobShape.NIGHTS
    return None
_KIDS = _bounded(r"kids?|sons?|daughters?|bab(?:y|ies)|toddlers?|child(?:ren)?|twins|stepkids?")
# a household with no child at home: none, child-free, an empty nest, or she lives alone
_NO_KIDS = _bounded(r"no (?:kids|children)|child[- ]?free|empty nest"
                    rf"|(?:doesn{_APOS}t|does not|don{_APOS}t|do not|never|without)\s+(?:\w+\s+){{0,2}}"
                    r"(?:kids|children)"
                    r"|(?:lives|living) alone(?!\s+with\s+(?:her|his|their)\b)")
# a child who is not a dependent at home (checked in the child's own clause); these may also
# follow the child after a comma ("a son, 19, away at uni")
_GONE = (r"grown(?:[- ]up)?|adult|away|(?:at|in) (?:college|uni|university)"
         r"|with (?:(?:the|their|her|his) )?(?:dad|ex|mum|mom|father|mother)"
         r"|visits?|visiting|moved out")
# a child not born yet (only in the child's own clause: "three kids, expecting a fourth")
_AWAY = _bounded(_GONE + r"|on the way|expecting|pregnant|trying for")
_QUALIFIER = re.compile(rf"\s*(?:\d|(?:{_GONE})\b)")
_CLAUSE = re.compile(r"[,;./]|\band\b")
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
    JobShape.EVENING: _Hours((_hm(16), _hm(23, 30)), ((_hm(15, 30), _hm(16)), (_hm(23, 30), DAY_MIN)),
                             _hm(10)),
    JobShape.PART: _Hours((_hm(9), _hm(13)), ((_hm(8, 30), _hm(9)), (_hm(13), _hm(13, 30))),
                          _hm(7, 30)),
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
    weekends: bool = False   # she works weekends: her days off are Monday and Tuesday

    @property
    def workday(self) -> bool:
        """Her job's schedule has her working (a night shift starting) on this date."""
        return _workday(self.job, self.guid, self.day, self.weekends)

    def block_at(self, minute: int) -> Block:
        if not 0 <= minute < DAY_MIN:
            raise ValueError("minute must be 0-1439")
        for span in self.spans:
            if span.start <= minute < span.end:
                return span.block
        raise ValueError("plan does not cover the minute")


@dataclass(frozen=True, slots=True)
class _Facts:
    """What her plan is built from: the cache key, so an edited persona gets a fresh plan."""
    guid: int
    job: str
    rhythm: str
    household: str
    life_thread: str
    night_owl: bool
    coherent: bool = True   # COHERENT when the plan was asked for: a flipped switch, a fresh plan


def _facts(p: Persona) -> _Facts:
    rl = p.real_life
    return _Facts(p.guid, rl.job, rl.rhythm, rl.household, rl.life_thread, rl.night_owl,
                  COHERENT)


def _job_shape(job: str, rhythm: str) -> JobShape:
    text = f"{job} {rhythm}".casefold()
    clauses = _work_clauses(f"{job}. {rhythm}".casefold()) if COHERENT else []
    if any(_LEAVE.search(c) and not _PAST.search(c) for c in clauses):
        return JobShape.HOME  # she has the job and is not at it
    stated = _clock_shape(clauses)
    if stated is not None:
        return stated
    for shape, pattern, unless in _JOB_PATTERNS:
        if pattern.search(text) and not (unless and unless.search(text)):
            return shape
    stated = _clock_shape(clauses, bare=True)
    if stated is not None:
        return stated
    for shape, pattern in _EXTRA_PATTERNS:
        if any(pattern.search(c) for c in clauses):
            return shape
    return JobShape.DAY


def _works_weekends(job: str, rhythm: str) -> bool:
    """Her card says she works weekends (in a clause about work): her days off fall in the
    week. Never with COHERENT off."""
    return COHERENT and any(_WEEKENDS.search(c) for c in _work_clauses(f"{job}. {rhythm}".casefold()))


def _kids_at_home(household: str) -> bool:
    """A dependent child at home: named in a clause of her household that does not put the
    child elsewhere, grown up or not born yet. Brackets stay in their clause, and a clause after
    a comma that starts with a number or a word of `_GONE` belongs to the child before it."""
    text = household.casefold().replace("(", " ").replace(")", " ")
    if _NO_KIDS.search(text):
        return False
    groups: list[str] = []
    for clause in _CLAUSE.split(text):
        if groups and not _KIDS.search(clause) and _QUALIFIER.match(clause):
            groups[-1] += " " + clause
        else:
            groups.append(clause)
    return any(_KIDS.search(g) and not _AWAY.search(g) for g in groups)


def job_shape(p: Persona) -> JobShape:
    return _job_shape(p.real_life.job, p.real_life.rhythm)


def has_kids(p: Persona) -> bool:
    return _kids_at_home(p.real_life.household)


def _gym(f: _Facts) -> bool:
    return bool(_GYM.search(f"{f.job} {f.rhythm} {f.life_thread}".casefold()))


def _workday(job: JobShape, guid: int, day: date, weekends: bool = False) -> bool:
    if job is JobShape.HOME:
        return False
    if weekends and job not in (JobShape.NIGHTS, JobShape.ROTATING):
        return day.weekday() >= 2  # Wednesday to Sunday
    if job is JobShape.NIGHTS:
        return day.weekday() in {(k + guid) % 7 for k in range(4)}
    if job is JobShape.ROTATING:
        return (day.toordinal() + guid) % 8 < 4
    if job is JobShape.EVENING:
        return day.weekday() >= 2  # Wednesday to Sunday: her weekend is Monday and Tuesday
    return day.weekday() < 5


def _day_off_sleep(owl: bool, kids: bool) -> tuple[int, int]:
    if owl:
        return _hm(3), _hm(9, 30) if kids else _hm(11)
    return 0, _hm(7) if kids else _hm(8, 30)


def _morning_sleep(job: JobShape, guid: int, day: date, owl: bool, kids: bool,
                   weekends: bool = False) -> tuple[int, int]:
    if job is JobShape.NIGHTS:
        if _workday(job, guid, day - timedelta(days=1)):
            return NIGHTS_SLEEP
        return _day_off_sleep(owl, kids)
    if _workday(job, guid, day, weekends):
        wake = _HOURS[job].wake
        return max(0, wake - (OWL_SLEEP_MIN if owl else SLEEP_MIN)), wake
    return _day_off_sleep(owl, kids)


def _evening_sleep(job: JobShape, guid: int, day: date, owl: bool,
                   weekends: bool = False) -> tuple[int, int] | None:
    """The part before midnight of the next workday's sleep."""
    if job in (JobShape.NIGHTS, JobShape.HOME) or not _workday(job, guid, day + timedelta(days=1),
                                                                weekends):
        return None
    start = _HOURS[job].wake - (OWL_SLEEP_MIN if owl else SLEEP_MIN)
    return (DAY_MIN + start, DAY_MIN) if start < 0 else None


@dataclass(slots=True)
class _Shift:
    work: tuple[int, int]
    commutes: list[tuple[int, int]]
    starts_today: bool


def _shifts(job: JobShape, guid: int, day: date, weekends: bool = False) -> list[_Shift]:
    """Yesterday's and today's shifts in today's minutes (a night shift runs past midnight)."""
    if job is JobShape.HOME:
        return []
    hours, out = _HOURS[job], []
    for back in (1, 0):
        if not _workday(job, guid, day - timedelta(days=back), weekends):
            continue
        off = -back * DAY_MIN
        out.append(_Shift((hours.work[0] + off, hours.work[1] + off),
                          [(s + off, e + off) for s, e in hours.commutes], back == 0))
    return out


def _ends_today(shift: _Shift) -> bool:
    return 0 < shift.work[1] <= DAY_MIN


def _ended_yesterday(shift: _Shift) -> bool:
    return -DAY_MIN < shift.work[1] <= 0


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


def _paint_shifts(minutes: list[Block], entries: list[tuple[Block, tuple[int, int]]]) -> None:
    for block in (Block.COMMUTE, Block.WORK):
        for b, span in entries:
            if b is block:
                _paint(minutes, span, block)


@lru_cache(maxsize=PLAN_CACHE)
def _build(f: _Facts, day: date, event: DayEvent | None, carry: bool) -> DayPlan:
    guid, owl = f.guid, f.night_owl
    job, kids = _job_shape(f.job, f.rhythm), _kids_at_home(f.household)
    weekends = _works_weekends(f.job, f.rhythm)
    shifts = _shifts(job, guid, day, weekends)
    ending = [s for s in shifts if _ends_today(s)]
    event = _event(guid, day, work_ends_today=bool(ending), kids=kids, event=event)
    # yesterday's shift, in today's minutes, keeps yesterday's drawn event past midnight
    carried = next((s for s in shifts if not s.starts_today), None)
    before = DayEvent.NONE
    if carried is not None:
        earlier = _shifts(job, guid, day - timedelta(days=1), weekends)
        before = _event(guid, day - timedelta(days=1), kids=kids, event=None,
                        work_ends_today=any(_ends_today(s) for s in earlier))
        if before is DayEvent.LATE_SHIFT and _ended_yesterday(carried):
            _late_shift(carried)
    if event is DayEvent.LATE_SHIFT:
        for shift in ending:
            _late_shift(shift)
    work, tail, duty = [], [], []
    for shift in shifts:
        sick = (event if shift.starts_today else before) is DayEvent.SICK_KID
        into = duty if sick else work if shift.starts_today else tail
        into.append((Block.WORK, shift.work))
        into.extend((Block.COMMUTE, c) for c in shift.commutes)
    morning = _morning_sleep(job, guid, day, owl, kids, weekends)

    minutes = [Block.FREE] * DAY_MIN
    if unit("game", guid, day) < GAMING_SHARE:
        _paint(minutes, GAMING_EVENING, Block.GAMING)
    if day.weekday() >= 5 and unit("game_day", guid, day) < GAMING_DAY_SHARE:
        _paint(minutes, GAMING_WEEKEND, Block.GAMING)
    if carry and morning[0] > 0:   # last night's gaming runs on to a bedtime past midnight
        yesterday = _build(f, day - timedelta(days=1), None, False)
        if yesterday.block_at(DAY_MIN - 1) is Block.GAMING:
            _paint(minutes, (0, morning[0]), Block.GAMING)
    if kids:
        if day.weekday() < 5:
            _paint(minutes, KIDS_MORNING, Block.HOME_DUTY)
        _paint(minutes, KIDS_EVENING, Block.HOME_DUTY)
    for _, span in duty:
        _paint(minutes, span, Block.HOME_DUTY)
    if _gym(f) and day.weekday() in GYM_DAYS:
        ends = [min(DAY_MIN, e) for block, (s, e) in work + tail
                if block is Block.COMMUTE and s < DAY_MIN and e > 0]
        start = max(GAMING_EVENING[0], max(ends, default=0))
        _paint(minutes, (start, start + GYM_MIN), Block.GYM)
    _paint_shifts(minutes, work + tail)
    _paint(minutes, morning, Block.ASLEEP)
    evening = _evening_sleep(job, guid, day, owl, weekends)
    if evening:
        _paint(minutes, evening, Block.ASLEEP)
    # a shift that runs past midnight is painted again over the sleep around it
    _paint_shifts(minutes, work + tail if job is JobShape.NIGHTS else tail)
    if event is DayEvent.NIGHT_OUT:
        _paint(minutes, NIGHT_OUT, Block.OUT)
    return DayPlan(guid, day, job, kids, event, _spans(minutes), weekends)


def day_plan(p: Persona, day: date, *, event: DayEvent | None = None) -> DayPlan:
    """Her plan for one local date. `event` None draws the day's event; an event the day cannot
    have (a late shift with no shift ending today, a sick kid without kids) reads as none. A
    forced event changes only this date: the part of a shift past midnight in the next date's
    plan follows the drawn event of the day it started. Plans are immutable and cached by her
    schedule facts and the date (PLAN_CACHE)."""
    return _build(_facts(p), day, event, True)


def block_at(p: Persona, her_local: datetime) -> tuple[Block, DayEvent]:
    plan = day_plan(p, her_local.date())
    return plan.block_at(her_local.hour * 60 + her_local.minute), plan.event
