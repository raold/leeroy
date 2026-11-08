"""Fixed vocabulary and fake-only readiness checks; no renderer or model is loaded."""
from dataclasses import replace
from itertools import combinations, product
import random

import pytest

from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.selfies import scene, scene_choices as choices
from leeroy_mind.selfies.kind import LUST_OUTFITS, LUST_COLOURS, SWIM_OUTFITS, WATER_SETTINGS
from leeroy_mind.selfies.stage import seed_for
from leeroy_mind.selfies.worker import _Malformed, _similarity
from nightly.conftest import IDENT
from selfies.test_selfie_prompt import woman, _tokens, _worker_fit

PICKS = {"photo": True, "kind": "snapshot", "framing": "upper_body", "setting": "park",
         "activity": "dog_walk", "outfit": "outdoors", "expression": "smile",
         "when": "day", "audience": "anyone", "prop": "leash"}


@pytest.mark.parametrize("field", list(PICKS)[1:])
def test_every_text_field_is_an_enum_and_rejects_free_text(field):
    check_schema(scene.SCENE_SCHEMA, PICKS)
    with pytest.raises(ValueError):
        check_schema(scene.SCENE_SCHEMA, {**PICKS, field: "anything off the list"})


def test_every_pair_of_choices_composes_only_screened_phrases():
    assert len(choices.ACTIVITIES) == 30 and len(choices.PLACES) == 20
    props = scene.SCENE_SCHEMA["properties"]
    fields = [f for f in props if f != "photo"]
    for first, second in combinations(fields, 2):
        for a, b in product(props[first]["enum"], props[second]["enum"]):
            picks = {**PICKS, first: a, second: b}
            check_schema(scene.SCENE_SCHEMA, picks)
            # Even incoherent picks cannot compose unsafe text; they are still refused.
            composed = choices.compose_choices(picks)
            assert scene.screen_scene(woman(), composed, ident=IDENT, member_names=[]) == []
    # Every list value has at least one reachable paired configuration.
    for activity, (_, places) in choices.ACTIVITIES.items():
        picks = {**PICKS, "activity": activity, "setting": places[0], "prop": "none"}
        assert choices.pairing_codes(picks) == []


def test_capped_full_product_has_no_banned_phrase():
    props = scene.SCENE_SCHEMA["properties"]
    fields = [f for f in props if f != "photo"]
    rng = random.Random(13)
    # Draw from every axis of the full Cartesian product, capped at 5000 combinations.
    for _ in range(5000):
        picks = {"photo": True, **{f: rng.choice(props[f]["enum"]) for f in fields}}
        composed = choices.compose_choices(picks)
        assert scene.screen_scene(woman(), composed, ident=IDENT, member_names=[]) == []


def test_all_activity_place_prop_pairs_follow_the_declared_rules():
    for activity, place, prop in product(choices.ACTIVITIES, choices.PLACES, choices.PROPS):
        codes = choices.pairing_codes({**PICKS, "activity": activity, "setting": place,
                                      "prop": prop})
        assert ("scene.activity: place pairing" in codes) == (
            place not in choices.ACTIVITIES[activity][1])
        assert ("scene.prop: activity pairing" in codes) == (
            prop != "none" and activity not in choices.PROP_ACTIVITIES[prop])


def test_lust_settings_keep_profile_pairings_and_never_bathing_or_mirrors():
    p = woman(stance="lust")
    for setting, outfit in product(choices.SCENE_LUST_SETTINGS, LUST_OUTFITS):
        picks = {**PICKS, "setting": setting, "activity": "relaxing", "prop": "none",
                 "outfit": outfit, "colour": LUST_COLOURS[0], "expression": "laughing"}
        check_schema(scene.SCENE_SCHEMA_LUST, picks)
        codes = scene.screen_scene(p, choices.compose_choices(picks), ident=IDENT,
                                   member_names=[])
        if outfit in SWIM_OUTFITS and setting not in WATER_SETTINGS:
            assert "scene.outfit: swimwear away from the water" in codes
        assert not scene.UNDRESS_RE.search(setting) and "mirror" not in setting


def test_all_lust_activity_place_pairs_are_coherent_or_refused():
    for activity, setting in product(choices.ACTIVITIES, choices.SCENE_LUST_SETTINGS):
        picks = {**PICKS, "setting": setting, "activity": activity, "prop": "none",
                 "colour": "navy", "outfit": "fitted little dress", "expression": "laughing"}
        codes = choices.pairing_codes(picks)
        expected = activity != "relaxing" and choices.LUST_PLACES[setting] not in choices.ACTIVITIES[activity][1]
        assert bool(codes) == expected


def test_old_bathing_scene_is_held_without_rewriting_or_rendering_it():
    from selfies.test_selfie_scene import SWIM_SCENE
    s = replace(scene.scene_from(SWIM_SCENE), setting="a hot tub on a wooden deck")
    assert scene.scene_renderable(woman(stance="lust"), s) == "scene.setting: undress"
    with pytest.raises(ValueError, match="undress"):
        scene.build_scene_prompt(woman(stance="lust"), s)


def test_tile_identity_and_screen_words_survive_fitting():
    p = woman()
    spec = scene.build_tile_prompt(p)
    fitted = _worker_fit()(spec.phrases, _tokens)
    # The local test fitter returns (prompt, dropped), just like the worker.
    text = fitted[0]
    for words, _ in scene.looks_phrases(p, scene.names_re(p)):
        assert (words, 0) in spec.phrases and words in text
    assert choices.GAME_MONITOR in text and "video call" not in text
    assert (choices.ONE_FACE_NEGATIVE, 0) in spec.negative


def test_tile_seeds_and_retries_are_distinct_and_repeatable():
    seeds = [seed_for("2026-11-08", guid, 7, slot, attempt)
             for guid, slot, attempt in product(range(101, 117), (0, 1), range(3))]
    assert len(set(seeds)) == len(seeds)
    assert seeds[0] == seed_for("2026-11-08", 101, 7, 0, 0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, 1.01, -1.01, "secret"])
def test_similarity_rejects_malformed_scores(bad):
    with pytest.raises(_Malformed):
        _similarity({"code": "ok", "pairs": 1, "pair": [0, 1], "max": bad, "mean": 0}, 2)


def test_similarity_only_copies_known_codes_and_numeric_fields():
    assert _similarity(None, 2) == {"code": "worker_unsupported"}
    assert _similarity({"code": "encoder_failed", "prompt": "private"}, 2) == {
        "code": "encoder_failed"}
    assert _similarity({"code": "ok", "pairs": 1, "pair": [0, 1], "max": .5,
                        "mean": .5, "prompt": "private"}, 2) == {
        "code": "ok", "pairs": 1, "pair": [0, 1], "max": .5, "mean": .5}


def test_new_composed_scene_persists_its_recipe_but_old_prose_never_gains_one():
    new = scene.scene_from(PICKS)
    stored = scene.scene_json(new)
    assert new.recipe == stored["recipe"] == choices.RECIPE
    assert scene.scene_from(stored) == new
    old = {k: v for k, v in stored.items() if k != "recipe"}
    assert scene.scene_from(old).recipe == 0
    assert "recipe" not in scene.scene_json(scene.scene_from(old))
