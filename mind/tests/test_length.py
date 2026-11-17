"""[talk] coherence, the length rule (talk/length.py): a draft that is only too long is cut in
code where the cut keeps the answer, sent as written within twice its words, and only past
that asked again with the draft itself. Invented lines; pure functions and the test cast."""
import asyncio

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.testing.fake_llm import script_from
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.length import (KEEP_SLACK, LONG_SLACK, carries, content_question,
                                     fit_answer)
from leeroy_mind.talk.shape import CONTENT_WORDS

LORE_Q = "nerd question, who ruled the ogres before the war"
LORE_DRAFT = ("it was Gorak. thats why the whole clan has this frozen vibe to it honestly, he "
              "sat in that keep for years and nobody dared climb up there")


@pytest.fixture
def clock():
    return engine_clock()


def run(coro):
    return asyncio.run(coro)


def his_whisper(r, text):
    ev = Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=102,
              text=text, map=0, zone=10)
    lid = r.store.insert_chat_line(ev, session_id=None, origin="human")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def answer(row=None):
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="whisper", addressee=HUMAN,
                to_guid=HUMAN, trigger_line=row.id if row is not None else None)


async def spoken(r, turn, seconds=40):
    task = asyncio.create_task(r.engine.speak(turn))
    await r.advance(seconds, 1.0)
    assert task.done()
    return task.result()


def test_a_reply_within_its_slack_is_left_alone():
    text = "one two three four five six seven eight nine ten"
    assert fit_answer(text, 8).action == "fits" and fit_answer(text, 8).text == text
    assert fit_answer(text, 0).action == "fits"  # no limit: nothing to fit


def test_a_long_answer_is_cut_to_the_clause_that_answers_him():
    fit = fit_answer(LORE_DRAFT, 8, LORE_Q)
    assert (fit.action, fit.text) == ("cut", "it was Gorak.")
    assert len(fit.text.split()) <= LONG_SLACK * 8


def test_a_cut_never_ends_on_a_dangling_word_and_keeps_a_yes():
    draft = ("lol yeah, i think so but only because the tank keeps pulling extra packs and the "
             "healers go oom every single time we try")
    fit = fit_answer(draft, 8, "you coming tonight?")
    assert fit.action == "cut" and fit.text.startswith("lol yeah")
    assert fit.text.split()[-1] not in ("but", "only", "because", "and", "the")


def test_a_reply_with_no_clean_cut_goes_as_written_within_twice_its_words():
    draft = "one two three four five six seven eight nine ten eleven twelve thirteen"
    fit = fit_answer(draft, 8, "what trinkets you running")
    assert (fit.action, fit.text) == ("kept", draft)
    assert len(draft.split()) <= KEEP_SLACK * 8


def test_a_cut_that_would_lose_the_answer_is_not_made():
    # the opener is filler: on a content question it carries nothing, so it is no cut
    draft = "haha oh man, " + " ".join(f"w{i}" for i in range(30))
    assert fit_answer(draft, 6, "who was it").action == "retry"
    # past twice its words with a sentence that answers inside that room: cut there
    late = "haha oh man. " + " ".join(f"w{i}" for i in range(8)) + ". " + " ".join(["more"] * 20)
    fit = fit_answer(late, 6, "who was it")
    assert fit.action == "kept" and fit.text.endswith("w7.")


def test_carries_reads_his_question():
    assert carries(["yeah"], "you around?")
    assert not carries(["yeah"], "who tanks tonight")
    assert not carries(["lol"], "")
    assert carries(["no", "idea"], "who tanks tonight")
    assert carries(["at", "8"], "when is raid")
    assert not carries(["who", "tanks", "tonight"], "who tanks tonight")  # his words back


def test_content_question():
    assert content_question("what trinkets you running") and content_question("explain it")
    assert content_question("so tell me about the fight")
    assert not content_question("?") and not content_question("you around?")
    assert not content_question("nice one") and not content_question("")


def test_a_long_whisper_answer_is_cut_and_never_asked_again(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={}, words_per_line=[2, 8], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([LORE_DRAFT, "it was the other"]))
        said = await spoken(r, answer(his_whisper(r, LORE_Q)))
        assert len(r.chat_calls()) == 1
        assert said == ["it was gorak"]
    run(main())


def test_a_draft_far_too_long_with_no_cut_is_asked_again_with_itself(store, clock, tmp_path):
    draft = " ".join(f"w{i}" for i in range(40))
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={}, words_per_line=[1, 4])
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "short one"]))
        assert await spoken(r, answer()) == ["short one"]
        assert len(r.chat_calls()) == 2
        again = r.prompts()[1]
        assert "Say the same thing shorter" in again and "w0 w1 w2" in again
        assert "Shorter: 1-4 words" not in again
    run(main())


def test_with_coherence_off_the_long_tell_asks_afresh(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, words_per_line=[1, 4])
        r = make_rig(store, clock, tmp_path, script=script_from([LORE_DRAFT, "gorak"]),
                     coherence=False)
        assert await spoken(r, answer()) == ["gorak"]
        assert "Shorter: 1-4 words" in r.prompts()[1]
    run(main())


def test_the_shape_gives_a_content_answer_room(store):
    from random import Random

    from leeroy_mind.talk.shape import Shape, ShapeContext, _sized
    seed_test_cast(store)
    save_member(store, 102, words_per_line=[1, 4], split_rate=0.0)
    typing = PersonaCache(store).get(102).typing
    base = dict(family="whisper", kind=TurnKind.REPLY_HUMAN, owed=True, intent=None,
                typing=typing, attention=None, state=None, late_by=0.0, his_words=5,
                discord_words=90, dm_words=200, dm_mirror=5, realism=False, two_lines=False)
    plain = _sized(Shape.SHORT, ShapeContext(**base), Random(1))
    raised = _sized(Shape.SHORT, ShapeContext(**base, content=True), Random(1))
    assert plain.words <= 4 and not plain.content
    assert raised.words == CONTENT_WORDS and raised.content
