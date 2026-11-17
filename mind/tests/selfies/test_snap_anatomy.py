"""Situational and request photos (832 x 1216 portraits): an anatomy negative that fits the
worker's 75 tokens, a lighter face on waist-up and three-quarter scenes, and no hand or limb
details in their prompts (controller A/B on the GPU, 2026-11-16). Every person here is invented."""
from dataclasses import replace

import pytest

from leeroy_mind.selfies.prompt import FIT_MAX, LUST_NEGATIVE, NEGATIVE, build_prompt
from leeroy_mind.selfies.render import MIN_SCALE, WIDE_FRAMINGS, WIDE_SCALE, scene_scale
from leeroy_mind.selfies.scene import (FRAMINGS, ONE_FACE_NEGATIVE, SNAP_ANATOMY, SNAP_NEGATIVE,
                                       SNAP_NEGATIVE_LUST, TILE_NEGATIVE, Scene,
                                       build_scene_prompt, build_tile_prompt, limb_detail,
                                       scene_from)
from nightly.test_night_enrich import APPEARANCE
from selfies import clip_bpe
from selfies.test_selfie_prompt import _worker_fit, woman
from selfies.test_selfie_scene import LUST_SCENE, SCENE, lusty

_TOKENIZERS = pytest.mark.skipif(
    clip_bpe.tokenizers() is None,
    reason="RealVisXL tokenizer files are not on this machine (selfie-data/models/realvisxl; "
           "set LEEROY_SELFIE_DATA to measure the snap negatives)")


def _terms(negative, priority=None) -> set[str]:
    return {w.strip() for t, pr in negative if priority is None or pr == priority
            for w in t.split(",")}


# -- the negative -------------------------------------------------------------------------------

def test_the_snap_negatives_require_the_anatomy_line():
    # since the photo audit (2026-11-17, safe.py): no eye words, a third hand, a duplicate
    # person, the mirror, the phone, text and the missing garments required
    assert SNAP_NEGATIVE == (
        ("cartoon, painting, 3d render", 0), (NEGATIVE[1][0], 0), ("child, teenager", 0),
        ("celebrity, famous person", 0), (SNAP_ANATOMY, 0),
        ("another person, duplicate person, mirror, phone, text", 0),
        ("bottomless, shirtless", 0), ("logo, watermark", 1),
        ("plastic skin, airbrushed, doll", 1), ("second face, crowd, group of people", 1),
        ("vintage, retro, film grain, sepia, old photo", 1),
        ("anime, illustration, cgi", 2), ("face on screen, webcam, video call, portrait on wall", 2))
    assert SNAP_NEGATIVE_LUST == (
        ("cartoon, painting", 0), (LUST_NEGATIVE[1][0], 0), *SNAP_NEGATIVE[2:11],
        ("3d render, anime, illustration, cgi", 2), *SNAP_NEGATIVE[12:])
    assert NEGATIVE[1][0] == "nsfw, nude, cleavage, lingerie, swimwear"
    assert LUST_NEGATIVE[1][0] == "nsfw, nude, topless, lingerie, underwear, see-through, sheer"
    for word in ("extra arms", "extra legs", "extra hands", "fused fingers", "extra fingers",
                 "malformed hands", "deformed feet"):
        assert word in _terms(SNAP_NEGATIVE, 0) and word in _terms(SNAP_NEGATIVE_LUST, 0)


@pytest.mark.parametrize("negative", [SNAP_NEGATIVE, SNAP_NEGATIVE_LUST], ids=["plain", "lust"])
def test_no_word_of_the_old_snap_negative_is_lost_and_the_safety_lines_stay_required(negative):
    old = TILE_NEGATIVE if negative is SNAP_NEGATIVE else LUST_NEGATIVE + TILE_NEGATIVE[-2:]
    assert _terms(old, 0) <= _terms(negative)
    covered = NEGATIVE[1][0] if negative is SNAP_NEGATIVE else LUST_NEGATIVE[1][0]
    assert _terms(((covered, 0),)) | {"child", "teenager", "celebrity", "famous person",
                                      "another person"} <= _terms(negative, 0)


def test_the_guild_tile_keeps_its_old_negative():
    assert TILE_NEGATIVE == NEGATIVE + (("another person, crowd, group of people", 0),
                                        (ONE_FACE_NEGATIVE, 0))
    assert build_tile_prompt(woman()).negative == TILE_NEGATIVE
    assert build_tile_prompt(lusty()).negative == TILE_NEGATIVE


def test_profile_sets_keep_their_negative():
    from leeroy_mind.selfies.prompt import BODY_NEGATIVE, HEAD_NEGATIVE
    assert build_prompt(woman()).negative == BODY_NEGATIVE  # a waist-up kind (safe.py)
    close = replace(woman(), photo=replace(woman().photo, framing="head_and_shoulders"))
    assert build_prompt(close).negative == HEAD_NEGATIVE and set(NEGATIVE) < set(HEAD_NEGATIVE)


@_TOKENIZERS
@pytest.mark.parametrize("negative", [SNAP_NEGATIVE, SNAP_NEGATIVE_LUST], ids=["plain", "lust"])
def test_snap_negatives_fit_with_the_anatomy_line(negative):
    """Measured with the worker's own fit and the real tokenizers: the required set, the anatomy
    line among it, counts at most FIT_MAX (72 plain, 74 lust when written). A future addition at
    priority 0 that pushes it over fails here (the worker would refuse every situational photo)."""
    required = ", ".join(t for t, pr in negative if pr == 0)
    assert clip_bpe.count(required) <= FIT_MAX
    kept, dropped = _worker_fit()(negative, clip_bpe.count, FIT_MAX)
    assert SNAP_ANATOMY in kept and SNAP_ANATOMY not in dropped


@_TOKENIZERS
def test_the_old_negative_always_dropped_its_hands_line():
    """The cause (2026-11-16): beside 61 required tokens the priority-1 hands line never fit."""
    assert clip_bpe.count(", ".join(t for t, pr in TILE_NEGATIVE if pr == 0)) == 61
    _, dropped = _worker_fit()(TILE_NEGATIVE, clip_bpe.count, FIT_MAX)
    assert any("bad hands" in d for d in dropped)


def test_both_scene_styles_render_with_the_new_negatives():
    assert build_scene_prompt(woman(), Scene(**SCENE)).negative == SNAP_NEGATIVE
    assert build_scene_prompt(lusty(), scene_from(LUST_SCENE)).negative == SNAP_NEGATIVE_LUST


# -- the face on wide scenes --------------------------------------------------------------------

def test_a_wide_scene_takes_a_lighter_face():
    assert WIDE_SCALE == 0.75 and set(WIDE_FRAMINGS) == {"upper_body", "three_quarter"}
    assert set(WIDE_FRAMINGS) < set(FRAMINGS)
    assert {f: scene_scale(0.6, f) for f in FRAMINGS} == {
        "face": 0.6, "head_and_shoulders": 0.6, "upper_body": 0.45, "three_quarter": 0.45}
    assert scene_scale(0.9, "three_quarter") == 0.68
    assert scene_scale(0.35, "upper_body") == MIN_SCALE         # the worker's floor
    assert scene_scale(0.35, "head_and_shoulders") == 0.35


# -- no limb details ----------------------------------------------------------------------------

HANDY = replace(APPEARANCE, face=("Small scar on left thumb", "freckles", "Rough fingertips",
                                  "thin glasses", "Restless hands", "chipped nail polish",
                                  "tattoo on her left forearm", "a warm, charming smile",
                                  "Faint crow's feet", "Glasses with taped arm"))
LIMBS = ("Small scar on left thumb", "Rough fingertips", "Restless hands", "chipped nail polish",
         "tattoo on her left forearm")
KEPT = ("freckles", "thin glasses", "a warm, charming smile", "Faint crow's feet",
        "Glasses with taped arm")


def test_limb_words_are_found_whole():
    assert all(limb_detail(d) for d in LIMBS + ("calloused hands and crow's feet", "Dry knuckles",
                                                "faint marker smudge on wrist", "sore feet"))
    for kept in KEPT + ("Armenian nose", "harmless grin", "legible tattoo on the neck",
                        "handsome jawline", "crows-feet", "crow’s feet",
                        "Smudge glasses with taped arm"):
        assert not limb_detail(kept), kept


@pytest.mark.parametrize("who", ["plain", "lust"])
def test_a_situational_prompt_leaves_out_limb_details(who):
    p = woman(appearance=HANDY) if who == "plain" else lusty(appearance=HANDY)
    s = Scene(**SCENE) if who == "plain" else scene_from(LUST_SCENE)
    texts = [t for t, _ in build_scene_prompt(p, s).phrases]
    assert not any(d in texts for d in LIMBS)
    assert all(d in texts for d in KEPT)
    # glasses still come right after her eye colour (identity_first)
    assert texts.index("hazel eyes") + 1 == texts.index("thin glasses")


def test_profile_sets_and_guild_tiles_keep_every_detail():
    p = woman(appearance=HANDY)
    profile = [t for t, _ in build_prompt(p).phrases]
    tile = [t for t, _ in build_tile_prompt(p).phrases]
    for d in HANDY.face:
        assert d in profile and d in tile
