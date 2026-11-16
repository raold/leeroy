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
    # Identity audit, 2026-11-15: his opening carried past his first sentence, and runs that
    # cross his sentences (a question among them), are his words said back as hers.
    ("are you sure? its kinda late. you know im a night owl, not a morning person",
     "are you sure? it's kinda late. you know i'm up with you anyway", True),
    ("are you sure? its kinda wild. you know", "are you sure? its kinda wild", True),
    ("are we still on for tonight? the raid starts at eight sharp",
     "ok. still on for tonight? the raid starts at eight. bring flasks", True),
    # ...but his whole short first sentence said back, and a run inside one question, are not.
    ("you drive me wild", "you drive me wild too. trust me.", False),
    ("i love you so much. see you tonight", "i love you so much too", False),
    ("do you want to come to the raid tonight with me?",
     "i do want to come to the raid tonight with you", False),
    ("are you sure? its kinda wild", "are you sure? yes", False),
    # Review 323 P3-3: a short first sentence of his opens no echo, and words he asked for are
    # the answer.
    ("haha. you drive me wild", "haha you drive me wild too", False),
    ("lol. you are so cute", "lol you are so cute too", False),
    ("ok. lets do this then. ready?", "ok lets do this then", False),
    ("repeat after me. i am yours. only yours. forever and always.",
     "i am yours. only yours. forever and always.", False),
    ("now say it: i belong to the guild and its raid nights", "i belong to the guild and its raid nights", False),
    ("id say that is a lot of flasks for one raid night", "id say that is a lot of flasks for one raid night", True),
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
def test_an_owed_answer_never_sends_an_echo(rig, monkeypatch, channel):
    """Owner, 2026-11-15: every message from him gets a reply. Both drafts said his message back,
    so one fresh draft is tried (never going on from the echo, which is never an assistant turn;
    what was wrong is said at the end of the instruction); when that one echoes too, his owed
    answer is the fallback line, built from his message and never his words (talk/owed.py)."""
    monkeypatch.setattr(live, "review", lambda *args: Verdict("drop", ("echo_parent",)))
    rig.script(HIS, HIS)
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                    whisper_reply=channel == "whisper"))
    assert len(sent) == 1 and meta.guard_rewritten and meta.owed_rescued == "guard_echo"
    assert not echoes(sent[0], HIS) and HIS.casefold() not in sent[0].casefold()
    [first, retry, fresh] = rig.chat_calls()
    for call in (retry, fresh):
        assert [m["role"] for m in call["messages"]] == ["system", "user"]
        assert "own words" in call["messages"][-1]["content"]
        assert call["messages"][-1]["content"].startswith(first["messages"][-1]["content"])


def test_an_owed_answer_sends_a_fresh_draft_when_both_echo(rig, monkeypatch):
    def verdict(stores, draft):
        return Verdict("send") if "girls" in draft.text else Verdict("drop", ("echo_parent",))
    monkeypatch.setattr(live, "review", verdict)
    rig.script(HIS, HIS, "two girls, five and eight")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and sent[0].startswith("two girls, five and eight")
    assert len(rig.chat_calls()) == 3


def test_an_owed_answer_keeps_a_rewrite_that_stopped_echoing(rig, monkeypatch):
    calls = []
    def verdict(*args):
        calls.append(args)
        return Verdict("drop", ("echo_parent",)) if len(calls) == 1 else Verdict("send")
    monkeypatch.setattr(live, "review", verdict)
    rig.script(HIS, "two girls, five and eight")
    sent, meta = speak(rig, turn("dm:101"), TurnMeta("owed", why="reply:named", answers_human=True))
    assert len(sent) == 1 and sent[0].startswith("two girls, five and eight")
