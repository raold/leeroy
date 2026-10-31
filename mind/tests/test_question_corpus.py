"""The detector against the whole labelled corpus (tests/question_corpus.py, fix wave part E,
round 3): every positive keeps its topic and its names, and no negative becomes a question. Its
known gaps (round 5) read as they do now, so flipping one is a deliberate change."""
import pytest

from question_corpus import CORPUS, KNOWN_GAPS, NOT_GAME, POSITIVES
from leeroy_mind.talk.questions import game_question

LINES: dict[str, tuple[str | None, tuple[str, ...]]] = {}
for _line, _topic, _names in CORPUS:
    assert LINES.setdefault(_line, (_topic, _names)) == (_topic, _names), _line  # one label


@pytest.mark.parametrize("line", sorted(LINES))
def test_every_corpus_line_reads_as_labelled(line):
    topic, names = LINES[line]
    q = game_question(line)
    if topic is None:
        assert q is None, (line, q)
    else:
        assert q is not None and (q.topic, q.names) == (topic, names), (line, q)


def test_the_corpus_holds_both_sides():
    assert len(POSITIVES) >= 250 and len(NOT_GAME) >= 250


@pytest.mark.parametrize("line, label, now", KNOWN_GAPS, ids=[g[0] for g in KNOWN_GAPS])
def test_a_known_gap_reads_as_it_does_now(line, label, now):
    """A gap is a line the corpus cannot hold yet: the detector's answer is not the raider's. A
    change that gives the raider's answer moves the line into the corpus; any other change to
    the answer is caught here."""
    q = game_question(line)
    assert (None if q is None else (q.topic, q.names)) == now, (line, q)
    assert label != (None if now is None else now[0]), (line, "not a gap: put it in the corpus")


def test_a_known_gap_is_no_corpus_line():
    gaps = [line for line, _, _ in KNOWN_GAPS]
    assert len(set(gaps)) == len(gaps) and not set(gaps) & set(LINES)
