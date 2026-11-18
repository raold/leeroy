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
from leeroy_mind.selfies.safe import selfie_framing
from leeroy_mind.selfies.prompt import (_FRAMINGS, _KINDS, BUILD_WORDS, BUST_WORDS, FIT_MAX,
                                        FULL_BUILDS, LUST_NEGATIVE, NEGATIVE, PLAIN, SHORT,
                                        SIZE_WORDS, hair_skin_short, over_fit,
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


def test_the_size_keeps_two_terms_and_the_build_and_bust_never_shorten():
    """Controller ruling, 2026-11-15, measured on the GPU: "plus-size" alone and a short
    "voluptuous figure" both rendered her slim. The fit's short body is the size's first two
    terms; the build and a set bust stay whole."""
    p = woman(build="voluptuous", weight_kg=100, bust="very_large")
    assert body_words(p) == [FULL_W, build_words(p)]
    assert body_words(p, short=True) == ["plus-size, heavyset", build_words(p)]
    assert body_words(woman(build="curvy", weight_kg=80), short=True) == [
        CHUBBY, BUILD_WORDS["curvy"]]  # two terms already: nothing to shorten
    assert body_words(woman(build="slim", weight_kg=100, bust="small"), short=True) == [
        "plus-size, heavyset", "small bust"]
    assert body_words(man(build="stocky", weight_kg=100), short=True) == [
        "very overweight, heavyset", BUILD_WORDS["stocky"]]
    assert size_words(woman(build="curvy", weight_kg=70), short=True) == "slightly chubby"


def test_hair_keeps_its_length_and_colour_and_skin_its_tone_word():
    p = woman(**LONG_LOOKS)
    assert hair_skin_short(p, None) == [
        ("shoulder-length honey blonde with highlights hair, loose beach waves over one shoulder",
         "shoulder-length honey blonde with highlights hair"),
        ("warm golden olive skin", "olive skin")]
    plain = woman(hair_style="", skin_tone="fair")
    assert hair_skin_short(plain, None) == []  # nothing shorter to say
    assert hair_skin_short(woman(hair_length="shaved"), None) == [("light olive skin", "olive skin")]


@pytest.mark.parametrize("tone,said", [
    ("fair with faint freckles", "fair"), ("pale, with rosy cheeks", "pale"),
    ("light olive with a few freckles", "light olive"), ("warm brown", "brown"),
    ("Pale with flush cheeks", "Pale"), ("fair, tired eyes", "fair"),
    ("Pale, with a faint dusting of", "Pale"), ("Fair With Sun Damage", "Fair"),
    ("sun-warmed tan", "tan")])
def test_the_skin_keeps_its_tone_not_its_last_word(tone, said):
    """Review of #316, round 3: in the looks the tone comes first ("fair with freckles"), so the
    short form is the first clause, before "," or " with "; only with no separator the last
    word. The last word gave "freckles skin", "eyes skin" and "of skin"."""
    from leeroy_mind.selfies.prompt import skin_tone_word
    assert skin_tone_word(tone) == said
    assert hair_skin_short(woman(hair_style="", skin_tone=tone), None) == [
        (f"{tone} skin", f"{said} skin")]


def test_every_word_is_clothed_adult_and_on_the_token_table():
    from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS
    sizes = [w for _, a, b in SIZE_WORDS for w in (a, b)]
    for text in sizes + list(BUST_WORDS.values()) + list(SHORT.values()):
        assert not EXPLICIT_RE.search(text) and not WEAR_RE.search(text), text
        assert not AGE_RE.search(text) and "cleavage" not in text and "petite" not in text
        for piece in PIECES.findall(text.lower()):
            assert not piece.isalpha() or piece in WORD_TOKENS, piece
    assert not any("bust" in w or "belly" in w for w in sizes)  # men's words have no bust words
    assert max(estimate(w) for w in sizes) == 14


def test_the_negatives_are_unchanged():
    p = woman(build="voluptuous", weight_kg=100, bust="very_large")
    from leeroy_mind.selfies.prompt import BODY_NEGATIVE, BODY_NEGATIVE_LUST
    from leeroy_mind.selfies.prompt import HEAD_NEGATIVE
    close = replace(p, photo=replace(p.photo, framing="head_and_shoulders"))
    assert build_prompt(close).negative == HEAD_NEGATIVE and set(NEGATIVE) < set(HEAD_NEGATIVE)
    # a waist-up or wider photo takes the body negative (safe.py), its covered line unchanged
    assert build_prompt(p).negative == BODY_NEGATIVE and NEGATIVE[1] in BODY_NEGATIVE
    lust = build_prompt(replace(p, photo=_lust_kind()), kind_style=LUST).negative
    assert lust == BODY_NEGATIVE_LUST and LUST_NEGATIVE[1] in lust
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
    with her bust), her hair and her skin, in that order, for plain and lust kinds alike; her
    build and bust stay whole, her size keeps at least two terms."""
    p = woman(build="voluptuous", weight_kg=88, bust="very_large")
    p = replace(p, identity=replace(p.identity, age=36, region="US"))
    for photo, style in ((replace(PHOTO, kind=kind_), PLAIN),
                         (replace(_lust_kind(), kind=kind_), LUST)):
        spec = build_prompt(replace(p, photo=photo), kind_style=style)
        texts = _texts(spec)
        at = texts.index("36-year-old American woman")
        assert texts[at + 1] in (PLUS_W, "plus-size, heavyset")
        assert texts[at + 2:at + 5] == [build_words(p), HAIR, SKIN]
        for text in texts[at + 1:at + 5]:
            assert (text, 0) in spec.phrases


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


def _never_cut(spec, p):
    """What round 3's ruling never drops from the required set: her build and a set bust whole,
    at least two terms of her size, her hair's length and colour, her skin tone and who she is."""
    required = _texts(spec, 0)
    long, short = body_words(p), body_words(p, short=True)
    if body_phrases(p)[0]:
        for i, text in enumerate(long):
            assert text in required or short[i] in required, text
        if p.appearance.build in FULL_BUILDS:
            assert build_words(p) in required
    pairs = dict(hair_skin_short(p, None))
    for text, _ in hair_skin(p, None):
        assert text in required or pairs.get(text) in required, text
    assert any("-year-old" in t for t in required)


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "average", "heavy"])
@pytest.mark.parametrize("bust", [None, "very_large"])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_the_longest_lust_prompt_fits_and_never_cuts_her_body_hair_or_skin(build, bust, looks):
    """Round 3 (controller, 2026-11-15): the required set fits the worker's 75 by the mind's own
    count, never below the real one, so the worker never refuses it, while her body, her hair's
    length and colour and her skin tone stay: the kind and framing shorten first, then her
    hair's style and her skin's modifiers, then her size to two terms, then the kind and the
    framing yield, keeping their places."""
    fit = _worker_fit()
    longest = _longest_lust_phrase()
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            ph = replace(longest, kind=kind_, framing=framing)
            for kg in (49, 64, 80, 88, 100, 180):
                p = woman(build=build, bust=bust, weight_kg=kg, **looks)
                spec = build_prompt(replace(p, photo=ph), kind_style=LUST)
                assert not over_fit(spec), (framing, kind_, kg)
                _never_cut(spec, p)
                assert f"wearing {ph.outfit}, {ph.setting}" in _texts(spec, 0)
                # a lust kind's mirror selfie renders as a selfie (safe.kind_of), and a lust
                # selfie waist-up (safe.selfie_framing)
                shown = "selfie" if kind_ == "mirror_selfie" else kind_
                for text in (_KINDS[shown], _FRAMINGS[selfie_framing(shown, framing)]):
                    forms = {text, SHORT.get(text, text)}
                    assert forms & set(_texts(spec, 0)) or forms & set(_texts(spec, 1))
                kept, dropped = fit(spec.phrases, estimate, 75)
                assert not set(_texts(spec, 0)) & set(dropped)


def test_the_steps_run_in_the_rulings_order():
    """The kind and framing shorten first, then her hair's style, then her skin's modifiers,
    then her size's last terms, and only then do the kind and framing yield: a later step is
    taken only once every earlier one has been."""
    ph0 = replace(_longest_lust_phrase(), kind="snapshot", framing="three_quarter")
    seen = set()
    for kg in (64, 80, 88, 100, 180):
        for bust in (None, "very_large"):
            for looks in ({}, LONG_LOOKS):
                p = woman(build="voluptuous", weight_kg=kg, bust=bust, **looks)
                required = _texts(build_prompt(replace(p, photo=ph0), kind_style=LUST), 0)
                pairs = hair_skin_short(p, None)
                size, two = size_words(p), size_words(p, short=True)
                steps = [_KINDS["snapshot"] not in required,  # (taken) per step that applies
                         _FRAMINGS["three_quarter"] not in required,
                         pairs[0][0] not in required, pairs[1][0] not in required]
                steps += [size not in required] if size != two else []
                steps += ["candid photo" not in required]
                taken = [i for i, x in enumerate(steps) if x]
                assert taken == list(range(len(taken))), (kg, bust, bool(looks), steps)
                seen.add(len(taken) == len(steps) or len(taken))
    # with her standing pose required (safe.POSES) the longest phrase takes every step
    assert True in seen
    heavy = woman(build="voluptuous", weight_kg=100, bust="very_large", **LONG_LOOKS)
    texts = _texts(build_prompt(replace(heavy, photo=ph0), kind_style=LUST), 0)
    assert "shoulder-length honey blonde with highlights hair" in texts and "olive skin" in texts
    assert build_words(heavy) in texts and "plus-size, heavyset" in texts
    from leeroy_mind.selfies.safe import POSE_SHORT as poses
    assert set(poses.values()) == {"standing", "walking", "walking away"}
    assert SHORT == {**poses,  # a body shot's pose shortens before her hair's style would go
                     "selfie, close arm's-length shot": "selfie",
                     "candid photo taken by a friend": "candid photo",
                     "professional headshot": "headshot",
                     "waist-up shot, upper body": "waist-up shot",
                     _FRAMINGS["three_quarter"]: "three-quarter shot, waist and hips in frame"}


# Two prompts measured on the GPU (controller, 2026-11-15), as invented members of the same shape:
# a voluptuous woman with no edit whose short "voluptuous figure" rendered slim, and one 28 kg
# heavier with a very large bust whose "plus-size" alone rendered slim.
PINNED = {"no_edit": (dict(build="voluptuous", height_cm=165, weight_kg=62, hair_colour="ash blonde",
                            hair_style="swept into a loose bun",
                            skin_tone="fair with rosy cool undertones"),
                       29, "UK", ("snapshot", "three_quarter", "fitted little dress", "black",
                                  "a lakeside dock")),
          "heavier": (dict(build="voluptuous", height_cm=168, weight_kg=96, bust="very_large",
                           hair_colour="dark auburn", hair_style="soft side part",
                           skin_tone="freckled"),
                      36, "US", ("selfie", "upper_body", "fitted little dress", "cobalt blue",
                                 "the deck of a sailing boat"))}


@pytest.mark.parametrize("case", sorted(PINNED))
def test_the_two_gpu_measured_prompts_keep_her_whole_figure_and_her_hair(case):
    looks, age, region, (kind_, framing, outfit, colour, setting) = PINNED[case]
    p = woman(**{k: v for k, v in looks.items() if k != "height_cm"})
    p = replace(p, appearance=replace(p.appearance, height_cm=looks["height_cm"]),
                identity=replace(p.identity, age=age, region=region))
    ph = kind.compose_lust({"kind": kind_, "framing": framing, "outfit": outfit, "colour": colour,
                            "setting": setting, "expression": "a wink"})
    spec = build_prompt(replace(p, photo=ph), kind_style=LUST)
    required = _texts(spec, 0)
    assert build_words(p) in required  # the full figure words, never "voluptuous figure"
    if case == "no_edit":
        assert build_words(p) == BUILD_WORDS["voluptuous"] and not size_words(p)
    else:
        assert PLUS_W in required  # her whole size, not "plus-size" alone
        assert "very large full bust" in build_words(p)
    hair = next(t for t in required if t.endswith(" hair") or " hair, " in t)
    assert hair.startswith(f"shoulder-length {looks['hair_colour']} hair")
    tone = looks["skin_tone"].split()[-1]
    assert any(t.endswith(f"{tone} skin") for t in required)
    assert not over_fit(spec)


def test_an_ordinary_plain_prompt_always_requires_the_size_and_keeps_its_kind():
    for build in ("curvy", "voluptuous", "average", "stocky"):
        for kg in (49, 80, 100, 180):
            p = woman(build=build, weight_kg=kg, bust="very_large")
            for framing in ("face", "three_quarter"):
                spec = build_prompt(replace(p, photo=replace(PHOTO, framing=framing)))
                if size_words(p):  # a full build below BMI 25 has none; two terms at least
                    said = {(size_words(p), 0), (size_words(p, short=True), 0)}
                    assert said & set(spec.phrases)
                _never_cut(spec, p)
                assert joined(_texts(spec, 0)) <= FIT_MAX
                kind_said = {(_KINDS["snapshot"], 0), ("candid photo", 0)}
                assert kind_said & set(spec.phrases)  # a plain kind has room for its kind


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
        assert not over_fit(spec), look
        _never_cut(spec, p)
        after = _after_subject(spec)
        assert after[0] in (long[0], short[0]) and after[1] == build_words(p)
        parts = varied_parts(look, look.phrase)
        assert parts["wearing"] in _texts(spec, 0)
        assert parts["view"] in _texts(spec, 0)  # the pose never yields (2026-11-15)


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
    for text in texts + list(SHORT.values()):
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
    for text in (PLUS_W, build_words(p), SKIN):
        assert (text, 0) in spec.phrases
    # what she wears is required too (safe.complete), so her hair may drop its style
    assert any(t == HAIR or HAIR.startswith(t + ",") for t, pr in spec.phrases if pr == 0)
    assert any(t.startswith("wearing ") and pr == 0 for t, pr in spec.phrases)
    plain = build_scene_prompt(woman(build="voluptuous"), Scene(**SCENE))
    assert _after_subject(plain)[0] == BUILD_WORDS["voluptuous"]
    assert not any(t in _texts(plain) for _, w, m in SIZE_WORDS for t in (w, m) if t)
    avg = build_scene_prompt(woman(), Scene(**SCENE))
    assert (BUILD_WORDS["average"], 1) in avg.phrases  # a plain average build stays optional
    heavy = woman(build="voluptuous", weight_kg=180, bust="very_large", **LONG_LOOKS)
    hot = build_scene_prompt(heavy, scene_from(LUST_SCENE))
    assert not over_fit(hot)
    _never_cut(hot, heavy)


def test_her_eye_colour_and_what_tells_her_apart_come_first_among_her_details():
    """Review of #316, round 3: with her hair, skin and body required, glasses and stubble lost
    their place in the fit to expressions and posture. Her details now go eye colour, then
    glasses, facial hair, scars, tattoos and piercings, then the rest, before the camera line."""
    from leeroy_mind.selfies.prompt import identity_first
    p = woman(face=("a tired half-smile", "slightly hunched", "day's stubble",
                    "thin black-framed glasses"))
    details = identity_first(p, [("hazel eyes", 2), ("a tired half-smile", 2),
                                 ("slightly hunched", 2), ("day's stubble", 2),
                                 ("thin black-framed glasses", 2)])
    assert [t for t, _ in details] == ["hazel eyes", "day's stubble", "thin black-framed glasses",
                                       "a tired half-smile", "slightly hunched"]
    spec = build_prompt(replace(p, photo=replace(PHOTO, kind="selfie")))
    texts = _texts(spec)
    at = texts.index("hazel eyes")
    assert texts[at:at + 5] == [t for t, _ in details]
    assert texts[at + 5] == "looking at the camera"
