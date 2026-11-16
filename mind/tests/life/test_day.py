from datetime import date, timedelta
from conftest import make_persona
from leeroy_mind.life.day import Block, DayEvent, JobShape, day_plan, job_shape, has_kids

THU, SAT = date(2026, 11, 19), date(2026, 11, 21)

def blocks(p, d, *hm, event=DayEvent.NONE):
    plan = day_plan(p, d, event=event)
    return [plan.block_at(h * 60 + m) for h, m in hm]

def test_job_shape_reads_rhythm_and_job():
    a = lambda **kw: job_shape(make_persona(101, "Arkon", **kw))
    assert a() is JobShape.EARLY                       # "early shifts, free evenings"
    assert a(rhythm="office temp, raid nights after tea") is JobShape.DAY
    assert a(rhythm="raid nights and late nights") is JobShape.DAY
    assert a(rhythm="night shifts on the ward") is JobShape.NIGHTS
    assert a(rhythm="5 on 5 off at the depot (7a-7p)") is JobShape.ROTATING
    assert a(rhythm="6 a.m. starts at the depot") is JobShape.EARLY
    assert a(job="student", rhythm="lectures, then raids") is JobShape.STUDENT
    assert a(job="designer", rhythm="WFH most days") is JobShape.REMOTE
    assert a(job="stay-at-home mum", rhythm="school runs") is JobShape.HOME
    assert a(job="", rhythm="") is JobShape.DAY

def test_kids_from_household():
    assert has_kids(make_persona(101, "Arkon", household="wife and three kids"))
    assert not has_kids(make_persona(101, "Arkon", household="lives alone, no kids"))

def test_early_weekday():   # EARLY: wake 04:45, bed 21:15
    got = blocks(make_persona(101, "Arkon"), THU, (4, 0), (5, 40), (10, 0), (14, 45), (21, 0), (22, 0))
    assert got[:4] == [Block.ASLEEP, Block.COMMUTE, Block.WORK, Block.COMMUTE]
    assert got[4] in (Block.FREE, Block.GAMING) and got[5] is Block.ASLEEP

def test_day_job_with_kids_and_night_owl():   # owl before a workday: 6.5 h, so bed 01:00
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five", household="a partner and a son in reception", night_owl=True)
    assert blocks(p, THU, (2, 0), (7, 45), (8, 30), (12, 0), (17, 20), (18, 30)) == [
        Block.ASLEEP, Block.HOME_DUTY, Block.COMMUTE, Block.WORK, Block.COMMUTE, Block.HOME_DUTY]

def test_events_change_blocks():
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five", household="three kids at home")
    assert blocks(p, THU, (18, 0), event=DayEvent.LATE_SHIFT) == [Block.WORK]
    assert blocks(p, THU, (12, 0), event=DayEvent.SICK_KID) == [Block.HOME_DUTY]
    assert blocks(p, THU, (21, 0), event=DayEvent.NIGHT_OUT) == [Block.OUT]

def test_weekend_and_night_shift():
    assert blocks(make_persona(101, "Arkon"), SAT, (7, 0), (12, 0)) == [Block.ASLEEP, Block.FREE]
    nights = make_persona(101, "Arkon", rhythm="night shifts at the depot")
    plans = [day_plan(nights, date(2026, 11, 16 + i), event=DayEvent.NONE) for i in range(7)]
    assert sum(p.block_at(23 * 60) is Block.WORK for p in plans) == 4

def test_every_minute_in_one_block_for_odd_text():   # Review Focus 3
    for kw in ({}, {"rhythm": "", "job": ""}, {"rhythm": "???", "night_owl": True}):
        p = make_persona(101, "Arkon", **kw)
        for i in range(7):
            plan = day_plan(p, date(2026, 11, 16 + i))
            covered = sorted((s.start, s.end) for s in plan.spans)
            assert covered[0][0] == 0 and covered[-1][1] == 1440
            assert all(a[1] == b[0] for a, b in zip(covered, covered[1:]))

def test_event_draw_is_deterministic():
    p = make_persona(101, "Arkon")
    assert [day_plan(p, date(2026, 11, d)).event for d in range(1, 31)] == \
           [day_plan(p, date(2026, 11, d)).event for d in range(1, 31)]


SHAPE_TEXT = {JobShape.DAY: "office admin, nine till five", JobShape.EARLY: "early shifts at the bakery",
              JobShape.LATE: "late shifts at the cinema", JobShape.NIGHTS: "night shifts at the depot",
              JobShape.ROTATING: "5 on 5 off", JobShape.STUDENT: "a student in her final year",
              JobShape.REMOTE: "wfh", JobShape.HOME: "retired"}


def test_every_shape_covers_each_day_in_order_with_any_event():
    for shape, rhythm in SHAPE_TEXT.items():
        for owl in (False, True):
            for household in ("with a lodger", "three kids at home"):
                p = make_persona(101, "Arkon", rhythm=rhythm, night_owl=owl, household=household,
                                 life_thread="bouldering at the climbing wall")
                assert job_shape(p) is shape
                for i in range(14):
                    for event in (None, *DayEvent):
                        plan = day_plan(p, date(2026, 11, 16 + i), event=event)
                        assert plan.spans[0].start == 0 and plan.spans[-1].end == 1440
                        assert all(a.end == b.start and a.block is not b.block
                                   for a, b in zip(plan.spans, plan.spans[1:]))
                        assert all(plan.block_at(m) in Block for m in range(0, 1440, 15))


def test_impossible_events_read_as_none():
    alone = make_persona(101, "Arkon", rhythm="office admin, nine till five", household="with a lodger")
    assert day_plan(alone, SAT, event=DayEvent.LATE_SHIFT).event is DayEvent.NONE
    assert day_plan(alone, THU, event=DayEvent.SICK_KID).event is DayEvent.NONE
    assert day_plan(alone, THU, event=DayEvent.BAD_DAY).event is DayEvent.BAD_DAY
    bad = day_plan(alone, THU, event=DayEvent.BAD_DAY)
    assert bad.spans == day_plan(alone, THU, event=DayEvent.NONE).spans


def test_night_shift_runs_past_midnight_and_late_shift_moves_its_end():
    nights = make_persona(101, "Arkon", rhythm="night shifts at the depot")   # start Thu-Sun
    fri = day_plan(nights, date(2026, 11, 20), event=DayEvent.NONE)
    assert fri.workday and [fri.block_at(m) for m in (3 * 60, 6 * 60 + 15, 10 * 60)] == [
        Block.WORK, Block.COMMUTE, Block.ASLEEP]
    late = day_plan(nights, date(2026, 11, 20), event=DayEvent.LATE_SHIFT)
    assert [late.block_at(m) for m in (7 * 60 + 30, 8 * 60 + 15, 9 * 60)] == [
        Block.WORK, Block.COMMUTE, Block.ASLEEP]
    wed = day_plan(nights, date(2026, 11, 18), event=DayEvent.LATE_SHIFT)   # no shift ends today
    assert not wed.workday and wed.event is DayEvent.NONE


def test_gym_after_the_commute_on_gym_days():
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five", life_thread="training for a marathon")
    mon, tue = date(2026, 11, 16), date(2026, 11, 17)
    assert day_plan(p, mon, event=DayEvent.NONE).block_at(18 * 60 + 30) is Block.GYM
    assert day_plan(p, tue, event=DayEvent.NONE).block_at(18 * 60 + 30) is not Block.GYM
    uplift = make_persona(101, "Arkon", rhythm="office admin, nine till five", life_thread="an uplifting recycling job")
    assert day_plan(uplift, mon, event=DayEvent.NONE).block_at(18 * 60 + 30) is not Block.GYM


def test_student_only_when_she_studies():
    a = lambda **kw: job_shape(make_persona(101, "Arkon", **kw))
    assert a(job="dental student", rhythm="exams in spring") is JobShape.STUDENT
    assert a(job="florist's assistant", rhythm="studying for an accounting exam") is JobShape.STUDENT
    assert a(job="trainee", rhythm="back to uni in October") is JobShape.STUDENT
    assert a(job="cashier", rhythm="night classes on Tuesdays") is JobShape.STUDENT
    assert a(job="cashier", rhythm="doing a college degree part-time") is JobShape.STUDENT
    assert a(job="cashier", rhythm="goes to lectures twice a week") is JobShape.STUDENT
    assert a(job="cashier", rhythm="still at uni, raids at night") is JobShape.STUDENT
    assert a(job="caretaker at Hillcrest College", rhythm="weekdays") is JobShape.DAY
    assert a(job="technician at a university lab", rhythm="standard hours") is JobShape.DAY
    assert a(job="assistant coach for a college rowing team", rhythm="regattas") is JobShape.DAY
    assert a(job="dance teacher", rhythm="teaches two classes a night") is JobShape.DAY
    for text in ("teaches evening classes", "teaches night classes", "gives lectures twice a week",
                 "works at uni as a cleaner", "admissions officer in university admin",
                 "accountant with a college degree", "went to college for nursing",
                 "student loans to pay off", "tutors a student on Sundays"):
        assert a(job="", rhythm=text) is JobShape.DAY, text


def test_night_shift_spellings():
    a = lambda **kw: job_shape(make_persona(101, "Arkon", **kw))
    assert a(job="hospital porter", rhythm="on the night-shift team") is JobShape.NIGHTS
    assert a(job="baker", rhythm="third shift, bed at dawn") is JobShape.NIGHTS
    assert a(job="baker", rhythm="third-shift baking") is JobShape.NIGHTS
    assert a(job="security guard", rhythm="graveyard hours") is JobShape.NIGHTS
    assert a(job="hospital porter", rhythm="nightshift, sleeps till two") is JobShape.NIGHTS
    assert a(job="cleaner", rhythm="working nights at the depot") is JobShape.NIGHTS
    assert a(job="nurse", rhythm="she's on nights this month") is JobShape.NIGHTS
    assert a(job="packer", rhythm="days at the warehouse, raid nights") is JobShape.DAY
    assert a(job="cook", rhythm="plays on nights and weekends") is JobShape.DAY
    assert a(job="cook", rhythm="logs on nights after the kids sleep") is JobShape.DAY


def test_kids_only_dependents_at_home():
    k = lambda household: has_kids(make_persona(101, "Arkon", household=household))
    for home in ("married, two kids (6 and 9)", "single dad with a daughter in year two",
                 "wife and a baby girl", "partner, a son (12) and a dog",
                 "she and her teenage son share a flat",
                 "two daughters; the elder at university, the younger at home",
                 "living alone with her son", "two kids, husband works away a lot",
                 "three kids, expecting a fourth", "a daughter (7) and a son, 20, away at uni"):
        assert k(home), home
    for away in ("lives alone, no kids", "her sons are grown up now",
                 "shares the house with two adult daughters",
                 "one daughter who is away at university", "son studying in college in another state",
                 "her kids live with their mother", "a son who stays with her ex",
                 "an empty nest since the twins left", "her stepkids visit every other weekend",
                 "living alone with a parrot; one son", "son moved out last year",
                 "no children, two cats", "child-free by choice", "doesn't have any kids",
                 "doesn\u2019t want kids", "never had kids", "married without kids",
                 "trying for a baby", "a baby on the way in May", "pregnant with twins",
                 "one son (away at university)", "son, 19, at college"):
        assert not k(away), away


def test_plans_are_cached_by_her_facts():
    from leeroy_mind.life import day
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five")
    first = day_plan(p, THU)
    hits = day._build.cache_info().hits
    assert day_plan(make_persona(101, "Arkon", rhythm="office admin, nine till five"), THU) is first
    assert day._build.cache_info().hits > hits
    edited = day_plan(make_persona(101, "Arkon", rhythm="graveyard hours"), THU, event=DayEvent.NONE)
    assert edited.job is JobShape.NIGHTS and edited.block_at(23 * 60) is Block.WORK


def first_drawn(p, event, *, weekdays=range(7)):
    d = date(2026, 1, 1)
    while not (day_plan(p, d).event is event and d.weekday() in weekdays):
        d += timedelta(days=1)
    return d


def test_a_late_shift_past_midnight_keeps_its_tail():   # review P2-C
    p = make_persona(101, "Arkon", rhythm="late shifts at the cinema")
    d = first_drawn(p, DayEvent.LATE_SHIFT, weekdays=range(4))   # Mon-Thu: the next day works too
    assert day_plan(p, d).block_at(23 * 60 + 59) is Block.WORK
    after = day_plan(p, d + timedelta(days=1))
    assert [after.block_at(m) for m in (15, 45)] == [Block.WORK, Block.COMMUTE]
    assert after.block_at(90) not in (Block.WORK, Block.COMMUTE)
    fri = first_drawn(p, DayEvent.LATE_SHIFT, weekdays=(4,))      # Saturday sleeps from 01:00
    sat = day_plan(p, fri + timedelta(days=1))
    assert [sat.block_at(m) for m in (15, 45, 90)] == [Block.WORK, Block.COMMUTE, Block.ASLEEP]


def test_a_sick_kid_keeps_her_home_for_the_whole_night_shift():   # review P2-C
    p = make_persona(101, "Arkon", rhythm="night shifts at the depot", household="three kids at home")
    d = first_drawn(p, DayEvent.SICK_KID, weekdays=(3, 4, 5, 6))   # her shifts start Thu-Sun
    assert day_plan(p, d).block_at(23 * 60) is Block.HOME_DUTY
    after = day_plan(p, d + timedelta(days=1))
    assert [after.block_at(m) for m in (15, 2 * 60, 6 * 60 + 15)] == [Block.HOME_DUTY] * 3


def test_a_forced_event_changes_only_its_own_date():
    p = make_persona(101, "Arkon", rhythm="late shifts at the cinema")
    d = date(2026, 11, 16)
    while day_plan(p, d).event is not DayEvent.NONE or d.weekday() > 3:
        d += timedelta(days=1)
    assert day_plan(p, d, event=DayEvent.LATE_SHIFT).block_at(23 * 60 + 59) is Block.WORK
    assert day_plan(p, d + timedelta(days=1)).block_at(15) is not Block.WORK
