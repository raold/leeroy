"""The romance passages: the distance frame, a passage per stage of the long-distance arc with
variants picked per turn, and the heat inside the top attraction band (variety plan, Task B2).
Synthetic people only: the woman is a test persona, he is "Zan"."""
import dataclasses
import random
import string

import pytest

from conftest import _base_persona_dict
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.storage.rows import ROMANCE_STAGES, Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.romance import (ARC_STAGES, BANDS, DEFER, DISTANCE, DISTANCE_PLAN,
                                      HEAT_FROM, HEAT_TEXT, INTO_HIM_ALREADY, LEGACY_STAGE,
                                      STAGE_DEPTH, STAGE_PASSAGES, WHISPER_OVERLAY,
                                      distance_text, effective_band, heat_step, passage_key,
                                      romance_instruction, stage_text, whisper_tail)
from leeroy_mind.text import estimate_tokens

GM = HumanIdentity(1, "Zanthor", "Zan", "Quillon", True, pronouns="he")
OVERLAY = "LOCAL-OVERLAY"
WHISPER_TEXT = "WHISPER-OVERLAY"
HE = dict(name="Zan", them="him", they="he", their="his")
WHISPER = dict(dial=8, channel="whisper", addressee_is_human=True, phase=DayPhase.EVENING,
               human=GM)
GUILD = dict(WHISPER, channel="guild")
EXPLICIT_GUILD = dict(GUILD, dial=9)
EXPLICIT_WHISPER = dict(dial=9, channel="whisper", forms=GM.forms)
ATTITUDES = ("fond", "drawn", "lust")


def lust(stance="lust"):
    d = _base_persona_dict(201, "Belra")
    d["identity"]["player_gender"] = "female"
    d["romance"] = {"stance": stance, "style": "bold", "jealousy_mode": None, "dial": None}
    return persona_from_dict(d)


def rel(stage="none", attraction=0.0):
    return Relationship(a=201, b=1, affinity=30.0, attraction=attraction, romance_stage=stage)


@pytest.fixture
def overlay_dir(tmp_path):
    d = tmp_path / "with-overlay"
    (d / "prompts").mkdir(parents=True)
    (d / "prompts" / "romance_explicit.txt").write_text(OVERLAY + "\n", encoding="utf-8")
    (d / "prompts" / f"{WHISPER_OVERLAY}.txt").write_text(WHISPER_TEXT + "\n", encoding="utf-8")
    return d


def fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def passages():
    yield from (v for vs in STAGE_PASSAGES.values() for v in vs if v)
    yield INTO_HIM_ALREADY


# -- the stages ------------------------------------------------------------------------------

def test_every_stage_has_passages_and_legacy_stages_read_their_new_one():
    assert set(ROMANCE_STAGES) <= set(STAGE_PASSAGES) | set(LEGACY_STAGE)
    assert set(ARC_STAGES) == set(STAGE_PASSAGES)
    assert all(len(STAGE_PASSAGES[s]) >= 2 for s in ARC_STAGES if s != "none")
    assert all(len(STAGE_PASSAGES[s]) <= 3 for s in ARC_STAGES)
    assert STAGE_PASSAGES["none"] == ("",)
    # the preflight ruling: "invitation" follows S1's mapping (planning), not confession
    assert passage_key("vulnerable") == "late_nights" and passage_key("invitation") == "planning"
    assert passage_key("acknowledged") == "confession"
    assert all(passage_key(s) == s for s in ARC_STAGES)
    for old, new in LEGACY_STAGE.items():
        assert stage_text("fond", old) == stage_text("fond", new)


def test_passages_are_short_and_take_only_his_name_and_forms():
    for v in passages():
        assert estimate_tokens(v) <= 40 and estimate_tokens(v.format(**HE)) <= 40, v
        assert fields(v) <= {"name", "them", "their"}, v
        assert not any(c in v for c in ";—–"), v


def test_notices_still_reads_by_attitude():
    for stance in ("drawn", "lust"):
        assert all(stage_text(stance, "notices", random.Random(i)) == INTO_HIM_ALREADY
                   for i in range(10))
    fond = {stage_text("fond", "notices", random.Random(i)) for i in range(30)}
    assert INTO_HIM_ALREADY not in fond and fond == set(STAGE_PASSAGES["notices"])
    assert stage_text("fond", "notices") == STAGE_PASSAGES["notices"][0]  # no rng: the first
    assert stage_text("lust", "none") == "" and stage_text("lust", "no_such_stage") == ""


def test_the_same_text_never_opens_every_whisper(tmp_path):
    texts = {romance_instruction(lust(), rel(stage="whispers"), rng=random.Random(i),
                                 data_dir=tmp_path, **WHISPER) for i in range(30)}
    assert len(texts) >= 2
    seen = {stage_text("lust", "whispers", random.Random(i)) for i in range(30)}
    assert seen == set(STAGE_PASSAGES["whispers"])


def test_after_passages_defer_to_his_version():
    assert all(DEFER in v for v in STAGE_PASSAGES["after"])
    assert all(DEFER in v for v in STAGE_PASSAGES["weekend"])  # the weekend itself too
    assert fields(DEFER) == {"name", "their"}


def test_ex_reads_as_history_and_carries_no_heat(tmp_path):
    for v in STAGE_PASSAGES["ex"]:
        assert "history" in v or "over" in v
    hot = romance_instruction(lust(), rel("ex", attraction=95), data_dir=tmp_path, **GUILD)
    assert not any(h.format(**HE) in hot for vs in HEAT_TEXT.values() for h in vs)


def test_a_deeper_stage_never_lowers_the_band():
    for stance in ATTITUDES:
        for dial in range(1, 11):
            floor = BANDS.index(effective_band(stance, "notices", dial))
            for stage in ARC_STAGES:
                assert BANDS.index(effective_band(stance, stage, dial)) >= floor, (stance, stage)
    assert {s: STAGE_DEPTH[s] for s in ARC_STAGES} == {
        "none": 0, "notices": 0, "banter": 1, "whispers": 1, "late_nights": 2, "voice": 2,
        "picture": 2, "confession": 2, "planning": 2, "weekend": 2, "after": 2, "ex": 0}
    assert all(STAGE_DEPTH[old] == 2 for old in LEGACY_STAGE)


# -- heat ------------------------------------------------------------------------------------

def test_heat_steps_inside_the_top_band():
    assert HEAT_FROM == 60
    got = [heat_step(x) for x in (0, 55, 59.9, 60, 74.9, 75, 89.9, 90, 100)]
    assert got == [None, None, None, "warm", "warm", "heated", "heated", "aching", "aching"]
    assert set(HEAT_TEXT) == {"warm", "heated", "aching"}
    for vs in HEAT_TEXT.values():
        assert len(vs) == 2 and len(set(vs)) == 2
        for v in vs:
            assert fields(v) <= {"name", "them", "their"} and estimate_tokens(v) <= 30, v
            assert not any(c in v for c in ";—–"), v


def test_heat_shows_a_climbing_score(tmp_path):
    a, b, c = (romance_instruction(lust(), rel(attraction=x), rng=random.Random(1),
                                   data_dir=tmp_path, **GUILD) for x in (65, 80, 95))
    assert len({a, b, c}) == 3 and heat_step(55) is None
    for text, step in ((a, "warm"), (b, "heated"), (c, "aching")):
        assert any(v.format(**HE) in text for v in HEAT_TEXT[step])
    cool = romance_instruction(lust(), rel(attraction=55), data_dir=tmp_path, **GUILD)
    assert not any(v.format(**HE) in cool for vs in HEAT_TEXT.values() for v in vs)


# -- the distance frame ----------------------------------------------------------------------

def test_every_romance_text_says_the_distance_once(tmp_path):
    for stance in ATTITUDES:
        for stage in (*ARC_STAGES, *LEGACY_STAGE):
            for channel in ("guild", "whisper"):
                text = romance_instruction(lust(stance), rel(stage), data_dir=tmp_path,
                                           **dict(GUILD, channel=channel))
                frame = distance_text(stage).format(**HE)
                assert text.count("live far apart") == 1 and frame in text, (stance, stage)


def test_the_planning_stage_reads_the_trip_as_the_one_meeting(tmp_path):
    assert distance_text("planning") == DISTANCE_PLAN == distance_text("invitation")
    assert all(distance_text(s) == DISTANCE for s in ARC_STAGES if s != "planning")
    assert "trip" in DISTANCE_PLAN and "come over" in DISTANCE_PLAN  # named to forbid it
    assert fields(DISTANCE) == fields(DISTANCE_PLAN) == {"them", "they"}
    text = romance_instruction(lust(), rel("planning"), data_dir=tmp_path, **WHISPER)
    assert DISTANCE_PLAN.format(**HE) in text and DISTANCE.format(**HE) not in text


def test_distance_comes_after_the_band_overlay_or_after_the_whisper_overlay(overlay_dir):
    text = romance_instruction(lust(), rel(), tail_follows=False, data_dir=overlay_dir,
                               **EXPLICIT_GUILD)
    assert text.index(OVERLAY) < text.index(DISTANCE.format(them="him", they="he")[:30])
    assert whisper_tail(lust(), rel(), data_dir=overlay_dir, **EXPLICIT_WHISPER).endswith(
        DISTANCE.format(them="him", they="he"))
    assert whisper_tail(lust(), rel(), data_dir=overlay_dir, **EXPLICIT_WHISPER) == (
        f"{WHISPER_TEXT} {DISTANCE.format(**HE)}")
    # the whisper tail says it, so the instruction leaves it out
    lead = romance_instruction(lust(), rel(), tail_follows=True, data_dir=overlay_dir,
                               **dict(EXPLICIT_GUILD, channel="whisper"))
    assert "live far apart" not in lead and OVERLAY in lead and "two adults" in lead


def test_the_whisper_tail_follows_his_pronouns_and_stage(overlay_dir):
    she = dataclasses.replace(GM, pronouns="she")
    tail = whisper_tail(lust(), rel("planning"), dial=9, channel="whisper", data_dir=overlay_dir,
                        forms=she.forms)
    assert tail == f"{WHISPER_TEXT} {DISTANCE_PLAN.format(they='she', them='her', their='her')}"
    assert whisper_tail(lust(), rel(), dial=9, channel="guild", data_dir=overlay_dir,
                        forms=GM.forms) == ""


def test_the_romance_text_reads_in_the_planned_order(overlay_dir):
    p = lust()
    text = romance_instruction(p, rel("whispers", attraction=95), data_dir=overlay_dir,
                               **EXPLICIT_GUILD)
    order = [text.index(x) for x in (
        "You want Zan", "Your way", STAGE_PASSAGES["whispers"][0].format(**HE),
        HEAT_TEXT["aching"][0].format(**HE), OVERLAY, "live far apart", "two adults",
        "can read it", "actually happening")]
    assert order == sorted(order)
