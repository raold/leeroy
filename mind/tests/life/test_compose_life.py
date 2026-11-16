"""Her state, want and situation in the prompt (human-likeness core, plan Task 8): [talk] intent
draws her want on every eligible turn and puts it in the one block after the instruction (the
beat's block on his private turns, where the beat chooses among the wants the turn allows and
leans toward the drawn one), [life] state says how his line lands and how she feels, [talk]
situation keeps the hard rules in the system message and "Right now:" by the instruction, and
[life] availability says where she is. With every switch off the prompts are byte-identical.
Fakes only (FakeLLM, FakeClock, a scratch mind.db); invented lines, checked against no persona
text; the test cast."""
import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.bakeoff.capture import want_of
from leeroy_mind.config import load_config
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.learn import apply, arms, bandit, recipe
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.presence import WHERE_TEXT, Attention, LifePresence, Where, where_text
from leeroy_mind.life.seed import seeded
from leeroy_mind.life.state import State
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk.discord_length import num_predict
from leeroy_mind.talk.intent import TAIL_OFF, Intent, allowed, draw_intent, topic_for, want_words
from leeroy_mind.talk.pipeline import TurnMeta, _with_hint
from leeroy_mind.talk.prompt import (DM_BUDGET, ESTIMATE_SLACK, FLIRT_RULE, MORE_LINES_TEXT,
                                     SITUATION_INSTRUCTIONS, PromptParts, discord_length,
                                     length_rule, render_instruction, render_messages,
                                     situation_length, trim_to_budget)
from leeroy_mind.talk.romance import flirt_chance
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens
from tuning.scenario import golden, golden_beat, record, record_beat

NUM_CTX = LIVE.options["num_ctx"]
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages
HE = ("he", "him", "his")
BEAT = {"read": "he wants the evening to slow down a bit", "lands": "a small warm tug",
        "want": "share", "use": None}
NO_LANDS = {"read": "he is cross about the queue again", "want": "vent", "use": None}
BRAKES = {i.value for i in TAIL_OFF}


@pytest.fixture
def clock():
    return engine_clock()


def rig_of(store, clock, path, **kw):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    return make_rig(store, clock, path, **kw)


def said(r, text, *, guid=HUMAN, name="Testmage", channel="guild", to_guid=0):
    """A line in chat (stored and seen), without the engine planning answers to it."""
    ev = Chat(ts=0, channel=channel, from_guid=guid, from_name=name, to_guid=to_guid, text=text,
              map=0, zone=10)
    lid = r.store.insert_chat_line(ev, session_id=None,
                                   origin="human" if guid == HUMAN else "mind")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def model(beat, *lines):
    """`beat` (a dict, dumped, or text as it is) for the beat's request, `lines` in order for the
    line's (the last one repeats)."""
    left = list(lines) or ["fair enough then"]

    def reply(model_name, messages):
        if beats.MARK in messages[-1]["content"]:
            return json.dumps(beat) if isinstance(beat, dict) else beat
        return left.pop(0) if len(left) > 1 else left[0]
    return reply


def spy(r):
    """Every (turn, meta, the want drawn) _draw_situation leaves, in order."""
    seen, orig = [], r.engine._draw_situation

    def draw(turn, meta, p, now):
        orig(turn, meta, p, now)
        seen.append((turn, meta, meta.intent))
    r.engine._draw_situation = draw
    return seen


def last_user(call):
    return call["messages"][-1]["content"]


def beat_calls(r):
    return [c for c in r.chat_calls() if c["format"] is not None]


def line_calls(r):
    return [c for c in r.chat_calls() if c["format"] is None]


def speak(r, turn, meta, seconds=200.0):
    """The turn through the whole pipeline: its lines. The FakeClock moves a second at a time,
    with a little real time for the store's reader thread, until the turn ends."""
    async def go():
        task = asyncio.create_task(r.engine._speak(turn, meta))
        for _ in range(int(seconds)):
            if task.done():
                break
            await r.advance(1)
            await asyncio.sleep(0.002)
        return task.result()
    return r.sync(go())


def composed(r, turn, meta):
    """_draw_situation then the live composition with its block (Pipeline._compose)."""
    r.engine._draw_situation(turn, meta, r.engine.personas.get(turn.speaker), r.clock.now())
    return r.sync(r.engine._compose(turn, meta, live=True))


# -- every switch off ----------------------------------------------------------------------------

def test_switches_off_compose_byte_identical(store, clock, tmp_path):
    """The golden invariant with [talk] intent and situation and [life] state off."""
    assert record(store, clock, tmp_path) == golden()


def test_switches_off_keep_the_beats_golden(store, clock, tmp_path):
    """think-dm's private golden, with the same switches off."""
    assert record_beat(store, clock, tmp_path) == golden_beat()


def test_switches_read_from_mind_toml_and_invalid_means_off(tmp_path, caplog):
    path = tmp_path / "mind.toml"
    assert (load_config(path).talk.intent, load_config(path).talk.situation) == (False, False)
    path.write_text("[talk]\nintent = true\nsituation = true\n", encoding="utf-8")
    assert (load_config(path).talk.intent, load_config(path).talk.situation) == (True, True)
    path.write_text('[talk]\nintent = "yes"\nsituation = 1\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        cfg = load_config(path)
        load_config(path)
    assert (cfg.talk.intent, cfg.talk.situation) == (False, False)
    assert caplog.text.count("talk_intent_invalid") == 1
    assert caplog.text.count("talk_situation_invalid") == 1
    assert "yes" not in caplog.text


# -- the block on every eligible turn ------------------------------------------------------------

def test_block_on_eligible_turns_only(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True)
    try:
        r.script("sounds about right to me", "nah the stall restocks at dawn")
        q = said(r, "who has the key to the tram depot tonight?")
        assert r.speak_turn(Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN,
                                 trigger_line=q.id))
        m = said(r, "the trade stall ran dry of copper wire", guid=101, name="Arkon")
        assert r.speak_turn(Turn(TurnKind.CONTINUE, 103, "guild", addressee=101,
                                 trigger_line=m.id))
        assert len(r.chat_calls()) == 2
        for call, other in zip(r.chat_calls(), ("Arkon", "Zan")):
            rows = last_user(call).split("\n")
            assert sum(x.startswith("You want to: ") for x in rows) == 1
            assert rows[-1].startswith("You want to: ")  # think-dm's slot: after the instruction
            assert other not in rows[-1]  # his turn names him, the member's turn the member

        now = r.clock.now()
        pushback = (Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, trigger_line=q.id),
                    TurnMeta("pb", answers_human=True, pushback="doubt"))
        callout = (Turn(TurnKind.CALLOUT, 101, "raid"), TurnMeta("co"))
        for turn, meta in (pushback, callout):
            c = composed(r, turn, meta)
            assert meta.intent == "" and "You want to:" not in c.messages[-1]["content"]
        template = Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, trigger_line=q.id,
                        template="a fixed line")
        meta = TurnMeta("tpl", why="say_as")
        r.engine._draw_situation(template, meta, r.engine.personas.get(102), now)
        assert meta.intent == "" and r.engine._block(template, meta) == ""
        r.engine.server_app = SimpleNamespace(server=object())
        server = Turn(TurnKind.CONTINUE, 103, "discord:general", addressee=101)
        meta = TurnMeta("srv", why="server:general")
        r.engine._draw_situation(server, meta, r.engine.personas.get(103), now)
        assert meta.intent == "" and r.engine._block(server, meta) == ""
    finally:
        r.close()


def test_the_want_is_seeded_by_turn_never_by_the_engines_rng(store, clock, tmp_path, caplog):
    r = rig_of(store, clock, tmp_path, intent=True)
    try:
        q = said(r, "who has the key to the tram depot tonight?")
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, trigger_line=q.id)
        state = r.engine.rng.getstate()
        first, second = TurnMeta("a"), TurnMeta("b")
        p = r.engine.personas.get(102)
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            r.engine._draw_situation(turn, first, p, r.clock.now())
            r.engine._draw_situation(turn, second, p, r.clock.now())
        assert r.engine.rng.getstate() == state
        assert first.intent == second.intent and first.want_source == "draw"
        assert (f"talk_want: guid=102 kind=reply_human want={first.intent} source=draw owed="
                in caplog.text)
        assert "tram depot" not in caplog.text
    finally:
        r.close()


# -- his private turns: the beat chooses among the allowed wants, leaning toward the drawn one -----

def test_private_turn_beat_chooses_among_allowed_with_a_lean(store, clock, tmp_path, caplog):
    r = rig_of(store, clock, tmp_path, intent=True, beat=True)
    try:
        seen = spy(r)
        r.llm.set_script(model({**BEAT, "want": "tease"}, "oh is that so, mister"))
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            r.whisper_from_him(102, "my manager kept me back late again")
            r.run()
        [(_, meta, drawn)] = seen
        ask, line = beat_calls(r)[0], line_calls(r)[0]
        enum = ask["format"]["properties"]["want"]["enum"]
        assert meta.intent_ctx.owed and "react" not in enum  # an owed answer adds something
        assert enum == [i.value for i in allowed(meta.intent_ctx)]
        assert drawn in enum
        assert (f"You lean toward {drawn} ({want_words(Intent(drawn), 'Zan')})."
                in last_user(ask))
        assert last_user(line).endswith("\nYou want to: tease Zan")
        assert (meta.intent, meta.want_source) == ("tease", "beat")
        assert f"want=tease use=0 ms=" in caplog.text and f"lean={drawn}" in caplog.text

        # a beat that fails the schema: the drawn want is the turn's
        r.llm.set_script(model("not json at all", "fine, tell me then"))
        r.whisper_from_him(102, "and the printer jammed twice")
        r.run()
        _, meta, drawn = seen[-1]
        want = Intent(drawn)
        topic = topic_for(want, meta.intent_ctx)
        assert last_user(line_calls(r)[-1]).endswith(
            "\nYou want to: " + want_words(want, "Zan", topic))
        assert meta.want_source == "draw"
    finally:
        r.close()


def test_a_fond_womans_private_turn_offers_and_leans_toward_no_flirt(store, clock, tmp_path):
    """Review-332: on his private turns the beat's gate (romance.flirt_chance) wins for flirting:
    a fond woman's beat offers no flirt and is never leant toward one; on his public turns the
    table's x0.3 stands."""
    seed_test_cast(store)
    save_member(store, 105, player_gender="female", stance="fond", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path, intent=True, beat=True)
    try:
        p = r.engine.personas.get(105)
        assert flirt_chance(p, 1.0) == 0
        seen = spy(r)
        r.llm.set_script(model(BEAT, "aw stop it you"))
        r.whisper_from_him(105, "you looked lovely in that green robe tonight")
        r.run()
        ctx = seen[0][1].intent_ctx
        assert "flirt" not in beat_calls(r)[0]["format"]["properties"]["want"]["enum"]
        assert Intent.FLIRT not in allowed(ctx)
        assert all(draw_intent(ctx, seeded("intent", "2026-10-29", 105, m, 1))
                   is not Intent.FLIRT for m in range(200))
        q = said(r, "you looked lovely in that green robe tonight")
        meta = TurnMeta("pub")
        r.engine._draw_situation(Turn(TurnKind.REPLY_HUMAN, 105, "guild", addressee=HUMAN,
                                      trigger_line=q.id), meta, p, r.clock.now())
        assert Intent.FLIRT in allowed(meta.intent_ctx)
    finally:
        r.close()


@pytest.mark.parametrize("state", [True, False])
def test_state_replaces_the_beats_lands(store, clock, tmp_path, state):
    r = rig_of(store, clock, tmp_path, beat=True, life_state=state)
    try:
        r.llm.set_script(model(NO_LANDS if state else {**NO_LANDS, "lands": "a sharp little sting"},
                               "ugh fine, what now"))
        r.whisper_from_him(102, "ugh you are so annoying today")
        r.run()
        ask, line = beat_calls(r)[0], line_calls(r)[0]
        props, needed = ask["format"]["properties"], ask["format"]["required"]
        assert ("lands" in props, "lands" in needed) == (not state, not state)
        assert ('"lands":' in last_user(ask)) is not state
        lands = "it annoys you" if state else "a sharp little sting"  # agreeableness 5: annoyed
        assert last_user(line).endswith(
            "\nYou read it as: he is cross about the queue again\nIt lands: " + lands
            + "\nYou want to: get something off your chest")
    finally:
        r.close()


def test_overlay_tail_turn_draws_no_brakes(store, clock, tmp_path):
    """PM ruling (2): under his explicit whisper overlay she never vents, gossips, deflects,
    draws a line, sorts out plans or keeps it light: not in 200 seeded draws, nor in the beat's
    enum. A lust woman at the rig's dial (the explicit band) with the overlay file."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_bytes(b"WHISPER-OVERLAY\n")
    r = make_rig(store, clock, tmp_path, intent=True, beat=True)
    try:
        seen = spy(r)
        r.llm.set_script(model({**BEAT, "want": "tease"}, "come here then"))
        r.whisper_from_him(102, "long shift, dead on my feet")
        r.run()
        ctx = seen[0][1].intent_ctx
        assert ctx.tail
        drawn = {draw_intent(ctx, seeded("intent", "2026-10-29", 102, m, 7)) for m in range(200)}
        assert None not in drawn and not {i.value for i in drawn} & BRAKES
        enum = beat_calls(r)[0]["format"]["properties"]["want"]["enum"]
        assert enum and not set(enum) & BRAKES
    finally:
        r.close()


# -- her state ----------------------------------------------------------------------------------

def test_feeling_toward_him_only_on_his_turns(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, life_state=True)
    try:
        r.engine.life.state_of = lambda guid: State(0.0, 0.0, 0.0, 0.0, 0.5)
        r.script("mm not bad, you?", "ha the stall is always empty")
        r.whisper_from_him(102, "how is your evening going")
        r.run()
        assert "You're happy with him right now." in last_user(r.chat_calls()[-1])
        m = said(r, "the trade stall ran dry of copper wire", guid=101, name="Arkon")
        c = composed(r, Turn(TurnKind.CONTINUE, 102, "guild", addressee=101, trigger_line=m.id),
                     TurnMeta("member"))
        user = c.messages[-1]["content"]
        assert "Right now:" in user and "happy with him" not in user
    finally:
        r.close()


def test_state_off_reads_nothing(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True)
    try:
        assert r.engine.life.state is None
        q = said(r, "who has the key to the tram depot tonight?")
        meta = TurnMeta("x")
        r.engine._draw_situation(Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN,
                                      trigger_line=q.id), meta, r.engine.personas.get(102),
                                 r.clock.now())
        assert meta.state is None and meta.lands == "" and meta.intent
    finally:
        r.close()


# -- where she is -----------------------------------------------------------------------------------

def test_where_she_is_in_right_now_never_in_a_raid_call(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, availability=6)
    try:
        work = LifePresence(Block.WORK, Where.AT_WORK, Attention.GLANCING, DayEvent.NONE, False)
        q = said(r, "who has the key to the tram depot tonight?")
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, trigger_line=q.id)
        meta = TurnMeta("w", life_presence=work)
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "You're at work, the game open on your PC." in user
        assert "It's Thursday evening." in user  # her own clock: 20:00 GMT
        meta = TurnMeta("c", life_presence=work)
        assert "You're at work" not in composed(r, Turn(TurnKind.CALLOUT, 101, "raid"),
                                                meta).messages[-1]["content"]
    finally:
        r.close()


def test_where_text_reads_by_block_medium_and_kids():
    def lp(block, kids=False):
        return LifePresence(block, Where.BUSY, Attention.HALF, DayEvent.NONE, kids)
    assert where_text(lp(Block.ASLEEP), "game") == "You're half asleep at the keyboard."
    assert where_text(lp(Block.ASLEEP), "discord") == "Your phone woke you up."
    assert where_text(lp(Block.WORK), "discord") == (
        "You're at work, checking your phone between tasks.")
    assert where_text(lp(Block.HOME_DUTY, True), "game") == "You're at home sorting the kids out."
    assert where_text(lp(Block.HOME_DUTY), "discord") == "You're busy at home."
    # in his group in game while he plays (PM log D15): at her PC, whatever her plan says
    assert where_text(lp(Block.WORK), "discord", playing=True) == "You're at your PC, playing."
    for block in Block:
        for medium in ("game", "discord"):
            text = where_text(lp(block), medium)
            assert text in WHERE_TEXT.values() and ";" not in text


def test_a_carried_dm_answer_takes_her_discord_presence_now(store, clock, tmp_path):
    """Review-336 note 1: a whisper answer carried to her DM planned with her game presence;
    as she answers in Discord she is read on her phone."""
    r = rig_of(store, clock, tmp_path, availability=6)
    try:
        game = r.engine.life.presence(102, medium="game")
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
        meta = TurnMeta("carry", answers_human=True, owed_answer=True, why="reply:carried",
                        recall_query="how is your evening going", life_presence=game)
        r.engine._draw_situation(turn, meta, r.engine.personas.get(102), r.clock.now())
        assert meta.life_presence == r.engine.life.presence(102, medium="discord")
        assert meta.life_presence.attention != game.attention  # gaming: full in game, half here
    finally:
        r.close()


# -- the situation mode -----------------------------------------------------------------------------

def test_situation_system_message_is_hard_rules(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, intent=True, situation=True)
    try:
        r.script("first thing i said tonight", "and a second thing then")
        for text in ("who has the key to the tram depot tonight?", "anyone else still up?"):
            q = said(r, text)
            assert r.speak_turn(Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN,
                                     trigger_line=q.id))
        call = r.chat_calls()[-1]
        system, user = call["messages"][0]["content"], call["messages"][1]["content"]
        assert "never say the year" in system.lower()
        assert FLIRT_RULE["female"].format(human="Zan") in system
        assert "usually" not in system and "Specific beats polite" not in system
        assert (user.index("Your last lines") < user.index("Right now:")
                < user.index("Reply to Zan's last line, as yourself.")
                < user.index("\nYou want to: "))
        typing = r.engine.personas.get(102).typing
        assert length_rule(typing) in user  # her usual line, given by the pipeline
        assert "respond to what" not in user.lower() and "one short line" not in user
    finally:
        r.close()


@pytest.mark.parametrize("channel", ["guild", "party", "whisper", "voice:3", "discord:general",
                                     "dm:102", "gdm:4"])
@pytest.mark.parametrize("words", [0, 90])
def test_every_situation_turn_has_a_length(channel, words):
    """Review of #333, requirement 1: every situation template gets a length from the pipeline,
    its own or the Discord one; the other kinds keep their templates' lengths."""
    typing = make_persona(101, "Arkon").typing
    for kind in TurnKind:
        given = situation_length(kind, channel=channel, words=words, typing=typing)
        if kind not in SITUATION_INSTRUCTIONS:
            assert given == ""
            continue
        on_discord = discord_length(channel, words)
        assert bool(given) is not bool(on_discord)
        text = render_instruction(kind, addressee="Zan", topic="TOPIC", forms=HE,
                                  channel=channel, discord_words=words, length_text=given,
                                  situation=True, typing=typing)
        assert (given or on_discord) in text
        assert situation_length(kind, channel=channel, words=words, typing=typing,
                                two_lines=True) == MORE_LINES_TEXT


def test_a_dm_at_zero_words_in_the_situation_mode_is_answered(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, situation=True)
    try:
        cfg = r.engine.cfg
        r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, dm_words=0)))
        r.script("hey you, all good here")
        mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human",
                          text="are you around later", now=r.clock.now())
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
        meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None))
        assert speak(r, turn, meta)
        assert length_rule(r.engine.personas.get(102).typing) in last_user(r.chat_calls()[-1])
    finally:
        r.close()


# -- the worst-case DM (Review Focus 5) ---------------------------------------------------------------

def _para(i: int, chars: int) -> str:
    words = ("the ferry was late again so we waited by the old pier and talked about "
             f"nothing much at all for round {i} ").split()
    out = []
    while len(" ".join(out)) < chars:
        out += words
    return " ".join(out)[:chars].rstrip()


def test_worst_case_dm_fits(store, clock, tmp_path):
    """His DM answered with awareness on, availability 6, state, intent, situation, the beat's
    four lines, a 600-character post and 12 history rows: the line's prompt, its block included,
    is within DM_BUDGET; Right now and the block are whole; with the answer's num_predict it fits
    num_ctx."""
    r = rig_of(store, clock, tmp_path, intent=True, situation=True, life_state=True,
               availability=6, beat=True)
    try:
        cfg = r.engine.cfg
        r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, aware=True)))
        base, rows = r.clock.now() - 3600, []
        for i in range(12):
            mine = i % 2 == 0
            text = _para(i, 900)
            mid = dstore.post(r.store, channel="dm:102", author=HUMAN if mine else 102,
                              origin="human" if mine else "mind", text=text, now=base + i)
            rows.append(ChatLineRow(mid, base + i, "dm:102", HUMAN if mine else 102,
                                    "Testmage" if mine else "Belra", 102 if mine else HUMAN,
                                    text, 0, 0, None, None, "human" if mine else "mind"))
        post = "thanks for earlier, " + _para(99, 580)
        mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human", text=post,
                          now=r.clock.now())
        read = _para(7, beats.READ_CHARS)
        r.llm.set_script(model({"read": read, "want": "share", "use": 1}, "aw that was nice"))
        # as discord.talk asks it: his post in the instruction, the DM's history as its chat
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN,
                    instruction="Zan wrote to you: " + post)
        meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None),
                        discord_chat=tuple(rows))
        assert speak(r, turn, meta)
        line = line_calls(r)[0]
        total = sum(estimate_tokens(m["content"]) for m in line["messages"])
        assert len(beat_calls(r)) == 1 and last_user(line).count("(Discord DM") < 12  # trimmed
        assert total <= DM_BUDGET
        assert total * ESTIMATE_SLACK + TEMPLATE_TOKENS + num_predict(200) <= NUM_CTX
        user = last_user(line)
        lp = r.engine.life.presence(102, medium="discord")
        assert "Right now:" in user and where_text(lp, "discord") in user
        block = user.split("\n")[-4:]
        assert block[0] == "You read it as: " + read
        assert block[1] == "It lands: it makes you smile"  # thanks: warm
        assert block[2].startswith("You want to: share something of your own")
        assert block[3].startswith("Bring in: ")
    finally:
        r.close()


def test_worst_case_dm_parts_fit_with_the_block_counted():
    """Review-333's extreme DM (an untrimmed section, a long awareness card, Right now with where
    and state words, a 600-character post, twelve long rows), trimmed to DM_BUDGET less the beat's
    block at its caps as the pipeline does: with the block at its caps it is within DM_BUDGET and
    num_ctx, and the untrimmed section, Right now and the block are whole."""
    typing = make_persona(102, "Belra").typing
    instruction = render_instruction(
        TurnKind.REPLY_HUMAN, addressee="Zan", forms=HE, channel="dm:102", discord_words=200,
        extra=_para(1, 600), situation=True, typing=typing, lead=_para(2, 400), after=_para(3, 200))
    mood = ("It's Thursday evening. You're at your PC, playing. You're tired, a bit short and "
            "in a bad mood today. " + _para(4, 160))
    untrimmed = "About you:\n" + _para(5, 1340)
    parts = PromptParts(
        card=_para(6, 1200), samples=[_para(i, 120) for i in range(5)], rules=_para(8, 1720),
        rel_cards=[_para(i, 300) for i in range(3)], memories=[_para(i, 240) for i in range(10)],
        mood=mood, chat=[_para(i, 900) for i in range(12)] + ["Zan: " + _para(9, 600)],
        own_recent=[_para(i, 200) for i in range(5)], instruction=instruction,
        awareness="What you notice:\n" + _para(10, 1400), untrimmed=[untrimmed],
        elsewhere=[_para(i, 200) for i in range(6)], style=[_para(i, 150) for i in range(6)],
        situation=True)
    lands = "nice, but you're still a bit sore with him"
    room = beats.block_room("Zan", lands=lands)
    messages = render_messages(trim_to_budget(parts, budget=DM_BUDGET - room))
    block = beats.render(read="r" * beats.READ_CHARS, lands=lands, want=Intent.REASSURE_SEEK,
                         who="Zan", topic="t" * beats.TOPIC_CHARS, use="u" * beats.ITEM_CHARS)
    assert estimate_tokens("\n" + block) <= room
    final = _with_hint(messages, block)
    total = sum(estimate_tokens(m["content"]) for m in final)
    assert total <= DM_BUDGET
    assert total * ESTIMATE_SLACK + TEMPLATE_TOKENS + num_predict(200) <= NUM_CTX
    user = final[-1]["content"]
    assert user.startswith(untrimmed) and user.endswith(instruction + "\n" + block)
    assert "Right now:\n" + mood + "\n\n" + instruction in user


def test_the_beats_block_never_outgrows_its_room():
    for who in ("Zan", "Someone-With-A-Long-Name"):
        room = beats.block_room(who)
        for want in Intent:
            block = beats.render(read="r" * beats.READ_CHARS, lands="l" * beats.LANDS_CHARS,
                                 want=want, who=who, topic="t" * beats.TOPIC_CHARS,
                                 use="u" * beats.ITEM_CHARS)
            assert estimate_tokens("\n" + block) <= room
    ctx_kinds = (TurnKind.REPLY_HUMAN, TurnKind.CONTINUE, TurnKind.WHISPER)
    from leeroy_mind.talk.intent import IntentContext
    for kind in ctx_kinds:
        for event in DayEvent:
            for block in (*Block, None):
                ctx = IntentContext(kind, True, False, "", "game", True, "lust", None, "secure",
                                    "dry", 5, 5, False, None, None, event, block, True, None)
                assert all(len(topic_for(i, ctx)) <= beats.TOPIC_CHARS for i in Intent)


# -- the beat module's new keywords ---------------------------------------------------------------

def test_the_beats_ask_takes_a_lean_and_can_leave_lands_out():
    wants, items = beats.wants_for(True), ["a memory"]
    plain = beats.instruction("Zan", wants, items, "his")
    assert beats.instruction("Zan", wants, items, "his", lean=None, ask_lands=True) == plain
    leant = beats.instruction("Zan", wants, items, "his", lean=Intent.TEASE)
    assert ("the one that fits best. You lean toward tease (tease Zan). One of:" in leant
            and ";" not in leant)
    bare = beats.instruction("Zan", wants, items, "his", ask_lands=False)
    assert '"lands":' not in bare and '"read":' in bare and '"want":' in bare
    schema = beats.schema(wants, "Zan", "his", ask_lands=False)
    assert "lands" not in schema["properties"] and schema["required"] == ["read", "want", "use"]
    assert beats.schema(wants, "Zan", "his")["required"] == ["read", "lands", "want", "use"]
    found, why = beats.parse(json.dumps(NO_LANDS), wants, items, ask_lands=False)
    assert why == "" and found.lands == "" and found.want == "vent"
    assert beats.parse(json.dumps(NO_LANDS), wants, items) == (None, "schema")
    assert beats.parse(json.dumps({**NO_LANDS, "read": " "}), wants, items,
                       ask_lands=False) == (None, "empty")


def test_the_capture_reads_a_want_with_its_topic():
    assert want_of("You read it as: x\nYou want to: get something off your chest (about work)") \
        == "vent"
    assert want_of("You want to: tease Zan") == "tease"
    assert want_of("You want to: sort out the plan (raid times, gear or who's coming)") \
        == "logistics"


# -- learning ---------------------------------------------------------------------------------

def test_learned_tease_arm_yields_to_the_want(store, clock, tmp_path, monkeypatch):
    def choose(conn, guid, context, **kwargs):
        arms.ensure(conn)
        return arms.controls() | ({"tease": "tease.1.v1"} if context.tease else {})
    monkeypatch.setattr(bandit, "choose", choose)
    r = rig_of(store, clock, tmp_path)
    try:
        r.engine.learn_enabled = True
        r.engine.learn_settings = LearnCfg(enabled=True, bandit=True, apply=True)
        q = said(r, "who has the key to the tram depot tonight?")
        for intent in (False, True):
            cfg = r.engine.cfg
            r.engine.cfg = replace(cfg, talk=replace(cfg.talk, intent=intent))
            turn = Turn(TurnKind.REPLY_HUMAN, 102, "guild", to_guid=HUMAN, addressee=HUMAN,
                        trigger_line=q.id, max_lines=1)
            meta = TurnMeta(r.engine._new_id())
            user = composed(r, turn, meta).messages[-1]["content"]
            assert bool(meta.intent) is intent
            assert ("teasing" in user) is not intent
            ids = meta.recipe_snapshot.arm_ids
            assert ("tease.1.v1" in ids) is not intent
            assert (arms.controls()["tease"] in ids) is intent
            apply.release(r.engine, meta)
    finally:
        r.close()


def test_yield_families_credits_control_and_drops_length_tokens():
    applied = apply.Applied(None, {"tease": "tease.1.v1", "length": "length.0.v1",
                                   "humour": "humour.1.v1"},
                            [("arm", "tease.1.v1", "t"), ("arm", "length.0.v1", "l"),
                             ("arm", "humour.1.v1", "h"), ("lesson", 3, "n")], num_predict=40)
    apply.yield_families(applied, {"tease"})
    assert [e[1] for e in applied.entries] == ["length.0.v1", "humour.1.v1", 3]
    assert applied.num_predict == 40
    apply.yield_families(applied, {"length"})
    assert [e[1] for e in applied.entries] == ["humour.1.v1", 3] and applied.num_predict is None
    apply.yield_families(None, {"tease"})  # nothing applied: nothing to do


def test_the_recipe_versions_on_the_situation_templates():
    from leeroy_mind.talk import prompt
    p = make_persona(101, "Arkon")
    base = load_config(__import__("pathlib").Path("absent.toml"))
    on = replace(base, talk=replace(base.talk, situation=True))
    reply = Turn(TurnKind.REPLY_HUMAN, 101, "guild")
    greet = Turn(TurnKind.GREET, 101, "guild")
    off_r, on_r = recipe.describe(reply, p, base), recipe.describe(reply, p, on)
    assert off_r.template_id == "talk.instruction.reply_human"
    assert on_r.template_id == "talk.situation_instruction.reply_human"
    assert off_r.template_version != on_r.template_version
    on_g = recipe.describe(greet, p, on)
    assert on_g.template_id == "talk.instruction.greet"
    assert on_g.template_version != recipe.describe(greet, p, base).template_version
    import hashlib
    assert on_r.template_version == hashlib.sha256(
        (prompt.SITUATION_RULES + prompt.SITUATION_INSTRUCTIONS[TurnKind.REPLY_HUMAN])
        .encode()).hexdigest()[:16]
