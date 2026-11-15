"""Weight and bust in the looks and the photo prompts (owner, 2026-11-15: curvy and voluptuous
members rendered slim): the optional bust field, the body size from weight and height, and both
inside the worker's 75-token fit. Every person here is invented; no model, GPU or real worker."""
import functools
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_persona
from leeroy_mind.nightly.enrich import ENRICH_SCHEMA, merge
from leeroy_mind.personas.schema import (BUSTS, PersonaError, ProfilePhoto, appearance_dict,
                                         persona_from_dict, persona_to_dict, validate_persona)
from leeroy_mind.selfies import kind, varied
from leeroy_mind.selfies.kind import EXPLICIT_RE, WEAR_RE
from leeroy_mind.selfies.prompt import (_FRAMINGS, _KINDS, BUILD_SHORT, BUILD_WORDS, BUST_WORDS,
                                        FIT_MAX, LUST_NEGATIVE, NEGATIVE, PLAIN, SHORT,
                                        SIZE_WORDS,
                                        body_phrases, body_words, build_prompt,
                                        build_varied_prompt, build_words, hair_skin,
                                        size_words, varied_parts)
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.style import AGE_RE, LUST, suggestive
from leeroy_mind.selfies.tokens import estimate, joined
from nightly.test_night_enrich import APPEARANCE, LOOKS
from selfies import clip_bpe

PHOTO = ProfilePhoto("snapshot", "upper_body", "a quiet cafe", "a grey hoodie", "a small smile")
TALL = 164  # cm: 49 kg is BMI 18.2, 64 kg 23.8, 80 kg 29.7, 88 kg 32.7, 100 kg 37.2


def woman(**looks):
    p = make_persona(101, "Arkon", player_gender="female", age=34, stance="lust")
    return replace(p, appearance=replace(APPEARANCE, height_cm=TALL, **looks), photo=PHOTO)


def man(**looks):
    p = make_persona(102, "Belra", age=34)
    return replace(p, appearance=replace(APPEARANCE, height_cm=TALL, **looks), photo=PHOTO)


def _worker_fit():
    path = Path(__file__).resolve().parents[3] / "selfie" / "leeroy_selfie" / "fit.py"
    spec = importlib.util.spec_from_file_location("_selfie_fit_for_weight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fit


# -- the schema ---------------------------------------------------------------------------------

def test_looks_stored_before_the_bust_existed_load_and_save_unchanged():
    p = woman()
    d = persona_to_dict(p)
    assert "bust" not in d["appearance"] and p.appearance.bust is None
    assert persona_from_dict(json.loads(json.dumps(d))) == p   # an old file, as it was written
    # the looks' hash (a planned photo's) is what it was before the field existed
    old = {k: v for k, v in LOOKS.items()} | {"height_cm": TALL}
    assert looks_hash(p.appearance) == hashlib.sha256(
        json.dumps(old, sort_keys=True).encode("utf-8")).hexdigest()
    big = replace(p, appearance=replace(p.appearance, bust="very_large"))
    assert persona_to_dict(big)["appearance"]["bust"] == "very_large"
    assert persona_from_dict(persona_to_dict(big)) == big and validate_persona(big) == []
    assert looks_hash(big.appearance) != looks_hash(p.appearance)  # a planned photo expires
    assert appearance_dict(big.appearance)["bust"] == "very_large"


@pytest.mark.parametrize("bust", ["huge", "Large", "", 3, True, ["large"]])
def test_a_bust_off_the_list_is_refused(bust):
    d = persona_to_dict(woman())
    d["appearance"]["bust"] = bust
    with pytest.raises(PersonaError, match="appearance.bust"):
        persona_from_dict(d)


def test_weight_keeps_its_bounds_and_a_man_has_no_bust():
    assert BUSTS == ("small", "medium", "large", "very_large")
    assert any("appearance.weight_kg: must be at least 40" in e
               for e in validate_persona(woman(weight_kg=39)))
    assert any("at most 180" in e for e in validate_persona(woman(weight_kg=181)))
    assert validate_persona(woman(weight_kg=180, bust="small")) == []
    assert "appearance.bust: women only; leave it empty" in validate_persona(man(bust="large"))
    assert validate_persona(man()) == []


def test_the_looks_pass_is_never_asked_for_a_bust_and_never_takes_one():
    assert "bust" not in ENRICH_SCHEMA["properties"]["appearance"]["properties"]
    data = {"appearance": {**LOOKS, "bust": "very_large"}, "drawn_to": [], "turned_off_by": [],
            "disclosures": []}
    assert merge(woman(), data).appearance.bust is None


# -- the words ----------------------------------------------------------------------------------

CHUBBY = "chubby, thick arms"
PLUS_W, PLUS_M = ("plus-size, heavyset, thick arms, full round face",
                  "overweight, heavyset, thick arms, full round face")
FULL_W, FULL_M = ("plus-size, heavyset, very full figure, full round face",
                  "very overweight, heavyset, full round face")


@pytest.mark.parametrize("kg,her,his", [  # at 164 cm
    (45, "slender", "very thin"), (49, "slender", "very thin"), (55, "slender", "lean"),
    (60, "", ""), (64, "", ""), (70, "slightly chubby", "slightly chubby"),
    (80, CHUBBY, CHUBBY), (88, PLUS_W, PLUS_M), (100, FULL_W, FULL_M), (180, FULL_W, FULL_M)])
def test_the_weight_is_said_as_a_body_size_by_its_bmi(kg, her, his):
    for build in ("slim", "average"):  # every band, as the BMI says
        assert size_words(woman(weight_kg=kg, build=build)) == her
        assert size_words(man(weight_kg=kg, build=build)) == his


def test_a_woman_is_never_said_very_thin():
    """A youthful cue (review of #316): a woman's lowest band is "slender"."""
    assert not any(w == "very thin" for _, w, _ in SIZE_WORDS)
    for person in (woman(weight_kg=40, build="slim"), man(weight_kg=40, build="slim")):
        tall = replace(person, appearance=replace(person.appearance, height_cm=190))
        said = size_words(tall)
        assert said == ("slender" if person.identity.player_gender == "female" else "very thin")


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "stocky", "heavy"])
@pytest.mark.parametrize("kg,her,his", [
    (45, "", ""), (49, "", ""), (55, "", ""), (60, "", ""), (64, "", ""),
    (70, "slightly chubby", "slightly chubby"), (80, CHUBBY, CHUBBY),
    (88, PLUS_W, PLUS_M), (100, FULL_W, FULL_M)])
def test_a_full_build_is_never_said_thin_only_heavier(build, kg, her, his):
    """Size words never pull against the build (controller ruling, 2026-11-15): a curvy,
    voluptuous, stocky or heavy build below BMI 25 has no size word, its build words alone."""
    assert size_words(woman(weight_kg=kg, build=build)) == her
    assert size_words(man(weight_kg=kg, build=build)) == his


def test_an_athletic_build_is_muscle_not_slightly_chubby():
    for kg in (66, 70, 73):  # BMI 24.5 to 27.1 at 164 cm
        assert size_words(woman(weight_kg=kg, build="athletic")) == ""
        assert size_words(man(weight_kg=kg, build="athletic")) == ""
    assert size_words(woman(weight_kg=55, build="athletic")) == "slender"
    assert size_words(woman(weight_kg=80, build="athletic")) == CHUBBY


@pytest.mark.parametrize("build", ["slim", "athletic", "average"])
def test_from_bmi_28_the_size_replaces_a_build_it_contradicts(build):
    """Controller ruling, 2026-11-15: a slim, athletic or average build at BMI 28 or more is said
    by its size alone; her bust, when set, stays as a phrase of its own."""
    assert body_words(woman(build=build, weight_kg=80)) == [CHUBBY]
    assert body_words(woman(build=build, weight_kg=100, bust="large")) == [FULL_W, "large full bust"]
    assert body_words(man(build=build, weight_kg=88)) == [PLUS_M]
    below = body_words(woman(build=build, weight_kg=64))
    assert below == [BUILD_WORDS[build]]  # the ordinary band: the build alone
    assert body_words(woman(build="curvy", weight_kg=100)) == [FULL_W, BUILD_WORDS["curvy"]]


def test_a_curvy_woman_of_bmi_20_renders_curvy_with_no_size_word():
    p = replace(woman(build="curvy"), appearance=replace(APPEARANCE, build="curvy",
                                                         height_cm=165, weight_kg=55))
    assert round(55 / 1.65 ** 2, 1) == 20.2 and size_words(p) == ""
    texts = _texts(build_prompt(p))
    assert (BUILD_WORDS["curvy"], 0) in build_prompt(p).phrases
    assert not any(t in texts for _, w, m in SIZE_WORDS for t in (w, m) if t)
    vol = replace(p, appearance=replace(p.appearance, build="voluptuous", bust="large"))
    assert size_words(vol) == "" and (build_words(vol), 0) in build_prompt(vol).phrases
    slim = replace(p, appearance=replace(p.appearance, build="slim"))
    assert size_words(slim) == "slender"  # a slim build keeps its band


def test_a_bust_set_is_said_in_place_of_the_builds_own_and_never_for_a_man():
    assert build_words(woman(build="curvy")) == BUILD_WORDS["curvy"]
    assert build_words(woman(build="curvy", bust="large")) == (
        "curvy hourglass figure, large full bust, wide hips, narrow waist")
    assert build_words(woman(build="voluptuous", bust="small")) == (
        "voluptuous full-figured body, small bust, wide hips, soft curves")
    assert build_words(woman(build="voluptuous", bust="very_large")) == (
        "voluptuous full-figured body, very large full bust, wide hips, soft curves")
    assert build_words(woman(build="average", bust="medium")) == "average build, medium bust"
    assert build_words(woman(build="slim", bust="small")) == "slim figure, small bust"
    # a man's bust never reaches a prompt, even one that got past the schema somehow
    assert build_words(man(build="stocky", bust="large")) == BUILD_WORDS["stocky"]


def test_the_short_forms_keep_the_first_size_term_the_figure_and_the_bust():
    p = woman(build="voluptuous", weight_kg=100, bust="very_large")
    assert body_words(p) == [FULL_W, build_words(p)]
    assert body_words(p, short=True) == ["plus-size", "voluptuous figure, very large full bust"]
    assert body_words(woman(build="curvy", weight_kg=80), short=True) == [
        "chubby", "curvy hourglass figure, full bust"]
    assert body_words(woman(build="slim", weight_kg=100, bust="small"), short=True) == [
        "plus-size", "small bust"]
    assert body_words(man(build="stocky", weight_kg=100), short=True) == [
        "very overweight", "stocky build"]
    for p in (woman(build="voluptuous", weight_kg=100), woman(build="average", bust="large")):
        assert len(body_words(p)) == len(body_words(p, short=True))


def test_every_word_is_clothed_adult_and_on_the_token_table():
    from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS
    sizes = [w for _, a, b in SIZE_WORDS for w in (a, b)]
    for text in sizes + list(BUST_WORDS.values()) + list(BUILD_SHORT.values()):
        assert not EXPLICIT_RE.search(text) and not WEAR_RE.search(text), text
        assert not AGE_RE.search(text) and "cleavage" not in text and "petite" not in text
        for piece in PIECES.findall(text.lower()):
            assert not piece.isalpha() or piece in WORD_TOKENS, piece
    assert not any("bust" in w or "belly" in w for w in sizes)  # men's words have no bust words
    assert max(estimate(w) for w in sizes) == 14


def test_the_negatives_are_unchanged():
    p = woman(build="voluptuous", weight_kg=100, bust="very_large")
    assert build_prompt(p).negative == NEGATIVE
    assert build_prompt(replace(p, photo=_lust_kind()), kind_style=LUST).negative == LUST_NEGATIVE
    assert "cleavage" in " ".join(t for t, _ in NEGATIVE)


# -- the profile prompt -------------------------------------------------------------------------

HAIR = "shoulder-length dark brown hair, tucked behind one ear"
SKIN = "light olive skin"


def _texts(spec, priority=None):
    return [t for t, pr in spec.phrases if priority is None or pr == priority]


def _after_subject(spec):
    """The phrases after who she is, in order."""
    texts = _texts(spec)
    at = next(i for i, t in enumerate(texts) if "-year-old" in t)
    return texts[at + 1:]


def test_an_ordinary_weight_and_no_bust_say_only_the_build():
    for build in ("curvy", "voluptuous", "slim"):  # a chosen build: right after who she is
        spec = build_prompt(woman(build=build, weight_kg=64))
        assert _after_subject(spec)[:3] == [BUILD_WORDS[build], HAIR, SKIN]
        assert not any(t in _texts(spec) for _, w, m in SIZE_WORDS for t in (w, m) if t)
    avg = build_prompt(woman(weight_kg=64))  # a plain average build: optional, after her looks
    assert (BUILD_WORDS["average"], 1) in avg.phrases
    assert _after_subject(avg)[:2] == [HAIR, SKIN]
    assert _texts(avg).index(BUILD_WORDS["average"]) > _texts(avg).index("hazel eyes")


@pytest.mark.parametrize("kind_", ["selfie", "mirror_selfie", "headshot", "snapshot"])
def test_her_size_build_hair_and_skin_follow_who_she_is_all_required(kind_):
    """Controller rulings, 2026-11-15: the required set is who she is, her body (size, then build
    with her bust), her hair and her skin, in that order, for plain and lust kinds alike."""
    p = woman(build="voluptuous", weight_kg=88, bust="very_large")
    p = replace(p, identity=replace(p.identity, age=36, region="US"))
    long, short = body_words(p), body_words(p, short=True)
    assert long == [PLUS_W, build_words(p)]
    for photo, style in ((replace(PHOTO, kind=kind_), PLAIN),
                         (replace(_lust_kind(), kind=kind_), LUST)):
        spec = build_prompt(replace(p, photo=photo), kind_style=style)
        texts = _texts(spec)
        at = texts.index("36-year-old American woman")
        body = texts[at + 1:at + 3]
        assert all(body[i] in (long[i], short[i]) for i in range(2))  # short when room is tight
        assert texts[at + 3:at + 5] == [HAIR, SKIN]
        for text in (*body, HAIR, SKIN):
            assert (text, 0) in spec.phrases
    plain = build_prompt(replace(p, photo=replace(PHOTO, kind=kind_, framing="face")))
    assert (PLUS_W, 0) in plain.phrases and (build_words(p), 0) in plain.phrases  # room: long


def test_a_bust_or_a_size_on_an_average_build_makes_the_build_required():
    set_ = woman(build="average", bust="large")
    assert _after_subject(build_prompt(set_))[0] == "average build, large full bust"
    assert ("average build, large full bust", 0) in build_prompt(set_).phrases
    heavy = build_prompt(woman(build="average", weight_kg=70))
    assert _after_subject(heavy)[:2] == ["slightly chubby", "average build"]
    assert ("average build", 0) in heavy.phrases and ("slightly chubby", 0) in heavy.phrases
    plus = build_prompt(woman(build="average", weight_kg=88))  # the size replaces the build
    assert _after_subject(plus)[:3] == [PLUS_W, HAIR, SKIN]
    assert BUILD_WORDS["average"] not in _texts(plus)


def _lust_kind(kind_="snapshot", framing="upper_body", setting="a cafe terrace",
               outfit="a red wrap dress"):
    return ProfilePhoto(kind_, framing, setting, outfit, "a playful smile")


@functools.cache
def _longest_lust_phrase():
    """The longest outfit and setting the lust lists compose, by the mind's count (estimate):
    the one part of the ordinary lust prompt's required set that varies with the kind."""
    best = None
    base = woman()
    for o in kind.LUST_OUTFITS:
        for c in kind.LUST_COLOURS:
            for s in kind.LUST_SETTINGS:
                data = {"kind": "snapshot", "framing": "upper_body", "setting": s, "outfit": o,
                        "colour": c, "expression": "a wink"}
                if kind.screen_lust(base, data):
                    continue
                ph = kind.compose_lust(data)
                n = estimate(f"wearing {ph.outfit}, {ph.setting}")
                if best is None or n > best[0]:
                    best = (n, ph)
    return best[1]


LONG_LOOKS = dict(hair_colour="honey blonde with highlights",
                  hair_style="loose beach waves over one shoulder", skin_tone="warm golden olive")


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "average", "heavy"])
@pytest.mark.parametrize("bust", [None, "very_large"])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_the_longest_lust_prompt_fits_and_keeps_her_hair_and_skin(build, bust, looks):
    """The required set counts at most 75 by the mind's own count, never below the real one, so
    the worker never refuses it. Who she is, her hair and skin and the outfit with its setting
    are always required; her body words are too, in their short forms when the room needs it;
    then the kind and the framing take their short forms, and only then yield, keeping their
    places for the worker's fit."""
    fit = _worker_fit()
    longest = _longest_lust_phrase()
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            ph = replace(longest, kind=kind_, framing=framing)
            for kg in (49, 64, 80, 88, 100, 180):
                p = woman(build=build, bust=bust, weight_kg=kg, **looks)
                spec = build_prompt(replace(p, photo=ph), kind_style=LUST)
                required = _texts(spec, 0)
                assert joined(required) <= FIT_MAX, (framing, kind_, kg)
                for text, _ in hair_skin(p, None):
                    assert text in required
                long, short = body_words(p), body_words(p, short=True)
                if not body_phrases(p)[0]:  # a plain average build: optional, as before
                    long = short = []
                for i, text in enumerate(long):
                    assert text in required or short[i] in required
                assert f"wearing {ph.outfit}, {ph.setting}" in required
                assert any("-year-old" in t for t in required)
                for text in (_KINDS[kind_], _FRAMINGS[framing]):
                    forms = {text, SHORT.get(text, text)}
                    assert forms & set(required) or forms & set(_texts(spec, 1))
                kept, dropped = fit(spec.phrases, estimate, 75)
                assert not set(required) & set(dropped)


def test_the_body_shortens_then_the_kind_and_framing_and_only_then_they_yield():
    p = woman(build="voluptuous", bust="very_large", weight_kg=100, **LONG_LOOKS)
    ph = replace(_longest_lust_phrase(), kind="snapshot", framing="three_quarter")
    spec = build_prompt(replace(p, photo=ph), kind_style=LUST)
    assert ("plus-size", 0) in spec.phrases
    assert ("voluptuous figure, very large full bust", 0) in spec.phrases
    assert _texts(spec)[:2] == ["candid photo", "three-quarter shot, waist and hips in frame"]
    assert SHORT == {"candid photo taken by a friend": "candid photo",
                     "professional headshot": "headshot",
                     "waist-up shot, upper body": "waist-up shot",
                     _FRAMINGS["three_quarter"]: "three-quarter shot, waist and hips in frame"}
    medium = build_prompt(replace(woman(build="voluptuous", weight_kg=80), photo=PHOTO))
    assert (CHUBBY, 0) in medium.phrases  # a plain kind has room for the long forms


def test_an_ordinary_plain_prompt_always_requires_the_size_and_keeps_its_kind():
    for build in ("curvy", "voluptuous", "average", "stocky"):
        for kg in (49, 80, 100, 180):
            p = woman(build=build, weight_kg=kg, bust="very_large")
            for framing in ("face", "three_quarter"):
                spec = build_prompt(replace(p, photo=replace(PHOTO, framing=framing)))
                if size_words(p):  # a full build below BMI 25 has none; short when room is tight
                    said = {(size_words(p), 0), (size_words(p, short=True), 0)}
                    assert said & set(spec.phrases)
                assert joined(_texts(spec, 0)) <= FIT_MAX
                assert (_KINDS["snapshot"], 0) in spec.phrases  # a plain kind has room


# -- the varied set -----------------------------------------------------------------------------

def _looks():
    for category, (outfits, settings) in kind.VARIED_LOOKS.items():
        for outfit in outfits:
            for setting in settings:
                for view in kind.VARIED_VIEWS:
                    for framing in kind.VARIED_FRAMINGS:
                        if framing == "three_quarter" and (
                                view not in kind.BACK_VIEWS or category == "swim"):
                            continue
                        yield varied.Look(category, view, framing, setting, outfit,
                                          kind.LUST_COLOURS[0], "an over-the-shoulder glance")


@pytest.mark.parametrize("build", ["curvy", "voluptuous"])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_a_varied_set_requires_her_body_hair_and_skin_right_after_who_she_is(build, looks):
    p = woman(build=build, weight_kg=100, bust="very_large", **looks)
    assert suggestive(p)
    long, short = body_words(p), body_words(p, short=True)
    for look in _looks():
        spec = build_varied_prompt(p, look)
        required = _texts(spec, 0)
        assert joined(required) <= FIT_MAX, look
        after = _after_subject(spec)
        assert all(after[i] in (long[i], short[i]) for i in range(len(long)))
        assert after[len(long):len(long) + 2] == [t for t, _ in hair_skin(p, None)]
        for text in after[:len(long) + 2]:
            assert text in required
        parts = varied_parts(look, look.phrase)
        assert parts["wearing"] in required
        if parts["view"] not in required:  # the framing yields before the pose
            assert parts["framing"] not in required


def test_a_varied_set_says_hair_and_skin_right_after_her_body():
    p = woman(build="curvy", weight_kg=80)
    look = varied.Look("gym", "front", "full_body", "the gym", "sports top and leggings", "red",
                       "laughing")
    phrases = build_varied_prompt(p, look).phrases
    texts = [t for t, _ in phrases]
    assert (CHUBBY, 0) in phrases and (HAIR, 0) in phrases and (SKIN, 0) in phrases
    assert texts.index(CHUBBY) + 2 == texts.index(HAIR)
    plain = build_varied_prompt(woman(), look)  # a plain average build stays required here
    assert (BUILD_WORDS["average"], 0) in plain.phrases


# -- the real tokenizers, when their files are on this machine ----------------------------------

_TOKENIZERS = pytest.mark.skipif(clip_bpe.tokenizers() is None,
                                 reason="RealVisXL tokenizer files are not on this machine")


@_TOKENIZERS
def test_the_new_words_count_what_the_real_tokenizers_count():
    from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS
    texts = [w for _, a, b in SIZE_WORDS for w in (a, b) if w] + list(BUST_WORDS.values())
    for text in texts + list(BUILD_SHORT.values()):
        assert estimate(text) >= clip_bpe.count(text), text
        for piece in PIECES.findall(text.lower()):
            if piece.isalpha() and piece in WORD_TOKENS:
                assert clip_bpe.count(piece) == WORD_TOKENS[piece], piece
    for text in texts:
        assert estimate(text) == clip_bpe.count(text), text
    assert [clip_bpe.count(w) for w in ("slightly chubby", CHUBBY, PLUS_W, FULL_W)] == [2, 4, 13, 14]
    assert [clip_bpe.count(w) for w in (PLUS_M, FULL_M)] == [11, 9]


@_TOKENIZERS
@pytest.mark.parametrize("build", ["curvy", "voluptuous"])
def test_the_heaviest_longest_prompts_need_at_most_75_real_tokens(build):
    longest = _longest_lust_phrase()
    p = woman(build=build, bust="very_large", weight_kg=180, **LONG_LOOKS)
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            ph = replace(longest, kind=kind_, framing=framing)
            required = _texts(build_prompt(replace(p, photo=ph), kind_style=LUST), 0)
            real = sum(clip_bpe.count(t) for t in required) + len(required) - 1
            assert real <= 75 and joined(required) >= real, (framing, kind_)
    seen: dict[str, int] = {}
    for look in _looks():
        required = _texts(build_varied_prompt(p, look), 0)
        real = sum(seen.setdefault(t, clip_bpe.count(t)) for t in required) + len(required) - 1
        assert real <= 75, look


# -- the situational photos ---------------------------------------------------------------------

def test_a_situational_photo_says_her_body_hair_and_skin_right_after_who_she_is():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt, scene_from
    from selfies.test_selfie_scene import LUST_SCENE, SCENE
    p = woman(build="voluptuous", weight_kg=88, bust="large")
    spec = build_scene_prompt(p, Scene(**SCENE))
    assert _after_subject(spec)[:2] == [PLUS_W, build_words(p)]
    for text in (PLUS_W, build_words(p), HAIR, SKIN):
        assert (text, 0) in spec.phrases
    plain = build_scene_prompt(woman(build="voluptuous"), Scene(**SCENE))
    assert _after_subject(plain)[0] == BUILD_WORDS["voluptuous"]
    assert not any(t in _texts(plain) for _, w, m in SIZE_WORDS for t in (w, m) if t)
    avg = build_scene_prompt(woman(), Scene(**SCENE))
    assert (BUILD_WORDS["average"], 1) in avg.phrases  # a plain average build stays optional
    heavy = woman(build="voluptuous", weight_kg=180, bust="very_large", **LONG_LOOKS)
    hot = build_scene_prompt(heavy, scene_from(LUST_SCENE))
    assert joined(_texts(hot, 0)) <= FIT_MAX
    assert all(t in _texts(hot, 0) for t, _ in hair_skin(heavy, None))
