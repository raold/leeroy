"""heat.resolve: the table, the order of its steps and the reason each cap carries."""
import itertools

import pytest

from leeroy_mind import heat as H
from leeroy_mind.heat.table import (CLEAN_CAP, DIAL_TO_HEAT, P0, P1, P2, P3, P4, ROWS, TIERS,
                                    tier_index)

LUST = H.Member(stance="lust", lust_style=True)
DRAWN = H.Member(stance="drawn")
LED = dict(his_heat=99)

# heat: tier, flirt, reassure, lead, starts, voice, photo, offers (the design's table)
TABLE = {
    0: ("none", 0.0, False, 0, 0.0, "none", P0, 0.5),
    1: ("warm", 0.5, True, 2, 0.3, "warm", P0, 0.7),
    2: ("warm", 0.5, True, 2, 0.3, "warm", P0, 0.7),
    3: ("warm", 0.5, True, 2, 0.3, "warm", P0, 0.7),
    4: ("flirty", 1.0, True, 1, 0.6, "flirty", P1, 0.8),
    5: ("flirty", 1.0, True, 1, 0.6, "flirty", P1, 0.8),
    6: ("suggestive", 1.0, True, 1, 0.8, "suggestive", P2, 0.9),
    7: ("suggestive", 1.0, True, 1, 0.8, "suggestive", P2, 0.9),
    8: ("frank", 1.0, True, 1, 0.9, "suggestive", P3, 1.0),
    9: ("explicit", 1.0, True, 0, 1.0, "frank", P4, 1.0),
    10: ("explicit", 1.0, True, 0, 1.3, "explicit", P4, 1.2),
}


@pytest.mark.parametrize("heat", range(11))
def test_each_heat_value_resolves_to_its_row(heat):
    r = H.resolve(heat, LUST, **LED)
    tier, flirt, reassure, lead, starts, voice, photo, offers = TABLE[heat]
    assert (r.heat, r.tier, r.ceiling, r.flirt, r.reassure, r.starts, r.voice, r.photo,
            r.offers) == (heat, tier, tier, flirt, reassure, starts, voice, photo, offers)
    assert ROWS[heat].lead == lead and r.applies and r.session is None


def test_heat_nine_is_today_every_multiplier_one_and_the_dial_nine():
    r = H.resolve(9, LUST, legacy=9, his_heat=0)  # no lead needed, as before the dial
    assert (r.tier, r.dial, r.flirt, r.starts, r.offers, r.photo, r.lead, r.every_turn) == (
        "explicit", 9, 1.0, 1.0, 1.0, P4, 0, "")
    assert r.reasons == (("voice", "voice"),)  # the one new rule at 9: a note speaks frank at most


def test_the_world_plus_her_offset_and_her_groups_is_clamped():
    assert H.resolve(9, H.Member(stance="lust", offset=-3), **LED).heat == 6
    assert H.resolve(9, H.Member(stance="lust", offset=5), **LED).heat == 10
    assert H.resolve(2, H.Member(stance="lust", offset=-5), **LED).heat == 0
    assert H.resolve(6, H.Member(stance="lust", offset=1, groups=2), **LED).heat == 9
    r = H.resolve(6, H.Member(stance="lust", groups=-2), **LED)
    assert (r.heat, r.offset, r.groups) == (4, 0, -2)


def test_a_dial_of_her_own_stands_until_an_offset_is_pinned():
    """The two women with their own dial 10 show as +1 at world 9 and keep that dial."""
    own = H.resolve(9, H.Member(stance="lust", own_dial=10), legacy=9)
    assert (own.heat, own.offset, own.dial, own.every_turn) == (10, 1, 10, "all")
    assert own.reason("heat") == "own_dial"
    pinned = H.resolve(9, H.Member(stance="lust", own_dial=10, offset=0), legacy=9)
    assert (pinned.heat, pinned.offset, pinned.dial, pinned.every_turn) == (9, 0, 9, "")
    low = H.resolve(9, H.Member(stance="lust", own_dial=4), legacy=9, **LED)
    assert (low.heat, low.tier, low.dial) == (4, "flirty", 4)


def test_the_legacy_dial_comes_back_when_heat_was_derived_from_it():
    for dial, heat in DIAL_TO_HEAT.items():
        r = H.resolve(heat, LUST, legacy=dial, **LED)
        assert r.dial == dial, dial
    assert H.resolve(5, LUST, **LED).dial == 5  # heat set outright: its row's dial
    assert H.resolve(8, LUST, **LED).dial == 8 and H.resolve(0, LUST, **LED).dial == 1


def test_monotonic_raising_heat_never_lowers_a_tier():
    for member in (LUST, DRAWN, H.Member(stance="fond")):
        for private, cap in itertools.product((True, False), (True, False)):
            rows = [H.resolve(h, member, private=private, public_cap=cap, **LED)
                    for h in range(11)]
            for a, b in zip(rows, rows[1:]):
                assert tier_index(a.tier) <= tier_index(b.tier)
                assert tier_index(a.voice) <= tier_index(b.voice)
                assert a.photo <= b.photo and a.starts <= b.starts and a.offers <= b.offers


def test_his_lead_holds_her_one_step_under_her_ceiling():
    assert H.resolve(5, LUST, his_heat=0).tier == "warm"
    assert H.resolve(5, LUST, his_heat=1).tier == "flirty"
    assert H.resolve(2, LUST, his_heat=0).tier == "warm"  # never under warm
    r = H.resolve(7, LUST, his_heat=0)
    assert (r.tier, r.ceiling, r.lead, r.reason("tier")) == ("flirty", "suggestive", 1, "lead")
    assert H.resolve(8, LUST, his_heat=0).tier == "suggestive"
    assert H.resolve(8, LUST, his_heat=0, late=True).tier == "frank"  # one step herself, late
    assert H.resolve(9, LUST, his_heat=0).tier == "explicit"  # as today
    assert H.resolve(10, LUST, his_heat=0).tier == "explicit"


def test_ten_carries_it_on_every_private_turn():
    assert H.resolve(10, LUST).every_turn == "private"
    assert H.resolve(9, H.Member(stance="lust", offset=1)).every_turn == "private"
    assert H.resolve(9, LUST).every_turn == ""


def test_clean_caps_and_open_lifts_only_the_dial():
    clean = H.resolve(9, LUST, session="clean", **LED)
    assert (clean.heat, clean.tier, clean.photo, clean.reason("heat")) == (
        CLEAN_CAP, "warm", P0, "clean")
    assert H.resolve(1, LUST, session="clean", **LED).heat == 1  # a cap, not a floor
    opened = H.resolve(4, LUST, session="open")
    assert (opened.heat, opened.tier, opened.voice, opened.photo, opened.reason("heat")) == (
        10, "explicit", "explicit", P4, "open")
    # never her stance: a woman with none, and a man, are untouched by either
    for member in (H.Member(stance="none"), H.Member(female=False, stance="none"),
                   H.Member(female=False, stance="lust")):
        for session in ("clean", "open", None):
            r = H.resolve(9, member, session=session)
            assert (r.applies, r.tier, r.session, r.flirt, r.starts, r.offers) == (
                False, "none", None, 1.0, 1.0, 1.0)
    assert H.resolve(9, H.Member(stance="none")).reason("heat") == "stance_none"
    assert H.resolve(9, H.Member(female=False)).reason("heat") == "man"
    # a fond woman on an open evening is still fond: her own reach is romance.py's
    assert H.resolve(9, H.Member(stance="fond"), session="open").tier == "explicit"


def test_the_photo_tier_never_passes_the_style_decided_before_heat():
    """Age rules are checked before heat: without the lust style she is P0 at every heat, on an
    open evening too."""
    for reason in ("age_guard", "plain_style"):
        member = H.Member(stance="lust", lust_style=False, photo_reason=reason)
        for heat, session in itertools.product(range(11), (None, "open", "clean")):
            r = H.resolve(heat, member, session=session)
            assert r.photo == P0 and r.reason("photo") == reason


def test_public_cap_is_off_by_default_and_last_when_on():
    assert H.resolve(9, LUST, private=False).tier == "explicit"  # today's "whatever channel"
    for heat, offset, session in itertools.product(range(11), range(-5, 6),
                                                   (None, "open", "clean")):
        member = H.Member(stance="lust", offset=offset, own_dial=10, lust_style=True)
        r = H.resolve(heat, member, private=False, session=session, public_cap=True, late=True,
                      **LED)
        assert tier_index(r.tier) <= tier_index("flirty")
        private = H.resolve(heat, member, private=True, session=session, public_cap=True,
                            late=True, **LED)
        assert private.tier == private.ceiling  # the cap is for channels others read only
    assert H.resolve(9, LUST, private=False, public_cap=True).reason("tier") == "public"


def test_the_voice_rule():
    nine, ten, eight = H.resolve(9, LUST), H.resolve(10, LUST), H.resolve(8, LUST, **LED)
    assert nine.speaks("frank") and nine.speaks("suggestive") and not nine.speaks("explicit")
    assert ten.speaks("explicit")
    assert eight.speaks("suggestive") and not eight.speaks("frank")
    assert H.resolve(9, LUST, session="open").speaks("explicit")  # a no-limits evening
    assert H.resolve(0, LUST).speaks(None) and not H.resolve(0, LUST).speaks("warm")
    for heat in range(1, 8):  # as the text tier
        r = H.resolve(heat, LUST, **LED)
        assert r.voice == r.ceiling


def test_explain_uses_the_table_and_names_no_file_text():
    assert "before the dial" in H.explain_row(9)
    assert "suggestive" in H.explain_row(6) and "going out" in H.explain_row(6)
    assert H.explain_row(0).startswith("Wording: no romance wording at all.")
    for heat in range(11):
        assert TABLE[heat][0] in H.explain_row(heat) or heat == 0
    text = H.what_changes(8, 6)
    assert text.startswith("8 to 6: ") and "frank to suggestive" in text and "lose gym" in text
    assert H.what_changes(4, 4) == ""
    card = H.explain(H.resolve(9, LUST, session="clean", **LED))
    assert card["title"] == "Heat 2 (world 9, hers +0)" and "clean for this session" in card["caps"]
    none = H.explain(H.resolve(9, H.Member(stance="none")))
    assert none == {"applies": False, "title": "stance none: heat does not apply", "lines": [],
                    "caps": ["stance none: heat does not apply"]}
    aged = H.explain(H.resolve(9, H.Member(stance="lust", photo_reason="age_guard")))
    assert "age guard: plain photos" in aged["caps"]
    assert set(TIERS) == {"none", "warm", "flirty", "suggestive", "frank", "explicit"}
