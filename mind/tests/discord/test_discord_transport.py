"""The guild's Discord as a talk-pipeline transport: a turn on `discord:<id>` or `dm:<guid>` is
composed and typed as usual, its line is stored as a discord_message row and recorded as said,
and nothing of it ever reaches the bridge (no Say, no emote), on any path. Invented text only."""
import asyncio

import pytest

from conftest import HUMAN, TEST_CAST
from engine_rig import engine_clock, make_rig
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.transport import DiscordTarget, channel_id, is_discord
from leeroy_mind.memory.threads import ThreadTracker, thread_key
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.pipeline import TurnMeta, chain_base
from leeroy_mind.talk.turns import Turn, TurnKind
from tuning.scenario import golden, record

THREE = ("the first pull went fine but the adds came in too early. "
         "we should try the drake first thing next week with more healers. "
         "bring your own flasks this time and read the tactics first please")


@pytest.fixture
def clock():
    return engine_clock()


def discord_turn(rig, speaker: int, channel: str, to: int = 0) -> Turn:
    """A member's answer to him in the guild's Discord: `channel` a layout id ("general") or a
    DM ("dm:<guid>")."""
    place = channel if channel.startswith("dm:") else f"discord:{channel}"
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel=place, to_guid=to,
                addressee=HUMAN)


def speak(rig, turn: Turn, meta: TurnMeta | None = None, seconds: float = 60.0) -> list[str]:
    """One turn through the whole pipeline with its own meta (TalkEngine._speak)."""
    meta = meta if meta is not None else TurnMeta(rig.engine._new_id(), why="test")

    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(seconds)
        assert task.done()
        return task.result()
    return rig.sync(go())


def chat_rows(store, channel: str):
    return store.conn.execute(
        "SELECT * FROM chat_line WHERE channel = ? ORDER BY id", (channel,)).fetchall()


def dropped(store) -> list[tuple[str, str]]:
    return [(u["channel"], u["dropped"]) for u in store.conn.execute(
        "SELECT channel, dropped FROM utterance WHERE dropped IS NOT NULL ORDER BY id")]


def his_post(store, clock, channel: str = "general", text: str = "who is around tonight") -> int:
    return dstore.post(store, channel=channel, author=HUMAN, origin="human", text=text,
                       now=clock.now())


def test_channel_strings():
    assert is_discord("discord:general") and is_discord("dm:102") and is_discord("dm:abc")
    assert not any(is_discord(c) for c in ("guild", "officer", "whisper", "say", "voice:raid",
                                           "party", ""))
    assert channel_id("discord:general") == "general"
    assert channel_id("dm:102") == "dm:102"


def test_a_discord_turn_never_sends_say_or_emote(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="lol same 😂")
    for turn in (discord_turn(rig, 101, "general"), discord_turn(rig, 102, "dm:102", to=HUMAN)):
        assert rig.speak_turn(turn)
    assert rig.sent.log == []
    rows = messages(store.conn, "general") + messages(store.conn, "dm:102")
    assert [r["origin"] for r in rows] == ["mind", "mind"]
    assert [r["author"] for r in rows] == [101, 102]
    rig.close()


def test_a_member_line_keeps_its_emoji(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="omg 👩🏽‍💻🔥")
    assert rig.speak_turn(discord_turn(rig, 101, "general"))
    assert messages(store.conn, "general")[-1]["text"].endswith("👩🏽‍💻🔥")
    rig.close()


def test_the_game_still_folds_emoji_away(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="omg 👩🏽‍💻🔥 nice")
    [line] = rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                 addressee=HUMAN))
    assert line.isascii() and [s.text for s in rig.sent.says()] == [line]
    rig.close()


def test_a_reply_turn_stores_its_reply_to_and_thread(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="count me in")
    parent = his_post(store, clock)
    thread = dstore.open_thread(store, parent, name="tonight", now=clock.now())
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=parent, thread=thread))
    assert speak(rig, discord_turn(rig, 103, "general"), meta)
    [m] = messages(store.conn, "general", thread=thread)
    assert (m["author"], m["origin"], m["reply_to"], m["thread"]) == (103, "mind", parent,
                                                                      thread)
    assert store.conn.execute("SELECT turn FROM discord_message WHERE id = ?",
                              (m["id"],)).fetchone()[0] == meta.turn_id
    assert rig.sent.log == []
    rig.close()


def test_a_discord_line_is_recorded_as_a_chat_line_and_counts_as_said(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="sounds good to me")
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply")
    [said] = speak(rig, discord_turn(rig, 101, "general"), meta)
    assert meta.said == 1
    [row] = chat_rows(store, "discord:general")
    assert (row["from_guid"], row["origin"], row["text"]) == (101, "mind", said)
    assert set(store.chat_lines([row["id"]])[0].audience) == set(TEST_CAST) | {HUMAN}
    [m] = messages(store.conn, "general")
    assert m["text"] == said
    [u] = store.conn.execute("SELECT * FROM utterance").fetchall()
    assert (u["guid"], u["channel"], u["dropped"], u["turn_id"]) == (
        101, "discord:general", None, meta.turn_id)
    # it counts for the guards like any line: the same member, right after it, is held back
    assert not rig.engine.guards.allow(101, TurnKind.REPLY_HUMAN, row["ts"])
    rig.close()


def test_the_audience_of_a_discord_line(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    cast = frozenset(TEST_CAST)
    assert rig.world.audience("discord:general", from_guid=101) == cast | {HUMAN}
    assert rig.world.audience("discord:officer-chat", from_guid=102) == {102, 104, HUMAN}
    assert rig.world.audience("dm:102", from_guid=102, to_guid=HUMAN) == {102, HUMAN}
    rig.close()


def test_a_discord_turn_is_typed_and_shows_typing_dots(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply="on my way", gate=gate)
        r.world.human_online = False  # out of the game: Discord shows the dots all the same
        start = clock.now()
        task = asyncio.create_task(r.engine.speak(discord_turn(r, 102, "dm:102", to=HUMAN)))
        await until(r, lambda: [p for _, p, _ in r.engine.turn_phases()] == ["composing"])
        [entry] = r.engine.typing_now()
        assert (entry["guid"], entry["channel"], entry["to"]) == (102, "dm:102", HUMAN)
        gate.set()
        await until(r, lambda: [p for _, p, _ in r.engine.turn_phases()] == ["typing"])
        [entry] = r.engine.typing_now()
        assert entry["channel"] == "dm:102"
        typing_from = clock.now()
        await until(r, task.done)
        assert task.result() and r.engine.typing_now() == []
        [m] = messages(store.conn, "dm:102")
        assert m["ts"] > typing_from > start  # the typing delay ran before it landed
        assert r.sent.log == []
    run(main())


def test_typing_dots_in_a_discord_channel_show_while_he_is_in_game(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply="on my way", gate=gate)
        task = asyncio.create_task(r.engine.speak(discord_turn(r, 101, "strategy")))
        await until(r, lambda: len(r.engine.turn_phases()) == 1)
        assert [(e["guid"], e["channel"], e["to"]) for e in r.engine.typing_now()] == [
            (101, "discord:strategy", None)]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        gate.set()
    run(main())


def test_whisper_rules_do_not_block_a_dm_when_he_is_offline(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="hey you around later")
    rig.world.human_online = False
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="whisper",
                               to_guid=HUMAN, addressee=HUMAN)) == []
    assert dropped(store) == [("whisper", "human_offline")]  # the game's rule, as before
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102", to=HUMAN))
    assert len(messages(store.conn, "dm:102")) == 1 and rig.sent.log == []
    rig.close()


def test_spoken_channel_rules_do_not_apply(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="ok see you there")
    rig.engine._dead[101] = clock.now()  # died a moment ago: /say is refused, Discord is not
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="say",
                               addressee=HUMAN)) == []
    assert dropped(store) == [("say", "dead")]
    assert rig.speak_turn(discord_turn(rig, 101, "general"))
    assert len(messages(store.conn, "general")) == 1
    rig.close()


def test_three_lines_become_one_message(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply=THREE)
    [said] = rig.speak_turn(discord_turn(rig, 101, "raid-planning"))
    [m] = messages(store.conn, "raid-planning")
    assert m["text"] == said and said.count("\n") == 2
    assert len(chat_rows(store, "discord:raid-planning")) == 1
    assert rig.sent.log == []
    rig.close()


def test_the_wow_golden_prompts_are_unchanged(store, clock, tmp_path):
    assert record(store, clock, tmp_path) == golden()


def test_a_cancelled_discord_turn_stores_nothing(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="on my way")
        task = asyncio.create_task(r.engine.speak(discord_turn(r, 101, "general")))
        await until(r, lambda: [p for _, p, _ in r.engine.turn_phases()] == ["typing"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await r.advance(60)
        assert messages(store.conn, "general") == [] and chat_rows(store, "discord:general") == []
        assert dropped(store) == [("discord:general", "cancelled")]
        assert r.sent.log == []
    run(main())


def test_nothing_reaches_the_bridge_on_any_path(store, clock, tmp_path):
    """Every command the bridge is handed is recorded before its ack, which here always times
    out: posts, a reply, a retry after a tell, an emote typed into the line, a refusal by the
    store, a malformed channel and an offline member all leave the recorder empty."""
    def ack(cmd):
        raise AckTimeout("ack timed out")
    replies = iter(["as an ai i cannot say", "fine by me /hug"])
    rig = make_rig(store, clock, tmp_path, ack=ack,
                   script=lambda model, messages: next(replies, "fine by me /hug"))
    assert rig.speak_turn(discord_turn(rig, 101, "general"))  # retried, then said
    assert "/hug" not in messages(store.conn, "general")[-1]["text"]
    parent = his_post(store, clock)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True,
                    discord=DiscordTarget(reply_to=parent, thread=None))
    assert speak(rig, discord_turn(rig, 102, "general"), meta)
    gone = his_post(store, clock, text="never mind")
    dstore.delete(store, gone, author=HUMAN, now=clock.now())
    meta = TurnMeta(rig.engine._new_id(), discord=DiscordTarget(reply_to=gone, thread=None))
    assert speak(rig, discord_turn(rig, 103, "general"), meta) == []
    assert speak(rig, Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="dm:abc",
                           addressee=HUMAN)) == []
    assert speak(rig, discord_turn(rig, 103, "dm:105", to=HUMAN)) == []  # not her DM
    rig.world.online.discard(105)
    assert speak(rig, discord_turn(rig, 105, "dm:105", to=HUMAN)) == []
    assert dropped(store) == [("discord:general", "discord_deleted_parent"),
                              ("dm:abc", "discord_bad_channel"),
                              ("dm:105", "discord_not_peer"), ("dm:105", "offline")]
    assert rig.sent.log == []
    assert [r["origin"] for r in messages(store.conn, "general")] == [
        "mind", "human", "mind", "human"]  # his deleted post stays as a row
    rig.close()


def test_a_discord_line_never_reaches_a_game_prompt(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    rig.script("zorblat in the dm 😂", "quixfen in general", "fair enough", "fair enough")
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102", to=HUMAN))
    assert rig.speak_turn(discord_turn(rig, 101, "general"))
    rig.run(120)
    for guid in (102, 101):
        assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel="guild",
                                   addressee=HUMAN))
        prompt = rig.prompts()[-1]
        assert "zorblat" not in prompt and "quixfen" not in prompt
    rig.close()


def test_a_discord_line_leaves_the_game_s_chat_state_alone(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply="sounds good")
    e = rig.engine
    history, quiet = len(rig.world.history), e._last_talk_at
    assert rig.speak_turn(discord_turn(rig, 101, "general"))
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102", to=HUMAN))
    assert len(rig.world.history) == history and e._last_talk_at == quiet
    assert 101 not in e._last_line_id and 102 not in e._last_line_id
    assert not any(k.startswith("say:") for k in e._chains)
    assert chain_base("discord:general", 101, 0, 10) == "discord:general"
    assert chain_base("dm:102", 102, HUMAN, 10) == "dm:102"
    rig.close()


def test_discord_lines_keep_out_of_the_game_s_threads(store, clock, tmp_path):
    def chat(channel, frm=101, to=0):
        return Chat(ts=0, channel=channel, from_guid=frm, from_name="x", to_guid=to,
                    text="invented", map=0, zone=10)
    assert thread_key(chat("discord:general")) == "discord:general"
    assert thread_key(chat("say")) == "say:10"
    tracker = ThreadTracker(store)
    ev = chat("dm:102", 102, HUMAN)
    line = store.insert_chat_line(ev, session_id=None, origin="mind")
    assert tracker.on_line_sent(line, ev) is None
    assert store.chat_lines([line])[0].thread_id is None  # a DM is never threaded


async def until(r, test, *, seconds: float = 40.0, step: float = 0.25) -> None:
    waited = 0.0
    while not test():
        assert waited < seconds, "timed out"
        await r.advance(step, step)
        waited += step


def run(coro):
    return asyncio.run(coro)
