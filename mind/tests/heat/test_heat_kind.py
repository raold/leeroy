"""The night's photo-kind pass under heat: a lust woman is offered only her tier's outfits and
settings, a pick above the tier is refused (never fixed), and at P4 the request is the one the
pass always made. The nightly harness's fakes only; every person here is invented."""
import pytest

from conftest import run
from heat.test_heat_photos import Tier
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat import photos
from leeroy_mind.heat.table import P0, P1, P2, P3, P4
from leeroy_mind.selfies.kind import LUST_SCHEMA, stage_photo
from nightly.conftest import h  # noqa: F401 (the nightly harness fixture)
from nightly.test_night_photo_lust import (LUST_PHOTO, LUST_REPLY, SWIM_REPLY, calls, lust,
                                           photo)


def _pass(h, reply, tier):  # noqa: F811
    lust(h, 102)
    ctx = h.ctx({"photo_lust": [reply, reply]})
    seen = calls(ctx)
    heat_live._LIVE = None if tier is None else Tier(tier)
    run(stage_photo(ctx))
    return [schema for _, _, schema in seen]


def test_at_p4_the_request_is_the_one_the_pass_always_made(h):  # noqa: F811
    assert _pass(h, SWIM_REPLY, P4) == [LUST_SCHEMA]
    assert photo(h, 102) is not None and "bikini" in photo(h, 102).outfit


def test_a_tier_narrows_what_she_is_offered_and_a_pick_inside_it_is_kept(h):  # noqa: F811
    schemas = _pass(h, LUST_REPLY, P2)  # a fitted little dress (P2) at her hallway mirror (P0)
    assert schemas == [photos.narrow_schema(LUST_SCHEMA, P2)]
    assert "string bikini" not in schemas[0]["properties"]["outfit"]["enum"]
    assert photo(h, 102) == LUST_PHOTO


def test_a_pick_above_the_tier_is_refused_and_nothing_is_stored(h):  # noqa: F811
    _pass(h, LUST_REPLY, P0)  # the same reply: the dress is above P0
    assert photo(h, 102) is None
    assert not photos.picks_ok(LUST_REPLY, P0) and photos.picks_ok(LUST_REPLY, P2)


# -- on main's photo audit (#362): review of #373, round 2 ---------------------------------------

def test_with_heat_off_the_tier_table_imports_and_the_pass_runs_as_main(h):  # noqa: F811
    """kind_one imports heat.photos for every member whatever the switch: on the tree with the
    photo audit's settings the tier table must load cleanly (its partition checks run again here,
    in a fresh module) and the pass with heat off ask exactly what main asks."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("leeroy_mind.heat._photos_fresh", photos.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)  # the import-time asserts: the tiers partition kind's lists
    from leeroy_mind.selfies import kind, safe
    assert set(fresh.SETTING_TIER) == set(kind.LUST_SETTINGS)
    assert not set(safe.RETIRED_SETTINGS) & set(fresh.SETTING_TIER)
    assert {fresh.SETTING_TIER[s] for s in safe.NEW_SETTINGS} == {P0}
    assert _pass(h, SWIM_REPLY, None) == [LUST_SCHEMA]  # heat off: the schema itself
    assert photo(h, 102) is not None and "bikini" in photo(h, 102).outfit


@pytest.mark.parametrize("tier", (P0, P1, P2, P3, P4, None))
def test_a_lust_selfie_keeps_its_framing_at_every_tier(h, tier):  # noqa: F811
    """Main's rules run before heat's check: a selfie keeps the framing the reply chose (the
    owner lifted its waist-up rule, 2026-11-19), and heat only refuses a pick above the tier,
    never re-frames."""
    reply = {**LUST_REPLY, "framing": "three_quarter", "outfit": "satin midi dress",
             "setting": "a cafe terrace"}
    _pass(h, reply, tier)
    got = photo(h, 102)
    assert got is not None and got.kind == "selfie" and got.framing == "three_quarter"
