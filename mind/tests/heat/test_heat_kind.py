"""The night's photo-kind pass under heat: a lust woman is offered only her tier's outfits and
settings, a pick above the tier is refused (never fixed), and at P4 the request is the one the
pass always made. The nightly harness's fakes only; every person here is invented."""
from conftest import run
from heat.test_heat_photos import Tier
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat import photos
from leeroy_mind.heat.table import P0, P2, P4
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
