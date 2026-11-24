"""[talk] wow_topic in the live pipeline (talk/wowtopic.py): a raid or guild answer is held to the
channel (the hold line, no unasked topics of hers, her words she just used left out of her voice
block), a loot turn is told bids are DKP, a game line past her usual words is cut back to its
first sentence with a tighter num_predict, the guards read the chat register and her own words
tighter, the mind's own instruction text loses an invented boss, and an opener on the raid's
channel while he raids brings up the game. A whisper's prompt and lines are byte for byte what
they were. Second round (2026-11-24): a line that hands DKP over is asked again, a held turn's
"Right now" says her availability only, and the instruction check fires through its real checks.
Invented cast and lines only."""
import logging
from dataclasses import replace

import pytest

from conftest import HUMAN, run, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.protocol import Group
from leeroy_mind.talk import wowtopic as w
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from sheets import give


@pytest.fixture
def clock():
    return engine_clock()


def reply(guid=102, channel="guild", kind=TurnKind.REPLY_HUMAN, trigger=None):
    to_him = channel == "whisper"
    return Turn(kind=kind, speaker=guid, channel=channel, to_guid=HUMAN if to_him else 0,
                addressee=HUMAN if kind is TurnKind.REPLY_HUMAN or to_him else None,
                trigger_line=trigger)


def prompt(r, t, meta=None):
    c = run(r.engine._compose(t, meta or TurnMeta("t"), live=False))
    return "\n\n".join(m["content"] for m in c.messages), c


def switch(r, **talk):
    r.engine.cfg = replace(r.engine.cfg, talk=replace(r.engine.cfg.talk, **talk))


def his_line(r, text, channel="guild", to_guid=0):
    """His line stored and seen, as the app does, without the engine planning its answers (the
    tests speak one turn themselves)."""
    from engine_rig import HUMAN_NAME
    from leeroy_mind.protocol import Chat
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name=HUMAN_NAME, to_guid=to_guid,
              text=text, map=0, zone=10)
    row = r.store.chat_lines([r.store.insert_chat_line(ev, session_id=None, origin="human")])[0]
    r.world.add_line(row, r.clock.now())
    return row


def test_a_guild_answer_is_held_to_the_channel(store, clock, tmp_path):
    seed_test_cast(store)
    give(store, 103)  # GRUFF: brings up the allotment and night shifts at the depot
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        row = his_line(r, "who's tanking heigan")
        text, _ = prompt(r, reply(103, trigger=row.id))
        assert w.hold_text("guild") in text
        assert "Unasked, you bring up" not in text and "night shifts at the depot" not in text
        assert "You say, only now and then: noted, sound as, on my tally, well so." in text
        switch(r, wow_topic=False)
        off, _ = prompt(r, reply(103, trigger=row.id))
        assert w.hold_text("guild") not in off
        assert "Unasked, you bring up: the allotment; night shifts at the depot." in off
    finally:
        r.close()


def test_his_line_about_her_life_is_not_held(store, clock, tmp_path):
    seed_test_cast(store)
    give(store, 103)
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        row = his_line(r, "how was work today")
        text, _ = prompt(r, reply(103, trigger=row.id))
        assert w.hold_text("guild") not in text
        assert "Unasked, you bring up: the allotment; night shifts at the depot." in text
    finally:
        r.close()


def test_a_whisper_prompt_is_byte_identical(store, clock, tmp_path):
    seed_test_cast(store)
    give(store, 103)
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        row = his_line(r, "bids on the helm?", channel="whisper", to_guid=103)
        on, _ = prompt(r, reply(103, channel="whisper", trigger=row.id))
        switch(r, wow_topic=False)
        off, _ = prompt(r, reply(103, channel="whisper", trigger=row.id))
        assert on == off and w.DKP_RULE not in on
    finally:
        r.close()


def test_a_loot_turn_is_told_bids_are_dkp(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        row = his_line(r, "bids on the helm go to me", channel="raid")
        text, _ = prompt(r, reply(102, channel="raid", trigger=row.id))
        assert w.DKP_RULE in text
        plain = his_line(r, "pull when ready", channel="raid")
        text, _ = prompt(r, reply(102, channel="raid", trigger=plain.id))
        assert w.DKP_RULE not in text
    finally:
        r.close()


def test_her_own_word_she_just_used_stays_out_of_the_block(store, clock, tmp_path):
    seed_test_cast(store)
    give(store, 103)
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        r.engine._own[103].append(("guild", 0, "noted, pulling now"))
        row = his_line(r, "go when ready")
        text, _ = prompt(r, reply(103, trigger=row.id))
        assert "You say, only now and then: sound as, on my tally, well so." in text
        whisper = his_line(r, "go when ready", channel="whisper", to_guid=103)
        private, _ = prompt(r, reply(103, channel="whisper", trigger=whisper.id))
        assert "You say, only now and then: noted, sound as, on my tally, well so." in private
    finally:
        r.close()


def _long_reply(store, clock, tmp_path, his, channel="guild", **rig):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", words_per_line=[3, 8], split_rate=0.0,
                typo_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path, wow_topic=True, **rig)
    r.script("pulling the next pack now. honestly that last pull was such a mess for all of us")
    row = his_line(r, his, channel=channel, to_guid=102 if channel == "whisper" else 0)
    return r, r.speak_turn(reply(102, channel=channel, trigger=row.id))


def test_a_long_game_line_goes_as_its_first_sentence(store, clock, tmp_path):
    r, lines = _long_reply(store, clock, tmp_path, "go when ready")
    try:
        assert " ".join(lines).startswith("pulling the next pack now")
        assert "honestly" not in " ".join(lines)
        assert len(r.chat_calls()) == 1  # cut in code: no "long" retry
        assert r.chat_calls()[-1]["options"]["num_predict"] == w.predict(8)
    finally:
        r.close()


def test_an_answer_to_his_question_is_never_cut(store, clock, tmp_path):
    r, _ = _long_reply(store, clock, tmp_path, "what went wrong on that pull?")
    try:
        assert r.chat_calls()[0]["options"]["num_predict"] != w.predict(8)
        assert len(r.chat_calls()) == 2  # main's "long" tell asks again, never a cut in code
    finally:
        r.close()


@pytest.mark.parametrize("channel, rig", [("whisper", {}), ("guild", {"coherence_length": True})])
def test_no_trim_in_a_whisper_or_under_the_length_rule(store, clock, tmp_path, channel, rig):
    r, _ = _long_reply(store, clock, tmp_path, "go when ready", channel=channel, **rig)
    try:
        assert r.chat_calls()[0]["options"]["num_predict"] != w.predict(8)
    finally:
        r.close()


def test_the_guards_read_the_register_on_a_wow_turn_only(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        meta = TurnMeta("t")
        assert r.engine.live_review.draft(reply(102), meta, "x").wow_topic is True
        assert r.engine.live_review.draft(reply(102, channel="whisper"), meta, "x").wow_topic is False
        switch(r, wow_topic=False)
        assert r.engine.live_review.draft(reply(102), meta, "x").wow_topic is False
    finally:
        r.close()


def test_an_invented_boss_leaves_the_minds_instruction(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    r = make_rig(store, clock, tmp_path)
    try:
        r.engine._invents_game_name = lambda text: "gloomfang" in text.casefold()
        t = replace(reply(102, kind=TurnKind.ASIDE), addressee=None,
                    instruction="The guild wiped on Gloomfang twice. Say how the night went.")
        text, _ = prompt(r, t)
        assert "Gloomfang" not in text and "Say how the night went." in text
        assert "instruction_entity_dropped: guid=102" in caplog.text and "n=1" in caplog.text
        assert "Gloomfang" not in caplog.text  # the count only, never the text
        private = replace(t, channel="whisper", to_guid=HUMAN, addressee=HUMAN,
                          kind=TurnKind.WHISPER)
        whispered, _ = prompt(r, private)
        assert "Gloomfang" in whispered  # a private turn's instruction is as it was
    finally:
        r.close()


def test_an_opener_on_the_raid_channel_while_he_raids_brings_up_the_game(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", wants=["the Invented Drake mount"],
                life_thread="her sister's wedding plans")
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        r.sync(r.event(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102, 103))))
        r.engine.bits.pick = lambda *a, **k: None  # an opener, never her bit
        turn, meta = r.engine._social_turn(TurnKind.OPEN, 102, "raid", addressee=None,
                                           trigger=None, room=2, now=clock.now(), delay=0.0)
        assert turn.kind is TurnKind.OPEN and meta.game_topic
        assert meta.topic == "how close you are to the Invented Drake mount in game"
        text, _ = prompt(r, turn, meta)
        assert w.hold_text("raid") in text and "wedding" not in text
        _, guild_meta = r.engine._social_turn(TurnKind.OPEN, 102, "guild", addressee=None,
                                              trigger=None, room=2, now=clock.now(), delay=0.0)
        assert not guild_meta.game_topic and "Invented Drake" not in guild_meta.topic
        switch(r, wow_topic=False)
        _, off = r.engine._social_turn(TurnKind.OPEN, 102, "raid", addressee=None, trigger=None,
                                       room=2, now=clock.now(), delay=0.0)
        assert not off.game_topic
    finally:
        r.close()


def test_a_game_line_cut_off_by_its_num_predict_goes_back_to_its_last_sentence(
        store, clock, tmp_path, caplog):
    from leeroy_mind.llm.diagnostics import capture_response
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", words_per_line=[3, 12], split_rate=0.0,
                typo_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path, wow_topic=True, reply="on my way. bringing the flasks and the")
    original = r.llm.chat

    async def chat(model, messages, *, options, think=None, **kw):
        result = await original(model, messages, options=options, think=think, **kw)
        return replace(result, diagnostics=capture_response(
            {"message": {"content": result.text}, "done": True, "done_reason": "length"}))
    r.llm.chat = chat
    try:
        row = his_line(r, "go when ready")
        lines = r.speak_turn(reply(102, trigger=row.id))
        assert " ".join(lines).startswith("on my way") and "flasks" not in " ".join(lines)
        assert any("wow_line_cutoff: guid=102" in rec.getMessage() for rec in caplog.records)
    finally:
        r.close()


# -- DKP is hers (the re-measure, 2026-11-24) -------------------------------------------------------

def _loot_reply(store, clock, tmp_path, *replies, wow_topic=True):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", typo_rate=0.0, split_rate=0.0,
                emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path, wow_topic=wow_topic)
    r.script(*replies)
    row = his_line(r, "bids on the helm, who wants it", channel="raid")
    return r, r.speak_turn(reply(102, channel="raid", trigger=row.id))


def test_a_line_that_hands_dkp_over_is_asked_again(store, clock, tmp_path):
    r, lines = _loot_reply(store, clock, tmp_path, "have 20 dkp for u if you want it", "40 on it")
    try:
        assert lines == ["40 on it"]
        assert w.DKP_HINT in r.chat_calls()[-1]["messages"][-1]["content"]
    finally:
        r.close()


def test_a_retry_that_still_hands_dkp_over_is_dropped(store, clock, tmp_path):
    r, lines = _loot_reply(store, clock, tmp_path, "just lend me your dkp then")
    try:
        assert lines == [] and len(r.chat_calls()) == 2
    finally:
        r.close()


def test_dkp_words_are_read_only_with_the_switch(store, clock, tmp_path):
    r, lines = _loot_reply(store, clock, tmp_path, "have 20 dkp for u if you want it",
                           wow_topic=False)
    try:
        assert lines == ["have 20 dkp for u if you want it"] and len(r.chat_calls()) == 1
    finally:
        r.close()


# -- "Right now" on a held turn: her availability only (the re-measure, 2026-11-24) ------------------

def _work():
    from leeroy_mind.life.day import Block, DayEvent
    from leeroy_mind.life.presence import Attention, LifePresence, Where
    return LifePresence(Block.WORK, Where.AT_WORK, Attention.GLANCING, DayEvent.NONE, False)


def _user(r, turn, meta):
    r.engine._draw_situation(turn, meta, r.engine.personas.get(turn.speaker), r.clock.now())
    c = r.sync(r.engine._compose(turn, meta, live=True))
    return c.messages[-1]["content"]


def test_a_held_turn_says_her_availability_not_where_she_is(store, clock, tmp_path):
    seed_test_cast(store)
    r = make_rig(store, clock, tmp_path, wow_topic=True, availability=6)
    try:
        row = his_line(r, "go when ready")
        user = _user(r, reply(102, trigger=row.id), TurnMeta("h", life_presence=_work()))
        assert w.hold_text("guild") in user
        assert "You only glance at the game now and then." in user and "at work" not in user
        asks = his_line(r, "how was work today")
        user = _user(r, reply(102, trigger=asks.id), TurnMeta("a", life_presence=_work()))
        assert "You're at work, the game open on your PC." in user  # he asked: not held
        switch(r, wow_topic=False)
        user = _user(r, reply(102, trigger=row.id), TurnMeta("o", life_presence=_work()))
        assert "You're at work, the game open on your PC." in user  # the switch off
    finally:
        r.close()


def test_a_held_turn_in_his_group_says_she_is_playing_with_him(store, clock, tmp_path):
    seed_test_cast(store)
    r = make_rig(store, clock, tmp_path, wow_topic=True, availability=6, coherence=True)
    try:
        r.sync(r.event(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102))))
        r.engine._own[102].append(("raid", 0, "still at the office, tanking from my desk"))
        row = his_line(r, "go when ready", channel="raid")
        user = _user(r, reply(102, channel="raid", trigger=row.id),
                     TurnMeta("g", life_presence=_work()))
        now = user.split("Right now:")[1].split("\n\n")[0]
        assert "You're at your PC, playing: you're in" in now
        assert "office" not in now and "at work" not in now  # never what she said of it
    finally:
        r.close()


def test_a_held_game_opener_leaves_what_is_on_her_mind_out(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", wants=["the Invented Drake mount"])
    r = make_rig(store, clock, tmp_path, wow_topic=True)
    try:
        r.store.put_state(replace(r.store.get_state(102), life_thread="her sister's wedding"))
        r.sync(r.event(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102, 103))))
        r.engine.bits.pick = lambda *a, **k: None
        turn, meta = r.engine._social_turn(TurnKind.OPEN, 102, "raid", addressee=None,
                                           trigger=None, room=2, now=clock.now(), delay=0.0)
        assert meta.game_topic
        text, _ = prompt(r, turn, meta)
        assert "On your mind lately" not in text and "wedding" not in text
        guild, guild_meta = r.engine._social_turn(TurnKind.OPEN, 102, "guild", addressee=None,
                                                  trigger=None, room=2, now=clock.now(),
                                                  delay=0.0)
        text, _ = prompt(r, guild, guild_meta)
        assert "On your mind lately: her sister's wedding." in text  # not held: as it was
    finally:
        r.close()


# -- instruction_entity_dropped through the real checks (the re-measure, 2026-11-24: it had never
# fired live; nothing in it was broken, the mind's own instructions carried no unknown name) ------

def test_the_minds_instruction_loses_an_unknown_boss_by_the_real_check(store, clock, tmp_path,
                                                                        caplog):
    from types import SimpleNamespace
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    r = make_rig(store, clock, tmp_path)
    try:
        library = {"marrowgar", "raid", "night", "guild", "wiped", "again"}
        r.engine.facts = SimpleNamespace(
            library=SimpleNamespace(known_words=lambda words: {x for x in words if x in library}))
        t = replace(reply(102, kind=TurnKind.ASIDE), addressee=None,
                    instruction="The guild wiped on Gloomfang twice. Say how the night went.")
        text, _ = prompt(r, t)
        assert "Gloomfang" not in text and "Say how the night went." in text
        assert "instruction_entity_dropped: guid=102 kind=aside n=1" in caplog.text
        known = replace(t, instruction="The guild wiped on Marrowgar twice. Say how it went.")
        caplog.clear()
        text, _ = prompt(r, known)
        assert "Marrowgar twice" in text and "instruction_entity_dropped" not in caplog.text
    finally:
        r.close()


def test_the_minds_instruction_loses_an_unknown_item_by_the_item_table(store, clock, tmp_path,
                                                                       caplog):
    from leeroy_mind.talk.items import ItemLinker
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    rows = [{"id": 50736, "name": "Invented Kryss", "quality": 4, "ilvl": 284, "req_level": 80}]
    items = ItemLinker(lambda: rows, clock.now)
    r = make_rig(store, clock, tmp_path, items=items)
    try:
        t = replace(reply(102, kind=TurnKind.ASIDE), addressee=None,
                    instruction="She wants [Made Up Blade]. Cheer the [Invented Kryss] drop.")
        text, _ = prompt(r, t)
        assert "Made Up Blade" not in text and "[Invented Kryss]" in text
        assert "instruction_entity_dropped: guid=102 kind=aside n=1" in caplog.text
    finally:
        r.close()
