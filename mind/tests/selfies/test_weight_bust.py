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
from leeroy_mind.selfies.prompt import (BUILD_WORDS, BUST_WORDS, LUST_NEGATIVE, NEGATIVE,
                                        REQUIRED_MAX, SIZE_WORDS, build_prompt,
                                        build_varied_prompt, build_words, size_words)
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

@pytest.mark.parametrize("kg,her,his", [
    (45, "very thin", "very thin"), (49, "very thin", "very thin"), (55, "slender", "lean"),
    (60, "", ""), (64, "", ""), (70, "slightly chubby", "slightly chubby"),
    (80, "chubby", "chubby"), (88, "plus-size", "overweight"),
    (100, "plus-size, overweight", "very overweight"), (180, "plus-size, overweight", "very overweight")])
def test_the_weight_is_said_as_a_body_size_by_its_bmi(kg, her, his):
    for build in ("slim", "athletic", "average"):  # every band, as the BMI says
        assert size_words(woman(weight_kg=kg, build=build)) == her
        assert size_words(man(weight_kg=kg, build=build)) == his


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "stocky", "heavy"])
@pytest.mark.parametrize("kg,her,his", [
    (45, "", ""), (49, "", ""), (55, "", ""), (60, "", ""), (64, "", ""),
    (70, "slightly chubby", "slightly chubby"), (80, "chubby", "chubby"),
    (88, "plus-size", "overweight"), (100, "plus-size, overweight", "very overweight")])
def test_a_full_build_is_never_said_thin_only_heavier(build, kg, her, his):
    """Size words never pull against the build (controller ruling, 2026-11-15): a curvy,
    voluptuous, stocky or heavy build below BMI 25 has no size word, its build words alone."""
    assert size_words(woman(weight_kg=kg, build=build)) == her
    assert size_words(man(weight_kg=kg, build=build)) == his


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


def test_every_word_is_clothed_adult_and_on_the_token_table():
    from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS
    words = [w for _, a, b in SIZE_WORDS for w in (a, b)] + list(BUST_WORDS.values())
    for text in words:
        assert not EXPLICIT_RE.search(text) and not WEAR_RE.search(text), text
        assert not AGE_RE.search(text) and "cleavage" not in text and "petite" not in text
        for piece in PIECES.findall(text.lower()):
            assert not piece.isalpha() or piece in WORD_TOKENS, piece
        assert estimate(text) <= 5  # short on purpose: it fits beside the longest required set


def test_the_negatives_are_unchanged():
    p = woman(build="voluptuous", weight_kg=100, bust="very_large")
    assert build_prompt(p).negative == NEGATIVE
    assert build_prompt(replace(p, photo=_lust_kind()), kind_style=LUST).negative == LUST_NEGATIVE
    assert "cleavage" in " ".join(t for t, _ in NEGATIVE)


# -- the profile prompt -------------------------------------------------------------------------

def _texts(spec, priority=None):
    return [t for t, pr in spec.phrases if priority is None or pr == priority]


def test_an_ordinary_weight_and_no_bust_leave_the_prompt_as_it_was():
    for build in ("average", "curvy", "voluptuous", "slim"):
        p = woman(build=build, weight_kg=64)
        texts = _texts(build_prompt(p))
        assert not any(t for _, w, m in SIZE_WORDS for t in (w, m) if t and t in texts)
        assert BUILD_WORDS[build] in texts


def test_her_weight_shows_right_after_her_build_as_a_required_phrase():
    p = woman(build="curvy", weight_kg=88)
    texts = _texts(build_prompt(p))
    i = texts.index(BUILD_WORDS["curvy"])
    assert texts[i + 1] == "plus-size" and ("plus-size", 0) in build_prompt(p).phrases
    assert all("hair" not in t for t in texts[:i + 2])  # before her hair: weighed more


def test_a_bust_set_on_an_average_build_makes_the_build_required():
    avg = woman(build="average")
    assert (BUILD_WORDS["average"], 1) in build_prompt(avg).phrases
    set_ = woman(build="average", bust="large")
    assert ("average build, large full bust", 0) in build_prompt(set_).phrases
    assert ("slightly chubby", 0) in build_prompt(woman(build="average", weight_kg=70)).phrases


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


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "average", "heavy"])
@pytest.mark.parametrize("bust", [None, "very_large"])
def test_the_longest_lust_prompt_stays_inside_the_workers_limit_with_any_weight(build, bust):
    """The required set (kind, framing, who she is, build with her bust, the outfit and setting)
    counts at most 75 by the mind's own count, which is never below the real one; her size is
    required while the set with it counts at most REQUIRED_MAX, else the first optional phrase,
    which the worker's fit keeps while there is room."""
    fit = _worker_fit()
    longest = _longest_lust_phrase()
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            ph = replace(longest, kind=kind_, framing=framing)
            for kg in (49, 64, 80, 88, 100, 180):
                p = woman(build=build, bust=bust, weight_kg=kg)
                spec = build_prompt(replace(p, photo=ph), kind_style=LUST)
                required = _texts(spec, 0)
                assert joined(required) <= 75, (framing, kind_, kg)
                size = size_words(p)
                if not size:
                    continue
                if (size, 0) in spec.phrases:
                    assert joined(required) <= REQUIRED_MAX
                else:
                    assert _texts(spec, 1)[0] == size  # the first the fit offers a place
                    kept, dropped = fit(spec.phrases, estimate, 75)
                    assert size in kept or joined(required) + estimate(size) + 1 > 75


def test_an_ordinary_plain_prompt_always_requires_the_size():
    for build in ("curvy", "voluptuous", "average", "stocky"):
        for kg in (49, 80, 100, 180):
            p = woman(build=build, weight_kg=kg, bust="very_large")
            for framing in ("face", "three_quarter"):
                spec = build_prompt(replace(p, photo=replace(PHOTO, framing=framing)))
                if size_words(p):  # a full build below BMI 25 has none
                    assert (size_words(p), 0) in spec.phrases
                assert joined(_texts(spec, 0)) <= REQUIRED_MAX


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
def test_a_varied_set_says_her_size_and_bust_and_keeps_five_tokens_to_spare(build):
    p = woman(build=build, weight_kg=100, bust="very_large")
    assert suggestive(p)
    shown = 0
    for look in _looks():
        spec = build_varied_prompt(p, look)
        required = _texts(spec, 0)
        assert joined(required) <= REQUIRED_MAX, look
        assert build_words(p) in required
        texts = _texts(spec)
        assert "plus-size, overweight" in texts  # at 0 while it fits, else the first at 1
        i = texts.index("plus-size, overweight")
        assert texts[i - 1] == build_words(p)
        shown += ("plus-size, overweight", 0) in spec.phrases
    assert shown  # required for every look short enough, not only offered


def test_a_varied_size_comes_before_her_hair_and_skin_in_the_required_chain():
    p = woman(build="curvy", weight_kg=80)
    look = varied.Look("gym", "front", "full_body", "the gym", "sports top and leggings", "red",
                       "laughing")
    phrases = build_varied_prompt(p, look).phrases
    texts = [t for t, _ in phrases]
    assert ("chubby", 0) in phrases
    assert texts.index("chubby") < min(i for i, t in enumerate(texts) if " hair" in t)


# -- the real tokenizers, when their files are on this machine ----------------------------------

_TOKENIZERS = pytest.mark.skipif(clip_bpe.tokenizers() is None,
                                 reason="RealVisXL tokenizer files are not on this machine")


@_TOKENIZERS
def test_the_new_words_count_what_the_real_tokenizers_count():
    from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS
    for text in [w for _, a, b in SIZE_WORDS for w in (a, b) if w] + list(BUST_WORDS.values()):
        assert estimate(text) >= clip_bpe.count(text), text
        for piece in PIECES.findall(text.lower()):
            if piece.isalpha():
                assert clip_bpe.count(piece) == WORD_TOKENS[piece], piece


@_TOKENIZERS
@pytest.mark.parametrize("build", ["curvy", "voluptuous"])
def test_the_heaviest_longest_lust_prompt_needs_at_most_75_real_tokens(build):
    longest = _longest_lust_phrase()
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            ph = replace(longest, kind=kind_, framing=framing)
            p = woman(build=build, bust="very_large", weight_kg=180)
            required = _texts(build_prompt(replace(p, photo=ph), kind_style=LUST), 0)
            real = sum(clip_bpe.count(t) for t in required) + len(required) - 1
            assert real <= 75 and joined(required) >= real, (framing, kind_)


# -- the situational photos ---------------------------------------------------------------------

def test_a_situational_photo_says_her_size_and_bust_beside_her_build():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt
    from selfies.test_selfie_scene import SCENE
    p = woman(build="voluptuous", weight_kg=88, bust="large")
    spec = build_scene_prompt(p, Scene(**SCENE))
    texts = _texts(spec)
    assert (build_words(p), 1) in spec.phrases and ("plus-size", 1) in spec.phrases
    assert texts.index("plus-size") == texts.index(build_words(p)) + 1
    plain = build_scene_prompt(woman(build="voluptuous"), Scene(**SCENE))
    assert (BUILD_WORDS["voluptuous"], 1) in plain.phrases
    assert not any(t in _texts(plain) for _, w, m in SIZE_WORDS for t in (w, m) if t)
