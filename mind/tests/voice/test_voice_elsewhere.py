"""Elsewhere conversations use real routing and synthetic guild members."""
import asyncio
import random

import pytest

from conftest import run
from engine_rig import engine_clock
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice.elsewhere import ElsewhereTalk


@pytest.fixture
def clock():
    return engine_clock()


def setup(v, *, limit=6, members=(103, 104, 105)):
    v.him("guild_hall")
    v.ext.comms = v.comms
    v.ext.on_comms = set(members)
    for g in members:
        v.seat(g, "smoke_room")
    replies = iter(("the kettle is taking forever today", "mine whistles like an angry bird",
                    "coffee sounds better honestly", "rain is hammering against my window",
                    "my cat refuses to leave this chair", "i forgot my toast again",
                    "those headphones look comfortable", "someone remind me to stretch",
                    "dinner should be ready soon", "the kitchen smells fantastic"))
    v.says(lambda model, messages: next(replies, "[SILENT]"))
    return ElsewhereTalk(v.ext, lines_per_10min=limit, rng=random.Random(7))


async def tick(v, policy):
    task = asyncio.create_task(policy.tick(v.clock.now()))
    await v.advance(55)
    assert task.done()
    return task.result()


def test_scene_is_unheard_background_and_answers_previous_line(vrig):
    v, turns, priorities = vrig, [], []
    policy = setup(v)
    speak, submit = v.engine.speak, v.engine.scheduler.submit

    async def capture(turn, **kw):
        turns.append((turn, kw))
        return await speak(turn, **kw)

    async def queued(priority, job, **kw):
        priorities.append(priority)
        return await submit(priority, job, **kw)

    v.engine.speak, v.engine.scheduler.submit = capture, queued
    run(tick(v, policy))
    rows = v.lines()
    assert 2 <= len(rows) <= 3
    assert {r["channel"] for r in rows} == {"voice:smoke_room"}
    assert not v.sent.says() and not v.speaks()
    assert all(r["state"] == "unheard" for r in v.rows("SELECT state FROM voice_line"))
    assert priorities and set(priorities) == {Priority.BACKGROUND}
    assert turns[0][0].kind == TurnKind.OPEN
    for index, (turn, kw) in enumerate(turns):
        assert kw["elsewhere"] and kw["guards"] is policy.guards
        assert turn.max_lines == 1 and turn.priority == Priority.BACKGROUND
        if index:
            assert turn.kind == TurnKind.CONTINUE
            assert turn.addressee == rows[index - 1]["from_guid"]
            assert turn.trigger_line == rows[index - 1]["id"]
    assert v.comms.audience("voice:smoke_room") == {103, 104, 105}


def test_scene_does_not_spend_human_guards_or_quiet_clock(vrig):
    v = vrig
    policy = setup(v)
    before = v.engine._last_talk_at
    run(tick(v, policy))
    assert v.lines() and policy.guards._window
    assert not v.engine.guards._window
    assert v.engine._last_talk_at == before


@pytest.mark.parametrize("room", ["guild_hall", "private1", "private2", "private3", "afk"])
def test_excluded_rooms(vrig, room):
    policy = setup(vrig)
    for guid in vrig.ext.on_comms:
        vrig.seat(guid, room)
    run(tick(vrig, policy))
    assert not vrig.lines() and not vrig.llm.calls


@pytest.mark.parametrize("condition", ["offline", "down", "not_on_comms", "alone", "afk", "disabled"])
def test_ineligible_scene(vrig, condition):
    v = vrig
    policy = setup(v, limit=0 if condition == "disabled" else 6)
    if condition == "offline":
        v.world.human_online = False
    elif condition == "down":
        v.fake.greeted = False
    elif condition == "not_on_comms":
        v.ext.on_comms.clear()
    elif condition == "alone":
        v.world.online = {104}
    elif condition == "afk":
        v.world.afk = set(v.ext.on_comms)
    run(tick(v, policy))
    assert not v.lines() and not v.llm.calls


@pytest.mark.parametrize("priority", [Priority.REPLY_HUMAN, Priority.RAID_LEAD, Priority.SOCIAL])
def test_pending_human_work_skips_scene(vrig, priority, monkeypatch):
    policy = setup(vrig)
    monkeypatch.setattr(vrig.engine.scheduler, "pending", lambda: {priority: 1})
    run(tick(vrig, policy))
    assert not vrig.lines()


def test_rolling_cap_and_interval(vrig):
    v = vrig
    policy = setup(v, limit=4)

    async def go():
        start = v.clock.now()
        for _ in range(8):
            await tick(v, policy)
            await v.advance(5)
        assert len(v.lines()) == 4
        await v.advance(180)
        await tick(v, policy)
        assert len(v.lines()) > 4
        times = [r["ts"] for r in v.lines()]
        assert all(sum(t <= other < t + 600 for other in times) <= 4 for t in times)
        assert times[3] - start >= 60
    run(go())


def test_stops_at_first_silent_turn(vrig):
    policy = setup(vrig)
    vrig.says("[SILENT]")
    run(tick(vrig, policy))
    assert not vrig.lines()
    assert len(vrig.rows("SELECT * FROM utterance")) == 1


@pytest.mark.parametrize("change", ["down", "speaker_moves", "human_joins"])
def test_scene_drops_if_room_changes_during_generation(vrig, change):
    v = vrig
    policy = setup(v)
    speak = v.engine.speak

    async def capture(turn, **kw):
        def changed(model, messages):
            if change == "down":
                v.fake.greeted = False
            elif change == "speaker_moves":
                v.seat(turn.speaker, "guild_hall")
            else:
                v.him("smoke_room")
            return "the kettle finally boiled"
        v.says(changed)
        return await speak(turn, **kw)

    v.engine.speak = capture
    run(tick(v, policy))
    assert not v.lines() and not v.sent.says() and not v.speaks()
    assert v.rows("SELECT dropped FROM utterance")[-1]["dropped"] == "elsewhere_changed"


def test_elsewhere_emotes_never_reach_the_game(vrig):
    v = vrig
    setup(v)

    async def go():
        task = asyncio.create_task(v.engine.speak(
            Turn(TurnKind.OPEN, 104, "voice:smoke_room", template="/laugh"),
            elsewhere=True))
        await v.advance(30)
        assert task.done()
    run(go())
    assert not v.sent.emotes()
