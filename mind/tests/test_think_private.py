"""The beat before an answer to his whisper or DM ([talk] think_private, talk/beat.py; owner,
2026-11-16: "go with structured thinking for dms and whispers"). One short call on the turn's own
prompt under Ollama's `format`, with think false, then the line as today with "You read it as",
"It lands", "You want to" (and "Bring in") after its instruction. A beat that fails the schema,
times out or comes back empty is skipped and the line goes as today. Every other turn is
untouched. The beat's words never reach a log or the store. The test cast and invented lines
only."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import HUMAN, seed_test_cast
from dbtemplate import open_migrated
from engine_rig import OrionLLM, engine_clock, make_rig, save_member
from leeroy_mind.config import THINK_BUDGET_RANGE, load_config
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.learn.recipe import BEAT_SUFFIX
from leeroy_mind.llm.client import GpuPaused, LLMError
from leeroy_mind.llm.presets import BEAT, LIVE
from leeroy_mind.storage.store import Store
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk import pipeline as pipeline_mod
from leeroy_mind.talk.discord_length import num_predict
from leeroy_mind.talk.pipeline import OWED_FALLBACK_HINT, Composed, TurnMeta
from leeroy_mind.talk.prompt import DM_BUDGET, ESTIMATE_SLACK, LIVE_BUDGET
from leeroy_mind.talk.romance import flirt_chance
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens

NUM_CTX = LIVE.options["num_ctx"]
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages
VENT = {"read": "he is wiped out after a long shift", "lands": "soft, a bit protective",
        "want": "vent", "use": None}
LINES_VENT = ("You read it as: he is wiped out after a long shift\nIt lands: soft, a bit protective\n"
              "You want to: get something off your chest")


@pytest.fixture
def clock():
    return engine_clock()


def _rig(store, clock, path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    return make_rig(store, clock, path, beat=True)


@pytest.fixture
def rig(store, clock, tmp_path):
    r = _rig(store, clock, tmp_path)
    yield r
    r.close()


def model(beat, *lines):
    """A script: `beat` (a dict, dumped, or text as it is) for the beat's request, `lines` in
    order for the line's (the last one repeats)."""
    left = list(lines) or ["fair enough"]

    def reply(model_name, messages):
        if beats.MARK in messages[-1]["content"]:
            return json.dumps(beat) if isinstance(beat, dict) else beat
        return left.pop(0) if len(left) > 1 else left[0]
    return reply


def beat_calls(rig):
    return [c for c in rig.chat_calls() if c["format"] is not None]


def line_calls(rig):
    return [c for c in rig.chat_calls() if c["format"] is None]


def last_user(call):
    return call["messages"][-1]["content"]


def dm_answer(rig, text="talk to me"):
    """His DM to 102 and her answer through the whole pipeline: the stored answer, or None."""
    mid = dstore.post(rig.store, channel="dm:102", author=HUMAN, origin="human", text=text,
                      now=rig.clock.now())
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=HUMAN,
                addressee=HUMAN)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None))

    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(200)
        return task.result()
    rig.sync(go())
    row = rig.store.conn.execute("SELECT text FROM discord_message WHERE channel='dm:102' "
                                 "AND origin='mind' ORDER BY id DESC").fetchone()
    return row[0] if row else None


def whisper_turn(guid=102):
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel="whisper", to_guid=HUMAN,
                addressee=HUMAN, trigger_line=1)
    turn.deadline = 1e12
    return turn, TurnMeta("t", answers_human=True, whisper_reply=True)


def composed(rig, guid=102, items=()):
    return Composed([{"role": "system", "content": "SYSTEM"},
                     {"role": "user", "content": "THE INSTRUCTION"}], [],
                    rig.engine.personas.get(guid), False, beat_items=list(items))


# -- the beat module ----------------------------------------------------------------------------

def test_lines_read_exactly_as_the_design_says():
    found = beats.Beat("he wants to know if you're coming tonight", "a little thrill",
                       "logistics", "[2 days ago] you promised him a run")
    assert beats.lines(found, "Zan") == (
        "You read it as: he wants to know if you're coming tonight\n"
        "It lands: a little thrill\n"
        "You want to: sort out the plan\n"
        "Bring in: [2 days ago] you promised him a run")
    assert beats.lines(replace(found, want="ask", use=None), "Zan").endswith(
        "You want to: get something from Zan")


def test_parse_takes_only_the_schema():
    wants, items = beats.wants_for(True), ["a memory", "a line"]
    ok, why = beats.parse(json.dumps({**VENT, "use": 2}), wants, items)
    assert why == "" and ok == beats.Beat(VENT["read"], VENT["lands"], "vent", "a line")
    assert beats.parse("", wants, items) == (None, "empty")
    assert beats.parse("  ", wants, items) == (None, "empty")
    assert beats.parse("not json", wants, items) == (None, "schema")
    assert beats.parse("[1, 2]", wants, items) == (None, "schema")
    assert beats.parse(json.dumps({"read": "x", "lands": "y", "want": "vent"}), wants, items) \
        == (None, "schema")  # no use
    for bad in ({"want": "dance"}, {"use": 3}, {"use": 0}, {"use": True}, {"use": "1"},
                {"read": 5}):
        assert beats.parse(json.dumps({**VENT, **bad}), wants, items) == (None, "schema"), bad
    assert beats.parse(json.dumps({**VENT, "read": "  "}), wants, items) == (None, "empty")
    assert beats.parse(json.dumps({**VENT, "want": "flirt"}), beats.wants_for(False), items) \
        == (None, "schema")


def test_parse_bounds_the_words():
    wants = beats.wants_for(True)
    long = {**VENT, "read": "word " * 100, "lands": '"' + "fine " * 40 + '"'}
    found, _ = beats.parse(json.dumps(long), wants, [])
    assert len(found.read) <= beats.READ_CHARS and len(found.lands) <= beats.LANDS_CHARS
    assert "\n" not in beats.parse(json.dumps({**VENT, "read": "a\n\nb"}), wants, [])[0].read


def test_flirt_is_offered_only_to_a_member_who_may_flirt(rig):
    assert "flirt" in beats.wants_for(True) and "flirt" not in beats.wants_for(False)
    assert flirt_chance(rig.engine.personas.get(101), 1.0) == 0
    rig.llm.set_script(model(VENT, "yeah, rough one"))
    rig.whisper_from_him(101, "long shift")
    rig.run()
    [call] = beat_calls(rig)
    assert "flirt" not in call["format"]["properties"]["want"]["enum"]
    assert "- flirt:" not in last_user(call) and "flirt:" not in call["format"]["properties"]["want"]["description"]


def test_items_are_memories_then_others_lines_newest_first():
    assert beats.items_of(["m1", "m2"], ["old", "new"]) == ["m1", "m2", "new", "old"]
    assert len(beats.items_of(["m"] * 9, [])) == beats.MAX_ITEMS
    assert len(beats.items_of(["x " * 200], [])[0]) <= beats.ITEM_CHARS


def test_each_want_is_described_and_ask_is_wanting_something_from_him():
    """Bake-off 2026-11-16: with only the names, Orion chose "ask" on 34 of 47 turns."""
    wants = beats.wants_for(True)
    asked = beats.instruction("Zan", wants, [])
    described = beats.schema(wants, "Zan")["properties"]["want"]["description"]
    for want in wants:
        assert f"\n- {want}: " in asked and f"{want}: " in described
    for text in (asked, described):
        assert "ask: you want something from Zan" in text and "not just that you ask a question" in text
    assert "one short sentence" in asked and "a few words, not a sentence" in asked
    props = beats.schema(wants, "Zan")["properties"]
    assert "one short sentence" in props["read"]["description"]
    assert "a few words" in props["lands"]["description"]


def test_the_token_arithmetic_fits_num_ctx():
    """Worst case, with the prompt's estimate 25% low and 64 tokens of chat template: his DM at
    DM_BUDGET with every item offered and the largest think_budget (the oldest items give way:
    beat.fitting), and the DM's line with the longest beat lines and a full dm_words answer."""
    wants = beats.wants_for(True)
    items = ["x" * beats.ITEM_CHARS] * beats.MAX_ITEMS
    longest = beats.Beat("r" * beats.READ_CHARS, "l" * beats.LANDS_CHARS, "reassure_seek",
                         "u" * beats.ITEM_CHARS)
    told = estimate_tokens(beats.lines(longest, "Zan"))
    assert told <= 120
    for budget, answer in ((DM_BUDGET, num_predict(200)), (LIVE_BUDGET, 80)):
        for predict in (240, THINK_BUDGET_RANGE[1]):
            kept = beats.fitting(budget, "Zan", wants, items, predict=predict, num_ctx=NUM_CTX)
            assert kept is not None
            asked = budget + 1 + estimate_tokens(beats.instruction("Zan", wants, kept))
            assert asked * ESTIMATE_SLACK + TEMPLATE_TOKENS + predict <= NUM_CTX
        assert (budget + told) * ESTIMATE_SLACK + TEMPLATE_TOKENS + answer <= NUM_CTX
    assert len(beats.fitting(LIVE_BUDGET, "Zan", wants, items, predict=240, num_ctx=NUM_CTX)) == 6
    assert len(beats.fitting(DM_BUDGET, "Zan", wants, items, predict=240, num_ctx=NUM_CTX)) == 4
    assert beats.fitting(3500, "Zan", wants, items, predict=240, num_ctx=NUM_CTX) is None


def test_a_beat_with_no_room_is_skipped(rig, caplog):
    rig.llm.set_script(model(VENT))
    turn, meta = whisper_turn()
    c = composed(rig)
    c = replace(c, messages=[*c.messages[:-1], {"role": "user", "content": "x" * 4 * 3500}])
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert rig.sync(rig.engine._beat(turn, meta, c)) == ""
    assert "think_beat_skipped: guid=102 reason=room" in caplog.text and not beat_calls(rig)


# -- which turns ---------------------------------------------------------------------------------

@pytest.mark.parametrize("channel,to_guid,answers,expect", [
    ("whisper", HUMAN, True, True),        # his whisper
    ("dm:102", HUMAN, True, True),         # his DM
    ("whisper", 103, True, False),         # a whisper to someone else
    ("guild", 0, True, False), ("party", 0, True, False), ("officer", 0, True, False),
    ("discord:general", 0, True, False),   # a Discord channel, even answering him
    ("gdm:7", 0, True, False),             # a group DM
    ("dm:102", HUMAN, False, False),       # her DM to him that answers nothing of his
])
def test_only_answers_to_his_whisper_or_dm_get_a_beat(rig, channel, to_guid, answers, expect):
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, to_guid=to_guid,
                addressee=HUMAN, trigger_line=1)
    assert rig.engine._beat_on(turn, TurnMeta("t", answers_human=answers)) is expect


def test_a_whisper_opener_or_a_template_gets_none(rig):
    opener = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                  addressee=HUMAN)
    assert not rig.engine._beat_on(opener, TurnMeta("t", initiates=True))
    turn, meta = whisper_turn()
    turn.template = "a fixed line"
    assert not rig.engine._beat_on(turn, meta)


def test_his_whisper_gets_a_beat_then_the_line(rig):
    rig.llm.set_script(model(VENT, "ugh, that sounds rough, tell me everything"))
    rig.whisper_from_him(102, "long shift, dead on my feet")
    rig.run()
    first, second = rig.chat_calls()
    assert first["format"] == beats.schema(beats.wants_for(True), "Zan")
    assert first["think"] is False  # never left out for Orion: it would think
    assert first["options"] == {"temperature": 0.9, "min_p": 0.05, "top_p": 1.0,
                                "num_predict": 240, "num_ctx": NUM_CTX}
    assert last_user(first).endswith(beats.USE_NONE)
    assert second["format"] is None and second["think"] is False
    assert second["options"] == json.loads(json.dumps(dict(LIVE.options)))
    assert last_user(second).endswith("\n" + LINES_VENT)
    # the beat's prompt is the line's own, its instruction after
    assert last_user(first).startswith(last_user(second)[:-len(LINES_VENT) - 1])
    assert first["messages"][:-1] == second["messages"][:-1]
    assert [s.text for s in rig.sent.says()] == ["ugh, that sounds rough, tell me everything"]


def test_his_dm_gets_a_beat_then_the_line(rig):
    rig.llm.set_script(model(VENT, "oh no, what happened? tell me"))
    assert dm_answer(rig) == "oh no, what happened? tell me"
    first, second = rig.chat_calls()
    assert first["format"] is not None and first["options"]["num_predict"] == 240
    assert second["options"]["num_predict"] == num_predict(200)
    assert last_user(second).endswith("\n" + LINES_VENT)


def test_a_guild_answer_to_him_gets_no_beat(rig):
    rig.llm.set_script(model(VENT, "gear first, then gems"))
    rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN))
    [call] = rig.chat_calls()
    assert call["format"] is None and "You read it as" not in last_user(call)


def test_bring_in_names_what_she_was_offered(rig):
    rig.llm.set_script(model({**VENT, "use": 1}, "mm hi stranger", "ugh, what did he do"))
    rig.whisper_from_him(102, "hey you")
    rig.run()
    rig.whisper_from_him(102, "my boss again")
    rig.run()
    first_beat, second_beat = beat_calls(rig)
    assert last_user(first_beat).endswith(beats.USE_NONE)  # nothing before his line
    offered = last_user(second_beat).rsplit("\n", 1)[-1]
    assert offered.startswith("1. ") and "Zan" in offered and "hey you" in offered
    listed = last_user(second_beat).split("worth bringing in, or null:", 1)[1]
    assert "mm hi stranger" not in listed and "my boss again" not in listed  # hers, his newest
    assert last_user(line_calls(rig)[-1]).endswith("\nBring in: " + offered[3:])


# -- skipped: the line goes as today ------------------------------------------------------------

@pytest.mark.parametrize("reply,reason", [
    ("", "empty"), ("not json", "schema"), (json.dumps({**VENT, "want": "dance"}), "schema"),
    (json.dumps({**VENT, "use": 3}), "schema"), (json.dumps({**VENT, "lands": ""}), "empty"),
])
def test_a_bad_beat_is_skipped(rig, caplog, reply, reason):
    rig.llm.set_script(model(reply, "hey, yeah"))
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        rig.whisper_from_him(102, "you up?")
        rig.run()
    line = line_calls(rig)
    assert len(beat_calls(rig)) == 1 and len(line) == 1
    assert "You read it as" not in last_user(line[0])
    assert f"think_beat_skipped: guid=102 reason={reason}" in caplog.text
    assert [s.text for s in rig.sent.says()] == ["hey, yeah"]


class Failing(OrionLLM):
    """The beat's request fails with `error`; the line's goes as usual."""

    def __init__(self, script, error):
        super().__init__(script)
        self.error = error

    async def chat(self, model_name, messages, *, options, think=None, format=None, **kw):
        if format is not None:
            self.calls.append({"model": model_name, "messages": messages, "format": format})
            raise self.error
        return await super().chat(model_name, messages, options=options, think=think,
                                  format=format, **kw)


@pytest.mark.parametrize("error,reason", [
    (LLMError("/api/chat: timed out"), "timeout"), (LLMError("/api/chat: HTTP 500"), "llm_error"),
    (RuntimeError("anything else"), "error"),
])
def test_a_failed_beat_request_is_skipped(rig, caplog, error, reason):
    rig.engine.scheduler._llm = rig.llm = Failing(model(VENT, "hey, yeah"), error)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        rig.whisper_from_him(102, "you up?")
        rig.run()
    assert f"think_beat_skipped: guid=102 reason={reason}" in caplog.text
    assert [s.text for s in rig.sent.says()] == ["hey, yeah"]
    assert "You read it as" not in last_user(line_calls(rig)[0])


def test_a_paused_gpu_skips_the_beat(rig, caplog):
    rig.engine.scheduler._llm = rig.llm = Failing(model(VENT), GpuPaused("paused"))
    turn, meta = whisper_turn()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert rig.sync(rig.engine._beat(turn, meta, composed(rig))) == ""
    assert "think_beat_skipped: guid=102 reason=gpu_paused" in caplog.text


def test_a_slow_beat_is_given_up_after_its_timeout(rig, caplog, monkeypatch):
    class Slow(OrionLLM):
        async def chat(self, model_name, messages, *, options, think=None, format=None, **kw):
            await asyncio.sleep(5)
            raise AssertionError("never reached")
    monkeypatch.setattr(pipeline_mod, "BEAT", replace(BEAT, timeout=0.05))
    rig.engine.scheduler._llm = rig.llm = Slow(model(VENT))
    turn, meta = whisper_turn()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert rig.sync(rig.engine._beat(turn, meta, composed(rig))) == ""
    assert "think_beat_skipped: guid=102 reason=timeout" in caplog.text


def test_a_skipped_beat_leaves_the_prompt_as_today(rig):
    turn, meta = whisper_turn()
    c = composed(rig)
    rig.llm.set_script(model("not json"))
    assert rig.sync(rig.engine._with_beat(turn, meta, c)) is c
    assert meta.beat == ""
    rig.llm.set_script(model(VENT))
    meta = TurnMeta("t", answers_human=True, whisper_reply=True)
    out = rig.sync(rig.engine._with_beat(turn, meta, c))
    assert out.messages[:-1] == c.messages[:-1]
    assert last_user({"messages": out.messages}) == "THE INSTRUCTION\n" + LINES_VENT


def test_the_beat_is_asked_once_a_turn(rig):
    rig.llm.set_script(model(VENT))
    turn, meta = whisper_turn()
    c = composed(rig)
    first = rig.sync(rig.engine._with_beat(turn, meta, c))
    again = rig.sync(rig.engine._with_beat(turn, meta, c))  # a guard's rewrite composes again
    assert first.messages == again.messages and len(beat_calls(rig)) == 1


def test_the_switch_off_is_today(store, clock, tmp_path):
    """The same whisper answer in two fresh installs: with the switch off, its one request is
    exactly the request a skipped beat leaves."""
    sent = []
    for n, (on, beat) in enumerate(((False, VENT), (True, "not json"))):
        if n:
            (tmp_path / "b").mkdir()
        s = store if n == 0 else Store(open_migrated(tmp_path / "b" / "mind.db"), clock=clock)
        r = _rig(s, clock, tmp_path / str(n))
        r.engine.think_private = on
        r.llm.set_script(model(beat, "hey, yeah"))
        r.whisper_from_him(102, "you up?")
        r.run()
        sent.append(line_calls(r))
        assert len(beat_calls(r)) == int(on)
        r.close()
        if n:
            s.close()
    assert sent[0] == sent[1] and len(sent[0]) == 1


# -- the owed fallback, the recipe and privacy ----------------------------------------------------

def test_the_owed_fallback_carries_the_beat(rig):
    rig.llm.set_script(model(VENT, "happy to help with that", "come over already",
                             "oh. wow. i don't know what to say"))
    rig.whisper_from_him(102, "leave him for me")
    rig.run()
    lines = line_calls(rig)
    assert len(beat_calls(rig)) == 1 and len(lines) == 3
    assert last_user(lines[-1]).endswith(OWED_FALLBACK_HINT.format(human="Zan"))
    for call in lines:
        assert LINES_VENT in last_user(call)
    assert [s.text for s in rig.sent.says()] == ["oh. wow. i don't know what to say"]


def test_a_line_after_a_beat_is_recorded_as_one(rig):
    rig.engine.learn_enabled = True
    rig.llm.set_script(model(VENT, "aw hey you", "gear first, then gems"))
    rig.whisper_from_him(102, "hey you")
    rig.run()
    rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN))
    models = dict(rig.store.conn.execute(
        "SELECT c.channel, p.model_id FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id"))
    assert models == {"whisper": rig.cfg.models.live + BEAT_SUFFIX,
                      "guild": rig.cfg.models.live}


def test_the_beat_never_reaches_a_log_a_line_or_the_store(rig, caplog):
    rig.engine.learn_enabled = True
    secret = {"read": "PRIVATE-BEAT-READ he misses her", "lands": "PRIVATE-BEAT-LANDS",
              "want": "flirt", "use": None}
    rig.llm.set_script(model(secret, "mm, missed you too"))
    with caplog.at_level(logging.DEBUG):
        rig.whisper_from_him(102, "missed you")
        rig.run()
        dm_answer(rig, "you there?")
    assert "think_beat: guid=102 want=flirt use=0 ms=" in caplog.text
    assert "PRIVATE-BEAT" not in caplog.text
    assert all("PRIVATE-BEAT" not in s.text for s in rig.sent.says())
    conn = rig.store.conn
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            assert not any(isinstance(v, str) and "PRIVATE-BEAT" in v for v in tuple(row)), table


# -- the switch ----------------------------------------------------------------------------------

def test_the_switch_and_budget_are_read_from_mind_toml(tmp_path):
    path = tmp_path / "mind.toml"
    talk = load_config(path).talk
    assert talk.think_private is True and talk.think_budget == 240
    path.write_text("[talk]\nthink_private = false\nthink_budget = 200\n", encoding="utf-8")
    talk = load_config(path).talk
    assert talk.think_private is False and talk.think_budget == 200
    path.write_text('[talk]\nthink_private = "yes"\nthink_budget = 5000\n', encoding="utf-8")
    talk = load_config(path).talk
    assert talk.think_private is False and talk.think_budget == 240  # invalid: off, the default


def test_the_budget_is_the_beats_num_predict(rig):
    rig.engine.think_budget = 200
    rig.llm.set_script(model(VENT))
    turn, meta = whisper_turn()
    rig.sync(rig.engine._beat(turn, meta, composed(rig)))
    assert beat_calls(rig)[0]["options"]["num_predict"] == 200


def test_the_switch_is_read_at_mind_start_not_at_a_login(rig):
    assert rig.engine.think_private is True and rig.engine.think_budget == 240
    off = replace(rig.cfg, talk=replace(rig.cfg.talk, think_private=False, think_budget=200))
    rig.engine.apply_config(off)
    assert rig.engine.think_private is True and rig.engine.think_budget == 240
