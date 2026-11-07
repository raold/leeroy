import pytest

from leeroy_selfie.fit import PromptTooLong, fit

words = lambda t: len(t.replace(",", " ").split())  # noqa: E731


def test_fit_keeps_required_and_higher_priorities_in_position_order():
    assert fit([("a b", 0), ("c d e", 3), ("f", 1), ("g h", 2)], words, limit=5) == (
        "a b, f, g h", ("c d e",))


def test_fit_skips_a_long_phrase_and_keeps_a_later_short_one_of_its_priority():
    assert fit([("a", 0), ("b c d e", 1), ("f", 1)], words, limit=3) == ("a, f", ("b c d e",))


def test_fit_refuses_required_phrases_that_alone_overflow():
    with pytest.raises(PromptTooLong):
        fit([("a b c", 0)], words, limit=2)


def test_fit_of_nothing_is_empty():
    assert fit([], words) == ("", ())
