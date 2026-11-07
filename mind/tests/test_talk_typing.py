"""Who is typing (the guild console's Comms dots): each model turn's phase, composing from the
compose call until its text exists and typing over the typing delay before each line is sent,
cleared when the line is sent, the turn dropped or cancelled, or an error ends it. The control
endpoint's status reads it through Pipeline.typing_now: ids, the place and when, never text."""
import asyncio
import json

import pytest

from conftest import HUMAN, TEST_CAST, run
from engine_rig import engine_clock, make_rig
from leeroy_mind.protocol import Group
from leeroy_mind.talk.turns import Turn, TurnKind

REPLY = "omw now"


@pytest.fixture
def clock():
    return engine_clock()


def reply_to_human(guid: int, channel: str = "guild", to_guid: int = 0) -> Turn:
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=channel, to_guid=to_guid,
                addressee=HUMAN)


def phases(r) -> list[str]:
    return [phase for _, phase, _ in r.engine.turn_phases()]


async def until(r, test, *, seconds: float = 40.0, step: float = 0.25) -> None:
    """Advance the clock in small steps until test() holds."""
    waited = 0.0
    while not test():
        assert waited < seconds, "timed out"
        await r.advance(step, step)
        waited += step


async def watch(r, task, *, seconds: float = 40.0, step: float = 0.25) -> list[str]:
    """The phases the engine's turns went through until `task` ended, each change once."""
    seen: list[str] = []
    waited = 0.0
    while not task.done():
        assert waited < seconds, "timed out"
        now = ",".join(phases(r)) or "-"
        if not seen or seen[-1] != now:
            seen.append(now)
        await r.advance(step, step)
        waited += step
    return seen


def test_a_turn_is_composing_then_typing_and_clears_when_its_line_is_sent(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply=REPLY, gate=gate)
        await r.session()
        task = asyncio.create_task(r.engine.speak(reply_to_human(101)))
        await until(r, lambda: phases(r) == ["composing"])
        [(turn, _, since)] = r.engine.turn_phases()
        assert turn.speaker == 101 and since <= clock.now()
        assert r.engine.typing_now() == [{"guid": 101, "name": TEST_CAST[101][0],
                                          "channel": "guild", "to": None, "since": since}]
        await r.advance(5)
        assert phases(r) == ["composing"]  # the model has not answered: still composing
        gate.set()
        seen = await watch(r, task)
        assert task.result() and r.sent.says()
        assert seen[0] == "composing" and "typing" in seen and seen[-1] == "typing"
        assert r.engine.turn_phases() == [] and r.engine.typing_now() == []
    run(main())


def test_a_dropped_turn_clears_its_phase(store, clock, tmp_path):
    async def main():
        gate, valid = asyncio.Event(), [True]
        r = make_rig(store, clock, tmp_path, reply=REPLY, gate=gate)
        await r.session()
        task = asyncio.create_task(r.engine.speak(reply_to_human(101),
                                                  still_valid=lambda: valid[0]))
        await until(r, lambda: phases(r) == ["composing"])
        valid[0] = False  # the owner's fence changed while the model was busy
        gate.set()
        await until(r, task.done)
        assert task.result() == [] and r.sent.says() == []
        assert r.engine.turn_phases() == [] and r.engine.typing_now() == []
    run(main())


def test_a_cancelled_turn_clears_its_phase(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply=REPLY, gate=gate)
        await r.session()
        task = asyncio.create_task(r.engine.speak(reply_to_human(101)))
        await until(r, lambda: phases(r) == ["composing"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert r.engine.turn_phases() == [] and r.engine.typing_now() == []
        gate.set()
    run(main())


def test_a_turn_that_raises_clears_its_phase(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply=REPLY)
        await r.session()

        async def broken(*a, **kw):
            assert phases(r) == ["typing"]  # its typing ran up to the send
            raise RuntimeError("send failed")
        r.engine._send_line = broken
        task = asyncio.create_task(r.engine.speak(reply_to_human(101)))
        await until(r, task.done)
        with pytest.raises(RuntimeError):
            task.result()
        assert r.engine.turn_phases() == [] and r.engine.typing_now() == []
    run(main())


def test_status_entries_say_where_and_when_never_what(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply=REPLY, gate=gate)
        await r.session()
        task = asyncio.create_task(r.engine.speak(reply_to_human(102, "whisper", HUMAN)))
        await until(r, lambda: phases(r) == ["composing"])
        [entry] = r.engine.typing_now()
        assert set(entry) == {"guid", "name", "channel", "to", "since"}
        assert (entry["guid"], entry["name"], entry["channel"], entry["to"]) == (
            102, TEST_CAST[102][0], "whisper", HUMAN)
        gate.set()
        await until(r, lambda: phases(r) == ["typing"])
        [entry] = r.engine.typing_now()
        assert entry["channel"] == "whisper" and entry["to"] == HUMAN
        text = json.dumps(r.engine.typing_now())
        assert REPLY not in text and "omw" not in text
        await until(r, task.done)
        assert r.engine.typing_now() == []
    run(main())


def test_only_places_comms_shows_carry_dots(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply=REPLY, gate=gate)
        await r.session()
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101)))
        turns = {101: reply_to_human(101, "party"),       # in his group: # party / raid
                 102: reply_to_human(102, "party"),       # another group's party: not his to see
                 103: reply_to_human(103, "say"),         # /say is not in Comms
                 104: reply_to_human(104, "whisper", 105),  # a whisper between members
                 105: reply_to_human(105, "officer")}
        tasks = [asyncio.create_task(r.engine.speak(t)) for t in turns.values()]
        await until(r, lambda: len(r.engine.turn_phases()) == len(turns))
        assert {t.speaker for t, phase, _ in r.engine.turn_phases()} == set(turns)
        assert [(e["guid"], e["channel"], e["to"]) for e in r.engine.typing_now()] == [
            (101, "party", None), (105, "officer", None)]
        r.world.human_online = False  # he logged out: nothing is his to see
        assert r.engine.typing_now() == []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        gate.set()
    run(main())


def test_a_template_line_has_no_model_work_and_shows_nothing(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.session()
        task = asyncio.create_task(r.engine.say_as(101, "guild", "gz", kind=TurnKind.GZ))
        seen = await watch(r, task)
        assert task.result() is True and seen == ["-"]
    run(main())
