"""Thinking first for his whispers and DMs ([talk] think_private, 2026-11-16): an answer to his
whisper or his Discord DM asks the live model with `think` left out (Orion refuses `think: true`
and thinks by default), num_predict = the line's allowance + think_budget, and no stop (the
blank-line stop fired inside Orion's thinking); the answer is cut at its first blank line. Empty
content after the thinking is asked once more with `think: false`. Every other turn keeps LIVE.
The thinking never reaches the line, a log or the store. The test cast and invented lines only."""
import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import ORION_THOUGHT, OrionLLM, engine_clock, make_rig, save_member
from leeroy_mind.config import load_config
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.learn.recipe import THINK_SUFFIX
from leeroy_mind.llm.diagnostics import capture_response
from leeroy_mind.llm.presets import LIVE, LIVE_THINK, THINK_BUDGET, cut_at_stop, thinking
from leeroy_mind.talk.discord_length import num_predict, preset_for
from leeroy_mind.talk.pipeline import OWED_FALLBACK_HINT, TurnMeta
from leeroy_mind.talk.prompt import (DM_BUDGET, ESTIMATE_SLACK, LIVE_BUDGET, TEMPLATE_TOKENS,
                                     room_for)
from leeroy_mind.talk.turns import Turn, TurnKind

NUM_CTX = LIVE.options["num_ctx"]
DM_ALLOWANCE = num_predict(200)  # dm_words 200: 344


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def dm_answer(rig, text="talk to me"):
    """His DM to 102 and her answer through the whole pipeline: the stored answer, or None."""
    mid = dstore.post(rig.store, channel="dm:102", author=HUMAN, origin="human", text=text,
                      now=rig.clock.now())
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=HUMAN,
                addressee=HUMAN)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None))

    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(200)
        return task.result()
    rig.sync(go())
    row = rig.store.conn.execute("SELECT text FROM discord_message WHERE channel='dm:102' "
                                 "AND origin='mind' ORDER BY id DESC").fetchone()
    return row[0] if row else None


def thinking_call(call, allowance):
    assert call["think"] is None  # left out: how Orion thinks
    assert "stop" not in call["options"]
    assert call["options"]["num_predict"] == allowance + THINK_BUDGET
    assert call["options"]["num_ctx"] == NUM_CTX


def plain_call(call, allowance=80):
    assert call["think"] is False
    assert call["options"]["stop"] == ["\n\n"]
    assert call["options"]["num_predict"] == allowance
    assert call["options"]["num_ctx"] == NUM_CTX


# -- the presets ---------------------------------------------------------------------------------

def test_the_thinking_preset_adds_the_budget_and_drops_the_stop():
    assert LIVE_THINK.think is None and "stop" not in LIVE_THINK.options
    assert LIVE_THINK.options["num_predict"] == 80 + 1400 == 1480
    assert LIVE_THINK.options["num_ctx"] == NUM_CTX and LIVE_THINK.options["temperature"] == 0.9
    dm = thinking(preset_for(LIVE, 200))
    assert dm.options["num_predict"] == 344 + 1400 == 1744 and dm.options["num_ctx"] == NUM_CTX
    assert thinking(LIVE, 900).options["num_predict"] == 980
    assert thinking(LIVE, flag=True).think is True  # a model that thinks only when asked
    with pytest.raises(ValueError):
        thinking(LIVE, flag=False)
    with pytest.raises(ValueError):
        thinking(LIVE, 0)
    assert LIVE.think is False and LIVE.options["stop"] == ("\n\n",)  # LIVE itself unchanged


def test_cut_at_stop_ends_where_the_server_stop_would_have():
    assert cut_at_stop("first part\n\nsecond part", ("\n\n",)) == "first part"
    assert cut_at_stop("no stop here", ("\n\n",)) == "no stop here"
    assert cut_at_stop("a\nb", ("\n\n",)) == "a\nb"


def test_the_prompt_room_fits_num_ctx_with_the_thinking():
    whisper = room_for(LIVE_BUDGET, predict=80 + THINK_BUDGET, num_ctx=NUM_CTX)
    dm = room_for(DM_BUDGET, predict=DM_ALLOWANCE + THINK_BUDGET, num_ctx=NUM_CTX)
    assert whisper == LIVE_BUDGET == 1800 and dm == 1830
    for budget, predict in ((whisper, 80 + THINK_BUDGET), (dm, DM_ALLOWANCE + THINK_BUDGET)):
        assert budget * ESTIMATE_SLACK + TEMPLATE_TOKENS + predict <= NUM_CTX
    # without thinking the old budgets stand: they already fit
    assert room_for(DM_BUDGET, predict=DM_ALLOWANCE, num_ctx=NUM_CTX) == DM_BUDGET
    assert room_for(LIVE_BUDGET, predict=80, num_ctx=NUM_CTX) == LIVE_BUDGET


# -- which turns think -----------------------------------------------------------------------------

@pytest.mark.parametrize("channel,to_guid,answers,expect", [
    ("whisper", HUMAN, True, THINK_BUDGET),        # his whisper
    ("dm:102", HUMAN, True, THINK_BUDGET),         # his DM
    ("whisper", 103, True, 0),                     # a whisper to someone else
    ("guild", 0, True, 0), ("party", 0, True, 0), ("officer", 0, True, 0),
    ("discord:general", 0, True, 0),               # a Discord channel, even naming him
    ("gdm:7", 0, True, 0),                         # a group DM
    ("dm:102", HUMAN, False, 0),                   # her DM to him that answers nothing of his
])
def test_only_answers_to_his_whisper_or_dm_think(rig, channel, to_guid, answers, expect):
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, to_guid=to_guid,
                addressee=HUMAN, trigger_line=1)
    meta = TurnMeta("t", answers_human=answers)
    assert rig.engine._think_budget(turn, meta) == expect


def test_a_whisper_opener_to_him_does_not_think(rig):
    turn = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                addressee=HUMAN)
    assert rig.engine._think_budget(turn, TurnMeta("t", initiates=True)) == 0


def test_his_whisper_is_answered_after_thinking(rig):
    rig.script("aw hey you")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    [call] = rig.chat_calls()
    thinking_call(call, 80)
    assert [s.text for s in rig.sent.says()] == ["aw hey you"]


def test_his_dm_is_answered_after_thinking_and_its_prompt_makes_room(rig):
    rig.script("hey you, long day here but good")
    assert dm_answer(rig) == "hey you, long day here but good"
    [call] = rig.chat_calls()
    thinking_call(call, DM_ALLOWANCE)
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=HUMAN,
                addressee=HUMAN)
    meta = TurnMeta("t", answers_human=True)
    assert rig.engine._prompt_budget(turn, meta) == 1830
    rig.engine.think_private = False
    assert rig.engine._prompt_budget(turn, meta) == DM_BUDGET


def test_a_guild_answer_to_him_keeps_live(rig):
    rig.script("gear first, then gems")
    to_him = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN)
    rig.speak_turn(to_him)
    [call] = rig.chat_calls()
    plain_call(call)


def test_the_switch_off_restores_live(rig):
    rig.engine.think_private = False
    rig.script("aw hey you")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    [call] = rig.chat_calls()
    plain_call(call)
    assert call["options"] == json.loads(json.dumps(dict(LIVE.options)))


def test_the_owed_fallback_thinks_too(rig):
    rig.script("happy to help with that", "come over already", "oh. wow. i don't know what to say")
    rig.whisper_from_him(102, "leave him for me")
    rig.run()
    calls = rig.chat_calls()
    assert len(calls) == 3
    assert calls[-1]["messages"][-1]["content"].endswith(OWED_FALLBACK_HINT.format(human="Zan"))
    for call in calls:
        thinking_call(call, 80)
    assert [s.text for s in rig.sent.says()] == ["oh. wow. i don't know what to say"]


def test_a_learned_length_bound_lowers_only_the_allowance(rig):
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="whisper", to_guid=HUMAN,
                addressee=HUMAN, trigger_line=1)
    turn.deadline = rig.clock.now() + 60
    meta = TurnMeta("t", answers_human=True, whisper_reply=True)
    meta.learning = SimpleNamespace(num_predict=40)
    rig.script("short one")
    assert rig.sync(rig.engine._ask(turn, [{"role": "user", "content": "hi"}], meta=meta)) == "short one"
    assert rig.chat_calls()[-1]["options"]["num_predict"] == 40 + THINK_BUDGET


# -- what comes back -------------------------------------------------------------------------------

def test_empty_after_thinking_is_asked_once_more_without_it(rig, caplog):
    rig.engine.think_budget = 500  # Orion thinks 1000 tokens here: nothing is left for the line
    rig.script("hey, sorry, was miles away")
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        rig.whisper_from_him(102, "you there?")
        rig.run()
    first, second = rig.chat_calls()
    assert first["think"] is None and first["options"]["num_predict"] == 580
    plain_call(second)  # think: false sent, the ordinary request
    assert [s.text for s in rig.sent.says()] == ["hey, sorry, was miles away"]
    assert "talk_think_empty" in caplog.text
    assert ORION_THOUGHT not in caplog.text


def test_both_empty_falls_to_the_usual_silence_handling(rig):
    class Mute(OrionLLM):
        async def chat(self, model, messages, *, options, think=None, **kw):
            result = await super().chat(model, messages, options=options, think=think, **kw)
            return replace(result, text="")
    rig.engine.scheduler._llm = rig.llm = Mute(lambda model, messages: "x")
    rig.whisper_from_him(102, "hey")
    rig.run()
    calls = rig.chat_calls()
    # each request thinks, then asks once without: a draft, its silent retry and the fallback
    assert [c["think"] for c in calls] == [None, False] * 3
    assert not rig.sent.says() and rig.utterances()[-1]["dropped"] == "silent"


def test_the_answer_is_cut_at_its_first_blank_line(rig):
    rig.script("yeah i'm here\n\nZan: and then he said")
    rig.whisper_from_him(102, "you there?")
    rig.run()
    said = " ".join(s.text for s in rig.sent.says())
    assert said == "yeah i'm here"


def test_a_thought_answer_cut_by_num_predict_goes_back_to_a_sentence_end(rig, caplog):
    class Cut(OrionLLM):
        async def chat(self, model, messages, *, options, think=None, **kw):
            result = await super().chat(model, messages, options=options, think=think, **kw)
            out = {"message": {"content": result.text, "thinking": ORION_THOUGHT},
                   "done_reason": "length"}
            return replace(result, diagnostics=capture_response(out))
    rig.engine.scheduler._llm = rig.llm = Cut(lambda model, messages: "")
    rig.script("long day. finally home and i was going to")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        rig.whisper_from_him(102, "how was today")
        rig.run()
    [said] = [s.text for s in rig.sent.says()]
    assert said.rstrip(".") == "long day"  # her typing may drop the period
    assert "think_line_trimmed" in caplog.text


def test_the_thinking_never_reaches_a_log_a_line_or_the_store(rig, caplog):
    rig.engine.learn_enabled = True
    rig.script("aw hey you")
    with caplog.at_level(logging.DEBUG):
        rig.whisper_from_him(102, "hey you")
        rig.run()
        rig.engine.think_budget = 500  # and once through the empty-content retry
        rig.whisper_from_him(102, "still there?")
        rig.run()
    assert len(rig.sent.says()) >= 2
    assert ORION_THOUGHT not in caplog.text
    assert all(ORION_THOUGHT not in s.text for s in rig.sent.says())
    conn = rig.store.conn
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            assert not any(isinstance(v, str) and "PRIVATE-THOUGHT" in v for v in tuple(row)), table


def test_a_thought_line_is_recorded_as_one(rig):
    rig.engine.learn_enabled = True
    rig.script("aw hey you")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    rig.script("gear first, then gems")
    rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN))
    models = dict(rig.store.conn.execute(
        "SELECT c.channel, p.model_id FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id"))
    assert models["whisper"] == rig.cfg.models.live + THINK_SUFFIX
    assert models["guild"] == rig.cfg.models.live


# -- the switch --------------------------------------------------------------------------------

def test_the_switch_and_budget_are_read_from_mind_toml(tmp_path):
    path = tmp_path / "mind.toml"
    assert load_config(path).talk.think_private is True
    assert load_config(path).talk.think_budget == THINK_BUDGET
    path.write_text("[talk]\nthink_private = false\nthink_budget = 900\n", encoding="utf-8")
    talk = load_config(path).talk
    assert talk.think_private is False and talk.think_budget == 900
    path.write_text('[talk]\nthink_private = "yes"\nthink_budget = 5000\n', encoding="utf-8")
    talk = load_config(path).talk
    assert talk.think_private is False  # invalid: off
    assert talk.think_budget == THINK_BUDGET  # invalid: the default


def test_the_switch_is_read_at_mind_start_not_at_a_login(rig):
    assert rig.engine.think_private is True and rig.engine.think_budget == THINK_BUDGET
    off = replace(rig.cfg, talk=replace(rig.cfg.talk, think_private=False, think_budget=900))
    rig.engine.apply_config(off)
    assert rig.engine.think_private is True and rig.engine.think_budget == THINK_BUDGET
