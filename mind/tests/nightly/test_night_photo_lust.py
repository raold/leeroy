"""The lust style of the profile photos (owner, 2026-11-07; controller round 3): a woman whose
romance stance toward the owner is lust picks her photo kind from fixed, vetted lists (setting,
outfit, colour, expression) and writes no text of her own; the code builds the kind. Swimwear
goes only with the water settings and the gym outfit only with the gym. A kind chosen under the
other rules is chosen again once when her stance moves, and a re-choice into the lust style
queues a fresh set. Everyone else keeps the plain rules, plus the age guard every free-text kind
now meets. Fakes only; every person here is invented."""
import logging
import re
from dataclasses import asdict, dataclass, replace
from string import Formatter

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.enrich import LIKENESS_RE, _facts
from leeroy_mind.nightly.llm import check_schema, system_prompt
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import kind
from leeroy_mind.selfies.kind import (AGE_RE, ERA_RE, GYM_OUTFITS, GYM_SETTINGS, KIND_PROMPT,
                                      KIND_PROMPT_LUST, LUST_ASK_AGAIN, LUST_COLOURS,
                                      LUST_EXPRESSIONS, LUST_OUTFITS, LUST_PROMPT, LUST_SCHEMA,
                                      LUST_SETTINGS, PAIRINGS, STAGE, SWIM_ONLY_SETTINGS,
                                      SWIM_OUTFITS, WATER_SETTINGS, chosen_style, compose_lust,
                                      looks_line,
                                      needs_rechoice, screen_kind, screen_lust, stage_photo)
from leeroy_mind.selfies.prompt import build_prompt
from leeroy_mind.selfies.store import queued_sets
from leeroy_mind.selfies.style import LUST, PLAIN, SUGGESTIVE_MIN_AGE, style_of, suggestive
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE

# today's strings, copied: the plain rules must stay exactly these
TODAY_KIND_PROMPT = (
    "Choose the profile picture the real person who plays {name} would put on their own social "
    "or work profile.\n{card}\n{facts}\nHow they look: {looks}\n"
    "Choose what fits who they are, their work, age, region, habits and personality: the kind "
    "of photo (a phone selfie, a mirror selfie, a professional headshot, or a snapshot a friend "
    "took), the framing (face, head and shoulders, or upper body), where it was taken (a place "
    "in their own life, in a few words), what they are wearing in it, and their expression. "
    "Name no year, no decade, no device and no brand. Fully clothed, nothing revealing or "
    "sexual, never like anyone famous, and no one else's name.")
TODAY_EXPLICIT = (
    r"\b(nude|naked|topless|shirtless|braless|lingerie|underwear|bra|panties|bikini|swimsuit|"
    r"swimwear|see-through|sheer|sexy|sexual|seductive|nsfw|cleavage)\b")
# The free-text lust screen of rounds 1 and 2, kept here as a yardstick only: no value of the
# lists, and no prompt phrase built from them, may use one of its words.
OLD_REFUSED = re.compile(
    r"\b(?:nude|nudes|naked|nudity|topless|shirtless|braless|undress\w*|unclothed|nipples?|"
    r"pasties|body[- ]?paint(?:ed)?|unbutton\w*|unzipped|wet|soaked|dripping|oiled|"
    r"nothing (?:but|else|underneath|on|at all)|wearing nothing|not wearing anything|"
    r"birthday suit|au naturel|(?:no|without) (?:a )?(?:top|bra|pants|shirt|clothes|clothing)|"
    r"(?:just|only) (?:a|an|her|his|my) (?:[\w'-]+ ){0,2}(?:shirt|hoodie|sweater|jersey)s?|"
    r"barely (?:dressed|there)|scantily|skimpy|half[- ]dressed|partially dressed|"
    r"covering (?:her|his|my) (?:chest|body)|bare[- ]?(?:chested|breast\w*)|breasts?|"
    r"\w*boob\w*|areolas?|lingerie|underwear|undies|bra|bras|bralettes?|bralets?|brassieres?|"
    r"bustiers?|panties|knickers|thong|g-string|boyshorts|negligee|nightie|nightgown|"
    r"nightdress|nightwear|sleepwear|pajamas?|pyjamas?|pjs|chemise|camisole|bodysuit|"
    r"babydolls?|baby-doll|teddy|teddies|corset|garters?|body ?stockings?|stockings|suspenders|"
    r"bandeau|catsuit|latex|harness|fishnets?|mesh|towels?|robes?|bathrobes?|monokini|"
    r"micro[- ]?bikini|see[- ]?thr(?:ough|u)|sheer|transparent|shower\w*|baths?|bathtubs?|"
    r"bubble bath|(?<!hot )(?<!hot-)tubs?|beds?|sheets?|covers|blankets?|duvets?|sauna|"
    r"steam room|locker room|changing room|fitting room|nsfw|sex|sexual|sexually|"
    r"sexuali[sz]ed|seductive|erotic|erotica|explicit|porn\w*|fetish|boudoir|horny|aroused|"
    r"kinky|lewd|xxx|onlyfans|on all fours|bent over|straddl\w*|legs spread|spread legs|"
    r"nudist|naturist|clothing-optional|strip\w*|bedroom|bathroom|spa|stage|jacuzzi)\b", re.I)

PLAIN_KIND = {"kind": "snapshot", "framing": "upper_body", "setting": "on the deck of a small boat",
              "outfit": "orange rain bib over a grey hoodie",
              "expression": "squinting into the wind"}
LUST_REPLY = {"kind": "mirror_selfie", "framing": "upper_body",
              "setting": "the mirror in her hallway", "outfit": "fitted little dress",
              "colour": "black", "expression": "a playful smile"}
LUST_PHOTO = ProfilePhoto("mirror_selfie", "upper_body", "the mirror in her hallway",
                          "a fitted little black dress", "a playful smile")
SWIM_REPLY = {"kind": "snapshot", "framing": "upper_body", "setting": "the beach at sunset",
              "outfit": "string bikini", "colour": "red", "expression": "a teasing grin"}
SWIM_AWAY = {**SWIM_REPLY, "setting": "a cafe terrace"}
GYM_REPLY = {"kind": "mirror_selfie", "framing": "upper_body", "setting": "the gym",
             "outfit": "sports top and leggings", "colour": "teal",
             "expression": "a confident smirk"}
GYM_AWAY = {**GYM_REPLY, "setting": "a wine bar"}
HAND = ProfilePhoto("headshot", "face", "an office", "a blazer", "a calm smile")
YOUNG_LOOKS = ({"face": ("a youthful face",)}, {"hair_style": "two pigtails"},
               {"face": ("braces on her teeth", "freckles")}, {"hair_style": "pig tails"},
               {"face": ("a babyface",)}, {"skin_tone": "teenage-clear"})
NIGHTS = ("2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01")


def lust(h, guid=102, appearance=APPEARANCE, **over):
    return h.woman(guid, "lust", appearance=appearance, **over)


def calls(ctx) -> list[tuple[str, str, dict]]:
    """(system, user, schema) of each request the photo pass sends, in order."""
    seen, real = [], ctx.reasoner.json

    async def json_(system, user, schema, **kw):
        seen.append((system, user, schema))
        return await real(system, user, schema, **kw)
    ctx.reasoner.json = json_
    return seen


def expected(ctx, guid, name, template) -> str:
    """The photo pass's system prompt for `guid` now, from prompt `name` with `template` as its
    built-in default."""
    p = ctx.base(guid)
    return system_prompt(name, template, ctx.settings.data_dir, human=IDENT, name=ctx.name(guid),
                         card=ctx.base_card(guid), facts=_facts(p), looks=looks_line(p.appearance))


def _save(h, guid, p, source):
    _, race, cls = TEST_CAST[guid]
    save_persona(h.store, p, source=source, ts=h.T, race=RACES[race], class_name=CLASSES[cls])


def restance(h, guid, stance, *, source="generated"):
    """Her stance moved (the night's stance move, or the owner's rebalance)."""
    p = load_persona(h.store, guid)
    _save(h, guid, replace(p, romance=replace(p.romance, stance=stance)), source)


def by_hand(h, guid, photo: ProfilePhoto):
    _save(h, guid, replace(load_persona(h.store, guid), photo=photo), "owner")


def ctx_for(h, n, plain=(), lusty=(), **kw):
    return h.ctx({"photo": list(plain), "photo_lust": list(lusty)}, night=NIGHTS[n], **kw)


def night(h, n, plain=(), lusty=()):
    ctx = ctx_for(h, n, plain, lusty)
    run(stage_photo(ctx))
    return ctx


def photo(h, guid):
    return load_persona(h.store, guid).photo


def style(h, guid):
    return chosen_style(h.store, load_persona(h.store, guid))


def queued(h, guid):
    return [(s.set_no, s.reason) for s in queued_sets(h.store) if s.guid == guid]


def _reply(outfit, colour="black", setting=None, expression="a wink"):
    if setting is None:
        setting = (WATER_SETTINGS[0] if outfit in SWIM_OUTFITS else
                   GYM_SETTINGS[0] if outfit in GYM_OUTFITS else "a rooftop bar at night")
    return {"kind": "selfie", "framing": "upper_body", "setting": setting, "outfit": outfit,
            "colour": colour, "expression": expression}


# -- who ----------------------------------------------------------------------------------------

def test_the_suggestive_style_is_a_lust_woman_of_21_or_more(h):
    assert SUGGESTIVE_MIN_AGE == 21
    assert suggestive(lust(h)) and style_of(lust(h)) == LUST
    assert suggestive(lust(h, age=21))
    assert not suggestive(lust(h, age=20)) and style_of(lust(h, age=20)) == PLAIN
    assert not suggestive(h.woman(103, "drawn", appearance=APPEARANCE))
    assert not suggestive(h.woman(104, "fond")) and not suggestive(h.woman(105, "none"))
    assert not suggestive(h.persona(101, appearance=APPEARANCE))
    assert not suggestive(None) and style_of(None) == PLAIN
    for looks in YOUNG_LOOKS:                            # looks that code young: never lust
        p = lust(h, 103, appearance=replace(APPEARANCE, **looks))
        assert not suggestive(p) and style_of(p) == PLAIN, looks


# -- the template and the schema -----------------------------------------------------------

def test_a_lust_woman_gets_the_lust_template_and_schema_and_everyone_else_todays(h):
    assert KIND_PROMPT == TODAY_KIND_PROMPT and LUST_PROMPT == "photo_lust"
    h.persona(101, appearance=APPEARANCE)
    lust(h, 102)
    h.woman(103, "fond", appearance=APPEARANCE)
    lust(h, 104, age=20)
    ctx = h.ctx({"photo": [PLAIN_KIND] * 3, "photo_lust": [LUST_REPLY]})
    want = {102: expected(ctx, 102, LUST_PROMPT, KIND_PROMPT_LUST),
            **{g: expected(ctx, g, STAGE, TODAY_KIND_PROMPT) for g in (101, 103, 104)}}
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert sorted(s for s, _, _ in seen) == sorted(want.values())
    assert [sc for s, _, sc in seen if s == want[102]] == [LUST_SCHEMA]
    assert all(sc == kind.KIND_SCHEMA for s, _, sc in seen if s != want[102])
    assert photo(h, 102) == LUST_PHOTO
    for g in (101, 103, 104):
        assert photo(h, g) == ProfilePhoto(**PLAIN_KIND)


def test_the_lust_template_takes_the_same_fields_and_asks_only_for_picks():
    def fields(t):
        return {f for _, f, _, _ in Formatter().parse(t) if f}
    assert fields(KIND_PROMPT_LUST) == fields(KIND_PROMPT) == {"name", "card", "facts", "looks"}
    text = KIND_PROMPT_LUST.lower()
    for rule in ("pick each part from the choices the answer allows",
                 "swimwear only at the beach, the pool deck, the lakeside dock, the hot tub, the "
                 "boat or the beach bar, framed upper body and never as a headshot",
                 "the hot tub only in swimwear", "sports top and leggings only at the gym"):
        assert rule in text
    assert OLD_REFUSED.search(KIND_PROMPT_LUST) is None and AGE_RE.search(KIND_PROMPT_LUST) is None


def test_the_schema_offers_only_the_lists_and_nothing_else():
    props = LUST_SCHEMA["properties"]
    assert LUST_SCHEMA["title"] == "photo_lust" and LUST_SCHEMA["additionalProperties"] is False
    assert set(LUST_SCHEMA["required"]) == set(props) == {"kind", "framing", "setting", "outfit",
                                                          "colour", "expression"}
    assert props["kind"] == kind._PROPS["kind"] and props["framing"] == kind._PROPS["framing"]
    assert props["setting"]["enum"] == list(LUST_SETTINGS)
    assert props["outfit"]["enum"] == list(LUST_OUTFITS)
    assert props["colour"]["enum"] == list(LUST_COLOURS)
    assert props["expression"]["enum"] == list(LUST_EXPRESSIONS)
    assert all("enum" in p for p in props.values())


@pytest.mark.parametrize("field,value", [
    ("outfit", "a towel"), ("outfit", "fitted little dress, nothing under it"),
    ("setting", "her bed"), ("setting", "a nudist beach"), ("expression", "a sleepy pout"),
    ("colour", "white"), ("colour", "sheer"), ("kind", "boudoir"), ("framing", "full_body")])
def test_a_reply_off_the_lists_is_refused_by_the_schema(field, value):
    with pytest.raises(ValueError):
        check_schema(LUST_SCHEMA, {**LUST_REPLY, field: value})
    with pytest.raises(ValueError):
        check_schema(LUST_SCHEMA, {**LUST_REPLY, "note": "anything"})
    check_schema(LUST_SCHEMA, LUST_REPLY)


def test_a_reply_off_the_lists_is_stored_nowhere(h):
    lust(h, 102)
    bad = {**LUST_REPLY, "outfit": "a towel"}
    ctx = night(h, 0, lusty=[bad, bad])
    assert photo(h, 102) is None and ctx.done(STAGE, "102")
    assert screen_lust(load_persona(h.store, 102), bad) == ["photo.outfit: not on its list"]


def test_the_overlay_replaces_the_template_but_cannot_add_a_word(h):
    prompts = h.settings.data_dir / "prompts"
    prompts.mkdir()
    (prompts / "nightly_photo_lust.txt").write_text(
        "OVERLAYLUST {name}: {looks} Put her in a towel.\n", encoding="utf-8")
    lust(h, 102)
    h.woman(103, "fond", appearance=APPEARANCE)
    ctx = h.ctx({"photo": [PLAIN_KIND], "photo_lust": [LUST_REPLY]})
    mine = expected(ctx, 102, LUST_PROMPT, "unused")
    other = expected(ctx, 103, STAGE, TODAY_KIND_PROMPT)
    assert mine.startswith("OVERLAYLUST Belra: ") and KIND_PROMPT_LUST[:40] not in mine
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert sorted(s for s, _, _ in seen) == sorted([mine, other])
    assert [sc for s, _, sc in seen if s == mine] == [LUST_SCHEMA]   # the lists still bind
    assert photo(h, 102) == LUST_PHOTO
    towel = {**LUST_REPLY, "outfit": "a towel"}
    lust(h, 104)
    run(stage_photo(h.ctx({"photo_lust": [towel, towel]}, night=NIGHTS[1])))
    assert photo(h, 104) is None


# -- the lists -------------------------------------------------------------------------------

def test_every_choice_composes_a_kind_within_the_line(h):
    p = lust(h)
    lengths = {"setting": 80, "outfit": 80, "expression": 40}
    replies = ([_reply(o, c) for o in LUST_OUTFITS for c in LUST_COLOURS]
               + [_reply("string bikini" if s in SWIM_ONLY_SETTINGS else "fitted little dress",
                         setting=s) for s in LUST_SETTINGS]
               + [_reply("fitted little dress", expression=e) for e in LUST_EXPRESSIONS])
    for r in replies:
        assert screen_lust(p, r) == [], r
        made = compose_lust(r)
        for field in kind._FIELDS:
            text = getattr(made, field)
            assert not OLD_REFUSED.search(text), (field, text)
            for guard in (AGE_RE, ERA_RE, LIKENESS_RE):
                assert not guard.search(text), (field, text)
            assert field not in lengths or len(text) <= lengths[field], (field, text)
        for phrase, _ in build_prompt(replace(p, photo=made)).phrases:
            assert not OLD_REFUSED.search(phrase) and not AGE_RE.search(phrase), phrase
    assert len(replies) == len(LUST_OUTFITS) * len(LUST_COLOURS) + len(LUST_SETTINGS) + \
        len(LUST_EXPRESSIONS)


def test_the_lists_hold_no_bedroom_bath_or_stage():
    places = " | ".join(LUST_SETTINGS).lower()
    for word in ("bedroom", "bathroom", "bed", "shower", "bath", "sauna", "spa", "stage",
                 "club stage", "changing", "locker", "nudist", "naturist"):
        assert not re.search(rf"\b{word}\b", places), word
    assert "corset" not in " ".join(LUST_OUTFITS)
    assert "satin slip dress" not in LUST_OUTFITS and "satin midi dress" in LUST_OUTFITS
    assert "cropped cardigan and wide-leg trousers" in LUST_OUTFITS
    assert "cropped cardigan and a miniskirt" not in LUST_OUTFITS
    assert set(SWIM_ONLY_SETTINGS) <= set(WATER_SETTINGS)
    assert set(SWIM_OUTFITS) < set(LUST_OUTFITS) and set(GYM_OUTFITS) < set(LUST_OUTFITS)
    assert set(WATER_SETTINGS) < set(LUST_SETTINGS) and set(GYM_SETTINGS) < set(LUST_SETTINGS)


def test_the_outfit_takes_its_colour_and_an_article():
    assert compose_lust(LUST_REPLY).outfit == "a fitted little black dress"
    assert compose_lust({**LUST_REPLY, "outfit": "satin midi dress", "colour": "emerald"}
                        ).outfit == "an emerald satin midi dress"
    assert compose_lust({**LUST_REPLY, "outfit": "off-the-shoulder top and jeans",
                         "colour": "red"}).outfit == "an off-the-shoulder red top and jeans"
    assert compose_lust(SWIM_REPLY) == ProfilePhoto("snapshot", "upper_body",
                                                    "the beach at sunset", "a red string bikini",
                                                    "a teasing grin")


# -- the pairings ------------------------------------------------------------------------------

def test_swimwear_goes_only_with_the_water_settings(h):
    p = lust(h)
    for outfit in SWIM_OUTFITS:
        for setting in LUST_SETTINGS:
            out = screen_lust(p, _reply(outfit, setting=setting))
            assert out == ([] if setting in WATER_SETTINGS else [PAIRINGS[0]]), (outfit, setting)


def test_the_gym_outfit_goes_only_with_the_gym(h):
    p = lust(h)
    for outfit in GYM_OUTFITS:
        for setting in LUST_SETTINGS:
            out = screen_lust(p, _reply(outfit, setting=setting))
            want = [] if setting in GYM_SETTINGS else [PAIRINGS[1]]
            if setting in SWIM_ONLY_SETTINGS:
                want.append(PAIRINGS[2])
            assert out == want, (outfit, setting)
    for setting in LUST_SETTINGS:  # dressed outfits go anywhere but the hot tub
        out = screen_lust(p, _reply("backless summer dress", setting=setting))
        assert out == ([PAIRINGS[2]] if setting in SWIM_ONLY_SETTINGS else []), setting


def test_the_hot_tub_takes_swimwear_only(h):
    p = lust(h)
    assert SWIM_ONLY_SETTINGS == ("a hot tub on a wooden deck",)
    for outfit in LUST_OUTFITS:
        out = screen_lust(p, _reply(outfit, setting="a hot tub on a wooden deck"))
        if outfit in SWIM_OUTFITS:
            assert out == [], outfit
        elif outfit in GYM_OUTFITS:
            assert out == [PAIRINGS[1], PAIRINGS[2]], outfit
        else:
            assert out == [PAIRINGS[2]], outfit


@pytest.mark.parametrize("kind_,framing,problems", [
    ("selfie", "upper_body", []), ("mirror_selfie", "upper_body", []),
    ("snapshot", "upper_body", []), ("selfie", "face", [3]),
    ("snapshot", "head_and_shoulders", [3]), ("headshot", "upper_body", [4]),
    ("headshot", "face", [3, 4])])
def test_swimwear_goes_only_framed_upper_body_and_never_as_a_headshot(h, kind_, framing,
                                                                        problems):
    p = lust(h)
    for outfit in SWIM_OUTFITS:
        out = screen_lust(p, {**_reply(outfit), "kind": kind_, "framing": framing})
        assert out == [PAIRINGS[i] for i in problems], (outfit, kind_, framing)
    dressed = {**_reply("bodycon mini dress"), "kind": kind_, "framing": framing}
    assert screen_lust(p, dressed) == []                # a dressed kind may be any framing


def test_a_swimwear_framing_that_fails_is_asked_once_more(h):
    lust(h, 102)
    ctx = ctx_for(h, 0, lusty=[{**SWIM_REPLY, "framing": "head_and_shoulders"}, SWIM_REPLY])
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 2 and photo(h, 102) == compose_lust(SWIM_REPLY)


def test_a_pairing_that_fails_is_asked_once_more(h, caplog):
    lust(h, 102)
    ctx = ctx_for(h, 0, lusty=[SWIM_AWAY, SWIM_REPLY])
    seen = calls(ctx)
    with caplog.at_level(logging.DEBUG):
        run(stage_photo(ctx))
    assert len(seen) == 2 and seen[1][1].endswith(LUST_ASK_AGAIN)
    assert not seen[0][1].endswith(LUST_ASK_AGAIN)
    assert photo(h, 102) == compose_lust(SWIM_REPLY)
    assert "selfies_kind_asked_again: night=2026-10-29 guid=102" in caplog.text


def test_a_pairing_that_fails_twice_is_refused(h):
    lust(h, 102)
    ctx = night(h, 0, lusty=[SWIM_AWAY, GYM_AWAY])
    assert ctx.reasoner.calls == 2 and photo(h, 102) is None and ctx.done(STAGE, "102")


# -- the age guard -----------------------------------------------------------------------------

AGE_WORDS = ["school", "her old school", "a schoolgirl look", "a school uniform",
             "a student uniform", "teen", "teens", "teenage", "a teenager", "childlike", "child",
             "children", "kid", "kids", "young-looking", "young looking", "barely legal",
             "lolita", "a little girl", "a cheerleader uniform", "teenaged", "a preteen",
             "pre-teen", "a tween", "youthful", "a young girl", "loli", "underage", "under-age",
             "an adolescent", "adolescence", "jailbait", "baby-faced", "baby faced",
             "a cheerleader", "pigtails", "a pigtail", "braces", "underaged", "pig tails",
             "jail bait", "baby face", "babyface", "brace-face", "braceface", "a schoolboy",
             "a schoolkid", "a high-schooler", "a highschooler", "childish", "kiddie",
             "kiddies", "teeny", "a teenybopper", "pig-tails", "jail-bait", "brace face",
             "under age", "barely 18", "barely eighteen", "just turned 18", "fresh-faced",
             "a young face", "looks young", "younger-looking", "babyish", "schoolgirlish",
             "teenie", "hair in bunches", "pig\u00a0tails", "barely  legal", "young\tgirl",
             "baby - faced", "little-girl", "student\u00a0uniform"]


@pytest.mark.parametrize("text", AGE_WORDS)
def test_the_age_guard_refuses_young_coding_in_every_free_text_field(h, text):
    people = [h.woman(103, "fond", appearance=APPEARANCE), h.persona(101, appearance=APPEARANCE),
              lust(h, 104, age=20), lust(h, 102)]
    for p in people:
        for field in ("setting", "outfit", "expression"):
            out = screen_kind(p, {**PLAIN_KIND, field: text}, ident=IDENT,
                              member_names=["Testb"])
            assert any(m.startswith(f"photo.{field}: age") for m in out), (p.guid, field, text)
    assert AGE_RE.search(text)


def test_the_plain_path_is_unchanged_but_for_the_age_guard(h):
    assert kind.EXPLICIT_RE.pattern == TODAY_EXPLICIT and KIND_PROMPT == TODAY_KIND_PROMPT
    assert kind.KIND_SCHEMA["title"] == "photo"
    for p in (h.woman(103, "fond", appearance=APPEARANCE), h.persona(101, appearance=APPEARANCE)):
        assert screen_kind(p, PLAIN_KIND, ident=IDENT, member_names=["Testb"]) == []
        out = screen_kind(p, {**PLAIN_KIND, "outfit": "a sexy black dress"}, ident=IDENT,
                          member_names=["Testb"])
        assert [m.split(': "')[0] for m in out] == ["photo.outfit: explicit"]


WEAR_WORDS = ["a lace bralette", "bralettes", "a bralet", "bralets", "a satin bustier",
              "a babydoll dress", "a baby-doll top", "a teddy", "teddies", "a bodystocking",
              "a body stocking", "a negligee", "a negligée", "a nightgown", "a nightdress",
              "a nightie", "a silk chemise", "a lace camisole", "pajamas", "a pajama top",
              "pyjamas", "pjs", "sleepwear", "nightwear", "a mesh top", "fishnet tights",
              "fishnets", "a translucent blouse"]


@pytest.mark.parametrize("text", WEAR_WORDS)
def test_the_plain_screen_refuses_underwear_sleepwear_and_see_through_words(h, text):
    assert kind.EXPLICIT_RE.pattern == TODAY_EXPLICIT          # a pure addition beside it
    assert kind.WEAR_RE.search(text) and not kind.EXPLICIT_RE.search(text)
    for p in (h.woman(103, "fond", appearance=APPEARANCE), h.persona(101, appearance=APPEARANCE)):
        for field in ("setting", "outfit", "expression"):
            out = screen_kind(p, {**PLAIN_KIND, field: text}, ident=IDENT,
                              member_names=["Testb"])
            assert any(m.startswith(f"photo.{field}: explicit") for m in out), (p.guid, field)


@pytest.mark.parametrize("reply", [
    {"outfit": "a navy blazer", "setting": "at her office desk"},
    {"outfit": "a hoodie", "setting": "at her kitchen table"},
    {"outfit": "a navy blazer at her office desk"},
    {"outfit": "a grey hoodie", "setting": "holding a teddy bear on the sofa"},
    {"outfit": "a knitted jumper", "setting": "a cafe by the river",
     "expression": "a warm smile"}])
def test_ordinary_plain_kinds_still_pass(h, reply):
    for p in (h.woman(103, "fond", appearance=APPEARANCE), h.persona(101, appearance=APPEARANCE)):
        assert screen_kind(p, {**PLAIN_KIND, **reply}, ident=IDENT, member_names=["Testb"]) == []


@pytest.mark.parametrize("who", ["fond", "drawn", "young", "man", "lust"])
@pytest.mark.parametrize("text", ["a low-cut top that shows some cleavage", "a sexy black dress",
                                  "a red bikini", "a sheer blouse"])
def test_free_text_always_meets_todays_screen(h, who, text):
    # the lust style writes no free text; free text, whoever it is for, is never screened softer
    p = {"fond": lambda: h.woman(103, "fond", appearance=APPEARANCE),
         "drawn": lambda: h.woman(103, "drawn", appearance=APPEARANCE),
         "young": lambda: lust(h, 103, age=20),
         "man": lambda: h.persona(101, appearance=APPEARANCE),
         "lust": lambda: lust(h, 103)}[who]()
    data = {**PLAIN_KIND, "setting": "on the beach at sunset", "outfit": text}
    out = screen_kind(p, data, ident=IDENT, member_names=["Testb"])
    assert any(m.startswith("photo.outfit: explicit") for m in out)


# -- over time: the re-choice --------------------------------------------------------------

def test_a_woman_who_became_lust_gets_one_new_kind_under_the_lust_rules(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND])
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN
    restance(h, 102, "lust")
    assert needs_rechoice(h.store, load_persona(h.store, 102))
    ctx = ctx_for(h, 1, lusty=[LUST_REPLY])
    want = expected(ctx, 102, LUST_PROMPT, KIND_PROMPT_LUST)
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert [(s, sc) for s, _, sc in seen] == [(want, LUST_SCHEMA)]
    assert photo(h, 102) == LUST_PHOTO and style(h, 102) == LUST
    assert not needs_rechoice(h.store, load_persona(h.store, 102))
    assert night(h, 2, lusty=[LUST_REPLY]).reasoner.calls == 0                  # never twice
    assert photo(h, 102) == LUST_PHOTO


def test_a_rechoice_into_lust_queues_a_fresh_set_and_leaves_his_waiting_one(h):
    from selfies.fakes import ready
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND, PLAIN_KIND])
    ready(h.store, h.settings.data_dir, 102, 1)          # plain candidates wait for his choice
    restance(h, 102, "lust")
    restance(h, 103, "lust")                             # never had a set
    night(h, 1, lusty=[LUST_REPLY, LUST_REPLY])
    assert queued(h, 102) == [(2, "more")] and queued(h, 103) == [(1, "first")]
    waiting = h.store.conn.execute("SELECT decided FROM selfie_set WHERE guid=102 AND set_no=1"
                                   ).fetchone()[0]
    assert waiting is None                               # still his to choose from


def test_only_a_rechoice_into_lust_queues_a_set(h):
    lust(h, 102)                                         # a first lust kind: the stage queues it
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND], lusty=[LUST_REPLY])
    assert queued(h, 102) == [] and queued(h, 103) == []
    restance(h, 102, "drawn")                            # out of lust: a plain re-choice
    night(h, 1, plain=[PLAIN_KIND])
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and queued(h, 102) == []
    restance(h, 103, "lust")
    from leeroy_mind.selfies.store import ensure_first_sets
    ensure_first_sets(h.store, [103], now=h.T)           # one already queued: no second
    night(h, 2, lusty=[LUST_REPLY])
    assert queued(h, 103) == [(1, "first")]


def test_the_owners_rebalance_into_lust_counts_too(h):
    h.woman(102, "drawn", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND])
    restance(h, 102, "lust", source="owner")  # rebalance-stances saves as the owner
    assert night(h, 1, lusty=[LUST_REPLY]).reasoner.calls == 1
    assert photo(h, 102) == LUST_PHOTO


def test_a_refused_rechoice_keeps_her_kind_and_is_asked_again_the_next_night(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND])
    restance(h, 102, "lust")
    first = night(h, 1, lusty=[SWIM_AWAY, GYM_AWAY])
    run(stage_photo(first))
    assert first.reasoner.calls == 2 and photo(h, 102) == ProfilePhoto(**PLAIN_KIND)
    assert style(h, 102) == PLAIN and queued(h, 102) == []
    assert night(h, 2, lusty=[LUST_REPLY]).reasoner.calls == 1
    assert photo(h, 102) == LUST_PHOTO


def test_a_kind_set_by_hand_is_never_chosen_again(h):
    lust(h, 102)
    by_hand(h, 102, HAND)
    assert style(h, 102) is None
    for n in range(2):
        assert night(h, n, lusty=[LUST_REPLY]).reasoner.calls == 0
    assert photo(h, 102) == HAND and queued(h, 102) == []


def test_a_pass_kind_the_owner_changed_by_hand_is_never_chosen_again(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND])
    by_hand(h, 102, HAND)
    restance(h, 102, "lust")
    assert style(h, 102) is None
    assert night(h, 1, lusty=[LUST_REPLY]).reasoner.calls == 0
    assert photo(h, 102) == HAND


def test_a_kind_set_by_hand_while_the_rechoice_ran_is_kept(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND])
    restance(h, 102, "lust")

    def meanwhile(text):
        by_hand(h, 102, HAND)
        return LUST_REPLY
    ctx = h.ctx(default=meanwhile, night=NIGHTS[1])
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 1 and photo(h, 102) == HAND and style(h, 102) is None
    assert queued(h, 102) == []
    assert night(h, 2, lusty=[LUST_REPLY]).reasoner.calls == 0


def _legacy(h, guid, *, ran: bool):
    """A kind from before the style records: saved with the persona and, when `ran`, the photo
    pass's unit for her on an earlier night, as the pass left them."""
    _save(h, guid, replace(load_persona(h.store, guid), photo=ProfilePhoto(**PLAIN_KIND)),
          "generated")
    if ran:
        with h.store.transaction():
            h.store.conn.execute("INSERT INTO night_step(night, stage, unit, finished) "
                                 "VALUES ('2026-10-20', ?, ?, ?)", (STAGE, str(guid), h.T))


def test_a_kind_from_before_the_records_is_the_pass_plain_one_when_the_pass_handled_her(h):
    lust(h, 102)
    _legacy(h, 102, ran=True)
    lust(h, 103)
    _legacy(h, 103, ran=False)                               # never the pass's: by hand
    assert style(h, 102) == PLAIN and style(h, 103) is None
    assert night(h, 0, lusty=[LUST_REPLY, LUST_REPLY]).reasoner.calls == 1
    assert photo(h, 102) == LUST_PHOTO and style(h, 102) == LUST
    assert photo(h, 103) == ProfilePhoto(**PLAIN_KIND) and style(h, 103) is None
    assert night(h, 1, lusty=[LUST_REPLY]).reasoner.calls == 0


def test_the_first_night_records_the_old_kinds_so_a_later_hand_change_is_kept(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    _legacy(h, 102, ran=True)
    h.persona(101, appearance=APPEARANCE)
    _legacy(h, 101, ran=True)
    assert night(h, 0).reasoner.calls == 0                  # nothing due: only the records
    assert style(h, 102) == PLAIN and style(h, 101) == PLAIN
    by_hand(h, 102, HAND)
    restance(h, 102, "lust")
    assert style(h, 102) is None
    assert night(h, 1, lusty=[LUST_REPLY]).reasoner.calls == 0
    assert photo(h, 102) == HAND


def test_a_woman_who_left_lust_gets_one_kind_under_the_plain_rules_again(h):
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    assert style(h, 102) == LUST
    restance(h, 102, "drawn")
    ctx = ctx_for(h, 1, plain=[PLAIN_KIND])
    want = expected(ctx, 102, STAGE, TODAY_KIND_PROMPT)
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert [s for s, _, _ in seen] == [want]
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN
    assert night(h, 2, plain=[PLAIN_KIND]).reasoner.calls == 0


def test_men_and_women_who_stay_out_of_lust_are_never_asked_again(h):
    h.persona(101, appearance=APPEARANCE)
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "drawn", appearance=APPEARANCE)
    lust(h, 104, age=20)
    assert night(h, 0, plain=[PLAIN_KIND] * 4).reasoner.calls == 4
    for n in (1, 2):
        assert night(h, n, plain=[PLAIN_KIND] * 4).reasoner.calls == 0
    for g in (101, 102, 103, 104):
        assert photo(h, g) == ProfilePhoto(**PLAIN_KIND) and style(h, g) == PLAIN


def test_the_record_holds_the_style_and_the_kind_as_saved_and_no_name(h):
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    raw = h.store.get_meta("selfies:photo-style:102")
    assert raw is not None and '"lust"' in raw
    assert "Belra" not in raw and IDENT.real_name not in raw and "dress" not in raw
    assert asdict(photo(h, 102)) == asdict(LUST_PHOTO)


def test_the_log_names_a_rechoice_that_landed_by_id_only(h, caplog):
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND, PLAIN_KIND])
    restance(h, 102, "lust")
    restance(h, 103, "lust")

    def reply(text):
        if "Coros" in text:
            by_hand(h, 103, HAND)                    # his hand wins: no re-choice landed
        return LUST_REPLY
    with caplog.at_level(logging.DEBUG):
        run(stage_photo(h.ctx(default=reply, night=NIGHTS[1])))
    assert "selfies_kind_rechosen: night=2026-10-30 guid=102 style=lust" in caplog.text
    assert "guid=103 style" not in caplog.text and "rechosen=1" in caplog.text
    for s in (LUST_PHOTO.outfit, LUST_PHOTO.setting, "Belra", "Coros"):
        assert s not in caplog.text


def test_the_record_ignores_a_field_added_to_the_photo_later(h):
    @dataclass(frozen=True)
    class Later(ProfilePhoto):  # a ProfilePhoto with one more field, as a later schema may add
        lighting: str = "golden hour"
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    fields = asdict(LUST_PHOTO)
    assert kind._digest(Later(**fields, lighting="soft window light")) == kind._digest(LUST_PHOTO)
    later = replace(load_persona(h.store, 102), photo=Later(**fields))
    assert chosen_style(h.store, later) == LUST and not needs_rechoice(h.store, later)
    changed = replace(later, photo=Later(**{**fields, "outfit": "a green sweater"}))
    assert chosen_style(h.store, changed) is None


def test_an_unreadable_persona_is_skipped_and_the_others_get_their_kinds(h):
    lust(h, 102)
    h.woman(103, "fond", appearance=APPEARANCE)
    _legacy(h, 103, ran=True)
    with h.store.transaction():
        h.store.conn.execute("UPDATE persona_core SET json = ? WHERE guid = ?",
                             ('{"schema": 1}', 103))
    ctx = ctx_for(h, 0, lusty=[LUST_REPLY])
    assert 103 in ctx.members(include_away=True)
    others = frozenset({"coach", "realm"})
    assert budget._counts(ctx, set(), others | {"photo"})["photo"] == 1
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 1 and photo(h, 102) == LUST_PHOTO
    assert h.store.get_meta("selfies:photo-style:103") is None


def test_the_counter_counts_the_rechoices(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, plain=[PLAIN_KIND, PLAIN_KIND])
    restance(h, 102, "lust")
    others = frozenset({"coach", "realm"})
    assert budget._counts(h.ctx(night=NIGHTS[1]), set(), others | {"photo"})["photo"] == 1


# -- at render time --------------------------------------------------------------------------

def test_the_render_follows_the_kinds_style_and_her_stance_at_render_time(h, tmp_path,
                                                                         caplog):
    from leeroy_mind.selfies.prompt import LUST_NEGATIVE, NEGATIVE
    from leeroy_mind.selfies.stage import make_stage
    from leeroy_mind.selfies.worker import SelfieWorker
    from selfies.fakes import SELFIE_CFG, fake_command

    for g in (102, 103, 105):
        lust(h, g)
    lust(h, 104)
    by_hand(h, 104, ProfilePhoto(**PLAIN_KIND))          # a kind the lust rules did not compose
    night(h, 0, lusty=[LUST_REPLY] * 3)
    assert {g: style(h, g) for g in (102, 103, 104, 105)} == {102: LUST, 103: LUST, 104: None,
                                                              105: LUST}
    from leeroy_mind.selfies.store import ensure_first_sets
    ensure_first_sets(h.store, [102, 103, 104, 105], now=h.T)    # queued while all could render
    restance(h, 103, "drawn")                            # she left lust before the render
    young = replace(APPEARANCE, face=("a youthful face", "freckles"))
    _save(h, 105, replace(load_persona(h.store, 105), appearance=young), "generated")
    seen: dict[str, set] = {}
    cmd, env = fake_command(tmp_path)

    class Seen(SelfieWorker):
        async def render(self, req, *, rid=None):
            seen.setdefault(rid.split(":")[0], set()).add((req.negative, req.phrases))
            return await super().render(req, rid=rid)
    with caplog.at_level(logging.INFO):
        run(make_stage(SELFIE_CFG, worker=lambda: Seen(cmd, env=env))(h.ctx(night=NIGHTS[1])))
    negatives = {g: {n for n, _ in v} for g, v in seen.items()}
    assert negatives == {"102": {LUST_NEGATIVE}, "104": {NEGATIVE}}   # 103 and 105 wait
    joined = ("wearing a fitted little black dress, the mirror in her hallway", 0)
    assert all(joined in ph for _, ph in seen["102"])
    assert not any(joined in ph for _, ph in seen["104"])
    for g in (103, 105):
        assert queued(h, g) == [(1, "first")]            # their sets wait for the re-choice
        assert f"selfies_set_held: night=2026-10-30 guid={g} set=1 reason=restyle" in caplog.text
    assert "youthful" not in caplog.text and "fitted little" not in caplog.text


@pytest.mark.parametrize("looks", YOUNG_LOOKS)
def test_a_lust_woman_whose_looks_code_young_gets_a_plain_kind(h, looks):
    lust(h, 102, appearance=replace(APPEARANCE, **looks))
    ctx = ctx_for(h, 0, plain=[PLAIN_KIND])
    want = expected(ctx, 102, STAGE, TODAY_KIND_PROMPT)
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert [(s, sc) for s, _, sc in seen] == [(want, kind.KIND_SCHEMA)]
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN


def test_a_lust_kind_whose_looks_turn_young_is_held_then_chosen_again_plain(h):
    from leeroy_mind.selfies.prompt import LUST_HELD, renderable
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    assert style(h, 102) == LUST
    young = replace(APPEARANCE, hair_style="two pigtails")
    _save(h, 102, replace(load_persona(h.store, 102), appearance=young), "generated")
    p = load_persona(h.store, 102)
    assert renderable(p, chosen_style(h.store, p)) == LUST_HELD and needs_rechoice(h.store, p)
    ctx = ctx_for(h, 1, plain=[PLAIN_KIND])
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert [sc for _, _, sc in seen] == [kind.KIND_SCHEMA]     # the plain rules, screened
    p = load_persona(h.store, 102)
    assert p.photo == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN
    assert renderable(p, chosen_style(h.store, p)) is None and queued(h, 102) == []


def test_a_lust_kind_waits_after_she_left_lust_until_her_rechoice_lands(h):
    from leeroy_mind.selfies.prompt import LUST_HELD, renderable
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    restance(h, 102, "fond")
    p = load_persona(h.store, 102)
    assert renderable(p, chosen_style(h.store, p)) == LUST_HELD
    night(h, 1, plain=[{**PLAIN_KIND, "outfit": "lace lingerie"}])     # refused: still waits
    p = load_persona(h.store, 102)
    assert renderable(p, chosen_style(h.store, p)) == LUST_HELD
    night(h, 2, plain=[PLAIN_KIND])
    p = load_persona(h.store, 102)
    assert renderable(p, chosen_style(h.store, p)) is None and p.photo == ProfilePhoto(**PLAIN_KIND)


# -- names ------------------------------------------------------------------------------------

def test_a_composed_part_that_holds_her_name_is_refused_and_asked_again(h):
    summer = lust(h, 102, real_name="Summer")
    park = {**LUST_REPLY, "setting": "a park in summer"}
    assert screen_lust(summer, park) == ["photo.setting: shares her name"]
    olive = lust(h, 103, real_name="Olive")
    assert screen_lust(olive, {**LUST_REPLY, "colour": "olive"}) == \
        ["photo.outfit: shares her name"]
    assert screen_lust(olive, LUST_REPLY) == [] and screen_lust(summer, LUST_REPLY) == []
    ctx = ctx_for(h, 0, lusty=[park, LUST_REPLY, {**LUST_REPLY, "colour": "olive"}, LUST_REPLY])
    seen = calls(ctx)
    ctx.settings = replace(ctx.settings, parallel=1)
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 4 and sum(u.endswith(LUST_ASK_AGAIN) for _, u, _ in seen) == 2
    assert photo(h, 102) == LUST_PHOTO and photo(h, 103) == LUST_PHOTO


def test_a_name_clash_twice_is_refused(h):
    lust(h, 102, real_name="Summer")
    park = {**LUST_REPLY, "setting": "a park in summer"}
    ctx = night(h, 0, lusty=[park, park])
    assert ctx.reasoner.calls == 2 and photo(h, 102) is None


def test_no_composed_lust_kind_loses_its_joined_phrase_to_the_name_filter(h):
    from leeroy_mind.selfies.prompt import build_prompt
    for name in ("Summer", "Olive", "Coral", "Gold", "Teal", "Plum", "Pink"):
        p = lust(h, 102, real_name=name)
        for o in LUST_OUTFITS:
            for c in LUST_COLOURS:
                r = _reply(o, c, setting=("a park in summer" if o not in SWIM_OUTFITS
                                          and o not in GYM_OUTFITS else None))
                if screen_lust(p, r):
                    continue                             # refused: never composed
                made = replace(p, photo=compose_lust(r))
                spec = build_prompt(made, kind_style=LUST)
                assert sum(1 for t, pr in spec.phrases if pr == 0 and t.startswith("wearing ")) \
                    == 1, (name, o, c)


# -- round 6: spacing, new variants, a name that would drop the joined phrase ---------------------

SPACED_LOOKS = ({"hair_style": "pig\u00a0tails"}, {"hair_style": "pig  tails"},
                {"hair_style": "pig\ttails"}, {"hair_style": "pig-tails"},
                {"face": ("baby\u00a0faced",)}, {"face": ("a  young\tface",)},
                {"face": ("fresh-faced",)}, {"hair_style": "in bunches"},
                {"skin_tone": "babyish"}, {"eye_colour": "looks young"},
                {"face": ("barely 18",)}, {"face": ("just turned eighteen",)},
                {"face": ("younger-looking",)}, {"face": ("schoolgirlish",)},
                {"face": ("jail-bait",)}, {"face": ("brace face",)}, {"face": ("under age",)},
                {"face": ("teenie",)})


@pytest.mark.parametrize("looks", SPACED_LOOKS)
def test_young_coded_looks_count_whatever_their_spacing(h, looks):
    from leeroy_mind.selfies.style import looks_age_coded
    p = replace(lust(h, 103), appearance=replace(APPEARANCE, **looks))
    assert looks_age_coded(p) and not suggestive(p) and style_of(p) == PLAIN, looks


def test_adult_looks_with_odd_spacing_keep_the_lust_style(h):
    from leeroy_mind.selfies.style import looks_age_coded
    p = replace(lust(h, 103), appearance=replace(APPEARANCE, hair_style="loose\u00a0 waves",
                                                 face=("a  beauty\tmark",)))
    assert not looks_age_coded(p) and suggestive(p)


def test_a_lust_kind_whose_name_filter_would_drop_its_phrase_is_held_and_chosen_again(h, caplog):
    from leeroy_mind.selfies.prompt import LUST_NAMED, build_prompt, lust_phrase, renderable
    from leeroy_mind.selfies.stage import wants_gpu
    from leeroy_mind.selfies.store import ensure_first_sets
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    assert photo(h, 102) == LUST_PHOTO
    _save(h, 102, replace(load_persona(h.store, 102), identity=replace(
        load_persona(h.store, 102).identity, real_name="Mirror")), "owner")   # renamed since
    p = load_persona(h.store, 102)
    assert "mirror" in lust_phrase(p.photo) and renderable(p, LUST) == LUST_NAMED
    with pytest.raises(ValueError, match="shares her name"):
        build_prompt(p, kind_style=LUST)
    ensure_first_sets(h.store, [102], now=h.T)
    assert not wants_gpu(ctx_for(h, 1))                 # a held set takes no GPU
    assert needs_rechoice(h.store, p) and style(h, 102) == LUST
    wine = {**LUST_REPLY, "setting": "a wine bar"}
    ctx = ctx_for(h, 1, lusty=[LUST_REPLY, wine])       # the mirror clashes: asked once more
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert len(seen) == 2 and seen[1][1].endswith(LUST_ASK_AGAIN)
    p = load_persona(h.store, 102)
    assert p.photo == compose_lust(wine) and renderable(p, LUST) is None
    assert wants_gpu(ctx_for(h, 2))
    assert "Mirror" not in caplog.text


def test_a_name_such_as_wearing_refuses_every_lust_kind(h):
    p = lust(h, 102, real_name="Wearing")
    for r in (LUST_REPLY, SWIM_REPLY, GYM_REPLY):
        assert screen_lust(p, r) == ["photo.outfit: shares her name"]


def test_the_stage_logs_a_name_hold_by_guid_only(h, tmp_path, caplog):
    from leeroy_mind.selfies.stage import make_stage
    from leeroy_mind.selfies.store import ensure_first_sets
    from selfies.fakes import SELFIE_CFG, fake_worker
    lust(h, 102)
    night(h, 0, lusty=[LUST_REPLY])
    ensure_first_sets(h.store, [102], now=h.T)
    _save(h, 102, replace(load_persona(h.store, 102), identity=replace(
        load_persona(h.store, 102).identity, real_name="Hallway")), "owner")
    with caplog.at_level(logging.INFO):
        run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path))(h.ctx(night=NIGHTS[1])))
    assert "selfies_set_held: night=2026-10-30 guid=102 set=1 reason=name" in caplog.text
    assert queued(h, 102) == [(1, "first")] and "Hallway" not in caplog.text
