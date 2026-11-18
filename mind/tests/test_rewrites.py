"""Rewrites that made it worse, the prompt's side (self-play audit, 2026-11-17): her last lines
are not shown twice in a private prompt, his goodbye is answered with one whatever she said
earlier, and a question back is never forced on his goodbye or his bad news. Invented lines."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN
from discord.test_dm_prompt import answer, answers, rig_for, talk_for
from engine_rig import numbered
from leeroy_mind.talk import pipeline
from leeroy_mind.talk.shape import Shape, ShapeDraw
from leeroy_mind.testing.fake_clock import FakeClock
from test_sofar import his_dm, section

LAST_LINES = "Your last lines (don't make the same move again):"


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def test_a_dm_prompt_never_shows_her_last_message_twice(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("ah {n}, the kettle is still broken"))
    talk = talk_for(rig)
    mids = []
    for text in ("did you fix the kettle", "what about the toaster", "and the fridge"):
        mids.append(his_dm(rig, text))
        answer(rig, talk, mids[-1])
        assert answers(rig, "dm:102", mids[-1])
    prompt = rig.prompts()[-1]
    hers = [m["text"] for mid in mids[:2] for m in answers(rig, "dm:102", mid)]
    assert len(hers) == 2
    for text in hers:
        assert text in section(prompt, "Chat:") and prompt.count(text) == 1
    assert "the kettle is still broken" not in section(prompt, LAST_LINES)
    # the repeat checks still read them
    assert any("the kettle is still broken" in x for x in rig.engine._own_lines(
        pipeline.Turn(pipeline.TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN,
                      addressee=HUMAN)))
    rig.close()


def test_a_whisper_prompt_never_shows_her_last_message_twice(store, clock, tmp_path):
    async def main():
        rig = rig_for(store, clock, tmp_path, script=numbered("ah {n}, still broken"))
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        start = len(rig.prompts())
        for text in ("did you fix the kettle", "what about the toaster", "and the fridge"):
            await rig.human(text, channel="whisper", to_guid=102)
            await rig.advance(60)
        prompt = rig.prompts()[-1]
        assert len(rig.prompts()) == start + 3
        hers = [x for x in section(prompt, "Chat:").splitlines() if "still broken" in x]
        assert len(hers) == 2
        assert "still broken" not in section(prompt, LAST_LINES)
        guild = len(rig.prompts())
        await rig.human("anyone seen my kettle", channel="guild")
        await rig.advance(60)
        assert len(rig.prompts()) > guild  # a public turn keeps its own lines as before
        rig.close()
    asyncio.run(main())


def later_twice(rig, channel="dm:102"):
    for text in ("ok talk later tonight then", "right, later then x"):
        rig.engine.moves.record(guid=102, counterpart=HUMAN, channel=channel, to_guid=HUMAN,
                                text=text, ts=rig.clock.now())


def test_his_goodbye_is_answered_with_one_whatever_she_said_before(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    later_twice(rig)
    rig.script("mm what else is new", "go on, talk later x")
    mid = his_dm(rig, "what are you up to")
    answer(rig, talk, mid)
    assert "said you'd see him later" in rig.prompts()[-1]  # an ordinary line: the hint stands
    calls = len(rig.chat_calls())
    later_twice(rig)
    mid = his_dm(rig, "right, gotta head to work now. talk tonight x")
    answer(rig, talk, mid)
    prompt = rig.prompts()[calls]
    assert "said you'd see him later" not in prompt
    assert len(rig.chat_calls()) == calls + 1  # her goodbye was not asked again
    assert [m["text"] for m in answers(rig, "dm:102", mid)] == ["go on, talk later x"]
    assert "move repeat" not in caplog.text.split("gotta")[-1]
    rig.close()


def test_his_goodbye_in_a_whisper_too(store, clock, tmp_path):
    async def main():
        rig = rig_for(store, clock, tmp_path)
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        later_twice(rig, "whisper")
        rig.script("sleep well, talk later x")
        before = len(rig.chat_calls())
        await rig.human("ok im heading to bed. night x", channel="whisper", to_guid=102)
        await rig.advance(60)
        said = [s.text for s in rig.sent.says() if s.guid == 102 and s.channel == "whisper"]
        assert said and "later" in said[-1]
        assert len(rig.chat_calls()) == before + 1
        assert "said you'd see him later" not in rig.prompts()[before]
        rig.close()
    asyncio.run(main())


@pytest.mark.parametrize("his,shape", [
    ("right, gotta head to work now. talk tonight x", "short"),
    ("my uncle is in hospital, they think its his heart", "short"),
    ("what did you think of the new patch then", "question"),
])
def test_a_question_back_is_never_forced_on_his_goodbye_or_bad_news(store, clock, tmp_path,
                                                                    caplog, monkeypatch, his,
                                                                    shape):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pipeline, "draw_shape",
                        lambda ctx, rng: ShapeDraw(Shape.QUESTION, 20, 1, 60))
    rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}, go on"), shape=True)
    talk = talk_for(rig)
    mid = his_dm(rig, his)
    answer(rig, talk, mid)
    assert answers(rig, "dm:102", mid)
    assert f"talk_shape: guid=102 kind=reply_human shape={shape}" in caplog.text
    asks = "ask him something back" in rig.prompts()[-1]
    assert asks is (shape == "question")
    rig.close()
