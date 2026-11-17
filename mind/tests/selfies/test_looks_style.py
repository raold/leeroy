"""The owner's hair, wardrobe, makeup and accessories in the looks, the photo prompts and the
outfits the photos pick (owner, 2026-11-17): optional owner-only values, for women and men. A
hair value set is said in place of the looks' own text, which stays stored; unset, the stored
JSON, its hash and every prompt are what they were. Wardrobe and palette bias the outfits, never
the looks words. Everything stays inside the worker's 75-token fit and the age, explicit and
wear screens. Every person here is invented; no model, GPU or real worker."""
import hashlib
import importlib.util
import json
from dataclasses import replace
from itertools import combinations
from pathlib import Path

import pytest

from conftest import make_persona
from leeroy_mind.nightly.enrich import ENRICH_SCHEMA
from leeroy_mind.personas.schema import (GLASSES, HAIR_COLOURS, HAIR_LENGTHS, HAIR_STYLES,
                                         JEWELLERY, MAKEUPS, MEN_HAIR_STYLES, OWNER_LOOKS,
                                         OWNER_STYLE, PALETTE, PIERCINGS, TATTOOS, WARDROBES,
                                         PersonaError, ProfilePhoto, persona_from_dict,
                                         persona_to_dict, validate_persona)
from leeroy_mind.selfies import kind, scene, scene_choices, style_variety, varied, wardrobe
from leeroy_mind.selfies.kind import ERA_RE, EXPLICIT_RE, WEAR_RE
from leeroy_mind.selfies.prompt import (_LENGTHS, ACCESSORY, FIT_MAX, GLASSES_WORDS,
                                        HAIR_COLOUR_WORDS,
                                        HAIR_STYLE_WORDS, JEWELLERY_WORDS, LUST_NEGATIVE,
                                        MAKEUP_WORDS, NEGATIVE, PIERCING_WORDS, STYLE_NEGATIVE,
                                        TATTOO_WORDS, VARIED_NEGATIVE, body_words, build_prompt,
                                        build_varied_prompt, face_details, hair_skin,
                                        hair_skin_short, hair_words, over_fit, style_negative, accessory_phrases,
                                        subject_phrase)
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.style import AGE_RE, LUST, suggestive
from leeroy_mind.selfies.tokens import estimate, joined
from nightly.test_night_enrich import APPEARANCE, LOOKS
from selfies import clip_bpe
from selfies.test_body_shape import LARGEST, LONG_LOOKS, _looks

_FIT = Path(__file__).resolve().parents[3] / "selfie" / "leeroy_selfie" / "fit.py"

PHOTO = ProfilePhoto("snapshot", "three_quarter", "a quiet cafe", "a grey hoodie",
                     "a small smile")
HAIR = dict(owner_hair_colour="strawberry_blonde", owner_hair_length="very_long",
            owner_hair_style="messy_bun")
EVERYTHING = dict(**HAIR, wardrobe=("edgy", "glam", "cosy", "swim"),
                  palette=("black", "cobalt blue", "burgundy"), makeup="glam", glasses="always",
                  jewellery="statement", tattoos="sleeve", piercings="nose")


def woman(**looks):
    p = make_persona(101, "Arkon", player_gender="female", age=34, stance="lust")
    return replace(p, appearance=replace(APPEARANCE, height_cm=164, **looks), photo=PHOTO)


def man(**looks):
    p = make_persona(102, "Belra", age=34)
    return replace(p, appearance=replace(APPEARANCE, height_cm=164, **looks), photo=PHOTO)


def _texts(spec, priority=None):
    return [t for t, pr in spec.phrases if priority is None or pr == priority]


# -- the schema ---------------------------------------------------------------------------------

def test_the_lists_are_the_owners():
    assert HAIR_COLOURS == (
        "black", "dark_brown", "brown", "light_brown", "auburn", "red", "copper",
        "strawberry_blonde", "dark_blonde", "blonde", "platinum", "grey", "silver",
        "pastel_pink", "blue", "purple")
    assert HAIR_LENGTHS == ("buzzed", "short", "chin", "shoulder", "long", "very_long")
    assert HAIR_STYLES == (
        "straight", "wavy", "curly", "coily", "ponytail", "high_bun", "messy_bun", "braid",
        "braids", "bob", "pixie", "bangs", "half_up", "slicked_back", "shaved_sides",
        "crew_cut", "fade", "quiff")
    assert MEN_HAIR_STYLES == ("crew_cut", "fade", "quiff")
    assert WARDROBES == ("casual", "sporty", "smart", "edgy", "boho", "glam", "cosy",
                         "workwear", "vintage", "outdoorsy", "minimalist", "swim")
    assert PALETTE == kind.LUST_COLOURS  # the existing 12 lust colours
    assert MAKEUPS == ("none", "natural", "defined", "glam")
    assert GLASSES == ("never", "always") and JEWELLERY == ("none", "minimal", "statement")
    assert TATTOOS == ("none", "small", "sleeve")
    assert PIERCINGS == ("none", "ears", "nose", "lip")
    assert set(OWNER_STYLE) <= set(OWNER_LOOKS)


def test_unset_leaves_the_stored_json_and_the_looks_hash_as_they_were():
    for p in (woman(), man()):
        d = persona_to_dict(p)
        assert not set(OWNER_LOOKS) & set(d["appearance"])
        assert persona_from_dict(json.loads(json.dumps(d))) == p
        old = dict(LOOKS) | {"height_cm": 164}
        assert d["appearance"] == old
        assert looks_hash(p.appearance) == hashlib.sha256(
            json.dumps(old, sort_keys=True).encode("utf-8")).hexdigest()


def test_set_values_are_stored_beside_the_free_text_which_stays():
    p = woman(**EVERYTHING)
    stored = json.loads(json.dumps(persona_to_dict(p)))["appearance"]
    assert stored["hair_colour"] == LOOKS["hair_colour"] and stored["hair_style"] == LOOKS["hair_style"]
    assert stored["hair_length"] == LOOKS["hair_length"]  # the looks' own text is untouched
    assert stored["owner_hair_colour"] == "strawberry_blonde"
    assert stored["wardrobe"] == ["edgy", "glam", "cosy", "swim"]
    assert stored["palette"] == ["black", "cobalt blue", "burgundy"]
    assert persona_from_dict(json.loads(json.dumps(persona_to_dict(p)))) == p
    assert validate_persona(p) == []
    assert looks_hash(p.appearance) != looks_hash(woman().appearance)
    # a wardrobe alone leaves the hair out of the stored looks
    only = persona_to_dict(woman(wardrobe=("casual",)))["appearance"]
    assert set(only) - set(LOOKS) == {"wardrobe"}


@pytest.mark.parametrize("name,bad", [
    ("owner_hair_colour", "green"), ("owner_hair_colour", "Black"), ("owner_hair_length", "shaved"),
    ("owner_hair_style", "pigtails"), ("owner_hair_style", "bunches"), ("wardrobe", "casual"),
    ("wardrobe", ["tweed"]), ("palette", ["white"]), ("makeup", "heavy"), ("glasses", "keep"),
    ("jewellery", 2), ("tattoos", "face"), ("piercings", "navel")])
def test_a_value_off_its_list_is_refused(name, bad):
    d = persona_to_dict(woman())
    d["appearance"][name] = bad
    with pytest.raises(PersonaError, match=f"appearance.{name}"):
        persona_from_dict(d)


def test_the_limits_and_whose_they_are():
    assert any("appearance.wardrobe" in e for e in validate_persona(
        woman(wardrobe=("casual", "sporty", "smart", "edgy", "boho"))))
    assert any("appearance.palette" in e for e in validate_persona(
        woman(palette=("black", "red", "navy", "gold"))))
    assert validate_persona(woman(wardrobe=("casual", "casual"))) == [
        "appearance.wardrobe: each value once"]
    assert validate_persona(man(makeup="natural")) == [
        "appearance.makeup: women only; leave it empty"]
    assert validate_persona(woman(owner_hair_style="quiff")) == [
        "appearance.owner_hair_style: a man's style; pick another"]
    him = man(owner_hair_colour="grey", owner_hair_length="buzzed", owner_hair_style="fade",
              wardrobe=("smart", "outdoorsy"), palette=("navy",), glasses="always",
              jewellery="minimal", tattoos="sleeve", piercings="ears")
    assert validate_persona(him) == []


def test_no_model_is_asked_for_them():
    assert not set(OWNER_STYLE) & set(ENRICH_SCHEMA["properties"]["appearance"]["properties"])
    assert not set(OWNER_STYLE) & set(ENRICH_SCHEMA["properties"]["appearance"]["required"])


# -- the screens: every new value and every new string ------------------------------------------

def _new_strings():
    values = [v.replace("_", " ") for group in (
        HAIR_COLOURS, HAIR_LENGTHS, HAIR_STYLES, WARDROBES, PALETTE, MAKEUPS, GLASSES, JEWELLERY,
        TATTOOS, PIERCINGS) for v in group]
    said = [*HAIR_COLOUR_WORDS.values(), *HAIR_STYLE_WORDS.values(), *_LENGTHS.values(),
            *MAKEUP_WORDS.values(), *(w for pair in JEWELLERY_WORDS.values() for w in pair),
            GLASSES_WORDS, *TATTOO_WORDS.values(), *PIERCING_WORDS.values(),
            *STYLE_NEGATIVE.values(), *wardrobe.SCENE_MORE.values(), *wardrobe.HINTS.values(),
            *(o for group in wardrobe.LUST_TAGS.values() for o in group)]
    return [t for t in values + said if t]


def test_every_new_value_and_string_passes_the_age_and_wear_screens():
    texts = _new_strings()
    assert len(texts) > 150
    for text in texts:
        assert not AGE_RE.search(text), text
        assert not WEAR_RE.search(text), text
    assert not any(word in text for text in texts for word in ("pigtail", "pig tail", "bunches"))
    swim = set(wardrobe.LUST_TAGS["swim"])  # the vetted lust swimwear, at the water only
    assert swim == set(kind.SWIM_OUTFITS)
    for text in texts:
        if text not in swim and text != "swim":
            assert not EXPLICIT_RE.search(text), text


def test_every_word_the_prompt_can_say_for_them_is_in_its_words():
    assert set(HAIR_COLOUR_WORDS) == set(HAIR_COLOURS) and set(HAIR_STYLE_WORDS) == set(HAIR_STYLES)
    assert set(HAIR_LENGTHS) <= set(_LENGTHS)
    assert set(MAKEUP_WORDS) == set(MAKEUPS) and set(JEWELLERY_WORDS) == set(JEWELLERY)
    assert set(TATTOO_WORDS) == set(TATTOOS) and set(PIERCING_WORDS) == set(PIERCINGS)
    covered = " ".join(t for t, _ in NEGATIVE + LUST_NEGATIVE + VARIED_NEGATIVE)
    for text in [*HAIR_COLOUR_WORDS.values(), *HAIR_STYLE_WORDS.values(), *MAKEUP_WORDS.values(),
                 *(w for pair in JEWELLERY_WORDS.values() for w in pair)]:
        for word in text.replace(",", "").split():
            assert word not in covered.replace(",", "").split(), word  # never a negative's word


def test_every_wardrobe_outfit_is_one_of_the_vetted_lists():
    assert set(wardrobe.LUST_TAGS) == set(WARDROBES) == set(wardrobe.SCENE_TAGS)
    assert set(wardrobe.HINTS) == set(WARDROBES) - set(wardrobe.LUST_ONLY)
    for tag, outfits in wardrobe.LUST_TAGS.items():
        assert outfits and set(outfits) <= set(kind.VARIED_OUTFITS) | set(kind.LUST_OUTFITS), tag
        assert set(outfits) & set(kind.LUST_OUTFITS), tag  # the kind pass has one for every tag
    for tag, keys in wardrobe.SCENE_TAGS.items():
        assert set(keys) <= set(scene_choices.ALL_OUTFITS), tag
    assert not set(wardrobe.SCENE_MORE) & set(scene_choices.OUTFITS)
    assert len(wardrobe.SCENE_MORE) <= len(WARDROBES)  # at most one more for a tag
    assert set(wardrobe.SCENE_MORE) == {k for keys in wardrobe.SCENE_TAGS.values() for k in keys
                                        } - set(scene_choices.OUTFITS)


def test_every_new_scene_outfit_passes_the_scene_screens():
    for key, text in wardrobe.SCENE_MORE.items():
        assert scene._text_codes("outfit", text, ident=scene._NOBODY, names=[]) == [], key
        assert not ERA_RE.search(text) and not EXPLICIT_RE.search(text), key
        composed = scene_choices.compose_choices({
            "photo": True, "kind": "selfie", "framing": "upper_body", "setting": "cafe",
            "activity": "coffee", "outfit": key, "expression": "smile", "when": "day",
            "audience": "anyone", "prop": "mug"})
        assert composed["outfit"] == text
        assert scene.screen_scene(make_persona(102, "Belra", age=34), composed, ident=scene._NOBODY,
                                  member_names=[]) == []


# -- the prompt: hair ---------------------------------------------------------------------------

def test_unset_the_prompt_is_what_the_looks_say():
    p = woman()
    assert hair_words(p) == ("shoulder-length dark brown hair", "tucked behind one ear")
    assert face_details(p) == list(APPEARANCE.face)
    assert accessory_phrases(p) == [] and style_negative(p, NEGATIVE) == ()
    assert build_prompt(p).negative == NEGATIVE


def test_a_set_hair_value_is_said_in_place_of_the_looks_own():
    assert hair_skin(woman(owner_hair_colour="pastel_pink"), None)[0] == (
        "shoulder-length pastel pink hair, tucked behind one ear", 1)
    assert hair_skin(woman(owner_hair_length="very_long"), None)[0] == (
        "very long dark brown hair, tucked behind one ear", 1)
    assert hair_skin(woman(owner_hair_style="bangs"), None)[0] == (
        "shoulder-length dark brown hair, with bangs", 1)
    assert hair_skin(woman(**HAIR), None)[0] == (
        "very long strawberry blonde hair, in a messy bun", 1)
    assert hair_skin(man(owner_hair_length="buzzed", owner_hair_colour="grey",
                         owner_hair_style="fade"), None)[0] == ("buzzed grey hair, fade haircut", 1)
    assert hair_skin(woman(owner_hair_colour="platinum"), None)[0][0].startswith(
        "shoulder-length platinum blonde hair")
    # a shaved head stays one until he sets a length over it
    bald = man(hair_length="shaved")
    assert hair_skin(bald, None)[0] == ("shaved head", 1)
    assert hair_skin(replace(bald, appearance=replace(bald.appearance, owner_hair_colour="red")),
                     None)[0] == ("shaved head", 1)
    # over looks that say a shaved head only his own values count: no "closely shaved" style,
    # no stored colour (review of #358)
    stubble = man(hair_length="shaved", hair_colour="", hair_style="closely shaved",
                  owner_hair_length="long", owner_hair_colour="black")
    assert hair_skin(stubble, None)[0] == ("long black hair", 1)
    styled = replace(stubble, appearance=replace(stubble.appearance, owner_hair_style="ponytail"))
    assert hair_skin(styled, None)[0] == ("long black hair, in a ponytail", 1)


def test_the_profile_prompt_golden_with_hair_makeup_and_accessories():
    p = replace(woman(**EVERYTHING), photo=replace(PHOTO, kind="selfie", framing="upper_body"))
    spec = build_prompt(p)
    assert spec.phrases == (
        ("phone selfie", 0), ("waist-up shot, upper body", 0), ("34-year-old British woman", 0),
        ("very long strawberry blonde hair, in a messy bun", 0), ("light olive skin", 0),
        # what he set, first among the optional phrases; her own glasses detail moves up here
        ("thin glasses", 1), ("tattoo sleeve", 1), ("nose piercing", 1), ("glam makeup", 1),
        ("statement jewellery", 1),
        ("hazel eyes", 2), ("freckles", 2), ("looking at the camera", 2), ("average build", 1),
        ("a grey hoodie", 2), ("a quiet cafe", 2), ("a small smile", 3),
        ("recent smartphone photo, natural light, natural skin texture, unretouched", 1))
    assert spec.negative == NEGATIVE  # nothing set to "never" or "none"
    assert spec.phrases[:3] == build_prompt(replace(woman(), photo=p.photo)).phrases[:3]


def test_the_profile_prompt_golden_for_a_man_who_set_none():
    p = man(owner_hair_colour="silver", owner_hair_length="short", owner_hair_style="crew_cut",
            glasses="never", jewellery="none", tattoos="none", piercings="none",
            face=("round wire glasses", "a short beard", "a small anchor tattoo", "pierced ear"))
    spec = build_prompt(p)
    assert spec.phrases == (
        ("candid photo taken by a friend", 0),
        ("three-quarter shot from mid-thigh up, waist and hips in frame", 0),
        ("34-year-old British man", 0), ("short silver hair, crew cut", 0), ("light olive skin", 0),
        ("hazel eyes", 2), ("a short beard", 2), ("average build", 1), ("a grey hoodie", 2),
        ("a quiet cafe", 2), ("a small smile", 3),
        ("recent smartphone photo, natural light, natural skin texture, unretouched", 1))
    # one word each, a phrase apiece, only as many as the fixed negatives leave room for
    assert spec.negative == NEGATIVE + (("glasses", 3), ("tattoo", 3))
    assert spec.negative[:len(NEGATIVE)] == NEGATIVE  # the fixed negatives keep their places


def test_identity_details_already_there_are_kept():
    face = ("round wire glasses", "a short beard", "a scar on the chin", "a small anchor tattoo")
    p = man(face=face, glasses="always", jewellery="minimal")
    # his own glasses detail is said first among the optional phrases, nothing added or lost
    assert accessory_phrases(p) == [("round wire glasses", ACCESSORY), ("chain necklace", ACCESSORY)]
    assert face_details(p) == list(face[1:])
    assert accessory_phrases(man(face=face[1:], glasses="always")) == [("glasses", ACCESSORY)]
    sleeve = man(face=face, tattoos="sleeve")
    assert face_details(sleeve) == ["round wire glasses", "a short beard", "a scar on the chin"]
    assert accessory_phrases(sleeve) == [("tattoo sleeve", ACCESSORY)]
    assert face_details(man(face=face, piercings="lip")) == list(face)
    assert accessory_phrases(woman(makeup="none", jewellery="none")) == [("no makeup", ACCESSORY)]
    assert accessory_phrases(man(makeup=None, jewellery="statement")) == [
        ("chunky jewellery", ACCESSORY)]
    assert GLASSES_WORDS == "glasses"


def test_only_the_clause_that_names_it_goes_from_a_combined_detail():
    """Review of #358: a scar beside a tattoo and freckles beside a nose ring are kept."""
    face = ("a faded tattoo and a scar on the chin", "freckles and a nose ring",
            "glasses, a dimple", "a pierced eyebrow")
    assert face_details(man(face=face, tattoos="none")) == [
        "a scar on the chin", "freckles and a nose ring", "glasses, a dimple", "a pierced eyebrow"]
    assert face_details(man(face=face, piercings="none")) == [
        "a faded tattoo and a scar on the chin", "freckles", "glasses, a dimple"]
    assert face_details(man(face=face, glasses="never")) == [
        "a faded tattoo and a scar on the chin", "freckles and a nose ring", "a dimple",
        "a pierced eyebrow"]
    small = man(face=face, tattoos="small", piercings="ears")
    assert face_details(small) == ["a scar on the chin", "freckles", "glasses, a dimple"]
    assert accessory_phrases(small) == [("small tattoo", ACCESSORY), ("pierced ears", ACCESSORY)]
    assert face_details(man(face=face)) == list(face)  # unset: as stored


def test_what_he_set_reaches_the_varied_situational_and_tile_prompts():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt, build_tile_prompt
    from selfies.test_selfie_scene import SCENE
    p = woman(build="curvy", makeup="natural", jewellery="minimal", tattoos="none")
    worn = [("natural makeup", ACCESSORY), ("delicate necklace", ACCESSORY)]
    look = next(_looks(views=("front",)))
    spec = build_varied_prompt(p, look)
    assert all(x in spec.phrases for x in worn)
    snap = build_scene_prompt(p, Scene(**SCENE))
    plain = build_scene_prompt(woman(build="curvy"), Scene(**SCENE))
    assert all(x in snap.phrases for x in worn)
    assert snap.negative == plain.negative  # a situational photo's negatives are unchanged
    assert all(x in build_tile_prompt(p).phrases for x in worn)
    # a limb word never reaches a situational photo (scene.limb_detail): none of these has one
    from leeroy_mind.selfies.scene import limb_detail
    assert not any(limb_detail(w) for w in [*TATTOO_WORDS.values(), *PIERCING_WORDS.values(),
                                            *MAKEUP_WORDS.values(), GLASSES_WORDS]
                   + [w for pair in JEWELLERY_WORDS.values() for w in pair])


# -- the negatives: a word apiece, only where there is room -------------------------------------

def test_each_never_or_none_is_its_own_one_word_negative_where_it_fits():
    """Review of #358: one joined phrase was always dropped and took glasses down with it. The
    fixed negatives keep every place; a word is added only where the worker's fit has room for
    it beside them, glasses first, and is otherwise left out here (never sent to be dropped)."""
    off = dict(glasses="never", tattoos="none", piercings="none", jewellery="none")
    assert set(STYLE_NEGATIVE.values()) == {"glasses", "tattoo", "piercing", "jewellery"}
    p = woman(build="curvy", **off)
    assert style_negative(p, NEGATIVE) == (("glasses", 3), ("tattoo", 3))
    assert style_negative(woman(jewellery="none", glasses="never"), NEGATIVE) == (
        ("glasses", 3), ("jewellery", 3))  # jewellery no longer costs him the glasses
    assert style_negative(woman(jewellery="none"), NEGATIVE) == (("jewellery", 3),)
    for base, room in ((NEGATIVE, 2), (LUST_NEGATIVE, 3), (VARIED_NEGATIVE, 0)):
        got = style_negative(p, base)
        assert [w for w, _ in got] == ["glasses", "tattoo", "piercing", "jewellery"][:room]
        dropped = fit(base + got)[1]
        assert not any(w in dropped for w, _ in got), base  # what is sent is kept
        assert fit(base)[1] == dropped  # and it costs the fixed negatives nothing
    # a varied set's fixed negatives fill the window: nothing is added there, said here plainly
    assert style_negative(p, VARIED_NEGATIVE) == ()
    look = next(_looks(views=("front",)))
    assert build_varied_prompt(p, look).negative == VARIED_NEGATIVE
    assert not any("glasses" in t for t, _ in build_varied_prompt(
        replace(p, appearance=replace(p.appearance, face=("thin glasses",))), look).phrases)


# -- the fit ------------------------------------------------------------------------------------

def fit(phrases, count=None):
    """The selfie worker's own fit (selfie/leeroy_selfie/fit.py, loaded from its file: the mind
    never imports the worker) over `phrases`, counted on RealVisXL's tokenizers when their
    files are on this machine, else by the mind's count, which is never below it."""
    spec = importlib.util.spec_from_file_location("leeroy_selfie_fit", _FIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if count is None:
        count = clip_bpe.count if clip_bpe.tokenizers() is not None else estimate
    return mod.fit(phrases, count)


# The worker counts on RealVisXL's tokenizers; the mind's count is only an upper bound for free
# text (an activity, a place), so what the worker keeps is checked where the tokenizer files are
# (as the token table is). Without them the ordering and the reserve are still checked below.
_TOKENIZERS = pytest.mark.skipif(clip_bpe.tokenizers() is None,
                                 reason="RealVisXL tokenizer files are not on this machine")
WORN = dict(glasses="always", tattoos="small")  # the review's heaviest realistic member


def heavy(**more):
    return woman(build="voluptuous", weight_kg=88, bust="large", **WORN, **more)


def _lost(spec, p):
    dropped = fit(spec.phrases)[1]
    return [t for t, _ in accessory_phrases(p) if t in dropped]


@_TOKENIZERS
def test_the_workers_fit_keeps_what_he_set_on_the_heaviest_realistic_member():
    """Review of #358: makeup, jewellery, glasses and tattoos were dropped by the worker's fit
    in almost every prompt. Now they are the first optional phrases and the required set
    shortens (and the kind of photo, or a varied front or side view's framing, makes way) to
    leave them room: a voluptuous woman of 88 kg with a large bust keeps her glasses and her
    tattoo in every profile, in her lust profile, in every varied look from the front or the
    side, and in a situational photo. What cannot fit is pinned below."""
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt
    from selfies.test_selfie_scene import SCENE
    p = heavy()
    assert [t for t, _ in accessory_phrases(p)] == ["thin glasses", "small tattoo"]
    lust_kind = ProfilePhoto("mirror_selfie", "upper_body", "the mirror in her hallway",
                             "a fitted little black dress", "a playful smile")
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            plain = replace(p, photo=replace(PHOTO, kind=kind_, framing=framing))
            assert _lost(build_prompt(plain), plain) == [], (kind_, framing)
            lusty = replace(p, photo=replace(lust_kind, kind=kind_, framing=framing))
            assert _lost(build_prompt(lusty, kind_style=LUST), lusty) == [], (kind_, framing)
    for look in _looks(views=("front", "side")):
        assert _lost(build_varied_prompt(p, look), p) == [], look
    for framing in ("face", "head_and_shoulders", "upper_body"):
        snap = build_scene_prompt(p, Scene(**{**SCENE, "framing": framing}))
        assert _lost(snap, p) == [], framing
    # a three-quarter situation with a long activity and place keeps her glasses, the first
    wide = build_scene_prompt(p, Scene(**{**SCENE, "framing": "three_quarter"}))
    assert "thin glasses" not in _lost(wide, p)


@_TOKENIZERS
def test_everything_set_shows_on_a_typical_member_and_makeup_is_no_longer_always_dropped():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt
    from selfies.test_selfie_scene import SCENE
    every = dict(glasses="always", tattoos="small", piercings="nose", makeup="glam",
                 jewellery="statement")
    p = woman(build="curvy", **every)
    assert len(accessory_phrases(p)) == 5
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "headshot", "snapshot"):
            q = replace(p, photo=replace(PHOTO, kind=kind_, framing=framing))
            assert _lost(build_prompt(q), q) == [], (kind_, framing)
    for look in _looks(views=("front", "side")):
        assert _lost(build_varied_prompt(p, look), p) == [], look
    for framing in ("face", "upper_body"):
        assert _lost(build_scene_prompt(p, Scene(**{**SCENE, "framing": framing})), p) == []
    him = man(glasses="always", tattoos="sleeve", jewellery="minimal", piercings="ears")
    for framing in ("face", "upper_body", "three_quarter"):
        q = replace(him, photo=replace(PHOTO, framing=framing))
        assert _lost(build_prompt(q), q) == []
        assert _lost(build_scene_prompt(him, Scene(**{**SCENE, "framing": framing})), him) == []


@_TOKENIZERS
def test_back_views_may_not_show_them_and_the_panel_says_so():
    """A varied back view keeps its pose, framing and glutes before anything he set her to wear
    (the pose is never yielded: a back view without it renders from the front), so on a full
    figure they may not fit there. The Looks panel says so."""
    p = heavy()
    back = [look for look in _looks(views=("behind", "rear_three_quarter"))]
    lost = [look for look in back if _lost(build_varied_prompt(p, look), p)]
    assert lost
    for look in back:  # and nothing is yielded for them there
        spec = build_varied_prompt(p, look)
        plain = build_varied_prompt(woman(build="voluptuous", weight_kg=88, bust="large"), look)
        assert [x for x in spec.phrases if x[1] == 0 and "shot" in x[0]] == [
            x for x in plain.phrases if x[1] == 0 and "shot" in x[0]]
    model = (Path(__file__).resolve().parents[2] / "leeroy_mind" / "console" / "static" / "js"
             / "looksmodel.mjs").read_text("utf-8")
    assert "may not show in back views" in model


def test_the_reserve_shortens_and_gives_way_only_when_something_is_set():
    """With nothing set the steps are what they were; with something set the required set
    shortens first, then the kind of photo makes way, and her body, hair length and colour,
    skin tone and framing stay required."""
    p = heavy(skin_tone=LONG_LOOKS["skin_tone"], hair_style=LONG_LOOKS["hair_style"])
    bare = woman(build="voluptuous", weight_kg=88, bust="large", skin_tone=LONG_LOOKS["skin_tone"],
                 hair_style=LONG_LOOKS["hair_style"], face=("freckles",))
    photo = replace(PHOTO, kind="snapshot", framing="three_quarter")
    a, b = build_prompt(replace(p, photo=photo)), build_prompt(replace(bare, photo=photo))
    assert joined(_texts(a, 0)) < joined(_texts(b, 0)) <= FIT_MAX
    _required_kept(a, p)
    assert any("three-quarter shot" in t for t in _texts(a, 0))  # the framing never makes way

def _longest_hair() -> dict:
    """The owner's hair that counts most by the mind's own count."""
    def count(c, n, s):
        return estimate(f"{_LENGTHS[n]} {HAIR_COLOUR_WORDS[c]} hair, {HAIR_STYLE_WORDS[s]}")
    c, n, s = max(((c, n, s) for c in HAIR_COLOURS for n in HAIR_LENGTHS for s in HAIR_STYLES
                   if s not in MEN_HAIR_STYLES), key=lambda x: count(*x))
    return dict(owner_hair_colour=c, owner_hair_length=n, owner_hair_style=s)


def _required_kept(spec, p):
    """Who she is, her body, her hair's length and colour and her skin tone are all required."""
    required = _texts(spec, 0)
    assert subject_phrase(p)[0] in required
    long, short = body_words(p), body_words(p, short=True)
    if p.appearance.build != "average" or len(long) > 1:
        for i, text in enumerate(long):
            assert text in required or short[i] in required, text
    hair, style = hair_words(p)
    assert hair in required or f"{hair}, {style}" in required, hair  # length and colour stay
    pairs = dict(hair_skin_short(p, None))
    for text, _ in hair_skin(p, None):
        assert text in required or pairs.get(text) in required, text
    assert not any(t in required for t, _ in accessory_phrases(p))  # never required themselves


@pytest.mark.parametrize("build", ["slim", "average", "curvy", "voluptuous", "heavy"])
@pytest.mark.parametrize("kg", [64, 100, 180])
def test_the_longest_combination_fits_every_profile(build, kg):
    from selfies.test_weight_bust import _longest_lust_phrase
    longest = _longest_lust_phrase()
    rest = {k: v for k, v in EVERYTHING.items() if k not in HAIR}
    for framing in ("face", "head_and_shoulders", "upper_body", "three_quarter"):
        for kind_ in ("selfie", "mirror_selfie", "headshot", "snapshot"):
            p = woman(build=build, weight_kg=kg, **LARGEST, **_longest_hair(), **rest,
                      skin_tone=LONG_LOOKS["skin_tone"])
            p = replace(p, photo=replace(longest, kind=kind_, framing=framing))
            for style in (LUST, "plain"):
                spec = build_prompt(p, kind_style=style)
                assert not over_fit(spec), (framing, kind_, style)
                assert joined(_texts(spec, 0)) <= FIT_MAX
                _required_kept(spec, p)


@pytest.mark.parametrize("build", ["curvy", "voluptuous", "average"])
@pytest.mark.parametrize("kg", [64, 88, 180])
def test_the_longest_hair_never_costs_a_varied_look_more_than_her_own_text_did(build, kg):
    """The enum hair is never longer than long free text was, so a varied look fits wherever it
    did (the one pinned over-fit, a voluptuous woman of BMI 32 or more with every shape largest
    and long skin words, stays as tests/selfies/test_body_shape.py pins it)."""
    rest = {k: v for k, v in EVERYTHING.items() if k not in HAIR}
    styled = woman(build=build, weight_kg=kg, **LARGEST, **_longest_hair(), **rest,
                   skin_tone=LONG_LOOKS["skin_tone"])
    free = woman(build=build, weight_kg=kg, **LARGEST, **LONG_LOOKS)
    assert suggestive(styled)
    for look in _looks():
        spec = build_varied_prompt(styled, look)
        assert joined(_texts(spec, 0)) <= max(FIT_MAX, joined(_texts(build_varied_prompt(free, look), 0)))
        _required_kept(spec, styled)


def test_the_longest_combination_fits_a_situational_photo():
    from leeroy_mind.selfies.scene import Scene, build_scene_prompt
    from selfies.test_selfie_scene import SCENE
    rest = {k: v for k, v in EVERYTHING.items() if k not in HAIR}
    for build in ("slim", "curvy", "voluptuous", "heavy"):
        for kg in (64, 180):
            p = woman(build=build, weight_kg=kg, **LARGEST, **_longest_hair(), **rest)
            for framing in ("face", "upper_body", "three_quarter"):
                spec = build_scene_prompt(p, Scene(**{**SCENE, "framing": framing}))
                assert not over_fit(spec), (build, kg, framing)
                _required_kept(spec, p)


def test_the_style_gives_way_before_the_length_and_colour():
    p = woman(build="voluptuous", weight_kg=180, **LARGEST, **_longest_hair(),
              skin_tone=LONG_LOOKS["skin_tone"])
    hair, style = hair_words(p)
    assert (f"{hair}, {style}", hair) in hair_skin_short(p, None)
    spec = build_prompt(replace(p, photo=replace(PHOTO, kind="snapshot")))
    required = _texts(spec, 0)
    assert not over_fit(spec) and (hair in required or f"{hair}, {style}" in required)


@_TOKENIZERS
def test_the_new_words_count_what_the_real_tokenizers_count():
    for text in [*HAIR_COLOUR_WORDS.values(), *HAIR_STYLE_WORDS.values(), *_LENGTHS.values(),
                 *MAKEUP_WORDS.values(), *(w for pair in JEWELLERY_WORDS.values() for w in pair),
                 GLASSES_WORDS, *TATTOO_WORDS.values(), *PIERCING_WORDS.values(),
                 *STYLE_NEGATIVE.values(), *wardrobe.SCENE_MORE.values()]:
        assert estimate(text) == clip_bpe.count(text), text


# -- wardrobe and palette: a bias on the outfits, never looks words ------------------------------

def test_wardrobe_and_palette_put_no_word_in_a_prompt():
    plain, worn = woman(), woman(wardrobe=("edgy", "glam"), palette=("black", "red"))
    assert build_prompt(worn) == build_prompt(plain)
    look = next(_looks())
    assert build_varied_prompt(replace(worn, appearance=replace(worn.appearance, build="curvy")),
                               look) == build_varied_prompt(
        replace(plain, appearance=replace(plain.appearance, build="curvy")), look)


def test_unset_every_list_and_schema_is_the_same_object():
    for p in (woman(), man(), None):
        assert wardrobe.lust_schema(p, kind.LUST_SCHEMA) is kind.LUST_SCHEMA
        assert wardrobe.lust_schema(p, scene.SCENE_SCHEMA_LUST) is scene.SCENE_SCHEMA_LUST
        assert wardrobe.scene_schema(p, scene.SCENE_SCHEMA, scene_choices.OUTFITS) is scene.SCENE_SCHEMA
        assert wardrobe.outfits(p, kind.LUST_OUTFITS) == tuple(kind.LUST_OUTFITS)
        assert wardrobe.colours(p, kind.LUST_COLOURS) == list(kind.LUST_COLOURS)
        assert wardrobe.ordered(p, [3, 1, 2]) == [3, 1, 2]
        assert wardrobe.ask_hint(p) == "" and wardrobe.digest(p) is None
    assert scene.scene_schema(woman()) is scene.SCENE_SCHEMA_LUST
    assert scene.scene_schema(man()) is scene.SCENE_SCHEMA


def test_the_lust_schema_offers_her_wardrobes_outfits_and_her_palette():
    p = woman(wardrobe=("edgy",), palette=("black", "burgundy"))
    got = wardrobe.lust_schema(p, kind.LUST_SCHEMA)
    props = got["properties"]
    assert props["outfit"]["enum"] == [
        "one-shoulder top and leather trousers", "cut-out dress", "leather jacket over a crop top",
        "silk blouse and a leather miniskirt"]
    assert props["colour"]["enum"] == ["black", "burgundy"]
    assert "a hot tub on a wooden deck" not in props["setting"]["enum"]  # nothing to wear there
    assert got["title"] == kind.LUST_SCHEMA["title"] and got["required"] == kind.LUST_SCHEMA["required"]
    assert kind.LUST_SCHEMA["properties"]["outfit"]["enum"] == list(kind.LUST_OUTFITS)  # untouched
    for outfit in props["outfit"]["enum"]:  # every offered pick still passes the lust screen
        data = {"kind": "selfie", "framing": "upper_body", "setting": "a wine bar",
                "outfit": outfit, "colour": "black", "expression": "a wink"}
        assert kind.screen_lust(p, data) == []
    only = wardrobe.lust_schema(woman(palette=("gold",)), kind.LUST_SCHEMA)["properties"]
    assert only["colour"]["enum"] == ["gold"]  # a palette alone narrows nothing else
    assert {k: v for k, v in only.items() if k != "colour"} == {
        k: v for k, v in kind.LUST_SCHEMA["properties"].items() if k != "colour"}


def test_a_swim_only_wardrobe_offers_only_what_the_pairings_allow():
    """Review of #358: five swimsuits against every setting, framing and kind left 5% of the
    picks valid. Only swimwear left: the water settings, framed upper body, no headshot."""
    props = wardrobe.lust_schema(woman(wardrobe=("swim",)), kind.LUST_SCHEMA)["properties"]
    assert set(props["outfit"]["enum"]) == set(kind.SWIM_OUTFITS)
    assert props["setting"]["enum"] == list(kind.WATER_SETTINGS)
    assert props["framing"]["enum"] == ["upper_body"]
    assert "headshot" not in props["kind"]["enum"] and len(props["kind"]["enum"]) == 3
    p = woman(wardrobe=("swim",))
    for k in props["kind"]["enum"]:  # every pick left passes the lust screen
        for place in props["setting"]["enum"]:
            for outfit in props["outfit"]["enum"]:
                data = {"kind": k, "framing": "upper_body", "setting": place, "outfit": outfit,
                        "colour": "teal", "expression": "a wink"}
                assert kind.screen_lust(p, data) == [], data
    mixed = wardrobe.lust_schema(woman(wardrobe=("swim", "sporty")), kind.LUST_SCHEMA)["properties"]
    assert len(set(mixed["outfit"]["enum"]) & set(kind.SWIM_OUTFITS)) == 1  # one, by her guid
    assert "a hot tub on a wooden deck" not in mixed["setting"]["enum"]
    glam = wardrobe.lust_schema(woman(wardrobe=("swim", "glam")), kind.LUST_SCHEMA)["properties"]
    assert set(kind.SWIM_OUTFITS) <= set(glam["outfit"]["enum"])  # room for every swimsuit


def test_pairing_ok_is_the_lust_screens_pairings():
    p, props = woman(), kind.LUST_SCHEMA["properties"]
    for k in props["kind"]["enum"]:
        for f in props["framing"]["enum"]:
            for place in kind.LUST_SETTINGS:
                for outfit in kind.LUST_OUTFITS:
                    data = {"kind": k, "framing": f, "setting": place, "outfit": outfit,
                            "colour": "teal", "expression": "a wink"}
                    refused = set(kind.screen_lust(p, data)) & set(kind.PAIRINGS)
                    assert wardrobe.pairing_ok(k, f, place, outfit) == (not refused), data


def test_every_wardrobe_keeps_at_least_half_of_its_picks_valid():
    """Every subset of up to four tags (793), with and without a palette: no empty list, and at
    least half of the kind, framing, setting and outfit picks left pass the pairings (the
    unnarrowed lists stand at 76%)."""
    base = kind.LUST_SCHEMA["properties"]
    assert wardrobe.valid_share(base["kind"]["enum"], base["framing"]["enum"],
                                base["setting"]["enum"], base["outfit"]["enum"]) > 0.75
    seen = 0
    for n in range(1, 5):
        for tags in combinations(WARDROBES, n):
            for guid in (101, 102, 105):
                p = replace(woman(wardrobe=tags), guid=guid)
                props = wardrobe.lust_schema(p, kind.LUST_SCHEMA)["properties"]
                lists = [props[k]["enum"] for k in ("kind", "framing", "setting", "outfit")]
                assert all(lists), tags
                assert wardrobe.valid_share(*lists) >= wardrobe.MIN_SHARE, tags
                assert set(props["outfit"]["enum"]) <= {o for t in tags for o in wardrobe.LUST_TAGS[t]}
            snap = wardrobe.lust_schema(woman(wardrobe=tags), scene.SCENE_SCHEMA_LUST)["properties"]
            assert snap["outfit"]["enum"] and snap["setting"]["enum"] and snap["framing"]["enum"]
            seen += 1
    assert seen == 793


def test_swim_counts_only_for_the_lust_style():
    fond = replace(woman(wardrobe=("swim", "casual")),
                   romance=replace(woman().romance, stance="fond"))
    assert not suggestive(fond) and wardrobe.tags(fond) == ("casual",)
    assert wardrobe.tags(woman(wardrobe=("swim", "casual"))) == ("swim", "casual")
    assert wardrobe.tags(man(wardrobe=("swim",))) == ()
    assert "tests/selfies/test_looks_style.py" in wardrobe.__doc__  # where these tests are
    assert "swim" not in wardrobe.ask_hint(fond) and "bikini" not in wardrobe.ask_hint(fond)
    assert not set(wardrobe.outfits(fond, kind.LUST_OUTFITS)) & set(kind.SWIM_OUTFITS)


def test_a_free_text_kind_is_told_what_they_wear_in_fixed_words():
    hint = wardrobe.ask_hint(man(wardrobe=("smart", "outdoorsy"), palette=("navy", "olive")))
    assert hint == (" They tend to wear smart, tailored clothes, practical outdoor clothes: "
                    "dress them in keeping with it. Colours they favour in what they wear: "
                    "navy, olive.")
    assert wardrobe.ask_hint(man(palette=("gold",))) == (
        " Colours they favour in what they wear: gold.")
    for tag in WARDROBES:  # the hint never asks for an era word the kind screen would refuse
        assert not ERA_RE.search(wardrobe.ask_hint(man(wardrobe=(tag,))))


def test_a_varied_set_wears_her_wardrobe_first_and_her_palette():
    base = woman(build="curvy")
    plain = varied.compose(base, "101:2:5")
    worn = replace(base, appearance=replace(base.appearance, wardrobe=("edgy", "sporty"),
                                            palette=("black", "plum")))
    looks = varied.compose(worn, "101:2:5")
    assert [x.category for x in looks] == [x.category for x in plain] == list(kind.CATEGORIES)
    assert [x.view for x in looks] == [x.view for x in plain]  # the same draw, another outfit
    by = {x.category: x for x in looks}
    assert by["going_out"].outfit in wardrobe.LUST_TAGS["edgy"]
    assert by["gym"].outfit in wardrobe.LUST_TAGS["sporty"]
    assert by["swim"].outfit in kind.SWIM_OUTFITS  # one of each category still: a bias
    assert {x.colour for x in looks} <= {"black", "plum"}
    assert all(not varied.screen(worn, x) for x in looks)
    front = varied.recompose_slot(worn, looks, 0, "101:2:n")
    assert front is not None and front.colour in ("black", "plum")
    assert varied.compose(base, "101:2:5") == plain  # no wardrobe: the looks as before


def test_the_variety_pass_dresses_her_from_her_wardrobe_and_palette():
    from nightly.conftest import IDENT
    p = replace(woman(wardrobe=("smart",), palette=("navy",)), photo=None)
    picked, rejected, _, _ = style_variety.assign({101: p}, [101], ident=IDENT)
    assert rejected == [] and "navy" in picked[101].photo.outfit
    smart = {kind.outfit_phrase(o, "navy") for o in wardrobe.LUST_TAGS["smart"]}
    assert picked[101].photo.outfit in smart
    plain, _, _, _ = style_variety.assign({101: replace(woman(), photo=None)}, [101], ident=IDENT)
    assert plain[101].codes["outfit"] < len(style_variety.DATE_OUTFITS)


def test_a_plain_scene_offers_the_outfits_her_wardrobe_names_and_keeps_the_gyms():
    base = list(scene_choices.OUTFITS)
    assert wardrobe.scene_outfits(man(wardrobe=("cosy", "smart")), base) == (
        "warm", "tailored", "exercise")
    assert wardrobe.scene_outfits(man(wardrobe=("sporty",)), base) == ("exercise", "track")
    assert wardrobe.scene_outfits(man(wardrobe=("swim",)), base) == tuple(base)  # nothing named
    got = scene.scene_schema(man(wardrobe=("edgy", "workwear")))
    assert got["properties"]["outfit"]["enum"] == ["dark", "tailored", "exercise"]
    assert got["title"] == scene.SCENE_SCHEMA["title"]
    assert scene.SCENE_SCHEMA["properties"]["outfit"]["enum"] == base  # the base is untouched
    lusty = scene.scene_schema(woman(wardrobe=("glam",), palette=("gold",)))["properties"]
    assert lusty["colour"]["enum"] == ["gold"]
    assert set(lusty["outfit"]["enum"]) == set(wardrobe.LUST_TAGS["glam"])


def test_the_digest_follows_the_stored_wardrobe_and_palette_only():
    a = wardrobe.digest(woman(wardrobe=("casual",)))
    assert a is not None and a == wardrobe.digest(woman(wardrobe=("casual",), makeup="glam", **HAIR))
    assert a != wardrobe.digest(woman(wardrobe=("casual",), palette=("red",)))
    assert a != wardrobe.digest(woman(wardrobe=("casual", "sporty")))
    assert wardrobe.digest(woman(**HAIR, makeup="glam")) is None


def test_the_models_looks_line_says_the_hair_he_set():
    line = kind.looks_line(woman().appearance)
    assert "dark brown shoulder hair worn tucked behind one ear" in line
    styled = kind.looks_line(woman(**HAIR).appearance)
    assert "strawberry blonde very_long hair worn in a messy bun" in styled
    assert styled.replace("strawberry blonde very_long hair worn in a messy bun",
                          "dark brown shoulder hair worn tucked behind one ear") == line
