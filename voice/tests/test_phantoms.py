"""The hallucination guard (stt/phantoms.py) and the Transcript it reads (stt/__init__.py)."""
import math

import pytest

from leeroy_voice.stt import MIN_SPEECH_MS, Transcript
from leeroy_voice.stt.phantoms import PHANTOM_MS, PHANTOMS, is_phantom, phrase


# -- the brief's ----------------------------------------------------------------------------------

def test_phantoms():
    assert is_phantom(Transcript("Thank you.", .9, 700), min_confidence=.4)
    assert not is_phantom(Transcript("Thank you.", .9, 1200), min_confidence=.4)   # M11: 1 s or more is kept
    assert not is_phantom(Transcript("Thank you, Belra.", .9, 1400), min_confidence=.4)
    assert is_phantom(Transcript("pull now", .3, 900), min_confidence=.4)
    assert is_phantom(Transcript("pull", .9, 200), min_confidence=.4)        # under 250 ms of speech


# -- the list and the edges -----------------------------------------------------------------------

def test_the_phantom_list():
    assert PHANTOMS == frozenset({"thank you", "thanks for watching", "you", "bye", "okay",
                                  "thank you so much", "please subscribe"})
    assert (PHANTOM_MS, MIN_SPEECH_MS) == (1000, 250)


@pytest.mark.parametrize("text", ["Thank you.", "THANK YOU!!", "thank-you", "Thanks for watching!",
                                  "You.", "Bye.", "Okay.", "Thank you so much.",
                                  "Please subscribe.", "  thank   you  "])
def test_every_phantom_under_a_second(text):
    assert is_phantom(Transcript(text, .95, 999), min_confidence=.4)
    assert not is_phantom(Transcript(text, .95, 1000), min_confidence=.4)


def test_a_phantom_inside_a_line_is_kept():
    for text in ("Thank you, Belra.", "bye for now", "okay pull", "you go"):
        assert not is_phantom(Transcript(text, .9, 600), min_confidence=.4), text
    assert is_phantom(Transcript("Thank you.", .9, 600), min_confidence=.4)


def test_the_speech_floor():
    assert is_phantom(Transcript("pull now", .9, 249), min_confidence=.4)
    assert not is_phantom(Transcript("pull now", .9, 250), min_confidence=.4)


def test_the_confidence_floor():
    assert is_phantom(Transcript("pull now", .399, 900), min_confidence=.4)
    assert not is_phantom(Transcript("pull now", .4, 900), min_confidence=.4)
    assert is_phantom(Transcript("pull now", .9, 5000), min_confidence=.95)   # always, any length
    assert not is_phantom(Transcript("pull now", 0.0, 900), min_confidence=0.0)


def test_no_words_is_a_phantom():
    assert is_phantom(Transcript("", 0.0, 900), min_confidence=0.0)
    assert is_phantom(Transcript("...", .9, 1500), min_confidence=.4)


def test_phrase():
    assert phrase("Thank you, Belra.") == "thank you belra"
    assert phrase("You\u2019re on ADDS!") == "you're on adds"
    assert phrase("Yogg-Saron") == "yogg saron"
    assert phrase(" ?! ") == ""


@pytest.mark.parametrize("bad", [-0.1, 1.1, math.nan, math.inf, "0.9", None, True])
def test_a_transcript_holds_a_probability(bad):
    with pytest.raises(ValueError):
        Transcript("pull", bad, 900)


@pytest.mark.parametrize("bad", [-1, 900.0, "900", None, True])
def test_a_transcript_holds_whole_milliseconds(bad):
    with pytest.raises(ValueError):
        Transcript("pull", .9, bad)


def test_a_transcript_holds_text_and_plain_numbers():
    with pytest.raises(ValueError):
        Transcript(None, .9, 900)
    t = Transcript("pull", 1, 900)
    assert type(t.confidence) is float and t.confidence == 1.0
    assert t == Transcript("pull", 1.0, 900)
