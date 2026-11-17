"""Compositions the photo audit showed render badly, designed out (selfies/safe.py): clothed by
construction, no mirror, phone, bicycle or gripped prop, one simple standing pose for a body
shot, the body negative, how much body shows as a setting, and `selfies audit`. Every person
here is invented."""
import json
import os
import re
from dataclasses import replace

import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.selfies import audit, kind, safe, scene_choices, varied, words_scene
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.prompt import (BODY_NEGATIVE, BODY_NEGATIVE_LUST, FIT_MAX,
                                        HEAD_NEGATIVE, HEAD_NEGATIVE_LUST, LUST_NEGATIVE,
                                        NEGATIVE, VARIED_NEGATIVE, body_words, build_prompt,
                                        build_varied_prompt, lust_phrase)
from leeroy_mind.selfies.scene import (SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, TILE_NEGATIVE, Scene,
                                       _NOBODY, _text_codes, build_scene_prompt,
                                       build_tile_prompt)
from leeroy_mind.selfies.store import pick as pick_photo
from leeroy_mind.selfies.style import AGE_RE, LUST
from nightly.test_night_enrich import APPEARANCE
from selfies import clip_bpe
from selfies.fakes import planned_snap, ready, ready_snap, renderable_cast, sent_snap
from selfies.test_selfie_prompt import _worker_fit, man, woman
from selfies.test_selfie_scene import lusty

_TOKENIZERS = pytest.mark.skipif(
    clip_bpe.tokenizers() is None,
    reason="RealVisXL tokenizer files are not on this machine (set LEEROY_SELFIE_DATA)")
COLOUR = "cobalt blue"  # the longest colour


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
    for phrase in (*scene_choices.OUTFITS.values(), *words_scene.PLAIN_WEAR.values(),
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
        "a rain jacket and jeans over a crew-neck t-shirt")
    assert safe.complete("", gender="male", framing="upper_body") == (
        "a crew-neck t-shirt and trousers")
    assert safe.complete("grey hoodie", gender="male", framing="upper_body") == (
        "grey hoodie and trousers")


def test_a_headshot_names_a_top_with_a_neckline():
    assert safe.complete("a silver necklace", gender="female", framing="head_and_shoulders") == (
        "a silver necklace over a crew-neck top")
    assert safe.complete("black graphic t-shirt", gender="male", framing="face") == (
        "black graphic t-shirt")
    assert safe.complete("", gender="male", framing="head_and_shoulders") == "a crew-neck t-shirt"
    assert safe.is_complete("a crew-neck top", head=True)
    assert not safe.is_complete("a silver necklace", head=True)


def test_an_overlong_outfit_gives_way_to_the_default_garments():
    long = " ".join(["soft"] * (safe.WEAR_MAX_WORDS + 1))
    assert safe.complete(long, gender="female", framing="upper_body") == (
        "a crew-neck top and trousers")


def test_a_plain_profile_prompt_requires_what_they_wear():
    spec = build_prompt(man(photo={"kind": "snapshot", "framing": "upper_body",
                                   "setting": "a quiet cafe", "outfit": "grey hoodie",
                                   "expression": "a small smile"}))
    assert ("wearing grey hoodie and trousers", 0) in spec.phrases
    assert spec.negative == BODY_NEGATIVE
    head = build_prompt(man(photo={"kind": "selfie", "framing": "head_and_shoulders",
                                   "setting": "a quiet cafe", "outfit": "a wool scarf",
                                   "expression": "a small smile"}))
    assert ("wearing a wool scarf over a crew-neck t-shirt", 0) in head.phrases
    assert head.negative == HEAD_NEGATIVE and set(NEGATIVE) < set(HEAD_NEGATIVE)
    assert {"shirtless", "mirror", "phone"} <= _terms(HEAD_NEGATIVE, 0)


def test_an_outfit_that_holds_a_name_gives_way_to_the_default_garments():
    spec = build_prompt(man(photo={"kind": "snapshot", "framing": "upper_body",
                                   "setting": "a quiet cafe", "outfit": "Arkon's guild hoodie",
                                   "expression": "a small smile"}))
    assert ("wearing a crew-neck t-shirt and trousers", 0) in spec.phrases
    assert "Arkon" not in _text(spec)


def test_a_situational_photo_and_a_tile_require_what_they_wear():
    s = Scene("snapshot", "upper_body", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["outdoors"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    spec = build_scene_prompt(man(), s)
    assert ("wearing a jacket over a t-shirt and trousers", 0) in spec.phrases
    tile = build_tile_prompt(man(appearance=replace(APPEARANCE, at_the_pc="a dressing gown")))
    assert ("wearing a crew-neck t-shirt", 1) in tile.phrases  # the screens refuse the gown
    tile = build_tile_prompt(man(appearance=replace(APPEARANCE, at_the_pc="a band t-shirt")))
    assert ("wearing a band t-shirt", 1) in tile.phrases
    first_optional = next(t for t, pr in tile.phrases if pr > 0)
    assert first_optional == "wearing a band t-shirt"  # the first phrase the fit keeps


# -- every new string meets the screens -----------------------------------------------------------

def _new_strings():
    out = [*safe.KIND_WORDS.values(), *safe.SETTING_WORDS.values(), *safe.NEW_SETTINGS,
           *safe.DEFAULT_TOP.values(), safe.DEFAULT_BOTTOM, safe.POSE_WAIST_UP, safe.ALONE,
           *(pose for poses in safe.POSES.values() for pose in poses),
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
    *(("activity", pose) for poses in safe.POSES.values() for pose in poses),
    ("activity", safe.POSE_WAIST_UP), ("activity", safe.ALONE)])
def test_every_composed_scene_phrase_meets_the_scene_screens(field, text):
    assert _text_codes(field, text, ident=_NOBODY, names=[]) == []


def test_the_new_settings_are_offered_and_the_retired_ones_are_not():
    assert set(safe.NEW_SETTINGS) <= set(kind.LUST_SETTINGS)
    assert not set(safe.RETIRED_SETTINGS) & set(kind.LUST_SETTINGS)
    assert len(kind.LUST_SETTINGS) == 21
    assert not set(safe.RETIRED_SETTINGS) & set(scene_choices.SCENE_LUST_SETTINGS)
    assert set(safe.NEW_SETTINGS) <= set(scene_choices.LUST_PLACES)


# -- no mirror, phone, bicycle or gripped prop ------------------------------------------------------

@pytest.mark.parametrize("photo_kind", ["selfie", "mirror_selfie"])
def test_a_selfie_names_no_phone_and_no_mirror(photo_kind):
    spec = build_prompt(woman(photo={"kind": photo_kind, "framing": "head_and_shoulders",
                                     "setting": "a quiet cafe", "outfit": "a grey jumper",
                                     "expression": "a small smile"}))
    said = _text(spec).replace("smartphone photo", "")
    assert spec.phrases[0] == ("selfie, close arm's-length shot", 0)
    assert "mirror" not in said and "phone" not in said
    assert {"mirror", "phone"} <= _terms(spec.negative, 0)


def test_the_lust_style_is_never_offered_a_mirror_selfie():
    props = kind.LUST_SCHEMA["properties"] if "properties" in kind.LUST_SCHEMA else None
    text = json.dumps(kind.LUST_SCHEMA)
    assert "mirror_selfie" not in text and "the mirror in her hallway" not in text
    assert props is None or "mirror_selfie" not in props["kind"]["enum"]
    data = {"kind": "mirror_selfie", "framing": "upper_body", "setting": "a wine bar",
            "outfit": "satin midi dress", "colour": "red", "expression": "a wink"}
    assert kind.screen_lust(lusty(), data) == ["photo.kind: not on its list"]
    assert kind.screen_lust(lusty(), {**data, "kind": "selfie"}) == []
    from leeroy_mind.selfies import scene, style_variety
    assert "mirror_selfie" not in scene.KINDS and "mirror_selfie" not in style_variety.KINDS


def test_a_kind_stored_before_the_audit_renders_with_the_words_that_replaced_it():
    stored = ProfilePhoto("mirror_selfie", "three_quarter", "the mirror in her hallway",
                          "a fitted little red dress", "a playful smile")
    assert lust_phrase(stored) == "wearing a fitted short red dress, her hallway at home"
    spec = build_prompt(lusty(photo=stored), kind_style=LUST)
    assert "mirror" not in _text(spec) and spec.negative == BODY_NEGATIVE_LUST
    for old, new in (("the front seat of her car", "a quiet street beside her parked car"),
                     ("a concert crowd", "an open-air festival field at dusk"),
                     ("the gym", "an empty gym")):
        assert lust_phrase(replace(stored, setting=old)).endswith(new)
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

def test_a_body_shot_stands_in_one_simple_pose_and_never_with_a_hand_on_a_hip():
    for view, poses in safe.POSES.items():
        for text in poses:
            assert text.startswith("standing, ") and not re.search(
                r"\b(hips?|rail|glass|sitting|seated|kneeling|lying|crossed)\b", text)
        assert safe.pose(view, "full_body", "seed") in poses
    assert set(safe.POSES) == set(kind.VARIED_VIEWS)
    assert safe.pose("front", "head_and_shoulders") == "" == safe.pose("front", "face")
    assert safe.pose("front", "upper_body") == safe.POSE_WAIST_UP
    assert safe.pose("front", "three_quarter", pos="his") == "standing, arms at his sides"
    assert safe.pose_priority("three_quarter") == 0 == safe.pose_priority("full_body")
    assert safe.pose_priority("upper_body") == 1


def test_a_three_quarter_profile_photo_requires_its_pose():
    stored = ProfilePhoto("snapshot", "three_quarter", "a rooftop bar at night",
                          "a red satin midi dress", "a playful smile")
    spec = build_prompt(lusty(photo=stored), kind_style=LUST)
    assert any(t.startswith("standing, ") and pr == 0 for t, pr in spec.phrases)
    assert (safe.ALONE, 3) in spec.phrases  # a bar: nobody else in frame, where there is room
    assert spec.phrases[0] == ("candid photo", 0)  # the short forms make the room


def test_a_situational_photo_keeps_its_hands_out_of_the_picture():
    s = Scene("snapshot", "upper_body", "at a quiet cafe table", "a cup of tea on the table",
              scene_choices.OUTFITS["casual"], "a relaxed smile", "day", "anyone", "x",
              recipe=scene_choices.RECIPE)
    spec = build_scene_prompt(woman(), s)
    texts = [t for t, _ in spec.phrases]
    assert safe.POSE_WAIST_UP in texts and safe.ALONE in texts
    assert texts.index(safe.POSE_WAIST_UP) < texts.index("a cup of tea on the table")
    close = build_scene_prompt(woman(), replace(s, framing="head_and_shoulders"))
    assert safe.POSE_WAIST_UP not in [t for t, _ in close.phrases]


# -- the body negative ------------------------------------------------------------------------------

def test_the_body_negative_trades_the_eye_words_for_hands_duplicates_and_garments():
    for negative in (BODY_NEGATIVE, BODY_NEGATIVE_LUST, VARIED_NEGATIVE, SNAP_NEGATIVE,
                     SNAP_NEGATIVE_LUST):
        required = _terms(negative, 0)
        assert {"extra hands", "extra arms", "extra legs", "fused fingers", "malformed hands",
                "deformed feet", "another person", "duplicate person", "mirror", "phone", "text",
                "bottomless", "shirtless", "child", "teenager", "celebrity",
                "famous person", "nsfw", "nude"} <= required
        assert not {"cross-eyed", "lazy eye", "asymmetric eyes"} & _terms(negative)
    assert {"thong", "g-string"} <= _terms(VARIED_NEGATIVE, 0)
    assert _terms(LUST_NEGATIVE, 0) <= _terms(BODY_NEGATIVE_LUST, 0) | {
        "anime", "illustration", "3d render", "cgi"}
    assert _terms(LUST_NEGATIVE, 0) < _terms(HEAD_NEGATIVE_LUST, 0)


@_TOKENIZERS
@pytest.mark.parametrize("negative", [HEAD_NEGATIVE, HEAD_NEGATIVE_LUST, BODY_NEGATIVE,
                                      BODY_NEGATIVE_LUST, VARIED_NEGATIVE, SNAP_NEGATIVE,
                                      SNAP_NEGATIVE_LUST, TILE_NEGATIVE],
                         ids=["head", "head-lust", "body", "body-lust", "varied", "snap",
                              "snap-lust", "tile"])
def test_every_negatives_required_set_fits_the_worker(negative):
    """Measured with the worker's own fit and the real tokenizers (67, 69 and 75 for the body,
    lust body and varied negatives when written): an addition at priority 0 that pushes one
    over fails here, since the worker would refuse every such photo."""
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
    assert SelfieCfg().body_shot_share == safe.BODY_SHOT_SHARE == 0.5
    toml = tmp_path / "mind.toml"
    toml.write_bytes(b"[selfies]\nbody_shot_share = 1\n")
    assert load_selfie_config(toml).body_shot_share == 1.0
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
        assert looks == varied.compose(p, f"seed-{seed}", share)


def test_the_default_set_frames_the_front_and_side_views_waist_up():
    p = lusty(appearance=APPEARANCE)
    looks = varied.compose(p, "seed")
    by_view = {x.view: x for x in looks}
    assert by_view["front"].framing == by_view["side"].framing == "upper_body"
    spec = build_varied_prompt(p, by_view["front"])
    assert ("waist-up shot", 0) in spec.phrases or ("waist-up shot", 1) in spec.phrases
    assert (safe.POSE_WAIST_UP, 1) in spec.phrases
    back = build_varied_prompt(p, by_view["behind"])
    assert ("standing, arms at her sides", 0) in back.phrases
    assert back.negative == VARIED_NEGATIVE


def test_a_recomposed_slot_follows_the_share():
    p = lusty(appearance=APPEARANCE)
    looks = varied.compose(p, "seed")
    assert varied.recompose_slot(p, looks, 0, "s").framing == "upper_body"
    assert varied.recompose_slot(p, looks, 0, "s", 1.0).framing == "full_body"


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
