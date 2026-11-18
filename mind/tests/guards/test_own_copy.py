"""Rewrites that made it worse (self-play audit, 2026-11-17): her own previous message is never
sent again (guard code `own_copy`, tell "own copy"), a draft flagged `claim_conflict` on every
review is not sent as it is, and her last lines are not shown twice in a private prompt.
Invented lines only."""
import pytest

from .test_live import rig, turn, speak, source  # noqa: F401
from leeroy_mind.guards import Draft, GuardStores, HistoryLine, Verdict, review
from leeroy_mind.guards.owncopy import asked_again, copies, copies_any
from leeroy_mind.guards.review import DROP_CODES, HARD_CODES, HINTS, fresh_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import DROP_ON_RETRY, OWED_HARD_TELLS, TurnMeta

LAST = ("oh no, im so sorry. the big brown one? i know how much he means to you, forget i asked "
        "about the shift, thats the last daft question from me today")


@pytest.mark.parametrize("draft,earlier,expected", [
    (LAST, LAST, True),
    ("oh no im so sorry. the big brown one? i know how much he means to u, forget i asked about "
     "the shift thats the last daft question from me today....", LAST, True),  # restyled
    ("i know how much he means to you, forget i asked about the shift", LAST, True),  # a piece
    (LAST + " anyway hows work", LAST, True),  # the whole of it and a little more
    ("im so sorry. do you want to talk about it or think about something else", LAST, False),
    ("lol ok", "lol ok", False),  # short lines never count
    ("night x", "night x", False),
    ("the big brown one is asleep on my foot again, means i cant move today", LAST, False),
])
def test_copies(draft, earlier, expected):
    assert copies(draft, earlier) is expected


def test_a_burst_sent_again_as_one_message_and_asked_again():
    burst = ["oh no, im so sorry about that", "the big brown one? he means so much to you"]
    assert copies_any("oh no im so sorry about that. the big brown one? he means so much to you",
                      burst)
    assert not copies_any("so sorry. what happened, is he at the vet now", burst)
    assert not copies_any("anything at all here", [])
    assert asked_again("sorry what did you say") and asked_again("say that again?")
    assert not asked_again("he is eleven, just want to think about something else")


def test_the_code_is_hard_and_said_in_words():
    assert "own_copy" in HARD_CODES and "own_copy" in DROP_CODES
    assert "own copy" in DROP_ON_RETRY and "own copy" in OWED_HARD_TELLS
    assert "previous message again" in HINTS["own_copy"]
    assert "own_copy" not in fresh_hint(("own_copy",))


@pytest.mark.parametrize("channel,room", [("whisper", "whisper"), ("dm", "dm:101")])
def test_review_names_her_own_previous_message_said_again(rig, channel, room):
    source(rig, LAST, who=101, channel=room, to=1)
    parent = source(rig, "he is eleven. just want to think about something else", channel=room,
                    to=101)
    stores = GuardStores(rig.store.conn, rig.engine.live_review.config)
    kw = dict(parent_id=parent, to_guid=1, now=rig.clock.now(), real_answer_due=True)
    if channel == "dm":
        kw["room"] = "101"
    piece = review(stores, Draft(101, channel, "i know how much he means to you, forget i asked "
                                 "about the shift", **kw))
    assert "own_copy" in piece.codes and piece.action == "drop"
    fresh = review(stores, Draft(101, channel, "ok. tell me about the worst pull you ever saw "
                                 "then, i want details", **kw))
    assert "own_copy" not in fresh.codes
    words = "sorry what did you say, missed that"
    again = source(rig, words, channel=room, to=101)
    if channel == "dm":  # a DM's parent reaches the review through the live history
        stores = GuardStores(rig.store.conn, rig.engine.live_review.config, history=(
            HistoryLine(again, rig.clock.now(), 1, words, "dm", to_guid=101, room="101",
                        readers=(1, 101), origin="human"),))
    asked = review(stores, Draft(101, channel, LAST, **{**kw, "parent_id": again}))
    assert "own_copy" not in asked.codes  # he asked her to say it again


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_an_owed_answer_never_sends_her_previous_message_again(rig, monkeypatch, channel):
    """Both drafts copy her last message: neither is sent, and a fresh draft told what was wrong
    answers him."""
    def verdict(stores, draft):  # each line of her styled answer is reviewed on its own
        copied = any(w in draft.text for w in ("sorry", "means to", "daft question"))
        return Verdict("drop", ("own_copy",)) if copied else Verdict("send")
    monkeypatch.setattr(live, "review", verdict)
    rig.script(LAST, LAST, "ok. no more vet talk. tell me the dumbest wipe you ever caused")
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                    whisper_reply=channel == "whisper"))
    assert sent and " ".join(sent).startswith("ok. no more vet talk")
    told = [c for c in rig.chat_calls()
            if "previous message again" in c["messages"][-1]["content"]]
    assert told  # the new draft was told what was wrong, in words
    for call in told:
        assert [m["role"] for m in call["messages"]] == ["system", "user"]  # never continued


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_the_tell_catches_a_copy_without_the_guards(rig, channel):
    """With the live guards off the tell alone holds it: "near repeat" was soft, so a retry that
    copied her last message went out."""
    rig.engine._own_of(101).append((channel, 1, LAST))
    rig.script(LAST + " x", "i know how much he means to you, forget i asked about the shift",
               "ok. no more vet talk. tell me the dumbest wipe you ever caused")
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                    whisper_reply=channel == "whisper"))
    assert sent and " ".join(sent).startswith("ok. no more vet talk")
    hint = rig.chat_calls()[1]["messages"][-1]["content"]
    assert "don't say your last message again" in hint


def test_a_draft_flagged_claim_conflict_on_every_review_is_not_sent_as_it_is(rig, monkeypatch):
    """Owed: the conflicting sentence is cut and the rest goes."""
    def verdict(stores, draft):
        return (Verdict("rewrite", ("claim_conflict",)) if "night shift" in draft.text
                else Verdict("send"))
    monkeypatch.setattr(live, "review", verdict)
    rig.script("im on the night shift tonight. the pump is acting up again. you ok",
               "its the night shift for me. the pump broke again. how are you")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    said = " ".join(sent)
    assert sent and "night shift" not in said and "pump" in said
    assert len(rig.chat_calls()) == 2  # no third generation: the cut stood


def test_claim_conflict_everywhere_falls_back_to_a_fresh_short_answer(rig, monkeypatch):
    def verdict(stores, draft):
        return (Verdict("rewrite", ("claim_conflict",)) if "night shift" in draft.text
                else Verdict("send"))
    monkeypatch.setattr(live, "review", verdict)
    rig.script("im on the night shift tonight", "night shift again for me",
               "long day, glad its over. you ok")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and sent[0].startswith("long day") and "night shift" not in sent[0]
    assert "contradicts something already known" in rig.chat_calls()[2]["messages"][-1]["content"]


def test_claim_conflict_that_nothing_fixes_still_answers_him(rig, monkeypatch):
    """The owner's rule stands: his message is never left unanswered. Her least-bad draft goes."""
    monkeypatch.setattr(live, "review", lambda *a: Verdict("rewrite", ("claim_conflict",)))
    rig.script("im on the night shift tonight", "night shift again for me", "night shift, yes")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and "night shift" in sent[0]


def test_one_conflict_then_a_clean_rewrite_goes_as_before(rig, monkeypatch):
    def verdict(stores, draft):
        return (Verdict("rewrite", ("claim_conflict",)) if "night shift" in draft.text
                else Verdict("send"))
    monkeypatch.setattr(live, "review", verdict)
    rig.script("im on the night shift tonight", "home tonight, feet up")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and sent[0].startswith("home tonight") and len(rig.chat_calls()) == 2
