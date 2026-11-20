"""Compositions the photo audit showed render badly, designed out (selfies/safe.py): clothed by
construction, no phone, bicycle or gripped prop, the body negative, how much body shows as a
setting, and `selfies audit`; and what the owner put back (2026-11-19): the mirror selfie and the
three settings on the lust lists, no pose phrase of the audit's, the quality words in the
negatives at their earlier priorities. Every person here is invented."""
import json
import os
import re
from dataclasses import replace

import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.selfies import (audit, kind, safe, scene_choices, varied, wardrobe,
                                 words_scene)
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.prompt import (BODY_NEGATIVE, BODY_NEGATIVE_LUST, BODY_NEGATIVE_MIRROR,
                                        BODY_NEGATIVE_LUST_MIRROR, FIT_MAX, HEAD_NEGATIVE,
                                        HEAD_NEGATIVE_LUST, HEAD_NEGATIVE_LUST_MIRROR,
                                        HEAD_NEGATIVE_MIRROR, LUST_NEGATIVE, NEGATIVE,
                                        _COVERED, _COVERED_LUST, body_negative,
                                        VARIED_NEGATIVE, accessory_phrases, negative_for, body_words, build_prompt,
                                        build_varied_prompt, lust_phrase)
from leeroy_mind.selfies.scene import (SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, SNAP_NEGATIVE_MIRROR,
                                       SNAP_NEGATIVE_LUST_MIRROR, TILE_NEGATIVE, Scene,
                                       _NOBODY, _text_codes, build_scene_prompt,
                                       build_tile_prompt)
from leeroy_mind.selfies.store import pick as pick_photo
from leeroy_mind.selfies.style import AGE_RE, LUST
from nightly.test_night_enrich import APPEARANCE
from selfies import clip_bpe
from selfies.fakes import planned_snap, ready, ready_snap, renderable_cast, sent_snap
from leeroy_mind.selfies.tokens import estimate
from selfies.test_selfie_prompt import _worker_fit, man, woman
from selfies.test_selfie_scene import lusty

_TOKENIZERS = pytest.mark.skipif(
    clip_bpe.tokenizers() is None,
    reason="RealVisXL tokenizer files are not on this machine (set LEEROY_SELFIE_DATA)")
COLOUR = "cobalt blue"  # the longest colour
# the audit's pose phrases, taken out again (owner, 2026-11-19): no prompt may carry one
AUDIT_POSE = re.compile(r"\b(?:arms at (?:her|his) sides|hands behind (?:her|his) back|"
                        r"hands clasped in front|arms down, hands out of frame)\b|"
                        r"^(?:standing|walking|walking away)$")
RESTORED = ("the mirror in her hallway", "the front seat of her car", "a concert crowd")


def _posed(spec) -> list[str]:
    return [t for t, _ in spec.phrases if AUDIT_POSE.search(t)]


def _snap(store, snap_id: int) -> tuple:
    return tuple(store.conn.execute("SELECT status, ended FROM selfie_snap WHERE id=?",
                                    (snap_id,)).fetchone())


def _text(spec, priority=None) -> str:
    return ", ".join(t for t, pr in spec.phrases if priority is None or pr == priority)


def _terms(negative, priority=None) -> set[str]:
    return {w.strip() for t, pr in negative if priority is None or pr == priority
            for w in t.split(",")}


# -- clothed by construction ----------------------------------------------------------------------

def _list_outfits():
    """Every outfit phrase a fixed list can put in a prompt, with who may wear it."""
    out = []
    for choice in {**kind.LUST_OUTFITS, **kind.VARIED_OUTFITS}:
        out.append((kind.outfit_phrase(choice, COLOUR, kind.VARIED_OUTFITS
                                       if choice in kind.VARIED_OUTFITS else kind.LUST_OUTFITS),
                    "female"))
    for phrase in (*scene_choices.ALL_OUTFITS.values(), *words_scene.PLAIN_WEAR.values(),
                   *(words for _, words in words_scene.UNIFORMS), words_scene.OFFICE_WEAR):
        out += [(phrase, "female"), (phrase, "male")]
    out += [(f"{safe.DEFAULT_TOP[g]} and {safe.DEFAULT_BOTTOM}", g) for g in ("female", "male")]
    return out


@pytest.mark.parametrize("phrase,gender", _list_outfits())
def test_every_outfit_of_every_list_names_a_top_and_a_bottom_or_a_one_piece_with_its_length(
        phrase, gender):
    assert safe.is_complete(phrase, gender=gender), phrase
    # and the completion adds nothing to it
    assert safe.complete(phrase, gender=gender, framing="three_quarter") == phrase


def test_the_lists_are_checked_whole():
    phrases = [p for p, _ in _list_outfits()]
    assert len(kind.LUST_OUTFITS) == 28 and len(kind.VARIED_OUTFITS) == 32
    assert len(phrases) > 50 and all("{colour}" not in p for p in phrases)


@pytest.mark.parametrize("phrase", [
    "a red crop top", "a leather jacket", "a fitted little dress", "a red party dress",
    "a blazer", "jeans", ""])
def test_an_incomplete_outfit_is_not_complete(phrase):
    assert not safe.is_complete(phrase)


def test_a_man_always_wears_a_shirt():
    assert not safe.is_complete("a rain jacket and jeans", gender="male")
    assert safe.complete("a rain jacket and jeans", gender="male", framing="upper_body") == (
        "a plain t-shirt, a rain jacket and jeans")
    assert safe.complete("shorts", gender="male", framing="upper_body") == (
        "a plain t-shirt and shorts")  # the top first, never "shorts over a t-shirt"
    assert safe.complete("", gender="male", framing="upper_body") == (
        "a plain t-shirt and trousers")
    assert safe.complete("grey hoodie", gender="male", framing="upper_body") == (
        "grey hoodie and trousers")


def test_a_headshot_names_a_top_with_a_neckline():
    assert safe.complete("a silver necklace", gender="female", framing="head_and_shoulders") == (
        "a plain top and a silver necklace")
    assert safe.complete("black graphic t-shirt", gender="male", framing="face") == (
        "black graphic t-shirt")
    assert safe.complete("", gender="male", framing="head_and_shoulders") == "a plain t-shirt"
    assert safe.is_complete("a plain top", head=True)
    assert not safe.is_complete("a silver necklace", head=True)


def test_an_overlong_outfit_gives_way_to_the_default_garments():
    long = " ".join(["soft"] * (safe.WEAR_MAX_WORDS + 1))
    assert safe.complete(long, gender="female", framing="upper_body") == (
        "a plain top and trousers")


def test_a_plain_profile_prompt_requires_what_they_wear():
    spec = build_prompt(man(photo={"kind": "snapshot", "framing": "upper_body",
                                   "setting": "a quiet cafe", "outfit": "grey hoodie",
                                   "expression": "a small smile"}))
    assert ("wearing grey hoodie and trousers", 0) in spec.phrases
    assert spec.negative == BODY_NEGATIVE
    head = build_prompt(man(photo={"kind": "selfie", "framing": "head_and_shoulders",
                                   "setting": "a quiet cafe", "outfit": "a wool scarf",
                                   "expression": "a small smile"}))
    assert ("wearing a plain t-shirt and a wool scarf", 0) in head.phrases
    assert head.negative == HEAD_NEGATIVE and set(NEGATIVE) < set(HEAD_NEGATIVE)
    assert {"shirtless", "mirror", "phone"} <= _terms(HEAD_NEGATIVE, 0)


def test_an_outfit_that_holds_a_name_gives_way_to_the_default_garments():
    spec = build_prompt(man(photo={"kind": "snapshot", "framing": "upper_body",
                                   "setting": "a quiet cafe", "outfit": "Arkon's guild hoodie",
                                   "expression": "a small smile"}))
    assert ("wearing a plain t-shirt and trousers", 0) in spec.phrases
    assert "Arkon" not in _text(spec)


def test_a_long_outfit_never_makes_the_worker_refuse_the_photo():
    from leeroy_mind.selfies.prompt import over_fit
    long_looks = replace(APPEARANCE, build="voluptuous", height_cm=160, weight_kg=118,
                         hair_colour="honey blonde with caramel highlights",
                         hair_style="long glossy waves over one shoulder",
                         skin_tone="warm golden brown, freckled")
    outfit = "an oversized hand-knitted mustard cardigan over a striped rollneck and corduroys"
    photo = {"kind": "snapshot", "framing": "three_quarter", "setting": "a quiet cafe",
             "outfit": outfit, "expression": "a small smile"}
    for p in (woman(appearance=long_looks, photo=photo), man(appearance=long_looks, photo=photo)):
        spec = build_prompt(p)
        assert not over_fit(spec)
        worn = [(t, pr) for t, pr in spec.phrases if t.startswith("wearing ")]
        assert len(worn) == 1 and "trousers" in worn[0][0]  # clothed, whichever phrase fitted
        for text in body_words(p, short=True):  # her body is never traded for it
            assert any(t.startswith(text) and pr == 0 for t, pr in spec.phrases)
        s = Scene("snapshot", "three_quarter", "at a quiet cafe table",
                  "a cup of tea on the table", outfit, "a relaxed smile", "day", "anyone", "x",
                  recipe=scene_choices.RECIPE)
        snap = build_scene_prompt(p, s)
        assert not over_fit(snap) and any(t.startswith("wearing ") for t, _ in snap.phrases)
    short = build_prompt(woman(photo=photo))  # ordinary looks: her own outfit stays
    assert (f"wearing {outfit} and trousers", 0) in short.phrases


def test_a_situational_photo_and_a_tile_require_what_they_wear():
    s = Scene("snapshot", "upper_body", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["outdoors"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    spec = build_scene_prompt(man(), s)
    assert ("wearing a jacket over a t-shirt and trousers", 0) in spec.phrases
    tile = build_tile_prompt(man(appearance=replace(APPEARANCE, at_the_pc="a dressing gown")))
    assert ("wearing a plain t-shirt", 1) in tile.phrases  # the screens refuse the gown
    tile = build_tile_prompt(man(appearance=replace(APPEARANCE, at_the_pc="a band t-shirt")))
    assert ("wearing a band t-shirt", 1) in tile.phrases
    first_optional = next(t for t, pr in tile.phrases if pr > 0)
    assert first_optional == "wearing a band t-shirt"  # the first phrase the fit keeps


# -- every new string meets the screens -----------------------------------------------------------

def _new_strings():
    out = [*safe.KIND_WORDS.values(), *safe.SETTING_WORDS.values(), *safe.NEW_SETTINGS,
           *safe.DEFAULT_TOP.values(), safe.DEFAULT_BOTTOM, safe.ALONE,
           *(phrase for phrase, _ in scene_choices.ACTIVITIES.values()),
           *filter(None, scene_choices.PROPS.values()), *scene_choices.OUTFITS.values(),
           *(words for _, words in words_scene.UNIFORMS), words_scene.OFFICE_WEAR,
           *words_scene.FRAMING_WORDS.values()]
    out += [t.format(colour=COLOUR) for c, t in safe.OUTFIT_WORDS.items()
            if c not in kind.SWIM_OUTFITS]
    return sorted(set(out))


@pytest.mark.parametrize("text", _new_strings())
def test_every_new_string_passes_the_age_explicit_and_wear_screens(text):
    assert not AGE_RE.search(text), text
    assert not kind.EXPLICIT_RE.search(text), text
    assert not kind.WEAR_RE.search(text), text


@pytest.mark.parametrize("field,text", [
    *(("activity", phrase) for phrase, _ in scene_choices.ACTIVITIES.values()),
    *(("activity", phrase) for phrase in scene_choices.PROPS.values() if phrase),
    *(("outfit", phrase) for phrase in scene_choices.OUTFITS.values()),
    *(("setting", phrase) for phrase in (*safe.SETTING_WORDS.values(), *safe.NEW_SETTINGS)),
    ("activity", safe.ALONE),
    *(("outfit", top) for top in safe.DEFAULT_TOP.values()), ("outfit", safe.DEFAULT_BOTTOM),
    *(("outfit", text) for text in wardrobe.SCENE_MORE.values())])
def test_every_composed_scene_phrase_meets_the_scene_screens(field, text):
    assert _text_codes(field, text, ident=_NOBODY, names=[]) == []


def test_the_new_settings_are_offered_beside_the_three_put_back():
    """The owner put the hallway mirror, the car's front seat and the concert crowd back on the
    lust lists (2026-11-19) and kept the audit's three plain places. A situational photo takes
    the car seat again; the hallway mirror and the crowd it never offered (before the audit
    too: scene_choices.LUST_PLACES leaves out crowd, bathing and mirror places)."""
    assert set(safe.NEW_SETTINGS) <= set(kind.LUST_SETTINGS)
    assert set(RESTORED) <= set(kind.LUST_SETTINGS)
    assert len(kind.LUST_SETTINGS) == len(set(kind.LUST_SETTINGS)) == 24
    assert set(RESTORED) <= set(kind.LUST_SCHEMA["properties"]["setting"]["enum"])
    assert "the front seat of her car" in scene_choices.SCENE_LUST_SETTINGS
    assert not {"the mirror in her hallway", "a concert crowd"} & set(
        scene_choices.SCENE_LUST_SETTINGS)
    assert set(safe.NEW_SETTINGS) <= set(scene_choices.LUST_PLACES)
    assert not set(RESTORED) & set(safe.SETTING_WORDS)  # said as they are named
    from leeroy_mind.selfies import style_variety
    assert set(RESTORED) <= set(style_variety.DATE_SETTINGS)


# -- no mirror, phone, bicycle or gripped prop ------------------------------------------------------

def test_a_selfie_names_no_phone():
    spec = build_prompt(woman(photo={"kind": "selfie", "framing": "head_and_shoulders",
                                     "setting": "a quiet cafe", "outfit": "a grey jumper",
                                     "expression": "a small smile"}))
    said = _text(spec).replace("smartphone photo", "")
    assert spec.phrases[0] == ("selfie, close arm's-length shot", 0)
    assert "mirror" not in said and "phone" not in said
    assert {"mirror", "phone"} <= _terms(spec.negative, 0)


@pytest.mark.parametrize("framing,negative", [("head_and_shoulders", HEAD_NEGATIVE_MIRROR),
                                              ("upper_body", BODY_NEGATIVE_MIRROR)])
def test_a_mirror_selfie_stays_one_in_every_style_and_its_negative_names_no_mirror(framing,
                                                                                   negative):
    """Plain mirror selfies were fine in the audit (3% of 92); the lust ones are mirror selfies
    again too (owner, 2026-11-19)."""
    photo = {"kind": "mirror_selfie", "framing": framing, "setting": "a quiet cafe",
             "outfit": "a grey jumper", "expression": "a small smile"}
    spec = build_prompt(woman(photo=photo))
    assert spec.phrases[0] == ("mirror selfie", 0) and spec.negative == negative
    assert not {"mirror", "phone"} & _terms(spec.negative)
    assert "shirtless" in _terms(spec.negative, 0)
    assert not any("arms down" in t for t, _ in spec.phrases)  # an arm holds the camera
    plain_scene = Scene("mirror_selfie", "upper_body", "in a living room", "taking a quiet break",
                        scene_choices.OUTFITS["casual"], "a relaxed smile", "day", "anyone", "x",
                        recipe=scene_choices.RECIPE)
    snap = build_scene_prompt(woman(), plain_scene)
    assert snap.phrases[0] == ("mirror selfie", 0) and snap.negative == SNAP_NEGATIVE_MIRROR
    hot = build_scene_prompt(lusty(), replace(plain_scene, setting="a cafe terrace",
                                              outfit="a red satin midi dress",
                                              expression="a wink", style=LUST))
    assert hot.phrases[0] == ("mirror selfie", 0) and hot.negative == SNAP_NEGATIVE_LUST_MIRROR
    stored = ProfilePhoto("mirror_selfie", framing, "a rooftop bar at night",
                          "a red satin midi dress", "a playful smile")
    lust = build_prompt(lusty(photo=stored), kind_style=LUST)
    assert lust.phrases[0] == ("mirror selfie", 0)
    assert lust.negative == (HEAD_NEGATIVE_LUST_MIRROR if framing == "head_and_shoulders"
                             else BODY_NEGATIVE_LUST_MIRROR)
    assert not {"mirror", "phone"} & _terms(lust.negative)
    assert _COVERED_LUST in [t for t, pr in lust.negative if pr == 0]


def test_a_mirror_selfie_he_asks_for_keeps_its_mirror_and_its_caption():
    p = woman()
    w = words_scene.Words("staff bathroom at work", "mirror_selfie", "casual",
                          scene_choices.OUTFITS["casual"], None, "day", "fluorescent", None, ())
    scene = words_scene.scene_of(p, w, audience="him")
    assert scene["shows"].startswith("a mirror selfie, ")
    spec = words_scene.build_words_prompt(p, scene)
    assert spec.phrases[0] == ("mirror selfie", 0) and spec.negative == SNAP_NEGATIVE_MIRROR
    held = words_scene.build_words_prompt(p, words_scene.scene_of(
        p, replace(w, framing="handheld_selfie"), audience="him"))
    assert held.phrases[0] == ("selfie, close arm's-length shot", 0)
    assert held.negative == SNAP_NEGATIVE and not any("arms down" in t for t, _ in held.phrases)
    timer = words_scene.build_words_prompt(p, words_scene.scene_of(
        p, replace(w, framing="timer"), audience="him"))
    assert timer.phrases[0] == ("photo taken on a timer", 0) and not _posed(timer)


def test_the_selfies_long_form_shortens_before_the_worker_would_refuse_a_request():
    from leeroy_mind.selfies.prompt import POSE_SHORT, over_fit
    assert POSE_SHORT["selfie, close arm's-length shot"] == "selfie"
    big = replace(APPEARANCE, build="voluptuous", height_cm=160, weight_kg=118, bust="very_large",
                  hips="very_wide", waist="wide",
                  hair_colour="honey blonde with caramel highlights",
                  hair_style="long glossy waves over one shoulder",
                  skin_tone="warm golden brown, freckled")
    p = woman(appearance=big)
    w = words_scene.Words("the quiet corner of the staff car park", "handheld_selfie",
                          "work_uniform", "a hi-vis vest over a work t-shirt and work trousers",
                          None, "day", "fluorescent", None, ())
    spec = words_scene.build_words_prompt(p, words_scene.scene_of(p, w, audience="him"))
    assert spec.phrases[0] == ("selfie", 0)  # the short form, as a back view's "from behind"
    w = replace(w, place="staff bathroom at work", outfit=scene_choices.OUTFITS["casual"])
    assert not over_fit(words_scene.build_words_prompt(p, words_scene.scene_of(
        p, w, audience="him")))


def test_a_request_keeps_the_owners_accessories_ahead_of_its_moment():
    """Review of #362, round 2: what the owner set her to wear are a request's first optional
    phrases, their room reserved; the moment comes after them. No framing takes a pose phrase
    (the audit's timer pose was taken out again, owner 2026-11-19)."""
    from leeroy_mind.selfies.scene import limb_detail
    a = replace(APPEARANCE, glasses="always", tattoos="small", piercings="nose",
                makeup="defined", jewellery="statement")
    p = woman(appearance=a)
    acc = [t for t, _ in accessory_phrases(p) if not limb_detail(t)]
    assert len(acc) == 5
    for framing in words_scene.FRAMINGS:
        w = words_scene.Words("staff bathroom at work", framing, "casual",
                              scene_choices.OUTFITS["casual"], "quick, between calls", "day",
                              "fluorescent", None, ())
        spec = words_scene.build_words_prompt(p, words_scene.scene_of(p, w, audience="him"))
        texts = [t for t, _ in spec.phrases]
        assert all((t, 1) in spec.phrases for t in acc)
        assert max(map(texts.index, acc)) < texts.index("quick, between calls")
        assert not _posed(spec)
        kept, dropped = _worker_fit()(spec.phrases, estimate, FIT_MAX)
        assert all(t in kept for t in acc), (framing, dropped)


def test_a_requests_selfie_framing_shortens_before_an_accessory_or_her_hair_style():
    """Review of #362, round 2: the selfie's long form takes its short one in the first step of
    the fit (prompt.fit_required's `short`), before her hair's style would go for the
    accessories' room; it never yields."""
    from leeroy_mind.selfies.prompt import hair_skin
    worn = dict(glasses="always", piercings="nose", makeup="defined", jewellery="statement")
    p = woman(appearance=replace(APPEARANCE, build="curvy", weight_kg=70, height_cm=165,
                                 hair_style="loose beach waves over one shoulder", **worn))
    w = words_scene.Words("staff bathroom at work", "handheld_selfie", "work_uniform",
                          words_scene.OFFICE_WEAR, "quick, between calls", "day", "fluorescent",
                          None, ())
    spec = words_scene.build_words_prompt(p, words_scene.scene_of(p, w, audience="him"))
    assert spec.phrases[0] == ("selfie", 0)  # shortened, still required
    assert (hair_skin(p, None)[0][0], 0) in spec.phrases  # her hair keeps its style
    kept, dropped = _worker_fit()(spec.phrases, estimate, FIT_MAX)
    assert all(t in kept for t, _ in accessory_phrases(p)), dropped
    for build, kg in (("average", 60), ("curvy", 70), ("voluptuous", 98)):
        for style in ("in a messy bun", "long glossy waves", "loose beach waves over one shoulder"):
            q = woman(appearance=replace(APPEARANCE, build=build, weight_kg=kg, height_cm=165,
                                         hair_style=style, **worn))
            spec = words_scene.build_words_prompt(q, words_scene.scene_of(q, w, audience="him"))
            if (hair_skin(q, None)[0][0], 0) not in spec.phrases:  # her hair lost its style ...
                assert spec.phrases[0] == ("selfie", 0)  # ... only after the framing shortened


def test_the_lust_style_is_offered_a_mirror_selfie_again():
    """The owner put the mirror selfie back on the lust lists (2026-11-19): the profile schema,
    the situational schema, the variety balancer and the re-roll offer it, as before the
    audit."""
    props = kind.LUST_SCHEMA["properties"]
    persona_kinds = ["selfie", "mirror_selfie", "headshot", "snapshot"]  # ProfilePhoto.kind
    assert props["kind"]["enum"] == persona_kinds == list(kind.LUST_KINDS)
    data = {"kind": "mirror_selfie", "framing": "upper_body", "setting": "a wine bar",
            "outfit": "satin midi dress", "colour": "red", "expression": "a wink"}
    assert kind.screen_lust(lusty(), data) == []
    assert kind.screen_lust(lusty(), {**data, "setting": "the mirror in her hallway"}) == []
    assert kind.screen_lust(lusty(), {**data, "kind": "selfie"}) == []
    assert kind.screen_lust(lusty(), {**data, "kind": "portrait"}) == [
        "photo.kind: not on its list"]
    from leeroy_mind.selfies import scene, style_variety, variety
    assert not hasattr(scene, "LUST_KINDS")
    assert scene.SCENE_SCHEMA_LUST["properties"]["kind"]["enum"] == list(scene.KINDS)
    assert scene.SCENE_SCHEMA["properties"]["kind"]["enum"] == list(scene.KINDS)
    assert "mirror_selfie" in scene.KINDS and "mirror_selfie" in style_variety.KINDS
    rolled = {variety.choose_photo(lusty(), seed=seed, ident=_NOBODY, names=()).kind
              for seed in range(40)}
    assert rolled == {"selfie", "mirror_selfie", "snapshot"}


def test_a_stored_kind_renders_as_it_was_chosen():
    """A lust mirror selfie at the hallway mirror renders as a mirror selfie there again; the
    car seat and the concert crowd are said as named; the audit's other words stay."""
    stored = ProfilePhoto("mirror_selfie", "three_quarter", "the mirror in her hallway",
                          "a fitted little red dress", "a playful smile")
    assert lust_phrase(stored) == "wearing a fitted short red dress, the mirror in her hallway"
    spec = build_prompt(lusty(photo=stored), kind_style=LUST)
    assert spec.phrases[0] == ("mirror selfie", 0) and spec.negative == BODY_NEGATIVE_LUST_MIRROR
    for setting in RESTORED:
        assert lust_phrase(replace(stored, setting=setting)).endswith(setting)
    assert lust_phrase(replace(stored, setting="the gym")).endswith("an empty gym")
    selfie = build_prompt(lusty(photo=replace(stored, kind="selfie")), kind_style=LUST)
    assert selfie.phrases[0] == ("selfie", 0) and selfie.negative == BODY_NEGATIVE_LUST
    jacket = replace(stored, outfit="a black leather jacket over a crop top")
    assert "a black leather jacket over a crop top and jeans" in lust_phrase(jacket)


def test_every_retemplated_outfit_keeps_its_key_and_reads_its_old_phrase():
    for choice, new in safe.OUTFIT_WORDS.items():
        assert kind.VARIED_OUTFITS.get(choice, kind.LUST_OUTFITS.get(choice)) == new
        old = kind._article(kind.OLD_TEMPLATES[choice].format(colour="red"))
        assert safe.outfit_words(old) == kind._article(new.format(colour="red"))
    assert safe.outfit_words("a grey hoodie") == "a grey hoodie"
    said = " ".join(kind.VARIED_OUTFITS.values())
    assert "bike" not in said and "one-shoulder" not in said and "little" not in said


def test_nothing_is_held_and_the_bike_is_gone():
    said = [phrase for phrase, _ in scene_choices.ACTIVITIES.values()]
    said += list(scene_choices.PROPS.values())
    for phrase in said:
        assert not re.search(r"\b(holding|bike|bicycle|chopping|knitting a|writing|raising|"
                             r"stirring|rolling|playing|sketching a|painting a)\b", phrase), phrase
    assert len(scene_choices.ACTIVITIES) == 30 and len(scene_choices.PLACES) == 20


def test_rows_composed_before_the_audit_keep_their_channel_and_codes():
    old = {"activity": "enjoying a cup of tea, holding a plain mug",
           "setting": scene_choices.PLACES["living_room"]}
    new = scene_choices.compose_choices({"activity": "tea", "setting": "living_room",
                                         "outfit": "casual", "expression": "smile",
                                         "prop": "mug"})
    assert scene_choices.channel_for(old) == scene_choices.channel_for(new) == "food"
    assert scene_choices.prop_key(old["activity"]) == scene_choices.prop_key(
        new["activity"]) == "mug"
    assert scene_choices.channel_for({"activity": "walking a dog on a leash, holding a dog leash",
                                      "setting": scene_choices.PLACES["park"]}) == "pets"
    assert new["activity"] == "a cup of tea on the table, a plain mug on the table"
    assert set(scene_choices.LEGACY_ACTIVITIES) <= set(scene_choices.ACTIVITIES)


# -- poses ------------------------------------------------------------------------------------------

def test_the_audits_poses_are_gone():
    """The owner took the audit's standing and waist-up poses out again (2026-11-19): safe.py
    composes none, and a body shot is posed as before the audit (a varied set by its view)."""
    for name in ("POSES", "POSE_SHORT", "POSE_WAIST_UP", "pose", "pose_priority", "kind_of",
                 "RETIRED_SETTINGS"):
        assert not hasattr(safe, name), name
    from leeroy_mind.selfies.prompt import SHORT
    assert not any(AUDIT_POSE.search(t) for t in (*SHORT, *SHORT.values()))


def test_a_selfie_takes_no_pose_and_a_lust_selfie_kind_keeps_its_framing():
    """Review of #362, round 2: an arm holds the camera, so no selfie takes a pose at any
    framing ("arms at her sides" would contradict it); since 2026-11-19 no photo takes one of
    the audit's. The owner lifted the lust selfie's waist-up rule (2026-11-19): it follows the
    share and renders as stored."""
    for k in safe.SELFIE_KINDS:
        assert safe.lust_framing(0, 9, 1.0, k) == "three_quarter"
        assert safe.lust_framing(9, 0, 0.5, k) == "upper_body"  # the share, as for any kind
        assert safe.selfie_framing(k, "three_quarter") == "three_quarter"
        assert safe.selfie_framing(k, "head_and_shoulders") == "head_and_shoulders"
    assert safe.selfie_framing("snapshot", "three_quarter") == "three_quarter"
    assert safe.lust_framing(0, 9, 1.0) == "three_quarter"
    # a plain kind keeps its framing as stored, and a selfie no pose
    for k in safe.SELFIE_KINDS:
        spec = build_prompt(woman(photo={"kind": k, "framing": "three_quarter",
                                         "setting": "a quiet cafe", "outfit": "a grey jumper",
                                         "expression": "a small smile"}))
        assert not _posed(spec)
        assert any(t.startswith("three-quarter shot") for t, _ in spec.phrases)
    # a lust selfie or mirror selfie stored three-quarter
    for k, said, negative in (("selfie", "selfie", BODY_NEGATIVE_LUST),
                              ("mirror_selfie", "mirror selfie", BODY_NEGATIVE_LUST_MIRROR)):
        stored = ProfilePhoto(k, "three_quarter", "a rooftop bar at night",
                              "a red satin midi dress", "a playful smile")
        spec = build_prompt(lusty(photo=stored), kind_style=LUST)
        texts = [t for t, _ in spec.phrases]
        assert spec.phrases[0] == (said, 0) and not _posed(spec)
        assert any(t.startswith("three-quarter shot") for t in texts)
        assert spec.negative == negative
    # a situational photo at three-quarter, a selfie or a snapshot: no pose
    s = Scene("selfie", "three_quarter", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["casual"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    for k in (*safe.SELFIE_KINDS, "snapshot"):
        assert not _posed(build_scene_prompt(woman(), replace(s, kind=k)))
    # the variety balancer and a re-roll frame a lust selfie like any other kind
    from leeroy_mind.selfies import variety
    rolled = [variety.choose_photo(lusty(), seed=seed, ident=_NOBODY, names=())
              for seed in range(40)]
    assert {x.framing for x in rolled if x.kind == "selfie"} == {"upper_body", "three_quarter"}
    assert {x.framing for x in rolled if x.kind == "snapshot"} == {"upper_body", "three_quarter"}


def test_a_three_quarter_profile_photo_takes_no_pose():
    stored = ProfilePhoto("snapshot", "three_quarter", "a rooftop bar at night",
                          "a red satin midi dress", "a playful smile")
    spec = build_prompt(lusty(photo=stored), kind_style=LUST)
    assert not _posed(spec)
    assert (safe.ALONE, 3) in spec.phrases  # a bar: nobody else in frame, where there is room
    assert spec.phrases[0] == ("candid photo", 0)  # the short forms make the room
    plain = build_prompt(man(photo={"kind": "snapshot", "framing": "three_quarter",
                                    "setting": "a quiet cafe", "outfit": "a grey jumper",
                                    "expression": "a small smile"}))
    assert not _posed(plain)


def test_a_situational_photo_takes_no_pose():
    s = Scene("snapshot", "upper_body", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["casual"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    spec = build_scene_prompt(woman(), s)
    texts = [t for t, _ in spec.phrases]
    assert safe.ALONE in texts and not _posed(spec)
    for framing in ("head_and_shoulders", "three_quarter"):
        assert not _posed(build_scene_prompt(woman(), replace(s, framing=framing)))


# -- the body negative ------------------------------------------------------------------------------

STYLE_WORDS = {"cartoon", "anime", "painting", "illustration", "3d render", "cgi"}
GUARDS = {"child", "teenager", "celebrity", "famous person", "nsfw", "nude", "bottomless",
          "shirtless"}


def test_the_body_negatives_require_the_style_words_again_beside_the_audits_lines():
    """The owner put the quality words back where they were before the audit (2026-11-19): in a
    profile body negative the style words are required again, as in the profile negative; the
    face and hands, the second face and the vintage words are at 1 and the watermark at 2. The
    audit's lines stay required where the worker's 75 tokens leave them room; the covered line
    and the missing garments always."""
    for negative, covered in ((BODY_NEGATIVE, _COVERED), (BODY_NEGATIVE_MIRROR, _COVERED),
                              (BODY_NEGATIVE_LUST, _COVERED_LUST),
                              (BODY_NEGATIVE_LUST_MIRROR, _COVERED_LUST),
                              (VARIED_NEGATIVE, _COVERED_LUST)):
        required = _terms(negative, 0)
        assert (covered, 0) in negative and ("bottomless, shirtless", 0) in negative
        assert STYLE_WORDS | GUARDS <= required
        assert {"extra hands", "extra arms", "extra legs", "fused fingers", "malformed hands",
                "deformed feet"} <= required
        assert negative[-len(safe.BODY_OPTIONAL):] == safe.BODY_OPTIONAL
    assert ("deformed face, bad hands", 1) in safe.BODY_OPTIONAL
    assert ("vintage, retro, film grain, sepia, old photo", 1) in safe.BODY_OPTIONAL
    assert ("second face, crowd, group of people", 1) in safe.BODY_OPTIONAL
    assert ("watermark", 2) in safe.BODY_OPTIONAL
    # the audit's lines: required where they fit, else first among the optional phrases
    for negative in (BODY_NEGATIVE, BODY_NEGATIVE_MIRROR, BODY_NEGATIVE_LUST,
                     BODY_NEGATIVE_LUST_MIRROR):
        assert ("another person, duplicate person", 0) in negative
    assert ("mirror, phone", 0) in BODY_NEGATIVE
    assert ("mirror, phone", 1) in BODY_NEGATIVE_LUST  # the style words took its room
    assert not {"mirror", "phone"} & (_terms(BODY_NEGATIVE_MIRROR)
                                      | _terms(BODY_NEGATIVE_LUST_MIRROR))
    assert ("another person, duplicate person", 1) in VARIED_NEGATIVE
    assert ("mirror", 0) in VARIED_NEGATIVE and "phone" not in _terms(VARIED_NEGATIVE)
    assert ("thong, g-string", 0) in VARIED_NEGATIVE
    assert _terms(NEGATIVE, 0) <= _terms(BODY_NEGATIVE, 0)
    assert _terms(LUST_NEGATIVE, 0) <= _terms(BODY_NEGATIVE_LUST, 0)
    assert _terms(LUST_NEGATIVE, 0) < _terms(HEAD_NEGATIVE_LUST, 0)
    with pytest.raises(ValueError):  # the missing garments are never loosened
        body_negative(_COVERED, loose=("bottomless, shirtless",))


def test_the_situational_negative_has_its_anatomy_line_back():
    """The owner put the situational negative's quality words back at their priorities before the
    audit (2026-11-19): the anatomy line with its limbs, fingers and eye words required, "3d
    render" required for a plain photo (2 for a lust one), the other style words at 2. The audit's
    "bottomless, shirtless" stays required beside them, which leaves "mutated, disfigured" at 1;
    its third hand, duplicate person, mirror and phone follow at 1."""
    anatomy = {"extra limbs", "extra legs", "extra arms", "fused fingers", "missing fingers",
               "extra fingers", "malformed hands", "deformed feet", "cross-eyed", "lazy eye",
               "asymmetric eyes"}
    for negative, lust, mirror in ((SNAP_NEGATIVE, False, False), (SNAP_NEGATIVE_LUST, True, False),
                                   (SNAP_NEGATIVE_MIRROR, False, True),
                                   (SNAP_NEGATIVE_LUST_MIRROR, True, True)):
        required = _terms(negative, 0)
        assert (_COVERED_LUST if lust else _COVERED, 0) in negative
        assert anatomy | GUARDS | {"another person", "cartoon", "painting"} <= required
        assert ("3d render" in required) is not lust
        assert not {"anime", "cgi", "illustration", "mutated", "disfigured"} & required
        optional = [x for x in negative if x[1] > 0]
        assert optional[:3] == [("mutated, disfigured", 1), ("extra hands", 1),
                                ("duplicate person", 1)]
        assert (("mirror, phone", 1) in negative) is not mirror
        for line in (("second face, crowd, group of people", 1),
                     ("vintage, retro, film grain, sepia, old photo", 1),
                     ("illustration, watermark", 2),
                     ("3d render, anime, cgi" if lust else "anime, cgi", 2),
                     ("face on screen, webcam, video call, portrait on wall", 2)):
            assert line in negative


KEPT = {  # what the worker's fit keeps of each body negative's optional phrases (real tokenizers)
    "body": ["plastic skin"],
    "body-mirror": ["plastic skin", "airbrushed, doll"],
    "body-lust": [],
    "varied": [],
    "snap": ["extra hands"],
    "snap-lust": [],
}


@pytest.mark.skipif(clip_bpe.tokenizers() is None, reason="no RealVisXL tokenizer files")
@pytest.mark.parametrize("name,negative", [
    ("body", BODY_NEGATIVE), ("body-mirror", BODY_NEGATIVE_MIRROR),
    ("body-lust", BODY_NEGATIVE_LUST), ("varied", VARIED_NEGATIVE),
    ("snap", SNAP_NEGATIVE), ("snap-lust", SNAP_NEGATIVE_LUST)])
def test_what_each_body_negative_keeps_and_drops_is_pinned_for_the_gpu_comparison(name, negative):
    """What the worker's fit keeps of a body negative's optional phrases beside its required set
    (71 plain, 67 mirror, 73 lust, 74 varied, 71 and 73 situational): the style and anatomy
    words at 0 leave little room, so most of the quality words at 1 and 2 are dropped, as on main
    before the owner's revert."""
    kept, dropped = _worker_fit()(negative, clip_bpe.count, FIT_MAX)
    assert [t for t, pr in negative if pr > 0 and t not in dropped] == KEPT[name]


def test_the_owners_never_words_go_ahead_of_a_body_negatives_optional_phrases():
    a = replace(APPEARANCE, glasses="never", tattoos="none", piercings="none", jewellery="none")
    p = lusty(appearance=a)
    for base in (BODY_NEGATIVE_LUST, VARIED_NEGATIVE, SNAP_NEGATIVE, SNAP_NEGATIVE_LUST):
        got = negative_for(p, base)
        first = next(i for i, (_, pr) in enumerate(got) if pr > 0)
        assert got[first:first + 4] == (("glasses", 1), ("tattoo", 1), ("piercing", 1),
                                        ("jewellery", 1))
        assert [x for x in got if x not in got[first:first + 4]] == list(base)
        if clip_bpe.tokenizers() is not None:  # room where the body negative allows: the
            kept, _ = _worker_fit()(got, clip_bpe.count, FIT_MAX)  # varied set's has none
            assert ("glasses" in kept.split(", ")) is (base is not VARIED_NEGATIVE)
    assert negative_for(lusty(), BODY_NEGATIVE_LUST) == BODY_NEGATIVE_LUST  # nothing set
    head = negative_for(p, HEAD_NEGATIVE)
    assert head[:len(HEAD_NEGATIVE)] == HEAD_NEGATIVE  # as #358 adds them there


def test_what_he_set_her_to_wear_keeps_its_room_on_a_body_shot():
    """The fit order: required, then the accessories (the kind of photo makes way for them),
    then her hair's style and eye colour."""
    a = replace(APPEARANCE, glasses="always", tattoos="sleeve", piercings="nose", makeup="glam",
                jewellery="statement")
    stored = ProfilePhoto("snapshot", "three_quarter", "a rooftop bar at night",
                          "a red satin midi dress", "a playful smile")
    p = lusty(appearance=a, photo=stored)
    spec = build_prompt(p, kind_style=LUST)
    acc = [t for t, _ in accessory_phrases(p)]
    assert len(acc) == 5
    kept, dropped = _worker_fit()(spec.phrases, estimate, FIT_MAX)
    assert all(t in kept for t in acc), dropped
    assert "wearing a red satin midi dress, a rooftop bar at night" in kept
    texts = [t for t, _ in spec.phrases]
    assert texts.index(acc[0]) < texts.index("hazel eyes")


@_TOKENIZERS
@pytest.mark.parametrize("negative", [HEAD_NEGATIVE, HEAD_NEGATIVE_LUST, HEAD_NEGATIVE_MIRROR,
                                      HEAD_NEGATIVE_LUST_MIRROR, BODY_NEGATIVE,
                                      BODY_NEGATIVE_LUST, BODY_NEGATIVE_MIRROR,
                                      BODY_NEGATIVE_LUST_MIRROR, VARIED_NEGATIVE, SNAP_NEGATIVE,
                                      SNAP_NEGATIVE_LUST, SNAP_NEGATIVE_MIRROR,
                                      SNAP_NEGATIVE_LUST_MIRROR, TILE_NEGATIVE],
                         ids=["head", "head-lust", "head-mirror", "head-lust-mirror", "body",
                              "body-lust", "body-mirror", "body-lust-mirror", "varied", "snap",
                              "snap-lust", "snap-mirror", "snap-lust-mirror", "tile"])
def test_every_negatives_required_set_fits_the_worker(negative):
    """Measured with the worker's own fit and the real tokenizers (71, 73 and 74 for the body,
    lust body and varied negatives, 71 and 73 for the situational ones, since the quality words
    came back on 2026-11-19): an addition at priority 0 that pushes one over fails here, since
    the worker would refuse every such photo."""
    required = ", ".join(t for t, pr in negative if pr == 0)
    assert clip_bpe.count(required) <= FIT_MAX
    kept, _ = _worker_fit()(negative, clip_bpe.count, FIT_MAX)
    assert all(t in kept for t, pr in negative if pr == 0)


# -- her body in situational and request photos -----------------------------------------------------

HEAVY = replace(APPEARANCE, build="voluptuous", height_cm=165, weight_kg=98, bust="large",
                hair_style="loose beach waves over one shoulder", skin_tone="warm golden olive")


def _request_scene(p):
    w = words_scene.Words("staff bathroom at work", "handheld_selfie", "work_uniform",
                          words_scene.OFFICE_WEAR, "quick, between calls", "day", "fluorescent",
                          None, ())
    return words_scene.scene_of(p, w, audience="him")


@_TOKENIZERS
@pytest.mark.parametrize("style", ["plain", "lust"])
def test_her_size_and_build_are_required_and_survive_the_fit_in_snaps_and_requests(style):
    p = (lusty if style == "lust" else woman)(appearance=HEAVY)
    s = Scene("snapshot", "upper_body", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["casual"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    if style == "lust":
        s = replace(s, setting="a cafe terrace", outfit="a red satin midi dress",
                    expression="a wink", style=LUST)
    body = body_words(p, short=True)
    assert len(body) == 2 and body[0] == "plus-size, heavyset"
    for spec in (build_scene_prompt(p, s),
                 words_scene.build_words_prompt(p, _request_scene(p))):
        kept, dropped = _worker_fit()(spec.phrases, clip_bpe.count, FIT_MAX)
        assert all(any(t.startswith(w) and pr == 0 for t, pr in spec.phrases) for w in body)
        assert all(w in kept for w in body), dropped
        assert "wearing " in kept


# -- how much body shows ----------------------------------------------------------------------------

def test_the_share_is_a_setting(tmp_path):
    assert SelfieCfg().body_shot_share == safe.BODY_SHOT_SHARE == 1.0  # four body views
    assert SelfieCfg().snap_body_share == safe.SNAP_BODY_SHARE == 0.5
    toml = tmp_path / "mind.toml"
    toml.write_bytes(b"[selfies]\nbody_shot_share = 0.5\nsnap_body_share = 1\n")
    assert load_selfie_config(toml).body_shot_share == 0.5
    assert load_selfie_config(toml).snap_body_share == 1.0
    assert [safe.lust_framing(w, c, 1.0) for w, c in ((0, 0), (9, 0), (3, 7))] == [
        "three_quarter"] * 3
    assert safe.lust_framing(1, 0, 0.5) == "upper_body" and safe.lust_framing(0, 5, 0) == (
        "upper_body")
    toml.write_bytes(b"[selfies]\nbody_shot_share = 1.5\n")
    with pytest.raises(Exception, match="body_shot_share"):
        load_selfie_config(toml)


def test_settle_framing_keeps_the_share():
    seeds = [f"s{i}" for i in range(400)]
    kept = sum(safe.settle_framing("three_quarter", 0.5, s) == "three_quarter" for s in seeds)
    assert 150 < kept < 250
    assert all(safe.settle_framing("three_quarter", 1.0, s) == "three_quarter" for s in seeds)
    assert all(safe.settle_framing("three_quarter", 0.0, s) == "upper_body" for s in seeds)
    assert safe.settle_framing("upper_body", 0.0, "s") == "upper_body"
    assert safe.settle_framing("upper_body", 0.0, "s", wide=("upper_body", "three_quarter"),
                               narrow="head_and_shoulders") == "head_and_shoulders"
    assert [safe.body_slots(x) for x in (0, 0.25, 0.5, 0.75, 1)] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("share,body", [(0.0, 0), (0.25, 1), (0.5, 2), (0.75, 3), (1.0, 4)])
def test_a_varied_set_shows_her_figure_in_that_share_of_its_candidates(share, body):
    p = lusty(appearance=APPEARANCE)
    for seed in range(40):
        looks = varied.compose(p, f"seed-{seed}", share)
        assert looks is not None and len({x.view for x in looks}) == 4
        shots = [x for x in looks if x.framing in kind.VARIED_BODY_FRAMINGS]
        assert len(shots) == body
        assert all(x.framing == "upper_body" for x in looks if x not in shots)
        backs = [x for x in looks if x.view in kind.BACK_VIEWS]
        if body >= 2:  # the views from behind first
            assert all(x in shots for x in backs)
        for x in looks:
            assert varied.screen(p, x) == []
            if x.category == "swim" and x.view in kind.BACK_VIEWS:
                assert x.framing == "full_body"
            elif x in shots and x.view not in kind.BACK_VIEWS:
                assert x.framing == "three_quarter"  # 6% defective against full-length's 12%
        assert looks == varied.compose(p, f"seed-{seed}", share)


def test_the_default_set_shows_her_figure_in_all_four_views():
    p = lusty(appearance=APPEARANCE)
    looks = varied.compose(p, "seed")
    assert all(x.framing in kind.VARIED_BODY_FRAMINGS for x in looks)
    by_view = {x.view: x for x in looks}
    assert by_view["front"].framing == by_view["side"].framing == "three_quarter"
    back = build_varied_prompt(p, by_view["behind"])
    assert ("from behind, looking over her shoulder", 0) in back.phrases  # its pose, the view
    assert not _posed(back) and back.negative == VARIED_NEGATIVE
    half = {x.view: x for x in varied.compose(p, "seed", 0.5)}
    assert half["front"].framing == half["side"].framing == "upper_body"
    spec = build_varied_prompt(p, half["front"])
    assert not _posed(spec)
    assert any(t == "waist-up shot" for t, _ in spec.phrases)


def test_a_recomposed_slot_follows_the_share():
    p = lusty(appearance=APPEARANCE)
    looks = varied.compose(p, "seed")
    assert varied.recompose_slot(p, looks, 0, "s").framing == "three_quarter"
    assert varied.recompose_slot(p, looks, 0, "s", 0.5).framing == "upper_body"


def test_a_stored_plan_from_before_the_audit_still_reads(store):
    seed_test_cast(store)
    old = varied.Look("gym", "side", "full_body", "the gym", "fitted tank top and bike shorts",
                      "red", "a wink")
    looks = (old,) + tuple(varied.compose(lusty(appearance=APPEARANCE), "seed")[1:])
    with store.transaction():
        varied.write_plan(store, 101, 3, looks, "2026-11-14")
        assert varied.plan_of(store.conn, 101, 3) == looks
    spec = build_varied_prompt(lusty(appearance=APPEARANCE), old)
    assert "bike" not in _text(spec) and "gym shorts" in _text(spec, 0)
    assert "an empty gym" in _text(spec, 0)


# -- selfies audit ----------------------------------------------------------------------------------

def _labels(folder, rows):
    folder.mkdir(exist_ok=True)
    (folder / "labels-0.jsonl").write_bytes(
        ("\n".join(json.dumps(r) for r in rows) + "\nnot json\n").encode())
    return folder


def _row(guid, tail, severity=0, same="yes"):
    return {"guid": guid, "kind": "x", "path": f"X:/somewhere/selfies/{guid}/{tail}",
            "severity": severity, "same_person_as_cover": same, "note": "private words"}


def test_the_audit_names_defective_covers_and_stocked_snaps_and_go_writes_rows_only(
        store, data, tmp_path):
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    for guid in (101, 102, 103):
        ready(store, data, guid, 1)
        pick_photo(store, guid, 1, 2 if guid == 101 else 0, source="console", now=5.0)
    bad_ready = ready_snap(store, data, 101)
    bad_planned = planned_snap(store, 102)
    bad_sent = sent_snap(store, data, 103)
    fine = ready_snap(store, data, 103)
    folder = _labels(tmp_path / "labels", [
        _row(101, "chosen.jpg", severity=2),            # a defective cover
        _row(102, "sets/1/c0.jpg", same="no"),          # 102's cover, as its set candidate
        _row(102, "sets/1/c1.jpg", severity=3),         # a candidate that is not the cover
        _row(103, "chosen.jpg", severity=1),            # fine
        _row(101, f"snaps/{bad_ready}.jpg", severity=3),
        _row(102, f"snaps/{bad_planned}.jpg", same="no"),
        _row(103, f"snaps/{bad_sent}.jpg", severity=2),  # already sent: left out
        _row(103, f"snaps/{fine}.jpg", severity=1),
        _row(104, f"snaps/{bad_ready}.jpg", severity=3),  # another member's id: unknown
        {"guid": "x", "path": 3, "severity": "bad"}])
    labels, counts = audit.read_labels(folder)
    assert counts == {"files": 1, "rows": 9, "malformed": 2}
    os.utime(folder / "labels-0.jsonl", (10.0, 10.0))
    labels, _ = audit.read_labels(folder)
    with store.transaction():
        report = audit.plan(store.conn, labels)
    assert report["covers"] == [
        {"guid": 101, "set": 1, "index": 2, "codes": ["severity"]},
        {"guid": 102, "set": 1, "index": 0, "codes": ["identity"]}]
    assert report["snaps"] == [
        {"id": bad_ready, "guid": 101, "status": "ready", "codes": ["severity"]},
        {"id": bad_planned, "guid": 102, "status": "planned", "codes": ["identity"]}]
    assert report["left_out"] == {"not_cover": 1, "snap_sent": 1, "snap_unknown": 1}
    assert "private words" not in json.dumps(report) and "somewhere" not in json.dumps(report)
    before = sorted(p.name for p in data.rglob("*") if p.is_file())
    done = audit.apply(store, report, now=20.0)
    assert done["expired"] == [bad_ready, bad_planned]
    assert done["queued"] == [{"guid": 101, "set": 2}, {"guid": 102, "set": 2}]
    assert _snap(store, bad_ready) == ("expired", "flagged")
    assert _snap(store, fine)[0] == "ready" and _snap(store, bad_sent)[0] == "sent"
    assert sorted(p.name for p in data.rglob("*") if p.is_file()) == before  # no file deleted
    cover = store.conn.execute("SELECT after_set, after_idx FROM selfie_pick WHERE guid=101 "
                               "ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(cover) == (1, 2)  # the cover itself is never changed
    with store.transaction():
        again = audit.apply(store, audit.plan(store.conn, labels), now=30.0)
    assert again["expired"] == [] and again["queued"] == [] and again["already_queued"] == [
        101, 102]


def test_a_cover_picked_after_the_labels_were_written_is_left_out(store, data, tmp_path):
    seed_test_cast(store)
    renderable_cast(store, 101)
    ready(store, data, 101, 1)
    folder = _labels(tmp_path / "labels", [_row(101, "chosen.jpg", severity=3)])
    os.utime(folder / "labels-0.jsonl", (10.0, 10.0))
    pick_photo(store, 101, 1, 0, source="console", now=50.0)
    with store.transaction():
        report = audit.plan(store.conn, audit.read_labels(folder)[0])
    assert report["covers"] == [] and report["left_out"] == {"cover_changed": 1}


def test_the_audit_command_plans_read_only_and_go_expires_and_queues(tmp_path, capsys):
    from leeroy_mind.selfies.cli import selfies_main
    from selfies.fakes import mind_app
    root = tmp_path / "mind-data"
    root.mkdir()
    made = {}

    def prepare(s):
        ready(s, root, 101, 1)
        pick_photo(s, 101, 1, 0, source="console", now=5.0)
        made["snap"] = ready_snap(s, root, 101)
    mind_app(root, prepare=prepare).store.close()
    with pytest.raises(ValueError, match="labels_missing"):
        audit.read_labels(tmp_path / "none")
    args = ["--data-dir", str(root), "audit", "--labels"]
    assert selfies_main([*args, str(tmp_path / "none")]) == 1
    assert "labels_missing" in capsys.readouterr().err
    folder = _labels(tmp_path / "labels", [_row(101, "sets/1/c0.jpg", severity=3),
                                           _row(101, f"snaps/{made['snap']}.jpg", same="no")])
    assert selfies_main([*args, str(folder)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True and [c["guid"] for c in plan["covers"]] == [101]
    assert [s["id"] for s in plan["snaps"]] == [made["snap"]] and "expired" not in plan
    assert selfies_main([*args, str(folder), "--go"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["expired"] == [made["snap"]] and done["queued"] == [{"guid": 101, "set": 2}]
    assert selfies_main([*args, str(folder)]) == 0  # nothing left to do
    again = json.loads(capsys.readouterr().out)
    assert again["snaps"] == [] and again["left_out"] == {"snap_expired": 1}
