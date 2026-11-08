"""The mind's profile-photo prompt (P2 Task 6): persona -> prioritised phrases and negative.
Every person here is invented."""
import importlib.util
import json
import logging
import re
from pathlib import Path
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.selfies.prompt import (LUST_HELD, LUST_NEGATIVE, NEGATIVE, PromptSpec,
                                        BUILD_WORDS, build_prompt, renderable)
from leeroy_mind.selfies.style import LUST, PLAIN, suggestive
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
    assert ("average build", 1) in ph
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


TODAY_NEGATIVE = (
    ("cartoon, anime, painting, illustration, 3d render, cgi", 0),
    ("nsfw, nude, cleavage, lingerie, swimwear", 0),
    ("child, teenager", 0),
    ("celebrity, famous person", 0),
    ("plastic skin, airbrushed, doll, deformed face, extra fingers, bad hands", 1),
    ("vintage, retro, film grain, sepia, old photo", 1),
    ("watermark, text, logo", 2),
)


def test_a_lust_womans_negative_drops_only_cleavage_and_swimwear():
    neg = build_prompt(woman(stance="lust"), kind_style=LUST).negative
    assert neg == LUST_NEGATIVE
    words = [w.strip() for t, _ in neg for w in t.split(",")]
    assert "cleavage" not in words and "swimwear" not in words
    zero = [w.strip() for t, pr in neg if pr == 0 for w in t.split(",")]
    for word in ("nsfw", "nude", "lingerie", "child", "teenager", "topless", "underwear",
                 "see-through", "sheer", "celebrity", "cartoon"):
        assert word in zero, word
    others = [x for x in TODAY_NEGATIVE if x[0] != "nsfw, nude, cleavage, lingerie, swimwear"]
    assert [x for x in neg if x in others] == others and len(neg) == len(TODAY_NEGATIVE)
    # the worker keeps every priority-0 phrase inside 75 tokens; words and marks bound them here
    marks = re.findall(r"\w+|[^\w\s]", ", ".join(t for t, pr in neg if pr == 0))
    assert len(marks) <= 60


@pytest.mark.parametrize("p", [
    lambda: woman(), lambda: woman(stance="fond"), lambda: woman(stance="drawn"),
    lambda: woman(stance="lust", age=20), lambda: man(), lambda: man(age=40),
    lambda: woman(stance="lust")])
@pytest.mark.parametrize("chosen", [PLAIN, LUST])
def test_everyone_else_keeps_todays_negative(p, chosen):
    # a lust woman whose kind the lust rules did not compose (by hand, plain, none recorded)
    # renders with the plain negative too; a lust-composed kind on anyone else waits
    person = p()
    if suggestive(person) and chosen == LUST:
        return
    if chosen == LUST:
        assert renderable(person, LUST) == LUST_HELD
        with pytest.raises(ValueError, match="re-choice"):
            build_prompt(person, kind_style=LUST)
        return
    assert NEGATIVE == TODAY_NEGATIVE
    spec = build_prompt(person, kind_style=chosen)
    assert spec.negative == TODAY_NEGATIVE
    assert (person.photo.outfit, 2) in spec.phrases and (person.photo.setting, 2) in spec.phrases


LUST_PHOTO = ProfilePhoto("snapshot", "upper_body", "the beach at sunset", "a red string bikini",
                          "a teasing grin")


def test_the_lust_prompt_puts_outfit_and_setting_in_one_phrase_at_priority_0():
    plain = build_prompt(woman(stance="fond", photo=LUST_PHOTO))
    hot = build_prompt(woman(stance="lust", photo=LUST_PHOTO), kind_style=LUST)
    joined = ("wearing a red string bikini, the beach at sunset", 0)
    assert joined in hot.phrases and joined not in plain.phrases
    assert ("a red string bikini", 2) in plain.phrases and ("the beach at sunset", 2) in plain.phrases
    assert not any(t in ("a red string bikini", "the beach at sunset") for t, _ in hot.phrases)
    rest = [x for x in plain.phrases if x not in (("a red string bikini", 2),
                                                  ("the beach at sunset", 2))]
    assert [x for x in hot.phrases if x != joined] == rest
    assert hot.negative == LUST_NEGATIVE and plain.negative == NEGATIVE
    assert build_prompt(woman(stance="lust", photo=LUST_PHOTO)).phrases == plain.phrases


def _worker_fit():
    """The selfie worker's own fit (selfie/leeroy_selfie/fit.py: no dependencies), loaded by
    path: the mind never imports the worker's package."""
    path = Path(__file__).resolve().parents[3] / "selfie" / "leeroy_selfie" / "fit.py"
    spec = importlib.util.spec_from_file_location("_selfie_fit_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fit


def _tokens(text: str) -> int:
    """More than CLIP counts: a token per mark and per started run of four letters."""
    return sum(max(1, -(-len(w) // 4))
               for w in re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]", text))


def test_the_longest_lust_kind_keeps_outfit_and_setting_through_the_workers_fit():
    from leeroy_mind.selfies.kind import LUST_COLOURS, LUST_OUTFITS, LUST_SETTINGS, compose_lust
    fit = _worker_fit()
    outfit = max((compose_lust({"kind": "snapshot", "framing": "head_and_shoulders",
                                "setting": "a cafe terrace", "outfit": o, "colour": c,
                                "expression": "a wink"}).outfit
                  for o in LUST_OUTFITS for c in LUST_COLOURS), key=len)
    setting = max(LUST_SETTINGS, key=len)
    big = replace(APPEARANCE, hair_style="h" * 40, face=("f" * 40,) * 4)
    for kind_, framing in (("snapshot", "head_and_shoulders"), ("mirror_selfie", "upper_body")):
        photo = ProfilePhoto(kind_, framing, setting, outfit, "an over-the-shoulder glance")
        spec = build_prompt(woman(stance="lust", age=25, appearance=big, photo=photo),
                            kind_style=LUST)
        kept, dropped = fit(spec.phrases, _tokens, 75)
        assert f"wearing {outfit}, {setting}" in kept and outfit not in " ".join(dropped)
        assert setting not in " ".join(dropped)
        assert any("f" * 40 == d for d in dropped)        # the long looks went, not the kind


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


def test_no_prompt_is_built_for_anyone_under_eighteen():
    # final review minor 9: the adults-only rule held here too, not only at the schema's save
    for age in (17, 12, 0):
        assert renderable(woman(age=age)) == "under age"
        with pytest.raises(ValueError, match="under age"):
            build_prompt(woman(age=age))
    assert renderable(woman(age=18)) is None and build_prompt(woman(age=18)).phrases


@pytest.mark.parametrize("looks", [
    {"hair_style": "in two pigtails"}, {"hair_style": "pig tails"},
    {"face": ("a youthful face",)}, {"face": ("braces on her teeth",)},
    {"face": ("baby-faced", "freckles")}, {"face": ("a babyface",)},
    {"skin_tone": "teenage-clear"}, {"hair_colour": "schoolgirl blonde"}])
def test_a_lust_kind_is_never_rendered_when_her_looks_code_young(looks):
    young = woman(stance="lust", appearance=replace(APPEARANCE, **looks), photo=LUST_PHOTO)
    assert not suggestive(young)                          # no lust style for her at all
    assert renderable(young, LUST) == LUST_HELD
    with pytest.raises(ValueError, match="re-choice"):
        build_prompt(young, kind_style=LUST)             # its text is never rendered plainly
    assert renderable(young) is None and build_prompt(young).negative == NEGATIVE


def test_a_lust_kind_is_never_rendered_once_she_left_lust():
    for stance in ("drawn", "fond", "none"):
        left = woman(stance=stance, photo=LUST_PHOTO)
        assert renderable(left, LUST) == LUST_HELD
        with pytest.raises(ValueError, match="re-choice"):
            build_prompt(left, kind_style=LUST)
    assert renderable(woman(stance="lust", age=20, photo=LUST_PHOTO), LUST) == LUST_HELD


def test_the_lust_prompt_stays_when_her_looks_are_adult():
    hot = woman(stance="lust", photo=LUST_PHOTO)
    assert renderable(hot, LUST) is None
    assert build_prompt(hot, kind_style=LUST).negative == LUST_NEGATIVE


def test_every_chosen_build_is_said_as_a_figure_at_priority_zero():
    curvy = woman()
    vol = replace(curvy, appearance=replace(curvy.appearance, build="voluptuous"))
    assert ("voluptuous full-figured body, large full bust, wide hips, soft curves", 0) in build_prompt(vol).phrases
    # woman() is average: optional, after her looks
    assert (BUILD_WORDS[curvy.appearance.build], 1) in build_prompt(curvy).phrases


def test_an_average_build_stays_optional_and_a_chosen_one_is_required():
    """A build other than average is priority 0, so the 75-token fit never cuts it (2026-11-08:
    two lust-style members' voluptuous phrase was cut behind their long outfit phrase)."""
    base = woman()
    avg = replace(base, appearance=replace(base.appearance, build="average"))
    vol = replace(base, appearance=replace(base.appearance, build="voluptuous"))
    assert (BUILD_WORDS["average"], 1) in build_prompt(avg).phrases
    assert (BUILD_WORDS["voluptuous"], 0) in build_prompt(vol).phrases
    assert (BUILD_WORDS["voluptuous"], 1) not in build_prompt(vol).phrases


def test_a_chosen_build_comes_right_after_who_she_is():
    """The text encoders weigh early phrases more: a chosen build follows the age and nationality
    phrase, before her hair and skin; average stays after her looks."""
    base = woman()
    vol = replace(base, appearance=replace(base.appearance, build="voluptuous"))
    texts = [t for t, _ in build_prompt(vol).phrases]
    i = texts.index(BUILD_WORDS["voluptuous"])
    assert "year-old" in texts[i - 1] or "looking at the camera" in texts[i - 1]
    assert all("hair" not in t for t in texts[:i])
    assert "wide hips" in BUILD_WORDS["curvy"]
