"""Glutes, waist and hips in the looks and the photo prompts (owner, 2026-11-15: "can you do the
same adjustment for glutes, waist and hips?"): optional women's shape words beside the bust,
owner-set only, said in place of the build's own words or after it, glutes as a phrase of their
own, all inside the worker's 75-token fit. Every person here is invented; no model, GPU or real
worker."""
import hashlib
import json
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.nightly.enrich import ENRICH_SCHEMA, merge
from leeroy_mind.personas.schema import (GLUTES, HIPS, OWNER_LOOKS, WAISTS, PersonaError,
                                         ProfilePhoto, persona_from_dict, persona_to_dict,
                                         validate_persona)
from leeroy_mind.selfies import kind, varied
from leeroy_mind.selfies.kind import EXPLICIT_RE, WEAR_RE
from leeroy_mind.selfies.prompt import (BUILD_WORDS, GLUTES_SIDE, GLUTES_VIEWS, GLUTES_WORDS,
                                        HIPS_WORDS,
                                        LUST_NEGATIVE, NEGATIVE, VARIED_NEGATIVE, WAIST_WORDS,
                                        body_words, build_prompt, build_varied_prompt, build_words,
                                        glutes_words, hair_skin, hair_skin_short, over_fit,
                                        POSE_SHORT, varied_parts)
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.style import AGE_RE, LUST, suggestive
from leeroy_mind.selfies.tokens import PIECES, WORD_TOKENS, estimate, joined
from nightly.test_night_enrich import APPEARANCE, LOOKS
from selfies import clip_bpe

PHOTO = ProfilePhoto("snapshot", "three_quarter", "a quiet cafe", "a grey hoodie",
                     "a small smile")
LARGEST = dict(bust="very_large", glutes="very_large", waist="wide", hips="very_wide")
LONG_LOOKS = dict(hair_colour="honey blonde with highlights",
                  hair_style="loose beach waves over one shoulder", skin_tone="warm golden olive")


def woman(**looks):
    p = make_persona(101, "Arkon", player_gender="female", age=34, stance="lust")
    return replace(p, appearance=replace(APPEARANCE, height_cm=164, **looks), photo=PHOTO)


def man(**looks):
    p = make_persona(102, "Belra", age=34)
    return replace(p, appearance=replace(APPEARANCE, height_cm=164, **looks), photo=PHOTO)


def _texts(spec, priority=None):
    return [t for t, pr in spec.phrases if priority is None or pr == priority]


# -- the schema ---------------------------------------------------------------------------------

def test_looks_without_the_shapes_load_and_save_as_before():
    p = woman()
    d = persona_to_dict(p)
    assert not set(OWNER_LOOKS) & set(d["appearance"])
    assert persona_from_dict(json.loads(json.dumps(d))) == p
    old = dict(LOOKS) | {"height_cm": 164}
    assert looks_hash(p.appearance) == hashlib.sha256(
        json.dumps(old, sort_keys=True).encode("utf-8")).hexdigest()
    shaped = woman(**LARGEST)
    stored = persona_to_dict(shaped)["appearance"]
    assert {k: stored[k] for k in LARGEST} == LARGEST
    assert persona_from_dict(persona_to_dict(shaped)) == shaped and validate_persona(shaped) == []
    assert looks_hash(shaped.appearance) != looks_hash(p.appearance)


@pytest.mark.parametrize("name,bad", [("glutes", "huge"), ("waist", "tiny"), ("hips", "Wide"),
                                      ("glutes", 2), ("hips", "")])
def test_a_shape_off_its_list_is_refused(name, bad):
    d = persona_to_dict(woman())
    d["appearance"][name] = bad
    with pytest.raises(PersonaError, match=f"appearance.{name}"):
        persona_from_dict(d)


def test_the_lists_and_a_man_has_none_and_the_looks_pass_never_sets_them():
    assert GLUTES == ("small", "medium", "large", "very_large")
    assert WAISTS == ("very_narrow", "narrow", "average", "wide")
    assert HIPS == ("narrow", "average", "wide", "very_wide")
    for name, value in (("glutes", "large"), ("waist", "narrow"), ("hips", "wide")):
        assert f"appearance.{name}: women only; leave it empty" in validate_persona(
            man(**{name: value}))
    for name in ("glutes", "waist", "hips"):
        assert name not in ENRICH_SCHEMA["properties"]["appearance"]["properties"]
    data = {"appearance": {**LOOKS, **LARGEST}, "drawn_to": [], "turned_off_by": [],
            "disclosures": []}
    a = merge(woman(), data).appearance
    assert (a.bust, a.glutes, a.waist, a.hips) == (None, None, None, None)


# -- the words ----------------------------------------------------------------------------------

def test_every_shape_word_is_clothed_adult_and_on_the_token_table():
    words = [*GLUTES_WORDS.values(), *WAIST_WORDS.values(), *HIPS_WORDS.values()]
    assert GLUTES_WORDS == {"small": "slim backside", "medium": "rounded glutes",
                            "large": "large round glutes", "very_large": "very large round glutes"}
    assert WAIST_WORDS == {"very_narrow": "very narrow waist", "narrow": "narrow waist",
                           "average": "average waist", "wide": "thick waist"}
    assert HIPS_WORDS == {"narrow": "narrow hips", "average": "average hips",
                          "wide": "wide hips", "very_wide": "very wide hips"}
    for text in words:
        assert not EXPLICIT_RE.search(text) and not WEAR_RE.search(text), text
        assert not AGE_RE.search(text), text
        for piece in PIECES.findall(text.lower()):
            assert not piece.isalpha() or piece in WORD_TOKENS, piece
        assert estimate(text) <= 6
    for negative in (NEGATIVE, LUST_NEGATIVE, VARIED_NEGATIVE):  # unchanged
        assert not any("glutes" in t or "backside" in t for t, _ in negative)


def test_waist_and_hips_take_the_builds_own_words_place_or_follow_it():
    assert build_words(woman(build="curvy", waist="very_narrow", hips="very_wide")) == (
        "curvy hourglass figure, full bust, very wide hips, very narrow waist")
    assert build_words(woman(build="curvy", **LARGEST)) == (  # a wide waist is no hourglass
        "curvy figure, very large full bust, very wide hips, thick waist")
    assert build_words(woman(build="curvy", waist="average")) == (
        "curvy hourglass figure, full bust, wide hips, average waist")
    assert build_words(woman(build="voluptuous", waist="narrow", hips="wide")) == (
        "voluptuous full-figured body, large full bust, wide hips, soft curves, narrow waist")
    assert build_words(woman(build="slim", bust="small", waist="narrow", hips="narrow")) == (
        "slim figure, small bust, narrow waist, narrow hips")
    assert build_words(woman(build="curvy")) == BUILD_WORDS["curvy"]  # nothing set: as before
    # a man's are never said, even ones that got past the schema somehow
    assert build_words(man(build="stocky", waist="narrow", hips="wide")) == BUILD_WORDS["stocky"]
    assert glutes_words(man(glutes="large")) == ""


def test_from_bmi_28_a_thin_builds_parts_stay_as_phrases_of_their_own():
    p = woman(build="average", weight_kg=88, bust="large", waist="wide", hips="very_wide")
    assert body_words(p) == ["plus-size, heavyset, thick arms, full round face",
                             "large full bust", "thick waist", "very wide hips"]
    assert body_words(p, short=True)[1:] == body_words(p)[1:]  # the parts never shorten


# -- where glutes are said ------------------------------------------------------------------------

def _looks(views=None):
    for category, (outfits, settings) in kind.VARIED_LOOKS.items():
        for outfit in outfits:
            for setting in settings:
                for view in kind.VARIED_VIEWS:
                    for framing in kind.VARIED_FRAMINGS:
                        if framing == "three_quarter" and (
                                view not in kind.BACK_VIEWS or category == "swim"):
                            continue
                        if views is None or view in views:
                            yield varied.Look(category, view, framing, setting, outfit,
                                              kind.LUST_COLOURS[0], "an over-the-shoulder glance")


def test_glutes_are_required_from_behind_after_who_she_is_from_the_side_last_from_the_front():
    """Required right after her build from behind, where her face is mostly hidden; from the
    side, where it shows, priority 2 right after her eye colour and the details that tell her
    apart (controller ruling, 2026-11-15); in a front view the last phrase of priority 2 (review
    of #318: optional glutes had taken their room)."""
    assert set(GLUTES_VIEWS) == set(kind.BACK_VIEWS)
    assert GLUTES_SIDE == ("side",)
    p = woman(build="curvy", glutes="large", face=("thin glasses", "a tired half-smile"))
    for look in _looks():
        spec = build_varied_prompt(p, look)
        texts = _texts(spec)
        if look.view in GLUTES_VIEWS:
            assert ("large round glutes", 0) in spec.phrases, look
            assert texts.index("large round glutes") == texts.index(build_words(p)) + 1
        elif look.view in GLUTES_SIDE and look.framing == "upper_body":
            assert "large round glutes" not in texts, look  # waist-up: not in the frame
        elif look.view in GLUTES_SIDE:
            assert ("large round glutes", 2) in spec.phrases, look
            assert texts.index("large round glutes") == texts.index("thin glasses") + 1
            assert texts.index("hazel eyes") < texts.index("thin glasses")
            assert texts.index("large round glutes") < texts.index("a tired half-smile")
        else:
            # the last phrase of priority 2 ("alone", priority 3, may follow at a busy place)
            assert [x for x in spec.phrases if x[1] <= 2][-1] == ("large round glutes", 2), look
            for detail in ("hazel eyes", "thin glasses"):
                assert texts.index(detail) < texts.index("large round glutes")


def test_a_profile_says_glutes_only_where_the_framing_shows_her_figure():
    p = woman(build="curvy", glutes="medium")
    for framing, said in (("three_quarter", 2), ("upper_body", None), ("face", None),
                          ("head_and_shoulders", None)):
        spec = build_prompt(replace(p, photo=replace(PHOTO, framing=framing)))
        if said is None:
            assert "rounded glutes" not in _texts(spec)
        else:
            assert ("rounded glutes", said) in spec.phrases
            texts = _texts(spec)
            assert texts.index("hazel eyes") < texts.index("rounded glutes")  # after her eyes


def test_a_situational_photo_says_glutes_where_its_framing_shows_her_figure():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt
    from selfies.test_selfie_scene import SCENE
    p = woman(build="curvy", glutes="large", hips="very_wide")
    wide = build_scene_prompt(p, Scene(**{**SCENE, "framing": "three_quarter"}))
    assert ("large round glutes", 2) in wide.phrases
    assert _texts(wide).index("hazel eyes") < _texts(wide).index("large round glutes")
    assert (build_words(p), 0) in wide.phrases and "very wide hips" in build_words(p)
    close = build_scene_prompt(p, Scene(**{**SCENE, "framing": "upper_body"}))
    assert "large round glutes" not in _texts(close)


# -- the fit ------------------------------------------------------------------------------------

def _never_cut(spec, p):
    required = _texts(spec, 0)
    long, short = body_words(p), body_words(p, short=True)
    for i, text in enumerate(long):
        assert text in required or short[i] in required, text
    pairs = dict(hair_skin_short(p, None))
    for text, _ in hair_skin(p, None):
        assert text in required or pairs.get(text) in required, text


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "average"])
@pytest.mark.parametrize("kg", [64, 88, 180])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_every_varied_look_keeps_its_pose_with_her_largest_shapes(build, kg, looks):
    """Controller ruling, 2026-11-15, after the GPU: a back view without its pose phrase renders
    from the front, so the pose is never yielded. Her body, hair and skin are never cut; glutes
    are required from behind while they fit, and yield (after the framing) before
    the pose; last of all "from behind, looking over her shoulder" says "from behind". What
    cannot fit even so, pinned here: a voluptuous woman of BMI 32 or more with every shape at
    its largest and long hair and skin words, 2 of 458 looks at 76 tokens. The worker refuses
    those (prompt_too_long) and the slot renders her ordinary photo instead
    (render.render_candidates' fallback). None of the live cast reaches it."""
    p = woman(build=build, weight_kg=kg, **LARGEST, **looks)
    assert suggestive(p)
    over = []
    for look in _looks():
        spec = build_varied_prompt(p, look)
        parts = varied_parts(look, look.phrase)
        required = _texts(spec, 0)
        pose = POSE_SHORT.get(parts["view"], parts["view"])
        assert parts["view"] in required or pose in required, look  # never yielded
        if pose in required and pose != parts["view"]:
            assert "very large round glutes" not in required  # glutes went first
        if over_fit(spec):
            over.append(joined(required))
        _never_cut(spec, p)
        if look.view in GLUTES_VIEWS and "very large round glutes" not in required:
            assert ("very large round glutes", 1) in spec.phrases  # yielded, still offered
            assert parts["framing"] not in required  # the framing gave way first
        if look.view not in GLUTES_VIEWS:
            assert "very large round glutes" not in required, look  # never required elsewhere
    if build == "voluptuous" and kg >= 88 and looks:
        # the same look in each of its framings (the waist-up one since 2026-11-17)
        assert len(over) <= 3 and max(over) <= 76
    else:
        assert over == []


def test_a_back_view_keeps_its_pose_with_every_shape_largest_and_25_kg_more():
    """The GPU case: her "behind" look, edited, lost "from behind, looking over her shoulder" to
    priority 1. With every shape at its largest, 25 kg more and long hair and skin words, every
    back and side look keeps its pose required, whole or (last of all) as "from behind"."""
    p = woman(build="voluptuous", weight_kg=APPEARANCE.weight_kg + 25, **LARGEST, **LONG_LOOKS)
    seen = 0
    for look in _looks(views=GLUTES_VIEWS + GLUTES_SIDE):
        spec = build_varied_prompt(p, look)
        view = varied_parts(look, look.phrase)["view"]
        assert (view, 0) in spec.phrases or (POSE_SHORT.get(view), 0) in spec.phrases, look
        seen += 1
    assert seen > 100
    assert POSE_SHORT == {"from behind, looking over her shoulder": "from behind"}


@pytest.mark.parametrize("over", [dict(glutes="very_large"), LARGEST])
@pytest.mark.parametrize("kg", [64, 100])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_from_the_side_glutes_never_cost_her_eye_colour_or_what_tells_her_apart(over, kg, looks):
    """Controller ruling, 2026-11-15: in a side view her face shows, so her glutes come after
    her eye colour and the details that tell her apart. The worker's own fit, with the mind's
    count, keeps every one of those it keeps with glutes unset."""
    from selfies.test_weight_bust import _worker_fit
    fit = _worker_fit()
    p = woman(build="voluptuous", weight_kg=kg, **over, **looks,
              face=("round wire glasses", "a small nose stud", "a faint scar on her chin",
                    "a dimpled smile", "freckles across her nose"))
    q = replace(p, appearance=replace(p.appearance, glutes=None))
    ident = ("hazel eyes", "round wire glasses", "a small nose stud", "a faint scar on her chin")
    for look in _looks(views=GLUTES_SIDE):
        with_glutes = fit(build_varied_prompt(p, look).phrases, estimate)[1]
        without = fit(build_varied_prompt(q, look).phrases, estimate)[1]
        assert [t for t in ident if t in with_glutes and t not in without] == [], look


def test_from_the_side_glutes_are_still_said_where_there_is_room():
    from selfies.test_weight_bust import _worker_fit
    fit = _worker_fit()
    p = woman(build="average", weight_kg=64, glutes="large")
    kept = [look for look in _looks(views=GLUTES_SIDE)
            if "large round glutes" not in fit(build_varied_prompt(p, look).phrases, estimate)[1]]
    assert len(kept) > 60


@pytest.mark.parametrize("build", ["curvy", "voluptuous"])
@pytest.mark.parametrize("looks", [{}, LONG_LOOKS])
def test_the_longest_lust_profile_fits_with_her_largest_shapes(build, looks):
    from selfies.test_weight_bust import _longest_lust_phrase
    longest = _longest_lust_phrase()
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            for kg in (64, 100, 180):
                p = woman(build=build, weight_kg=kg, **LARGEST, **looks)
                spec = build_prompt(replace(p, photo=replace(longest, kind=kind_,
                                                             framing=framing)),
                                    kind_style=LUST)
                assert not over_fit(spec), (framing, kind_, kg)
                _never_cut(spec, p)
                assert joined(_texts(spec, 0)) <= 75


_TOKENIZERS = pytest.mark.skipif(clip_bpe.tokenizers() is None,
                                 reason="RealVisXL tokenizer files are not on this machine")


@_TOKENIZERS
def test_the_shape_words_count_what_the_real_tokenizers_count():
    for text in [*GLUTES_WORDS.values(), *WAIST_WORDS.values(), *HIPS_WORDS.values()]:
        assert estimate(text) == clip_bpe.count(text), text
    assert [clip_bpe.count(w) for w in GLUTES_WORDS.values()] == [3, 3, 4, 5]
