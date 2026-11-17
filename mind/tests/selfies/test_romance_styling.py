"""Private clothed styling; fake models, invented adults, no GPU or live services."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.config import ConfigError
from leeroy_mind.life.day import Block
from leeroy_mind.personas.io import load_persona
from leeroy_mind.selfies import words_scene as W
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.kind import CLOTHED_OUTFITS, LUST_COLOURS
from leeroy_mind.selfies.request_scene import choose_scene
from leeroy_mind.selfies.romance_styling import EXPRESSIONS, enum_category, level, restyle
from leeroy_mind.selfies.scene import (SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, _text_codes,
                                     build_scene_prompt, scene_from, scene_json, scene_renderable)
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.scenes import make_scenes_stage
from leeroy_mind.selfies.snaps import planned
from leeroy_mind.selfies.tokens import estimate
from leeroy_mind.tuning.effective import MemberTuning, effective
from nightly.conftest import IDENT, h  # noqa: F401
from nightly.test_night_scenes import SNAPS_CFG, cast
from selfies.test_request_scene import app, save  # noqa: F401
from selfies.test_scene_choices import PICKS
from selfies.test_selfie_prompt import _worker_fit, woman, man
from selfies.test_words_scene import LUST_PICKS, words


def original(**over):
    return replace(scene_from(compose_choices(PICKS)), audience="him", **over)


def styled(p=None, scene=None, **over):
    args = dict(enabled=True, category="casual", relationship=None, dial=6, seed=7,
                ident=IDENT, names=[])
    return restyle(p or woman(age=30, stance="drawn"), scene or original(), **(args | over))


def enable(app, stance="drawn", dial=6):
    save(app, woman(age=30, stance=stance))
    app.cfg.romance = SimpleNamespace(dial=dial)
    app.selfies.cfg = replace(app.selfies.cfg, romance_styling=True)


def test_configuration_is_explicit_boolean_and_unknown_tables_do_not_enable(tmp_path):
    path = tmp_path / "mind.toml"
    assert not load_selfie_config(path).romance_styling
    path.write_text('[selfies]\nromance_styling=true\n')
    assert load_selfie_config(path).romance_styling
    assert load_selfie_config(path).nsfw_max == SelfieCfg().nsfw_max == .2
    path.write_text('[selfies]\nromance_styling=true\nunknown=true\n')
    assert not load_selfie_config(path).romance_styling
    path.write_text('[selfies]\nromance_styling="true"\n')
    with pytest.raises(ConfigError):
        load_selfie_config(path)


@pytest.mark.parametrize("pool", CLOTHED_OUTFITS)
def test_entire_wardrobe_passes_existing_screens_in_every_colour(pool):
    for template in CLOTHED_OUTFITS[pool]:
        for colour in LUST_COLOURS:
            text = template.format(colour=colour)
            assert not _text_codes("outfit", text, ident=IDENT, names=[]), text
            assert len(text.split()) <= 12
    all_choices = [t for values in CLOTHED_OUTFITS.values() for t in values]
    assert len(set(all_choices)) == len(all_choices) == 128


def test_all_expressions_and_head_tilts_pass_the_original_screens():
    for choices in EXPRESSIONS.values():
        for text in choices:
            assert not _text_codes("expression", text, ident=IDENT, names=[]), text


def test_styling_uses_the_owner_palette_and_preserves_a_selected_wardrobe():
    p = woman(age=30, stance="drawn")
    p = replace(p, appearance=replace(p.appearance, palette=("plum",)))
    assert all("plum" in styled(p, seed=i).outfit for i in range(30))
    p = replace(p, appearance=replace(p.appearance, wardrobe=("sporty",)))
    s = original()
    result = styled(p, s)
    assert result.outfit == s.outfit and result.romance_style == "playful"


def test_words_uses_her_wardrobe_before_styling_and_retains_the_work_uniform(app):
    enable(app)
    p = load_persona(app.store, 101)
    save(app, replace(p, appearance=replace(p.appearance, wardrobe=("smart",))))
    s = words(app, {"wearing": "casual"})
    assert s["outfit"] == "a button-up shirt and tailored trousers"
    assert s["romance_style"] == "playful"
    assert "romance_style" not in words(app, {"wearing": "work_uniform"})


@pytest.mark.parametrize("framing", ["handheld_selfie", "mirror_selfie", "timer"])
def test_private_swimwear_allows_three_cameras_and_keeps_waist_up(app, framing):
    enable(app, stance="lust", dial=10)
    s = words(app, {"place": "pool deck", "wearing": "swimwear", "framing": framing,
                    "moment": "relaxing", "prop": None, "lighting": "daylight"})
    assert s["words"]["wearing"] == "swimwear" and s["style"] == "lust"
    spec = W.build_words_prompt(load_persona(app.store, 101), s)
    assert (W.SWIM_FRAMING, 0) in spec.phrases
    assert spec.negative == SNAP_NEGATIVE_LUST
    assert "fitted outfits, date-night clothes" in app.llm.calls[-1][1][0]["content"]


@pytest.mark.parametrize("place,framing", [
    ("kitchen at home", "timer"), ("pool deck", W.BEHIND), ("bedroom by the pool", "timer")])
def test_swimwear_keeps_location_and_rear_view_limits(app, place, framing):
    enable(app, stance="lust", dial=10)
    s = words(app, {"place": place, "wearing": "swimwear", "framing": framing})
    assert s["words"]["wearing"] != "swimwear"


def test_disabled_style_keeps_the_old_prompt_and_swimwear_camera_rule(app):
    enable(app, stance="lust", dial=10)
    app.selfies.cfg = replace(app.selfies.cfg, romance_styling=False)
    s = words(app, {"place": "pool deck", "wearing": "swimwear", "framing": "timer"})
    assert s["words"]["wearing"] == "casual"
    assert "nothing revealing or sexual" in app.llm.calls[-1][1][0]["content"]


@pytest.mark.parametrize("audience,age", [("anyone", 30), ("him", 24)])
def test_words_prompt_stays_plain_for_public_or_ineligible_member(app, audience, age):
    enable(app)
    save(app, woman(age=age, stance="drawn"))
    if age < 25:
        # The request age gate refuses before making any model call.
        from leeroy_mind.selfies.request_scene import RequestSceneError
        with pytest.raises(RequestSceneError):
            words(app, audience=audience)
        assert not app.llm.calls
    else:
        s = words(app, {"wearing": "casual"}, audience=audience)
        assert "romance_style" not in s
        assert "nothing revealing or sexual" in app.llm.calls[-1][1][0]["content"]


@pytest.mark.parametrize("stance,stage,dial,tone", [
    ("none", "planning", 10, None), ("fond", "none", 10, "warm"),
    ("drawn", "none", 3, "warm"), ("drawn", "none", 6, "playful"),
    ("drawn", "voice", 8, "confident"), ("lust", "none", 10, "confident"),
    ("lust", "ex", 10, None),
])
def test_stance_stage_and_dial_use_existing_romance_controls(stance, stage, dial, tone):
    p = woman(age=30, stance=stance)
    assert level(p, SimpleNamespace(romance_stage=stage), dial) == tone


def test_member_dial_overrides_world_and_never_exceeds_clothed_styles():
    p = woman(age=30, stance="lust")
    p = replace(p, romance=replace(p.romance, dial=2))
    assert level(p, None, 10) == "warm"
    p = replace(p, romance=replace(p.romance, dial=10))
    assert level(p, None, 1) == "confident"


def test_forward_zero_is_read_from_the_effective_persona():
    p = effective(woman(age=30, stance="lust"), MemberTuning({"forward": 0}))
    assert level(p, None, 10) is None


@pytest.mark.parametrize("p", [woman(age=24, stance="lust"), man(age=30, stance="fond"),
                               woman(age=30, stance="none")])
def test_ineligible_members_keep_the_original_scene(p):
    s = original()
    assert styled(p, s) is s


def test_age_coded_looks_do_not_receive_romance_styling():
    p = woman(age=30, stance="lust")
    p = replace(p, appearance=replace(p.appearance, face=("teenage features",)))
    assert level(p, None, 10) is None


def test_public_and_disabled_paths_are_byte_identical_and_profiles_stay_intact():
    p, s = woman(age=30, stance="drawn"), original()
    before = p.photo
    assert styled(p, s, enabled=False) is s
    public = replace(s, audience="anyone")
    assert styled(p, public) is public
    assert "romance_style" not in scene_json(s)
    assert scene_from(scene_json(s)) == s
    assert styled(p, s).romance_style == "playful"
    assert p.photo == before


@pytest.mark.parametrize("category", ["as_now", "work_uniform", "swimwear", None])
def test_specific_existing_outfits_are_preserved(category):
    s = original()
    assert styled(scene=s, category=category) is s


def test_variation_is_repeatable_and_captions_agree_with_clothing():
    first = styled()
    assert styled() == first
    assert first.outfit in first.shows and first.expression in first.shows
    assert first.expression in EXPRESSIONS["playful"]
    assert len({styled(seed=i).outfit for i in range(50)}) > 35
    assert scene_from(scene_json(first)) == first


def test_added_clothes_and_expressions_obey_owner_and_member_name_screens():
    first = styled()
    s = original()
    colour = first.outfit.split()[1]
    assert styled(scene=s, names=[colour]) is s
    assert styled(scene=s, ident=replace(IDENT, real_name=colour)) is s
    p = woman(age=30, stance="drawn")
    p = replace(p, identity=replace(p.identity, real_name=colour))
    assert styled(p, s) is s


@pytest.mark.parametrize("activity,category", [("hiking", "outdoors"), ("gardening", "outdoors"),
    ("cooking", "casual"), ("repairing", "casual"), ("exercise", "gym")])
def test_activity_keeps_the_outfit_practical(activity, category):
    assert enum_category({**PICKS, "activity": activity}) == category


def test_clothing_and_identity_survive_the_worker_fit_with_unchanged_negatives():
    p = woman(age=30, stance="drawn")
    fit = _worker_fit()
    for category in ("casual", "going_out", "warm", "outdoors", "gym"):
        for seed in range(40):
            s = styled(p, category=category, seed=seed)
            spec = build_scene_prompt(p, s)
            prompt, _ = fit(spec.phrases, estimate, 75)
            assert s.outfit in prompt and "30-year-old" in prompt
            assert "hair" in prompt and "skin" in prompt
            assert spec.negative == SNAP_NEGATIVE


def test_stored_styled_scene_is_held_if_privacy_or_adult_identity_changes():
    p = woman(age=30, stance="drawn")
    s = styled(p)
    assert scene_renderable(p, replace(s, audience="anyone")) == "scene.styling: held"
    assert scene_renderable(woman(age=24, stance="drawn"), s) == "scene.styling: held"
    assert scene_renderable(woman(age=30, stance="none"), s) == "scene.styling: held"
    renamed = replace(p, identity=replace(p.identity, real_name=s.outfit.split()[1]))
    assert scene_renderable(renamed, s) == "scene.styling: named"


def test_live_enum_request_styles_private_output_after_original_screens(app):
    enable(app)
    app.llm.answers = [{**PICKS, "audience": "anyone"}]
    s = run(choose_scene(app, 101, "a walking photo", "him"))
    assert s.romance_style == "playful" and s.audience == "him"
    assert s.outfit in s.shows
    assert s.recipe == 1
    app.llm.answers = [{**PICKS, "audience": "him"}]
    public = run(choose_scene(app, 101, "a walking photo", "anyone"))
    assert public.romance_style == "" and public.outfit == "a jacket and trousers"


def test_words_request_retains_place_light_prop_framing_and_styled_clothing(app):
    enable(app)
    s = words(app, {"place": "kitchen at home", "wearing": "casual"})
    assert s["romance_style"] == "playful" and s["recipe"] == W.WORDS_RECIPE
    assert s["outfit"] in s["shows"]
    assert s["words"]["lighting"] == "fluorescent"
    assert s["words"]["prop"] == "paper coffee cup"
    spec = W.build_words_prompt(load_persona(app.store, 101), s)
    prompt, _ = _worker_fit()(spec.phrases, estimate, 75)
    assert s["outfit"] in prompt and "kitchen at home" in prompt
    assert (s["expression"], 2) in spec.phrases
    assert spec.phrases.index((s["expression"], 2)) > spec.phrases.index(("hazel eyes", 2))
    assert spec.negative == SNAP_NEGATIVE


def test_words_as_now_and_work_uniform_remain_unchanged(app):
    enable(app)
    for wearing in ("as_now", "work_uniform"):
        s = words(app, {"wearing": wearing})
        assert "romance_style" not in s
        assert s["words"]["wearing"] == wearing
    s = words(app, {"wearing": "as_now"}, block=Block.OUT)
    assert "romance_style" not in s and s["words"]["wearing"] == "going_out"


def test_lust_private_scene_keeps_its_original_holds_and_negatives(app):
    enable(app, stance="lust", dial=10)
    app.llm.answers = [LUST_PICKS]
    s = run(choose_scene(app, 101, "a photo please", "him"))
    assert (s.style, s.romance_style) == ("lust", "confident")
    assert build_scene_prompt(load_persona(app.store, 101), s).negative == SNAP_NEGATIVE_LUST
    assert scene_renderable(woman(age=30, stance="fond"), s) is not None


def test_night_uses_final_outlet_even_when_model_chooses_private(h):
    h.woman(101, "drawn", age=30)
    cast(h, 101)
    ctx = h.ctx({"scene": [{**PICKS, "audience": "him"}] * 2})
    ctx.settings = replace(ctx.settings, romance_dial=6)
    run(make_scenes_stage(replace(SNAPS_CFG, romance_styling=True), dial=lambda: 1,
                         outlets=lambda: True)(ctx))
    rows = planned(h.store, 10)
    assert {r.audience for r in rows} == {"him", "anyone"}
    private, public = sorted(rows, key=lambda r: r.audience, reverse=True)
    assert private.scene["romance_style"] == "playful"
    assert "romance_style" not in public.scene
    assert private.scene["outfit"] in private.scene["shows"]
