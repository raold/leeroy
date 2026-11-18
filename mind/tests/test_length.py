"""[talk] coherence, the length rule (talk/length.py): a game line that is only too long loses
only what follows its answering sentence, goes as written within twice its words when nothing
proves where the answer is, and only past that is asked again with the draft itself. Invented
lines (length_drafts.py holds the review's 40 buried-answer drafts); the test cast."""
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
from leeroy_mind.talk.length import (KEEP_SLACK, LINE_BYTES, LONG_SLACK, Fit, answering, carries,
                                     content_question, fit_answer, split_lines)
from length_drafts import ADVERSARIAL, DRAFTS
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


def test_a_long_answer_loses_only_what_follows_its_last_proof():
    # a number answers "how many"; what follows its sentence goes
    fit = fit_answer("We have 22 now. The calendar looked thin this morning and most of them "
                     "are alts anyway", 8, "how many people signed up?")
    assert fit.action == "cut" and fit.text.startswith("We have 22 now.")
    assert len(fit.text.split()) <= LONG_SLACK * 8 and "alts" not in fit.text
    # the LAST clause with a number is the answer: a number in the lead-in proves nothing
    late = ("I counted 3 times to be sure. It was looking thin all week honestly. "
            "We have 22 now. Bring food.")
    fit = fit_answer(late, 10, "how many people signed up?")
    assert "22" in fit.text
    # a who, what, where, why or how answer is never cut: a name proves nothing on its own
    assert fit_answer(LORE_DRAFT, 8, LORE_Q) == Fit(LORE_DRAFT, "retry")
    assert fit_answer(LORE_DRAFT, 16, LORE_Q) == Fit(LORE_DRAFT, "kept")
    # nor is an answer to two questions
    two = "yes i'm in for sure. it starts at eight i think, so bring flasks and food for all"
    assert fit_answer(two, 6, "are you coming? and what time does it start?").text == two
    # a lead-in "no" before the real "yeah" never goes out as the opposite answer
    opp = "oh no not this again lol. yeah i'll be there right after dinner with the kids"
    assert "yeah" in fit_answer(opp, 6, "are you coming tonight?").text


@pytest.mark.parametrize("limit", [6, 8, 10, 12, 15])
def test_no_line_that_goes_out_is_without_its_answer(limit):
    """The reviews' 72 invented drafts (tests/length_drafts.py): 40 with the answer in the
    second or third clause, 32 adversarial ones (a false proof in the lead-in, the answer in
    the last clause, lower-case names, two questions, quotes, an emoji tail). The first rule
    lost 34 of 37 cuts at 12 words; the second lost 18, 20, 17 and 6 of the 32 at 6, 8, 10 and
    12. Now none of the 72 goes out, cut or as written, without its answer at any size; the
    rest are asked again with the draft."""
    counts = {"fits": 0, "cut": 0, "kept": 0, "retry": 0}
    lost = []
    for his, draft, mark, kind in (*DRAFTS, *ADVERSARIAL):
        fit = fit_answer(draft, limit, his)
        counts[fit.action] += 1
        marks = mark if isinstance(mark, tuple) else (mark,)
        if fit.action != "retry" and not all(m.casefold() in fit.text.casefold() for m in marks):
            lost.append((kind, his))
    assert sum(counts.values()) == 72 and lost == []


def test_a_cut_never_ends_inside_a_quote_on_a_dangling_word_or_under_three_words():
    quoted = ('he said "eight, maybe nine, we will see" and then left. the rest of the night '
              "was a mess and nobody knew who was leading at all")
    fit = fit_answer(quoted, 8, "when does the raid start?")
    assert fit.text.count('"') % 2 == 0
    # a bare yes is no cut: the sentence it starts goes with it
    draft = ("yes but only because the tank keeps pulling extra packs and the healers go oom "
             "every single time we try it that way")
    fit = fit_answer(draft, 8, "you coming tonight?")
    assert fit.action == "retry" and fit.text == draft
    short = "No. " + " ".join(f"w{i}" for i in range(30))
    assert fit_answer(short, 6, "is he tauntable?").action == "retry"  # never a one-word cut


def test_a_reply_with_no_provable_answer_goes_as_written_within_twice_its_words():
    draft = "one two three four five six seven eight nine ten eleven twelve thirteen"
    fit = fit_answer(draft, 8, "what trinkets you running")
    assert (fit.action, fit.text) == ("kept", draft)
    assert len(draft.split()) <= KEEP_SLACK * 8
    assert fit_answer(draft + " " + draft, 8, "what trinkets you running").action == "retry"


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


def test_answering_is_never_vacuous():
    q = "who tanks tonight"
    assert answering(["lol"], "") and answering(["nice", "one"], "good raid")  # nothing to prove
    assert not answering(["yeah"], q) and not answering("it was Gorak".split(), q)
    assert not answering(["no", "idea"], q)  # a who question: no clause proves its answer
    assert not carries("yeah i can. let me think about it".split(), "explain the fight")
    assert answering(["at", "8"], "when is raid") and not answering(["at", "8"], q)
    assert answering(["yeah"], "you around?") and not answering(["hmm"], "you around?")
    assert not answering(["yeah"], "are you coming? and when?")  # two questions: never
    assert answering("the flask is ready".split(), "", "your flask")
    assert not answering("nice weather".split(), "", "your flask")
    assert carries("hmm let me see. we start at eight".split(), "when do we start")


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


def test_a_long_whisper_answer_is_cut_and_never_asked_again(store, clock, tmp_path):
    draft = ("we have 22 now. the calendar looked thin this morning and most of them are alts "
             "anyway so who knows what we end up with on the night")
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={}, words_per_line=[2, 8], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "it was the other"]))
        said = await spoken(r, answer(his_whisper(r, "how many people signed up?")))
        assert len(r.chat_calls()) == 1
        assert said == ["we have 22 now. the calendar looked thin this morning"]
    run(main())


@pytest.mark.parametrize("words", [60, 80, 100])
@pytest.mark.parametrize("split_rate", [0.0, 0.5])
def test_a_long_draft_with_the_answer_last_never_loses_it(store, clock, tmp_path, words,
                                                          split_rate):
    """Re-review of #370: a draft "kept as written" on one line was trimmed word by word at the
    bridge's 255 bytes, and an answer in the last clause went with the tail. What is sent now
    holds the answer: split on her lines by whole clauses when it fits them, else asked again
    with the draft."""
    tail = "it is in storm peaks north of the temple"
    fill = ("honestly that place is a real maze and i walked around it for ages last week with "
            "the whole group behind me complaining loudly, then we stopped twice for the warlock "
            "who kept falling off the ledge near the bridge, and after that the tank went the "
            "wrong way round the frozen lake while everyone else waited by the big gate arguing "
            "about the map, and the healer kept saying we should have taken the flight path from "
            "the dwarven camp instead of riding all the way round the mountain in the snow, which "
            "in fairness was a good point that nobody wanted to hear at that hour of the "
            "night").split()
    draft = " ".join(fill[:words - len(tail.split())]).rstrip(",") + ", " + tail
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=split_rate,
                    abbreviations={}, words_per_line=[5, 25], case="lower", end_punct="none")
        r = make_rig(store, clock, tmp_path, script=script_from([draft, "ah, " + tail]))
        said = await spoken(r, answer(his_whisper(r, "where is the entrance to ulduar?")),
                            seconds=400)
        assert said and "storm peaks" in " ".join(said)
        assert all(len(x.encode()) <= 255 for x in said)
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
    draft = "We have 22 now. The calendar looked thin this morning and most of them are alts anyway"
    q = "how many people signed up?"
    assert fit_answer(draft, 8, q).text == "We have 22 now. The calendar looked thin this morning"
    draft = "We have 22 now. " + " ".join(["filler"] * 20)
    # a sheet that names six words or more: the four-word sentence alone is no cut
    fit = fit_answer(draft, 5, q, floor=6)
    assert len(fit.text.split()) >= 6 and "22" in fit.text
    assert fit_answer(draft, 5, q).text == "We have 22 now."


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
