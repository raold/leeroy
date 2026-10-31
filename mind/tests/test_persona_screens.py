"""Persona screens (personas/screens.py): the romance screen and the real-name matcher, for the
items carried to the final fix wave. Names are invented."""
import pytest

from conftest import _base_persona_dict
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.screens import (check_no_real_name, check_romance, names_real_name,
                                          redact_real_name)

H = HumanIdentity(1, "Testmage", "Testy", "", True, "he")
MEMBERS = ["Belra", "Arkon"]


@pytest.mark.parametrize("text", [
    "I flirt with Testy but Belra does too.",
    "Belra flirts with Testy; Arkon laughs about it",
    "kissed Testy once, then Arkon walked in",
])
def test_romance_whose_first_person_named_is_the_human_is_allowed(text):
    check_romance({"secret": text}, H, MEMBERS)


@pytest.mark.parametrize("text", [
    "flirts with Arkon to make Testy jealous",
    "has a crush on Belra, not Testy",
    "flirts with Testy and has a crush on Arkon",
    "keeps flirting with Testy and with Arkon",   # two objects
    "flirts with Testy and Arkon",
    "flirts with Testy, Arkon",
])
def test_romance_whose_first_person_named_is_a_member_is_refused(text):
    with pytest.raises(ValueError, match="romance"):
        check_romance({"secret": text}, H, MEMBERS)


def test_character_names_count_in_any_case():
    with pytest.raises(ValueError):
        check_romance({"secret": "has a crush on arkon"}, H, [], characters=["Arkon"])
    with pytest.raises(ValueError):
        check_romance({"secret": "flirts with ARKON to make Testy jealous"}, H, [],
                      characters=["Arkon"])
    check_romance({"secret": "has a crush on arkon"}, H, ["Arkon"])  # as written: not him
    with pytest.raises(ValueError):  # a relationship's own target too
        check_romance({"relationships": [{"other": "Arkon", "kind": "friend",
                                          "note": "secretly dating arkon"}]}, H, [])


def _record(**fields):
    d = _base_persona_dict(101, "Coros")
    for path, value in fields.items():
        cur = d
        *head, leaf = path.split(".")
        for k in head:
            cur = cur[int(k)] if isinstance(cur, list) else cur[k]
        cur[leaf] = value
    return d


@pytest.mark.parametrize("path", ["sample_lines.0.text", "bits.0.instruction",
                                  "real_life.household", "real_life.life_thread", "likes",
                                  "speech_habits", "nevers", "mock_interview"])
def test_every_free_text_field_is_screened(path):
    text = "has a crush on Belra"
    value = [text] if path in ("likes", "speech_habits", "nevers") else text
    with pytest.raises(ValueError, match="romance"):
        check_romance(_record(**{path: value}), H, MEMBERS)


def test_a_lower_case_typist_may_tease_a_rival_over_the_human_in_a_sample_line():
    check_romance(_record(**{"sample_lines.0.text": "lol belra stop flirting with testy"}), H, [],
                  characters=MEMBERS)
    with pytest.raises(ValueError):
        check_romance(_record(**{"sample_lines.0.text": "lol stop flirting with belra"}), H, [],
                      characters=MEMBERS)


# -- the real-name matcher -------------------------------------------------------------------------

@pytest.mark.parametrize("configured,kept", [("o'neil", "O'neil"), ("jean-luc", "Jean-luc"),
                                             ("mary jane", "Mary jane"), ("deShawn", "DeShawn")])
def test_the_spelling_with_only_its_first_letter_raised_is_matched(configured, kept):
    h = HumanIdentity(1, "Testmage", "Testy", configured, True)
    assert names_real_name(f"ask {kept} about it", h)
    assert redact_real_name(f"ask {kept} about it", h, "Testy") == "ask Testy about it"


def test_a_name_without_cased_letters_is_matched_as_written():
    name = chr(0x674E) + chr(0x5A1C)
    h = HumanIdentity(1, "Testmage", "Testy", name, True)
    assert names_real_name(f"tell {name} hi", h)
    assert redact_real_name(f"tell {name} hi", h, "Testy") == "tell Testy hi"
    with pytest.raises(ValueError):
        check_no_real_name({"secret": f"once told {name} a secret"}, h)


def test_a_long_text_is_screened_sentence_by_sentence():
    filler = "We raid three nights a week and wipe less than we used to. " * 6
    calm = filler + "I don't tolerate flirting during pulls. Belra tanks the adds."
    check_romance(_record(mock_interview=calm), H, MEMBERS)
    with pytest.raises(ValueError, match="mock_interview"):
        check_romance(_record(mock_interview=filler + "I have a crush on Belra."), H, MEMBERS)
