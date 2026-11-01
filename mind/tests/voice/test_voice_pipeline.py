"""Lines into rooms (Task 14): a member's turn is spoken in their room, typed, or both ways
the same as in phase 1 when voice is off or down. The invented cast only."""
import asyncio
import json
import logging

import pytest

from conftest import HUMAN, TEST_CAST, mem, run
from engine_rig import REAL, RAID, engine_clock, make_rig, numbered, save_member
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.memory.threads import ThreadTracker
from leeroy_mind.personas.schema import Bit
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.protocol import Boss, Cast, Chat, Group, Logout
from leeroy_mind.storage.rows import ChatLineRow, Relationship
from leeroy_mind.talk import engine as engine_module
from leeroy_mind.talk import pipeline as pipeline_module
from leeroy_mind.talk.budget import BurstPlan
from leeroy_mind.talk.filters import strip_copied
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import chat_line_text
from leeroy_mind.talk.romance import AsidePlan
from leeroy_mind.talk.states import Altered
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice import wire
from leeroy_mind.voice.client import VoiceDown
from leeroy_mind.voice.comms import AGE_OUT_S, SPOKEN_HINT
from voice.conftest import status

SECRET = "the line itself"


@pytest.fixture
def clock():
    return engine_clock()


async def said(v, turn: Turn, seconds: float = 40, **keywords) -> list[str]:
    task = asyncio.create_task(v.engine.speak(turn, **keywords))
    await v.advance(seconds)
    assert task.done()
    return task.result()


def opener(speaker: int = 101, channel: str = "guild", **kw) -> Turn:
    return Turn(kind=kw.pop("kind", TurnKind.OPEN), speaker=speaker, channel=channel, **kw)


def hall(v, *guids) -> None:
    for g in guids:
        v.seat(g, "guild_hall")
    v.him("guild_hall")


def voice_lines(v) -> list:
    return v.rows("SELECT * FROM voice_line ORDER BY ts, speak_id")


# -- templates (C1, Revision 3.2) ------------------------------------------------------------

def test_kill_cheer_is_spoken(vrig):
    """The whole kill: the lead's line from the model, the cheers from templates, all spoken."""
    v = vrig

    async def go():
        await v.session()
        await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
        for g in (101, 102, 103, 104, 105):
            v.seat(g, "raid")
        v.him("raid")
        await v.event(Boss(0, 631, 5, 0, "", "engaged", RAID))
        await v.event(Boss(0, 631, 5, 0, "", "killed", RAID))
        await v.advance(30)
        await v.engine._cancel_all()
    run(go())
    assert not v.sent.says()
    cheers = [s for s in v.speaks() if s.kind == "gz"]
    assert cheers and all(s.template and s.channel == "raid" for s in cheers)
    assert all(not s.template for s in v.speaks() if s.kind == "kill")
    assert {r["channel"] for r in v.lines()} == {"voice:raid"}


def test_say_as_lead_is_p0(vrig):
    v = vrig

    async def go():
        await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
        v.seat(102, "raid")
        v.him("raid")
        return await v.engine.say_as(102, "raid", "Pulling in three.", kind=TurnKind.CALLOUT)
    assert run(go()) is True
    (s,) = v.speaks()
    assert (s.priority, s.text, s.template, s.channel) == ("p0", "Pulling in three.", True, "raid")
    assert s.expires_ms == int(v.clock.now() * 1000) + 3000
    assert not v.sent.says() and not v.chat_calls()


def test_template_off_comms_is_typed(vrig):
    v = vrig

    async def go():
        await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
        v.seat(101, "raid")
        v.him("raid")
        await said(v, opener(103, "raid", kind=TurnKind.GZ, template="gg"))
    run(go())
    assert [(s.channel, s.guid) for s in v.sent.says()] == [("raid", 103)]
    assert not v.speaks()


def test_an_lm_turn_is_no_template(vrig):
    v = vrig
    hall(v, 101)
    run(said(v, opener()))
    (s,) = v.speaks()
    assert s.template is False


def test_deliver_refuses_voice_channels(vrig, caplog):
    """C1: with no comms to place it, a voice channel is refused, never sent to the bridge."""
    v = vrig
    v.engine.comms = None
    with caplog.at_level(logging.INFO):
        out = run(said(v, opener(channel="voice:raid", kind=TurnKind.CALLOUT, template="hello")))
    assert out == [] and not v.sent.says()
    assert v.utterances()[-1]["dropped"] == "voice_channel"
    assert "say_refused_voice_channel" in caplog.text


# -- who speaks, in which room -----------------------------------------------------------------

def test_burst_spoken_in_his_room(vrig):
    v = vrig
    v.says(numbered())
    v.world.online &= {101, 102}
    hall(v, 101, 102)

    async def go():
        await v.session()
        task = asyncio.create_task(v.engine._burst(BurstPlan(2, 2)))
        await v.advance(40)
        await task
    run(go())
    assert not v.sent.says()
    assert len(v.speaks()) == 2 and {s.channel for s in v.speaks()} == {"guild_hall"}
    rows = v.lines()
    assert {r["channel"] for r in rows} == {"voice:guild_hall"}
    assert all(json.loads(r["audience"]) == [101, 102] for r in rows)
    for s in v.speaks():
        wire.encode(s, 1)  # the real client's encoder takes every line


def test_off_comms_member_types(vrig):
    v = vrig
    hall(v, 101)
    run(said(v, opener(103)))
    assert [(s.channel, s.guid) for s in v.sent.says()] == [("guild", 103)]
    assert not v.speaks()


def test_burst_stays_in_the_room(vrig, monkeypatch):
    v = vrig
    v.says(numbered())
    hall(v, 101, 102)
    v.world.online &= {101, 102, 103}
    pools, real = [], engine_module.pick_speakers

    def first_is_arkon(weights, n, rng, reasons=None):
        pools.append(set(weights))
        return [101] if len(pools) == 1 else real(weights, n, rng, reasons)
    monkeypatch.setattr(engine_module, "pick_speakers", first_is_arkon)

    async def go():
        await v.session()
        task = asyncio.create_task(v.engine._burst(BurstPlan(2, 2)))
        await v.advance(40)
        await task
    run(go())
    assert pools[1] == {102}  # 103, off comms, is in the opener's pool, never the room's
    assert [(s.guid, s.channel) for s in v.speaks()] == [(101, "guild_hall"), (102, "guild_hall")]
    assert not v.sent.says()


def bit() -> Bit:
    return Bit(id="feasts", instruction="bring up the fish feasts nobody eats",
               min_days_between=3.0, voice_bit=True, contexts=("any",), lines=1, dates=())


def test_bit_spoken_or_typed(vrig):
    v = vrig
    v.says(numbered("the feasts again, take {n}"))
    hall(v, 101)

    def turn(g):
        return Turn(kind=TurnKind.BIT, speaker=g, channel="guild", bit=bit(),
                    instruction="bring up the fish feasts nobody eats", max_lines=1)

    async def go():
        await said(v, turn(101))
        await said(v, turn(103))
    run(go())
    assert [s.guid for s in v.speaks()] == [101]
    assert [s.guid for s in v.sent.says()] == [103]


def test_the_clean_line(vrig):
    """Settled item 2: a voiced turn skips the typing, so the voice gets the model's words."""
    v = vrig
    save_member(v.store, 101, typo_rate=0.15, emoticon_rate=0.9)
    v.engine.personas.invalidate()
    v.says("Okay, that pull was ALL you.")
    hall(v, 101)
    out = run(said(v, opener()))
    assert out == ["Okay, that pull was ALL you."]
    assert [s.text for s in v.speaks()] == out and not v.sent.says()
    assert [r["text"] for r in v.lines()] == out
    # her own line for the next prompts and the repeat check, as the typed path keeps it
    assert list(v.engine._own[101]) == [("voice:guild_hall", 0, out[0])]


def test_voice_lines_take_no_typing_time(vrig):
    v = vrig
    v.says("Okay, that pull was all you, and I mean every single bit of it.")
    hall(v, 101)

    async def go():
        task = asyncio.create_task(v.engine.speak(opener()))
        await v.advance(0.5, step=0.1)
        assert task.done() and v.speaks()
    run(go())


def test_a_turns_lines_are_one_speak(vrig):
    """Settled item 8."""
    v = vrig
    v.says("first we pull the left pack\nthen we rest up and go right")
    hall(v, 101)
    run(said(v, Turn(kind=TurnKind.BIT, speaker=101, channel="guild", bit=bit(), max_lines=2,
                     instruction="bring up the fish feasts nobody eats")))
    (s,) = v.speaks()
    assert s.text == "first we pull the left pack then we rest up and go right"


def test_a_voiced_lines_emote_goes_to_the_game(vrig):
    v = vrig
    v.says("/wave hello all")
    hall(v, 101)
    assert run(said(v, opener())) == ["hello all"]
    assert [s.text for s in v.speaks()] == ["hello all"]
    assert [e.emote for e in v.sent.emotes()] == ["wave"]


def test_only_an_emote_speaks_nothing(vrig):
    v = vrig
    v.says("/wave")
    hall(v, 101)
    assert run(said(v, opener())) == []
    assert not v.speaks() and not v.sent.says()
    assert [e.emote for e in v.sent.emotes()] == ["wave"]
    assert v.utterances()[-1]["dropped"] == "emote_only"


def test_spoken_hint(vrig):
    """M6: last in the model's message, and in its retry's."""
    v = vrig
    long = " ".join(["word"] * 80)
    replies = iter([long, "fine, short then"])
    v.says(lambda model, messages: next(replies))
    hall(v, 101)
    run(said(v, opener()))
    calls = [c["messages"][-1]["content"] for c in v.chat_calls()]
    assert len(calls) == 2
    assert all(c.endswith(SPOKEN_HINT) for c in calls)
    assert "Shorter:" in calls[1]


def test_no_spoken_hint_on_a_typed_turn(vrig):
    v = vrig
    hall(v, 101)
    run(said(v, opener(103)))
    assert not any(SPOKEN_HINT in p for p in v.prompts())


def test_romance_wording_on_vent(vrig):
    """M6."""
    v = vrig
    save_member(v.store, 102, player_gender="female", stance="lust")
    v.engine.personas.invalidate()
    v.says("well hello there Zan")
    hall(v, 102)
    run(said(v, Turn(kind=TurnKind.FLIRT, speaker=102, channel="voice:guild_hall",
                     addressee=HUMAN)))
    (prompt,) = v.prompts()
    assert "The people in the room can hear you, so play to the room." in prompt
    assert "The everyone around" not in prompt
    assert v.speaks()


def test_reply_to_typed_is_typed(vrig):
    v = vrig
    v.says("all good here")
    hall(v, 101)
    v.world.online &= {101}

    async def go():
        await v.human("arkon how is it going")
        await v.advance(45)
    run(go())
    assert [(s.guid, s.channel) for s in v.sent.says()] == [(101, "guild")]
    assert not v.speaks()


def test_reply_to_spoken_is_spoken(vrig):
    v = vrig
    v.says("all good here")
    hall(v, 101)
    v.world.online &= {101}

    async def go():
        await v.human("arkon how is it going", channel="voice:guild_hall")
        await v.advance(45)
    run(go())
    (s,) = v.speaks()
    assert (s.guid, s.channel, s.priority, s.to_human) == (101, "guild_hall", "p3", True)
    assert not v.sent.says()


def test_his_spoken_line_is_answered_only_from_his_room(vrig):
    v = vrig
    v.says("all good here")
    hall(v, 101)
    v.seat(102, "raid")

    async def go():
        await v.human("everyone how is it going", channel="voice:guild_hall")
        await v.advance(45)
    run(go())
    assert {s.guid for s in v.speaks()} <= {101} and not v.sent.says()


def test_a_member_in_another_room_still_answers_a_typed_line(vrig):
    """His typed guild line reads in every room; the answer is typed."""
    v = vrig
    v.says("all good here")
    hall(v, 101)
    v.seat(102, "smoke_room")
    v.world.online &= {102}

    async def go():
        await v.human("belra how is it going")
        await v.advance(45)
    run(go())
    assert [(s.guid, s.channel) for s in v.sent.says()] == [(102, "guild")]


def test_private_room_is_a_whisper(vrig):
    v = vrig
    v.says("glad we can talk here")
    v.seat(102, "private1")
    v.him("private1")
    run(said(v, opener(102)))
    (s,) = v.speaks()
    assert (s.channel, s.priority, s.to_human) == ("private1", "p3", True)
    assert s.expires_ms == int(v.lines()[0]["ts"] * 1000) + 20000
    (row,) = v.lines()
    assert (row["channel"], row["to_guid"]) == ("whisper", HUMAN)
    assert voice_lines(v)[0]["room"] == "private1"


def test_a_private_room_line_stays_out_of_every_other_prompt(vrig):
    """Settled item 5: every whisper rule applies, the chat window and recall alike."""
    v = vrig
    v.says("something only between us")
    v.seat(102, "private1")
    v.him("private1")
    v.seat(101, "guild_hall")

    async def go():
        await v.session()
        await said(v, opener(102))
        v.him("guild_hall")
        v.says("fair enough")
        await said(v, opener(101))
    run(go())
    assert not any("something only between us" in p for p in v.prompts()[1:])
    assert json.loads(v.lines()[0]["audience"]) == [102]


def test_a_spoken_private_line_is_answered_in_the_room(vrig):
    """His line in a private room arrives as a whisper (contract note 1)."""
    v = vrig
    v.says("it was a long day honestly")
    v.seat(102, "private1")
    v.him("private1")

    async def go():
        await v.human("how was your day", channel="whisper", to_guid=102)
        await v.advance(45)
    run(go())
    (s,) = v.speaks()
    assert (s.guid, s.channel) == (102, "private1") and not v.sent.says()
    assert v.lines()[0]["channel"] == "whisper"


def test_voice_whisper(vrig):
    v = vrig
    v.says("psst you around later")
    v.seat(102, "raid")
    v.him("guild_hall")
    run(said(v, Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                     addressee=HUMAN)))
    (s,) = v.speaks()
    assert (s.channel, s.priority) == ("whisper", "p3")
    assert s.expires_ms == int(v.lines()[0]["ts"] * 1000) + 30000
    assert (v.lines()[0]["channel"], v.lines()[0]["to_guid"]) == ("whisper", HUMAN)
    assert voice_lines(v)[0]["room"] == "whisper"


def test_typed_whisper_reply(vrig):
    v = vrig
    v.says("long day here honestly")
    hall(v, 102)

    async def go():
        for _ in range(6):  # a whispered line is answered as its reply chance says
            await v.human("how was your day", channel="whisper", to_guid=102)
            await v.advance(45)
    run(go())
    assert v.sent.says() and all(s.channel == "whisper" for s in v.sent.says())
    assert not v.speaks()


def test_other_room_unheard(vrig):
    v = vrig
    hall(v)
    v.seat(104, "smoke_room")
    v.seat(105, "smoke_room")
    out = run(said(v, opener(104, "voice:smoke_room"), why="elsewhere:open", elsewhere=True))
    assert out and not v.speaks() and not v.sent.says()
    (row,) = voice_lines(v)
    assert (row["state"], row["room"]) == ("unheard", "smoke_room")
    assert json.loads(v.lines()[0]["audience"]) == [104, 105]
    assert v.lines()[0]["channel"] == "voice:smoke_room"


def test_other_rooms_not_in_main_bursts(vrig):
    v = vrig
    v.says(numbered())
    hall(v, 101, 102)
    v.seat(104, "smoke_room")
    v.seat(105, "smoke_room")
    assert v.engine._candidates() == [101, 102, 103]
    assert v.engine._candidates(hearable=False) == [101, 102, 103, 104, 105]

    async def go():
        await v.session()
        for _ in range(8):
            task = asyncio.create_task(v.engine._burst(BurstPlan(3, 3)))
            await v.advance(120, step=2)
            await task
    run(go())
    assert not {u["guid"] for u in v.utterances()} & {104, 105}


def test_a_whisper_reaches_him_from_any_room(vrig):
    v = vrig
    hall(v)
    v.seat(102, "smoke_room")
    seen = []

    def spy(now, trigger, cands):
        seen.append(set(cands))
    v.engine.whispers.maybe = spy
    v.engine._maybe_whisper("idle", v.clock.now())
    assert 102 in seen[0]


def test_candidates_are_as_ever_without_comms(vrig):
    v = vrig
    v.seat(104, "smoke_room")
    v.engine.comms = None
    assert v.engine._candidates() == [101, 102, 103, 104, 105]


def test_attitude_and_state(vrig):
    v = vrig
    save_member(v.store, 102, player_gender="female", stance="lust")
    v.engine.personas.invalidate()
    v.says(numbered("well hello there {n}"))
    hall(v, 101, 102)
    v.engine._altered[101] = Altered.TIPSY

    async def go():
        await said(v, Turn(kind=TurnKind.FLIRT, speaker=102, channel="voice:guild_hall",
                           addressee=HUMAN), seconds=65)  # her own interval
        await said(v, Turn(kind=TurnKind.CONTINUE, speaker=102, channel="voice:guild_hall",
                           addressee=101))
        await said(v, opener(101))
    run(go())
    first, second, man = v.speaks()
    assert (first.attitude, second.attitude, man.attitude) == ("lust", "none", "none")
    assert (man.state, first.state) == ("tipsy", "sober")


# -- the defuse and `after` (I7) ---------------------------------------------------------------

def jealous(v, aside=103, defuse=105) -> None:
    """The men's plan as JealousyAsides gives it: a sour aside, then a good sport's defuse."""
    v.engine.asides.on_trigger = lambda *a: [
        AsidePlan(aside, "Grumble about it.", n=1),
        AsidePlan(defuse, "Lighten things up.", counted=False)]
    v.engine._jealous("public_flirt", v.engine.personas.get(102), "voice:guild_hall",
                      v.clock.now())


def test_defuse_after(vrig):
    v = vrig
    v.says(numbered("oh come on {n}"))
    hall(v, 102, 103, 105)

    async def go():
        jealous(v)
        await v.advance(60)
    run(go())
    first, second = v.speaks()
    assert first.after == "" and second.after == first.id and first.kind == "aside"


def test_defuse_after_a_typed_aside_has_no_after(vrig):
    """The aside by a man off comms: spoken defuse, typed aside, no id to wait for."""
    v = vrig
    v.says(numbered("oh come on {n}"))
    hall(v, 102, 105)

    async def go():
        v.engine.asides.on_trigger = lambda *a: [
            AsidePlan(103, "Grumble about it.", n=1),
            AsidePlan(105, "Lighten things up.", counted=False)]
        v.engine._jealous("public_flirt", v.engine.personas.get(102), "guild", v.clock.now())
        await v.advance(90)
    run(go())
    assert [s.guid for s in v.sent.says()] == [103]
    (s,) = v.speaks()
    assert (s.guid, s.after) == (105, "")


def test_defuse_dropped_when_its_aside_said_nothing(vrig):
    v = vrig
    v.says("(nothing)")
    hall(v, 102, 103, 105)

    async def go():
        jealous(v)
        await v.advance(60)
    run(go())
    assert not v.speaks() and not v.sent.says()
    assert [u["dropped"] for u in v.utterances()] == ["silent", "after_dropped"]


def test_a_dropped_defuse_stays_as_today_with_voice_off(vrig):
    """Voice off: the defuse goes out as it does in phase 1, whatever the aside did."""
    v = vrig
    v.engine.comms = None
    v.says(lambda model, messages: ("(nothing)" if "Grumble" in messages[-1]["content"]
                                    else "easy now lads"))

    async def go():
        v.engine.asides.on_trigger = lambda *a: [
            AsidePlan(103, "Grumble about it.", n=1),
            AsidePlan(105, "Lighten things up.", counted=False)]
        v.engine._jealous("public_flirt", v.engine.personas.get(102), "guild", v.clock.now())
        await v.advance(60)
    run(go())
    assert [s.guid for s in v.sent.says()] == [105]


# -- the quiet clock (I8, N3) ------------------------------------------------------------------

def test_unheard_lines_leave_the_quiet_clock(vrig):
    v = vrig
    hall(v)
    v.seat(104, "smoke_room")
    v.seat(105, "smoke_room")

    async def go():
        talk, answered = v.engine._last_talk_at, dict(v.engine._answered_at)
        out = await said(v, opener(104, "voice:smoke_room", addressee=HUMAN), elsewhere=True)
        assert out
        assert v.engine._last_talk_at == talk and v.engine._answered_at == answered
    run(go())


def test_typed_lines_keep_the_quiet_clock(vrig):
    """N3, NP1."""
    v = vrig
    v.says(numbered("all good here {n}"))
    hall(v, 101, 102)

    async def go():
        before = v.engine._last_talk_at
        await said(v, opener(103))
        assert v.engine._last_talk_at > before
        v.world.online &= {101, 102}
        before = v.engine._last_talk_at
        await v.human("arkon how is it going")
        await v.advance(45)
        assert v.engine._last_talk_at > before and 101 in v.engine._answered_at
        before, v.engine._answered_at = v.engine._last_talk_at, {}
        task = asyncio.create_task(v.engine._speak(
            Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="whisper", to_guid=HUMAN,
                 addressee=HUMAN), TurnMeta("w1", answers_human=True, whisper_reply=True)))
        await v.advance(40)
        await task
        assert [s.channel for s in v.sent.says()][-1] == "whisper"
        assert 102 in v.engine._answered_at and v.engine._last_talk_at == before
    run(go())


def test_a_line_in_his_room_keeps_the_quiet_clock(vrig):
    v = vrig
    hall(v, 101)

    async def go():
        before = v.engine._last_talk_at
        await said(v, opener(addressee=HUMAN))
        assert v.engine._last_talk_at > before and 101 in v.engine._answered_at
    run(go())


# -- voice down: phase 1, at once --------------------------------------------------------------

def test_voice_down_types(vrig):
    v = vrig
    hall(v, 101)
    v.fake.greeted = False
    run(said(v, opener()))
    assert [(s.channel, s.guid) for s in v.sent.says()] == [("guild", 101)]
    assert not v.speaks() and not voice_lines(v)


def test_voice_down_adds_no_delay(vrig):
    """A turn typed with comms down takes the same time as one with no comms at all."""
    v = vrig
    hall(v, 101)
    v.fake.greeted = False
    other = make_rig(v.store, v.clock, v.rig.engine.data_dir)
    other.engine.rng.seed(5)
    v.engine.rng.seed(5)

    async def go():
        now = v.clock.now()
        await said(v, opener())
        await other.advance(0)
        task = asyncio.create_task(other.engine.speak(opener()))
        await other.advance(40)
        await task
        return now
    now = run(go())
    typed = v.sent.timed_says()[0][0] - now
    plain = other.sent.timed_says()[0][0] - (now + 40)
    assert typed == pytest.approx(plain)


def test_voice_down_mid_burst_types(vrig):
    """N6."""
    v = vrig
    v.says(numbered())
    hall(v, 101, 102)
    v.world.online &= {101, 102}
    send = v.fake.send

    def once(msg):
        send(msg)
        v.fake.greeted = False  # the service goes with its first line
    v.fake.send = once

    async def go():
        await v.session()
        task = asyncio.create_task(v.engine._burst(BurstPlan(2, 2)))
        await v.advance(40)
        await task
    run(go())
    (first,) = v.speaks()
    (second,) = v.sent.says()
    assert second.channel == "guild" and second.guid != first.guid
    assert [r["channel"] for r in v.lines()] == ["voice:guild_hall", "guild"]


def test_a_reply_on_the_officers_room_types_in_officer(vrig):
    v = vrig
    v.seat(102, "officers")
    v.him("officers")
    v.fake.greeted = False
    v.says("noted, thanks")
    run(said(v, Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="voice:officers",
                     addressee=HUMAN)))
    assert [s.channel for s in v.sent.says()] == ["officer"]
    assert v.lines()[0]["channel"] == "officer"


def test_service_lost_while_the_model_writes_types_the_line(vrig):
    v = vrig
    hall(v, 101)

    def lost(model, messages):
        v.fake.greeted = False
        return "that one was close"
    v.says(lost)
    run(said(v, opener(channel="voice:guild_hall", addressee=HUMAN)))
    assert [s.channel for s in v.sent.says()] == ["guild"] and not v.speaks()
    assert v.lines()[0]["channel"] == "guild"


def test_a_speaker_gone_while_the_model_writes_says_nothing(vrig):
    v = vrig
    hall(v, 101)

    def gone(model, messages):
        v.world.online.discard(101)
        return "that one was close"
    v.says(gone)
    assert run(said(v, opener())) == []
    assert not v.speaks() and not v.sent.says()
    assert v.utterances()[-1]["dropped"] == "offline"


def test_a_speaker_moved_while_the_model_writes_speaks_in_the_new_room(vrig):
    v = vrig
    hall(v, 101)

    def moved(model, messages):
        v.seat(101, "raid")
        v.him("raid")
        return "that one was close"
    v.says(moved)
    run(said(v, opener()))
    assert [s.channel for s in v.speaks()] == ["raid"]
    assert v.lines()[0]["channel"] == "voice:raid"


@pytest.mark.parametrize("error", [VoiceDown("gone"), RuntimeError(SECRET),
                                   wire.WireError("bad field: text", SECRET)])
def test_a_failed_send_types_the_line(vrig, caplog, error):
    """The ruling: the voice path never loses a line, and the log never holds its words."""
    v = vrig
    v.says(SECRET)
    hall(v, 101)

    def fail(msg):
        raise error
    v.fake.send = fail
    with caplog.at_level(logging.DEBUG):
        out = run(said(v, opener(channel="voice:guild_hall", addressee=HUMAN)))
    assert out and [s.channel for s in v.sent.says()] == ["guild"]
    assert [r["channel"] for r in v.lines()] == ["guild"]  # one line, where it was said
    (row,) = voice_lines(v)
    assert (row["state"], row["line_id"]) == ("dropped", 0)
    assert SECRET not in caplog.text


def test_voice_down_on_send_is_logged_as_the_plan_names_it(vrig):
    v = vrig
    hall(v, 101)

    def fail(msg):
        raise VoiceDown("gone")
    v.fake.send = fail
    run(said(v, opener()))
    (row,) = voice_lines(v)
    assert (row["state"], row["reason"]) == ("dropped", "voice_down")


@pytest.mark.parametrize("name", ["place", "voiced", "deliver"])
def test_a_fault_in_routing_types_the_line(vrig, caplog, name):
    v = vrig
    v.says(SECRET)
    hall(v, 101)

    def boom(*a, **k):
        raise RuntimeError(SECRET)
    setattr(v.comms, name, boom)
    with caplog.at_level(logging.DEBUG):
        out = run(said(v, opener(channel="voice:guild_hall", addressee=HUMAN)))
    assert out and [s.channel for s in v.sent.says()] == ["guild"]
    assert SECRET not in caplog.text


@pytest.mark.parametrize("name", ["hold", "log_line", "hearable", "unheard", "available"])
def test_a_fault_elsewhere_never_reaches_the_talk_loop(vrig, caplog, name):
    v = vrig
    v.says(SECRET)
    hall(v, 101)

    def boom(*a, **k):
        raise RuntimeError(SECRET)
    setattr(v.comms, name, boom)

    async def go():
        assert v.engine._candidates()
        return await said(v, opener())
    with caplog.at_level(logging.DEBUG):
        out = run(go())
    assert out and (v.speaks() or v.sent.says())
    assert SECRET not in caplog.text


def test_his_being_offline_is_no_dead_service(vapp):
    """Task 11's carry: the service reports ok False while he is off, the output being closed.
    That stops nothing: the session still goes out, and the next status settles it."""
    async def go():
        comms = vapp.ext.comms
        vapp.fake.status = status(ok=False)
        assert not comms.available()
        await vapp.login()
        assert vapp.types().count("session") >= 1
        assert next(m for m in vapp.sent if isinstance(m, wire.Session)).online is True
        await vapp.ext.on_message(status())  # the output opened on `session`
        vapp.fake.status = status()
        assert comms.available()
        vapp.fake.status = status(ok=False)  # mid-session: the output really is gone (M22)
        assert not comms.available()
        vapp.fake.status = status()  # and nothing was latched
        assert comms.available()
        await vapp.logout()
        vapp.fake.status = status(ok=False)
        assert not comms.available()
    run(go())


# -- his key (settled item 13) -----------------------------------------------------------------

def test_his_key_holds_a_voiced_model_turn(vrig):
    v = vrig
    hall(v, 101)

    async def go():
        v.comms.ptt_down = True
        task = asyncio.create_task(v.engine.speak(opener()))
        await v.advance(5)
        assert not v.chat_calls() and not v.speaks()
        v.comms.ptt_down = False
        await v.advance(2)
        assert v.chat_calls() and v.speaks() and task.done()
    run(go())


def test_the_hold_is_bounded_by_the_turns_deadline(vrig):
    v = vrig
    hall(v, 101)
    v.comms.ptt_down = True

    async def go():
        out = await said(v, opener(), seconds=25)
        assert out == []
    run(go())
    assert not v.chat_calls() and not v.speaks()
    assert v.utterances()[-1]["dropped"] == "expired"


@pytest.mark.parametrize("turn", [
    Turn(kind=TurnKind.GZ, speaker=101, channel="guild", template="gz"),
    Turn(kind=TurnKind.URGENT, speaker=101, channel="guild"),
    Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid")])
def test_his_key_never_holds_a_template_p0_or_p1(vrig, turn):
    v = vrig

    async def go():
        await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
        v.seat(101, "raid")
        v.seat(102, "raid")
        v.him("raid")
        v.comms.ptt_down = True
        task = asyncio.create_task(v.engine.speak(turn))
        await v.advance(1, step=0.05)
        assert task.done() and v.speaks()
    run(go())


def test_voice_lost_during_a_hold_types_at_once(vrig):
    v = vrig
    hall(v, 101)

    async def go():
        v.comms.ptt_down = True
        task = asyncio.create_task(v.engine.speak(opener()))
        await v.advance(3)
        v.fake.greeted = False
        await v.advance(20)
        assert task.done()
    run(go())
    assert [s.channel for s in v.sent.says()] == ["guild"]


def test_a_typed_turn_is_never_held(vrig):
    v = vrig
    hall(v, 101)
    v.comms.ptt_down = True

    async def go():
        task = asyncio.create_task(v.engine.speak(opener(103)))
        await v.advance(1)
        assert v.chat_calls()
        await v.advance(20)
        await task
    run(go())
    assert [s.guid for s in v.sent.says()] == [103]


# -- his pushback on the air (G5, G6) ----------------------------------------------------------

def test_his_pushback_works_in_a_room(vrig):
    v = vrig
    hall(v, 101, 102)
    v.world.online &= {101, 102}
    v.says(numbered("you are bankrupting the guild bank {n}"))

    async def go():
        await v.session()
        await said(v, opener())
        await v.advance(15)
        v.says("I meant the repair bill, my bad\nand another thing")
        await v.human("what are you talking about?", channel="voice:guild_hall")
        await v.advance(45)
    run(go())
    first = v.speaks()[1]
    assert (first.guid, first.text, first.channel) == (
        101, "I meant the repair bill, my bad", "guild_hall")
    row = v.rows("SELECT why FROM utterance WHERE text = ?", (first.text,))[0]
    assert row["why"] == "reply:pushback"
    assert any("pushing back" in p for p in v.prompts())


# -- what else the plan names ------------------------------------------------------------------

def test_elsewhere_is_at_background_priority(vrig):
    v = vrig
    hall(v)
    v.seat(104, "smoke_room")
    seen, submit = [], v.engine.scheduler.submit

    async def spy(priority, job, **kw):
        seen.append(priority)
        return await submit(priority, job, **kw)
    v.engine.scheduler.submit = spy
    run(said(v, opener(104, "voice:smoke_room"), elsewhere=True))
    assert Priority.BACKGROUND in seen and Priority.SOCIAL not in seen


def test_speak_keywords_default_to_todays_turn(vrig):
    v = vrig
    v.engine.comms = None
    seen = []
    real = v.engine._speak

    async def spy(turn, meta):
        seen.append(meta)
        return await real(turn, meta)
    v.engine._speak = spy
    run(said(v, opener()))
    assert (seen[0].why, seen[0].elsewhere, seen[0].guards, seen[0].after) == (
        "speak", False, None, "")


def test_a_turns_own_guards_count_it(vrig):
    from leeroy_mind.talk.guards import Guards
    v = vrig
    hall(v)
    v.seat(104, "smoke_room")
    v.seat(105, "smoke_room")
    own = Guards(rng=v.engine.rng)
    run(said(v, opener(104, "voice:smoke_room"), elsewhere=True, guards=own))
    assert len(own._window) == 1 and not v.engine.guards._window


def test_played_updates_voice_line(vrig):
    v = vrig
    hall(v, 101)
    run(said(v, opener()))
    (s,) = v.speaks()
    assert voice_lines(v)[0]["state"] == "sent"
    assert voice_lines(v)[0]["line_id"] == v.lines()[0]["id"]
    v.comms.played(wire.Played(s.id, "started"))
    assert voice_lines(v)[0]["state"] == "started"
    v.comms.played(wire.Played(s.id, "done"))
    assert voice_lines(v)[0]["state"] == "done"


def test_played_reaches_the_row_through_the_extension(vapp):
    async def go():
        vapp.ext.store.add_line("r.t1", 7, "guild_hall", "sent", 1.0)
        await vapp.ext.on_message(wire.Played("r.t1", "dropped", "expired"))
        (row,) = vapp.rows("SELECT state, reason FROM voice_line")
        assert (row["state"], row["reason"]) == ("dropped", "expired")
    run(go())


def test_speak_ids_carry_the_run(vrig):
    v = vrig
    hall(v, 101)
    run(said(v, opener()))
    assert v.speaks()[0].id.startswith("run.t")


def test_a_line_the_service_never_answers_ages_out(vrig):
    """A speak the service rejects with an error gets no played (Task 2's carry)."""
    v = vrig
    hall(v, 101, 102)

    async def go():
        await said(v, opener(), seconds=5)
        await said(v, opener(102), seconds=5)
        first, second = v.speaks()
        v.comms.played(wire.Played(second.id, "started"))
        v.comms.age_out()
        assert voice_lines(v)[0]["state"] == "sent"  # not yet
        await v.advance(AGE_OUT_S + 16)
        v.comms.age_out()
    run(go())
    states = {r["speak_id"]: (r["state"], r["reason"]) for r in voice_lines(v)}
    first, second = v.speaks()
    assert states[first.id] == ("dropped", "no_ack")
    assert states[second.id] == ("started", None)


def test_spoken_and_stored_text_are_separate(vrig):
    """The seam for the cue mapper: what he hears need not be what the log keeps."""
    v = vrig
    v.says("that was funny lol")
    hall(v, 101)
    real = v.comms.deliver
    v.comms.deliver = lambda turn, meta, text: real(turn, meta, text.replace("lol", "<chuckle>"))
    run(said(v, opener()))
    assert v.speaks()[0].text == "that was funny <chuckle>"
    assert v.lines()[0]["text"] == "that was funny lol"


def test_raid_and_cancel_forwarded(vapp):
    async def go():
        ext, ts = vapp.ext, 0

        def tick():
            for t in ext.tickers:
                t()
        await vapp.app.handle(Cast(ts=ts, members=tuple(mem(g) for g in TEST_CAST)))
        await vapp.login()
        await vapp.app.handle(Group(ts=ts, leader_guid=102, raid=True, members=RAID))
        vapp.sent.clear()
        tick()
        assert vapp.sent == [wire.Raid("trash", 102)]
        vapp.sent.clear()
        tick()  # nothing changed: nothing sent
        assert vapp.sent == []
        await vapp.app.handle(Boss(ts, 631, 5, 0, "", "engaged", RAID))
        tick()
        assert vapp.sent == [wire.Raid("combat", 102), wire.Cancel(below="p3")]
        vapp.sent.clear()
        await vapp.app.handle(Boss(ts, 631, 5, 0, "", "wiped", RAID))
        tick()
        assert vapp.sent == [wire.Raid("wipe_silence", 102)]
        vapp.sent.clear()
        await vapp.clock.advance(10.5)
        tick()
        assert vapp.sent == [wire.Raid("wipe_debrief", 102)]
        vapp.sent.clear()
        await vapp.app.handle(Logout(ts, 101, False))
        assert vapp.sent == [wire.Cancel(guid=101)]
    run(go())


def test_the_snapshot_carries_the_raid_pair(vapp):
    async def go():
        await vapp.login()
        await vapp.app.handle(Group(ts=0, leader_guid=102, raid=True, members=RAID))
        vapp.ext.snapshot()
        vapp.sent.clear()
        for t in vapp.ext.tickers:
            t()
        assert not any(isinstance(m, wire.Raid) for m in vapp.sent)
    run(go())


def test_ptt_is_followed_and_cleared(vapp):
    """A press cut off by a dead service never holds turns after a reconnect (NP5)."""
    async def go():
        ext = vapp.ext
        await ext.on_message(wire.Ptt(True, "guild_hall"))
        assert ext.comms.ptt_down
        await ext.on_message(wire.Ptt(False, "guild_hall"))
        assert not ext.comms.ptt_down
        await ext.on_message(wire.Ptt(True, "guild_hall"))
        await ext.on_connection(False)
        assert not ext.comms.ptt_down
        ext.comms.ptt_down = True
        ext.snapshot()
        assert not ext.comms.ptt_down
    run(go())


def test_attach_wires_the_engine(vapp):
    assert vapp.app.engine.comms is vapp.ext.comms
    assert vapp.app.world.room_audience == vapp.ext.comms.audience


def test_voice_off_touches_nothing(tmp_path, clock, voice_data):
    (tmp_path / "mind.toml").write_text("", encoding="utf-8")
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
    assert app.engine.comms is None and app.voice.comms is None
    assert app.world.room_audience is None and not app.voice.tickers


def test_voice_threads(store):
    tracker = ThreadTracker(store)
    for text in ("first line", "second line"):
        ev = Chat(ts=0, channel="voice:guild_hall", from_guid=101, from_name="Arkon", to_guid=0,
                  text=text, map=0, zone=10)
        tracker.on_line_sent(store.insert_chat_line(ev, session_id=None, origin="mind"), ev)
    rows = store.conn.execute("SELECT key FROM chat_thread").fetchall()
    assert [r["key"] for r in rows] == ["voice:guild_hall"]


def test_prompt_tag():
    row = ChatLineRow(id=1, ts=0.0, channel="voice:raid", from_guid=101, from_name="Arkon",
                      to_guid=0, text="on my mark", map=0, zone=0, session_id=None,
                      thread_id=None, origin="mind")
    assert chat_line_text(row, {101: "Arkon"}) == "Arkon (on vent): on my mark"


def test_a_fresh_rig_has_no_comms(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    assert rig.engine.comms is None and rig.world.room_audience is None


# -- fix round 1: his real name on a voiced line (I2) ------------------------------------------

def inner_circle(v, guid: int) -> None:
    v.store.put_relationship(Relationship(a=guid, b=HUMAN, affinity=60, trust=40, familiarity=85,
                                          interactions=30))


def offered(monkeypatch) -> None:
    """Every inner-circle prompt carries his real name (the real share is 15%)."""
    monkeypatch.setattr(pipeline_module, "real_name_line",
                        lambda human, rel, rng: f"real name, {human.real_name}")


def caught(v, text: str) -> str:
    """What a member outside his inner circle sees of a stored line (prompt.redact_for)."""
    return redact_real_name(text, v.engine.human, v.engine.human.called)


CASES = [REAL.lower(), REAL, REAL.upper(), "qUiLLoN"]


@pytest.mark.parametrize("written", CASES)
def test_an_offered_voiced_line_spells_his_name(vrig, monkeypatch, written):
    """The stored line and the spoken one hold the name in the form redaction catches, however
    the model typed it."""
    v = vrig
    inner_circle(v, 101)
    offered(monkeypatch)
    v.says(f"{written} you legend")
    hall(v, 101)
    run(said(v, opener(addressee=HUMAN)))
    (s,) = v.speaks()
    (row,) = v.lines()
    assert s.text == row["text"] == f"{REAL} you legend"
    assert REAL.casefold() not in caught(v, row["text"]).casefold()


@pytest.mark.parametrize("written", CASES)
def test_an_offered_line_is_guarded_the_same_voiced_or_typed(vrig, monkeypatch, written):
    v = vrig
    inner_circle(v, 101)
    inner_circle(v, 103)
    offered(monkeypatch)
    v.says(f"{written} you legend")
    hall(v, 101)

    async def go():
        await said(v, opener(addressee=HUMAN))
        v.engine._real_name_at = float("-inf")  # the guild-wide gap between two offers
        await said(v, opener(103, addressee=HUMAN))
    run(go())
    voiced, typed_line = v.speaks()[0].text, v.sent.says()[0].text
    for line in (voiced, typed_line):
        assert REAL in line and REAL.casefold() not in caught(v, line).casefold()


@pytest.mark.parametrize("written,seen", [
    (REAL, "Zan"), (REAL.upper(), "Zan"), (REAL.lower(), REAL.lower())])
def test_outside_the_circle_a_voiced_line_is_redacted_like_a_typed_one(vrig, written, seen):
    """A capitalised use is taken out; the lower-case word is the ordinary word it may also be
    (the ruling in screens._real_name_use), on either path."""
    v = vrig
    v.says(f"{written} you legend")
    hall(v, 101)

    async def go():
        await said(v, opener(addressee=HUMAN))
        await said(v, opener(103, addressee=HUMAN))
    run(go())
    voiced, typed_line = v.speaks()[0].text, v.sent.says()[0].text
    assert voiced.split()[0] == seen
    assert typed_line.split()[0].casefold() == seen.casefold()


def test_his_name_spelt_on_the_air_never_reaches_a_member_outside_the_circle(vrig, monkeypatch):
    """What the guard is for: the next prompt of someone who must not know it."""
    v = vrig
    inner_circle(v, 101)
    offered(monkeypatch)
    v.says(f"{REAL.lower()} you legend")
    hall(v, 101, 102)

    async def go():
        await v.session()
        await said(v, opener(addressee=HUMAN))
        messages, _ = await v.engine.compose(
            Turn(kind=TurnKind.CONTINUE, speaker=102, channel="voice:guild_hall", addressee=101))
        return "\n".join(m["content"] for m in messages)
    prompt = run(go())
    assert "Zan you legend" in prompt and REAL.casefold() not in prompt.casefold()


def test_an_offered_line_that_falls_back_to_typed_is_spelt(vrig, monkeypatch):
    v = vrig
    inner_circle(v, 101)
    offered(monkeypatch)
    v.says(f"{REAL.lower()} you legend")
    hall(v, 101)

    def fail(msg):
        raise VoiceDown("gone")
    v.fake.send = fail
    run(said(v, opener(addressee=HUMAN)))
    (say,) = v.sent.says()
    assert REAL in say.text and REAL.casefold() not in caught(v, say.text).casefold()


# -- fix round 1: no raid line vanishes (I1) ---------------------------------------------------

async def raid_formed(v) -> None:
    await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))


def said_as(v, guid: int, channel: str, text: str = "Pulling in three.",
            kind: TurnKind = TurnKind.CALLOUT) -> asyncio.Task:
    return asyncio.create_task(v.engine.say_as(guid, channel, text, kind=kind))


@pytest.mark.parametrize("guid", [101, 102])  # a raider, and the lead
def test_a_raid_say_as_from_another_room_is_typed(vrig, guid):
    """The ruling: it goes out in raid chat, and nothing is left in a room he can't hear."""
    v = vrig

    async def go():
        await raid_formed(v)
        v.seat(guid, "guild_hall")
        v.him("raid")
        task = said_as(v, guid, "raid")
        await v.advance(20)
        return task.result()
    assert run(go()) is True
    assert [(s.channel, s.guid) for s in v.sent.says()] == [("raid", guid)]
    assert not v.speaks() and not voice_lines(v)
    assert [r["channel"] for r in v.lines()] == ["raid"]


def test_say_as_is_false_when_the_game_refused_the_line(vrig):
    v = vrig
    v.sent.ack = lambda cmd: (False, "refused")

    async def go():
        await raid_formed(v)
        v.seat(102, "guild_hall")
        v.him("raid")
        task = said_as(v, 102, "raid")
        await v.advance(20)
        return task.result()
    assert run(go()) is False
    assert not v.lines()


def test_say_as_is_false_for_a_line_nobody_can_hear(vrig):
    """It went into a room he is not in: stored there, gone out nowhere."""
    v = vrig
    v.seat(104, "smoke_room")
    v.him("guild_hall")

    async def go():
        task = said_as(v, 104, "voice:smoke_room", kind=TurnKind.OPEN)
        await v.advance(20)
        return task.result()
    assert run(go()) is False
    assert voice_lines(v)[0]["state"] == "unheard"


def test_say_as_is_true_for_a_line_the_service_took(vrig):
    v = vrig

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        return await v.engine.say_as(102, "raid", "Pulling in three.", kind=TurnKind.CALLOUT)
    assert run(go()) is True
    assert v.speaks()


def test_a_raid_line_he_leaves_while_the_model_writes_is_typed(vrig):
    v = vrig

    def left(model, messages):
        v.him("guild_hall")
        return "Pull is in three"
    v.says(left)

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        await said(v, Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid"))
    run(go())
    assert [s.channel for s in v.sent.says()] == ["raid"]
    assert not v.speaks() and not voice_lines(v)


def test_a_raid_line_he_leaves_behind_the_send_lock_is_typed(vrig):
    """A template is never rerouted after the model: the send is the last moment."""
    v = vrig

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        async with v.engine._send_lock:
            task = said_as(v, 102, "raid")
            await v.advance(1)
            v.him("guild_hall")
        await v.advance(20)
        return task.result()
    assert run(go()) is True
    assert [s.channel for s in v.sent.says()] == ["raid"]
    assert not v.speaks() and not voice_lines(v)


# -- fix round 1: a line that can't be spoken types on its own channel (I3) --------------------

def down_on_send(v) -> None:
    def fail(msg):
        raise VoiceDown("gone")
    v.fake.send = fail


@pytest.mark.parametrize("channel", ["raid", "party", "say"])
def test_a_failed_send_stays_on_the_turns_channel(vrig, channel):
    v = vrig
    down_on_send(v)

    async def go():
        await raid_formed(v)
        hall(v, 101)
        task = said_as(v, 101, channel, "gg", TurnKind.GZ)
        await v.advance(20)
        return task.result()
    assert run(go()) is True
    assert [s.channel for s in v.sent.says()] == [channel]
    assert [r["channel"] for r in v.lines()] == [channel]


def test_a_failed_send_of_the_leads_call_types_in_raid(vrig):
    v = vrig
    down_on_send(v)

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        task = said_as(v, 102, "raid")
        await v.advance(20)
        return task.result()
    assert run(go()) is True
    assert [s.channel for s in v.sent.says()] == ["raid"]


def test_voice_lost_while_the_model_writes_stays_on_the_turns_channel(vrig):
    v = vrig

    def lost(model, messages):
        v.fake.greeted = False
        return "Pull is in three"
    v.says(lost)

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        await said(v, Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid"))
    run(go())
    assert [s.channel for s in v.sent.says()] == ["raid"]
    assert v.lines()[0]["channel"] == "raid"


def test_a_fault_on_the_second_route_stays_on_the_turns_channel(vrig):
    v = vrig
    real, calls = v.comms.place, []

    def second_fails(turn, meta):
        calls.append(turn.channel)
        if len(calls) > 1:
            raise RuntimeError(SECRET)
        return real(turn, meta)
    v.comms.place = second_fails

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        await said(v, Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid"))
    run(go())
    assert calls[1] == "voice:raid"
    assert [s.channel for s in v.sent.says()] == ["raid"]


def test_a_failed_send_in_a_private_room_stays_a_whisper(vrig):
    """It came on guild, but it was written as a whisper between the two: never public."""
    v = vrig
    v.says("something only between us")
    down_on_send(v)
    v.seat(102, "private1")
    v.him("private1")
    run(said(v, opener(102)))
    assert [(s.channel, s.to_guid) for s in v.sent.says()] == [("whisper", HUMAN)]
    assert [r["channel"] for r in v.lines()] == ["whisper"]


def test_voice_lost_in_a_private_room_stays_a_whisper(vrig):
    v = vrig

    def lost(model, messages):
        v.fake.greeted = False
        return "something only between us"
    v.says(lost)
    v.seat(102, "private1")
    v.him("private1")
    run(said(v, opener(102)))
    assert [(s.channel, s.to_guid) for s in v.sent.says()] == [("whisper", HUMAN)]


def test_a_raid_burst_stays_in_raid_when_voice_goes(vrig):
    """The opener came on raid; the next line arrives on the room's channel, carrying it."""
    v = vrig
    v.says(numbered())
    send = v.fake.send

    def once(msg):
        send(msg)
        v.fake.greeted = False
    v.fake.send = once

    async def go():
        await v.session()
        await raid_formed(v)
        v.world.online &= {101, 102}
        v.seat(101, "raid")
        v.seat(102, "raid")
        v.him("raid")
        task = asyncio.create_task(v.engine._burst(BurstPlan(2, 2, "raid")))
        await v.advance(40)
        await task
    run(go())
    assert len(v.speaks()) == 1
    assert [s.channel for s in v.sent.says()] == ["raid"]


# -- fix round 1: a typed pushback at a line he heard (I4, G6) ---------------------------------

PUSHED = "what are you talking about?"


def pushback_rows(v) -> list:
    return v.rows("SELECT guid, channel FROM utterance WHERE why = 'reply:pushback'")


def test_a_typed_pushback_finds_the_member_he_heard(vrig):
    v = vrig
    v.says(numbered("you are bankrupting the guild bank {n}"))
    hall(v, 101, 102)
    v.world.online &= {101, 102}

    async def go():
        await v.session()
        await said(v, opener(), seconds=5)
        await v.advance(15)
        v.says("I meant the repair bill, my bad")
        await v.human(PUSHED)
        await v.advance(45)
    run(go())
    assert [(r["guid"], r["channel"]) for r in pushback_rows(v)] == [(101, "guild")]
    assert v.sent.says()[0].guid == 101  # typed: an answer to a typed line
    assert any("pushing back" in p for p in v.prompts())


def test_a_typed_pushback_shows_the_member_their_spoken_line(vrig):
    v = vrig
    v.says("you are bankrupting the guild bank man")
    hall(v, 101)
    v.world.online &= {101}

    async def go():
        await v.session()
        await said(v, opener(), seconds=5)
        await v.advance(15)
        v.says("I meant the repair bill, my bad")
        await v.human(PUSHED)
        await v.advance(45)
    run(go())
    assert "you are bankrupting the guild bank man" in v.prompts()[-1]


def test_a_typed_pushback_passes_an_older_typed_line_for_the_one_he_heard(vrig):
    """Risk 8: an off-comms member's typed line is still in the window, and is not the last."""
    v = vrig
    hall(v, 101)
    v.world.online &= {101, 103}

    async def go():
        await v.session()
        v.says("the flask prices are a joke this week")
        await said(v, opener(103), seconds=30)
        v.says("you are bankrupting the guild bank man")
        assert await said(v, opener(), seconds=5)  # the line he heard, after the typed one
        await v.advance(15)
        v.says("I meant the repair bill, my bad")
        await v.human(PUSHED)
        await v.advance(45)
    run(go())
    assert [r["guid"] for r in pushback_rows(v)] == [101]


def test_a_typed_pushback_at_a_newer_typed_line_stays_on_it(vrig):
    v = vrig
    v.says(numbered("you are bankrupting the guild bank {n}"))
    hall(v, 101)
    v.world.online &= {101, 103}

    async def go():
        await v.session()
        await said(v, opener(), seconds=5)
        await said(v, opener(103), seconds=30)
        v.says("I meant the repair bill, my bad")
        await v.human(PUSHED)
        await v.advance(45)
    run(go())
    assert [r["guid"] for r in pushback_rows(v)] == [103]


def test_a_typed_pushback_ignores_a_room_he_is_not_in(vrig):
    """He never heard it."""
    v = vrig
    v.says(numbered("you are bankrupting the guild bank {n}"))
    hall(v)
    v.seat(104, "smoke_room")
    v.seat(105, "smoke_room")

    async def go():
        await v.session()
        await said(v, opener(104, "voice:smoke_room"), seconds=5, elsewhere=True)
        await v.human(PUSHED)
        await v.advance(45)
    run(go())
    assert not pushback_rows(v)


# -- fix round 2: a raid line from a private room (the ruling), and the vent tag (M1) ----------

def test_a_raid_call_from_a_private_room_is_typed_and_her_talk_stays_private(vrig):
    v = vrig
    v.says("glad we can talk here")

    async def go():
        await raid_formed(v)
        v.seat(102, "private1")
        v.him("private1")
        task = said_as(v, 102, "raid")
        await v.advance(20)
        assert task.result() is True
        await said(v, opener(102))
    run(go())
    assert [(s.channel, s.to_guid) for s in v.sent.says()] == [("raid", 0)]
    assert [s.channel for s in v.speaks()] == ["private1"]
    raid, private = v.lines()
    assert (raid["channel"], raid["to_guid"]) == ("raid", 0)
    assert (private["channel"], private["to_guid"]) == ("whisper", HUMAN)
    assert [r["room"] for r in voice_lines(v)] == ["private1"]  # no row for the typed line


def test_a_raid_line_whose_speaker_goes_private_is_typed(vrig):
    v = vrig

    def private(model, messages):
        v.seat(102, "private1")
        v.him("private1")
        return "Pull is in three"
    v.says(private)

    async def go():
        await raid_formed(v)
        v.seat(102, "raid")
        v.him("raid")
        await said(v, Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid"))
    run(go())
    assert [(s.channel, s.to_guid) for s in v.sent.says()] == [("raid", 0)]
    assert not v.speaks()


def test_a_raid_turn_from_a_private_room_is_not_told_what_she_whispered(vrig):
    """It goes out where the raid sees it, so its prompt keeps to what is public."""
    v = vrig
    v.says("something only between us")

    async def go():
        await v.session()
        await raid_formed(v)
        v.seat(102, "private1")
        v.him("private1")
        await said(v, opener(102))
        v.says("Pull is in three")
        await said(v, Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid"))
    run(go())
    assert not any("something only between us" in p for p in v.prompts()[1:])
    assert [s.channel for s in v.sent.says()] == ["raid"]


@pytest.mark.parametrize("copied,kept", [
    ("(on vent): that was close", "that was close"),
    ("(on vent) that was close", "that was close"),
    ("[3m ago] (on vent): that was close", "that was close"),
    ("[3m ago] Arkon (on vent): that was close", "that was close"),
    ("(ON VENT): that was close", "that was close")])
def test_the_vent_tag_is_never_copied_into_a_line(copied, kept):
    assert strip_copied(copied, ("Arkon",)) == kept


def test_a_copied_vent_tag_is_never_spoken(vrig):
    v = vrig
    v.says("(on vent): that was close")
    hall(v, 101)
    run(said(v, opener()))
    assert [s.text for s in v.speaks()] == ["that was close"]
    assert v.lines()[0]["text"] == "that was close"


def test_words_about_being_on_vent_stay(vrig):
    assert strip_copied("on vent now, one sec", ("Arkon",)) == "on vent now, one sec"

