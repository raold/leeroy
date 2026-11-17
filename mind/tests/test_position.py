"""[talk] coherence, her position inside a conversation (talk/position.py, talk/intent.py, the
pipeline's hold and block, personas/card.py): answer-first wants on his question or pushback,
no tease or flirt on his bad news, a want held while his question or disagreement is open, what
she has said so far in her prompt and the beat's, and the opinion his line touches on her card.
Fakes only; invented lines; the test cast."""
import asyncio
import json
import logging
from dataclasses import replace
from random import Random

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk import position
from leeroy_mind.talk.intent import (ANSWER_OFF, BAD_NEWS_OFF, Intent, IntentContext, allowed,
                                     draw_intent, weights)
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

BEAT = {"read": "he wants a straight answer", "lands": "a little sting", "want": "share",
        "use": None}


@pytest.fixture
def clock():
    return engine_clock()


def ctx(**over):
    base = dict(kind=TurnKind.REPLY_HUMAN, to_him=True, owed=True,
                his_line="what time is raid tonight", family="whisper", female=True,
                stance="lust", style="bold", attachment="anxious", humour="teasing",
                agreeableness=5, extraversion=5, tail=False, attention=None, state=None,
                event="none", block=None, kids=False, last=None)
    return IntentContext(**{**base, **over})


# -- the words ------------------------------------------------------------------------------------

def test_disagreement_bad_news_and_settling_are_read_from_his_words():
    assert position.disagrees("idk that sheet looked pretty legit")
    assert position.disagrees("wait you said it was nine")
    assert position.disagrees("which is it lol") and position.disagrees("what are you talking about")
    assert not position.disagrees("nice pull") and not position.disagrees("")
    assert position.bad_news("hes in hospital, heart thing")
    assert position.bad_news("rough day, the dog is at the vet")
    assert not position.bad_news("good raid tonight")
    assert position.settled("ok fair enough") and position.settled("lol")
    assert not position.settled("ok but why") and not position.settled("ok?")


def test_a_hold_stands_until_a_new_subject_ten_minutes_or_his_ok():
    hold = position.Hold("share", 100.0, position.words_of("that sheet looked legit"))
    assert position.held(hold, "which is it lol", 200.0)
    assert position.held(hold, "brb", 200.0) and position.held(hold, "?", 200.0)
    assert position.held(hold, "the sheet has you behind though", 200.0)
    assert not position.held(hold, "ok fair", 200.0)
    assert not position.held(hold, "what flask should i bring tonight", 200.0)
    assert not position.held(hold, "?", 100.0 + position.HOLD_S + 1)
    assert not position.held(None, "?", 200.0)


def test_her_claims_are_her_numbers_and_what_she_said_of_herself():
    lines = ["i have 7 wins against him and 9 losses", "lol", "oh is that so, mister",
             "he is down by 2. what about you?", "im at work till six", "who told you that?"]
    assert position.claims_so_far(lines) == [
        "i have 7 wins against him and 9 losses", "he is down by 2", "im at work till six"]
    # numbers first, the newest winning a tie, in the order she said them
    many = [f"i did pull number {i}" for i in range(6)]
    assert position.claims_so_far(many) == many[2:]
    assert position.claims_so_far(many, limit=1) == many[-1:]
    assert position.claims_so_far([]) == [] and position.said_line([]) == ""
    assert '"im at work till six"' in position.said_line(["im at work till six"])


def test_the_opinion_his_line_touches_and_whether_it_is_a_rule():
    opinions = ["Pugs are fine for farm nights.", "Decay is not optional; it is the law.",
                "Loot drama is boring."]
    his = "thinking of dropping the decay from 10 to 5 percent"
    assert position.opinion_for(opinions, his) == opinions[1]
    assert position.opinion_for(opinions, "anyone seen my hat") is None
    assert position.strong(opinions[1]) and not position.strong(opinions[0])
    block = position.position_block(["it stays at 10"], opinions[1])
    assert "it stays at 10" in block and "You never argue against it." in block
    assert "rule" not in position.position_block([], opinions[0])


# -- the want --------------------------------------------------------------------------------------

@pytest.mark.parametrize("line,push", [("what time is raid tonight", False),
                                       ("no way, you said nine", True)])
def test_his_question_or_pushback_leaves_only_answering_wants(line, push):
    w = weights(ctx(his_line=line, pushback=push, answer_first=True))
    assert all(w[i] == 0 for i in ANSWER_OFF)
    assert w[Intent.SHARE] > 0 and w[Intent.TEASE] > 0
    for seed in range(200):
        drawn = draw_intent(ctx(his_line=line, pushback=push, answer_first=True), Random(seed))
        assert drawn not in ANSWER_OFF
    # without the switch's fields the table is what it was
    old = weights(ctx(his_line=line))
    assert old[Intent.REASSURE_SEEK] > 0 and old[Intent.ASK] > 0


def test_his_bad_news_draws_no_tease_or_flirt_and_a_scene_no_reassurance():
    w = weights(ctx(his_line="my dad is in hospital", bad_news=True, answer_first=True))
    assert all(w[i] == 0 for i in BAD_NEWS_OFF) and w[Intent.SHARE] > 0
    assert weights(ctx(his_line="come here then", scene=True))[Intent.REASSURE_SEEK] == 0
    assert weights(ctx(his_line="come here then"))[Intent.REASSURE_SEEK] > 0


def test_a_held_want_is_not_drawn_again_and_is_the_only_one_offered():
    held = ctx(his_line="which is it lol", answer_first=True, hold=Intent.SHARE, last=Intent.SHARE)
    assert {draw_intent(held, Random(s)) for s in range(50)} == {Intent.SHARE}
    assert allowed(held) == (Intent.SHARE,)
    # a hold the turn no longer allows (answer-first) lets go
    gone = ctx(his_line="which is it lol", answer_first=True, hold=Intent.VENT)
    assert Intent.VENT not in {draw_intent(gone, Random(s)) for s in range(50)}
    assert len(allowed(gone)) > 1


# -- the pipeline: a whisper ------------------------------------------------------------------------

def rig_of(store, clock, path, **kw):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={}, words_per_line=[3, 14],
                opinions=["Pugs are fine for farm nights.",
                          "Wipes are how you learn a fight.",
                          "Decay is not optional; it is the law."])
    return make_rig(store, clock, path, **kw)


def model(beat, *lines):
    left = list(lines)

    def reply(model_name, messages):
        if beats.MARK in messages[-1]["content"]:
            return json.dumps(beat)
        return left.pop(0) if len(left) > 1 else left[0]
    return reply


def spy(r):
    seen, orig = [], r.engine._draw_situation

    def draw(turn, meta, p, now):
        orig(turn, meta, p, now)
        seen.append(meta)
    r.engine._draw_situation = draw
    return seen


def line_calls(r):
    return [c for c in r.chat_calls() if c["format"] is None]


def beat_calls(r):
    return [c for c in r.chat_calls() if c["format"] is not None]


def last_user(call):
    return call["messages"][-1]["content"]


def speak(r, turn, meta, seconds=200.0):
    async def go():
        task = asyncio.create_task(r.engine._speak(turn, meta))
        for _ in range(int(seconds)):
            if task.done():
                break
            await r.advance(1)
            await asyncio.sleep(0.002)
        return task.result()
    return r.sync(go())


def test_a_whisper_holds_her_want_and_shows_what_she_said(store, clock, tmp_path, caplog):
    r = rig_of(store, clock, tmp_path, intent=True, beat=True)
    try:
        seen = spy(r)
        r.llm.set_script(model(BEAT, "i have 7 wins on him and 9 losses",
                               "he is behind, i checked", "raid is at eight"))
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            r.whisper_from_him(102, "idk that duel sheet of his looked pretty legit")
            r.run()
        first = seen[-1]
        assert first.intent_ctx.pushback and first.intent_ctx.hold is None
        enum = beat_calls(r)[-1]["format"]["properties"]["want"]["enum"]
        assert not {i.value for i in ANSWER_OFF} & set(enum)
        assert "So far in this conversation" not in last_user(line_calls(r)[-1])

        # his short follow-up keeps her on it: the same want, the only one offered, and her
        # own tally in both requests
        r.whisper_from_him(102, "which is it lol")
        r.run()
        second = seen[-1]
        assert second.intent_ctx.hold == Intent(first.intent)
        assert beat_calls(r)[-1]["format"]["properties"]["want"]["enum"] == [first.intent]
        assert second.intent == first.intent
        for call in (beat_calls(r)[-1], line_calls(r)[-1]):
            assert 'you\'ve said: "i have 7 wins on him and 9 losses"' in last_user(call)
        assert last_user(line_calls(r)[-1]).rstrip().endswith("say that you do.")

        # a new subject lets the hold go
        r.whisper_from_him(102, "anyway what time is raid tonight")
        r.run()
        assert seen[-1].intent_ctx.hold is None
        assert len(beat_calls(r)[-1]["format"]["properties"]["want"]["enum"]) > 1
        assert "7 wins" not in caplog.text  # never logged
    finally:
        r.close()


def test_coherence_off_draws_and_shows_as_before(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True, beat=True, coherence=False)
    try:
        seen = spy(r)
        r.llm.set_script(model(BEAT, "i have 7 wins on him and 9 losses", "he is behind"))
        r.whisper_from_him(102, "idk that duel sheet of his looked pretty legit")
        r.run()
        r.whisper_from_him(102, "which is it lol")
        r.run()
        ctx2 = seen[-1].intent_ctx
        assert not ctx2.answer_first and ctx2.hold is None and not ctx2.pushback
        assert seen[-1].position == ""
        assert "So far in this conversation" not in last_user(line_calls(r)[-1])
    finally:
        r.close()


def test_a_rule_on_her_card_leads_it_and_is_never_argued_against(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True, beat=True)
    try:
        r.llm.set_script(model(BEAT, "ten stays, sorry"))
        r.whisper_from_him(102, "thinking of dropping the decay from 10 to 5 percent, thoughts")
        r.run()
        call = line_calls(r)[-1]
        system = call["messages"][0]["content"]
        assert "Opinions: Decay is not optional; it is the law." in system
        assert ('You hold this as a rule: "Decay is not optional; it is the law." You never '
                "argue against it.") in last_user(call)
        # a line that touches no opinion shows her first two, as ever
        r.whisper_from_him(102, "anyone seen my hat")
        r.run()
        system = line_calls(r)[-1]["messages"][0]["content"]
        assert "Opinions: Pugs are fine for farm nights. | Wipes are how you learn a fight." in system
    finally:
        r.close()


def test_the_card_keeps_a_touched_opinion_when_the_budget_is_tight(store):
    seed_test_cast(store)
    long = "x" * 140
    save_member(store, 102, opinions=["Pugs are fine " + long, "Wipes teach " + long,
                                      "Decay is not optional; it is the law."])
    p = PersonaCache(store).get(102)
    plain = render_prompt_card(p, race="Gnome", class_name="Mage")
    touched = render_prompt_card(p, race="Gnome", class_name="Mage", topic="drop the decay?")
    assert render_prompt_card(p, race="Gnome", class_name="Mage", topic="") == plain
    assert "Decay is not optional" in touched
    assert "Decay is not optional" not in plain


# -- the pipeline: his DM ---------------------------------------------------------------------------

def dm_turn(r, text, earlier=()):
    now = r.clock.now()
    mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human", text=text, now=now)
    rows = tuple(ChatLineRow(900 + i, now - 300 + i, "dm:102", guid, "x", to, line, 0, 0, None,
                             None, "mind" if guid == 102 else "human")
                 for i, (guid, to, line) in enumerate(earlier))
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
    meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None), discord_chat=rows)
    return turn, meta


def test_a_dm_shows_what_she_said_and_takes_bad_news_gently(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True, beat=True)
    try:
        seen = spy(r)
        r.llm.set_script(model(BEAT, "oh no, is he alright"))
        turn, meta = dm_turn(r, "bad news, my dad is in hospital since this morning",
                             earlier=[(102, HUMAN, "im on the late shift till 10 tonight"),
                                      (HUMAN, 102, "ok")])
        assert speak(r, turn, meta)
        assert meta.intent_ctx.bad_news
        assert 'you\'ve said: "im on the late shift till 10 tonight"' in meta.position
        assert meta.position in last_user(line_calls(r)[-1])
        assert meta.position in last_user(beat_calls(r)[-1])
        enum = beat_calls(r)[-1]["format"]["properties"]["want"]["enum"]
        assert not {i.value for i in BAD_NEWS_OFF} & set(enum)
        assert seen[-1] is meta
    finally:
        r.close()


def test_an_explicit_scene_brings_nothing_in(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", dial=10, typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    for coherence in (True, False):
        r = make_rig(store, clock, tmp_path, intent=True, beat=True, coherence=coherence)
        try:
            r.llm.set_script(model({**BEAT, "want": "flirt"}, "mm come closer then"))
            turn, meta = dm_turn(r, "you looked lovely in that green robe tonight",
                                 earlier=[(HUMAN, 102, "the tram depot was packed today"),
                                          (HUMAN, 102, "and the key went missing again")])
            assert speak(r, turn, meta)
            ask = beat_calls(r)[-1]
            offered = ask["format"]["properties"]["use"]["type"]
            assert (offered == "null") is coherence
            assert meta.intent_ctx.scene is coherence
        finally:
            r.close()
