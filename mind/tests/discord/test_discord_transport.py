"""The guild's Discord as a talk-pipeline transport: a turn on `discord:<id>` or `dm:<guid>` is
composed and typed as usual, its line is stored as a discord_message row and recorded as said,
and nothing of it ever reaches the bridge (no Say, no emote), on any path. Invented text only."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, TEST_CAST
from engine_rig import engine_clock, make_rig
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.layout import audience as layout_audience
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
    # A channel turn takes the paragraph band (discord_length.band); a DM never draws.
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel=place, to_guid=to,
                addressee=HUMAN, discord_draw=0.99)


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
    # [talk] discord_words = 0: her game line's length, as before the longer Discord messages
    rig.engine.cfg = replace(rig.cfg, talk=replace(rig.cfg.talk, discord_words=0))
    # typed as one message: its typing runs within the 120 s answer deadline (ruling R9)
    [said] = rig.speak_turn(discord_turn(rig, 101, "raid-planning"), seconds=150)
    [m] = messages(store.conn, "raid-planning")
    assert m["text"] == said and said.count("\n") == 2
    assert len(chat_rows(store, "discord:raid-planning")) == 1
    assert rig.sent.log == []
    rig.close()


def test_a_longer_message_is_one_paragraph(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path, reply=THREE)  # [talk] discord_words, 90 by default
    [said] = rig.speak_turn(discord_turn(rig, 101, "raid-planning"), seconds=150)
    [m] = messages(store.conn, "raid-planning")
    assert m["text"] == said and "\n" not in said
    assert rig.engine.said_words(chat_rows(store, "discord:raid-planning")[0]["id"]) == THREE
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
    store, a malformed channel and a member with no body in the game all leave the recorder
    empty."""
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
    rig.world.online.discard(105)  # no body in the game: Discord still takes her line (R14)
    assert speak(rig, discord_turn(rig, 105, "dm:105", to=HUMAN))
    assert [m["author"] for m in messages(store.conn, "dm:105")] == [105]
    assert dropped(store) == [("discord:general", "discord_deleted_parent"),
                              ("dm:abc", "discord_bad_channel"),
                              ("dm:105", "discord_not_peer")]
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
    assert thread_key(chat("dm:102", 102, HUMAN)) == "dm:102"
    assert thread_key(chat("say")) == "say:10"
    tracker = ThreadTracker(store)
    ids = []
    for channel in ("discord:general", "say"):
        ev = chat(channel)
        ids.append(store.insert_chat_line(ev, session_id=None, origin="mind"))
        tracker.on_line_sent(ids[-1], ev)
    a, b = store.chat_lines(ids)
    assert a.thread_id != b.thread_id  # a Discord line never joins the zone's /say thread


async def until(r, test, *, seconds: float = 40.0, step: float = 0.25) -> None:
    waited = 0.0
    while not test():
        assert waited < seconds, "timed out"
        await r.advance(step, step)
        waited += step


def run(coro):
    return asyncio.run(coro)


def test_a_speaker_outside_the_channel_s_audience_is_refused(store, clock, tmp_path):
    """R8: the store refuses what the member choice should never ask for: a non-officer in an
    officers' channel, anyone but the DM's own member in a DM. The turn is dropped, nothing is
    stored and nothing reaches the bridge."""
    rig = make_rig(store, clock, tmp_path, reply="noted, thanks")
    assert rig.speak_turn(discord_turn(rig, 101, "officer-chat")) == []
    assert rig.speak_turn(discord_turn(rig, 103, "dm:102", to=HUMAN)) == []
    assert dropped(store) == [("discord:officer-chat", "discord_officers_only"),
                              ("dm:102", "discord_not_peer")]
    assert messages(store.conn, "officer-chat") == [] and messages(store.conn, "dm:102") == []
    assert rig.speak_turn(discord_turn(rig, 102, "officer-chat"))  # an officer may
    assert [m["author"] for m in messages(store.conn, "officer-chat")] == [102]
    assert rig.sent.log == []
    rig.close()


def test_his_dm_and_an_officers_line_reach_no_one_else_s_prompt(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    rig.script("glorbish plans for the officers", "fair enough")
    assert rig.speak_turn(discord_turn(rig, 102, "officer-chat"))
    ev = Chat(ts=0, channel="dm:102", from_guid=HUMAN, from_name="Testmage", to_guid=102,
              text="vexmarrow is my secret plan", map=0, zone=10)
    line = store.insert_chat_line(ev, session_id=None, origin="human",
                                  audience=rig.world.audience("dm:102", from_guid=HUMAN,
                                                              to_guid=102))
    rig.world.add_line(store.chat_lines([line])[0], clock.now())
    rig.run(120)
    for guid, channel in ((103, "guild"), (103, "general"), (101, "general"), (102, "guild"),
                          (104, "general")):
        turn = (Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=channel, addressee=HUMAN)
                if channel == "guild" else discord_turn(rig, guid, channel))
        rig.speak_turn(turn)
        prompt = rig.prompts()[-1]
        assert "vexmarrow" not in prompt and "glorbish" not in prompt, (guid, channel)
    rig.run(120)
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102", to=HUMAN))
    assert "vexmarrow" in rig.prompts()[-1]  # the DM itself still shows him his own line
    assert {c.channel for c in rig.sent.says()} == {"guild"}  # only the game turns
    rig.close()


def test_a_discord_exchange_moves_no_in_game_clock(store, clock, tmp_path):
    """R11: his talk with members in Discord never holds back the game's openers and whispers
    (Pipeline._answered_at, his-talk time, the emote reply hold) nor counts as a member having
    spoken for the game's bystander pick (MindApp._spoke_at)."""
    rig = make_rig(store, clock, tmp_path, reply="sure thing")
    e = rig.engine
    before = e._his_talk_at()
    for turn in (discord_turn(rig, 102, "dm:102", to=HUMAN), discord_turn(rig, 101, "general")):
        meta = TurnMeta(e._new_id(), answers_human=True, why="discord:reply")
        assert speak(rig, turn, meta)
    assert e._answered_at == {} and e._his_talk_at() == before
    assert 101 not in e._replied_at and 102 not in e._replied_at
    assert not e._replying(101, clock.now())
    # the game's own answer still moves them (the control)
    meta = TurnMeta(e._new_id(), answers_human=True, why="reply:named")
    assert speak(rig, Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                           addressee=HUMAN), meta)
    assert set(e._answered_at) == {103} and e._his_talk_at() > before
    # the app's bystander clock
    app = SimpleNamespace(_spoke_at={}, clock=clock, threads=ThreadTracker(store),
                          _queue_closed=lambda closed: None)
    for channel, guid in (("dm:102", 102), ("discord:general", 101), ("guild", 103)):
        ev = Chat(ts=0, channel=channel, from_guid=guid, from_name="x", to_guid=0,
                  text="invented", map=0, zone=10)
        MindApp._on_line_sent(app, store.insert_chat_line(ev, session_id=None, origin="mind"), ev)
    assert set(app._spoke_at) == {103}
    rig.close()


def test_a_dm_turn_is_to_him_and_an_unknown_channel_has_no_audience(store, clock, tmp_path):
    """R12: a dm: turn goes to him even when its turn says nobody; a discord: channel the layout
    lacks reads by nobody, so it fails closed as is_restricted does."""
    rig = make_rig(store, clock, tmp_path, reply="hey you")
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102"))  # to_guid left at 0
    [row] = chat_rows(store, "dm:102")
    assert row["to_guid"] == HUMAN
    [u] = store.conn.execute("SELECT to_guid FROM utterance WHERE channel = 'dm:102'").fetchall()
    assert u["to_guid"] == HUMAN
    assert rig.world.audience("discord:no-such-channel", from_guid=101) == frozenset()
    assert layout_audience("no-such-channel", cast={101}, officers=set(),
                           human=HUMAN) == frozenset()
    rig.close()


def test_a_discord_line_sets_off_no_aside(store, clock, tmp_path):
    """R12: reactions' _after_sent reads a DM as the private line it is, as a whisper; and no
    line in the guild's Discord sets off the men's asides or a rival's flirt there, since D1
    is his to start (members post only in answer to him). A guild line still does."""
    rig = make_rig(store, clock, tmp_path)
    e, seen = rig.engine, []
    e._jealous = lambda trigger, woman, channel, now: seen.append(channel)
    e._rival = lambda turn, woman, now: seen.append("rival")
    p = e.personas.get(102)
    for channel in ("whisper", "dm:102", "discord:general", "discord:officer-chat", "guild"):
        for cheer in (False, True):
            turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel,
                        to_guid=HUMAN if channel in ("whisper", "dm:102") else 0,
                        addressee=HUMAN)
            meta = TurnMeta(e._new_id(), romance=True, loot_cheer=cheer)
            e._after_sent(turn, meta, p, "miss you zan")
    assert seen == ["guild", "rival", "guild"]
    rig.close()


def test_a_discord_line_leaves_his_game_session_alone(store, clock, tmp_path):
    """R11: with his game session open, a member's Discord line, said or dropped, is stored with
    no session id, so the session's last activity (what a restart resumes or ends it by) stays
    where the game left it; a game line (the control) moves it."""
    rig = make_rig(store, clock, tmp_path)
    sid = store.start_session(clock.now())
    rig.engine._session_id = sid  # his session as the engine holds it, without the greetings
    started = store.session_last_activity(sid)
    assert rig.speak_turn(discord_turn(rig, 102, "dm:102", to=HUMAN))
    assert rig.speak_turn(discord_turn(rig, 101, "general"))
    assert speak(rig, discord_turn(rig, 103, "dm:105", to=HUMAN)) == []  # not her DM: dropped
    assert store.session_last_activity(sid) == started
    assert {r["session_id"] for r in store.conn.execute(
        "SELECT session_id FROM chat_line UNION ALL SELECT session_id FROM utterance")} == {None}
    assert rig.speak_turn(Turn(kind=TurnKind.OPEN, speaker=104, channel="guild"))
    assert store.session_last_activity(sid) > started
    rig.close()
