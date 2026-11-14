"""A reply that only says his message back is never sent: the live guard names it (echo_parent),
the retry is told in words, and an owed answer drops rather than fall back to the echo (owner
2026-11-14: "sometimes when DM'ing the bots will just quote what i said back")."""
import pytest

from .test_live import rig, turn, speak, source
from leeroy_mind.guards import Draft, GuardStores, Verdict, review
from leeroy_mind.guards.review import echoes, rewrite_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta

HIS = "how many kids do you have? what about their dad?"


@pytest.mark.parametrize("parent,reply,expected", [
    (HIS, "how many kids do you have? waht about their dad?", True),  # a typo pass keeps the echo
    (HIS, HIS.upper(), True),
    ("single and no kids. five and eight are wonderful ages, youre a lucky woman",
     "single, no kids. five and eight are wonderful ages, youre a lucky woman. maybe one day", True),
    ("can i be honest? i would kill a man to see you smile",
     "can i be honest? i would kill a man to see you smile", True),
    (HIS, "two girls, five and eight. their dad left years ago", False),
    ("hey babe", "hey babe! missed you", False),
    ("are you coming to the raid tonight?", "coming to the raid, yes, after dinner", False),
    ("", "anything", False),
    # His question turned into its answer is an answer, not an echo.
    ("Are the spare flasks ready for the next raid?", "The spare flasks are ready for the next raid.", False),
    ("can you bring the spare flasks to the raid tonight", "ill bring the spare flasks to the raid tonight", False),
    ("i love you", "i love you", False),
    ("how are you doing tonight?", "how are you doing tonight?", False),
    ("how are you doing tonight babe?", "how are you doing tonight babe?", True),
])
def test_echo_is_a_copied_run_of_his_words(parent, reply, expected):
    assert echoes(reply, parent) is expected


def test_review_drops_an_echo_of_the_line_it_answers(rig):
    parent = source(rig, HIS, channel="whisper", to=101)
    verdict = review(GuardStores(rig.store.conn, rig.engine.live_review.config),
                     Draft(101, "whisper", HIS, parent_id=parent, to_guid=1, now=rig.clock.now(),
                           real_answer_due=True))
    assert "echo_parent" in verdict.codes and verdict.action == "drop"


def test_review_never_calls_her_own_line_an_echo(rig):
    mine = source(rig, HIS, who=101)
    verdict = review(GuardStores(rig.store.conn, rig.engine.live_review.config),
                     Draft(101, "guild", HIS + " anyway", parent_id=mine, now=rig.clock.now()))
    assert "echo_parent" not in verdict.codes


def test_rewrite_hint_says_what_is_wrong_in_words():
    hint = rewrite_hint(("echo_parent", "nothing_new", "gag_ration"))
    assert "echo_parent" not in hint and "nothing_new" not in hint
    assert "repeats" in hint and "own words" in hint
    assert rewrite_hint(("some_new_code",)).endswith("some_new_code")


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_an_owed_answer_drops_instead_of_sending_an_echo(rig, monkeypatch, channel):
    monkeypatch.setattr(live, "review", lambda *args: Verdict("drop", ("echo_parent",)))
    rig.script(HIS, HIS)
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                    whisper_reply=channel == "whisper"))
    assert not sent and meta.guard_rewritten
    [first, retry] = rig.chat_calls()
    assert "own words" in retry["messages"][-1]["content"]


def test_an_owed_answer_keeps_a_rewrite_that_stopped_echoing(rig, monkeypatch):
    calls = []
    def verdict(*args):
        calls.append(args)
        return Verdict("drop", ("echo_parent",)) if len(calls) == 1 else Verdict("send")
    monkeypatch.setattr(live, "review", verdict)
    rig.script(HIS, "two girls, five and eight")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and sent[0].startswith("two girls, five and eight")
