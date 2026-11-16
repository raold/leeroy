"""A member speaking with the other sex's body or role, and contradicting her own persona (identity
audit, 2026-11-15): the guard names it, the retry is told in words, an owed answer is never her
speaking as a man (the retry if clean, else the draft cut short of what slipped), and the persona
outranks a stored chat claim that contradicts it."""
import importlib

import pytest

from engine_rig import save_member
from leeroy_mind.claims.check import Conflict
from leeroy_mind.guards import Draft, GuardStores, review
from leeroy_mind.guards.identity import slips, without_slips
from leeroy_mind.guards.review import HARD_CODES, rewrite_hint
from leeroy_mind.talk.pipeline import TurnMeta

from .test_live import rig, speak, turn  # noqa: F401  (rig is a fixture)

R = importlib.import_module("leeroy_mind.guards.review")  # the package exports review()

WOMAN_HOME = "Lives with her husband and a rescue terrier"


@pytest.mark.parametrize("text", [
    "you know i'm not some nice gentleman",
    "i'm a real gentleman about it",
    "as a guy, i get it",
    "i'd bury myself in you so deep",
    "holding my balls in your hand",
    "my erection says hi",
    "im your boyfriend now",
    "i'm hard for you right now",
])
def test_a_woman_speaking_as_a_man_slips(text):
    assert slips(text, "female") and not slips(text, "male")


@pytest.mark.parametrize("text", [
    "i'm not a guy lol",  # correcting a misgendering
    "you're such a gentleman",
    "your balls are not my problem",  # about him
    "i want your hands on me",
    "he's a real gentleman",
    "i'm hard to please tonight",
    "i'm a guild officer, not your secretary",
    "bury myself in your arms",
])
def test_her_words_about_him_or_herself_as_a_woman_never_slip(text):
    assert not slips(text, "female")


@pytest.mark.parametrize("text", ["as a woman, i think so", "i'm a lady, thank you",
                                  "my breasts ache from the gym", "i'm pregnant again"])
def test_a_man_is_held_to_the_mirror_list(text):
    assert slips(text, "male") and not slips(text, "female")


def test_without_slips_keeps_the_sentences_that_do_not_slip():
    text = "are you sure? fine by me. i'm not some nice gentleman. see you at nine"
    assert without_slips(text, "female") == "are you sure? fine by me. see you at nine"
    assert without_slips("i'm not some nice gentleman", "female") == ""


def _her(rig, **over):
    save_member(rig.store, 101, player_gender="female", household=WOMAN_HOME, **over)
    rig.engine.personas.invalidate()


def _review(rig, text, channel="whisper", **kw):
    return review(GuardStores(rig.store.conn, rig.engine.live_review.config),
                  Draft(101, channel, text, to_guid=1 if channel == "whisper" else 0,
                        now=rig.clock.now(), **kw))


def test_review_names_a_slip_and_asks_for_one_rewrite_in_words(rig):
    _her(rig)
    verdict = _review(rig, "you know i'm not some nice gentleman, come here")
    assert verdict.action == "rewrite" and "self_male_slip" in verdict.codes
    assert "self_male_slip" in HARD_CODES
    hint = rewrite_hint(verdict.codes)
    assert "you are a woman" in hint and "self_male_slip" not in hint
    assert _review(rig, "you're such a gentleman").codes == ()


def test_review_holds_a_man_to_the_mirror_list(rig):
    verdict = _review(rig, "as a woman, i think you're right")  # 101 is a man by default
    assert "self_female_slip" in verdict.codes and verdict.action == "rewrite"


def test_review_names_a_line_that_gets_her_own_life_wrong(rig):
    _her(rig)
    verdict = _review(rig, "single, no kids. just me and the telly")
    assert "persona_conflict" in verdict.codes and verdict.action == "rewrite"
    assert any(r.kind == "persona" for r in verdict.related)
    assert "who you live with" in rewrite_hint(verdict.codes)
    assert _review(rig, "my husband is cooking tonight, walking the terrier after").codes == ()


def test_the_persona_outranks_a_stored_chat_claim_that_contradicts_it(rig, monkeypatch):
    """A claim stored from an invention ("has two daughters") never holds back a line that
    agrees with her persona; a claim that does not contradict it still does."""
    _her(rig)
    hits = [Conflict(9, "children", "has two daughters aged five and eight", .9, "negation")]
    monkeypatch.setattr(R, "_claim_hits", lambda *a: hits)
    assert "claim_conflict" not in _review(rig, "i don't have any daughters lol").codes
    hits[:] = [Conflict(9, "saurfang", "loves the saurfang fight", .9, "polarity")]
    assert "claim_conflict" in _review(rig, "i hate the saurfang fight").codes


def _dropped(rig):
    return [r["dropped"] for r in rig.utterances() if r["dropped"]]


OWED = dict(why="reply:named", answers_human=True, whisper_reply=True)


def test_an_owed_answer_sends_a_clean_retry(rig):
    _her(rig)
    rig.script("you know i'm not some nice gentleman", "fine by me, the cabin is warm")
    sent, meta = speak(rig, turn("whisper"), TurnMeta("owed", **OWED))
    assert len(sent) == 1 and "cabin" in sent[0] and meta.guard_rewritten
    [_, retry] = rig.chat_calls()
    assert "you are a woman" in retry["messages"][-1]["content"]


def test_an_owed_answer_that_slips_twice_goes_out_short_of_what_slipped(rig):
    _her(rig)
    rig.script("are you sure? i'm not some nice gentleman. the cabin is warm tonight",
               "i'm a real gentleman. the cabin is warm tonight and the fire is lit")
    sent, meta = speak(rig, turn("whisper"), TurnMeta("owed", **OWED))
    said = " ".join(sent)
    assert "gentleman" not in said and "fire is lit" in said
    assert "self_male_slip" not in meta.guard_codes


def test_an_owed_answer_with_nothing_left_but_the_slip_drops(rig):
    _her(rig)
    rig.script("i'm not some nice gentleman", "i'm a real gentleman")
    sent, meta = speak(rig, turn("whisper"), TurnMeta("owed", **OWED))
    assert not sent and "guard_identity" in _dropped(rig)


def test_an_unowed_line_that_slips_twice_is_dropped(rig):
    _her(rig)
    rig.script("as a guy, i agree", "as a guy, i still agree")
    sent, meta = speak(rig, turn("guild"), TurnMeta("unowed", why="test"))
    assert not sent and meta.guard_rewritten
