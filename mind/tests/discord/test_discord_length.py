"""Longer member messages on the guild's Discord ([talk] discord_words and dm_words, owner
2026-11-13: "Long and chatty", then channels a mix and "DMs max expressive"): every Discord turn,
a DM or a channel, is told to write the way people write on Discord, gets the model room for it
and keeps its words through every step after the model; a channel message draws its length band
once; every game turn is exactly as before. Invented cast and text, fake model and clock."""
import asyncio
import logging
import math
import random
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import HUMAN, seed_test_cast, unit
from engine_rig import engine_clock, make_rig
from leeroy_mind import config
from leeroy_mind.config import DEFAULT_TOML, load_config
from leeroy_mind.console import discordview
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.text import MAX_TEXT as DISCORD_MAX_TEXT
from leeroy_mind.eval.talk import nonsequitur
from leeroy_mind.eval.talk.common import words
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, HistoryLine, review
from leeroy_mind.guards.review import _server_filler
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.diagnostics import capture_response
from leeroy_mind.llm.presets import EXTRACT, LIVE
from leeroy_mind.memory.extract import build_extraction_messages
from leeroy_mind.memory.threads import worth_extracting
from leeroy_mind.storage.rows import ChatLineRow, pack_vec
from leeroy_mind.talk import discord_length
from leeroy_mind.talk.filters import find_tells, is_silence, repair
from leeroy_mind.talk.pipeline import LONG_SLACK, TurnMeta
from leeroy_mind.talk.pipeline import Pipeline
from leeroy_mind.talk.prompt import (DISCORD_DM, DISCORD_FEW, DISCORD_LENGTH, DISCORD_QUICK,
                                     INSTRUCTIONS, LIVE_BUDGET, MORE_LINES, discord_template,
                                     render_instruction)
from leeroy_mind.talk.realism import Realism
from leeroy_mind.talk.states import EFFECTS, Altered
from leeroy_mind.talk.style import Styled, style_lines
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens

# A member's DM to him and a member's post in a channel, 78 and 73 words, in the test cast's
# lower case: nothing in them is an assistant phrase, an echo of his post, an action or a claim
# of shared history.
LONG_DM = ("oh nice, i was hoping you would message me tonight. work was long but the new "
           "manager finally let us leave early, so i have the whole evening free. i was thinking "
           "we could run a few dungeons before the raid and maybe farm some cloth for the guild "
           "bank. i can bring flasks and food for everyone if that helps. how was your day? are "
           "you still working on your tanking gear or did you finish it?")
LONG_CHANNEL = ("count me in for thursday. i can tank the first wing if somebody else takes the "
                "drakes, my gear is finally good enough for it. we should bring at least three "
                "healers this time and i will make sure everyone has flasks before we pull. if "
                "anyone needs a hand with their enchants beforehand just whisper me and i will "
                "sort it out. the guild bank could also use some more frost lotus")
HIS_POST = "hey you around tonight"
GAME_CHANNELS = ("guild", "officer", "whisper", "party", "raid", "raid_warning", "say", "yell",
                 "voice:raid", "")
DISCORD_CHANNELS = ("dm:102", "discord:general")


@pytest.fixture
def clock():
    return engine_clock()


def test_the_fixtures_are_long():
    assert 70 <= len(LONG_DM.split()) <= 90 and 70 <= len(LONG_CHANNEL.split()) <= 90


# -- the setting -------------------------------------------------------------------------------

def _talk(tmp_path, body: str):
    path = tmp_path / "mind.toml"
    path.write_text("[talk]\n" + body + "\n", encoding="utf-8")
    return load_config(path).talk


def test_discord_words_defaults_to_90_and_takes_0_to_200(tmp_path):
    assert load_config(tmp_path / "absent.toml").talk.discord_words == 90
    assert "discord_words = 90" in DEFAULT_TOML
    for n in (0, 1, 90, 200):
        assert _talk(tmp_path, f"discord_words = {n}").discord_words == n


def test_dm_words_defaults_to_200_and_takes_0_to_200(tmp_path):
    assert load_config(tmp_path / "absent.toml").talk.dm_words == 200
    assert "dm_words = 200" in DEFAULT_TOML
    for n in (0, 1, 90, 200):
        assert _talk(tmp_path, f"dm_words = {n}").dm_words == n


@pytest.mark.parametrize("value", ["-1", "201", '"90"', "9.5", "true"])
def test_an_invalid_dm_words_is_the_default_logged_once(tmp_path, caplog, monkeypatch, value):
    monkeypatch.setattr(config, "_WARNED", set())
    caplog.set_level(logging.WARNING, logger="leeroy_mind")
    assert _talk(tmp_path, f"dm_words = {value}").dm_words == 200
    assert _talk(tmp_path, f"dm_words = {value}").dm_words == 200  # read again
    found = [r for r in caplog.records if "talk_dm_words_invalid" in r.getMessage()]
    assert len(found) == 1


@pytest.mark.parametrize("value", ["-1", "201", '"90"', "9.5", "true"])
def test_an_invalid_discord_words_is_the_default_logged_once(tmp_path, caplog, monkeypatch,
                                                            value):
    monkeypatch.setattr(config, "_WARNED", set())
    caplog.set_level(logging.WARNING, logger="leeroy_mind")
    assert _talk(tmp_path, f"discord_words = {value}").discord_words == 90
    assert _talk(tmp_path, f"discord_words = {value}").discord_words == 90  # read again
    found = [r for r in caplog.records if "talk_discord_words_invalid" in r.getMessage()]
    assert len(found) == 1


def test_words_for_a_turn():
    talk = load_config(Path("absent.toml")).talk
    assert discord_length.words_for(talk, "dm:102") == 200  # his DMs: as full as she likes
    assert discord_length.words_for(talk, "discord:general") == 90
    assert discord_length.words_for(talk, "gdm:7") == 90  # a group DM is a chat, not a pair
    for channel in GAME_CHANNELS:
        assert discord_length.words_for(talk, channel) == 0
    assert discord_length.words_for(replace(talk, dm_words=0), "dm:102") == 0
    assert discord_length.words_for(replace(talk, dm_words=0), "discord:general") == 90
    assert discord_length.words_for(replace(talk, discord_words=0), "dm:102") == 200
    assert discord_length.words_for(replace(talk, discord_words=0), "discord:general") == 0
    assert discord_length.words_for(object(), "dm:102") == 0  # a config without the keys
    from types import SimpleNamespace
    assert discord_length.words_for(SimpleNamespace(discord_words=90), "dm:102") == 90


def test_a_channel_message_draws_its_band():
    """Most a quick line, some a couple of sentences, now and then the paragraph; never over
    the setting."""
    assert [discord_length.band(90, d) for d in (0.0, 0.54, 0.55, 0.84, 0.85, 0.999)] == [
        20, 20, 45, 45, 90, 90]
    assert [discord_length.band(30, d) for d in (0.1, 0.7, 0.9)] == [20, 30, 30]
    assert [discord_length.band(10, d) for d in (0.1, 0.7, 0.9)] == [10, 10, 10]
    rng = random.Random(7)
    draws = [discord_length.band(90, rng.random()) for _ in range(10000)]
    shares = {n: draws.count(n) / len(draws) for n in (20, 45, 90)}
    assert abs(shares[20] - .55) < .03 and abs(shares[45] - .30) < .03
    assert abs(shares[90] - .15) < .03


# -- the instruction ---------------------------------------------------------------------------

def render(kind: TurnKind, **kw) -> str:
    return render_instruction(kind, addressee="Zan", topic="the drake", **kw)


VARIANTS = [{}, {"long": True}, {"to_him": True}, {"quiet": False}]


@pytest.mark.parametrize("channel", DISCORD_CHANNELS)
@pytest.mark.parametrize("kind", list(TurnKind))
@pytest.mark.parametrize("variant", VARIANTS)
def test_every_discord_turn_kind_has_the_new_wording_and_no_short_line(channel, kind, variant):
    text = render(kind, channel=channel, discord_words=90, **variant)
    wording = (DISCORD_DM.format(words=90) if channel.startswith("dm:")
               else DISCORD_LENGTH.format(where="Discord chat", words=90))
    assert wording in text
    assert "at most about 90 words" in text and "One paragraph, no line breaks." in text
    assert "short line" not in text.casefold()
    assert "a few words" not in text and MORE_LINES not in text


def test_the_wording_follows_the_setting_and_the_place():
    dm = render(TurnKind.REPLY_HUMAN, channel="dm:102", discord_words=150)
    channel = render(TurnKind.REPLY_HUMAN, channel="discord:raid-planning", discord_words=150)
    assert "at most about 150 words" in dm and "private Discord DM" in dm
    assert "write as fully as you feel" in dm and "single quick sentence" not in dm
    assert "Discord chat" in channel and "private Discord DM" not in channel


def test_a_channel_band_reads_its_own_wording_and_a_dm_never_changes():
    quick = render(TurnKind.CONTINUE, channel="discord:general", discord_words=20)
    few = render(TurnKind.CONTINUE, channel="discord:general", discord_words=45)
    paragraph = render(TurnKind.CONTINUE, channel="discord:general", discord_words=90)
    assert DISCORD_QUICK.format(words=20) in quick and "paragraph" not in quick
    assert DISCORD_FEW.format(words=45) in few and "a couple of sentences" in few
    assert DISCORD_LENGTH.format(where="Discord chat", words=90) in paragraph
    assert render(TurnKind.CONTINUE, channel="gdm:7", discord_words=20) == quick
    for words in (20, 45, 90, 200):
        assert discord_template("dm:102", words) == DISCORD_DM
        assert DISCORD_DM.format(words=words) in render(TurnKind.REPLY_HUMAN, channel="dm:102",
                                                        discord_words=words)
    assert discord_template("guild", 90) == "" and discord_template("discord:general", 0) == ""


def test_each_template_reads_whole_without_its_game_length():
    """The phrase goes with its comma or space, never leaving a broken sentence."""
    for kind in TurnKind:
        text = render(kind, channel="discord:general", discord_words=90)
        assert ",." not in text and " ." not in text and ",," not in text and " ," not in text
    assert render(TurnKind.REPLY_HUMAN, channel="dm:102", discord_words=90).startswith(
        "Answer Zan's last line. Respond to what they actually said, as yourself. Bring in")
    assert render(TurnKind.WHISPER, channel="dm:102", discord_words=90).startswith(
        "You're whispering Zan privately. Say it the way you'd really whisper them.")
    assert render(TurnKind.GREET, channel="dm:102", discord_words=90).startswith(
        "Zan just logged on. Greet them your way. This is your private Discord DM")


@pytest.mark.parametrize("channel", GAME_CHANNELS)
@pytest.mark.parametrize("kind", list(TurnKind))
@pytest.mark.parametrize("variant", VARIANTS)
def test_every_game_turn_kind_is_byte_identical(channel, kind, variant):
    for words in (0, 90, 200):
        assert render(kind, channel=channel, discord_words=words, **variant) == render(
            kind, **variant)


def test_game_instructions_are_pinned():
    """Today's text, letter for letter, so a template edit for Discord shows up here."""
    assert render(TurnKind.REPLY_HUMAN, channel="guild", discord_words=90) == (
        "Answer Zan's last line. Respond to what they actually said, as yourself, in one short "
        "line. Bring in a memory or a running joke only if it really fits.")
    assert render(TurnKind.WHISPER, channel="whisper", discord_words=90, long=True) == (
        "You're whispering Zan privately. A line or two, more than your usual one line, the way "
        "you'd really whisper them.")
    assert "One short line" in INSTRUCTIONS[TurnKind.WHISPER]


@pytest.mark.parametrize("channel", DISCORD_CHANNELS)
@pytest.mark.parametrize("kind", list(TurnKind))
@pytest.mark.parametrize("variant", VARIANTS)
def test_discord_words_zero_restores_the_one_short_line_rule(channel, kind, variant):
    assert render(kind, channel=channel, discord_words=0, **variant) == render(kind, **variant)


def test_the_wording_ends_the_template_before_what_she_answers_lately_and_her_tail():
    """Her romance lead first, the turn's own text with the wording where its one short line was
    (at the template's end), then the extra (his post, a seed), the Lately line and her tail, as
    before: what she answers stays last."""
    text = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", lead="LEAD", extra="EXTRA",
                              after="AFTER", tail="TAIL", channel="dm:102", discord_words=90)
    wording = DISCORD_DM.format(words=90)
    assert text.startswith("LEAD Answer Zan's last line.")
    assert text.index("if it really fits.") < text.index(wording) < text.index("EXTRA")
    assert text.index("EXTRA") < text.index("AFTER") < text.index("TAIL")
    assert text.endswith(f"{wording} EXTRA AFTER TAIL")


def test_a_long_extra_is_capped_but_the_wording_never_is():
    text = render(TurnKind.REPLY_HUMAN, extra="word " * 2000, channel="dm:102", discord_words=90)
    assert DISCORD_DM.format(words=90) in text


# -- room for the reply --------------------------------------------------------------------------

def test_num_predict_follows_the_words():
    assert discord_length.num_predict(90) == 168
    assert discord_length.num_predict(200) == 344
    assert discord_length.num_predict(1) == 80  # never under LIVE's own
    preset = discord_length.preset_for(LIVE, 90)
    assert preset.options["num_predict"] == 168
    assert {k: v for k, v in preset.options.items() if k != "num_predict"} == {
        k: v for k, v in LIVE.options.items() if k != "num_predict"}
    assert preset.options["num_ctx"] == 4096 and preset.options["stop"] == ("\n\n",)
    assert (preset.think, preset.timeout) == (LIVE.think, LIVE.timeout)
    assert LIVE.options["num_predict"] == 80  # a per-request override, never a new global


def test_the_longest_discord_reply_still_fits_the_live_context():
    # LIVE_BUDGET is unchanged; the game's own margin is 2216 (test_prompt_capacity)
    longest = discord_length.num_predict(200)
    assert LIVE.options["num_ctx"] - LIVE_BUDGET - longest == 1952
    assert LIVE.options["num_ctx"] - LIVE_BUDGET - discord_length.num_predict(90) == 2128


def discord_turn(speaker: int, channel: str, *, kind=TurnKind.REPLY_HUMAN, draw=0.99) -> Turn:
    """A channel turn draws the paragraph band unless `draw` says otherwise (a DM never draws)."""
    to = HUMAN if channel.startswith("dm:") else 0
    return Turn(kind=kind, speaker=speaker, channel=channel, to_guid=to, addressee=HUMAN,
                discord_draw=draw)


def with_words(rig, n: int):
    """Both settings at `n`: a channel's discord_words and a DM's dm_words."""
    rig.engine.cfg = replace(rig.engine.cfg, talk=replace(rig.engine.cfg.talk, discord_words=n,
                                                          dm_words=n))
    return rig


def test_a_discord_turn_asks_for_room_and_a_game_turn_does_not(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="sounds good, see you there")
    assert rig.speak_turn(discord_turn(102, "dm:102"), seconds=150)
    assert rig.speak_turn(discord_turn(101, "discord:general"), seconds=150)
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                               addressee=HUMAN), seconds=60)
    options = [c["options"] for c in rig.chat_calls()]
    assert [o["num_predict"] for o in options] == [344, 168, 80]  # his DM's 200 words, 90, game
    assert {o["num_ctx"] for o in options} == {4096}
    assert all(o["stop"] == ["\n\n"] for o in options)
    rig.close()


@pytest.mark.parametrize("draw,words,room,wording", [
    (0.1, 20, 80, DISCORD_QUICK), (0.6, 45, 96, DISCORD_FEW), (0.9, 90, 168, DISCORD_LENGTH)])
def test_a_channel_turn_reads_and_asks_by_the_band_it_drew(store, clock, tmp_path, draw, words,
                                                            room, wording):
    rig = make_rig(store, clock, tmp_path, reply="sounds good, see you there")
    turn = discord_turn(101, "discord:general", draw=draw)
    assert rig.engine._discord_words(turn) == words
    assert rig.speak_turn(turn, seconds=150)
    [call] = rig.chat_calls()
    assert call["options"]["num_predict"] == room
    assert wording.format(where="Discord chat", words=words) in call["messages"][-1]["content"]
    rig.close()


def test_a_channel_turn_draws_once_and_a_dm_never_draws(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    rig.engine._length_rng = random.Random(3)
    turn = Turn(kind=TurnKind.CONTINUE, speaker=101, channel="discord:general")
    first = rig.engine._discord_words(turn)
    assert turn.discord_draw is not None
    assert {rig.engine._discord_words(turn) for _ in range(20)} == {first}
    assert rig.engine._discord_words(replace(turn)) == first  # a copy keeps its draw
    dm = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=HUMAN)
    assert rig.engine._discord_words(dm) == 200 and dm.discord_draw is None
    game = Turn(kind=TurnKind.CONTINUE, speaker=101, channel="guild")
    assert rig.engine._discord_words(game) == 0 and game.discord_draw is None
    rig.close()


def test_a_silent_channel_answer_is_asked_again_for_its_band():
    assert [Pipeline._silent_length(n) for n in (10, 20, 25, 45, 50, 90, 200)] == [
        "a sentence", "a sentence", "a sentence", "a couple of sentences",
        "a couple of sentences", "a few sentences", "a few sentences"]


def test_discord_words_zero_asks_for_80(store, clock, tmp_path):
    rig = with_words(make_rig(store, clock, tmp_path, reply="sounds good"), 0)
    assert rig.speak_turn(discord_turn(102, "dm:102"), seconds=150)
    [call] = rig.chat_calls()
    assert call["options"]["num_predict"] == 80
    user = call["messages"][-1]["content"]
    assert "as yourself, in one short line." in user and "This is Discord" not in user
    rig.close()


@pytest.mark.parametrize("n,length", [(90, "a few sentences"), (0, "a line")])
def test_a_silent_answer_to_his_dm_is_asked_again_for_its_length(store, clock, tmp_path, n,
                                                                 length):
    rig = with_words(make_rig(store, clock, tmp_path), n)
    rig.script("(nothing)", "oh hey, sorry")
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply")

    async def go():
        task = asyncio.create_task(rig.engine._speak(discord_turn(104, "dm:104"), meta))
        await rig.advance(150)
        return task.result()
    assert rig.sync(go())
    assert rig.last_user().endswith(f"Answer Zan's message in {length}, in your own voice.")
    rig.close()


def test_the_discord_wording_is_in_her_instruction_the_last_thing_she_reads(store, clock,
                                                                            tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="sounds good")
    turn = discord_turn(102, "dm:102")
    turn.instruction = "You are in a private message with Zan\nAnswer his message: you around"
    assert rig.speak_turn(turn, seconds=150)
    user = rig.last_user()
    wording = DISCORD_DM.format(words=200)
    instruction = user.split("\n\n")[-1]
    assert instruction.endswith(f"{wording} You are in a private message with Zan Answer his "
                                "message: you around")
    assert "short line" not in user
    rig.close()


# -- a reply cut by num_predict -----------------------------------------------------------------

@pytest.mark.parametrize("text,trimmed", [
    ("that sounds great. i can bring flasks for everyone! maybe we could also run the",
     "that sounds great. i can bring flasks for everyone!"),
    ("are you around? i was thinking about the", "are you around?"),
    ("well… maybe we could", "well…"),
    ("see you there 🔥 and then we", "see you there 🔥"),
    ('she said "go." and then we', 'she said "go."'),
    ("maybe we could also run the", "maybe we could also run the"),  # no sentence end: as it is
    ("we need 3.5 more hours of", "we need 3.5 more hours of"),
    ("all done. see you thursday.", "all done. see you thursday."),  # it ended on one
])
def test_trim_to_the_last_sentence_end(text, trimmed):
    assert discord_length.trim_to_sentence(text) == trimmed


def cut_by(rig, reason: str):
    """The fake model's answers, reported as Ollama ends them: `reason` ("length", "stop")."""
    original = rig.llm.chat

    async def chat(model, messages, *, options, think=None, **kw):
        result = await original(model, messages, options=options, think=think, **kw)
        return replace(result, diagnostics=capture_response(
            {"message": {"content": result.text}, "done": True, "done_reason": reason}))
    rig.llm.chat = chat


CUT = "that sounds great. i can bring flasks for everyone! maybe we could also run the"


def said(rig, channel: str) -> str:
    row = store_rows(rig, channel)[-1]
    return rig.engine.said_words(row["id"])


def store_rows(rig, channel: str):
    return rig.store.conn.execute("SELECT * FROM chat_line WHERE channel = ? AND origin = 'mind' "
                                  "ORDER BY id", (channel,)).fetchall()


def test_a_discord_reply_cut_by_num_predict_goes_back_to_its_last_sentence(store, clock,
                                                                          tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind.talk")
    rig = make_rig(store, clock, tmp_path, reply=CUT)
    cut_by(rig, "length")
    assert rig.speak_turn(discord_turn(102, "dm:102"), seconds=150)
    assert said(rig, "dm:102") == "that sounds great. i can bring flasks for everyone"
    logged = [r.getMessage() for r in caplog.records if "discord_line_trimmed" in r.getMessage()]
    assert len(logged) == 1 and "flasks" not in logged[0]  # counts, never the text
    rig.close()


def test_a_discord_reply_with_no_sentence_end_is_kept_as_it_is(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind.talk")
    rig = make_rig(store, clock, tmp_path, reply="maybe we could also run the drakes and the")
    cut_by(rig, "length")
    assert rig.speak_turn(discord_turn(102, "dm:102"), seconds=150)
    assert said(rig, "dm:102") == "maybe we could also run the drakes and the"
    assert not any("discord_line_trimmed" in r.getMessage() for r in caplog.records)
    rig.close()


def test_a_normal_stop_and_a_game_turn_are_never_trimmed(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply=CUT)
    cut_by(rig, "stop")
    assert rig.speak_turn(discord_turn(102, "dm:102"), seconds=150)
    assert said(rig, "dm:102") == CUT
    cut_by(rig, "length")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                               addressee=HUMAN), seconds=60)
    game = " ".join(rig.engine.said_words(r["id"]) for r in store_rows(rig, "guild"))
    assert "maybe we could" in game  # the game's own typing lays it out, never this trim
    rig.close()


# -- every step after the model accepts the longer line -----------------------------------------

def test_the_filters_accept_a_long_discord_message():
    for text in (LONG_DM, LONG_CHANNEL):
        tells = find_tells(text, recent_own=["ok pulling in 5"], speaker="Belra",
                           answers=[HIS_POST], others=["evening all"], samples=["ok pulling in 5"],
                           support=["- I like dungeons"], people=["Arkon", "Belra", "Coros"],
                           window=["anyone up for heroics", HIS_POST], keep_emoji=True)
        assert tells == []
        assert repair(text, speaker="Belra", keep_emoji=True) == text
        assert not is_silence(text)


def test_the_style_keeps_a_long_discord_message_whole(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    typing = rig.engine.personas.get(101).typing
    sober = EFFECTS[Altered.SOBER]
    [line] = style_lines(LONG_CHANNEL, typing, sober, random.Random(1), max_lines=1,
                         cap=math.ceil(90 * LONG_SLACK), game=False)
    assert line.plain == LONG_CHANNEL
    game = style_lines(LONG_CHANNEL, typing, sober, random.Random(1), max_lines=2)
    assert sum(len(s.plain.split()) for s in game) < 40  # the game's line, cut as before
    rig.close()


@pytest.mark.parametrize("channel,text,expected", [
    ("dm:102", LONG_DM, LONG_DM),
    ("discord:general", LONG_CHANNEL, LONG_CHANNEL),
])
def test_a_long_discord_message_goes_out_whole_in_one_ask(store, clock, tmp_path, channel, text,
                                                          expected):
    rig = make_rig(store, clock, tmp_path, reply=text)
    speaker = 102 if channel.startswith("dm:") else 101
    [line] = rig.speak_turn(discord_turn(speaker, channel), seconds=150)
    assert len(rig.chat_calls()) == 1  # no "long" retry
    assert said(rig, channel) == expected and "\n" not in line
    [m] = messages(store.conn, channel.removeprefix("discord:"))
    assert m["text"] == line and len(line.split()) >= len(expected.split())
    rig.close()


def test_the_same_reply_in_the_game_is_still_held_to_her_line(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply=LONG_CHANNEL)
    lines = rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                addressee=HUMAN), seconds=60)
    assert len(rig.chat_calls()) == 2  # asked again shorter, as before
    assert sum(len(x.split()) for x in lines) < 40
    rig.close()


def test_discord_words_zero_keeps_the_old_discord_style(store, clock, tmp_path):
    rig = with_words(make_rig(store, clock, tmp_path, reply=LONG_CHANNEL), 0)
    lines = rig.speak_turn(discord_turn(101, "discord:general"), seconds=150)
    assert len(rig.chat_calls()) == 2  # the "long" tell, as before
    assert sum(len(x.split()) for x in lines) < 60
    rig.close()


def test_the_guards_accept_a_long_discord_message(store):
    seed_test_cast(store)
    now = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()
    on = GuardsCfg(enabled=True)
    earlier = HistoryLine(10, now - 600, 102, LONG_CHANNEL, channel="dm", to_guid=HUMAN,
                          room="102", readers=(HUMAN, 102))
    dm = Draft(102, "dm", LONG_DM, room="102", to_guid=HUMAN, now=now)
    verdict = review(GuardStores(store.conn, config=on, history=(earlier,)), dm)
    assert verdict.action == "send", verdict.codes
    # a server opener: forced semantic checks, its anchor, the filler rule
    server = Draft(101, "discord", LONG_CHANNEL, room="general", now=now, server=True,
                   anchor="raid planning", embedding=pack_vec(unit(0)),
                   anchor_embedding=pack_vec(unit(0)))
    old = HistoryLine(11, now - 900, 101, LONG_DM, channel="discord", room="general",
                      readers=(HUMAN, 101, 102), embedding=pack_vec(unit(1)))
    verdict = review(GuardStores(store.conn, config=on, history=(old,)), server)
    assert verdict.action == "send", verdict.codes
    assert _server_filler(LONG_CHANNEL) is None and not nonsequitur.is_filler(words(LONG_DM))


def test_realism_types_a_long_discord_message_within_25_s(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    rig.engine.cfg = replace(rig.cfg, talk=replace(rig.cfg.talk, realism=True))
    e = rig.engine
    for channel in DISCORD_CHANNELS:
        turn = discord_turn(102, channel)
        turn.deadline = 10 ** 12
        for seed in range(20):
            e.rng = random.Random(seed)
            e.realism = Realism()
            e.realism.member(102).credit = 1.0  # a correction may come too
            baseline = e._first_typing(turn, 400.0)
            assert baseline == 25.0
            plan = e.realism.plan(e, turn, TurnMeta("t1"), e.personas.get(102),
                                  [Styled(LONG_DM, LONG_DM)], [baseline])
            assert sum(plan.delays) + plan.correction_delay <= 25.0 + 1e-9
    # the game keeps its own budget
    game = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN,
                deadline=10 ** 12)
    assert e._first_typing(game, 400.0) == 400.0
    rig.close()


def test_a_long_discord_message_shows_typing_for_a_bounded_time(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply=LONG_DM)
        task = asyncio.create_task(r.engine.speak(discord_turn(102, "dm:102")))
        await until(r, lambda: [p for _, p, _ in r.engine.turn_phases()] == ["typing"])
        typing_from = clock.now()
        await until(r, task.done, seconds=60)
        [m] = messages(store.conn, "dm:102")
        assert 20.0 <= m["ts"] - typing_from <= 25.5  # it types, and not for minutes
        r.close()
    asyncio.run(main())


async def until(r, test, *, seconds: float = 40.0, step: float = 0.25) -> None:
    waited = 0.0
    while not test():
        assert waited < seconds, "timed out"
        await r.advance(step, step)
        waited += step


def test_the_discord_transport_takes_a_long_message_and_the_style_keeps_it_under_2000(
        store, clock, tmp_path):
    message = dstore.post(store, channel="dm:102", author=102, origin="mind", text=LONG_DM,
                          now=clock.now())
    assert messages(store.conn, "dm:102")[-1]["id"] == message
    assert messages(store.conn, "dm:102")[-1]["text"] == LONG_DM
    rig = make_rig(store, clock, tmp_path)
    huge = " ".join(["extraordinarily"] * 250)  # 250 sixteen-character words: 3999 characters
    [line] = style_lines(huge, rig.engine.personas.get(101).typing, EFFECTS[Altered.SOBER],
                         random.Random(1), max_lines=1, cap=math.ceil(200 * LONG_SLACK),
                         game=False, max_chars=DISCORD_MAX_TEXT)
    assert len(line.line) <= DISCORD_MAX_TEXT
    # the plain words lose the same tail: what is stored is what was sent (bot P2 on #271)
    assert len(line.plain.split()) == len(line.line.split())
    # through the pipeline at the largest setting: stored, never refused for length
    with_words(rig, 200)
    rig.script(huge)
    assert rig.speak_turn(discord_turn(101, "discord:general"), seconds=150)
    assert len(messages(store.conn, "general")[-1]["text"]) <= DISCORD_MAX_TEXT
    rig.close()


def test_memory_extraction_reads_a_long_line_whole():
    human = HumanIdentity(1, "Testmage", "Testy", "Zorblat", True)
    lines = [ChatLineRow(i, 1000.0 + i, "dm:102", 102 if i % 2 else 1,
                         "Belra" if i % 2 else "Testmage", 1 if i % 2 else 102,
                         LONG_DM if i % 2 else LONG_CHANNEL, 0, 0, None, None,
                         "mind" if i % 2 else "human") for i in range(1, 13)]
    assert worth_extracting(lines, 1)
    msgs = build_extraction_messages(lines, {1: "Testy", 102: "Belra"}, 1, human=human)
    user = msgs[-1]["content"]
    assert f"[L1] Belra: {LONG_DM}" in user and f"[L2] Testy: {LONG_CHANNEL}" in user
    # a full thread (ThreadTracker's 12 lines) of long lines leaves the extractor room to answer
    used = sum(estimate_tokens(m["content"]) for m in msgs)
    assert EXTRACT.options["num_ctx"] - used > 1000


def test_the_console_shows_a_long_message_whole(store, clock):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    dstore.post(store, channel="dm:102", author=102, origin="mind", text=LONG_DM,
                now=clock.now())
    store.conn.commit()
    view = discordview.discord_view(store.conn, owner_now=datetime(2026, 10, 29, 20,
                                                                    tzinfo=timezone.utc),
                                    trial_rank=5, channel="dm:102")
    [m] = view["messages"]
    assert m["text"] == LONG_DM and not view["limited"]["messages"]
