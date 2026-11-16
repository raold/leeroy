"""A requested photo read from his words ([photos] scene_from_words): the structured live call,
each free-text screen and its own fallback, the outfit resolution and the prompt at the worker's
fit. FakeLLM-style doubles only; no model, GPU or live data. Every person and word is invented."""
import json
import logging
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.life.day import Block
from leeroy_mind.llm.client import GpuPaused, LLMError
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.personas.io import load_persona
from leeroy_mind.selfies import words_scene as W
from leeroy_mind.selfies.kind import VARIED_LOOKS
from leeroy_mind.selfies.prompt import FIT_MAX, over_fit
from leeroy_mind.selfies.request_scene import RequestSceneError, choose_words_scene
from leeroy_mind.selfies.scene import SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, scene_from
from leeroy_mind.selfies.scene_choices import OUTFITS, RECIPE
from leeroy_mind.selfies.tokens import estimate
from nightly.conftest import IDENT
from selfies import clip_bpe
from selfies.test_request_scene import app, save  # noqa: F401  (the request scene's fixture)
from selfies.test_scene_choices import PICKS
from selfies.test_selfie_prompt import _worker_fit, woman

REPLY = {"place": "supply cupboard at work", "framing": "mirror_selfie",
         "wearing": "work_uniform", "moment": "slipped out for a minute", "time": "now",
         "lighting": "fluorescent", "prop": "paper coffee cup"}
HIS = "sneak off and send me a pic from the supply cupboard"
HIVIS = "a hi-vis vest over a work t-shirt"  # the test cast's job: warehouse supervisor
LUST_PICKS = {**PICKS, "setting": "her living room couch", "activity": "relaxing", "prop": "none",
              "outfit": "satin midi dress", "colour": "navy", "expression": "laughing"}


def words(app, reply=None, **kw):
    app.llm.answers = [dict(REPLY, **(reply or {}))] + app.llm.answers[1:]
    return run(choose_words_scene(app, 101, kw.pop("his", HIS), kw.pop("audience", "him"),
                                  request=kw.pop("request", 7), **kw))


def lusty(app, **over):
    p = load_persona(app.store, 101)
    p = replace(p, identity=replace(p.identity, player_gender="female", age=30),
                romance=replace(p.romance, stance="lust"), **over)
    save(app, p)
    return p


def test_one_structured_live_call_through_the_queue_without_thinking(app):
    scene = words(app, hour=10)
    assert app.events == ["queued", "admitted", "chat"]
    assert app.scheduler.priorities == [Priority.REPLY_HUMAN]
    model, messages, options = app.llm.calls[0]
    assert model == "invented-live" and options["think"] is False
    assert options["format"] == W.SCHEMA and options["format"]["title"] == "scene_words"
    assert options["options"]["num_ctx"] == 4096 and options["options"]["num_predict"] == 320
    assert "stop" not in options["options"]
    assert HIS in messages[-1]["content"]
    assert scene["recipe"] == W.WORDS_RECIPE and scene["audience"] == "him"
    s = scene_from(scene)
    assert (s.setting, s.outfit, s.activity, s.kind) == (
        "supply cupboard at work", HIVIS, "slipped out for a minute", "mirror_selfie")
    assert scene["words"] == {"framing": "mirror_selfie", "wearing": "work_uniform",
                              "time": "morning", "lighting": "fluorescent",
                              "prop": "paper coffee cup", "fallbacks": []}
    assert scene["shows"] == ("a mirror selfie, supply cupboard at work, wearing " + HIVIS
                              + ", slipped out for a minute, with paper coffee cup.")


def test_the_day_plan_block_shapes_her_prompt_and_as_now(app):
    scene = words(app, {"wearing": "as_now"}, block=Block.WORK)
    assert "Right now they are at work." in app.llm.calls[0][1][0]["content"]
    assert scene["outfit"] == HIVIS and scene["words"]["wearing"] == "work_uniform"


def test_as_now_with_no_day_plan_is_what_she_wears_at_the_computer(app):
    scene = words(app, {"wearing": "as_now"})
    assert scene["outfit"] == "an old hoodie, fluffy socks and a mug of tea"
    assert scene["words"]["wearing"] == "as_now"


OTHER = "Belra"  # another test cast member


@pytest.mark.parametrize("place,code", [
    (f"{OTHER} desk at work", "member_name"),
    ("desk 42 at work", "chars"),
    ("desk (masterpiece:1.4)", "chars"),
    ("supply cupboard, best quality", "chars"),
    ("the quiet corner of the big supply cupboard at work", "long"),
    ("shower block at work", "undress"),
    ("office with colleagues", "other_person"),
    ("cupboard behind my manager", "other_person"),
    ("vintage camera shop", "era"),
    ("Starbucks corner", "name"),
    ("", "empty"),
])
def test_a_refused_place_falls_back_without_a_second_model_call(app, place, code):
    app.llm.answers = [dict(REPLY, place=place), PICKS]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert len(app.llm.calls) == 1  # his words' reading only: the fallback asks no model
    assert scene["setting"] == "in a living room" and scene["recipe"] == W.WORDS_RECIPE
    assert scene["words"]["fallbacks"][:2] == [f"place:{code}", "place:everyday"]
    assert place not in json.dumps(scene) or not place
    assert scene["outfit"] == HIVIS  # the other fields keep his words' reading


@pytest.mark.parametrize("his,place", [
    ("sneak off from work and send me one from the staff toilets", W.WORK_BATHROOM),
    ("on your break? send me a pic", W.WORK_BREAK),
    ("send me one from your kitchen", "in a home kitchen"),
    ("go on, one from the garden", "in a small garden"),
])
def test_the_place_fallback_follows_his_words(app, his, place):
    app.llm.answers = [dict(REPLY, place="staff toilets")]
    scene = run(choose_words_scene(app, 101, his, "him", request=7))
    assert len(app.llm.calls) == 1 and scene["setting"] == place
    assert "place:toilet" in scene["words"]["fallbacks"]


def test_her_own_names_in_the_place_are_refused(app):
    p = load_persona(app.store, 101)
    for place, code in ((f"{p.character_name.lower()} corner of the cupboard", "place:her_name"),
                        (f"{p.character_name} corner of the cupboard", "place:name"),
                        (f"{p.identity.real_name.split()[0]} corner of the cupboard", "place:name")):
        app.llm.answers = [dict(REPLY, place=place)]
        scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
        assert code in scene["words"]["fallbacks"] and scene["setting"] == "in a living room"


@pytest.mark.parametrize("field,text,code", [
    ("moment", "topless and soaked", "explicit"),
    ("moment", "hiding from my coworkers", "other_person"),
    ("moment", "quick 2 second one", "chars"),
    ("prop", "my sunglasses", "looks"),
    ("prop", "mug, plate", "chars"),
])
def test_a_refused_moment_or_prop_is_none_alone(app, field, text, code):
    scene = words(app, {field: text})
    assert len(app.llm.calls) == 1
    assert f"{field}:{code}" in scene["words"]["fallbacks"]
    assert text not in json.dumps(scene)
    assert scene["setting"] == "supply cupboard at work"
    if field == "moment":
        assert scene["activity"] == "" and scene["words"]["prop"] == "paper coffee cup"
    else:
        assert scene["words"]["prop"] is None and scene["activity"] == "slipped out for a minute"


def test_a_null_prop_is_no_prop_and_no_fallback(app):
    scene = words(app, {"prop": None})
    assert scene["words"]["prop"] is None and scene["words"]["fallbacks"] == []
    assert "with" not in scene["shows"]


@pytest.mark.parametrize("wearing,outfit,code", [
    ("swimwear", OUTFITS["casual"], "wearing:swimwear_plain"),
    ("pyjamas", OUTFITS["warm"], None),
    ("gym", OUTFITS["exercise"], None),
    ("going_out", OUTFITS["outdoors"], None),
    ("casual", OUTFITS["casual"], None),
])
def test_the_plain_outfits_come_from_the_vetted_lists(app, wearing, outfit, code):
    scene = words(app, {"wearing": wearing})
    assert scene["outfit"] == outfit
    assert (code in scene["words"]["fallbacks"]) if code else scene["words"]["fallbacks"] == []
    assert "pyjama" not in scene["outfit"] + scene["shows"]


def test_no_job_to_go_to_means_casual(app):
    p = load_persona(app.store, 101)
    save(app, replace(p, real_life=replace(p.real_life, job="stay-at-home parent",
                                           rhythm="home all day, raids after dinner")))
    scene = words(app)
    assert scene["outfit"] == OUTFITS["casual"]
    assert "wearing:no_work" in scene["words"]["fallbacks"]


@pytest.mark.parametrize("job,uniform", [("night nurse on a children's ward", "medical scrubs"),
                                         ("barista in a station cafe", "a black work apron over a black t-shirt"),
                                         ("claims handler", W.OFFICE_WEAR)])
def test_work_uniform_is_read_from_her_job(job, uniform):
    p = woman(age=30)
    assert W.uniform(replace(p, real_life=replace(p.real_life, job=job))) == uniform


def test_lust_swimwear_only_at_the_water_handheld_and_framed_waist_up(app):
    p = lusty(app)
    swim = words(app, {"wearing": "swimwear", "place": "sunny beach", "framing": "handheld_selfie"})
    assert swim["style"] == "lust" and swim["audience"] == "him"
    assert any(word in swim["outfit"] for word in ("bikini", "swimsuit"))
    spec = W.build_words_prompt(load_persona(app.store, 101), swim)
    assert (W.SWIM_FRAMING, 0) in spec.phrases and spec.negative == SNAP_NEGATIVE_LUST
    away = words(app, {"wearing": "swimwear", "place": "supply cupboard at work",
                       "framing": "handheld_selfie"})
    assert "wearing:swimwear_pairing" in away["words"]["fallbacks"]
    behind = words(app, {"wearing": "swimwear", "place": "sunny beach", "framing": W.BEHIND})
    assert "wearing:swimwear_pairing" in behind["words"]["fallbacks"]
    assert p.appearance is not None and VARIED_LOOKS["swim"][0]


def test_lust_gym_wear_on_a_sofa_is_casual_and_her_bed_is_refused(app):
    lusty(app)
    sofa = words(app, {"wearing": "gym", "place": "sofa at home"})
    assert "wearing:indoor_pairing" in sofa["words"]["fallbacks"]
    app.llm.answers = [dict(REPLY, place="her bedroom at home")]
    bed = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert bed["setting"] == "her living room couch" and bed["style"] == "lust"
    assert "place:bed" in bed["words"]["fallbacks"] and len(app.llm.calls) == 2  # one a request


def test_the_lust_outfit_draw_is_seeded_by_the_request(app):
    lusty(app)
    first = words(app, {"wearing": "going_out"}, request=11)["outfit"]
    assert words(app, {"wearing": "going_out"}, request=11)["outfit"] == first


def test_a_reply_off_the_schema_twice_takes_todays_enum_scene(app, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    app.llm.answers = [{**REPLY, "framing": "drone shot"}, "not json", PICKS]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert len(app.llm.calls) == 2  # chat_json's own retry; the enum path asks no model
    assert scene["recipe"] == RECIPE and "words" not in scene
    assert scene["setting"] == "in a living room"
    assert any(r.leeroy_fields == {"request": 7, "guid": 101, "source": "enum", "reason": "schema"}
               for r in caplog.records if getattr(r, "leeroy_event", "") == "selfie_request_scene")


def test_a_model_error_takes_the_enum_scene_without_asking_again(app):
    app.llm.answers = [LLMError("unavailable")]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert len(app.llm.calls) == 1 and scene["setting"] == "in a living room"


def test_a_pause_propagates_to_the_caller(app):
    app.llm.answers = [GpuPaused("paused")]
    with pytest.raises(GpuPaused):
        run(choose_words_scene(app, 101, HIS, "him", request=7))


def test_identity_checks_come_before_any_call(app):
    p = load_persona(app.store, 101)
    save(app, replace(p, identity=replace(p.identity, age=24)))
    with pytest.raises(RequestSceneError):
        run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert app.llm.calls == []


def test_logs_carry_ids_and_codes_never_his_words_or_the_free_text(app, caplog):
    caplog.set_level(logging.DEBUG, logger="leeroy_mind")
    his = "PRIVATEASKWORDS please, from the supply cupboard"
    words(app, {"place": "supply cupboard at work", "moment": "slipped out for a minute",
                "prop": "paper coffee cup"}, his=his)
    app.llm.answers = [dict(REPLY, place="shower block at work")]
    run(choose_words_scene(app, 101, his, "him", request=8))
    app.llm.answers = [dict(REPLY, moment="looking young and sweet")]
    run(choose_words_scene(app, 101, his, "him", request=9))
    rows = [r for r in caplog.records if getattr(r, "leeroy_event", "") == "selfie_request_scene"]
    assert len(rows) == 3 and rows[0].leeroy_fields["source"] == "words"
    assert rows[0].leeroy_fields["place"] == "words" and rows[1].leeroy_fields["place"] == "everyday"
    assert rows[2].leeroy_fields == {"request": 9, "guid": 101, "source": "enum", "reason": "age"}
    refused = [r.leeroy_fields for r in caplog.records
               if getattr(r, "leeroy_event", "") == "scene_words_refused"]
    assert refused == [{"request": 9, "guid": 101, "reason": "age"}]
    text = "\n".join(r.getMessage() + json.dumps(getattr(r, "leeroy_fields", {}), default=str)
                     for r in caplog.records)
    for secret in ("PRIVATEASKWORDS", "supply", "cupboard", "slipped", "coffee", "shower",
                   "young", "sweet", "hi-vis"):
        assert secret not in text


def _scene(p, **over):
    w = W.Words(place="supply cupboard at work", framing="mirror_selfie", wearing="work_uniform",
                outfit=HIVIS, moment="slipped out for a minute", time="morning",
                lighting="fluorescent", prop="paper coffee cup", fallbacks=())
    return W.scene_of(p, replace(w, **over), audience="him")


def test_wearing_place_and_framing_are_required_and_the_rest_keep_their_priorities():
    p = woman(age=30)
    spec = W.build_words_prompt(p, _scene(p))
    required = [t for t, pr in spec.phrases if pr == 0]
    assert required[0] == "mirror selfie"
    assert f"wearing {HIVIS}" in required and "supply cupboard at work" in required
    assert ("slipped out for a minute", 1) in spec.phrases
    assert ("with paper coffee cup", 2) in spec.phrases
    assert ("harsh fluorescent light", 2) in spec.phrases and ("early morning", 3) in spec.phrases
    assert ("looking at the camera", 3) in spec.phrases
    assert ("recent smartphone photo, natural skin texture, unretouched", 1) in spec.phrases
    assert spec.negative == SNAP_NEGATIVE


def test_from_behind_turns_away_and_takes_the_face_lightly():
    p = woman(age=30)
    scene = _scene(p, framing=W.BEHIND)
    spec = W.build_words_prompt(p, scene)
    assert spec.phrases[0] == ("from behind, looking over her shoulder", 0)
    assert all(t != "looking at the camera" for t, _ in spec.phrases)
    assert W.face_scale(scene, 0.6) == 0.3 and W.face_scale(_scene(p), 0.6) == 0.6
    assert W.face_scale({"recipe": RECIPE}, 0.6) == 0.6


def _big(p):
    """The fullest body the prompt says: voluptuous, the heaviest size band, long looks."""
    a = replace(p.appearance, build="voluptuous", weight_kg=118, height_cm=160,
                hair_style="long glossy waves over one shoulder",
                skin_tone="warm golden brown, freckled")
    return replace(p, appearance=a)


def _curvy(p):
    a = replace(p.appearance, build="curvy", weight_kg=74, height_cm=165, bust="large",
                hair_style="long glossy waves over one shoulder",
                skin_tone="warm golden brown, freckled")
    return replace(p, appearance=a)


def test_priority_0_survives_the_workers_fit_with_long_looks_and_a_full_place():
    fit = _worker_fit()
    p = _curvy(woman(age=30))
    place = "quiet corner of the supply cupboard at work"  # eight words, the most allowed
    spec = W.build_words_prompt(p, _scene(p, place=place, framing=W.BEHIND))
    assert not over_fit(spec)
    kept, dropped = fit(spec.phrases, estimate, FIT_MAX)
    for phrase in (place, f"wearing {HIVIS}"):
        assert phrase in kept and phrase not in dropped
    assert kept.startswith("from behind")


@pytest.mark.skipif(clip_bpe.tokenizers() is None,
                    reason="RealVisXL tokenizer files are not on this machine")
def test_the_required_set_fits_the_real_tokenizers_too():
    p = _curvy(woman(age=30))
    spec = W.build_words_prompt(p, _scene(p, place="quiet corner of the supply cupboard at work",
                                          framing=W.BEHIND))
    required = [t for t, pr in spec.phrases if pr == 0]
    assert sum(clip_bpe.count(t) for t in required) + len(required) - 1 <= FIT_MAX


def test_a_place_the_fit_cannot_carry_takes_the_enum_setting(app):
    p = load_persona(app.store, 101)
    p = _big(replace(p, identity=replace(p.identity, player_gender="female", age=30)))
    save(app, p)
    long = "administrative refurbishment accommodation headquarters photocopier"
    scene = W.scene_of(p, W.resolve(p, dict(REPLY, place=long), W.Screened(long, None, None, ()),
                                    place=long, block=None, hour=10, seed=7), audience="him")
    assert over_fit(W.build_words_prompt(p, scene))  # the precondition of the fallback
    app.llm.answers = [dict(REPLY, place=long, wearing="casual")]
    got = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert "place:prompt_fit" in got["words"]["fallbacks"] and got["setting"] == "in a living room"
    assert len(app.llm.calls) == 1
    assert not over_fit(W.build_words_prompt(load_persona(app.store, 101), got))


def test_his_names_never_reach_the_scene(app):
    app.engine.human = replace(IDENT, nickname="Cupboard")
    app.llm.answers = [dict(REPLY)]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert "place:member_name" in scene["words"]["fallbacks"]


# The review of #339's hostile reads of his asks (a model copying them): each is refused, the
# whole reply for an age word or a child, the field alone otherwise; none ever reaches a render.
AGE_ASKS = [
    ("moment", "looking young and cute"), ("moment", "young"), ("moment", "looking younger"),
    ("moment", "youngest one here"), ("moment", "tiny and cute"), ("moment", "little and cute"),
    ("moment", "petite and cute"), ("moment", "small girl energy"), ("moment", "naive"),
    ("moment", "virgin"), ("moment", "innocent"), ("moment", "after class"),
    ("moment", "in uniform skirt"), ("moment", "just turned eighteen"), ("place", "classroom"),
    ("place", "college campus"), ("place", "school staff room"), ("place", "dorm room"),
    ("prop", "homework"), ("prop", "lollipop"), ("place", "kids corner at home"),
    ("prop", "baby bottle"),
]


@pytest.mark.parametrize("field,text", AGE_ASKS)
def test_an_age_word_or_a_child_refuses_all_his_free_text(app, caplog, field, text):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    app.llm.answers = [dict(REPLY, **{field: text})]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert len(app.llm.calls) == 1 and scene["recipe"] == RECIPE and "words" not in scene
    assert scene["setting"] == "in a living room"  # the enum path's everyday choice
    for kept in (text, "supply cupboard", "slipped out", "paper coffee"):
        assert kept not in json.dumps(scene)
    reasons = [r.leeroy_fields["reason"] for r in caplog.records
               if getattr(r, "leeroy_event", "") == "scene_words_refused"]
    assert reasons in (["age"], ["minor"])


HOSTILE = [
    ("moment", "shirt half open"), ("moment", "blouse open"), ("moment", "open shirt"),
    ("moment", "top undone"), ("moment", "buttons undone"), ("moment", "pants down"),
    ("moment", "skirt hiked up"), ("moment", "skirt riding up"), ("moment", "wearing his shirt"),
    ("moment", "scrubs top undone"), ("moment", "tits out"), ("moment", "ass out"),
    ("moment", "big ass"), ("moment", "showing my butt"), ("moment", "showing off curves"),
    ("moment", "showing legs"), ("moment", "on her knees"), ("moment", "kneeling"),
    ("moment", "biting lip"), ("moment", "sitting on the toilet"), ("moment", "peeing"),
    ("moment", "pretending to pee"), ("moment", "two women kissing"),
    ("moment", "two guys behind"), ("moment", "kissing the mirror"), ("moment", "with someone"),
    ("moment", "posing like amber heard"), ("moment", "drake style"),
    ("moment", "cleavage on show"), ("moment", "thong peeking"),
    ("place", "bathroom, plunging neckline"), ("place", "bathroom, low cut top, tight"),
    ("place", "bathroom, thick curvy booty"), ("place", "bathroom, low angle from below"),
    ("place", "bathroom, masterpiece, best quality"), ("place", "work toilets"),
    ("place", "bra shop changing area"), ("prop", "christian bale poster"),
    ("prop", "poster of my idol"), ("prop", "her bra"),
]


@pytest.mark.parametrize("field,text", HOSTILE)
def test_hostile_free_text_falls_back_or_drops_and_never_renders(app, field, text):
    app.llm.answers = [dict(REPLY, **{field: text})]
    scene = run(choose_words_scene(app, 101, HIS, "him", request=7))
    assert len(app.llm.calls) == 1
    assert any(c.startswith(f"{field}:") for c in scene["words"]["fallbacks"])
    spec = W.build_words_prompt(load_persona(app.store, 101), scene)
    rendered = " | ".join(t for t, _ in spec.phrases)
    assert text not in json.dumps(scene) and text not in rendered


FALSE_REFUSALS = [
    ("place", "staff loo"), ("place", "work loos"), ("place", "loo at work"), ("place", "the loos"),
    ("place", "restroom"), ("place", "office restroom"), ("place", "women's restroom"),
    ("place", "staffroom"), ("place", "shop backroom"), ("place", "en suite"), ("place", "ensuite"),
    ("place", "café at work"), ("place", "nurses\u2019 station"),
    ("moment", "quick selfie"), ("moment", "sneaky selfie"), ("moment", "quick pic"),
    ("moment", "cheeky one"), ("moment", "hungover"), ("moment", "between patients"),
    ("prop", "latte"), ("prop", "lanyard"), ("prop", "takeaway coffee"), ("prop", "vape"),
    ("prop", "headset"), ("prop", "teddy bear"),
]
PASS_TOO = [
    ("place", "work bathroom"), ("place", "ladies' room"), ("place", "bathroom stall"),
    ("place", "break room"), ("place", "behind the bar"), ("place", "walk-in fridge"),
    ("place", "front seat of the van"), ("place", "hotel bathroom"), ("place", "gym mirror"),
    ("moment", "sneaked away, hurried"), ("moment", "on my lunch break"),
    ("moment", "blowing a kiss"), ("prop", "handcuffs"), ("prop", "a whip"), ("prop", "condom"),
]


@pytest.mark.parametrize("field,text", FALSE_REFUSALS + PASS_TOO)
def test_ordinary_free_text_passes(app, field, text):
    scene = words(app, {field: text})
    assert scene["words"]["fallbacks"] == [] and len(app.llm.calls) == 1
    folded = W.fold(text)
    got = {"place": scene["setting"], "moment": scene["activity"],
           "prop": scene["words"]["prop"]}[field]
    assert got == folded
    phrases = [t for t, _ in W.build_words_prompt(load_persona(app.store, 101), scene).phrases]
    assert (f"with {folded}" if field == "prop" else folded) in phrases


def test_names_in_finds_a_member_named_since_in_any_free_text():
    p = woman(age=30)
    scene = _scene(p)
    assert not W.names_in(scene, ["Belra", "Ownerly"])
    assert W.names_in(scene, ["Cupboard"]) and W.names_in(scene, ["coffee"])
    assert W.names_in(scene, ["minute"])
