"""[talk] coherence, one "right now" per conversation: job shapes for evening, early-morning,
part-time and leave (life/day.py), where she said she is holding for the conversation and a
grouped member told she is in his group and where (life/presence.py, Pipeline._where_line).
Fakes only; invented lines and jobs; the test cast."""
from datetime import date

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from engine_rig import engine_clock, save_member
from engine_rig import make_rig as _make_rig


def make_rig(*args, **kw):
    kw.setdefault("coherence", True)  # the switch under test; a test of "off" says so
    return _make_rig(*args, **kw)
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.life.day import Block, DayEvent, JobShape, day_plan, job_shape
from leeroy_mind.life.presence import (STILL, Attention, LifePresence, Where, playing_text,
                                       settled_where, stated_where, where_text)
from leeroy_mind.protocol import Chat, Group, Member
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

MON, WED, SAT = date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 21)


pytestmark = pytest.mark.usefixtures("coherent")  # [talk] coherence is off by default
JOBS = [
    ("bartender at a sports bar", "evenings and weekends, logs on after closing", JobShape.EVENING),
    ("sous chef", "home after midnight", JobShape.EVENING),
    ("runs the bar at a rugby club", "", JobShape.EVENING),
    ("personal trainer", "early mornings for clients, afternoons free", JobShape.EARLY),
    ("baker", "in bed by nine", JobShape.EARLY),
    ("barista", "opens the shop most days", JobShape.EARLY),
    ("keeper at a wildlife centre", "half-day work, home by lunch", JobShape.PART),
    ("shop assistant, part-time", "school hours", JobShape.PART),
    ("nurse on nights at the county hospital", "sleeps through the morning", JobShape.NIGHTS),
    ("paralegal on maternity leave", "up with the baby", JobShape.HOME),
    ("fitter, signed off sick", "", JobShape.HOME),
    ("developer, working from home", "flexible", JobShape.REMOTE),
    ("accountant", "nine to five, raids 7pm to 11pm", JobShape.DAY),
    ("teacher", "drinks at the bar on fridays", JobShape.DAY),
    # a card with two clocks has one: the hours it states outright
    ("plant operator, third shift", "4am to 1pm work, sleeps early", JobShape.EARLY),
    ("warehouse picker", "works 2pm-10pm", JobShape.LATE),
    ("security guard", "shift 22:00 to 06:00", JobShape.NIGHTS),
    ("receptionist", "works 9am to 1pm", JobShape.PART),
]


# review of #370: hours and words in a clause about raiding, playing, being online, the gym or
# sleep are not her work; past leave is no leave; bare hours never outrank "night shifts"
NOT_WORK = [
    ("office admin", "works 9 to 5, raids 8pm to 11pm", JobShape.DAY),
    ("clerk", "plays 7pm-11pm after work", JobShape.DAY),
    ("analyst", "works 9 to 5, online 19:30 to 23:00", JobShape.DAY),
    ("student", "varying shifts, online from 6am to 8am some days", JobShape.STUDENT),
    ("receptionist", "early mornings at the gym", JobShape.DAY),
    ("cashier", "online early morning", JobShape.DAY),
    ("clerk", "opens the guild bank", JobShape.DAY),
    ("fitter", "sick leave last year, back at it now", JobShape.DAY),
    ("driver", "sleeps 10pm to 6am, works days", JobShape.DAY),
    ("nurse", "night shifts, 7 to 7", JobShape.NIGHTS),
    ("porter", "works 1 to 9", JobShape.LATE),
]


@pytest.mark.parametrize("job,rhythm,shape", NOT_WORK)
def test_only_work_clauses_shape_her_job(job, rhythm, shape):
    assert job_shape(make_persona(101, "Arkon", job=job, rhythm=rhythm)) is shape


def test_working_weekends_moves_her_days_off_into_the_week():
    p = make_persona(101, "Arkon", job="shop assistant", rhythm="works weekends")
    assert day_plan(p, SAT, event=DayEvent.NONE).block_at(11 * 60) is Block.WORK
    assert day_plan(p, MON, event=DayEvent.NONE).block_at(11 * 60) is not Block.WORK
    plain = make_persona(101, "Arkon", job="shop assistant", rhythm="nine to five")
    assert day_plan(plain, SAT, event=DayEvent.NONE).block_at(11 * 60) is not Block.WORK


def test_with_the_switch_off_every_card_reads_as_before():
    from leeroy_mind.life import day
    day.COHERENT = False
    for job, rhythm, shape in JOBS:
        got = job_shape(make_persona(101, "Arkon", job=job, rhythm=rhythm))
        assert got not in (JobShape.EVENING, JobShape.PART), (job, got)
    assert job_shape(make_persona(101, "Arkon", job="bartender", rhythm="")) is JobShape.DAY
    p = make_persona(101, "Arkon", job="shop assistant", rhythm="works weekends")
    assert day_plan(p, SAT, event=DayEvent.NONE).block_at(11 * 60) is not Block.WORK


@pytest.mark.parametrize("job,rhythm,shape", JOBS)
def test_job_shapes_read_the_card(job, rhythm, shape):
    assert job_shape(make_persona(101, "Arkon", job=job, rhythm=rhythm)) is shape


def test_a_bartender_is_free_by_day_and_behind_the_bar_at_night():
    p = make_persona(101, "Arkon", job="bartender", rhythm="evenings and weekends")
    wed = day_plan(p, WED, event=DayEvent.NONE)
    assert wed.block_at(14 * 60 + 45) is not Block.WORK  # 14:45: not at work
    assert wed.block_at(20 * 60) is Block.WORK and wed.block_at(9 * 60) is Block.ASLEEP
    assert day_plan(p, SAT, event=DayEvent.NONE).block_at(21 * 60) is Block.WORK
    assert day_plan(p, MON, event=DayEvent.NONE).block_at(21 * 60) is not Block.WORK  # her weekend


LATE_CARDS = [  # an early job whose card says she plays late: up until midnight
    ("baker", "early shifts, late nights are for the game", False),
    ("postie", "4am to 1pm work, raids at 8pm and plays until 11pm", False),
    ("barista", "opens the shop most days, raid nights wed and sun", False),
    ("plant operator", "early starts", True),  # the night-owl field alone
]


@pytest.mark.parametrize("job,rhythm,owl", LATE_CARDS)
def test_an_early_job_never_puts_a_late_player_to_bed_in_his_evening(job, rhythm, owl):
    p = make_persona(101, "Arkon", job=job, rhythm=rhythm, night_owl=owl)
    assert job_shape(p) is JobShape.EARLY
    plan = day_plan(p, WED, event=DayEvent.NONE)  # Thursday is a workday
    assert all(plan.block_at(m) is not Block.ASLEEP for m in range(17 * 60, 24 * 60))


def test_no_job_shape_alone_has_her_asleep_before_half_past_eleven():
    plain = make_persona(101, "Arkon", job="baker", rhythm="early shifts")
    plan = day_plan(plain, WED, event=DayEvent.NONE)
    assert all(plan.block_at(m) is not Block.ASLEEP for m in range(17 * 60, 23 * 60 + 30))
    assert plan.block_at(23 * 60 + 45) is Block.ASLEEP and plan.block_at(3 * 60) is Block.ASLEEP
    # a card that states an early bedtime keeps it
    early = make_persona(101, "Arkon", job="baker", rhythm="early shifts, in bed by nine")
    assert day_plan(early, WED, event=DayEvent.NONE).block_at(22 * 60 + 30) is Block.ASLEEP
    # with the switch off the early plan is main's
    from leeroy_mind.life import day
    day.COHERENT = False
    assert day_plan(plain, WED, event=DayEvent.NONE).block_at(22 * 60 + 30) is Block.ASLEEP


def test_weekend_work_is_read_across_an_and():
    p = make_persona(101, "Arkon", job="carer", rhythm="works saturdays and sundays")
    assert day_plan(p, SAT, event=DayEvent.NONE).workday
    q = make_persona(101, "Arkon", job="carer", rhythm="raids saturdays and sundays")
    assert not day_plan(q, SAT, event=DayEvent.NONE).workday


def test_part_time_and_leave_days():
    part = make_persona(101, "Arkon", job="keeper", rhythm="half-day work")
    plan = day_plan(part, WED, event=DayEvent.NONE)
    assert plan.block_at(10 * 60) is Block.WORK and plan.block_at(15 * 60) is not Block.WORK
    leave = make_persona(101, "Arkon", job="paralegal on maternity leave", rhythm="")
    assert all(s.block is not Block.WORK for s in day_plan(leave, WED, event=DayEvent.NONE).spans)


def lp(block):
    return LifePresence(block, Where.FREE, Attention.FULL, DayEvent.NONE, False)


def test_where_she_said_she_is_is_read_from_her_newest_line():
    assert stated_where(["hi", "im at work, worst shift of my life"]) == "work"
    assert stated_where(["im at work", "ok im home now"]) == "home"
    assert stated_where(["im in bed already"]) == "bed"
    assert stated_where(["nice pull", "who has the key"]) == "" and stated_where([]) == ""


@pytest.mark.parametrize("line", [
    "she's at work", "maald is at work again", "im not at work today", "are you at work?",
    "you still in bed?", "i was at work all day", "ill be at work by nine", "going to bed soon",
    "he's in bed already", "when i get home ill log on", "if i was at home id be playing",
    "wish i was in bed", "my husband is at the gym", "not in bed yet", "i hate being at work",
    "work was awful", "dont tell me youre at work", "nobody is home",
    "we were at the pub last night", "tomorrow im at work early", "is anyone home", "nice pull"])
def test_a_place_she_never_claimed_for_herself_now_is_not_read(line):
    """Review of #370: someone else's place, a negation, a question, the past and the future
    were read as where she is (14 of 22 such lines)."""
    assert stated_where([line]) == ""


@pytest.mark.parametrize("line,kind", [
    ("im at work, worst shift of my life", "work"), ("im still in bed", "bed"),
    ("ok im home now", "home"), ("lol im on my break", "work"),
    ("im stuck at work till six", "work"), ("im at the gym", "out")])
def test_her_own_place_now_is_read(line, kind):
    """One reader: talk/sofar.py's her_said gives the words, stated_where names their kind."""
    assert stated_where([line]) == kind


def test_the_line_agrees_with_what_she_said():
    playing = playing_text("Zan", "him", raid=True, place="Naxxramas")
    assert playing == ("You're at your PC, playing: you're in Zan's raid with him right now, "
                       "in Naxxramas.")
    assert "group with him right now." in playing_text("Zan", "him")
    # she said she was at work and is now in his group: she has just logged on from there
    joined = settled_where(playing, "work", lp(Block.WORK), playing=True, them="him")
    assert joined.startswith("You told him a little while ago you were at work: you've just got "
                             "in from work and logged on. ") and joined.endswith(playing)
    # her plan moved on but she said she was at work: she still is
    free = where_text(lp(Block.FREE), "game")
    assert settled_where(free, "work", lp(Block.FREE), playing=False, them="him") == \
        STILL["work"].format(them="him")
    # agreement, or nothing said: the line as it was
    work = where_text(lp(Block.WORK), "game")
    assert settled_where(work, "work", lp(Block.WORK), playing=False, them="him") == work
    assert settled_where(free, "", lp(Block.FREE), playing=False, them="him") == free
    assert settled_where(free, "home", lp(Block.FREE), playing=False, them="him") == free


# -- the pipeline ----------------------------------------------------------------------------------

@pytest.fixture
def clock():
    return engine_clock()


def rig_of(store, clock, path, **kw):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    return make_rig(store, clock, path, availability=6, situation=True, **kw)


def said(r, text, *, guid, to_guid):
    ev = Chat(ts=0, channel="whisper", from_guid=guid, from_name="x", to_guid=to_guid, text=text,
              map=0, zone=10)
    lid = r.store.insert_chat_line(ev, session_id=None,
                                   origin="human" if guid == HUMAN else "mind")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def whisper_prompt(r, presence):
    q = said(r, "you free for a heroic?", guid=HUMAN, to_guid=102)
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", addressee=HUMAN, to_guid=HUMAN,
                trigger_line=q.id)
    meta = TurnMeta(r.engine._new_id(), life_presence=presence, answers_human=True)
    r.engine._draw_situation(turn, meta, r.engine.personas.get(102), r.clock.now())
    return r.sync(r.engine._compose(turn, meta, live=True)).messages[-1]["content"]


def group_up(r, *, raid=False, map_id=0):
    r.world.human_online = True
    r.world.group = Group(ts=0, leader_guid=HUMAN, raid=raid, members=(102,))
    if map_id:
        r.world._touch_member(102, map=map_id)


def test_a_whisper_keeps_her_at_work_then_logs_her_on_from_there(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path)
    try:
        free = LifePresence(Block.FREE, Where.FREE, Attention.HALF, DayEvent.NONE, False)
        said(r, "im at work, worst shift of my life honestly", guid=102, to_guid=HUMAN)
        user = whisper_prompt(r, free)
        assert "You're still at work, as you told him a little while ago." in user
        assert "free time" not in user
        # she joins his raid: never a silent flip to "playing"
        group_up(r, raid=True, map_id=533)
        user = whisper_prompt(r, free)
        assert ("You told him a little while ago you were at work: you've just got in from work "
                "and logged on. You're at your PC, playing: you're in Zan's raid with him right "
                "now, in Naxxramas.") in user
    finally:
        r.close()


def test_a_grouped_member_is_told_she_is_in_his_group(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path)
    try:
        work = LifePresence(Block.WORK, Where.AT_WORK, Attention.FULL, DayEvent.NONE, False)
        group_up(r)
        user = whisper_prompt(r, work)
        assert "You're at your PC, playing: you're in Zan's group with him right now." in user
        assert "at work" not in user
    finally:
        r.close()


def test_coherence_off_keeps_the_plain_line(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, coherence=False)
    try:
        free = LifePresence(Block.FREE, Where.FREE, Attention.HALF, DayEvent.NONE, False)
        said(r, "im at work, worst shift of my life honestly", guid=102, to_guid=HUMAN)
        assert "You're at home with some free time." in whisper_prompt(r, free)
        group_up(r)
        user = whisper_prompt(r, free)
        assert "You're at your PC, playing." in user and "raid with him" not in user
    finally:
        r.close()


def test_a_dm_keeps_where_she_said_she_is(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path)
    try:
        now = r.clock.now()
        mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human",
                          text="what are you up to", now=now)
        rows = (ChatLineRow(901, now - 400, "dm:102", 102, "x", HUMAN,
                            "im in bed already, long day", 0, 0, None, None, "mind"),)
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
        meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None), discord_chat=rows)
        r.engine._draw_situation(turn, meta, r.engine.personas.get(102), r.clock.now())
        user = r.sync(r.engine._compose(turn, meta, live=True)).messages[-1]["content"]
        if meta.life_presence.block is Block.ASLEEP:
            assert "still in bed" not in user  # her plan agrees: the line as it was
        else:
            assert "You're still in bed, as you told him a little while ago." in user
    finally:
        r.close()


def test_the_life_cli_reads_the_switch_as_the_mind_does(tmp_path):
    from leeroy_mind.life import cli, day
    (tmp_path / "mind.toml").write_text("[talk]\ncoherence = false\n", encoding="utf-8")
    cli._switch(tmp_path)
    assert day.COHERENT is False
    (tmp_path / "mind.toml").write_text("[talk]\ncoherence = true\n", encoding="utf-8")
    cli._switch(tmp_path)
    assert day.COHERENT is True
