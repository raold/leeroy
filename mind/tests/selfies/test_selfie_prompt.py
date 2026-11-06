"""The mind's profile-photo prompt (P2 Task 6): persona -> prioritised phrases and negative.
Every person here is invented."""
import json
import re
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.selfies.prompt import NEGATIVE, PromptSpec, build_prompt, renderable
from nightly.test_night_enrich import APPEARANCE

PHOTO = {"kind": "snapshot", "framing": "upper_body", "setting": "on the deck of a small boat",
         "outfit": "orange rain bib over a grey hoodie", "expression": "squinting into the wind"}
MAX_PHRASES, MAX_PHRASE_CHARS = 64, 500            # the worker's limits (selfie protocol.py)


def _persona(gender, over):
    over = dict(over)
    photo = over.pop("photo", PHOTO)
    appearance = over.pop("appearance", APPEARANCE)
    p = make_persona(101, "Arkon", player_gender=gender, **over)
    if isinstance(photo, dict):
        photo = ProfilePhoto(**photo)
    return replace(p, appearance=appearance, photo=photo)


def woman(**over):
    return _persona("female", over)


def man(**over):
    return _persona("male", over)


def test_the_prompt_opens_with_kind_framing_and_subject_and_ends_with_the_phone_look():
    spec = build_prompt(woman(age=34, region="UK", photo={**PHOTO, "kind": "selfie", "framing": "head_and_shoulders"}))
    assert spec.phrases[:3] == (("phone selfie", 0), ("head and shoulders", 0), ("34-year-old British woman", 0))
    assert spec.phrases[-1] == ("recent smartphone photo, natural light, natural skin texture, unretouched", 1)


def test_a_member_under_25_is_called_an_adult():
    assert ("22-year-old adult American man", 0) in build_prompt(man(age=22, region="US")).phrases


def test_a_headshot_has_no_phone_look_and_a_snapshot_does_not_look_at_the_camera():
    head = build_prompt(woman(photo={**PHOTO, "kind": "headshot", "framing": "face"}))
    assert head.phrases[0] == ("professional headshot", 0)
    assert head.phrases[1] == ("close-up of the face", 0)
    assert head.phrases[-1] == ("recent photo, natural skin texture, unretouched", 1)
    assert ("looking at the camera", 2) in head.phrases
    assert ("looking at the camera", 2) not in build_prompt(woman()).phrases
    assert build_prompt(woman(photo={**PHOTO, "kind": "mirror_selfie"})).phrases[0] == ("mirror selfie", 0)
    assert build_prompt(woman()).phrases[0] == ("candid photo taken by a friend", 0)


def test_hair_skin_eyes_and_face_details_come_from_the_looks():
    ph = build_prompt(woman()).phrases
    assert ("shoulder-length dark brown hair, tucked behind one ear", 1) in ph
    assert ("light olive skin", 1) in ph
    assert ("hazel eyes", 2) in ph
    assert ("freckles", 2) in ph and ("thin glasses", 2) in ph
    assert ("average build", 3) in ph
    assert ("orange rain bib over a grey hoodie", 2) in ph
    assert ("on the deck of a small boat", 2) in ph
    assert ("squinting into the wind", 3) in ph
    shaved = build_prompt(man(appearance=replace(APPEARANCE, hair_length="shaved"))).phrases
    assert ("shaved head", 1) in shaved
    assert not any("shaved head," in t for t, _ in shaved)
    for length, word in [("short", "short"), ("chin", "chin-length"), ("long", "long"),
                         ("very_long", "very long")]:
        ph = build_prompt(woman(appearance=replace(APPEARANCE, hair_length=length))).phrases
        assert (f"{word} dark brown hair, tucked behind one ear", 1) in ph


def test_no_name_reaches_a_prompt():
    p = woman(real_name="Quentina")
    spec = build_prompt(replace(p, photo=replace(p.photo, setting="at a bar with Arkon and Quentina")))
    for text, _ in spec.phrases + spec.negative:
        assert "arkon" not in text.lower() and "quentina" not in text.lower()


def test_the_negative_prompt_keeps_minors_nudity_likeness_and_period_looks_out():
    neg = dict(build_prompt(woman()).negative)
    assert neg == dict(NEGATIVE)
    zero = " ".join(t for t, pr in NEGATIVE if pr == 0)
    for word in ("child, teenager", "nsfw", "nude", "celebrity", "cartoon"):
        assert word in zero
    assert ("vintage, retro, film grain, sepia, old photo", 1) in NEGATIVE
    assert ("watermark, text, logo", 2) in NEGATIVE


def test_required_phrases_stay_short():
    longest = replace(APPEARANCE, hair_colour="x" * 30, hair_style="y" * 40, skin_tone="z" * 30)
    spec = build_prompt(woman(age=99, region="UK", appearance=longest,
                              photo={**PHOTO, "kind": "mirror_selfie", "framing": "head_and_shoulders"}))
    assert sum(len(t.split()) for t, pr in spec.phrases if pr == 0) <= 30


def test_renderable_names_what_is_missing():
    assert renderable(None) == "no persona"
    assert renderable(woman(appearance=None)) == "no looks yet"
    assert renderable(woman(photo=None)) == "no photo kind yet"
    assert renderable(woman()) is None
    with pytest.raises(ValueError, match="no looks yet"):
        build_prompt(woman(appearance=None))
    with pytest.raises(ValueError, match="no photo kind yet"):
        build_prompt(woman(photo=None))


def test_the_longest_prompt_the_schema_allows_stays_inside_the_worker_limits():
    big = Appearance(height_cm=170, build="heavy", weight_kg=90, hair_colour="é" * 30,
                     hair_length="very_long", hair_style="é" * 40, skin_tone="é" * 30,
                     eye_colour="é" * 20, face=("é" * 40,) * 4, at_the_pc="é" * 120)
    photo = {"kind": "mirror_selfie", "framing": "head_and_shoulders", "setting": "é" * 80,
             "outfit": "é" * 80, "expression": "é" * 40}
    spec = build_prompt(woman(age=99, appearance=big, photo=photo))
    for lst in (spec.phrases, spec.negative):
        assert len(lst) <= MAX_PHRASES
        for text, pr in lst:
            assert text.strip() and len(text) <= MAX_PHRASE_CHARS and 0 <= pr <= 9
            text.encode("utf-8")
    json.dumps({"phrases": [list(x) for x in spec.phrases]})


def test_empty_free_text_leaves_no_blank_phrase():
    spec = build_prompt(woman(appearance=replace(APPEARANCE, hair_style="", face=()),
                              photo={**PHOTO, "setting": "", "outfit": " ", "expression": ""}))
    assert all(t.strip() for t, _ in spec.phrases)
    assert ("shoulder-length dark brown hair", 1) in spec.phrases


def test_a_lone_surrogate_never_reaches_the_prompt():
    spec = build_prompt(woman(appearance=replace(APPEARANCE, hair_style="curly\ud800")))
    for t, _ in spec.phrases:
        t.encode("utf-8")
