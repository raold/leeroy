"""The owner's wardrobe and palette in the night's photo-kind pass (owner, 2026-11-17): a bias on
what the pass picks, never a replacement. A free-text kind is asked with a sentence of fixed
words; a lust woman picks from the lust outfits her wardrobe names and her palette's colours; a
kind the pass chose is chosen once more when they change. With neither set the requests are
today's. Fakes only; every person here is invented."""
from dataclasses import replace

from conftest import TEST_CAST, run
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import kind, wardrobe
from leeroy_mind.selfies.kind import (KIND_ASK, KIND_SCHEMA, LUST_SCHEMA, chosen_style,
                                      needs_rechoice, stage_photo, wardrobe_pending)
from leeroy_mind.selfies.style import LUST, PLAIN
from leeroy_mind.wow import CLASSES, RACES
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_lust import LUST_REPLY, NIGHTS, PLAIN_KIND, calls, ctx_for


def _wear(h, guid, **looks):
    """The owner's edit: her stored persona again with those looks, as an owner version."""
    p = load_persona(h.store, guid)
    _, race, cls = TEST_CAST[guid]
    save_persona(h.store, replace(p, appearance=replace(p.appearance, **looks)), source="owner",
                 ts=h.T, race=RACES[race], class_name=CLASSES[cls])


def test_with_no_wardrobe_the_requests_are_todays(h):
    h.persona(101, appearance=APPEARANCE)
    h.woman(102, "lust", appearance=APPEARANCE)
    ctx = h.ctx({"photo": [PLAIN_KIND], "photo_lust": [LUST_REPLY]})
    seen = calls(ctx)
    run(stage_photo(ctx))
    by = {sc["title"]: (user, sc) for _, user, sc in seen}
    assert by["photo"] == (KIND_ASK.format(name=ctx.name(101)), KIND_SCHEMA)
    assert by["photo_lust"][1] is LUST_SCHEMA
    assert by["photo_lust"][0] == KIND_ASK.format(name=ctx.name(102))


def test_a_free_text_kind_is_asked_with_what_they_wear_and_screened_as_ever(h):
    h.persona(101, appearance=replace(APPEARANCE, wardrobe=("smart", "cosy"), palette=("navy",)))
    ctx = h.ctx({"photo": [PLAIN_KIND]})
    seen = calls(ctx)
    run(stage_photo(ctx))
    (_, user, schema), = seen
    assert schema == KIND_SCHEMA  # still a free-text kind under the plain screens
    assert user == KIND_ASK.format(name=ctx.name(101)) + (
        " They tend to wear smart, tailored clothes, cosy knitwear: dress them in keeping with "
        "it. Colours they favour in what they wear: navy.")
    assert load_persona(h.store, 101).photo == ProfilePhoto(**PLAIN_KIND)
    refused = h.ctx({"photo": [{**PLAIN_KIND, "outfit": "lace lingerie"}]}, night=NIGHTS[1])
    h.persona(103, appearance=replace(APPEARANCE, wardrobe=("glam",)))
    run(stage_photo(refused))
    assert load_persona(h.store, 103).photo is None  # the hint loosens no screen


def test_a_lust_woman_picks_from_her_wardrobe_and_palette(h):
    h.woman(102, "lust", appearance=replace(APPEARANCE, wardrobe=("glam",), palette=("black", "gold")))
    ctx = h.ctx({"photo_lust": [LUST_REPLY]})
    seen = calls(ctx)
    run(stage_photo(ctx))
    (_, user, schema), = seen
    assert user == KIND_ASK.format(name=ctx.name(102))  # no words added: she writes none
    assert schema["title"] == LUST_SCHEMA["title"]
    assert set(schema["properties"]["outfit"]["enum"]) == set(wardrobe.LUST_TAGS["glam"])
    assert schema["properties"]["colour"]["enum"] == ["black", "gold"]
    assert "a hot tub on a wooden deck" not in schema["properties"]["setting"]["enum"]
    p = load_persona(h.store, 102)
    assert p.photo.outfit == "a fitted little black dress" and chosen_style(h.store, p) == LUST


def test_a_kind_the_pass_chose_is_chosen_once_more_when_the_wardrobe_changes(h):
    h.persona(101, appearance=APPEARANCE)
    run(stage_photo(ctx_for(h, 0, plain=[PLAIN_KIND])))
    p = load_persona(h.store, 101)
    assert chosen_style(h.store, p) == PLAIN and not needs_rechoice(h.store, p)
    _wear(h, 101, owner_hair_colour="red", makeup=None)  # hair alone: the kind stays
    assert not needs_rechoice(h.store, load_persona(h.store, 101))
    assert run_night(h, 1, plain=[PLAIN_KIND]).reasoner.calls == 0
    _wear(h, 101, wardrobe=("outdoorsy",))
    p = load_persona(h.store, 101)
    assert wardrobe_pending(h.store, p) and needs_rechoice(h.store, p)
    again = {**PLAIN_KIND, "outfit": "a waxed jacket and walking boots"}
    ctx = ctx_for(h, 2, plain=[again])
    seen = calls(ctx)
    run(stage_photo(ctx))
    assert len(seen) == 1 and "practical outdoor clothes" in seen[0][1]
    p = load_persona(h.store, 101)
    assert p.photo == ProfilePhoto(**again) and chosen_style(h.store, p) == PLAIN
    assert not wardrobe_pending(h.store, p) and not needs_rechoice(h.store, p)
    assert run_night(h, 3, plain=[PLAIN_KIND]).reasoner.calls == 0  # never twice for one change


def test_a_kind_set_by_hand_is_never_chosen_again_for_a_wardrobe(h):
    from nightly.test_night_photo_lust import by_hand
    h.persona(101, appearance=APPEARANCE)
    by_hand(h, 101, ProfilePhoto(**PLAIN_KIND))
    _wear(h, 101, wardrobe=("edgy",), palette=("black",))
    p = load_persona(h.store, 101)
    assert chosen_style(h.store, p) is None
    assert not wardrobe_pending(h.store, p) and not needs_rechoice(h.store, p)
    assert run_night(h, 0, plain=[PLAIN_KIND]).reasoner.calls == 0


def test_a_record_from_before_the_wardrobes_still_matches(h):
    h.persona(101, appearance=APPEARANCE)
    run(stage_photo(ctx_for(h, 0, plain=[PLAIN_KIND])))
    import json
    rec = json.loads(h.store.get_meta(kind.STYLE_KEY.format(guid=101)))
    assert set(rec) == {"style", "kind"}  # with no wardrobe the record is what it was


def run_night(h, n, plain=(), lusty=()):
    ctx = ctx_for(h, n, plain, lusty)
    run(stage_photo(ctx))
    return ctx
