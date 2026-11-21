"""Five fixed coverage levels: fake subjects/workers only, no GPU or live state."""
from dataclasses import replace

import pytest

from leeroy_mind.config import ConfigError
from leeroy_mind.heat import live as heat_live, photos
from leeroy_mind.selfies import kind, outfit_reveal as R, varied, words_scene
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.prompt import build_prompt, build_varied_prompt, over_fit
from leeroy_mind.selfies.request_scene import choose_scene
from leeroy_mind.selfies.scene import (build_scene_prompt, scene_from, scene_json,
                                     scene_renderable)
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.style import LUST, PLAIN
from conftest import run
from nightly.conftest import h  # noqa: F401
from heat.test_heat_photos import Tier
from selfies.test_request_scene import app, save  # noqa: F401
from selfies.test_selfie_prompt import woman, man
from selfies.test_varied_sets import DAY, ON, _prompts, _render, lust
from selfies.test_words_scene import LUST_PICKS, words


def scene(category="swim", **over):
    picks = {**LUST_PICKS, "audience": "him", "framing": "upper_body",
             "setting": kind.WATER_SETTINGS[0] if category == "swim" else "the gym",
             "outfit": next(iter(kind.SWIM_OUTFITS if category == "swim" else kind.GYM_OUTFITS))}
    return replace(scene_from(compose_choices(picks)), **over)


def test_setting_is_omitted_by_default_and_requires_integer_one_to_five(tmp_path):
    path = tmp_path / "mind.toml"
    assert SelfieCfg().outfit_reveal is load_selfie_config(path).outfit_reveal is None
    for level in range(1, 6):
        path.write_text(f"[selfies]\noutfit_reveal = {level}\n")
        cfg = load_selfie_config(path)
        assert cfg.outfit_reveal == level and cfg.nsfw_max == .2
        assert not cfg.romance_styling and not cfg.enabled
    for bad in ("0", "6", "-1", "true", '"5"', "2.5"):
        path.write_text(f"[selfies]\noutfit_reveal = {bad}\n")
        with pytest.raises(ConfigError, match="outfit_reveal"):
            load_selfie_config(path)
    path.write_text("[selfies]\noutfit_reveal = 5\nunknown = true\n")
    assert load_selfie_config(path).outfit_reveal is None


@pytest.mark.parametrize("category", ["swim", "gym"])
@pytest.mark.parametrize("level", range(1, 6))
@pytest.mark.parametrize("parts", [False, True])
def test_each_level_has_distinct_complete_outfits_and_all_colours_pass_existing_screens(category, level, parts):
    p = woman(age=30, stance="lust")
    if parts:
        p = replace(p, appearance=replace(p.appearance, bust="very_large", glutes="very_large",
                                          waist="wide", hips="very_wide"))
    look = next(x for x in varied.compose(p, "coverage") if x.category == category)
    assert len(set(R.choices(category, level))) == 2
    for choice in R.choices(category, level):
        for colour in kind.LUST_COLOURS:
            x = replace(look, outfit=choice, colour=colour)
            assert varied.screen(p, x) == []
            spec = build_varied_prompt(p, x)
            assert not over_fit(spec)
            assert x.phrase in " ".join(t for t, _ in spec.phrases)
            assert photos.look_tier(x) == max(R.HEAT_TIER[category], photos.VIEW_TIER[x.view])
            assert photos.choice_of(x.phrase) == choice
            negative = " ".join(t for t, _ in spec.negative)
            assert "underwear" in negative and "topless" in negative
    assert set(R.choices(category, level)).isdisjoint(
        c for other in range(1, 6) if other != level for c in R.choices(category, other))


def test_level_five_is_opaque_beach_swimwear_and_gym_remains_gymwear():
    assert all("thong bikini bottoms" in c and "opaque" in c for c in R.choices("swim", 5))
    assert all("shorts" in c and "sports crop top" in c for c in R.choices("gym", 5))
    assert not any("bikini" in c or "thong" in c for c in R.choices("gym", 5))


@pytest.mark.parametrize("category", ["swim", "gym"])
@pytest.mark.parametrize("level", range(1, 6))
def test_private_scene_roundtrip_prompt_caption_and_heat_recognize_actual_outfit(category, level):
    p, original = woman(age=30, stance="lust"), scene(category)
    changed = R.restyle_scene(p, original, level=level, category=category, seed=7)
    assert changed != original and changed.outfit_reveal == level
    assert changed.outfit in changed.shows and original.outfit not in changed.shows
    assert scene_from(scene_json(changed)) == changed
    assert scene_renderable(p, changed) is None
    spec = build_scene_prompt(p, changed)
    assert changed.outfit in " ".join(t for t, _ in spec.phrases)
    assert not over_fit(spec)
    assert photos.scene_tier(scene_json(changed)) == R.HEAT_TIER[category]
    assert R.restyle_scene(p, original, level=level, category=category, seed=7) == changed
    negative = " ".join(t for t, _ in spec.negative)
    assert "underwear" in negative and "topless" in negative


@pytest.mark.parametrize("over", [{"audience": "anyone"}, {"style": PLAIN},
    {"setting": "a bedroom beside a pool"}, {"setting": "in her living room"},
    {"activity": "sitting on the bed"}])
def test_reveal_never_spills_into_public_plain_or_wrong_place(over):
    p, original = woman(age=30, stance="lust"), scene(**over)
    assert R.restyle_scene(p, original, level=5, category="swim", seed=1) is original


@pytest.mark.parametrize("p", [woman(age=24, stance="lust"), woman(age=30, stance="fond"),
                               man(age=30, stance="lust")])
def test_reveal_cannot_grant_eligibility(p):
    original = scene()
    assert R.restyle_scene(p, original, level=5, category="swim", seed=1) is original


def test_name_collisions_and_young_looks_keep_original_clothes():
    p, original = woman(age=30, stance="lust"), scene()
    assert R.restyle_scene(p, original, level=5, category="swim", seed=1,
                           names=("bikini",)) is original
    p = replace(p, appearance=replace(p.appearance, face=("a youthful teenage face",)))
    assert R.restyle_scene(p, original, level=5, category="swim", seed=1) is original


def test_heat_still_limits_the_category_independently(monkeypatch):
    p = woman(age=30, stance="lust")
    monkeypatch.setattr(heat_live, "_LIVE", Tier(3))
    assert R.restyle_scene(p, scene(), level=5, category="swim", seed=1).outfit_reveal == 0
    assert R.restyle_scene(p, scene("gym"), level=5, category="gym", seed=1).outfit_reveal == 5
    monkeypatch.setattr(heat_live, "_LIVE", Tier(2))
    assert R.restyle_scene(p, scene("gym"), level=5, category="gym", seed=1).outfit_reveal == 0


def test_missing_setting_keeps_all_legacy_outputs_identical():
    p, original = woman(age=30, stance="lust"), scene()
    assert R.restyle_scene(p, original, level=None, category="swim", seed=1) is original
    assert "outfit_reveal" not in scene_json(original)
    for look in varied.compose(p, "legacy"):
        assert R.restyle_look(p, look, None, 1) is look
    assert R.profile(p, None, 1, kind_style=LUST) is p


@pytest.mark.parametrize("over", [{"outfit_reveal": 6}, {"outfit_reveal": True},
    {"outfit_reveal": False}, {"outfit_reveal": "5"},
    {"outfit": "invented unlisted outfit"}, {"setting": "a bedroom"},
    {"audience": "anyone"}, {"style": PLAIN}])
def test_stored_reveal_is_rechecked_at_render(over):
    p = woman(age=30, stance="lust")
    changed = R.restyle_scene(p, scene(), level=5, category="swim", seed=1)
    assert scene_renderable(p, replace(changed, **over))


def test_varied_worker_prompt_and_persisted_plan_agree_and_only_swim_gym_change(h, tmp_path):
    lust(h)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    before = varied.plan_of(h.store.conn, 101, 1)
    _render(h, tmp_path, replace(ON, outfit_reveal=5))
    after = varied.plan_of(h.store.conn, 101, 1)
    prompts = _prompts(h, 101, 1)
    assert len(prompts) == 4
    for old, new, prompt in zip(before, after, prompts):
        if old.category in R.POOLS:
            assert new.outfit in R.choices(old.category, 5)
        else:
            assert old == new
        assert new.phrase in prompt
        assert (old.view, old.framing, old.setting) == (new.view, new.framing, new.setting)


def test_profile_transform_preserves_kind_and_stored_persona():
    original = scene("gym")
    p = woman(age=30, stance="lust", photo={f: getattr(original, f)
        for f in ("kind", "framing", "outfit", "setting", "expression")})
    changed = R.profile(p, 5, 1, kind_style=LUST)
    assert changed.photo != p.photo and p.photo.outfit == original.outfit
    assert changed.photo.kind == p.photo.kind and changed.photo.setting == p.photo.setting
    assert "sports crop top" in " ".join(t for t, _ in build_prompt(changed, kind_style=LUST).phrases)
    assert R.profile(p, 5, 1, kind_style=PLAIN) is p


@pytest.mark.parametrize("audience", ["him", "anyone"])
def test_enum_request_reaches_reveal_and_channel_request_is_routed_privately(app, audience):
    save(app, woman(age=30, stance="lust"))
    app.selfies.cfg = replace(app.selfies.cfg, outfit_reveal=5)
    app.llm.answers = [{**LUST_PICKS, "outfit": next(iter(kind.SWIM_OUTFITS)),
                        "setting": kind.WATER_SETTINGS[0]}]
    chosen = run(choose_scene(app, 101, "a beach photo", audience))
    assert chosen.audience == "him" and chosen.outfit_reveal == 5
    assert "thong bikini bottoms" in chosen.outfit


def test_plain_public_request_remains_plain_with_reveal_enabled(app):
    app.selfies.cfg = replace(app.selfies.cfg, outfit_reveal=5)
    chosen = run(choose_scene(app, 101, "an ordinary photo", "anyone"))
    assert chosen.audience == "anyone" and chosen.outfit_reveal == 0
    assert chosen.style == PLAIN and "bikini" not in chosen.outfit


@pytest.mark.parametrize("romance", [False, True])
def test_words_request_gets_actual_private_beach_outfit_and_keeps_gym_separate(app, romance):
    save(app, woman(age=30, stance="lust"))
    if romance:
        from selfies.test_romance_styling import enable
        enable(app, stance="lust")
    app.selfies.cfg = replace(app.selfies.cfg, outfit_reveal=5)
    reply = {"place": "on a beach", "wearing": "swimwear", "framing": "handheld_selfie",
             "moment": "taking a break", "prop": "", "lighting": "daylight"}
    chosen = words(app, reply)
    assert chosen["outfit_reveal"] == 5 and "thong bikini bottoms" in chosen["outfit"]
    assert bool(chosen.get("romance_style")) == romance
    assert chosen["outfit"] in chosen["shows"]
    assert chosen["outfit"] in " ".join(t for t, _ in words_scene.build_words_prompt(
        app.personas.get(101), chosen).phrases)
    gym = words(app, {**reply, "place": "at the gym", "wearing": "gym"})
    assert gym["outfit_reveal"] == 5 and "sports crop top" in gym["outfit"]
    assert "bikini" not in gym["outfit"]
    build_scene_prompt(app.personas.get(101), scene_from(gym))


def test_ordinary_profile_worker_draws_configured_gym_outfit_without_editing_persona(h, tmp_path):
    from leeroy_mind.personas.io import load_persona, save_persona
    from leeroy_mind.personas.schema import ProfilePhoto
    from leeroy_mind.selfies.store import queue_more
    original = scene("gym")
    photo = ProfilePhoto(**{f: getattr(original, f)
        for f in ("kind", "framing", "outfit", "setting", "expression")})
    p = replace(lust(h), photo=photo)
    save_persona(h.store, p, source="generated", ts=h.T, race="Human", class_name="Warrior")
    kind._record(h.store, 101, LUST, photo)
    queue_more(h.store, 101, now=h.T)
    _render(h, tmp_path, SelfieCfg(enabled=True, outfit_reveal=5))
    prompts = _prompts(h, 101, 1)
    assert len(prompts) == 4 and all("sports crop top" in text for text in prompts)
    assert all("shorts" in text and "bikini" not in text for text in prompts)
    assert load_persona(h.store, 101).photo == photo


@pytest.mark.parametrize("audience,expected", [("him", 5), ("anyone", 0)])
def test_night_scene_uses_final_audience_and_private_reveal(h, audience, expected):
    from leeroy_mind.selfies.scenes import ask_scene
    lust(h)
    reply = {**LUST_PICKS, "outfit": next(iter(kind.SWIM_OUTFITS)),
             "setting": kind.WATER_SETTINGS[0]}
    chosen, codes = run(ask_scene(h.ctx({"scene_lust": [reply]}), 101,
        about="a quiet afternoon", audience=audience, outfit_reveal=5))
    assert not codes and chosen.audience == audience
    assert chosen.outfit_reveal == expected


def test_weight_room_is_an_existing_gym_place_not_a_new_context():
    p, original = woman(age=30, stance="lust"), scene("gym", setting="in the weights room")
    changed = R.restyle_scene(p, original, level=5, category="gym", seed=1)
    assert changed.outfit_reveal == 5 and scene_renderable(p, changed) is None
