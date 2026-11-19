"""Private clothed styling; fake models, invented adults, no GPU or live services."""
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.config import ConfigError
from leeroy_mind.life.day import Block
from leeroy_mind.personas.io import load_persona
from leeroy_mind.selfies import safe
from leeroy_mind.selfies import words_scene as W
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.kind import CLOTHED_OUTFITS, LUST_COLOURS
from leeroy_mind.selfies.request_scene import choose_scene
from leeroy_mind.selfies.romance_styling import (EXPRESSIONS, enum_category, level,
                                                 outfit_forms, pose_choices, restyle,
                                                 short_outfit, template_of)
from leeroy_mind.selfies.prompt import negative_for, over_fit
from leeroy_mind.selfies.scene import (SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, _text_codes,
                                     build_scene_prompt, scene_from, scene_json, scene_renderable,
                                     snap_negative)
from leeroy_mind.selfies.scene_choices import OUTFITS, compose_choices
from leeroy_mind.selfies.scenes import make_scenes_stage
from leeroy_mind.selfies.snaps import planned
from leeroy_mind.selfies.tokens import estimate
from leeroy_mind.tuning.effective import MemberTuning, effective
from nightly.conftest import IDENT, h  # noqa: F401
from nightly.test_night_scenes import SNAPS_CFG, cast
from selfies.test_request_scene import app, save  # noqa: F401
from selfies.test_scene_choices import PICKS
from selfies import clip_bpe
from selfies.test_selfie_prompt import _worker_fit, woman, man
from selfies.test_words_scene import LUST_PICKS, words

_TOKENIZERS = pytest.mark.skipif(
    clip_bpe.tokenizers() is None,
    reason="RealVisXL tokenizer files are not on this machine (set LEEROY_SELFIE_DATA)")
LONGEST = max(LUST_COLOURS, key=len)
TEMPLATES = [t for pool in CLOTHED_OUTFITS.values() for t in pool]
# what the photo audit found drawn wrong (safe.OUTFIT_WORDS): fused garments, a skirt read as
# bottomless, a bicycle in the gym
AUDIT_TRIGGERS = re.compile(r"one-shoulder|mini ?skirt|\bbike\b|bicycle|cycling", re.I)
PARTS = dict(bust="very_large", glutes="very_large", waist="wide", hips="very_wide")


def composed(template: str, colour: str = LONGEST) -> str:
    phrase = template.format(colour=colour)
    return ("an " if phrase[0] in "aeiou" else "a ") + phrase


def drawn(parts: bool = False):
    p = woman(age=30, stance="drawn")
    return replace(p, appearance=replace(p.appearance, **PARTS)) if parts else p


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
    assert len(set(all_choices)) == len(all_choices) == 512


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
    p = load_persona(app.store, 101)
    spec = W.build_words_prompt(p, s)
    assert (W.SWIM_FRAMING, 0) in spec.phrases
    assert spec.negative == negative_for(p, snap_negative(True, framing == "mirror_selfie"))
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


@pytest.mark.parametrize("activity,setting,category", [
    ("hiking", "trail", "outdoors"), ("gardening", "garden", "outdoors"),
    ("cooking", "kitchen", "casual"), ("repairing", "workbench", "casual"),
    ("exercise", "gym", "gym"), ("stretching", "gym", "gym"),
    # the gym pairing (scene_choices.pairing_codes): gym wear only at the gym
    ("stretching", "park", "outdoors")])
def test_activity_keeps_the_outfit_practical(activity, setting, category):
    assert enum_category({**PICKS, "activity": activity, "setting": setting}) == category


def test_lust_gym_wear_stays_at_the_gym():
    gym = {**LUST_PICKS, "activity": "stretching", "outfit": "sports top and leggings"}
    assert enum_category({**gym, "setting": "the gym"}) == "gym"
    assert enum_category({**gym, "setting": "a park in summer"}) == "outdoors"


# -- the catalogue: clothed by construction (safe.py) ---------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES)
def test_every_template_is_complete_short_enough_and_free_of_the_audit_triggers(template):
    assert not AUDIT_TRIGGERS.search(template), template
    for colour in LUST_COLOURS:
        phrase = composed(template, colour)
        assert safe.is_complete(phrase), phrase
        assert len(phrase.split()) <= safe.WEAR_MAX_WORDS, phrase
        # so the required clothing phrase is the template itself, never the default garments
        for framing in ("upper_body", "three_quarter", "head_and_shoulders"):
            assert safe.wearing(phrase, gender="female", framing=framing) == f"wearing {phrase}"


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_short_form_keeps_the_garments_colour_and_screens(template):
    for colour in LUST_COLOURS:
        phrase = composed(template, colour)
        assert template_of(phrase) == template
        forms = outfit_forms(phrase)
        assert forms[0] == phrase and len(forms) == len(set(forms))
        for form in forms:
            assert safe.is_complete(form) and colour in form, (phrase, form)
            assert set(form.split()) - {"a", "an"} <= set(phrase.split()), (phrase, form)
            assert not _text_codes("outfit", form, ident=IDENT, names=[]), form
            assert not AUDIT_TRIGGERS.search(form)


def test_short_forms_name_the_same_garments():
    phrase = "a cobalt blue cap-sleeve ribbed top and wide-leg cotton trousers"
    assert short_outfit(phrase) == "a cobalt blue ribbed top and cotton trousers"
    assert short_outfit(phrase, minimal=True) == "a cobalt blue top and trousers"
    dress = "an emerald satin sweetheart-neckline knee-length fit-and-flare dress"
    assert short_outfit(dress) == "an emerald knee-length fit-and-flare dress"
    assert short_outfit(dress, minimal=True) == "an emerald knee-length dress"
    jacket = "a navy cropped field jacket over a tee and walking shorts"
    assert short_outfit(jacket, minimal=True) == "a navy jacket over a tee and shorts"
    # only a catalogue outfit has short forms: swimwear and the lists' own stay whole
    assert outfit_forms("a red string bikini") == ["a red string bikini"]
    assert outfit_forms(OUTFITS["outdoors"]) == [OUTFITS["outdoors"]]


# -- the prompt: one clothing phrase, the setting required, the expression's room -----------------

def _styled_scene(template: str, framing: str = "upper_body"):
    base = replace(scene_from(compose_choices({**PICKS, "framing": framing})), audience="him")
    return replace(base, outfit=composed(template), expression="a knowing smile",
                   romance_style="confident")


@pytest.mark.parametrize("parts", [False, True])
def test_every_template_fits_with_one_clothing_phrase_and_the_setting_required(parts):
    p, fit = drawn(parts), _worker_fit()
    for framing in ("upper_body", "three_quarter", "head_and_shoulders"):
        for template in TEMPLATES:
            s = _styled_scene(template, framing)
            spec = build_scene_prompt(p, s)
            assert not over_fit(spec), (framing, template)
            worn = [(t, pr) for t, pr in spec.phrases if t.startswith("wearing ")]
            assert len(worn) == 1 and worn[0][1] == 0, worn
            assert worn[0][0][len("wearing "):] in outfit_forms(s.outfit)
            assert (s.setting, 0) in spec.phrases
            assert (s.expression, 1) in spec.phrases
            assert spec.negative == negative_for(p, SNAP_NEGATIVE)
            prompt, _ = fit(spec.phrases, estimate, 75)
            assert "30-year-old" in prompt and "hair" in prompt and "skin" in prompt


def test_the_whole_outfit_is_said_when_it_costs_nothing():
    s = _styled_scene("{colour} cotton shirt and jeans")
    spec = build_scene_prompt(woman(age=30, stance="drawn"), s)
    assert (f"wearing {s.outfit}", 0) in spec.phrases


def test_a_lust_scene_keeps_one_joined_phrase_in_one_of_its_forms(app):
    enable(app, stance="lust", dial=10)
    app.llm.answers = [LUST_PICKS]
    s = run(choose_scene(app, 101, "a photo please", "him"))
    spec = build_scene_prompt(load_persona(app.store, 101), s)
    worn = [t for t, pr in spec.phrases if t.startswith("wearing ")]
    assert len(worn) == 1
    assert any(worn[0] == f"wearing {form}, {s.setting}" for form in outfit_forms(s.outfit))


@_TOKENIZERS
@pytest.mark.parametrize("parts", [False, True])
def test_on_the_real_tokenizer_the_setting_always_and_the_expression_nearly_always_show(parts):
    """The worker's own fit and count (RealVisXL's tokenizers) over all 512 templates in the
    longest colour, on the review's fixture (a waist-up dog walk in the park): the setting is
    required; the expression is kept unless her required set leaves it no room."""
    p, fit = drawn(parts), _worker_fit()
    kept = {"setting": 0, "expression": 0}
    for template in TEMPLATES:
        s = _styled_scene(template)
        prompt, _ = fit(build_scene_prompt(p, s).phrases, clip_bpe.count, 75)
        kept["setting"] += s.setting in prompt
        kept["expression"] += s.expression in prompt
    assert kept["setting"] == len(TEMPLATES)
    assert kept["expression"] >= (len(TEMPLATES) if not parts else 500), kept


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
    assert public.romance_style == "" and public.outfit == OUTFITS["outdoors"]


def test_words_request_retains_place_light_prop_framing_and_styled_clothing(app):
    enable(app)
    s = words(app, {"place": "kitchen at home", "wearing": "casual"})
    assert s["romance_style"] == "playful" and s["recipe"] == W.WORDS_RECIPE
    assert s["outfit"] in s["shows"]
    assert s["words"]["lighting"] == "fluorescent"
    assert s["words"]["prop"] == "paper coffee cup"
    p = load_persona(app.store, 101)
    spec = W.build_words_prompt(p, s)
    prompt, _ = _worker_fit()(spec.phrases, estimate, 75)
    assert "kitchen at home" in prompt and s["expression"] in prompt
    worn = [t for t, pr in spec.phrases if t.startswith("wearing ")]
    assert len(worn) == 1 and worn[0][len("wearing "):] in outfit_forms(s["outfit"])
    assert worn[0] in prompt
    # her expression is the first optional phrase after what the owner set her to wear, before
    # the moment
    assert (s["expression"], 1) in spec.phrases
    assert spec.phrases.index((s["expression"], 1)) < spec.phrases.index((s["activity"], 1))
    assert spec.negative == negative_for(p, snap_negative(
        False, s["words"]["framing"] == "mirror_selfie"))


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


# -- the place pairings, for every styled woman (review of #364, P1-3) ----------------------------

GYM_POOL = frozenset(CLOTHED_OUTFITS["gym"])
STYLED_POOLS = frozenset(t for pool in CLOTHED_OUTFITS.values() for t in pool)


@pytest.mark.parametrize("stance", ["fond", "drawn", "lust"])
@pytest.mark.parametrize("place", ["bedroom at home", "living room sofa", "bed with a book"])
def test_no_styled_woman_wears_gym_clothes_indoors(app, stance, place):
    enable(app, stance=stance, dial=10)
    s = words(app, {"place": place, "wearing": "gym", "framing": "handheld_selfie"})
    if place == "bed with a book" and stance == "lust":  # her place is never a bed (screened)
        assert s["setting"] != place
    assert s["words"]["wearing"] == "casual"
    assert "wearing:indoor_pairing" in s["words"]["fallbacks"]
    assert template_of(s["outfit"]) not in GYM_POOL


@pytest.mark.parametrize("stance", ["fond", "drawn"])
@pytest.mark.parametrize("wearing", ["gym", "going_out", "pyjamas", "casual"])
@pytest.mark.parametrize("place", ["bedroom at home", "on my bed", "bed with a book"])
def test_no_styled_outfit_on_a_bed(app, stance, wearing, place):
    enable(app, stance=stance, dial=10)
    s = words(app, {"place": place, "wearing": wearing, "framing": "handheld_selfie"})
    assert W.BED_RE.search(s["setting"])
    assert "romance_style" not in s and not s["expression"]
    assert template_of(s["outfit"]) not in STYLED_POOLS


def test_a_styled_sofa_scene_takes_the_casual_wardrobe_not_the_gym_one(app):
    enable(app, stance="fond", dial=10)
    s = words(app, {"place": "living room sofa", "wearing": "gym", "framing": "handheld_selfie"})
    assert s["romance_style"] == "warm"
    assert template_of(s["outfit"]) in CLOTHED_OUTFITS["casual"]


@pytest.mark.parametrize("stance", ["fond", "drawn", "lust"])
def test_restyle_never_styles_a_bed_and_makes_indoor_gym_wear_casual(stance):
    p = woman(age=30, stance=stance)
    for setting in ("on her bed", "in her bedroom", "propped up on pillows"):
        s = original(setting=setting)
        for category in ("casual", "going_out", "warm", "pyjamas", "gym"):
            assert styled(p, s, category=category) is s
    sofa = original(setting="on her living room sofa")
    for seed in range(40):
        got = styled(p, sofa, category="gym", seed=seed)
        assert template_of(got.outfit) not in GYM_POOL and got.romance_style
    # her wardrobe would keep the scene's own clothes, which the indoors refuses: unstyled
    tagged = replace(p, appearance=replace(p.appearance, wardrobe=("sporty",)))
    assert styled(tagged, sofa, category="gym") is sofa


def test_a_stored_styled_scene_on_a_bed_is_held():
    p = woman(age=30, stance="drawn")
    s = styled(p)
    assert scene_renderable(p, s) is None
    assert scene_renderable(p, replace(s, setting="in her bedroom")) == "scene.styling: held"
    assert scene_renderable(p, replace(s, activity="reading in bed")) == "scene.styling: held"


# -- a selfie takes no body pose (safe.pose) -------------------------------------------------------

@pytest.mark.parametrize("kind", ["selfie", "mirror_selfie"])
@pytest.mark.parametrize("tone", ["warm", "playful", "confident"])
def test_a_styled_selfie_only_looks_never_turns(kind, tone):
    s = original(kind=kind, activity="")
    for framing in (None, "handheld_selfie", "mirror_selfie"):
        assert set(pose_choices(s, tone, framing)) <= {"direct", "tilt"}


def test_a_timer_photo_or_snapshot_may_turn():
    s = original(activity="")
    assert "angled" in pose_choices(s, "confident")
    assert "glance" in pose_choices(s, "confident", "timer")
    assert "glance" not in pose_choices(s, "confident", "mirror_selfie")
    assert pose_choices(original(), "confident") == ("",)  # an activity keeps its own pose


def test_the_softer_private_instruction_replaces_the_sentence_it_names(tmp_path):
    # a silent no-op if the sentence were ever reworded
    assert "Fully clothed, nothing revealing or sexual," in W.PROMPT
    kw = dict(human=IDENT, name="Ana", card="", facts="", looks="", block=None)
    plain = W.system(tmp_path, **kw)
    private = W.system(tmp_path, private_style=True, **kw)
    assert "nothing revealing or sexual" in plain and "nothing revealing" not in private
    assert "no nudity or sexual acts" in private
