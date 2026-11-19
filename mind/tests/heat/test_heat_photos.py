"""The photo tiers: subsets of the vetted lists and nothing else. P4 is the lists as they are,
so heat 9 composes exactly what the code composed before; no tier adds an outfit, setting or
pose; the age rules are decided before heat; and lowering heat only filters what is newly
composed and what is sent. Every person here is invented; no model, GPU or worker."""
import itertools
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind import heat as H
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat import photos
from leeroy_mind.heat.config import HeatConfig
from leeroy_mind.heat.table import P0, P1, P2, P3, P4, ROWS
from leeroy_mind.selfies import kind, scene, varied, words_scene
from leeroy_mind.selfies.kind import (EXPLICIT_RE, LUST_OUTFITS, LUST_SCHEMA, LUST_SETTINGS,
                                      VARIED_LOOKS, VARIED_OUTFITS, VARIED_VIEWS, WEAR_RE)
from leeroy_mind.selfies.style import AGE_RE, SUGGESTIVE_MIN_AGE, suggestive
from nightly.test_night_enrich import APPEARANCE

TIERS = (P0, P1, P2, P3, P4)


class Tier:
    """heat.live's HeatLive as the photo composers read it: one photo tier for everyone."""

    def __init__(self, tier):
        self.tier, self.config = tier, HeatConfig(enabled=True)

    def photo_tier(self, p):
        return self.tier

    def offers(self, p):
        return ROWS[9].offers


@pytest.fixture
def at():
    def set_tier(tier):
        heat_live._LIVE = None if tier is None else Tier(tier)
    return set_tier


def lust(guid=104, **identity):
    p = make_persona(guid, "Dunna", player_gender="female", stance="lust", **identity)
    return replace(p, appearance=APPEARANCE)


# -- the lists ------------------------------------------------------------------------------------

def test_every_tier_is_a_subset_of_the_vetted_lists_and_of_the_next():
    every_outfit = set(LUST_OUTFITS) | set(VARIED_OUTFITS)
    for a, b in zip(TIERS, TIERS[1:]):
        la, lb = photos.listing(a), photos.listing(b)
        for key, vetted in (("outfits", every_outfit), ("settings", set(LUST_SETTINGS)),
                            ("views", set(VARIED_VIEWS))):
            assert set(la[key]) <= set(lb[key]) <= vetted, (a, key)
            assert len(la[key]) == len(set(la[key]))
        assert set(photos.outfits(tuple(LUST_OUTFITS), a)) <= set(LUST_OUTFITS)
        assert set(photos.settings(LUST_SETTINGS, a)) <= set(LUST_SETTINGS)
    assert set(photos.listing(P0)["outfits"]) and set(photos.listing(P0)["settings"])


def test_p4_is_exactly_the_lists_as_they_are():
    top = photos.listing(P4)
    assert set(top["outfits"]) == set(LUST_OUTFITS) | set(VARIED_OUTFITS)
    assert set(top["settings"]) == set(LUST_SETTINGS)
    assert tuple(top["views"]) == VARIED_VIEWS
    assert photos.categories(P4) == kind.CATEGORIES and photos.views(P4) == VARIED_VIEWS
    pool = tuple(LUST_OUTFITS)
    assert photos.outfits(pool, P4) is pool and photos.settings(LUST_SETTINGS, P4) is LUST_SETTINGS
    assert photos.narrow_schema(LUST_SCHEMA, P4) is LUST_SCHEMA
    assert photos.narrow_schema(scene.SCENE_SCHEMA_LUST, P4) is scene.SCENE_SCHEMA_LUST
    for category in kind.CATEGORIES:
        assert photos.varied_looks(category, P4) == VARIED_LOOKS[category]
    assert photos.clamp(99) == P4 and photos.clamp(None) == P4  # anything unreadable: no more


def test_no_tier_holds_a_string_the_lists_do_not_and_every_one_passes_the_screens():
    """Nothing is added: each outfit and setting of each tier is a list value, and (as the lists'
    own tests check) names no underwear, sleepwear or age word."""
    for tier in TIERS:
        row = photos.listing(tier)
        for choice in row["outfits"]:
            template = {**LUST_OUTFITS, **VARIED_OUTFITS}[choice]
            assert not WEAR_RE.search(template) and not AGE_RE.search(template)
        for place in row["settings"]:
            assert place in LUST_SETTINGS
            assert not AGE_RE.search(place) and not EXPLICIT_RE.search(place)


def test_swimwear_only_at_p4_and_gym_wear_only_from_p3():
    for tier in TIERS:
        row = photos.listing(tier)
        assert bool(set(row["outfits"]) & kind.SWIM_OUTFITS) == (tier == P4)
        assert bool(set(row["outfits"]) & kind.VARIED_GYM_OUTFITS) == (tier >= P3)
        assert bool(set(row["settings"]) & set(kind.WATER_SETTINGS)) == (tier == P4)
        assert bool(set(row["settings"]) & set(kind.GYM_SETTINGS)) == (tier >= P3)
        assert ("behind" in row["views"]) == (tier >= P3)
    assert photos.categories(P0) == ("casual",) and photos.categories(P2) == ("casual",
                                                                               "going_out")


def test_the_proposed_split_of_the_dressed_outfits():
    dressed = {kind._choice(t) for t in kind._DRESSED}
    covered = {o for o in dressed if photos.OUTFIT_TIER[o] <= P1}
    assert covered == {"fitted blazer dress", "cropped cardigan and wide-leg trousers",
                       "square-neck top and a pencil skirt", "satin midi dress",
                       "off-the-shoulder sweater dress", "sweetheart-neckline sundress",
                       "off-the-shoulder top and jeans"}
    assert {o for o in dressed if photos.OUTFIT_TIER[o] == P2} == dressed - covered
    assert len(dressed) == 22 and all(photos.OUTFIT_TIER[o] <= P2 for o in dressed)


# -- the schemas the model picks from -----------------------------------------------------------------

@pytest.mark.parametrize("tier", TIERS[:-1])
def test_a_narrowed_schema_only_removes_picks_and_keeps_every_pairing_possible(tier):
    for base in (LUST_SCHEMA, scene.SCENE_SCHEMA_LUST):
        narrow = photos.narrow_schema(base, tier)
        props, was = narrow["properties"], base["properties"]
        assert set(props) == set(was)
        for key in props:
            if key in ("outfit", "setting"):
                assert set(props[key]["enum"]) < set(was[key]["enum"]) and props[key]["enum"]
            else:
                assert props[key] == was[key]  # kind, framing, colour, expression: untouched
        assert not set(props["outfit"]["enum"]) & kind.SWIM_OUTFITS
        assert not set(props["setting"]["enum"]) & set(kind.SWIM_ONLY_SETTINGS)
    p = lust()
    for outfit, place in itertools.product(photos.narrow_schema(LUST_SCHEMA, tier)["properties"][
            "outfit"]["enum"], photos.narrow_schema(LUST_SCHEMA, tier)["properties"]["setting"][
            "enum"]):
        data = dict(kind="selfie", framing="upper_body", setting=place, outfit=outfit,
                    colour="navy", expression="a playful smile")
        problems = kind.screen_lust(p, data)  # the screens still run, and still pass
        if outfit in kind.GYM_OUTFITS:
            assert (problems == []) == (place in kind.GYM_SETTINGS)
        else:
            assert problems == [], (outfit, place, problems)
        assert photos.picks_ok(data, tier)
    assert not photos.picks_ok(dict(outfit="string bikini", setting="the beach at sunset"), tier)


def test_scene_schema_is_the_same_object_with_heat_off_and_at_p4(at):
    p = lust()
    assert suggestive(p)
    off = scene.scene_schema(p)
    at(P4)
    assert scene.scene_schema(p) is off
    at(P1)
    narrow = scene.scene_schema(p)
    assert set(narrow["properties"]["outfit"]["enum"]) < set(off["properties"]["outfit"]["enum"])
    plain = replace(make_persona(101, "Arkon"), appearance=APPEARANCE)
    at(P0)
    plain_low = scene.scene_schema(plain)
    at(None)
    assert scene.scene_schema(plain) == plain_low  # the plain rules are not heat's


# -- varied sets --------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [str(n) for n in range(12)])
def test_a_varied_set_at_heat_nine_is_the_set_composed_without_heat(at, seed):
    p = lust()
    before = varied.compose(p, seed)
    at(P4)
    assert varied.compose(p, seed) == before
    assert varied.compose(p, seed, tier=P4) == before


@pytest.mark.parametrize("tier,seed", list(itertools.product(TIERS[:-1], ("a", "b", "c", "d"))))
def test_a_varied_set_below_p4_stays_inside_its_tier_and_passes_the_screens(tier, seed):
    p = lust()
    looks = varied.compose(p, seed, tier=tier)
    assert looks is not None and len(looks) == 4
    for look in looks:
        assert photos.look_tier(look) <= tier
        assert varied.screen(p, look) == [] and varied._valid(look)
        assert look.outfit not in kind.SWIM_OUTFITS
        assert look.category in photos.categories(tier) and look.view in photos.views(tier)
    assert len({x.colour for x in looks}) == 4


# -- a photo read from his words --------------------------------------------------------------------------

@pytest.mark.parametrize("code,needs", [("swimwear", P4), ("gym", P3), ("going_out", P2)])
def test_wearing_resolves_inside_her_tier(at, code, needs):
    p = lust()
    place, framing = ("the beach", "handheld_selfie") if code == "swimwear" else (
        "the gym floor" if code == "gym" else "a bar", "mirror_selfie")
    at(None)
    today = words_scene.resolve_wearing(code, p, place=place, framing=framing, block=None, seed=7)
    assert today[1] == code
    at(P4)
    assert words_scene.resolve_wearing(code, p, place=place, framing=framing, block=None,
                                       seed=7) == today
    at(needs - 1)
    phrase, got, fallback = words_scene.resolve_wearing(code, p, place=place, framing=framing,
                                                        block=None, seed=7)
    assert (got, fallback) == ("casual", "wearing:heat")
    assert photos.OUTFIT_TIER[photos.choice_of(phrase)] <= needs - 1


def test_every_listed_outfit_a_request_draws_is_within_the_tier(at):
    p = lust()
    for tier, code, seed in itertools.product(TIERS, ("casual", "pyjamas"), range(6)):
        at(tier)
        phrase = words_scene._listed(code, p, seed)
        assert photos.OUTFIT_TIER[photos.choice_of(phrase)] <= tier
    plain = replace(make_persona(101, "Arkon"), appearance=APPEARANCE)
    at(P0)
    assert words_scene._listed("casual", plain, 1) == words_scene.PLAIN_WEAR["casual"]


# -- which tier a stored photo is, and what is sent -----------------------------------------------------------

def test_a_stored_scenes_tier():
    lust_scene = dict(style="lust", outfit="a red halter bikini", setting="the beach at sunset")
    assert photos.scene_tier(lust_scene) == P4
    assert photos.scene_tier(dict(style="lust", outfit="a navy satin midi dress",
                                  setting="her living room couch")) == P0
    assert photos.scene_tier(dict(style="lust", outfit="a navy satin midi dress",
                                  setting="a nightclub")) == P2
    assert photos.scene_tier(dict(style="lust", outfit="a black sports top and leggings",
                                  setting="the gym")) == P3
    assert photos.scene_tier(dict(outfit="a cotton shirt and trousers")) == P0  # the plain style
    assert photos.scene_tier(dict(style="lust", outfit="something else", setting="x")) == P4
    assert photos.scene_tier(None) == P4 and photos.scene_tier({"style": "lust"}) == P4
    words = dict(style="lust", outfit="medical scrubs", setting="a staff bathroom",
                 words={"wearing": "work_uniform"})
    assert photos.scene_tier(words) == P0
    assert photos.scene_tier(dict(words, outfit="a red halter bikini",
                                  words={"wearing": "swimwear"})) == P4
    for choice, template in {**LUST_OUTFITS, **VARIED_OUTFITS}.items():
        assert photos.choice_of(kind.outfit_phrase(choice, "emerald", {choice: template})) \
            == choice


def test_sending_passes_over_a_photo_above_the_tier_and_never_with_heat_off(at):
    p = lust()
    swim = dict(style="lust", outfit="a red halter bikini", setting="the beach at sunset")
    dressed = dict(style="lust", outfit="a navy satin midi dress", setting="a cafe terrace")
    at(None)
    assert heat_live.sends(p, swim) and heat_live.offers_of(p) == 1.0
    at(P4)
    assert heat_live.sends(p, swim) and heat_live.sends(p, dressed)
    at(P2)
    assert not heat_live.sends(p, swim) and heat_live.sends(p, dressed)
    at(P4)  # raising the heat makes the stock eligible again: nothing was expired or deleted
    assert heat_live.sends(p, swim)


def test_the_sender_filters_without_expiring_and_the_request_stock_too():
    """Ruling 4: a lower heat never hides, expires or deletes: the two send paths only ask
    heat_live.sends, beside (never inside) the checks that expire a photo."""
    import inspect

    from leeroy_mind.photos import request_stock, send
    fresh = inspect.getsource(send.PhotoSender._fresh)
    assert "heat" not in fresh and "snaps.expire" in fresh
    offer = inspect.getsource(send.PhotoSender.offer)
    assert offer.count("heat_live.sends(") == 2 and "heat_live.offers_of(" in offer
    safe = inspect.getsource(request_stock._safe_snap)
    assert "heat_live.sends(" in safe and "heat_live.sends_tier(" in safe
    assert "heat_live.sends_tier(" in inspect.getsource(request_stock._candidates)
    for module in (photos, heat_live):
        source = inspect.getsource(module)
        assert "nsfw" not in source.replace("NSFW gate", "") and "unlink" not in source
        assert "expire(" not in source and "DELETE" not in source


# -- the fixed limits -----------------------------------------------------------------------------------

def test_the_age_rules_are_decided_before_heat():
    """A lust woman under the lust style's age, or with age-coded looks, has the plain style: P0
    at every heat and on a no-limits evening."""
    young = lust(age=SUGGESTIVE_MIN_AGE - 1)
    coded = replace(lust(), appearance=replace(APPEARANCE, hair_style="in pigtails"))
    assert AGE_RE.search("in pigtails")
    for p in (young, coded):
        member = heat_live.member_of(p)
        assert not suggestive(p) and not member.lust_style
        assert member.photo_reason == "age_guard"
        for heat, session in itertools.product(range(11), (None, "open")):
            assert H.resolve(heat, member, session=session).photo == P0
    grown = heat_live.member_of(lust(age=SUGGESTIVE_MIN_AGE))
    assert grown.lust_style and H.resolve(10, grown).photo == P4
    assert heat_live.member_of(make_persona(101, "Arkon")).photo_reason == "plain_style"


def test_heat_touches_no_screen_gate_or_negative():
    """By construction: the heat package names none of the screens, the gate or the negatives,
    and the composers reach it only through photo_tier, narrow_schema, the list filters and
    sends."""
    import inspect

    from leeroy_mind.heat import guard, overlays, resolve, session, table
    for module in (photos, heat_live, resolve, table, session, overlays, guard):
        source = inspect.getsource(module)
        for name in ("nsfw_max", "EXPLICIT_RE", "WEAR_RE", "NEGATIVE", "SEND_MIN_AGE",
                     "screen_lust", "screen_scene", "screen_kind"):
            assert name not in source, (module.__name__, name)
    assert "AGE_RE" not in inspect.getsource(photos)
    live = inspect.getsource(heat_live)  # the age rule is read, never set: style.suggestive
    assert "suggestive(p)" in live and "SUGGESTIVE_MIN_AGE" in live
    assert SUGGESTIVE_MIN_AGE == 25


# -- main's photo audit (#362): review of #373, round 2 ---------------------------------------------

def test_the_audits_settings_are_tiered_and_the_retired_ones_read_p4():
    from leeroy_mind.selfies import safe
    assert {photos.SETTING_TIER[s] for s in safe.NEW_SETTINGS} == {P0}
    for retired in safe.RETIRED_SETTINGS:  # in no tier: a stored scene naming one fails closed
        assert retired not in photos.SETTING_TIER
        assert photos.scene_tier(dict(style="lust", outfit="satin midi dress",
                                      setting=retired)) == P4


def test_a_prompt_is_read_in_the_words_it_was_rendered_with_then_and_now():
    """prompt_tier reads a setting as a prompt says it (safe.setting_words: "a garden party"
    renders as "a summer garden") and an outfit in the words of before the audit
    (kind.OLD_TEMPLATES) and after it."""
    from leeroy_mind.selfies import safe
    templates = {**kind.LUST_OUTFITS, **kind.VARIED_OUTFITS}
    couch = "her living room couch"
    for place, tier in photos.SETTING_TIER.items():
        text = f"a woman, wearing a navy satin midi dress, {safe.setting_words(place)}, a smile"
        assert photos.prompt_tier(text) == tier, place
    for choice in templates:
        olds = (kind.OLD_TEMPLATES[choice],) if choice in kind.OLD_TEMPLATES else ()
        for template in (templates[choice], *olds):
            text = f"a woman, wearing a {template.format(colour='red')}, {couch}, a smile"
            assert photos.prompt_tier(text) == photos.OUTFIT_TIER[choice], (choice, template)
    assert set(kind.OLD_TEMPLATES) <= set(photos.OUTFIT_TIER)


@pytest.mark.parametrize("share", (0.0, 0.25, 0.5, 0.75, 1.0))
def test_below_p4_a_varied_set_keeps_mains_body_shot_share(at, share):
    """#362's audit: body_slots(share) of the four show her figure (front and side
    three-quarter), the others waist-up. Below P4 the same count, never more."""
    from leeroy_mind.selfies import safe
    p = lust()
    for tier, n in itertools.product(TIERS, range(16)):
        looks = varied.compose(p, f"share-{n}", share, tier=tier)
        assert looks is not None, (tier, n)
        body = [x for x in looks if x.framing in kind.VARIED_BODY_FRAMINGS]
        assert len(body) == safe.body_slots(share), (tier, n, share)
        for x in looks:
            assert photos.look_tier(x) <= tier
            if x.framing not in kind.VARIED_BODY_FRAMINGS:
                assert x.framing == "upper_body"
            elif x.view in ("front", "side"):
                assert x.framing == "three_quarter"
        at(tier)
        front = varied.recompose_slot(p, looks, 0, f"r-{n}", share)
        at(None)
        assert front is not None and front.view == "front" and photos.look_tier(front) <= tier
        assert front.framing == ("three_quarter" if safe.body_slots(share) > 2 else "upper_body")


@pytest.mark.parametrize("tier", TIERS)
def test_the_variety_kind_pass_picks_inside_her_tier(tier):
    """[selfies] variety: the lust kind comes from style_variety's balanced lists, which heat
    narrows as it narrows the reply schema; at P4 the pick is the one it always was."""
    from leeroy_mind.human import HumanIdentity
    from leeroy_mind.selfies import style_variety
    ident = HumanIdentity(guid=1, character_name="Zan", nickname="Zan", real_name="",
                          is_guild_master=True)
    cast = {g: lust(g) for g in (104, 105, 106)}
    before, *_ = style_variety.assign(cast, list(cast), seed="s", ident=ident)
    got, *_ = style_variety.assign(cast, list(cast), seed="s", ident=ident, tier=tier)
    if tier == P4:
        assert got == before
    assert len(got) == len(cast)  # every tier still has a pick for each of them
    for g, pick in got.items():
        assert photos.choice_of(pick.photo.outfit) is not None
        assert photos.picks_ok(dict(outfit=photos.choice_of(pick.photo.outfit),
                                    setting=pick.photo.setting), tier), (tier, pick.photo)
        assert pick.photo.kind != "mirror_selfie"
        if pick.photo.kind == "selfie":
            assert pick.photo.framing == "upper_body"
