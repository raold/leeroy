import dataclasses
import json

import pytest

from conftest import make_persona
from leeroy_mind.personas.card import card_race_class, render_core_card
from leeroy_mind.personas.schema import (PersonaError, persona_from_dict, persona_json_schema,
                                         persona_to_dict, validate_persona)
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import CLASSES, RACES, faction_of


def test_round_trip():
    p = make_persona(101, "Arkon")
    assert persona_from_dict(persona_to_dict(p)) == p
    assert persona_from_dict(json.loads(json.dumps(persona_to_dict(p)))) == p


def test_valid_persona_has_no_errors():
    assert validate_persona(make_persona(101, "Arkon")) == []
    assert validate_persona(make_persona(101, "Arkon", maximal=True)) == []


def test_minor_rejected():
    assert "identity.age: must be at least 18" in validate_persona(make_persona(101, "Arkon", age=17))


def test_enum_error_has_path():
    d = persona_to_dict(make_persona(101, "Arkon"))
    d["attachment"] = "clingy"
    with pytest.raises(PersonaError, match="attachment"):
        persona_from_dict(d)


def test_nested_errors_have_dotted_paths_and_all_are_reported():
    d = persona_to_dict(make_persona(101, "Arkon"))
    d["romance"]["stance"] = "smitten"
    d["big_five"]["openness"]["score"] = "high"
    d["bits"][0]["contexts"] = ["any", "nowhere"]
    del d["real_life"]["job"]
    d["extra"] = 1
    with pytest.raises(PersonaError) as e:
        persona_from_dict(d)
    errs = "\n".join(e.value.errors)
    for path in ("romance.stance", "big_five.openness.score", "bits[0].contexts[1]",
                 "real_life.job: missing", "extra: unknown field"):
        assert path in errs


def test_romance_has_no_target_field():
    d = persona_to_dict(make_persona(101, "Arkon"))
    d["romance"]["target"] = "Belra"
    with pytest.raises(PersonaError, match="romance.target"):
        persona_from_dict(d)


def test_nullable_romance_fields_round_trip():
    p = make_persona(101, "Arkon", stance="drawn", style="shy", jealousy_mode="teaser")
    assert persona_from_dict(persona_to_dict(p)).romance.style == "shy"


def test_meta_text_rejected():
    assert any("opinions[0]" in e for e in validate_persona(
        make_persona(101, "Arkon", opinions=["as an AI i love raids", "b", "c"])))


def test_range_and_count_errors_name_the_field():
    errs = validate_persona(make_persona(
        101, "Arkon", opinions=["a"], typo_rate=0.5, years_played=2, birthday="13-40"))
    joined = "\n".join(errs)
    for needle in ("opinions: must have at least 3", "typing.typo_rate: must be at most 0.15",
                   "game.years_played: must be at least 3", "real_life.birthday"):
        assert needle in joined
    assert validate_persona(make_persona(101, "Arkon", words_per_line=[9, 3]))


def test_json_schema_enums():
    s = persona_json_schema()
    assert s["properties"]["attachment"]["enum"] == ["secure", "anxious", "avoidant"]
    assert s["properties"]["identity"]["properties"]["age"]["minimum"] == 18
    assert s["properties"]["opinions"]["minItems"] == 3
    assert "anyOf" in s["properties"]["romance"]["properties"]["style"]
    assert "mock_interview" not in s["required"] and "guid" in s["required"]
    assert s["additionalProperties"] is False


def test_card_budget_stable_and_clean():
    p = make_persona(101, "Arkon", maximal=True)
    card = render_core_card(p, race="Orc", class_name="Warrior")
    assert estimate_tokens(card) <= 150 and card == render_core_card(p, race="Orc", class_name="Warrior")
    assert "Arkon" in card and p.identity.real_name in card
    assert p.bits[0].instruction not in card and p.sample_lines[0].text not in card
    assert p.secret not in card
    assert "BITINSTR" not in card and "SAMPLELINE" not in card and "SECRETTEXT" not in card


def test_normal_card_has_its_sections_in_order():
    p = make_persona(101, "Arkon")
    card = render_core_card(p, race="Orc", class_name="Warrior")
    assert estimate_tokens(card) <= 150
    assert card.splitlines()[0] == (
        "You are Jo, 34, UK. You play Arkon, an Orc Protection Warrior (tank, raider).")
    order = ["warehouse supervisor", "chats easily", "Attachment: secure", "In game:",
             "Opinions:", "Never:"]
    assert [card.index(x) for x in order] == sorted(card.index(x) for x in order)
    assert "rarely rattled" in card  # the trait furthest from 5


def test_card_uses_officer_title_and_caps_opinions():
    p = make_persona(101, "Arkon", guild_role="officer", officer_title="Quartermaster",
                     opinions=["one", "two", "three", "four"])
    card = render_core_card(p, race="Orc", class_name="Warrior")
    assert "(tank, officer Quartermaster)" in card
    assert "two" in card and "three" not in card


def test_card_without_race_and_class():
    card = render_core_card(make_persona(101, "Arkon"), race="", class_name="")
    assert card.splitlines()[0].startswith("You are Jo, 34, UK. You play Arkon, a Protection (")
    assert card_race_class(card) == ("", "")


def test_card_race_class_recovered():
    p = make_persona(101, "Arkon")
    for race, cls in (("Night Elf", "Death Knight"), ("Orc", "Warrior"), ("Undead", "Mage")):
        assert card_race_class(render_core_card(p, race=race, class_name=cls)) == (race, cls)


def test_wow_ids():
    assert (RACES[10], CLASSES[6], faction_of(8)) == ("Blood Elf", "Death Knight", "horde")
    assert faction_of(1) == faction_of(11) == "alliance"


def test_lust_is_an_attitude():
    p = make_persona(201, "Belra", stance="lust", style="bold")
    assert persona_from_dict(persona_to_dict(p)).romance.stance == "lust"
    stance = persona_json_schema()["properties"]["romance"]["properties"]["stance"]
    assert stance["enum"] == ["none", "fond", "drawn", "lust"]


# -- typing: full form -> the shorter chat form (rehearsal 1) -----------------------------------

def test_abbreviations_map_the_full_form_to_the_chat_form():
    ok = make_persona(101, "Arkon", abbreviations={"you": "u", "laughing out loud": "lol"})
    assert validate_persona(ok) == []
    for backwards in ({"lol": "laughing out loud"}, {"ok": "ok"}, {"u": "you"}):
        errs = validate_persona(make_persona(101, "Arkon", abbreviations=backwards))
        key = next(iter(backwards))
        assert errs == [f'typing.abbreviations.{key}: map the full form to the shorter way they '
                        f'type it, e.g. "laughing out loud": "lol"']


def test_typing_is_plain_ascii_text():
    errs = validate_persona(make_persona(101, "Arkon", emoticons=[":)", "\u2615", " "]))
    assert errs == ["typing.emoticons[1]: plain text only, like :) or xD (the game cannot show "
                    "emoji)", "typing.emoticons[2]: plain text only, like :) or xD (the game "
                    "cannot show emoji)"]
    errs = validate_persona(make_persona(101, "Arkon", abbreviations={"caf\u00e9": "cf"}))
    assert errs and errs[0].startswith("typing.abbreviations.caf\u00e9: map the full form")


def test_the_json_schema_shows_the_model_which_way_round_typing_goes():
    t = persona_json_schema()["properties"]["typing"]["properties"]
    assert '{"you": "u", "laughing out loud": "lol"}' in t["abbreviations"]["description"]
    assert "never emoji" in t["emoticons"]["description"]


# -- carried minors (final-wave-carry.md, Task 6) -------------------------------------------------

def test_a_number_that_is_not_finite_is_refused():
    errs = validate_persona(make_persona(101, "Arkon", emoticon_rate=float("nan")))
    assert errs == ["typing.emoticon_rate: must be a finite number"]
    errs = validate_persona(make_persona(101, "Arkon", speed=float("inf")))
    assert errs == ["identity.voice.speed: must be a finite number"]


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_the_schema_version_is_the_integer_one(bad):
    d = persona_to_dict(make_persona(101, "Arkon"))
    d["schema"] = bad
    with pytest.raises(PersonaError, match="schema: must be 1"):
        persona_from_dict(d)


def test_the_json_schema_has_the_month_day_pattern_and_key_lengths():
    import re
    s = persona_json_schema()["properties"]
    mm_dd = s["real_life"]["properties"]["birthday"]["pattern"]
    assert re.fullmatch(mm_dd, "02-29") and re.fullmatch(mm_dd, "12-31")
    assert not any(re.fullmatch(mm_dd, x) for x in ("13-01", "2-9", "00-10", "01-32", "0101"))
    assert s["bits"]["items"]["properties"]["dates"]["items"]["pattern"] == mm_dd
    assert s["typing"]["properties"]["abbreviations"]["propertyNames"] == {"maxLength": 20}


def test_ages_from_18_to_99():
    assert validate_persona(make_persona(101, "Arkon", age=18)) == []
    assert validate_persona(make_persona(101, "Arkon", age=99)) == []
    assert validate_persona(make_persona(101, "Arkon", age=100)) == [
        "identity.age: must be at most 99"]


def test_the_jealousy_modes_are_the_schemas():
    from typing import get_args, get_type_hints
    from leeroy_mind.personas.schema import JEALOUSY_MODES, Romance
    hint = get_type_hints(Romance)["jealousy_mode"]
    assert set(JEALOUSY_MODES) == {a for x in get_args(hint) for a in get_args(x)}


# -- a member's own romance dial (fix wave G4) ---------------------------------------------------

def test_a_persona_stored_before_the_member_dial_has_none():
    d = persona_to_dict(make_persona(101, "Arkon"))
    assert d["romance"]["dial"] is None
    del d["romance"]["dial"]
    assert persona_from_dict(d).romance.dial is None


@pytest.mark.parametrize("dial", [None, *range(1, 11)])
def test_the_member_dial_is_none_or_one_to_ten_and_round_trips(dial):
    p = make_persona(101, "Arkon", dial=dial)
    assert p.romance.dial == dial and validate_persona(p) == []
    assert persona_from_dict(json.loads(json.dumps(persona_to_dict(p)))) == p


@pytest.mark.parametrize("dial,problem", [(0, "at least 1"), (11, "at most 10"),
                                          (-2, "at least 1"), (100, "at most 10")])
def test_the_member_dial_out_of_range_is_refused(dial, problem):
    assert validate_persona(make_persona(101, "Arkon", dial=dial)) == [
        f"romance.dial: must be {problem}"]


@pytest.mark.parametrize("bad", ["7", "high", True, 7.5, [7]])
def test_the_member_dial_must_be_an_integer(bad):
    d = persona_to_dict(make_persona(101, "Arkon"))
    d["romance"]["dial"] = bad
    with pytest.raises(PersonaError, match=r"romance\.dial: must be an integer"):
        persona_from_dict(d)


def test_the_json_schema_has_the_member_dial_as_an_optional_one_to_ten():
    s = persona_json_schema()["properties"]["romance"]
    assert s["properties"]["dial"] == {
        "anyOf": [{"type": "integer", "minimum": 1, "maximum": 10}, {"type": "null"}]}
    assert "dial" not in s["required"] and s["additionalProperties"] is False


# -- emoticons are marks (fix wave G1, item 9) --------------------------------------------------

def test_emoticons_are_marks_and_a_stored_word_is_read_as_a_word():
    """Rehearsal 2: "lmao", "gg", "aww" and "brb" sat among the emoticons and were bolted
    onto finished lines. The schema holds marks; a stored persona with words there still
    loads, each word read as a word (abbreviations with its full form, else a speech habit)."""
    from leeroy_mind.personas.schema import is_mark
    for mark in (":)", ";)", ":D", "xD", "XD", "xDDD", ":P", "<3", "o7", "^^", "-_-", ";_;"):
        assert is_mark(mark), mark
    for word in ("lol", "lmao", "gg", "aww", "ily", "omg", "brb", "hehe"):
        assert not is_mark(word), word
    p = make_persona(101, "Arkon", emoticons=[":)", "lol", "aww", "xD", "gg"],
                     abbreviations={"you": "u"}, speech_habits=["says mate a lot"])
    assert p.typing.emoticons == (":)", "xD")
    assert p.typing.abbreviations == {"you": "u", "laughing out loud": "lol",
                                      "good game": "gg"}
    assert p.speech_habits == ("says mate a lot", 'types "aww" in chat')
    assert validate_persona(p) == []
    full = make_persona(101, "Arkon", emoticons=["aww"], speech_habits=[f"h{i}" for i in range(5)])
    assert full.typing.emoticons == () and len(full.speech_habits) == 5  # no room: left out
    bad = dataclasses.replace(p, typing=dataclasses.replace(p.typing, emoticons=(":)", "lmao")))
    assert validate_persona(bad) == ["typing.emoticons[1]: a chat word, not a mark: put it in "
                                     "speech_habits, or in abbreviations with its full form"]
    t = persona_json_schema()["properties"]["typing"]["properties"]["emoticons"]
    assert "never words like lol" in t["description"]


def test_every_stored_emoticon_word_loads_into_a_persona_that_saves(store):
    """Fix round 1 (review I7): "rofl" became a 29-character abbreviation key, past the cap,
    so the loaded persona failed validation and every later save of it raised. Each chat word
    is moved only where it fits the schema, and the persona still saves."""
    from leeroy_mind.personas.io import save_persona
    from leeroy_mind.personas.schema import CHAT_WORD_FORMS
    for word in CHAT_WORD_FORMS:
        p = make_persona(101, "Arkon", emoticons=[":)", word])
        assert validate_persona(p) == [], word
        save_persona(store, p, source="seed", ts=1000.0, race="Orc", class_name="Warrior")
    many = ["aww", "hehe", "lolz", "teehee", "mwahaha", "hahaha", "rofl", "omgosh", "yay"]
    p = make_persona(101, "Arkon", emoticons=[":)", *many[:6]], speech_habits=["says mate"])
    assert validate_persona(p) == [] and p.typing.emoticons == (":)",)
    full = make_persona(101, "Arkon", emoticons=["lol", "gg"],
                        abbreviations={f"word{i} phrase": f"w{i}" for i in range(12)})
    assert validate_persona(full) == []


def test_a_word_with_its_punctuation_is_no_mark():
    """Fix round 1 (review minor 4): "gg!" and "ty." read as marks and were added to lines."""
    from leeroy_mind.personas.schema import is_mark
    for word in ("gg!", "ty.", "lol!", "lmao..."):
        assert not is_mark(word), word
    for mark in (":)", "xD!", ";)", "<3", "o7"):
        assert is_mark(mark), mark
