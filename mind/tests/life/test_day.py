from datetime import date
from conftest import make_persona
from leeroy_mind.life.day import Block, DayEvent, JobShape, day_plan, job_shape, has_kids

THU, SAT = date(2026, 11, 19), date(2026, 11, 21)

def blocks(p, d, *hm, event=DayEvent.NONE):
    plan = day_plan(p, d, event=event)
    return [plan.block_at(h * 60 + m) for h, m in hm]

def test_job_shape_reads_rhythm_and_job():
    a = lambda **kw: job_shape(make_persona(101, "Arkon", **kw))
    assert a() is JobShape.EARLY                       # "early shifts, free evenings"
    assert a(rhythm="9-5 office, raid nights 8-11pm") is JobShape.DAY
    assert a(rhythm="raid nights and late nights") is JobShape.DAY
    assert a(rhythm="night shifts on the ward") is JobShape.NIGHTS
    assert a(rhythm="4 on 4 off shifts (6a-2:30p)") is JobShape.ROTATING
    assert a(rhythm="5 a.m. starts, six crew") is JobShape.EARLY
    assert a(job="student", rhythm="lectures, then raids") is JobShape.STUDENT
    assert a(job="designer", rhythm="WFH most days") is JobShape.REMOTE
    assert a(job="stay-at-home mum", rhythm="school runs") is JobShape.HOME
    assert a(job="", rhythm="") is JobShape.DAY

def test_kids_from_household():
    assert has_kids(make_persona(101, "Arkon", household="husband and two kids"))
    assert not has_kids(make_persona(101, "Arkon", household="lives alone, no kids"))

def test_early_weekday():   # EARLY: wake 04:45, bed 21:15
    got = blocks(make_persona(101, "Arkon"), THU, (4, 0), (5, 40), (10, 0), (14, 45), (21, 0), (22, 0))
    assert got[:4] == [Block.ASLEEP, Block.COMMUTE, Block.WORK, Block.COMMUTE]
    assert got[4] in (Block.FREE, Block.GAMING) and got[5] is Block.ASLEEP

def test_day_job_with_kids_and_night_owl():   # owl before a workday: 6.5 h, so bed 01:00
    p = make_persona(101, "Arkon", rhythm="9-5 office", household="husband and a son", night_owl=True)
    assert blocks(p, THU, (2, 0), (7, 45), (8, 30), (12, 0), (17, 20), (18, 30)) == [
        Block.ASLEEP, Block.HOME_DUTY, Block.COMMUTE, Block.WORK, Block.COMMUTE, Block.HOME_DUTY]

def test_events_change_blocks():
    p = make_persona(101, "Arkon", rhythm="9-5 office", household="two kids")
    assert blocks(p, THU, (18, 0), event=DayEvent.LATE_SHIFT) == [Block.WORK]
    assert blocks(p, THU, (12, 0), event=DayEvent.SICK_KID) == [Block.HOME_DUTY]
    assert blocks(p, THU, (21, 0), event=DayEvent.NIGHT_OUT) == [Block.OUT]

def test_weekend_and_night_shift():
    assert blocks(make_persona(101, "Arkon"), SAT, (7, 0), (12, 0)) == [Block.ASLEEP, Block.FREE]
    nights = make_persona(101, "Arkon", rhythm="night shifts")
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


SHAPE_TEXT = {JobShape.DAY: "9-5 office", JobShape.EARLY: "early shifts", JobShape.LATE: "late shifts",
              JobShape.NIGHTS: "night shifts", JobShape.ROTATING: "4 on 4 off",
              JobShape.STUDENT: "lectures", JobShape.REMOTE: "wfh", JobShape.HOME: "retired"}


def test_every_shape_covers_each_day_in_order_with_any_event():
    for shape, rhythm in SHAPE_TEXT.items():
        for owl in (False, True):
            for household in ("lives alone", "two kids"):
                p = make_persona(101, "Arkon", rhythm=rhythm, night_owl=owl, household=household,
                                 life_thread="climbing on weekends")
                assert job_shape(p) is shape
                for i in range(14):
                    for event in (None, *DayEvent):
                        plan = day_plan(p, date(2026, 11, 16 + i), event=event)
                        assert plan.spans[0].start == 0 and plan.spans[-1].end == 1440
                        assert all(a.end == b.start and a.block is not b.block
                                   for a, b in zip(plan.spans, plan.spans[1:]))
                        assert all(plan.block_at(m) in Block for m in range(0, 1440, 15))


def test_impossible_events_read_as_none():
    alone = make_persona(101, "Arkon", rhythm="9-5 office", household="lives alone")
    assert day_plan(alone, SAT, event=DayEvent.LATE_SHIFT).event is DayEvent.NONE
    assert day_plan(alone, THU, event=DayEvent.SICK_KID).event is DayEvent.NONE
    assert day_plan(alone, THU, event=DayEvent.BAD_DAY).event is DayEvent.BAD_DAY
    bad = day_plan(alone, THU, event=DayEvent.BAD_DAY)
    assert bad.spans == day_plan(alone, THU, event=DayEvent.NONE).spans


def test_night_shift_runs_past_midnight_and_late_shift_moves_its_end():
    nights = make_persona(101, "Arkon", rhythm="night shifts")   # shifts start Thu-Sun
    fri = day_plan(nights, date(2026, 11, 20), event=DayEvent.NONE)
    assert fri.workday and [fri.block_at(m) for m in (3 * 60, 6 * 60 + 15, 10 * 60)] == [
        Block.WORK, Block.COMMUTE, Block.ASLEEP]
    late = day_plan(nights, date(2026, 11, 20), event=DayEvent.LATE_SHIFT)
    assert [late.block_at(m) for m in (7 * 60 + 30, 8 * 60 + 15, 9 * 60)] == [
        Block.WORK, Block.COMMUTE, Block.ASLEEP]
    wed = day_plan(nights, date(2026, 11, 18), event=DayEvent.LATE_SHIFT)   # no shift ends today
    assert not wed.workday and wed.event is DayEvent.NONE


def test_gym_after_the_commute_on_gym_days():
    p = make_persona(101, "Arkon", rhythm="9-5 office", life_thread="training for a marathon")
    mon, tue = date(2026, 11, 16), date(2026, 11, 17)
    assert day_plan(p, mon, event=DayEvent.NONE).block_at(18 * 60 + 30) is Block.GYM
    assert day_plan(p, tue, event=DayEvent.NONE).block_at(18 * 60 + 30) is not Block.GYM
    uplift = make_persona(101, "Arkon", rhythm="9-5 office", life_thread="an uplifting recycling job")
    assert day_plan(uplift, mon, event=DayEvent.NONE).block_at(18 * 60 + 30) is not Block.GYM
