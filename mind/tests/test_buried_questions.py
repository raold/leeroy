"""His question buried in his line with no question mark (blind sandbox test, 2026-11-20:
"...i have not eaten yet so did you eat yet" and "you ever been out west" were read as no
question, so the length rule cut the answer and nothing told her to answer him first).
talk/asking.py's `buried` reading; talk/length.py's `asks`, which always has it; the pipeline's
readings, which have it with [talk] coherence. Invented lines; the test cast."""
import asyncio

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.protocol import Chat
from leeroy_mind.talk import length, sofar
from leeroy_mind.talk.asking import asks, buried_question, is_question
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import script_from

BURIED = [
    "that was rough, you ever been out west",
    "you ever been out west",
    "work was brutal today, did you have lunch yet",
    "man that sounds like a nightmare but at least i dont have a manager breathing down my "
    "neck while i am on shift today and i have not eaten yet so did you eat yet",
    "long day at work and have you eaten",
    "same old here, what about you",
    "dinner was great and are you working tonight",
    "long day lol you free tonight",
    "im good, you",
    "im fine and u",
    "pizza for dinner, wbu",
    "tired tbh how about you",
    "raid was fun but how was work",
    "i saw the new mount and is it worth the grind",
    "we wiped twice anyway when does it start tomorrow",
    "back from the gym, you eaten yet",
    "just got home, you still at work",
    "rough night, you ok",
    "heard about the breakup, you still ok",
    "big day for you, you doing well",
    "rain all day here, hows the weather there",
    "my sister visited and whats new with you",
    "the boss was hard but was it worth it",
    "guild meeting thursday, can you make it",
    "i had pizza, did you",
    "heading out soon, wanna raid later",
    "got home late, any plans tonight",
    "had a long shift, how hard was yours",
    "am i late",
    "busy day so far and you finished work yet",
    "sorry for the wall of text lol anyone up for heroics later",
    "rough day here, got a sec",
]
STATEMENTS = [
    "that pull was rough",
    "i went out and when i got home the cat was gone",
    "i got home and did the dishes",
    "i love raiding and so do you",
    "you did great tonight",
    "you still owe me gold",
    "i know what you mean",
    "you are the best tank we have",
    "work was brutal today, my manager stacked three meetings back to back",
    "i have not eaten yet",
    "you killed it last night",
    "see ya tomorrow",
    "i told him where we were going",
    "ok im out",
    "i wonder how long it takes",
    "gotta run, talk later",
    "you know how it is",
    "nice one",
    "you got this",
    "anyway i went home and was asleep by ten",
    "you been great tonight",
    "i said i would fix it and did it",
    "you havent seen it yet",
    "you",
    "thank you so much",
    "got a new mount today",
    "any time, happy to help",
    "neither do i",
    "i miss you and i love you",
    "that was what i needed",
    "the healer was late, so was the tank",
    "i did well today and so did you",
]


@pytest.mark.parametrize("line", BURIED)
def test_a_question_buried_without_a_question_mark_is_a_question(line):
    assert is_question(line, buried=True) and asks(line, buried=True)
    assert length.asks(line)  # the length rule never cuts the answer to it


@pytest.mark.parametrize("line", STATEMENTS)
def test_a_statement_is_no_question(line):
    assert not buried_question(line)
    assert not is_question(line, buried=True) and not asks(line, buried=True)
    assert not length.asks(line)


# his lines with line breaks (a Discord post): a break ends a clause (review of #387, Codex P2)
MULTI_LINE = [
    "long day at work\ndid you eat yet",
    "the raid went fine\nyou ever been out west",
    "im good\nyou",
    "rough night\r\nyou ok",
    "pizza for dinner\n\nwbu",
    "busy busy   today\n  are you working tonight  ",
]
MULTI_STATEMENTS = [
    "long day at work\nim starving",
    "you\ndid great tonight",
    "i got home\nand did the dishes",
    "see ya\ntomorrow",
]


@pytest.mark.parametrize("line", MULTI_LINE)
def test_a_question_on_its_own_line_is_a_question(line):
    assert buried_question(line) and is_question(line, buried=True)
    assert asks(line, buried=True) and length.asks(line)


@pytest.mark.parametrize("line", MULTI_STATEMENTS)
def test_a_statement_over_several_lines_is_no_question(line):
    assert not buried_question(line) and not is_question(line, buried=True)
    assert not length.asks(line)


def test_his_lines_keep_their_breaks_through_the_readers():
    """Each reader takes his text as it came: the waiting lines' instruction, the So far
    block's open questions and the length rule's content question."""
    from leeroy_mind.human import HumanIdentity
    from leeroy_mind.talk.prompt import UNANSWERED_OLDEST, unanswered_text
    me = HumanIdentity(HUMAN, "Testmage", "Testy", "", True, pronouns="he")
    first = unanswered_text(["ok so", "long day at work\ndid you eat yet"], me)
    assert "his question first" in first and UNANSWERED_OLDEST not in first
    assert UNANSWERED_OLDEST in unanswered_text(["ok so", "long day at work\nim starving"], me)
    lines = [sofar.Line(1, 10.0, False, "hey"),
             sofar.Line(2, 20.0, True, "long day at work\ndid you eat yet"),
             sofar.Line(3, 30.0, True, "anyway")]
    assert sofar.open_questions(lines, skip=3, buried=True) == ["did you eat yet"]
    assert sofar.open_questions(lines, skip=3) == []  # off: as before
    assert length.content_question("long day\nwhat did you eat")


def test_off_the_reading_is_as_before():
    """Without `buried` nothing changes: the line-start reading only."""
    assert not is_question("that was rough, you ever been out west")
    assert not asks("work was brutal today so did you eat yet")
    assert is_question("did you eat yet") and is_question("hows work")
    assert len(BURIED) >= 20 and len(STATEMENTS) >= 20


def test_his_buried_question_is_still_owed_in_so_far():
    lines = [sofar.Line(1, 10.0, False, "hey"),
             sofar.Line(2, 20.0, True, "long day at work so did you eat yet"),
             sofar.Line(3, 30.0, True, "anyway the raid went fine")]
    assert sofar.open_questions(lines, skip=3) == []
    assert sofar.open_questions(lines, skip=3, buried=True) == [
        "long day at work so did you eat yet"]


# -- the pipeline: with [talk] coherence his buried question is his question -----------------

def _prompt(store, tmp_path, coherence):
    clock = engine_clock()
    seed_test_cast(store)
    save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0, abbreviations={},
                words_per_line=[2, 12], case="lower", end_punct="none")
    r = make_rig(store, clock, tmp_path / str(coherence), coherence=coherence,
                 script=script_from(["not yet, crisps from the machine", "other"]))
    try:
        ev = Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=102,
                  text="work was brutal today so did you eat yet", map=0, zone=10)
        lid = r.store.insert_chat_line(ev, session_id=None, origin="human")
        row = r.store.chat_lines([lid])[0]
        r.world.add_line(row, r.clock.now())
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="whisper", addressee=HUMAN,
                    to_guid=HUMAN, trigger_line=row.id)

        async def main():
            task = asyncio.create_task(r.engine.speak(turn))
            await r.advance(60, 1.0)
            return task.result()
        said = asyncio.run(main())
        return said, r.prompts()[0]
    finally:
        r.close()


@pytest.mark.parametrize("coherence", [True, False])
def test_with_coherence_she_answers_his_buried_question_first(store, tmp_path, coherence):
    said, prompt = _prompt(store, tmp_path, coherence)
    assert said
    assert ("question first, plainly" in prompt) is coherence
