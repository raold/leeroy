"""Hot-mic policy: rate, persistent nightly limit and the real voice route."""
import asyncio
from datetime import datetime, timezone
import random

import pytest

from conftest import run
from engine_rig import engine_clock
from leeroy_mind.clock import night_of
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.voice.hotmic import CHECK_S, HOT_MIC, NIGHT_KEY, HotMic


@pytest.fixture
def clock():
    return engine_clock()


class Always(random.Random):
    def random(self):
        return 0.0


def seat(v):
    v.ext.comms = v.comms
    v.ext.on_comms = {101, 102, 103, 104, 105}
    v.world.human_online = True
    v.him("guild_hall")
    for guid in v.ext.on_comms:
        v.seat(guid, "guild_hall")


def recording(v, monkeypatch):
    calls = []
    async def speak(turn, **kwargs):
        calls.append((night_of(v.clock.local()), turn, kwargs))
        return ["ok"]
    monkeypatch.setattr(v.engine, "speak", speak)
    return calls


def test_rate(vrig, monkeypatch):
    v = vrig
    seat(v)
    calls = recording(v, monkeypatch)
    policy = HotMic(v.ext, per_month=1.0, rng=random.Random(44))
    assert policy.chance == 1 / (30 * 4 * 60)
    start = datetime(2026, 1, 1, 20, tzinfo=timezone.utc).timestamp()
    async def simulate():
        for night in range(30):
            for minute in range(4 * 60):
                v.clock._t = start + night * 86400 + minute * 60
                await policy.tick(v.clock.now())
    run(simulate())
    assert 0 <= len(calls) <= 3
    assert len({night for night, _, _ in calls}) == len(calls)
    calls.clear()
    policy = HotMic(v.ext, per_month=0, rng=Always())
    run(simulate())
    assert not calls


def test_check_cadence_does_not_redraw_between_minutes(vrig, monkeypatch):
    v = vrig
    seat(v)
    recording(v, monkeypatch)
    class Never(random.Random):
        draws = 0
        def random(self):
            self.draws += 1
            return 1.0
    rng = Never()
    policy = HotMic(v.ext, per_month=1, rng=rng)
    async def test():
        for offset in (0, 1, 30, 59, 60, 61):
            await policy.tick(v.clock.now() + offset)
    run(test())
    assert rng.draws == 2


def test_nightly_limit_survives_policy_reconstruction_and_relogin(vrig, monkeypatch):
    v = vrig
    seat(v)
    calls = recording(v, monkeypatch)
    async def test():
        await HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now())
        v.clock._t += 3600
        v.world.human_online = False
        await HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now())
        v.world.human_online = True
        await HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now())
        assert len(calls) == 1
        v.clock._t += 86400
        await HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now())
    run(test())
    assert len(calls) == 2


@pytest.mark.parametrize("phase", [RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF])
def test_never_in_a_fight(vrig, monkeypatch, phase):
    v = vrig
    seat(v)
    calls = recording(v, monkeypatch)
    monkeypatch.setattr(v.raid, "phase", lambda now: phase)
    run(HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now()))
    assert not calls and v.store.get_meta(NIGHT_KEY) is None


@pytest.mark.parametrize("blocked", ["offline", "service_down", "private", "afk_room", "no_same_room", "off_comms", "member_offline", "member_afk"])
def test_only_in_his_room_on_comms(vrig, monkeypatch, blocked):
    v = vrig
    seat(v)
    v.ext.on_comms = {101}
    calls = recording(v, monkeypatch)
    if blocked == "offline": v.world.human_online = False
    elif blocked == "service_down": v.fake.greeted = False
    elif blocked == "private": v.him("private1"); v.seat(101, "private1")
    elif blocked == "afk_room": v.him("afk"); v.seat(101, "afk")
    elif blocked == "no_same_room": v.seat(101, "lore")
    elif blocked == "off_comms": v.ext.on_comms.clear()
    elif blocked == "member_offline": v.world.online.discard(101)
    elif blocked == "member_afk": v.world.afk.add(101)
    run(HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now()))
    assert not calls


def test_one_check_per_minute_and_one_inflight(vrig, monkeypatch):
    v = vrig
    seat(v)
    calls = []
    async def test():
        gate = asyncio.Event()
        async def speak(turn, **kwargs):
            calls.append(turn)
            await gate.wait()
            return []
        monkeypatch.setattr(v.engine, "speak", speak)
        policy = HotMic(v.ext, per_month=1, rng=Always())
        task = asyncio.create_task(policy.tick(v.clock.now()))
        await asyncio.sleep(0)
        await policy.tick(v.clock.now() + CHECK_S)
        gate.set()
        await task
        await policy.tick(v.clock.now() + CHECK_S * 2)
    run(test())
    assert len(calls) == 1


def test_marked_hotmic(vrig):
    v = vrig
    seat(v)
    v.ext.on_comms = {101}
    v.says("I'll put the kettle on.")
    async def test():
        await v.session()
        before = v.clock.now()
        await HotMic(v.ext, per_month=1, rng=Always()).tick(before)
        assert len(v.speaks()) == 1
        message = v.speaks()[0]
        assert (message.kind, message.priority, message.channel) == ("hotmic", "p4", "guild_hall")
        assert message.expires_ms == int(v.clock.now() * 1000) + 15000
        assert not v.sent.says()
        prompt = "\n".join(v.prompts())
        assert HOT_MIC.format(household=v.engine.personas.get(101).real_life.household) in prompt
    run(test())


@pytest.mark.parametrize("change", ["down", "human_moves", "speaker_moves", "logout", "combat"])
def test_household_line_is_dropped_when_context_changes_during_generation(vrig, monkeypatch, change):
    v = vrig
    seat(v)
    v.ext.on_comms = {101}

    def answer(model, messages):
        if change == "down":
            v.fake.greeted = False
        elif change == "human_moves":
            v.him("lore")
        elif change == "speaker_moves":
            v.seat(101, "private1")
        elif change == "logout":
            v.world.human_online = False
        else:
            monkeypatch.setattr(v.raid, "phase", lambda now: RaidPhase.COMBAT)
        return "I'll put the kettle on."

    v.says(answer)
    async def check():
        task = asyncio.create_task(HotMic(v.ext, per_month=1, rng=Always()).tick(v.clock.now()))
        await v.advance(40)
        assert task.done()
        task.result()
    run(check())
    assert not v.speaks() and not v.sent.says() and not v.lines()
    assert v.rows("SELECT dropped FROM utterance")[-1]["dropped"] == "hotmic_changed"
