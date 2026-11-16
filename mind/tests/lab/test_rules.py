"""The rule checks on synthetic answers (invented lines only)."""
import pytest

from leeroy_mind.lab import rules


@pytest.mark.parametrize("reply, ask, nouns, ok", [
    ("yeah totally, why", "yesno", (), True),
    ("nah not tonight", "yesno", (), True),
    ("i guess we'll see what happens", "yesno", (), False),
    ("usually around 8 or so", "number", (), True),
    ("a couple of hours most nights", "number", (), True),
    ("whenever really", "number", (), False),
    ("we're doing Ulduar i think", "name", (), True),
    ("prob naxx again lol", "name", ("naxx",), True),
    ("I think so", "name", (), False),
    ("I'm not sure. Maybe Ulduar", "name", (), False),  # the name is past her first sentence
    ("pasta again, i'm lazy", "noun", ("pasta", "dinner"), True),
    ("cooked something easy", "noun", ("cook",), True),
    ("why do you ask", "noun", ("dinner",), False),
    ("", "yesno", (), False),
    ("anything at all", None, (), True),
])
def test_her_first_sentence_answers_what_he_asked(reply, ask, nouns, ok):
    assert rules.answers(reply, ask, nouns) is ok


def test_silence_flags_silent_and_unanswered_only():
    flags = rules.check("you around?", "", ask="yesno")
    assert flags == {c: int(c in ("silent", "unanswered")) for c in rules.CODES}
    assert rules.check("hey", "")["unanswered"] == 0


def test_wordy_is_far_more_words_than_his_line():
    assert rules.check("hi", " ".join(["word"] * 19))["wordy"] == 1
    assert rules.check("hi", " ".join(["word"] * 18))["wordy"] == 0


def test_repeat_is_the_same_subject_as_an_earlier_answer():
    earlier = ["my sister bought a puppy and the puppy chews everything"]
    assert rules.check("lol", "the puppy my sister bought chews everything again", earlier=earlier)["repeat"] == 1
    assert rules.check("lol", "work was long today", earlier=earlier)["repeat"] == 0
    # his own subject is his topic, not her repetition
    assert rules.check("tell me about the puppy your sister bought, does it chew everything",
                       "the puppy chews everything", earlier=earlier)["repeat"] == 0


def test_echo_and_stock_and_rewritten():
    his = "i really think the healers did nothing wrong on that pull"
    assert rules.check(his, "you really think the healers did nothing wrong on that pull?")["echo"] == 1
    assert rules.check(his, "fair, the tank pulled early")["echo"] == 0
    assert rules.check("hi", "you absolute menace")["stock"] == 1
    assert rules.check("hi", "hey you")["stock"] == 0
    assert rules.check("hi", "hey", rewritten=True)["rewritten"] == 1


def test_tally_sums_flags():
    rows = [rules.check("you there?", "", ask="yesno"), rules.check("hi", "hey")]
    assert rules.tally(rows) == {"silent": 1, "unanswered": 1, "wordy": 0, "repeat": 0,
                                 "rewritten": 0, "echo": 0, "stock": 0}
