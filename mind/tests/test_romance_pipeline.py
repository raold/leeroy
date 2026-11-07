"""The romance prompt in the live pipeline (conversation variety, task B4): her whispers read
a stage passage and an overlay variant picked per turn, her heat, the distance frame once, and
"Between you two lately:", the scene notes her turns to him leave in mind.db. Notes keep the
whisper rule: a whisper's note is shown only in a whisper between the same two. The test cast
and invented lines only."""
import asyncio
import dataclasses
import logging

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import HUMAN_NAME, OVERLAY, engine_clock, make_rig, save_member
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.romance import (ATTITUDE_TEXT, FULL_DIAL, HEAT_TEXT, NOTE_TEXT,
                                      SCENE_HEAD, STAGE_PASSAGES)
from leeroy_mind.talk.turns import Turn, TurnKind

HE = dict(name="Zan", they="he", them="him", their="his")
WHISPER_OVERLAY = "WHISPER-OVERLAY"


@pytest.fixture
def clock():
    return engine_clock()


def _cast(store, stance: str = "drawn") -> None:
    """Belra (102) as a woman with an attitude toward him, whose words arrive as written."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance=stance, typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})


def _rel(store, *, stage: str, attraction: float) -> None:
    store.put_relationship(Relationship(a=102, b=HUMAN, affinity=40.0, trust=30.0,
                                        attraction=attraction, romance_stage=stage))


@pytest.fixture
def rig(store, clock, tmp_path):
    _cast(store)
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


@pytest.fixture
def lust_rig(store, clock, tmp_path):
    _cast(store, stance="lust")
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def _notes(store) -> list[tuple]:
    return [tuple(r) for r in store.conn.execute(
        "SELECT guid, other, kind, text, channel, to_guid FROM pair_note ORDER BY id")]


def _filled(variants) -> list[str]:
    return [v.format(**HE) for v in variants]


def _his_line_unanswered(rig, text: str, channel: str = "guild", to_guid: int = 0) -> int:
    """His line, stored and seen, without the engine's answer: its line id."""
    line_id = rig.store.insert_chat_line(
        Chat(ts=0, channel=channel, from_guid=HUMAN, from_name=HUMAN_NAME, to_guid=to_guid,
             text=text, map=0, zone=10), session_id=None, origin="human")
    rig.world.add_line(rig.store.chat_lines([line_id])[0], rig.clock.now())
    return line_id


def test_her_whisper_reads_a_stage_variant_and_heat(rig):
    """Attraction 95 reads an "aching" line, and the live generator picks the stage's passage,
    so the same text does not open every whisper."""
    _rel(rig.store, stage="whispers", attraction=95.0)
    aching, passages = _filled(HEAT_TEXT["aching"]), _filled(STAGE_PASSAGES["whispers"])
    seen = set()
    for i, reply in enumerate(("how was work?", "lol true", "ok handsome", "you look great",
                               "bet you can't keep up", "haha nice one")):
        rig.script(reply)
        rig.whisper_from_him(102, f"tell me about thing number {i}")
        rig.run()
        user = rig.last_user()
        assert any(a in user for a in aching)
        seen |= {v for v in passages if v in user}
    assert len(rig.sent.says()) == 6
    assert len(seen) >= 2


def test_a_band_overlay_with_variants_varies_across_whispers(lust_rig, tmp_path):
    (tmp_path / "prompts" / "romance_explicit.txt").write_text(
        "BAND-ONE\n---\nBAND-TWO\n---\nBAND-THREE\n", encoding="utf-8")
    (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_text(
        "TAIL-ONE\n---\nTAIL-TWO\n---\nTAIL-THREE\n", encoding="utf-8")
    band, tail = set(), set()
    for i, reply in enumerate(("how was work?", "lol true", "ok handsome", "you look great",
                               "bet you can't keep up", "haha nice one")):
        lust_rig.script(reply)
        lust_rig.whisper_from_him(102, f"tell me about thing number {i}")
        lust_rig.run()
        user = lust_rig.last_user()
        band |= {w for w in ("BAND-ONE", "BAND-TWO", "BAND-THREE") if w in user}
        tail |= {w for w in ("TAIL-ONE", "TAIL-TWO", "TAIL-THREE") if w in user}
    assert len(band) >= 2 and len(tail) >= 2


def test_distance_is_said_once_in_an_explicit_whisper(lust_rig, tmp_path):
    """At the explicit band his whisper overlay comes last, then the distance frame, once."""
    (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_text(
        WHISPER_OVERLAY + "\n", encoding="utf-8")
    lust_rig.script("fair enough")
    lust_rig.whisper_from_him(102, "hey")
    lust_rig.run()
    user = lust_rig.last_user()
    assert OVERLAY in user and WHISPER_OVERLAY in user
    assert user.count("live far apart") == 1
    assert user.index(WHISPER_OVERLAY) < user.index("live far apart")


def test_distance_stays_in_the_romance_text_without_a_whisper_overlay(lust_rig):
    lust_rig.script("fair enough")
    lust_rig.whisper_from_him(102, "hey")
    lust_rig.run()
    user = lust_rig.last_user()
    assert user.count("live far apart") == 1
    assert user.index(OVERLAY) < user.index("live far apart")


def test_scene_notes_from_her_reply_reach_her_next_whisper(rig):
    rig.script("bet you can't keep up", "fair enough")
    rig.whisper_from_him(102, "how was your day?")
    rig.run()
    assert _notes(rig.store) == [
        (102, HUMAN, "his_question", "he asked about you", "whisper", HUMAN),
        (102, HUMAN, "her_tease", "you teased him", "whisper", HUMAN)]
    rig.whisper_from_him(102, "anyway")
    rig.run()
    assert f"{SCENE_HEAD} he asked about you; you teased him." in rig.last_user()


def test_the_scene_shows_her_three_newest_notes(rig):
    for i, (his, hers) in enumerate((("how was your day?", "bet you can't keep up"),
                                     ("you look great", "like you promised"),
                                     ("how was work?", "you look amazing in that tier"))):
        rig.script(hers)
        rig.whisper_from_him(102, his)
        rig.run()
    assert len(_notes(rig.store)) == 6
    rig.script("fair enough")
    rig.whisper_from_him(102, "anyway")
    rig.run()
    scene = rig.last_user().split(SCENE_HEAD)[1].split(".")[0]
    assert scene.count(";") == 2
    assert scene.strip() == "you brought back something he said before; he asked about you; " \
                            "you complimented him"


def test_whisper_notes_stay_out_of_a_guild_turn(rig):
    """Whisper scope: her whisper's notes exist, and her guild flirt at him (which carries her
    romance text) shows none of them."""
    rig.script("bet you can't keep up", "fair enough")
    rig.whisper_from_him(102, "how was your day?")
    rig.run()
    assert {n[2] for n in _notes(rig.store)} == {"his_question", "her_tease"}
    rig.run(300)  # past her loop guards
    assert rig.speak_turn(Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild",
                               addressee=HUMAN))
    prompt = rig.prompts()[-1]
    assert any(v.format(**HE) in prompt for v in ATTITUDE_TEXT["drawn"].values())  # romance
    assert SCENE_HEAD not in prompt
    assert "he asked about you" not in prompt and "you teased him" not in prompt
    assert rig.engine.notes.recent(102, HUMAN, turn_channel="guild", to_guid=0,
                                   now=rig.clock.now()) == []


def test_his_whisper_never_becomes_a_note_on_a_guild_turn(rig):
    """Her guild reply to his whisper line: the whisper is not hers to see there, so only her
    own move is noted, on the guild channel."""
    line = _his_line_unanswered(rig, "how was your day?", "whisper", 102)
    rig.script("bet you can't keep up")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN, trigger_line=line))
    assert _notes(rig.store) == [(102, HUMAN, "her_tease", "you teased him", "guild", 0)]


def test_his_guild_line_is_noted_on_her_guild_reply(rig):
    line = _his_line_unanswered(rig, "belra you look great")
    rig.script("bet you can't keep up")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN, trigger_line=line))
    assert (102, HUMAN, "his_compliment", "he complimented you", "guild", 0) in _notes(rig.store)


@pytest.mark.parametrize("his", ["anyone up for heroics?", "arkon you look great"])
def test_his_public_line_to_someone_else_is_no_note_on_her(rig, his):
    """A line to the guild or to Arkon was not to her: her reply notes only her own move."""
    line = _his_line_unanswered(rig, his)
    rig.script("bet you can't keep up")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN, trigger_line=line))
    assert _notes(rig.store) == [(102, HUMAN, "her_tease", "you teased him", "guild", 0)]


def test_his_public_line_to_her_reaches_her_next_whisper(rig):
    line = _his_line_unanswered(rig, "belra you look great")
    rig.script("bet you can't keep up", "fair enough")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN, trigger_line=line))
    rig.whisper_from_him(102, "anyway")
    rig.run()
    assert f"{SCENE_HEAD} he complimented you; you teased him." in rig.last_user()


def test_his_ask_in_a_whisper_is_no_declaration_note(rig):
    rig.script("bet you can't keep up")
    rig.whisper_from_him(102, "i need you to heal me tonight")
    rig.run()
    assert [n[2] for n in _notes(rig.store)] == ["her_tease"]


def test_only_her_turns_to_him_leave_notes(rig):
    """A turn to another member, a man's whisper to him and a template leave none."""
    rig.script("bet you can't keep up")
    assert rig.speak_turn(Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild",
                               addressee=101))
    rig.script("how was work?")
    rig.whisper_from_him(103, "how was your day?")
    rig.run()
    assert rig.sent.says()[-1].guid == 103

    async def go():
        task = asyncio.create_task(rig.engine.say_as(102, "whisper", "bet you can't keep up",
                                                     kind=TurnKind.WHISPER, to_guid=HUMAN))
        await rig.advance(40)
        return task.result()
    assert rig.sync(go())
    assert _notes(rig.store) == []


def test_a_failing_note_write_is_logged_without_the_text(rig, caplog, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rig.engine.notes, "add", broken)
    rig.script("bet you can't keep up")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.talk"):
        rig.whisper_from_him(102, "how was your day?")
        rig.run()
    assert [s.text for s in rig.sent.says()] == ["bet you can't keep up"]
    assert rig.utterances()[-1]["dropped"] is None
    assert "note_record_failed" in caplog.text
    assert "keep up" not in caplog.text and "asked about you" not in caplog.text


def test_every_note_kind_fits_the_column_once_filled():
    assert all(len(t.format(**HE)) <= 80 for t in NOTE_TEXT.values())


def _preview_user(rig, p, seed: int) -> str:
    """Her guild reply to him through the console's preview: its prompt's user message."""
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN,
                priority=Priority.BACKGROUND, deadline=rig.clock.now() + 15.0)
    rig.script("fair enough")
    assert rig.sync(rig.engine.preview_line(turn, p, trigger="hey you", seed=seed)) == (
        "fair enough", None)
    return rig.last_user()


def test_a_preview_picks_the_same_variants_before_and_after(lust_rig):
    """The console's dial preview composes before and after with one seed. Her own dial at
    FULL_DIAL makes every turn to him carry her romance, so the share is rolled only before:
    where the roll carried it, the variants (stage passage, heat) still match, and only the
    dial differs."""
    _rel(lust_rig.store, stage="whispers", attraction=95.0)
    p = lust_rig.engine.personas.get(102)
    full = dataclasses.replace(p, romance=dataclasses.replace(p.romance, dial=FULL_DIAL))
    picks = [*_filled(STAGE_PASSAGES["whispers"]), *_filled(HEAT_TEXT["aching"])]
    compared = 0
    for seed in range(1, 9):
        before, after = _preview_user(lust_rig, p, seed), _preview_user(lust_rig, full, seed)
        after_picks = [v for v in picks if v in after]
        assert len(after_picks) == 2  # one passage, one heat line
        if any(v in before for v in _filled(ATTITUDE_TEXT["lust"].values())):  # rolled in
            assert [v for v in picks if v in before] == after_picks, seed
            compared += 1
    assert compared >= 2
