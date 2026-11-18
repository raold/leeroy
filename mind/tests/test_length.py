"""[talk] coherence, the length rule (talk/length.py): an answer to a line of his that asks
anything is never cut (sent as written when it fits her lines, else asked again with the draft,
then laid on up to three lines or sent by its tail); a line that asks nothing keeps its leading
clauses. Invented lines (length_drafts.py holds the reviews' 92 drafts); the test cast."""
import asyncio

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import engine_clock, save_member
from engine_rig import make_rig as _make_rig


def make_rig(*args, **kw):
    kw.setdefault("coherence", True)  # the switch under test; a test of "off" says so
    return _make_rig(*args, **kw)
from leeroy_mind.testing.fake_llm import script_from
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.length import (KEEP_SLACK, LINE_BYTES, LONG_SLACK, Fit, asks,
                                     content_question, fit_answer, split_lines, tail_lines)
from length_drafts import ADVERSARIAL, DRAFTS, FRESH
from leeroy_mind.talk.shape import CONTENT_WORDS

LORE_Q = "nerd question, who ruled the ogres before the war"
LORE_DRAFT = ("it was Gorak. thats why the whole clan has this frozen vibe to it honestly, he "
              "sat in that keep for years and nobody dared climb up there")


pytestmark = pytest.mark.usefixtures("coherent")  # [talk] coherence is off by default


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


def test_an_answer_to_a_line_of_his_that_asks_is_never_cut():
    for his, draft in [
            ("how many people signed up?", "We have 22 now. The calendar looked thin this "
                                           "morning and most of them are alts anyway"),
            (LORE_Q, LORE_DRAFT),
            ("are you coming tonight?", "oh no not this again lol. yeah i'll be there right "
                                        "after dinner with the kids"),
            ("when does it start", "we wiped 4 times last week because people were late. it "
                                   "starts right after the officers meeting"),
            ("so explain the fight to me", "first you stack on the tank and then you run out "
                                           "when the circle shows up under your feet"),
            ("ok. so?", " ".join(["word"] * 60))]:
        for limit in (3, 6, 8, 12):
            assert fit_answer(draft, limit, his) in (Fit(draft, "kept"), Fit(draft, "fits"))
    assert asks("you around?") and asks("so explain it") and asks("ok? fine")
    assert not asks("that pull was rough") and not asks("")


@pytest.mark.parametrize("limit", [6, 8, 10, 12, 15])
def test_no_line_that_goes_out_is_without_its_answer(limit):
    """The reviews' 92 invented drafts (tests/length_drafts.py): 40 with the answer in the
    second or third clause, 32 adversarial ones (a false proof in the lead-in, the answer in
    the last clause, lower-case names, two questions, quotes, an emoji tail) and 20 fresh ones
    (the answer split across two sentences; a number, clock time or quoted yes in the lead-in;
    answers with no token at all). The first rule lost 34 of 37 cuts at 12 words, the second
    18, 20, 17 and 6 of the 32, the third 17, 16, 17 and 7 of the 20. Now an answer to a line
    of his that asks is never cut: none of the 92 goes out without its answer at any size."""
    lost = []
    rows = (*DRAFTS, *ADVERSARIAL, *FRESH)
    for his, draft, mark, kind in rows:
        fit = fit_answer(draft, limit, his)
        assert fit.text == draft and fit.action in ("fits", "kept")
        marks = mark if isinstance(mark, tuple) else (mark,)
        if not all(m.casefold() in fit.text.casefold() for m in marks):
            lost.append((kind, his))
    assert len(rows) == 92 and lost == []


def test_a_cut_never_ends_inside_a_quote_on_a_dangling_word_or_under_three_words():
    quoted = ('he said "eight, maybe nine, we will see" and then left, the rest of the night '
              "was a mess and nobody knew who was leading at all")
    fit = fit_answer(quoted, 8, "raid was late again")
    assert fit.text.count('"') % 2 == 0
    draft = ("yes but only because the tank keeps pulling extra packs and the healers go oom "
             "every single time we try it that way")
    fit = fit_answer(draft, 8, "the tank is fine")
    assert not fit.text.endswith(("only", "but", "the", "and"))
    short = "No, " + " ".join(f"w{i}" for i in range(30))
    assert fit_answer(short, 6, "he is tauntable").action == "retry"  # never a one-word cut


def test_a_line_that_asks_nothing_with_no_clean_cut_goes_as_written_within_twice_its_words():
    draft = "one two three four five six seven eight nine ten eleven twelve thirteen"
    fit = fit_answer(draft, 8, "nice trinkets")
    assert (fit.action, fit.text) == ("kept", draft)
    assert len(draft.split()) <= KEEP_SLACK * 8
    assert fit_answer(draft + " " + draft, 8, "nice trinkets").action == "retry"


def test_a_line_that_answers_no_question_keeps_its_leading_clauses():
    draft = "lol that pull was a mess, we should have waited for the healers and the tank"
    fit = fit_answer(draft, 5)
    assert fit.action == "cut" and fit.text == "lol that pull was a mess"
    # to a remark of his: as many leading clauses as fit, never only the one that repeats him
    reply = ("yeah the pull was rough, the healers were out of range the whole time, and "
             "honestly the tank should have waited for the mark before charging in like that")
    fit = fit_answer(reply, 12, "that pull was rough")
    assert fit.text == "yeah the pull was rough, the healers were out of range the whole time"


def test_an_opener_or_item_is_cut_only_after_the_clause_that_names_its_subject():
    draft = ("so i was sorting my bags all morning, such a chore honestly, and the flask recipe "
             "finally dropped for me. going to make a batch for sunday and maybe sell the rest")
    fit = fit_answer(draft, 10, subject="the flask recipe she was farming")
    assert fit.action in ("cut", "kept") and "flask recipe" in fit.text
    # a draft that never names it is not cut: nothing proves where the item is
    other = "so i was sorting my bags all morning, such a chore honestly, " + "and more " * 12
    assert fit_answer(other, 10, subject="the flask recipe").action == "retry"


def test_tail_lines_keeps_the_end_by_whole_clauses():
    long = ". ".join(f"sentence number {i} goes on for quite a while here you see" for i in range(30))
    assert split_lines(long, 3) is None
    tail = tail_lines(long, 3)
    assert tail and len(tail) <= 3 and all(len(x.encode()) <= LINE_BYTES for x in tail)
    joined = " ".join(tail)
    assert long.endswith(joined) and joined.startswith("sentence number")  # a sentence's start
    assert "sentence number 29" in joined and "sentence number 0 " not in joined
    assert tail_lines("see you at eight", 3) == ["see you at eight"]
    assert tail_lines("w " * 300, 3) is None  # not even its last clause fits a line


def test_split_lines_keeps_whole_clauses_within_the_bridges_bytes():
    short = "see you at eight, bring flasks"
    assert split_lines(short, 1) == [short]
    long = ", ".join(f"clause number {i} goes on for a while here" for i in range(12))
    assert len(long.encode()) > 2 * LINE_BYTES // 2 and split_lines(long, 1) is None
    two = split_lines(long, 2)
    assert two is None or (len(two) == 2 and " ".join(two) == long)
    fits = ", ".join(f"clause number {i} goes on for a while here" for i in range(8))
    parts = split_lines(fits, 2)
    assert parts is not None and len(parts) == 2 and " ".join(parts) == fits
    assert all(len(x.encode()) <= LINE_BYTES for x in parts)
    assert split_lines("w " * 200, 2) is None  # one clause with no break


def test_content_question():
    assert content_question("what trinkets you running") and content_question("explain it")
    assert content_question("so tell me about the fight")
    assert content_question("nerd question, who ruled the ogres before the war")
    assert not content_question("?") and not content_question("you around?")
    assert not content_question("nice one") and not content_question("")


def test_a_long_whisper_answer_goes_as_written_and_is_never_asked_again(store, clock,
                                                                        tmp_path):
    draft = ("we have 22 now. the calendar looked thin this morning and most of them are alts "
             "anyway so who knows what we end up with on the night")
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={}, words_per_line=[2, 8], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "it was the other"]))
        said = await spoken(r, answer(his_whisper(r, "how many people signed up?")))
        assert len(r.chat_calls()) == 1 and said == [draft]
    run(main())


def test_a_long_remark_is_cut_to_its_leading_clauses(store, clock, tmp_path):
    draft = ("yeah it was a mess, the healers were out of range the whole time, and "
             "honestly the tank should have waited for the mark before charging in like that")
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={}, words_per_line=[2, 10], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "other"]))
        said = await spoken(r, answer(his_whisper(r, "last night went badly")))
        assert len(r.chat_calls()) == 1
        assert said and said[0].startswith("yeah it was a mess") and "tank" not in " ".join(said)
    run(main())


FILL = ("honestly that place is a real maze and i walked around it for ages last week with "
        "the whole group behind me complaining loudly, then we stopped twice for the warlock "
        "who kept falling off the ledge near the bridge, and after that the tank went the "
        "wrong way round the frozen lake while everyone else waited by the big gate arguing "
        "about the map, and the healer kept saying we should have taken the flight path from "
        "the dwarven camp instead of riding all the way round the mountain in the snow, which "
        "in fairness was a good point that nobody wanted to hear at that hour of the "
        "night").split()
TAIL = "it is in storm peaks north of the temple"


def buried(words):
    return " ".join(FILL[:words - len(TAIL.split())]).rstrip(",") + ", " + TAIL


@pytest.mark.parametrize("words", [60, 80, 100])
@pytest.mark.parametrize("split_rate", [0.0, 0.5])
@pytest.mark.parametrize("cap", [8, 25])
def test_a_model_that_ignores_say_it_shorter_never_costs_him_the_answer(store, clock, tmp_path,
                                                                        words, split_rate, cap):
    """Re-review round 2 of #370: when the retry was as long as the first draft the turn fell
    back to the byte trim and lost the answer (76 of 79 turns). An answer he is owed is now laid
    on up to three game lines by whole clauses, nothing dropped, or sent by its tail; never its
    head. The model here answers every call with the same long draft."""
    draft = buried(words)
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=split_rate,
                    abbreviations={}, words_per_line=[2, cap], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, reply=draft)
        said = await spoken(r, answer(his_whisper(r, "where is the entrance to ulduar?")),
                            seconds=400)
        assert said and "storm peaks" in said[-1] and len(said) <= 3
        assert all(len(x.encode()) <= 255 for x in said)
        assert len(r.chat_calls()) <= 2  # asked again once, with the draft
        if len(draft.encode()) <= 3 * LINE_BYTES - 40:
            assert " ".join(said).replace(",", "").split() == draft.replace(",", "").split()
    run(main())


@pytest.mark.parametrize("words", [60, 80, 100])
@pytest.mark.parametrize("split_rate", [0.0, 0.5])
def test_a_long_draft_with_the_answer_last_never_loses_it(store, clock, tmp_path, words,
                                                          split_rate):
    """Re-review of #370: a draft "kept as written" on one line was trimmed word by word at the
    bridge's 255 bytes, and an answer in the last clause went with the tail. What is sent now
    holds the answer: split on her lines by whole clauses when it fits them, else asked again
    with the draft."""
    draft = buried(words)
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=split_rate,
                    abbreviations={}, words_per_line=[5, 25], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "ah, " + TAIL]))
        said = await spoken(r, answer(his_whisper(r, "where is the entrance to ulduar?")),
                            seconds=400)
        assert said and "storm peaks" in " ".join(said)
        assert all(len(x.encode()) <= 255 for x in said)
    run(main())


def test_a_draft_of_exactly_her_words_keeps_its_last_clause(store, clock, tmp_path):
    """Re-review round 2: a draft of exactly the turn's words "fit", the styling packed it on
    two lines and dropped the last clause."""
    draft = buried(50)
    assert len(draft.split()) == 50
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=1.0,
                    abbreviations={}, words_per_line=[5, 25], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "ah, " + TAIL]))
        said = await spoken(r, answer(his_whisper(r, "where is the entrance to ulduar?")),
                            seconds=400)
        assert said and "storm peaks" in " ".join(said)
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


# -- her voice sheet's line (personas/speech.py word_range) ------------------------------------

def test_a_cut_never_leaves_fewer_words_than_her_sheets_range():
    draft = "We have 22 now, the calendar looked thin this morning, most of them are alts anyway"
    said = "signups look bad"
    assert fit_answer(draft, 5, said).text == "We have 22 now"
    # a sheet that names six words or more: the four-word clause alone is no cut
    fit = fit_answer(draft, 5, said, floor=6)
    assert len(fit.text.split()) >= 6 and "22" in fit.text


def test_the_content_floor_never_lifts_a_line_past_her_sheets_range(store):
    from random import Random

    from leeroy_mind.talk.shape import Shape, ShapeContext, _sized
    seed_test_cast(store)
    typing = PersonaCache(store).get(102).typing
    base = dict(family="whisper", kind=TurnKind.REPLY_HUMAN, owed=True, intent=None,
                typing=typing, attention=None, state=None, late_by=0.0, his_words=5,
                discord_words=90, dm_words=200, dm_mirror=5, realism=False, two_lines=False,
                content=True)
    sheeted = _sized(Shape.SHORT, ShapeContext(**base, voice_words=(3, 7)), Random(1))
    assert 3 <= sheeted.words <= 7  # "three to seven words": never "about 12 words"
    roomy = _sized(Shape.SHORT, ShapeContext(**base, voice_words=(8, 20)), Random(1))
    assert CONTENT_WORDS <= roomy.words <= 20
    # a DM keeps its own length (the mirror and the short-line cap): never raised here
    dm = _sized(Shape.SHORT, ShapeContext(**{**base, "family": "dm", "his_words": 3}), Random(1))
    assert not dm.content and dm.words <= 25


def test_a_line_inside_her_sheets_range_is_never_long(store, clock, tmp_path):
    from sheets import GRUFF, give
    reply = "the allotment flooded again and i spent the whole morning bailing it out with a bucket"
    async def main():
        seed_test_cast(store)
        give(store, 102, {**GRUFF, "shape": "long rambling lines, twelve to eighteen words"},
             typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0, abbreviations={},
             words_per_line=[2, 6], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([reply, "short"]))
        said = await spoken(r, answer(his_whisper(r, "how was your morning")))
        assert len(r.chat_calls()) == 1 and len(" ".join(said).split()) == len(reply.split())
    run(main())
