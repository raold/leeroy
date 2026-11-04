"""Existing Speak/Played behavior with separate bounded mind timing state."""
import asyncio
import json

import pytest

from conftest import run
from engine_rig import engine_clock
from leeroy_mind.talk.timing import TurnTiming, voice_token
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice import wire
from leeroy_mind.voice.comms import AGE_OUT_S


@pytest.fixture
def clock():
    return engine_clock()


def observed(v):
    now, events = [100.0], []
    v.engine.timing = TurnTiming(timer=lambda: now[0], emit=events.append)
    v.seat(101, "guild_hall")
    v.him("guild_hall")
    return now, events


async def speak(v):
    task = asyncio.create_task(v.engine.speak(Turn(
        kind=TurnKind.CALLOUT, speaker=101, channel="guild", template="synthetic line")))
    await v.advance(1)
    assert await task == ["synthetic line"]
    return v.speaks()[-1]


def test_started_and_terminal_ack_have_separate_lifetimes_and_no_sound_claim(vrig):
    v = vrig
    now, events = observed(v)

    async def go():
        msg = await speak(v)
        before = wire.encode(msg, 1)
        assert not events and msg.id in v.comms._unanswered
        now[0] += 2
        v.comms.played(wire.Played(msg.id, "started"))
        assert msg.id not in v.comms._unanswered  # existing store lifetime is unchanged
        assert len(v.engine.timing._active) == 1
        v.comms.played(wire.Played(msg.id, "started"))
        v.comms.played(wire.Played("unknown private id", "started"))
        now[0] += 3
        v.comms.played(wire.Played(msg.id, "done"))
        v.comms.played(wire.Played(msg.id, "done"))
        assert wire.encode(msg, 1) == before
        assert len(events) == 2 and not v.engine.timing._active
        first, final = events
        assert first["phase"] == "first_ack" and final["outcome"] == "done"
        assert first["send_to_started_ack_ms"] == 2000
        assert final["send_to_terminal_ack_ms"] == 5000
        assert final["origin_to_terminal_ack_ms"] == 5000
        assert final["voice_token"] == voice_token(msg.id)
        assert final["llm_client_call_ms"] is None  # template
        assert final["first_nonzero_output_ms"] is None
        assert final["first_nonzero_output_scope"] == "not_observed"
        assert final["physical_audible_ms"] is None
        assert msg.id not in json.dumps(events)
        await v.engine.scheduler.close()
    run(go())


@pytest.mark.parametrize("started", [True, False])
def test_disconnect_releases_timing_without_rewriting_voice_store(vrig, started):
    v = vrig
    now, events = observed(v)

    async def go():
        msg = await speak(v)
        if started:
            v.comms.played(wire.Played(msg.id, "started"))
        before = [tuple(r) for r in v.rows("SELECT * FROM voice_line")]
        now[0] += 4
        v.comms.disconnected()
        assert [tuple(r) for r in v.rows("SELECT * FROM voice_line")] == before
        assert events[-1]["outcome"] == "incomplete"
        assert events[-1]["reason"] == "disconnected"
        count = len(events)
        v.comms.played(wire.Played(msg.id, "done"))
        assert len(events) == count and not v.engine.timing._active
        await v.engine.scheduler.close()
    run(go())


def test_no_ack_policy_uses_wall_clock_timing_uses_monotonic(vrig):
    v = vrig
    now, events = observed(v)

    async def go():
        msg = await speak(v)
        now[0] += 5
        await v.advance(msg.expires_ms / 1000 + AGE_OUT_S - v.clock.now())
        v.comms.age_out()
        assert events[-1]["reason"] == "no_ack"
        assert events[-1]["elapsed_ms"] == 5000
        assert events[-1]["send_to_started_ack_ms"] is None
        row = v.rows("SELECT state, reason FROM voice_line WHERE speak_id = ?", (msg.id,))[0]
        assert tuple(row) == ("dropped", "no_ack")
        await v.engine.scheduler.close()
    run(go())


def test_failed_timing_sink_does_not_change_voice_delivery_or_ack(vrig):
    v = vrig
    observed(v)

    def broken(_):
        raise RuntimeError("private timing sink")

    v.engine.timing._emit = broken

    async def go():
        msg = await speak(v)
        v.comms.played(wire.Played(msg.id, "started"))
        v.comms.played(wire.Played(msg.id, "done"))
        assert not v.engine.timing._active
        assert v.rows("SELECT state FROM voice_line WHERE speak_id = ?", (msg.id,))[0][0] == "done"
        assert not v.sent.says()
        await v.engine.scheduler.close()
    run(go())
