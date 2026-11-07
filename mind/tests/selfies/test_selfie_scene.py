"""Situational photos (P4 Task 6): what a photo shows, its screen and its prompts. A lust woman
(style.style_of) picks setting, outfit, colour and expression from the profile photo's vetted
lists (selfies/kind.py) and her outfit and setting render as one priority-0 phrase (controller
ruling R2); everyone else writes the plain scene. Every person and scene here is invented."""
from dataclasses import replace
from string import Formatter

import pytest

from leeroy_mind.nightly.llm import check_schema, night_prompt_names
from leeroy_mind.selfies import kind, scene
from leeroy_mind.selfies.kind import (AGE_RE, GYM_SETTINGS, LUST_COLOURS, LUST_EXPRESSIONS,
                                      LUST_OUTFITS, LUST_SETTINGS, PAIRINGS, SWIM_ONLY_SETTINGS,
                                      WATER_SETTINGS)
from leeroy_mind.selfies.prompt import (LUST_HELD, LUST_NAMED, LUST_NEGATIVE, NEGATIVE,
                                        build_prompt, looks_phrases, names_re, subject_phrase)
from leeroy_mind.selfies.scene import (ABOUT, BIRTHDAY_S, EVERYDAY_DAYS, GUILD_DAYS, LIFE_DAYS,
                                       LOOKS_RE, MINOR_RE, PHASES, SCENE_ASK, SCENE_LUST_NAME,
                                       SCENE_LUST_SETTINGS, SCENE_NAME, SCENE_PROMPT,
                                       SCENE_PROMPT_LUST, SCENE_SCHEMA, SCENE_SCHEMA_LUST,
                                       SNAP_NEGATIVE, SNAP_NEGATIVE_LUST, Scene,
                                       build_scene_prompt, build_tile_prompt, ref_crop,
                                       scene_from, scene_json, scene_renderable, scene_schema,
                                       scene_system, screen_scene, window)
from leeroy_mind.selfies.style import LUST, PLAIN, style_of
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_lust import OLD_REFUSED
from selfies.test_selfie_prompt import LUST_PHOTO, PHOTO, _tokens, _worker_fit, man, woman

SCENE = {"kind": "snapshot", "framing": "upper_body", "setting": "on a wooden pier by the harbour",
         "activity": "throwing a ball for a scruffy terrier",
         "outfit": "a yellow raincoat over a grey jumper", "expression": "laughing",
         "when": "day", "audience": "anyone",
         "shows": "Them on a wooden pier throwing a ball for their dog."}
LUST_SCENE = {"photo": True, "kind": "mirror_selfie", "framing": "upper_body",
              "setting": "a rooftop bar at night", "activity": "raising a glass to the camera",
              "outfit": "low-cut cocktail dress", "colour": "emerald",
              "expression": "a confident smirk", "when": "evening", "audience": "him",
              "shows": "Her at a rooftop bar at night, raising a glass."}
SWIM_SCENE = {**LUST_SCENE, "kind": "snapshot", "setting": "a hotel pool deck",
              "outfit": "halter bikini", "colour": "coral", "activity": "lounging in the sun",
              "when": "day", "shows": "Her lounging on a pool deck in the sun."}
GYM_SCENE = {**LUST_SCENE, "setting": "the gym", "outfit": "sports top and leggings",
             "colour": "teal", "activity": "between two sets"}
FIELDS = {"name": "Arkon", "card": "CARD", "facts": "FACTS", "looks": "LOOKS",
          "about": ABOUT["everyday"]}


def lusty(**over):
    return woman(stance="lust", **over)


def plain_members():
    young = replace(APPEARANCE, face=("a youthful face",))
    return [woman(), woman(stance="fond"), woman(stance="drawn"), lusty(age=20),
            lusty(appearance=young), man(), man(age=40)]


def scode(photo_code: str) -> str:
    """A screen_lust problem as the scene screen reports it."""
    return "scene" + photo_code[len("photo"):]


# -- the plain scene (the brief as written) --------------------------------------------------

@pytest.mark.parametrize("field,text,code", [
    ("activity", "showing off a fresh haircut", "looks"), ("activity", "trying on new glasses", "looks"),
    ("shows", "her after losing weight", "looks"), ("activity", "with his beard shaved off", "looks"),
    ("setting", "at the park with her daughter", "minor"), ("shows", "the kids at the school gate", "minor"),
    ("setting", "a 2009 house party", "era"), ("outfit", "lingerie", "explicit"),
    ("expression", "looking like a famous actress", "likeness")])
def test_the_screen_refuses_looks_changes_minors_era_explicit_and_likeness(field, text, code):
    assert f"scene.{field}: {code}" in screen_scene(woman(), {**SCENE, field: text}, ident=IDENT, member_names=["Testb"])


def test_the_scene_prompt_keeps_the_face_and_puts_the_scene_first():
    spec = build_scene_prompt(woman(age=34, region="UK"), Scene(**{**SCENE, "kind": "selfie", "when": "late"}))
    assert spec.phrases[:3] == (("phone selfie", 0), ("waist-up shot, upper body", 0), ("34-year-old British woman", 0))
    assert spec.phrases[3] == (SCENE["activity"], 1) and spec.phrases[-1] == ("recent smartphone photo, natural skin texture, unretouched", 1)
    assert ("at night, lit by a lamp or a screen", 2) in spec.phrases and spec.negative == SNAP_NEGATIVE


def test_the_scene_prompt_runs_in_the_briefs_order():
    spec = build_scene_prompt(woman(), Scene(**{**SCENE, "kind": "mirror_selfie", "when": "morning"}))
    assert spec.phrases == (
        ("mirror selfie", 0), ("waist-up shot, upper body", 0), ("34-year-old British woman", 0),
        (SCENE["activity"], 1), (SCENE["setting"], 1),
        ("shoulder-length dark brown hair, tucked behind one ear", 1), ("light olive skin", 1),
        ("looking at the camera", 3), ("hazel eyes", 2), ("freckles", 2), ("thin glasses", 2),
        (SCENE["outfit"], 2), ("soft morning light", 2), (SCENE["expression"], 3),
        ("average build", 1),
        ("recent smartphone photo, natural light, natural skin texture, unretouched", 1))
    assert SNAP_NEGATIVE == NEGATIVE + (("another person, crowd, group of people", 1),)


@pytest.mark.parametrize("when,light", [("morning", "soft morning light"), ("day", "daylight"),
                                        ("evening", "warm evening light"), ("any", None)])
def test_the_light_and_the_tail_follow_the_time_of_day(when, light):
    ph = build_scene_prompt(woman(), Scene(**{**SCENE, "when": when})).phrases
    lights = [t for t, pr in ph if t in ("soft morning light", "daylight", "warm evening light",
                                         "at night, lit by a lamp or a screen")]
    assert lights == ([light] if light else [])
    assert ph[-1] == ("recent smartphone photo, natural light, natural skin texture, unretouched", 1)
    assert ("looking at the camera", 3) not in ph                    # a snapshot


def test_lifting_weights_and_a_glass_of_wine_are_not_looks_changes():
    ok = {**SCENE, "activity": "lifting weights, then a glass of wine",
          "shows": "Them with a glass of wine after lifting weights."}
    assert screen_scene(woman(), ok, ident=IDENT, member_names=["Testb"]) == []
    assert screen_scene(woman(), SCENE, ident=IDENT, member_names=["Testb"]) == []
    assert LOOKS_RE.search("lifting weights") is None and LOOKS_RE.search("a glass of wine") is None
    assert MINOR_RE.search("a scruffy terrier") is None
    # the brief's patterns, as written
    assert LOOKS_RE.pattern == r"\b(hair ?cut|new (?:hair|hairstyle|look|glasses|specs)|dyed|dyes?|bleach(?:ed)?|highlights|fringe|bangs|perm|buzz ?cut|shaved (?:head|off)|beard|moustache|mustache|goatee|stubble|eyeglasses|spectacles|wearing glasses|contact lenses|tattoo(?:s|ed)?|piercings?|pierced|weight|slimmer|thinner|makeover|braces|wig|extensions)\b"  # noqa: E501
    assert MINOR_RE.pattern == r"\b(child|children|kids?|kiddo|bab(?:y|ies)|toddlers?|infants?|sons?|daughters?|boys?|girls?|teens?|teenagers?|nieces?|nephews?|grand(?:child|children|son|daughter)s?|schoolkids?|pupils?)\b"  # noqa: E501


def test_the_screen_gives_codes_never_the_text():
    bad = {**SCENE, "setting": "at Quillon's place", "activity": "waving at TESTB",
           "outfit": "as an AI would dress", "shows": "a selfie from the 90s"}
    codes = screen_scene(woman(), bad, ident=IDENT, member_names=["Testb"])
    assert codes == ["scene.setting: real_name", "scene.activity: member_name",
                     "scene.outfit: meta", "scene.shows: era"]
    assert screen_scene(woman(), {**SCENE, "kind": "headshot"}, ident=IDENT,
                        member_names=[]) == ["scene.kind: not on its list"]


def test_no_name_reaches_a_scene_or_tile_prompt():
    p = woman(real_name="Quentina")
    p = replace(p, appearance=replace(p.appearance, at_the_pc="Arkon's old hoodie"))
    s = Scene(**{**SCENE, "setting": "at a bar with Arkon and Quentina",
                 "activity": "waving at quentina", "outfit": "ARKON's scarf",
                 "expression": "grinning at Arkon"})
    for spec in (build_scene_prompt(p, s), build_tile_prompt(p)):
        for text, _ in spec.phrases + spec.negative:
            assert "arkon" not in text.lower() and "quentina" not in text.lower()


def test_the_tile_prompt_wears_what_they_wear_at_the_pc():
    for p in (woman(), lusty()):                              # the guild photo is plain for all
        spec = build_tile_prompt(p)
        assert spec.phrases == (
            ("frame from a video call", 0), ("head and shoulders", 0), subject_phrase(p),
            ("at their desk at home at night, wearing a headset, lit by the monitor", 1),
            *looks_phrases(p, names_re(p)), (APPEARANCE.at_the_pc, 2),
            ("looking at the screen, smiling", 3),
            ("recent photo, natural skin texture, unretouched", 1))
        assert spec.negative == SNAP_NEGATIVE


def test_crops_are_squares_inside_the_photo():
    assert ref_crop("face") == (0.0, 0.0, 1.0, 1.0)
    assert ref_crop("head_and_shoulders") == (0.125, 0.05, 0.875, 0.8)
    assert ref_crop("upper_body") == (0.25, 0.05, 0.75, 0.55)
    for framing in ("face", "head_and_shoulders", "upper_body"):
        x0, y0, x1, y1 = ref_crop(framing)
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
        assert abs((x1 - x0) - (y1 - y0)) < 1e-9


def test_windows_and_phases_per_source():
    assert (LIFE_DAYS, EVERYDAY_DAYS, GUILD_DAYS, BIRTHDAY_S) == (3, 5, 3, 64800.0)
    assert window("birthday", 1000.0) == (1000.0, 65800.0)
    assert window("life", 1000.0) == (1000.0, 1000.0 + 3 * 86400)
    assert window("raid", 1000.0) == (1000.0, 1000.0 + 3 * 86400)
    assert window("everyday", 1000.0) == (1000.0, 1000.0 + 5 * 86400)
    with pytest.raises(ValueError):
        window("rumour", 1000.0)
    assert PHASES == {"morning": ("morning",), "day": ("day",), "evening": ("evening",),
                      "late": ("late", "dead"),
                      "any": ("morning", "day", "evening", "late", "dead")}
    assert set(PHASES) == set(SCENE_SCHEMA["properties"]["when"]["enum"])


def test_the_schema_template_and_ask_are_the_briefs():
    props = SCENE_SCHEMA["properties"]
    assert SCENE_SCHEMA["title"] == "scene" and SCENE_SCHEMA["additionalProperties"] is False
    assert list(props) == ["photo", "kind", "framing", "setting", "activity", "outfit",
                           "expression", "when", "audience", "shows"]
    assert set(SCENE_SCHEMA["required"]) == set(props)
    assert props["photo"] == {"type": "boolean"}
    assert props["kind"]["enum"] == ["selfie", "mirror_selfie", "snapshot"]
    assert props["framing"]["enum"] == ["face", "head_and_shoulders", "upper_body"]
    assert props["audience"]["enum"] == ["him", "anyone"]
    assert {f: props[f]["maxLength"] for f in ("setting", "activity", "outfit", "expression",
                                               "shows")} == {
        "setting": 80, "activity": 80, "outfit": 80, "expression": 40, "shows": 120}
    assert SCENE_ASK == "Choose {name}'s photo." and SCENE_NAME == "scene"
    assert ABOUT == {"life": "Tonight in their life away from the game: {beat}",
                     "birthday": "It is their birthday tomorrow.",
                     "everyday": "An ordinary day for them this week."}
    assert SCENE_PROMPT.startswith(
        "Choose a photo the real person who plays {name} might take in the coming days and "
        "send to a friend from the guild.\n{card}\n{facts}\nHow they look: {looks}\n"
        "What is going on: {about}\nSay whether they would take a photo of it at all.")
    assert SCENE_PROMPT.endswith("Fully clothed, nothing revealing or sexual, no children, never "
                                 "like anyone famous, and no one else's name.")


def test_a_scene_round_trips_through_its_json():
    s = scene_from({"photo": True, **SCENE})
    assert s == Scene(**SCENE) and s.style == PLAIN
    assert scene_json(s) == SCENE                                   # the plain shape as written
    assert scene_from(scene_json(s)) == s
    hot = scene_from(LUST_SCENE)
    assert hot.style == LUST and hot.outfit == "a low-cut emerald cocktail dress"
    assert scene_json(hot) == {**{k: v for k, v in LUST_SCENE.items()
                                  if k not in ("photo", "colour")},
                               "outfit": "a low-cut emerald cocktail dress", "style": LUST}
    assert scene_from(scene_json(hot)) == hot


def test_build_prompt_is_unchanged():
    # the profile photo's prompt, pinned before subject_phrase and looks_phrases were factored out
    hair = ("shoulder-length dark brown hair, tucked behind one ear", 1)
    looks = (hair, ("light olive skin", 1), ("hazel eyes", 2), ("freckles", 2),
             ("thin glasses", 2), ("average build", 1))
    kind_ = (("orange rain bib over a grey hoodie", 2), ("on the deck of a small boat", 2),
             ("squinting into the wind", 3))
    phone = ("recent smartphone photo, natural light, natural skin texture, unretouched", 1)
    subject = ("34-year-old British woman", 0)
    assert build_prompt(woman()).phrases == (
        ("candid photo taken by a friend", 0), ("waist-up shot, upper body", 0), subject, *looks, *kind_, phone)
    assert build_prompt(woman(age=22, photo={**PHOTO, "kind": "selfie",
                                             "framing": "head_and_shoulders"})).phrases == (
        ("phone selfie", 0), ("head and shoulders", 0), ("22-year-old adult British woman", 0),
        ("looking at the camera", 2), *looks, *kind_, phone)
    shaved = man(appearance=replace(APPEARANCE, hair_length="shaved", skin_tone=""),
                 photo={**PHOTO, "kind": "mirror_selfie"})
    assert build_prompt(shaved).phrases == (
        ("mirror selfie", 0), ("waist-up shot, upper body", 0), ("34-year-old British man", 0),
        ("looking at the camera", 2), ("shaved head", 1), *looks[2:], *kind_, phone)
    assert build_prompt(woman(photo={**PHOTO, "kind": "headshot", "framing": "face"})).phrases == (
        ("professional headshot", 0), ("close-up of the face", 0), subject,
        ("looking at the camera", 2), *looks, *kind_,
        ("recent photo, natural skin texture, unretouched", 1))
    assert build_prompt(lusty(photo=LUST_PHOTO), kind_style=LUST).phrases == (
        ("candid photo taken by a friend", 0), ("waist-up shot, upper body", 0), subject, *looks,
        ("wearing a red string bikini, the beach at sunset", 0), ("a teasing grin", 3), phone)
    assert subject_phrase(woman()) == subject and looks_phrases(woman(), None) == list(looks[:5])


# -- the lust scene (ruling R2) --------------------------------------------------------------

def test_the_lust_lists_are_the_profile_photos_own():
    assert scene.LUST_OUTFITS is kind.LUST_OUTFITS and scene.LUST_COLOURS is kind.LUST_COLOURS
    assert scene.LUST_EXPRESSIONS is kind.LUST_EXPRESSIONS
    assert scene.screen_lust is kind.screen_lust and scene.compose_lust is kind.compose_lust
    # a subset of her profile settings: the one that names a crowd goes, she is alone in it
    assert set(SCENE_LUST_SETTINGS) < set(LUST_SETTINGS)
    assert [s for s in LUST_SETTINGS if s not in SCENE_LUST_SETTINGS] == ["a concert crowd"]
    assert set(WATER_SETTINGS + GYM_SETTINGS + SWIM_ONLY_SETTINGS) <= set(SCENE_LUST_SETTINGS)


def test_a_lust_womans_scene_schema_allows_only_the_enum_values():
    assert style_of(lusty()) == LUST and scene_schema(lusty()) is SCENE_SCHEMA_LUST
    props = SCENE_SCHEMA_LUST["properties"]
    assert SCENE_SCHEMA_LUST["title"] == SCENE_LUST_NAME == "scene_lust"
    assert SCENE_SCHEMA_LUST["additionalProperties"] is False
    assert list(props) == ["photo", "kind", "framing", "setting", "activity", "outfit", "colour",
                           "expression", "when", "audience", "shows"]
    assert set(SCENE_SCHEMA_LUST["required"]) == set(props)
    for f in ("photo", "kind", "framing", "activity", "when", "audience", "shows"):
        assert props[f] == SCENE_SCHEMA["properties"][f], f        # the same as everyone's
    assert props["setting"]["enum"] == list(SCENE_LUST_SETTINGS)
    assert props["outfit"]["enum"] == list(LUST_OUTFITS)
    assert props["colour"]["enum"] == list(LUST_COLOURS)
    assert props["expression"]["enum"] == list(LUST_EXPRESSIONS)
    check_schema(SCENE_SCHEMA_LUST, LUST_SCENE)
    for field, value in [("outfit", "a towel"), ("outfit", "a fitted little dress"),
                         ("setting", "her bed"), ("setting", "a concert crowd"),
                         ("expression", "a sleepy pout"), ("colour", "white"),
                         ("colour", "sheer"), ("kind", "headshot"), ("framing", "full_body")]:
        with pytest.raises(ValueError):
            check_schema(SCENE_SCHEMA_LUST, {**LUST_SCENE, field: value})
    with pytest.raises(ValueError):
        check_schema(SCENE_SCHEMA_LUST, {**LUST_SCENE, "note": "anything"})


def test_a_swimwear_pick_away_from_the_water_is_refused():
    def codes(data):
        return screen_scene(lusty(), data, ident=IDENT, member_names=["Testb"])
    assert codes(SWIM_SCENE) == [] and codes(LUST_SCENE) == [] and codes(GYM_SCENE) == []
    assert codes({**SWIM_SCENE, "setting": "a cafe terrace"}) == [scode(PAIRINGS[0])]
    assert scode(PAIRINGS[0]) == "scene.outfit: swimwear away from the water"
    assert codes({**GYM_SCENE, "setting": "a wine bar"}) == [scode(PAIRINGS[1])]
    assert codes({**LUST_SCENE, "setting": "a hot tub on a wooden deck"}) == [scode(PAIRINGS[2])]
    for framing in ("face", "head_and_shoulders"):              # swimwear: upper body only
        assert codes({**SWIM_SCENE, "framing": framing}) == [scode(PAIRINGS[3])]
    assert codes({**LUST_SCENE, "setting": "a concert crowd"}) == ["scene.setting: not on its list"]
    assert codes({**LUST_SCENE, "outfit": "a towel"}) == ["scene.outfit: not on its list"]


def test_the_lust_free_text_meets_the_scene_screen_and_the_age_and_wear_guards():
    def codes(field, text):
        return screen_scene(lusty(), {**LUST_SCENE, field: text}, ident=IDENT,
                            member_names=["Testb"])
    assert "scene.activity: explicit" in codes("activity", "lounging in her pyjamas")   # WEAR_RE
    assert "scene.activity: explicit" in codes("activity", "posing in a bikini")   # EXPLICIT_RE
    assert "scene.shows: explicit" in codes("shows", "Her in a sheer robe.")
    assert "scene.activity: age" in codes("activity", "with her hair in two pigtails")  # AGE_RE
    assert "scene.shows: age" in codes("shows", "Her dressed as a cheerleader.")
    assert "scene.activity: looks" in codes("activity", "showing off a fresh haircut")
    assert "scene.activity: minor" in codes("activity", "walking her daughter home")
    assert "scene.shows: era" in codes("shows", "A 2009 night out.")
    assert "scene.shows: likeness" in codes("shows", "Her looking like a famous singer.")
    assert "scene.activity: member_name" in codes("activity", "toasting Testb")
    assert "scene.activity: real_name" in codes("activity", "toasting Quillon")
    assert "scene.shows: meta" in codes("shows", "As an AI I cannot say.")
    assert all(AGE_RE.search(c) is None for c in codes("activity", "in two pigtails"))


@pytest.mark.parametrize("field", ["setting", "activity", "outfit", "expression", "shows"])
@pytest.mark.parametrize("text,code", [
    ("lounging in her pyjamas", "explicit"), ("a lace camisole", "explicit"),     # WEAR_RE
    ("in fishnets", "explicit"), ("with her hair in two pigtails", "age"),        # AGE_RE
    ("dressed as a cheerleader", "age"), ("a youthful grin", "age")])
def test_a_plain_scene_meets_the_age_and_wear_guards_as_a_profile_kind_does(field, text, code):
    # controller ruling R13: every free-text field, as kind.screen_kind reads a plain kind
    for p in plain_members():
        assert f"scene.{field}: {code}" in screen_scene(
            p, {**SCENE, field: text}, ident=IDENT, member_names=["Testb"])


UNDRESSED = ["just out of the shower", "undressing for the pool", "wrapped in a towel",
             "towelling off after a swim", "showering after the gym", "in a bubble bath",
             "relaxing in the bath", "soaking in the bathtub", "bathing in the lake",
             "half undressed after a long day", "changing clothes after work",
             "changing her clothes", "getting changed at the beach", "getting dressed",
             "naked under a blanket", "nude", "topless on the deck", "in her bra",
             "in her underwear", "lingerie", "tangled in the sheets", "under the bedsheets",
             "in a robe", "in her fluffy bathrobe", "two bathrobes on the hook"]
DRESSED = ["a robin on the feeder", "sorting her wardrobe", "a cobra pose at yoga",
           "getting dressed up for a party", "a bathroom mirror selfie", "sunbathing on a lawn",
           "reading in the bedroom", "stretching out in bed", "lying back on the sofa"]


@pytest.mark.parametrize("field", ["activity", "shows"])
@pytest.mark.parametrize("text", UNDRESSED)
def test_undressing_and_bathing_are_refused_for_every_style(field, text):
    # controller ruling R12
    for p, data in ((lusty(), LUST_SCENE), *((m, SCENE) for m in plain_members())):
        assert f"scene.{field}: undress" in screen_scene(
            p, {**data, field: text}, ident=IDENT, member_names=[]), (p.romance.stance, text)


@pytest.mark.parametrize("field", ["activity", "shows"])
@pytest.mark.parametrize("text", DRESSED)
def test_whole_words_only_and_a_clothed_bed_scene_passes_for_every_style(field, text):
    # R12 as the owner amended it: a clothed bed or bedroom scene is fine for both styles
    for p, data in ((lusty(), LUST_SCENE), *((m, SCENE) for m in plain_members())):
        assert screen_scene(p, {**data, field: text}, ident=IDENT, member_names=[]) == [], text


def test_a_plain_scenes_other_free_text_is_screened_for_undressing_too():
    # a plain outfit and setting are free text as well: "a towel" is no outfit
    for field, text in (("outfit", "a fluffy bathrobe"), ("outfit", "just a towel"),
                        ("setting", "in the bath"), ("expression", "fresh out of the shower")):
        assert f"scene.{field}: undress" in screen_scene(woman(), {**SCENE, field: text},
                                                         ident=IDENT, member_names=[])


def test_the_probes_of_the_first_round():
    def codes(p, data, text):
        return screen_scene(p, {**data, "activity": text}, ident=IDENT, member_names=[])
    for text in ("just out of the shower", "undressing for the pool"):
        assert codes(lusty(), LUST_SCENE, text) == ["scene.activity: undress"]
        assert codes(woman(), SCENE, text) == ["scene.activity: undress"]
    assert codes(lusty(), LUST_SCENE, "stretching out in bed") == []       # owner: clothed is fine
    assert codes(woman(), SCENE, "stretching out in bed") == []
    assert codes(lusty(), LUST_SCENE, "posing on the bed") == []


def test_a_reply_for_the_other_style_is_refused():
    assert screen_scene(lusty(), {"photo": True, **SCENE}, ident=IDENT,
                        member_names=[]) == ["scene: picks for another style"]
    for p in plain_members():
        assert screen_scene(p, LUST_SCENE, ident=IDENT,
                            member_names=[]) == ["scene: picks for another style"]


def test_the_lust_prompt_joins_outfit_and_setting_at_priority_0():
    p = lusty()
    hot = build_scene_prompt(p, scene_from(LUST_SCENE))
    joined = ("wearing a low-cut emerald cocktail dress, a rooftop bar at night", 0)
    assert hot.phrases[:5] == (("mirror selfie", 0), ("waist-up shot, upper body", 0), subject_phrase(p),
                               (LUST_SCENE["activity"], 1), joined)
    flat = Scene(**{k: v for k, v in scene_json(scene_from(LUST_SCENE)).items() if k != "style"})
    plain = build_scene_prompt(p, flat)                      # the same scene, written plainly
    parts = (("a low-cut emerald cocktail dress", 2), ("a rooftop bar at night", 1))
    assert all(x in plain.phrases for x in parts) and joined not in plain.phrases
    assert not any(t in ("a low-cut emerald cocktail dress", "a rooftop bar at night")
                   for t, _ in hot.phrases)
    assert [x for x in hot.phrases if x != joined] == [x for x in plain.phrases if x not in parts]
    assert hot.negative == SNAP_NEGATIVE_LUST == LUST_NEGATIVE + (
        ("another person, crowd, group of people", 1),)
    assert plain.negative == SNAP_NEGATIVE


def test_the_lust_prompts_joined_phrase_keeps_both_parts_within_the_token_fit():
    fit = _worker_fit()
    outfit = max((kind.compose_lust({**LUST_SCENE, "outfit": o, "colour": c}).outfit
                  for o in LUST_OUTFITS for c in LUST_COLOURS), key=len)
    setting = max(SCENE_LUST_SETTINGS, key=len)
    big = replace(APPEARANCE, hair_style="h" * 40, face=("f" * 40,) * 4)
    for kind_, framing in (("snapshot", "head_and_shoulders"), ("mirror_selfie", "upper_body")):
        s = replace(scene_from(LUST_SCENE), kind=kind_, framing=framing, setting=setting,
                    outfit=outfit, activity="a" * 80, expression="an over-the-shoulder glance")
        spec = build_scene_prompt(lusty(age=25, region="US", appearance=big), s)
        kept, dropped = fit(spec.phrases, _tokens, 75)
        assert f"wearing {outfit}, {setting}" in kept
        assert outfit not in " ".join(dropped) and setting not in " ".join(dropped)
        assert any("f" * 40 == d for d in dropped)          # the long looks went, not the pick


def test_a_lust_scene_waits_once_she_left_lust_and_never_renders_plainly():
    s = scene_from(LUST_SCENE)
    assert scene_renderable(lusty(), s) is None
    for p in plain_members():
        assert scene_renderable(p, s) == LUST_HELD
        with pytest.raises(ValueError, match="re-choice"):
            build_scene_prompt(p, s)
        assert scene_renderable(p, Scene(**SCENE)) is None           # a plain scene still goes


def test_a_pick_that_shares_her_name_is_refused_and_a_rename_holds_the_scene():
    teal = lusty(real_name="Teal")
    assert screen_scene(teal, GYM_SCENE, ident=IDENT,
                        member_names=[]) == ["scene.outfit: shares her name"]
    renamed = lusty(real_name="Emerald")
    assert scene_renderable(renamed, scene_from(LUST_SCENE)) == LUST_NAMED
    with pytest.raises(ValueError):
        build_scene_prompt(renamed, scene_from(LUST_SCENE))


def test_the_lust_template_has_its_own_overlay_name(tmp_path):
    def fields(t):
        return {f for _, f, _, _ in Formatter().parse(t) if f}
    assert fields(SCENE_PROMPT_LUST) == fields(SCENE_PROMPT) == {"name", "card", "facts",
                                                                "looks", "about"}
    assert OLD_REFUSED.search(SCENE_PROMPT_LUST) is None and AGE_RE.search(SCENE_PROMPT_LUST) is None
    for rule in ("pick the kind of photo, the framing, where it is, what she is wearing and its "
                 "colour, and her expression from the choices the answer allows",
                 "swimwear only at the beach, the pool deck, the lakeside dock, the hot tub, the "
                 "boat or the beach bar, framed upper body", "the hot tub only in swimwear",
                 "sports top and leggings only at the gym", "she is the only person in it"):
        assert rule in SCENE_PROMPT_LUST.lower(), rule
    assert {SCENE_NAME, SCENE_LUST_NAME} <= night_prompt_names()   # the doctor knows both
    built_in = scene_system(lusty(), tmp_path, human=IDENT, **FIELDS)
    assert built_in.startswith(SCENE_PROMPT_LUST.format(**FIELDS))
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "nightly_scene_lust.txt").write_text("OVERLAYLUST {name}: {about}\n",
                                                    encoding="utf-8")
    hot = scene_system(lusty(), tmp_path, human=IDENT, **FIELDS)
    assert hot.startswith(f"OVERLAYLUST Arkon: {ABOUT['everyday']}")
    for p in plain_members():                                       # her overlay is hers only
        assert scene_system(p, tmp_path, human=IDENT, **FIELDS).startswith(
            SCENE_PROMPT.format(**FIELDS))
    (prompts / "nightly_scene.txt").write_text("OVERLAYPLAIN {name}\n", encoding="utf-8")
    assert scene_system(woman(), tmp_path, human=IDENT, **FIELDS).startswith("OVERLAYPLAIN Arkon")
    assert scene_system(lusty(), tmp_path, human=IDENT, **FIELDS).startswith("OVERLAYLUST")


def test_a_plain_members_scene_never_uses_the_lust_lists():
    lists = set(LUST_SETTINGS) | set(LUST_OUTFITS) | set(LUST_EXPRESSIONS) | set(LUST_COLOURS)
    props = SCENE_SCHEMA["properties"]
    assert "colour" not in props
    assert all("enum" not in props[f] for f in ("setting", "activity", "outfit", "expression",
                                                "shows"))
    assert not any(isinstance(v, dict) and lists & set(v.get("enum", ())) for v in props.values())
    for p in plain_members():
        assert style_of(p) == PLAIN and scene_schema(p) is SCENE_SCHEMA
        spec = build_scene_prompt(p, Scene(**SCENE))
        assert spec.negative == SNAP_NEGATIVE
        assert (SCENE["outfit"], 2) in spec.phrases and (SCENE["setting"], 1) in spec.phrases
        assert not any(t.startswith("wearing ") for t, _ in spec.phrases)
        # a lust pick written as free text meets the plain screen, which refuses swimwear
        swim = {**SCENE, "setting": "a hotel pool deck", "outfit": "a coral halter bikini"}
        assert "scene.outfit: explicit" in screen_scene(p, swim, ident=IDENT, member_names=[])


# -- fix round 2 (controller rulings R16 to R20) ---------------------------------------------

# The review's phrase vectors (t6-refuse-groups1.json and t6-refuse-groups2.json, copied),
# each group with the codes any one of which refuses its phrases.
REFUSE = {
    'undress verbs (known finding)': (('undress', 'explicit'), [
        'slipping off a strap', 'slipping a strap off her shoulder',
        'a strap sliding off her shoulder', 'her dress strap falling down', 'lowering one strap',
        'pulling down a strap', 'pulling her top down', 'taking off her top',
        'taking her shirt off', 'took off her dress', 'unzipping her dress',
        'her dress half unzipped', 'unbuttoning her blouse', 'her shirt unbuttoned',
        'peeling off her leggings', 'peeling off her wet top', 'stripping off', 'stripped down',
        'strip tease', 'disrobing', 'slipping out of her dress', 'wriggling out of her jeans',
        'stepping out of her dress', 'shrugging off her dress', 'letting her dress fall',
        'dropping her dress to the floor', 'unhooking her top', 'unfastening her dress',
        'unlacing her corset', 'loosening her top', 'lifting her top', 'pulling up her shirt',
        'flashing the camera', 'clothes off', 'her clothes on the floor', 'out of her clothes',
        'with her top off', 'shirt off', 'no shirt', 'without a top', 'nothing on',
        'wearing nothing', 'nothing but a smile', 'half-dressed', 'partially dressed',
        'barely dressed', 'barely covered', 'scantily clad', 'skimpy', 'a revealing outfit',
        'bare shoulders', 'her bare back to the mirror', 'bare-chested', 'exposed skin',
    ]),
    'bath and shower variants': (('undress', 'explicit'), [
        'fresh from the tub', 'soaking in the tub', 'relaxing in the hot tub', 'in the jacuzzi',
        'in the sauna', 'a steam room', 'at the spa', 'an onsen', 'a hot spring', 'skinny dipping',
        'a long soak', 'hair still wet', 'dripping wet', 'soaking wet',
        'wet hair and a fogged-up mirror', 'a steamy bathroom', 'rinsing off', 'washing her hair',
        'post-swim rinse', 'oiled up', 'glistening with oil', 'towlling off', 'bath-robe',
        'bath robe', 'bubblebath', 'a showery morning',
    ]),
    'bedding and robe variants': (('undress', 'explicit'), [
        'wrapped in a sheet', 'a white sheet around her', 'under the covers', 'under the duvet',
        'wrapped in a blanket', 'wrapped in a duvet', 'under a quilt', 'a dressing gown',
        'a silk kimono', 'a housecoat', 'a peignoir', 'an oversized shirt and nothing else',
        'only his shirt', 'just a t-shirt', 'boxers', 'briefs', 'a thong', 'knickers', 'undies',
        'a corset', 'stockings and garters', 'a bodysuit', 'a leotard', 'booty shorts',
        'a nightshirt', 'a sleep shirt', 'getting ready for bed', 'ready for bed',
    ]),
    'changing variants': (('undress',), [
        'changing into something comfy', 'changing out of her work clothes', 'changing outfits',
        'in the changing room', 'in the fitting room', 'in the locker room', 'trying on dresses',
        'putting on her dress', 'pulling on her jeans', 'zipping up her dress',
        'getting dressed for work', 'got dressed', 'dressing for the day',
        'half way through getting dressed',
    ]),
    'sexual poses and tone (bed allowed by R12a)': (('explicit', 'undress'), [
        'on all fours on the bed', 'bent over the bed', 'straddling a pillow',
        'legs spread on the bed', 'arching her back on the bed', 'lying on her stomach, legs up',
        'biting her lip in bed', 'a boudoir shoot', 'an erotic pose', 'a lewd pose',
        'a sultry pose', 'steamy', 'racy', 'provocative', 'naughty', 'risque', 'spicy pic',
        'bedroom eyes', 'come-hither look', 'a thirst trap', 'horny', 'kinky',
        'teasing the camera with her neckline', 'a sexy pout', 'covering her chest with her arm',
        'hands covering herself', 'implied nude', 'censored',
    ]),
    'another person in the photo': (('other_person',), [
        'with her boyfriend', 'kissing her girlfriend', 'hugging her husband', 'with her mum',
        'with a friend', 'with friends at the bar', 'with her coworkers', "a couple's selfie",
        'cuddling her date', 'a stranger behind her', 'with the team', 'with her sister',
        "her partner's arm around her", 'two of them on the sofa', 'with Sarah',
        'a man beside her',
    ]),
    'minors beyond MINOR_RE': (('minor', 'age'), [
        'with her stepson', 'with her stepdaughter', 'the grandkids', 'her kiddos',
        'the little ones', 'her little one', 'the twins', 'youngsters', "a toddler's toy",
        'a newborn', 'a tot', 'the bairns', 'her godson', 'at the playground', 'at daycare',
        'the nursery', 'the school run', 'minors', 'a juvenile', 'her class of first-graders',
        'the cub scouts', 'a pregnant belly',
    ]),
    'age coding on plain scenes': (('age',), [
        'a plaid pleated skirt and knee socks', 'a prom dress', 'at her prom',
        'a sweet sixteen party', 'her 16th birthday', 'turning 17 tomorrow',
        'looking about fifteen', 'girly', 'girlish giggle', 'dressed like a little girl',
        'innocent doe eyes', 'sucking a lollipop', 'a retainer', 'freshman year',
        'in her dorm room', 'barely nineteen', 'just turned nineteen', 'petite and tiny',
        'a school-style uniform', 'a schoolgirl outfit', 'a student uniform', 'teeny bikini',
        'high-school reunion', 'a kawaii outfit', 'doll-like', 'knee-high socks and a short skirt',
        'a 17 year old', 'seventeen',
    ]),
    'likeness and real names': (('likeness',), [
        'dressed as Marilyn Monroe', 'doing her best Audrey Hepburn', 'at a Taylor Swift concert',
        'channelling Beyonce', 'in a Barbie outfit', 'Wonder Woman cosplay',
        'her Kim Kardashian pose', 'a lookalike contest', 'a dead ringer for an actress',
        'styled like a movie star', 'her influencer pose', 'like a supermodel',
    ]),
    'looks changes beyond LOOKS_RE': (('looks',), [
        'in her glasses', 'with glasses on', 'putting on her glasses', 'wearing sunglasses',
        'reading glasses', 'shaved her head', 'a pixie cut', 'cut her hair short',
        'chopped her hair', 'colored her hair', 'went blonde', 'her new red hair',
        'clean-shaven now', 'a fresh tan', 'pregnant', 'a new nose ring', 'her new bob',
        'grew out his hair', 'a crew cut', 'with a new mohawk', 'a lip filler', 'botox',
        'her hair dyed pink',
    ]),
    'era beyond ERA_RE': (('era',), [
        'a nineties party', 'the eighties', 'Y2K style', 'a throwback look', 'old-school',
        'a disco', 'a flip phone', 'a 70s outfit', '1999', 'a polaroid',
    ]),
}
PLAIN_FIELDS = ("setting", "activity", "outfit", "expression", "shows")
CASES = [(label, family, text) for label, (family, texts) in REFUSE.items() for text in texts]


@pytest.mark.parametrize("label,family,text", CASES, ids=[c[2] for c in CASES])
def test_every_phrase_of_the_review_is_refused_for_both_styles_in_every_field(label, family,
                                                                              text):
    # R16: lust free text (activity, shows) beside a dress pick, and every plain field
    checks = [(lusty(), LUST_SCENE, f) for f in ("activity", "shows")]
    checks += [(woman(), SCENE, f) for f in PLAIN_FIELDS]
    for p, data, field in checks:
        codes = screen_scene(p, {**data, field: text}, ident=IDENT, member_names=["Testb"])
        assert any(f"scene.{field}: {code}" in codes for code in family), (field, codes)


def test_the_allowances_pass_and_the_accepted_over_refusals_are_refused():
    def codes(p, data, field, text):
        return screen_scene(p, {**data, field: text}, ident=IDENT, member_names=["Testb"])
    for field in ("activity", "shows"):
        for text in ("stretching out in bed", "a robin on the feeder", "a bathroom mirror selfie"):
            assert codes(lusty(), LUST_SCENE, field, text) == [], (field, text)   # a dress pick
    for field in PLAIN_FIELDS:
        for text in ("stretching out in bed", "a robin on the feeder", "a bathroom mirror selfie"):
            assert codes(woman(), SCENE, field, text) == [], (field, text)
    # R20: accepted over-refusals stay refused
    for text in ("taking off her coat", "kicking off her shoes", "old-school", "a beach towel",
                 "a sports bra at the gym", "a bathing suit"):
        assert codes(lusty(), LUST_SCENE, "activity", text), text
        assert codes(woman(), SCENE, "activity", text), text
    assert "scene.activity: undress" in codes(woman(), SCENE, "activity", "taking off her coat")


@pytest.mark.parametrize("text", ["stretching out in bed", "curled up on the sofa",
                                  "on the couch", "propped on pillows", "under a blanket",
                                  "on a mattress", "in her bedroom", "under the duvet"])
def test_free_text_never_takes_a_swimwear_or_gym_pick_indoors_or_into_bed(text):
    # the review's critical finding: "stretching out in bed" with a bikini at the pool deck
    for data, pairing in ((SWIM_SCENE, PAIRINGS[0]), (GYM_SCENE, PAIRINGS[1])):
        for field in ("activity", "shows"):
            codes = screen_scene(lusty(), {**data, field: text}, ident=IDENT, member_names=[])
            assert scode(pairing) in codes, (field, codes)
    assert scode(PAIRINGS[0]) == "scene.outfit: swimwear away from the water"
    if text == "stretching out in bed":                       # with a dress pick it is fine
        assert screen_scene(lusty(), {**LUST_SCENE, "activity": text}, ident=IDENT,
                            member_names=[]) == []


@pytest.mark.parametrize("text", ["in her one-piece", "a tankini", "a monokini", "swim trunks",
                                  "her swimsuit", "a red bikini", "in his trunks",
                                  "a one piece by the pool"])
def test_swimwear_is_never_named_in_free_text(text):
    # R19: hers is only ever her pick; a plain scene never wears it
    for field in ("activity", "shows"):
        assert f"scene.{field}: explicit" in screen_scene(
            lusty(), {**SWIM_SCENE, field: text}, ident=IDENT, member_names=[])
    for p in plain_members():
        for field in PLAIN_FIELDS:
            assert f"scene.{field}: explicit" in screen_scene(
                p, {**SCENE, field: text}, ident=IDENT, member_names=[])


@pytest.mark.parametrize("text,code", [
    ("half\tundressed", "undress"), ("bubble \n bath", "undress"), ("pig   tails", "age"),
    ("on  all\nfours", "explicit"), ("with   her   boyfriend", "other_person"),
    ("dressed\u00a0as a star", "likeness")])
def test_the_screen_reads_text_with_its_whitespace_folded(text, code):
    for p, data in ((lusty(), LUST_SCENE), (woman(), SCENE)):
        assert f"scene.activity: {code}" in screen_scene(
            p, {**data, "activity": text}, ident=IDENT, member_names=[])


def test_a_pet_is_fine_and_a_friend_behind_the_camera_is_not_in_it():
    for text in ("walking her dog", "the cat on her lap", "a snapshot a friend took",
                 "taken by a friend on the pier"):
        for p, data in ((lusty(), LUST_SCENE), (woman(), SCENE)):
            assert screen_scene(p, {**data, "shows": text}, ident=IDENT,
                                member_names=[]) == [], text
    assert "scene.shows: other_person" in screen_scene(
        woman(), {**SCENE, "shows": "Her with a friend on the pier."}, ident=IDENT,
        member_names=[])


def test_a_name_after_the_first_word_is_a_likeness_and_a_place_in_the_setting_may_be_one():
    def codes(field, text):
        return screen_scene(woman(), {**SCENE, field: text}, ident=IDENT, member_names=[])
    for field in ("activity", "outfit", "expression"):
        assert f"scene.{field}: likeness" in codes(field, "smiling like Mona")
    assert codes("setting", "a pub in Leeds") == [] and codes("shows", "Them in Leeds.") == []
    assert "scene.setting: likeness" in codes("setting", "a gig by Taylor Swift")


@pytest.mark.parametrize("text,young", [
    ("her 16th birthday", True), ("a 17 year old", True), ("turning 17 tomorrow", True),
    ("aged twelve", True), ("a ten-year-old", True), ("barely nineteen", True),
    ("her 34th birthday", False), ("a 40-year-old", False), ("turning 30", False),
    ("about 5 minutes from home", False), ("two years old", True), ("aged 21", True), ("aged 25", False)])  # 25 is the floor since #178
def test_the_age_number_rule(text, young):
    codes = screen_scene(woman(), {**SCENE, "activity": text}, ident=IDENT, member_names=[])
    assert ("scene.activity: age" in codes) == young, codes


def test_the_lust_template_keeps_her_place_out_of_her_free_text():
    assert "never what she wears or where she is" in SCENE_PROMPT_LUST
    assert OLD_REFUSED.search(SCENE_PROMPT_LUST) is None    # still non-explicit (round 1 test)


# -- final fix wave (controller ruling R22) --------------------------------------------------

@pytest.mark.parametrize("pc", ["a lacy camisole and sleep shorts", "her old school hoodie",
                                "a vintage 90s band tee"])
def test_the_tile_leaves_out_an_unscreened_at_the_pc_phrase(pc):
    p = woman()
    p = replace(p, appearance=replace(p.appearance, at_the_pc=pc))
    spec = build_tile_prompt(p)
    assert all(pc not in text for text, _ in spec.phrases)
    assert ("at their desk at home at night, wearing a headset, lit by the monitor", 1) \
        in spec.phrases


def test_the_tile_keeps_a_clean_at_the_pc_phrase():
    p = woman()
    p = replace(p, appearance=replace(p.appearance, at_the_pc="a grey hoodie"))
    assert ("a grey hoodie", 2) in build_tile_prompt(p).phrases


I2_REFUSED = [("wearing only an apron", "undress"), ("only socks", "undress"),
              ("twerking on the dance floor", "explicit"), ("giving a lap dance", "explicit"),
              ("pole dancing", "explicit"), ("grinding on the dance floor", "explicit"),
              ("eating a banana suggestively", "explicit"), ("in a cheerleading top", "age"),
              ("a short silk slip", "undress")]


@pytest.mark.parametrize("text,code", I2_REFUSED, ids=[t for t, _ in I2_REFUSED])
def test_the_final_review_gaps_are_refused_for_both_styles_in_every_field(text, code):
    checks = [(lusty(), LUST_SCENE, f) for f in ("activity", "shows")]
    checks += [(woman(), SCENE, f) for f in PLAIN_FIELDS]
    for p, data, field in checks:
        codes = screen_scene(p, {**data, field: text}, ident=IDENT, member_names=[])
        assert f"scene.{field}: {code}" in codes, (field, codes)


@pytest.mark.parametrize("text", ["a slip dress", "slipping on her shoes",
                                  "grinding coffee beans", "only a little tired"])
def test_the_final_review_allowances_still_pass(text):
    # "grinding" is told apart from the dance floor only by a food word after it (coffee, beans,
    # pepper, spices, salt, herbs, flour, seeds); anything else after it is refused (R20)
    for p, data in ((lusty(), LUST_SCENE), (woman(), SCENE)):
        for field in ("activity", "shows"):
            assert screen_scene(p, {**data, field: text}, ident=IDENT, member_names=[]) == []


def test_a_cheerleading_look_is_age_coded_for_a_profile_photo_too():
    assert AGE_RE.search("in a cheerleading top") and AGE_RE.search("cheerleader")
