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
from leeroy_mind.talk.romance import (ARC_STAGES, ATTITUDE_TEXT, BANDS, COOLING_TEXT, DEFER, DISTANCE, DISTANCE_PLAN,
                                      HEAT_FROM, HEAT_TEXT, INTO_HIM_ALREADY, LEGACY_STAGE,
                                      NOTE_TEXT, REPROACH_RE, SCENE_HEAD, rival_text,
                                      scene_notes,
                                      PRIVATE_GUARD, PUBLIC_PASSAGES, STAGE_DEPTH, STAGE_PASSAGES,
                                      WHISPER_OVERLAY,
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
PUBLIC_CHANNELS = ("guild", "officer", "party", "raid", "say", "voice:lounge")
PRIVATE = ("late_nights", "picture", "confession")


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
    yield from (v for vs in PUBLIC_PASSAGES.values() for v in vs)
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
    assert fields(DISTANCE) == {"them", "they"} and fields(DISTANCE_PLAN) <= {"them", "they"}
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


# -- fix round 1 -----------------------------------------------------------------------------

def test_private_stages_stay_out_of_public_talk(tmp_path):
    """Whisper scope: the late talks, the picture and the confession are said only in a whisper
    to him. Anywhere else she reads a public passage that keeps them in whispers."""
    assert set(PUBLIC_PASSAGES) == set(PRIVATE)
    for stage in (*PRIVATE, "vulnerable", "acknowledged"):
        key = passage_key(stage)
        assert 2 <= len(PUBLIC_PASSAGES[key]) <= 3
        private = [v.format(**HE) for v in STAGE_PASSAGES[key]]
        for channel in PUBLIC_CHANNELS:
            for i in range(20):
                text = romance_instruction(lust(), rel(stage), rng=random.Random(i),
                                           data_dir=tmp_path, **dict(GUILD, channel=channel))
                assert not any(v in text for v in private), (stage, channel)
                assert "picture" not in text and "late talks" not in text, (stage, channel)
                assert any(v.format(**HE) in text for v in PUBLIC_PASSAGES[key])
        whispers = {romance_instruction(lust(), rel(stage), rng=random.Random(i),
                                        data_dir=tmp_path, **WHISPER) for i in range(20)}
        assert all(any(v in t for v in private) for t in whispers), stage
        assert stage_text("lust", stage, private=True) == STAGE_PASSAGES[key][0]
        assert stage_text("lust", stage) == PUBLIC_PASSAGES[key][0]  # public unless asked


def test_heat_shows_only_for_a_drawn_or_lust_woman(tmp_path):
    lines = [v.format(**HE) for vs in HEAT_TEXT.values() for v in vs]
    for stance, shows in (("fond", False), ("drawn", True), ("lust", True)):
        for x in (65, 80, 95):
            text = romance_instruction(lust(stance), rel(attraction=x), data_dir=tmp_path,
                                       **GUILD)
            assert any(h in text for h in lines) is shows, (stance, x)


def test_the_frame_is_short_and_keeps_its_points():
    for frame in (DISTANCE, DISTANCE_PLAN):
        assert estimate_tokens(frame) <= 45 and estimate_tokens(frame.format(**HE)) <= 45
        assert "live far apart" in frame and "only in game" in frame and "come over" in frame
        assert "waiting" in frame
    assert "don't ask" in DISTANCE and "don't say you're waiting" in DISTANCE  # B3's check
    assert "just said or to what's going on" in DISTANCE  # an opener has nothing from him
    assert "longing" in DISTANCE and "want" in DISTANCE
    assert "trip" in DISTANCE_PLAN and "where or when" in DISTANCE_PLAN
    assert "longing" in DISTANCE_PLAN and "want" in DISTANCE_PLAN


def test_no_line_frames_their_chat_as_lacking():
    """Anti-circling: "typing isn't enough" turns into a repeated "get on Vent" ask. A want is
    worded as a want."""
    lacking = ("isn't enough", "feels thin", "rather hear", "chat window can")
    texts = [*passages(), *(v for vs in HEAT_TEXT.values() for v in vs)]
    assert [t for t in texts if any(w in t for w in lacking)] == []


# -- the scene log, arc and rival slots, and the no-reproach scan (Task B3) -------------------

def builtin_strings():
    """Every built-in romance string except DISTANCE/DISTANCE_PLAN, which name the don'ts to
    forbid them."""
    yield from (v for d in ATTITUDE_TEXT.values() for v in d.values())
    yield from (v for vs in STAGE_PASSAGES.values() for v in vs)
    yield from (v for vs in PUBLIC_PASSAGES.values() for v in vs)
    yield from (v for vs in HEAT_TEXT.values() for v in vs)
    yield from COOLING_TEXT
    yield from NOTE_TEXT.values()
    yield rival_text("Belra", GM.forms)
    yield INTO_HIM_ALREADY
    yield DEFER


def test_no_builtin_romance_text_reproaches_or_summons():
    filled = [s.format(**HE) for s in builtin_strings()]
    assert len(filled) > 40
    assert [s for s in filled if REPROACH_RE.search(s)] == []


def test_the_scan_catches_a_reproach():
    for bad in ("You never call.", "Where were you?", "come over", "I'm still waiting",
                "Why don't you answer", "you forgot me", "Hurry up"):
        assert REPROACH_RE.search(bad), bad


def test_distance_frames_the_summons_as_dont():
    assert "don't ask" in DISTANCE and "don't say you're waiting" in DISTANCE
    assert "don't ask" in DISTANCE_PLAN and "don't say you're waiting" in DISTANCE_PLAN
    assert estimate_tokens(DISTANCE_PLAN) <= 45
    assert estimate_tokens(DISTANCE_PLAN.format(**HE)) <= 45


def test_public_passages_never_mention_whispers_outside_the_guard():
    for vs in PUBLIC_PASSAGES.values():
        for v in vs:
            assert "whisper" not in v.replace(PRIVATE_GUARD, "").lower(), v


def test_no_scene_note_for_a_summons():
    assert scene_notes(frozenset({"summon"}), frozenset()) == []
    assert scene_notes(frozenset(), frozenset({"summon"})) == []


def test_scene_notes_cover_every_move_and_stay_short():
    his = frozenset({"question", "declare", "compliment", "gift", "petname"})
    her = frozenset({"declare", "tease", "question", "callback", "compliment", "plan"})
    notes = scene_notes(her, his)
    assert len(notes) == 11 and len({k for k, _ in notes}) == 11
    assert {k for k, _ in notes} == set(NOTE_TEXT)
    assert dict(notes)["his_question"] == NOTE_TEXT["his_question"]
    assert all(len(t.format(**HE)) <= 80 for t in NOTE_TEXT.values())
    assert "asked about you" in NOTE_TEXT["his_question"]
    assert "sent you something" in NOTE_TEXT["his_gift"]


def test_between_you_two_lately_shows_at_most_three_notes(tmp_path):
    notes = ["{they} asked about you", "you teased {them}", "{they} sent you something",
             "you asked {them} something personal"]
    text = romance_instruction(lust(), rel("banter"), data_dir=tmp_path, scene=notes, **WHISPER)
    assert f"{SCENE_HEAD} he asked about you; you teased him; he sent you something" in text
    assert "something personal" not in text
    assert SCENE_HEAD == "Between you two lately:"
    none = romance_instruction(lust(), rel("banter"), data_dir=tmp_path, **WHISPER)
    assert SCENE_HEAD not in none


def test_slots_come_after_the_heat_line_in_order(tmp_path):
    kw = dict(scene=["you teased {them}"], arc_lines=["ARC-LINE"], rival="Quillon",
              cooling=True)
    text = romance_instruction(lust(), rel("banter", 95), data_dir=tmp_path, rng=random.Random(1),
                               **WHISPER, **kw)
    heat = next(h.format(**HE) for h in HEAT_TEXT["aching"] if h.format(**HE) in text)
    cool = next(c.format(**HE) for c in COOLING_TEXT if c.format(**HE) in text)
    order = [text.index(x) for x in (heat, SCENE_HEAD, "ARC-LINE", "Quillon has been", cool)]
    assert order == sorted(order)
    assert text.index(cool) < text.index("It's only ever with")


def test_scene_arc_rival_and_cooling_stay_out_of_every_other_channel(tmp_path):
    kw = dict(scene=["you teased {them}"], arc_lines=["ARC-LINE"], rival="Quillon",
              cooling=True)
    for channel in PUBLIC_CHANNELS:
        base = dict(WHISPER, channel=channel)
        with_slots = romance_instruction(lust(), rel("banter", 95), data_dir=tmp_path,
                                         rng=random.Random(1), **base, **kw)
        plain = romance_instruction(lust(), rel("banter", 95), data_dir=tmp_path,
                                    rng=random.Random(1), **base)
        assert with_slots == plain, channel
        assert SCENE_HEAD not in with_slots and "ARC-LINE" not in with_slots
        assert "Quillon" not in with_slots


def test_rival_line_aims_at_the_rival_never_at_him():
    t = rival_text("Belra", GM.forms)
    assert "Belra" in t and "her" in t and "{name}" in t
    assert not REPROACH_RE.search(t.format(**HE))
    assert "never him" in t
    assert rival_text("A{b}", GM.forms).format(**HE).startswith("A{b} has been")


def test_cooling_is_wistful_never_nagging_and_adds_one_variant(tmp_path):
    assert len(COOLING_TEXT) >= 2
    for c in COOLING_TEXT:
        low = c.lower()
        assert "offline" not in low and "away" not in low and "where" not in low
        assert estimate_tokens(c.format(**HE)) <= 40
    a = romance_instruction(lust(), rel("banter"), data_dir=tmp_path, cooling=True,
                            rng=random.Random(0), **WHISPER)
    assert sum(c.format(**HE) in a for c in COOLING_TEXT) == 1
