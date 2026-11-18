"""Reply shape in the pipeline (human-likeness core, plan Task 9): with [talk] shape on, every
turn that gets a want and is typed draws its shape (talk/shape.py) as she starts composing: its
length line in the prompt, its words through the "long" tell, the trim and the typing, a game
burst on two lines and a Discord burst split by realism, a late answer that says so, and on a
channel a line left on read or only a reaction. An answer he is owed is never left on read,
never a reaction and never a word or two (Review Focus 4). A DM's length follows his by
[talk] dm_mirror (0: exactly today's). Fakes only (FakeLLM, FakeClock, a scratch mind.db);
invented lines, checked against no persona text; the test cast."""
import asyncio
import logging
from dataclasses import replace

import pytest

from conftest import HUMAN, seed_test_cast
from dbtemplate import open_migrated
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.config import load_config
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.learn import apply, arms, bandit
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.presence import Attention, LifePresence, Where, where_text
from leeroy_mind.life.state import State, words as state_words
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.server.reacting import PALETTE
from leeroy_mind.storage.store import Store
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk import pipeline as pipeline_mod
from leeroy_mind.talk.discord_length import num_predict
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import DM_BUDGET, ESTIMATE_SLACK, length_rule
from leeroy_mind.talk.shape import LINE_ONE_WORD, Shape, ShapeDraw
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens
from tuning.scenario import golden, record

NUM_CTX = LIVE.options["num_ctx"]
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages
SMALL = {Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD}
DROPS = ("left_on_read", "reacted")


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


def speak(r, turn, meta, seconds=200.0):
    """The turn through the whole pipeline: its lines."""
    async def go():
        task = asyncio.create_task(r.engine._speak(turn, meta))
        for _ in range(int(seconds)):
            if task.done():
                break
            await r.advance(1)
            await asyncio.sleep(0.002)
        return task.result()
    return r.sync(go())


def drawn(r, turn, meta):
    """_draw_situation then _draw_shape, as _speak_now draws them: the turn's shape."""
    p = r.engine.personas.get(turn.speaker)
    r.engine._draw_situation(turn, meta, p, r.clock.now())
    r.engine._draw_shape(turn, meta, p, r.clock.now())
    return turn.shape


def composed(r, turn, meta):
    """The shape drawn, then the live composition with its block (Pipeline._compose)."""
    drawn(r, turn, meta)
    return r.sync(r.engine._compose(turn, meta, live=True))


def line_calls(r):
    return [c for c in r.chat_calls() if c["format"] is None]


def beat_calls(r):
    return [c for c in r.chat_calls() if c["format"] is not None]


def last_user(call):
    return call["messages"][-1]["content"]


def dropped(r):
    return [row["dropped"] for row in r.utterances() if row["dropped"] is not None]


def force(monkeypatch, draw):
    """Every shape drawn is `draw`."""
    monkeypatch.setattr(pipeline_mod, "draw_shape", lambda ctx, rng: draw)


def dm_turn(r, text, guid=102):
    mid = dstore.post(r.store, channel=f"dm:{guid}", author=HUMAN, origin="human", text=text,
                      now=r.clock.now())
    turn = Turn(TurnKind.REPLY_HUMAN, guid, f"dm:{guid}", to_guid=HUMAN, addressee=HUMAN)
    meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None))
    return turn, meta


def channel_turn(r, text, guid=103, *, owed=False):
    """A member's answer to his post in #general: the first (owed) or a later one."""
    mid = dstore.post(r.store, channel="general", author=HUMAN, origin="human", text=text,
                      now=r.clock.now())
    turn = Turn(TurnKind.REPLY_HUMAN, guid, "discord:general", addressee=HUMAN)
    meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None), owed_answer=owed)
    return turn, meta, mid


def varied():
    """A FakeLLM script whose replies share few words (no repeat or clause tells over a run)."""
    a = ("honestly", "wait", "nah", "mm", "ok but", "lmao", "yeah well")
    b = ("the tank", "my bags", "that wipe", "our priest", "our dps", "the boat ride")
    c = ("again", "for real", "each round", "tonight", "somehow", "as usual", "no joke", "lately")
    n = [0]

    def reply(model, messages):
        n[0] += 1
        i = n[0]
        return f"{a[i % 7]} {b[i % 6]} {c[i % 8]} {i}"
    return reply


def guild_answer(r, line, guid=103, *, owed=False):
    """A member's answer to his guild line: the one he is owed, or a later one."""
    turn = Turn(TurnKind.REPLY_HUMAN, guid, "guild", addressee=HUMAN, trigger_line=line.id)
    return turn, TurnMeta(r.engine._new_id(), answers_human=True, owed_answer=owed)


# -- off is today ------------------------------------------------------------------------------

def test_shape_off_and_dm_mirror_zero_change_nothing(store, clock, tmp_path):
    """Shape off: the golden invariant. Shape on with dm_mirror 0: an answer to his DM sends
    exactly the requests and lines of shape off (two rigs over two scratch databases)."""
    assert record(store, clock, tmp_path) == golden()

    def run_dm(shape: bool) -> tuple[list, list]:
        path = tmp_path / ("on" if shape else "off")
        path.mkdir()
        s = Store(open_migrated(path / "mind.db"), clock=clock)
        r = rig_of(s, clock, path, shape=shape, dm_mirror=0, intent=True, situation=True,
                   life_state=True, availability=6)
        try:
            r.script("ha, it was a long day here too", "and the queue was awful")
            turn, meta = dm_turn(r, "how did the dungeon run go after i left")
            assert speak(r, turn, meta)
            assert turn.shape is None and meta.shape_ctx is None
            calls = [(c["messages"], c["options"]) for c in r.chat_calls()]
            return calls, [m["text"] for m in dstore.messages(s.conn, "dm:102")]
        finally:
            r.close()
            s.close()
    assert run_dm(False) == run_dm(True)


def test_shape_and_dm_mirror_are_read_from_mind_toml_and_invalid_means_default(tmp_path, caplog):
    path = tmp_path / "mind.toml"
    assert (load_config(path).talk.shape, load_config(path).talk.dm_mirror) == (False, 5)
    path.write_bytes(b"[talk]\nshape = true\ndm_mirror = 8\n")
    assert (load_config(path).talk.shape, load_config(path).talk.dm_mirror) == (True, 8)
    caplog.set_level(logging.WARNING)
    path.write_bytes(b"[talk]\nshape = \"yes\"\ndm_mirror = 11\n")
    cfg = load_config(path)
    assert (cfg.talk.shape, cfg.talk.dm_mirror) == (False, 5)
    assert any("talk_shape_invalid key=shape" in m for m in caplog.messages)
    assert any("talk_dm_mirror_invalid key=dm_mirror" in m for m in caplog.messages)


def test_the_draw_is_its_own_stream_never_the_engines_rng(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    r = rig_of(store, clock, tmp_path, shape=True, intent=True)
    try:
        q = said(r, "who still has the spare hearthstone")
        before = r.engine.rng.getstate()
        turn, meta = guild_answer(r, q, owed=True)
        first = drawn(r, turn, meta)
        assert r.engine.rng.getstate() == before
        again, _ = guild_answer(r, q, owed=True)
        assert drawn(r, again, TurnMeta("x", answers_human=True, owed_answer=True)) == first
        logs = [m for m in caplog.messages if m.startswith("talk_shape:")]
        assert logs and logs[0] == (f"talk_shape: guid=103 kind=reply_human "
                                    f"shape={first.shape.value} words={first.words} owed=1")
    finally:
        r.close()


# -- Review Focus 4: owed answers and the small shapes -------------------------------------------

def test_owed_answers_never_left_or_reacted(store, clock, tmp_path):
    """His whispers and DMs over 200 seeded minutes never draw a small shape; spoken through the
    pipeline, every one is answered with words and no row is left on read or reacted."""
    r = rig_of(store, clock, tmp_path, shape=True, intent=True)
    try:
        cfg = r.engine.cfg
        r.engine.cfg = replace(cfg, talk=replace(cfg.talk, realism=True))  # Discord bursts too
        w = said(r, "you around tonight", channel="whisper", to_guid=102)
        for minute in range(100):
            turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                        trigger_line=w.id)
            got = drawn(r, turn, TurnMeta("w", answers_human=True, whisper_reply=True))
            assert got is not None and got.shape not in SMALL
            turn, meta = dm_turn(r, "you around tonight")
            got = drawn(r, turn, meta)
            assert got is not None and got.shape not in SMALL
            r.run(60, step=60)  # the next minute: another seed
        r.script("yeah, here now", "mm here, what's up", "sure, give me a sec")
        for i in range(6):
            w = said(r, f"you free for the crypt run {i}", channel="whisper", to_guid=102)
            turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                        trigger_line=w.id)
            assert speak(r, turn, TurnMeta(r.engine._new_id(), answers_human=True,
                                           whisper_reply=True))
            assert speak(r, *dm_turn(r, f"you free for the crypt run {i}"))
            r.run(60, step=60)
        assert not set(dropped(r)) & set(DROPS)
    finally:
        r.close()


def test_non_owed_guild_answer_can_be_left_on_read(store, clock, tmp_path, caplog):
    """A later answer to his guild remark draws over 40 lines of his: some are left on read
    (dropped before any model call), the rest answered; the owed answer never is."""
    caplog.set_level(logging.INFO)
    r = rig_of(store, clock, tmp_path, shape=True, intent=True)
    try:
        r.llm.set_script(varied())
        left = said_ = 0
        for i in range(40):
            q = said(r, f"that pull went sideways fast {i}")
            calls = len(r.chat_calls())
            lines = speak(r, *guild_answer(r, q))
            if dropped(r)[-1:] == ["left_on_read"] and not lines:
                left += 1
                assert len(r.chat_calls()) == calls  # no model call for it
            elif lines:
                said_ += 1
            assert speak(r, *guild_answer(r, q, guid=104, owed=True))
            r.run(60, step=60)
        assert left >= 3 and said_ >= 5
        rows = [row for row in r.utterances() if row["guid"] == 104]
        assert rows and all(row["dropped"] not in DROPS for row in rows)
        assert any("shape=leave" in m and "owed=0" in m for m in caplog.messages)
    finally:
        r.close()


def test_discord_reaction_shape_posts_an_emoji_without_a_model_call(store, clock, tmp_path,
                                                                   monkeypatch):
    force(monkeypatch, ShapeDraw(Shape.REACTION, 0, 0, None))
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        turn, meta, mid = channel_turn(r, "finally downed the last boss lol")
        assert speak(r, turn, meta) == []
        assert r.chat_calls() == []
        (got,) = dstore.reactions(r.store.conn, [mid])[mid]
        assert got["members"] == [103] and got["emoji"] in PALETTE["joke"]  # a laugh
        row = r.utterances()[-1]
        assert row["dropped"] == "reacted"
        assert r.store.conn.execute("SELECT guard_verdict FROM utterance ORDER BY id DESC"
                                    ).fetchone()[0] == "react"
        # nothing to react to (no message it replies to): a word or two instead
        r.script("ha nice")
        turn = Turn(TurnKind.CONTINUE, 104, "discord:general", addressee=103)
        meta = TurnMeta(r.engine._new_id(), discord=DiscordTarget(reply_to=None, thread=None))
        assert drawn(r, turn, meta).shape is Shape.ONE_WORD
    finally:
        r.close()


def test_one_word_shape_does_not_loop_retries(store, clock, tmp_path, monkeypatch):
    """A word or two whose model reply is nine words: asked once more (told so), then sent or
    dropped; never a third ask. Its room is the shape's 24 tokens."""
    force(monkeypatch, ShapeDraw(Shape.ONE_WORD, 2, 1, 24))
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        r.script("well that was a long way round to the boss room")
        q = said(r, "who forgot the water again")
        speak(r, *guild_answer(r, q))
        calls = line_calls(r)
        assert len(calls) == 2  # the "long" tell asked once more, then no more
        assert all(c["options"]["num_predict"] == 24 for c in calls)
        assert LINE_ONE_WORD in last_user(calls[0])
        assert last_user(calls[1]).endswith(pipeline_mod.ONE_WORD_HINT)
        r.run(120)
        assert len(line_calls(r)) == len(calls)  # nothing asked later either
    finally:
        r.close()


# -- lengths -----------------------------------------------------------------------------------

def test_dm_length_follows_dm_mirror(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    r = rig_of(store, clock, tmp_path, shape=True, dm_mirror=10)
    try:
        r.script("ugh mine was slow, the queue ate half of it",
                 "same as ever, the printer jammed twice before lunch")  # never her last again
        turn, meta = dm_turn(r, "long day at work here, how was yours")  # 8 words
        assert speak(r, turn, meta)
        assert turn.shape is not None and meta.shape_ctx.his_words == 8
        assert line_calls(r)[-1]["options"]["num_predict"] <= num_predict(30)
        cfg = r.engine.cfg
        r.engine.cfg = replace(cfg, talk=replace(cfg.talk, dm_mirror=0))
        turn, meta = dm_turn(r, "long day at work here, how was yours")
        assert speak(r, turn, meta)
        assert turn.shape is None and line_calls(r)[-1]["options"]["num_predict"] == 344
    finally:
        r.close()


def test_a_carried_dm_mirrors_his_whisper(store, clock, tmp_path):
    """review-336 note 3: a whisper answer carried to her DM mirrors his whisper (its recall
    query), not an old post in the DM; CARRY_TEXT is its only excuse (no LATE in a DM)."""
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human",
                    text="ok", now=r.clock.now() - 86400)
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
        meta = TurnMeta("c", answers_human=True, owed_answer=True, why="reply:carried",
                        recall_query="did you ever find the second key for the vault door",
                        heard_at=r.clock.now() - 900)
        got = drawn(r, turn, meta)
        assert got is not None and got.shape is not Shape.LATE
        assert meta.shape_ctx.his_words == 11 and meta.shape_ctx.late_by == 0.0
    finally:
        r.close()


def test_his_words_come_with_his_line(store, clock, tmp_path):
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        seen = []
        r.engine._queue = lambda items: seen.extend(items)
        row = said(r, "anyone got a spare flask for tonight please")
        r.sync(r.engine.on_human_chat(row))
        assert seen and all(m.his_words == 8 for _, m in seen)
    finally:
        r.close()


def test_learned_length_arm_yields_to_shape(store, clock, tmp_path, monkeypatch):
    def choose(conn, guid, context, **kwargs):
        arms.ensure(conn)
        return arms.controls() | {"length": "length.0.v1", "questions": "questions.2.v1",
                                  "followup": "followup.2.v1"}
    monkeypatch.setattr(bandit, "choose", choose)
    r = rig_of(store, clock, tmp_path)
    try:
        r.engine.learn_enabled = True
        r.engine.learn_settings = LearnCfg(enabled=True, bandit=True, apply=True)
        q = said(r, "who has the key to the tram depot tonight?")
        for shape in (False, True):
            cfg = r.engine.cfg
            r.engine.cfg = replace(cfg, talk=replace(cfg.talk, shape=shape))
            turn, meta = guild_answer(r, q, guid=102, owed=True)
            user = composed(r, turn, meta).messages[-1]["content"]
            assert (turn.shape is not None) is shape
            assert ("This reply usually fits in" in user) is not shape
            ids = meta.recipe_snapshot.arm_ids
            for family, arm in (("length", "length.0.v1"), ("questions", "questions.2.v1"),
                                ("followup", "followup.2.v1")):
                assert (arm in ids) is not shape
                assert (arms.controls()[family] in ids) is shape
            assert ("new question" in user or "relevant question" in user) is not shape
            assert ("complete thought" in user or "follow-up thought" in user) is not shape
            assert meta.learning.num_predict is None if shape else meta.learning.num_predict
            apply.release(r.engine, meta)
    finally:
        r.close()


# -- Review Focus 5: the worst-case DM -------------------------------------------------------------

def _para(i: int, chars: int) -> str:
    words = ("the ferry was late again so we waited by the old pier and talked about "
             f"nothing much at all for round {i} ").split()
    out = []
    while len(" ".join(out)) < chars:
        out += words
    return " ".join(out)[:chars].rstrip()


def test_line_prompt_and_num_predict_fit_num_ctx(store, clock, tmp_path, monkeypatch):
    """His DM answered with awareness, availability 6, state, intent, situation, the beat's four
    lines and a paragraph shape at dm_words 200: the line's prompt times ESTIMATE_SLACK plus its
    num_predict fits num_ctx; the beat call keeps its own think_budget."""
    force(monkeypatch, ShapeDraw(Shape.PARAGRAPH, 200, 1, num_predict(200)))
    r = rig_of(store, clock, tmp_path, intent=True, situation=True, life_state=True,
               availability=6, beat=True, shape=True)
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
        import json

        def reply(model, messages):
            if beats.MARK in messages[-1]["content"]:
                return json.dumps({"read": read, "want": "share", "use": 1})
            return "aw that was nice"
        r.llm.set_script(reply)
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN,
                    instruction="Zan wrote to you: " + post)
        meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None),
                        discord_chat=tuple(rows))
        assert speak(r, turn, meta)
        line = line_calls(r)[0]
        total = sum(estimate_tokens(m["content"]) for m in line["messages"])
        assert total <= DM_BUDGET
        assert total * ESTIMATE_SLACK + TEMPLATE_TOKENS + line["options"]["num_predict"] < NUM_CTX
        assert line["options"]["num_predict"] == num_predict(200) == 344
        (beat,) = beat_calls(r)
        assert beat["options"]["num_predict"] == r.engine.think_budget
        user = last_user(line)
        assert "at most about 200 words" in user and user.split("\n")[-1].startswith("Bring in: ")
    finally:
        r.close()


# -- the words of each shape ---------------------------------------------------------------------

def test_a_game_burst_goes_out_on_two_lines(store, clock, tmp_path, monkeypatch):
    force(monkeypatch, ShapeDraw(Shape.BURST, 16, 2, None))
    r = rig_of(store, clock, tmp_path, shape=True, situation=True)
    try:
        r.script("no way, that was the third wipe tonight on the same pull honestly")
        q = said(r, "who pulled the extra pack")
        turn, meta = guild_answer(r, q, guid=102, owed=True)
        lines = speak(r, turn, meta)
        assert len(lines) == 2 and meta.two_lines
        user = last_user(line_calls(r)[-1])
        assert "Say it as two quick lines, the way you'd double-text." in user
        assert "usual one line" not in user  # the situation prompt states no usual line
    finally:
        r.close()


def test_shape_with_situation_off_says_more_than_her_usual_line(store, clock, tmp_path,
                                                                monkeypatch):
    """Review of #333, requirement 7: comms_rules states her one line, so a shape of more lines
    says it is more than that; a one-line shape needs no such words."""
    force(monkeypatch, ShapeDraw(Shape.BURST, 16, 2, None))
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        q = said(r, "who pulled the extra pack")
        turn, meta = guild_answer(r, q, guid=102, owed=True)
        c = composed(r, turn, meta)
        system, user = c.messages[0]["content"], c.messages[-1]["content"]
        assert length_rule(c.persona.typing) in system
        assert ("Say it as two quick lines, more than your usual one line, the way you'd "
                "double-text.") in user
        force(monkeypatch, ShapeDraw(Shape.SHORT, 6, 1, None))
        turn, meta = guild_answer(r, q, guid=102, owed=True)
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "One short line, about 6 words." in user and "usual one line" not in user
    finally:
        r.close()


@pytest.mark.parametrize("situation", [False, True])
def test_discord_shapes_say_their_medium(store, clock, tmp_path, monkeypatch, situation):
    """Review of #331, P2-2: every Discord shape says it is Discord before its length."""
    r = rig_of(store, clock, tmp_path, shape=True, situation=situation)
    try:
        force(monkeypatch, ShapeDraw(Shape.SHORT, 20, 1, num_predict(20)))
        turn, meta, _ = channel_turn(r, "what are we running tomorrow night")
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "This is Discord chat, not the game. One short line, about 20 words." in user
        force(monkeypatch, ShapeDraw(Shape.QUESTION, 40, 1, num_predict(40)))
        turn, meta = dm_turn(r, "what are we running tomorrow night")
        user = composed(r, turn, meta).messages[-1]["content"]
        assert ("This is your private Discord DM, not the game. Keep it to about 40 words and "
                "ask him something back.") in user
        assert ("usual one line" in user) is False
    finally:
        r.close()


# -- late answers --------------------------------------------------------------------------------

def _late(r, block, where, *, minutes=5, guid=102):
    """His whisper, then `minutes` before her answer starts, as Task 7 plans it: heard_at his
    line's time, life_presence her game presence then."""
    w = said(r, "you still around", channel="whisper", to_guid=guid)
    r.run(60 * minutes, step=60)
    lp = LifePresence(block, where, Attention.GLANCING, DayEvent.NONE, False)
    turn = Turn(TurnKind.REPLY_HUMAN, guid, "whisper", to_guid=HUMAN, addressee=HUMAN,
                trigger_line=w.id)
    meta = TurnMeta(r.engine._new_id(), answers_human=True, whisper_reply=True,
                    heard_at=w.ts, life_presence=lp)
    return turn, meta


def test_a_late_answer_says_where_she_was_as_right_now_does(store, clock, tmp_path):
    """review-340: still at work, she says so in the present, under "You're at work"; asleep
    then, she was. Under two minutes late nothing is said."""
    r = rig_of(store, clock, tmp_path, shape=True, situation=True, availability=6)
    try:
        turn, meta = _late(r, Block.WORK, Where.AT_WORK)
        user = composed(r, turn, meta).messages[-1]["content"]
        assert turn.shape.shape is Shape.LATE
        assert "You're at work, the game open on your PC." in user
        assert ("You're only now seeing this (you're at work): say so in a few words, then "
                "answer.") in user
        turn, meta = _late(r, Block.ASLEEP, Where.ASLEEP)
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "You're only now seeing this (you were asleep)" in user
        turn, meta = _late(r, Block.WORK, Where.AT_WORK, minutes=1)
        assert drawn(r, turn, meta).shape is not Shape.LATE
    finally:
        r.close()


def test_a_late_answer_to_several_lines_says_these(store, clock, tmp_path):
    """review-336 note 2: his newer whisper joined the waiting answer (its trigger line moved):
    she is only now seeing these."""
    r = rig_of(store, clock, tmp_path, shape=True, situation=True, availability=6)
    try:
        turn, meta = _late(r, Block.GYM, Where.BUSY)
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "You're at the gym." in user
        assert "You're only now seeing this (you're at the gym)" in user
        turn, meta = _late(r, Block.GYM, Where.BUSY)
        newer = said(r, "no rush, whenever", channel="whisper", to_guid=102)
        turn.trigger_line = newer.id  # as talk.engine._life_waiting moves it
        user = composed(r, turn, meta).messages[-1]["content"]
        assert "You're only now seeing these (you're at the gym)" in user
    finally:
        r.close()


def test_a_late_answer_asleep_at_night(store, clock, tmp_path):
    """The rig's cast is asleep at 22:00: his whisper is answered late (Task 7), and when that
    is two minutes or more after his line, the answer says she only now sees it."""
    async def main():
        r = rig_of(store, clock, tmp_path, shape=True, situation=True, availability=6)
        r.script("sorry, dozed off on the sofa")
        await r.session()
        await r.advance(7200, step=60)
        seen = []
        for i in range(4):
            t0 = clock.now()
            await r.human(f"you awake {i}", channel="whisper", to_guid=101)
            await r.advance(500, step=2)
            says = [t for t, s in r.sent.timed_says() if t >= t0 and s.guid == 101]
            if says:
                seen.append((says[0] - t0, last_user(line_calls(r)[-1])))
        assert any("You're only now seeing this (you were asleep)" in u for _, u in seen)
        for after, user in seen:  # composed two minutes or more after his line, said later
            assert after >= 120 or "only now seeing" not in user
        r.close()
    asyncio.run(main())


# -- her state with a shape --------------------------------------------------------------------

def test_a_shape_says_how_much_she_says_not_her_chatty_mood():
    """D22: with a shape drawn, the state line leaves out how chatty she feels."""
    st = State(valence=0.0, energy=0.0, stress=0.0, social=0.6, toward_him=0.0)
    he = ("he", "him", "his")
    assert state_words(st, to_him=False, forms=he) == "You're chatty today."
    assert state_words(st, to_him=False, forms=he, talkative=False) == ""
    low = replace(st, social=-0.6, energy=-0.6)
    assert state_words(low, to_him=False, forms=he, talkative=False) == "You're tired today."


# -- review of #343 ---------------------------------------------------------------------------------

def test_a_reaction_follows_the_server_reactors_ties_and_palette(store, clock, tmp_path,
                                                                  monkeypatch):
    """P2-2: a complaint gets its own palette's emoji, never a forced thumbs-up; a feud never
    reacts and a rival only rolls its eyes at drama 7 or more: else the message is left on
    read, with no reaction and no model call."""
    from leeroy_mind.server import state as server_state
    from leeroy_mind.tuning import effective
    force(monkeypatch, ShapeDraw(Shape.REACTION, 0, 0, None))
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        turn, meta, mid = channel_turn(r, "my car broke down again, so annoyed")
        speak(r, turn, meta)
        (got,) = dstore.reactions(r.store.conn, [mid])[mid]
        assert got["emoji"] in PALETTE["complaint"]
        real = effective.dials_of
        for guid, tie, drama, outcome in ((104, "feud", 5, "left_on_read"),
                                          (105, "rival", 5, "left_on_read"),
                                          (101, "rival", 8, "reacted")):
            r.run(120, step=60)
            monkeypatch.setattr(server_state, "tie_kind", lambda conn, a, b, t=tie: t)

            def dials(p, d=drama):
                out = real(p)
                return type("D", (), {"level": lambda self, name: d if name == "drama"
                                      else out.level(name)})()
            monkeypatch.setattr(effective, "dials_of", dials)
            turn, meta, mid = channel_turn(r, "finally downed the last boss lol", guid=guid)
            assert speak(r, turn, meta) == []
            assert r.utterances()[-1]["dropped"] == outcome
            given = dstore.reactions(r.store.conn, [mid]).get(mid, [])
            assert [x["emoji"] for x in given] == (["\U0001F644"] if outcome == "reacted" else [])
        assert r.chat_calls() == []
    finally:
        r.close()


def test_a_reaction_that_cannot_be_posted_is_not_recorded_as_one(store, clock, tmp_path,
                                                                   monkeypatch):
    """P3-3: `reacted` only once the reaction is posted."""
    force(monkeypatch, ShapeDraw(Shape.REACTION, 0, 0, None))
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        async def refused(turn, meta, reaction, allowed=None):
            return False
        monkeypatch.setattr(r.engine, "_guard_react", refused)
        turn, meta, mid = channel_turn(r, "finally downed the last boss lol")
        assert speak(r, turn, meta) == []
        row = r.store.conn.execute("SELECT dropped, guard_verdict FROM utterance ORDER BY id "
                                   "DESC").fetchone()
        assert tuple(row) == ("react_skipped", "not_run")
    finally:
        r.close()


def test_a_failed_shape_line_keeps_the_turn_on_todays_length(store, clock, tmp_path, monkeypatch,
                                                             caplog):
    """P3-2: the shape's words are built inside the draw's containment: a failure is logged by
    its class and the answer goes on with today's length."""
    caplog.set_level(logging.WARNING)
    force(monkeypatch, ShapeDraw(Shape.SHORT, 6, 1, None))
    r = rig_of(store, clock, tmp_path, shape=True, situation=True)
    try:
        def broken(*a, **kw):
            raise KeyError("they")
        monkeypatch.setattr(pipeline_mod, "shape_line", broken)
        r.script("yeah the tram key is with me")
        q = said(r, "who has the key to the tram depot tonight?")
        turn, meta = guild_answer(r, q, guid=102, owed=True)
        assert speak(r, turn, meta)
        user = last_user(line_calls(r)[-1])
        assert length_rule(r.engine.personas.get(102).typing) in user
        assert turn.shape is None
        assert any(m == "talk_shape_failed: guid=102 reason=KeyError" for m in caplog.messages)
    finally:
        r.close()


def test_a_turn_spoken_again_starts_from_what_it_was(store, clock, tmp_path, monkeypatch):
    """P3-5: what a draw changed (her lines, a Discord burst) is put back before the next draw."""
    r = rig_of(store, clock, tmp_path, shape=True)
    try:
        cfg = r.engine.cfg
        r.engine.cfg = replace(cfg, talk=replace(cfg.talk, realism=True))
        force(monkeypatch, ShapeDraw(Shape.BURST, 40, 3, num_predict(40)))
        turn, meta, _ = channel_turn(r, "what are we running tomorrow night", owed=True)
        assert drawn(r, turn, meta).shape is Shape.BURST and meta.burst
        force(monkeypatch, ShapeDraw(Shape.SHORT, 20, 1, num_predict(20)))
        assert drawn(r, turn, meta).shape is Shape.SHORT and not meta.burst
        q = said(r, "who pulled the extra pack")
        turn, meta = guild_answer(r, q, guid=102, owed=True)
        turn.max_lines = 1
        force(monkeypatch, ShapeDraw(Shape.BURST, 16, 2, None))
        drawn(r, turn, meta)
        assert turn.max_lines == 2
        force(monkeypatch, ShapeDraw(Shape.SHORT, 6, 1, None))
        drawn(r, turn, meta)
        assert turn.max_lines == 1
    finally:
        r.close()


def test_a_burst_never_sits_beside_a_bit_short():   # P3-7
    st = State(valence=0.0, energy=0.0, stress=0.5, social=-0.3, toward_him=0.0)
    he = ("he", "him", "his")
    assert "a bit short" in state_words(st, to_him=False, forms=he)
    assert "a bit short" not in state_words(st, to_him=False, forms=he, curt=False)
