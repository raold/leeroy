"""His speech (Task 15): a transcript is his line in his room, a few exact short phrases are
commands in game chat, and his key holds the answers. The invented cast only."""
import asyncio
import json
import logging

import pytest

from conftest import TEST_CAST, run
from engine_rig import RAID, engine_clock, numbered
from leeroy_mind.bridge import AckTimeout, ConnectionLost, NotConnected
from leeroy_mind.protocol import Ack, Cast, Chat, DcStatus, Group, HumanSay, Member, parse_event
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice import wire
from leeroy_mind.voice.heard import BOT_COMMANDS, DC_COMMANDS, command_for, is_command
from voice.conftest import HUMAN, status

SECRET = "the words he said"
HIS_NAME = "Testmage"
HIS_MAP = 571
HIS_ZONE = 10


@pytest.fixture
def clock():
    return engine_clock()


class Game:
    """bridge.send: what reached game chat, or `error` raised at it."""

    def __init__(self, v, error=None, ok=True):
        self.sent, self.error, self.ok = [], error, ok
        v.app.bridge.send = self

    async def __call__(self, cmd):
        if self.error is not None:
            raise self.error
        self.sent.append(cmd)
        return Ack(0, cmd.id, self.ok, "" if self.ok else "human offline")

    def lines(self) -> list[tuple[str, str]]:
        return [(c.channel, c.text) for c in self.sent]


async def on_air(v, *members, room="guild_hall", him=None) -> None:
    """His session open, the whole cast online and these members seated with him."""
    v.fake.status = status()
    await v.hello()
    rows = [Member(guid=g, name=n, race=r, class_=c, gender=0, level=80, rank=3, online=True,
                   map=HIS_MAP, zone=HIS_ZONE, spec_tab=-1) for g, (n, r, c) in TEST_CAST.items()]
    rows.append(Member(guid=HUMAN, name=HIS_NAME, race=10, class_=8, gender=0, level=80, rank=0,
                       online=True, map=HIS_MAP, zone=HIS_ZONE, spec_tab=-1))
    await v.app.handle(Cast(v._ts(), tuple(rows)))
    v.app.world.human_zone = HIS_ZONE
    rooms = v.ext.rooms
    rooms.clear()
    for g in members:
        rooms.move(g, room)
    rooms.move(HUMAN, him or room)
    v.sent.clear()


async def settle(v) -> None:
    """The bridge's dispatcher, for what on_heard injected."""
    queue = v.app.bridge._queue
    while not queue.empty():
        _, ev = queue.get_nowait()
        await v.app.handle(ev)


async def hear(v, text, room="guild_hall", *, raw=None, confidence=0.9) -> None:
    await v.ext.on_message(wire.Heard(text, raw or text, confidence, room, 1200, 150))
    await settle(v)


async def group(v, *members, raid=True, leader=102) -> None:
    await v.app.handle(Group(ts=v._ts(), leader_guid=leader, raid=raid, members=members))


def talks(v, reply="on it") -> None:
    v.app.llm._script = reply if callable(reply) else (lambda model, messages: reply)


def speaks(v) -> list:
    return [m for m in v.sent if isinstance(m, wire.Speak)]


def lines(v) -> list:
    return v.rows("SELECT * FROM chat_line WHERE origin = 'human' ORDER BY id")


def model_calls(v) -> list:
    return [c for c in v.app.llm.calls if "messages" in c]


def heard_rows(v) -> list:
    return v.rows("SELECT * FROM voice_heard ORDER BY line_id")


# -- the phrases -------------------------------------------------------------------------------

def test_the_command_maps():
    assert DC_COMMANDS == {"pull": "dc pause resume", "go": "dc pause resume",
                           "hold": "dc pause pause", "stop": "dc pause pause"}
    assert BOT_COMMANDS == {"follow": "follow", "follow me": "follow", "stay": "stay",
                            "stay here": "stay", "attack": "attack", "pull": "pull",
                            "hold": "stay", "stop": "stay"}


@pytest.mark.parametrize("text", ["hold", "Hold.", "HOLD!", " pull ", "Follow me!", "stay here.",
                                  "Stay, here.", "go", "Stop...", "attack\n", "“go”"])
def test_is_command(text):
    assert is_command(text)


@pytest.mark.parametrize("text", [
    "", "   ", ".", "hold on", "go ahead", "pull now", "follow me to the bank later", "stop it",
    "stay on the boss", "attack the adds", "please hold", "I said pull", "pull pull", "goes",
    "follow-me", "poll", "pool", "Pull?", "go?", "Stop?", "Belra, hold"])
def test_is_not_a_command(text):
    assert not is_command(text)


def test_command_for_in_and_out_of_a_run():
    assert command_for("Hold.", dc_run=True) == "dc pause pause"
    assert command_for("stop", dc_run=True) == "dc pause pause"
    assert command_for("pull", dc_run=True) == "dc pause resume"
    assert command_for("Go!", dc_run=True) == "dc pause resume"
    assert command_for("follow me", dc_run=True) == "follow"  # no dc form: the bots' own
    assert command_for("attack", dc_run=True) == "attack"
    assert command_for("hold", dc_run=False) == "stay"
    assert command_for("stop", dc_run=False) == "stay"
    assert command_for("pull", dc_run=False) == "pull"
    assert command_for("stay here", dc_run=False) == "stay"
    assert command_for("go", dc_run=False) is None
    assert command_for("hold on a second", dc_run=False) is None
    assert command_for("hold on a second", dc_run=True) is None
    assert command_for("Pull?", dc_run=True) is None


# -- his line in the room ----------------------------------------------------------------------

def test_heard_is_his_line_in_the_room(vapp):
    v = vapp

    async def go():
        await on_air(v, 101, 102, room="raid")
        v.ext.rooms.move(103, "guild_hall")
        talks(v)
        await hear(v, "Belra, you're on adds", "raid", raw="bella youre on ads", confidence=0.83)
        (row,) = lines(v)
        assert (row["channel"], row["from_guid"], row["to_guid"], row["text"]) == (
            "voice:raid", HUMAN, 0, "Belra, you're on adds")
        assert row["audience"] == "[101, 102]"
        assert (row["map"], row["zone"]) == (HIS_MAP, HIS_ZONE)
        assert row["thread_id"] is not None
        (kept,) = heard_rows(v)
        assert (kept["line_id"], kept["raw"], kept["confidence"], kept["speech_ms"],
                kept["latency_ms"]) == (row["id"], "bella youre on ads", 0.83, 1200, 150)
        await v.clock.advance(20, 1.0)
        await v.app.engine._cancel_all()
    run(go())
    assert [(s.guid, s.channel) for s in speaks(v)] == [(102, "raid")]  # G5: she alone


def test_heard_in_a_private_room(vapp):
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        await hear(v, "I missed you", "private1", raw="i mist you")
        (row,) = lines(v)
        assert (row["channel"], row["to_guid"], row["audience"]) == ("whisper", 102, "[102]")
        (kept,) = heard_rows(v)
        assert (kept["line_id"], kept["raw"]) == (row["id"], "i mist you")
    run(go())


def test_her_answer_in_a_private_room_is_a_whisper_he_hears(vapp):
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        v.app.engine.rng.random = lambda: 0.0  # she answers
        talks(v)
        await hear(v, "I missed you", "private1")
        await v.clock.advance(20, 1.0)
        await v.app.engine._cancel_all()
    run(go())
    (s,) = speaks(v)
    assert (s.guid, s.channel, s.to_human, s.priority) == (102, "private1", True, "p3")
    (row,) = v.rows("SELECT channel, to_guid, audience FROM chat_line WHERE origin = 'mind'")
    assert (row["channel"], row["to_guid"], row["audience"]) == ("whisper", HUMAN, "[102]")


def test_alone_in_a_private_room_is_no_whisper(vapp):
    v = vapp

    async def go():
        await on_air(v, room="private1")
        await hear(v, "testing, one two", "private1")
        (row,) = lines(v)
        assert (row["channel"], row["to_guid"], row["audience"]) == ("voice:private1", 0, "[]")
    run(go())


def test_a_whisper_in_the_room_he_left_stays_a_whisper(vapp):
    """Said in the private room: the key came up there, whatever the seats are by now."""
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        v.ext.rooms.move(HUMAN, "guild_hall")
        await hear(v, "back in a bit", "private1")
        (row,) = lines(v)
        assert (row["channel"], row["to_guid"]) == ("whisper", 102)
    run(go())


def test_a_private_line_stays_out_of_every_other_prompt(vapp):
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        v.ext.rooms.move(101, "guild_hall")
        talks(v, numbered("line {n} from me"))
        await hear(v, SECRET, "private1")
        await v.clock.advance(25, 1.0)
        v.ext.rooms.move(HUMAN, "guild_hall")
        v.app.llm.calls.clear()
        await v.app.engine.speak(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        await v.app.engine._cancel_all()
    run(go())
    prompts = [m["content"] for c in model_calls(v) if c for m in c["messages"]]
    assert prompts and not any(SECRET in p for p in prompts)


def test_injected_line_is_fresh_and_his(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        v.app.store.set_meta("human_name", "Chatname")
        v.app._human_name = "Chatname"
        talks(v)
        await v.clock.advance(120, 10.0)
        v.app._hello_ts = v._ts()  # a ts from before this would read as replayed
        now = v.clock.now()
        await hear(v, "Belra, you there?")
        (row,) = lines(v)
        assert row["ts"] == now
        assert row["from_name"] == "Chatname"
        assert v.app.store.get_meta("human_name") == "Chatname"
        assert heard_rows(v)[0]["line_id"] == row["id"]
        await v.clock.advance(20, 1.0)
        await v.app.engine._cancel_all()
    run(go())
    assert [s.guid for s in speaks(v)] == [102]


def test_his_name_falls_back_to_the_roster(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        assert not v.app.store.get_meta("human_name")
        await hear(v, "evening all")
        assert lines(v)[0]["from_name"] == HIS_NAME
    run(go())


def test_two_lines_alike_each_keep_their_own_recognition(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        await hear(v, "nice one", raw="first")
        await v.clock.advance(1, 0.5)
        await hear(v, "nice one", raw="second")
        ids = [r["id"] for r in lines(v)]
        assert [(r["line_id"], r["raw"]) for r in heard_rows(v)] == [(ids[0], "first"),
                                                                     (ids[1], "second")]
    run(go())


def test_a_line_that_never_landed_touches_no_older_row(vapp):
    """The app's row failed: the older line with the same words keeps its recognition."""
    v = vapp

    async def go():
        await on_air(v, 102)
        await hear(v, "nice one", raw="first")
        await v.clock.advance(1, 0.5)

        def fail(*a, **k):
            raise RuntimeError("no row")
        real = v.app.store.insert_chat_line
        v.app.store.insert_chat_line = fail
        await hear(v, "nice one", raw="second")
        v.app.store.insert_chat_line = real
        assert [r["raw"] for r in heard_rows(v)] == ["first"]
    run(go())


def test_empty_text_enters_nothing(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        await hear(v, "   ")
        assert not lines(v)
    run(go())


@pytest.mark.parametrize("room", ["", "nowhere"])
def test_off_comms_nothing_enters_a_room(vapp, room):
    """Task 11's carry: a press off comms sends heard with channel "": nobody hears it."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        talks(v)
        await hear(v, "Belra, you're on adds", room)
        await hear(v, "hold", room)
        await v.clock.advance(20, 1.0)
        assert not lines(v) and not heard_rows(v) and not speaks(v) and not game.sent
    run(go())


def test_heard_after_logout_ignored(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 102)
        await group(v, *RAID)
        await v.logout()
        await hear(v, "hold")
        await hear(v, "are you still there")
        assert not lines(v) and not game.sent
    run(go())


def test_his_line_is_heard_with_the_output_down(vapp):
    """The service reports its output, not him: a dead output takes no word of his away."""
    v = vapp

    async def go():
        await on_air(v, 102)
        v.fake.status = status(ok=False)
        await hear(v, "can anyone hear me")
        assert len(lines(v)) == 1
    run(go())


# -- commands ----------------------------------------------------------------------------------

def test_commands_in_a_dc_run(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await v.app.handle(DcStatus(v._ts(), 101, True, False, "STATUS"))
        await hear(v, "Hold.", "raid")
        await hear(v, "pull", "raid")
        await hear(v, "go!", "raid")
        await hear(v, "Stop", "raid")
        await hear(v, "follow me", "raid")
        assert game.lines() == [("raid", "dc pause pause"), ("raid", "dc pause resume"),
                                ("raid", "dc pause resume"), ("raid", "dc pause pause"),
                                ("raid", "follow")]
        assert all(isinstance(c, HumanSay) for c in game.sent)
    run(go())


def test_commands_outside_a_dc_run(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "pull", "raid")
        await hear(v, "hold", "raid")
        await hear(v, "stop", "raid")
        assert game.lines() == [("raid", "pull"), ("raid", "stay"), ("raid", "stay")]
        game.sent.clear()
        await hear(v, "go", "raid")
        assert game.lines() == []
        await group(v, HUMAN, 101, raid=False, leader=HUMAN)
        await hear(v, "Follow me!", "raid")
        assert game.lines() == [("party", "follow")]
        game.sent.clear()
        await v.app.handle(Group(ts=v._ts(), leader_guid=0, raid=False, members=()))
        await hear(v, "follow me", "raid")
        await hear(v, "hold", "raid")
        assert game.lines() == []
    run(go())


def test_a_group_without_him_is_not_his(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, 101, 102, 103)
        await hear(v, "hold", "raid")
        assert not game.sent
    run(go())


@pytest.mark.parametrize("tank, status_on, members, sent", [
    (105, True, (HUMAN, 101, 102), "stay"),  # a tank outside his group is no run
    (101, False, (HUMAN, 101, 102), "stay"),  # the latest says it ended
    (101, True, (HUMAN, 101, 102), "dc pause pause"),
])
def test_the_run_follows_his_groups_tank(vapp, tank, status_on, members, sent):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *members)
        await v.app.handle(DcStatus(v._ts(), tank, status_on, False, "STATUS"))
        await hear(v, "hold", "raid")
        assert game.lines() == [("raid", sent)]
    run(go())


def test_the_latest_status_wins_per_tank(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, HUMAN, 101, 102)
        await v.app.handle(DcStatus(v._ts(), 101, True, False, "STATUS"))
        await v.app.handle(DcStatus(v._ts(), 105, False, False, "STATUS"))  # someone else's
        await hear(v, "hold", "raid")
        await v.app.handle(DcStatus(v._ts(), 101, False, False, "STATUS"))
        await hear(v, "hold", "raid")
        await v.app.handle(DcStatus(v._ts(), 101, True, False, "STATUS"))
        await group(v, HUMAN, 102, 103)  # the tank left his group
        await hear(v, "hold", "raid")
        assert game.lines() == [("raid", "dc pause pause"), ("raid", "stay"), ("raid", "stay")]
    run(go())


def dc_edge(v, tank, enabled):
    """A run's edge as mod-leeroy puts it on the wire: no addon payload (LeeroyPollDcTanks)."""
    return parse_event(json.dumps({"v": 1, "type": "dc_status", "ts": v._ts(), "tank_guid": tank,
                                   "enabled": enabled, "paused": False, "payload": ""}))


def test_both_edges_of_a_run_move_the_mapping(vapp):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "hold", "raid")
        await v.app.handle(dc_edge(v, 101, True))
        await hear(v, "hold", "raid")
        await hear(v, "pull", "raid")
        await v.app.handle(dc_edge(v, 101, False))
        await hear(v, "hold", "raid")
        await hear(v, "pull", "raid")
        await hear(v, "go", "raid")
        assert game.lines() == [("raid", "stay"), ("raid", "dc pause pause"),
                                ("raid", "dc pause resume"), ("raid", "stay"), ("raid", "pull")]
    run(go())


def test_a_run_outlives_his_relog(vapp):
    """mod-leeroy tells only of the edges, and he leaving is none: the map stays its mirror."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await v.app.handle(dc_edge(v, 101, True))
        await v.logout()
        await v.login()
        v.ext.rooms.move(HUMAN, "raid")
        await hear(v, "hold", "raid")
        await v.app.handle(dc_edge(v, 101, False))
        await hear(v, "hold", "raid")
        assert game.lines() == [("raid", "dc pause pause"), ("raid", "stay")]
    run(go())


def test_a_hello_forgets_the_runs(vapp):
    """A greeting starts over: the module tells a new connection who is running right after it,
    and a restarted server has nobody running and could not tell of a run's end."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await v.app.handle(dc_edge(v, 101, True))
        await v.hello()
        await hear(v, "hold", "raid")
        await v.app.handle(dc_edge(v, 101, True))
        await hear(v, "hold", "raid")
        assert game.lines() == [("raid", "stay"), ("raid", "dc pause pause")]
    run(go())


@pytest.mark.parametrize("error", [NotConnected("down"), AckTimeout("slow"), ConnectionLost("x"),
                                   ValueError(SECRET)])
def test_human_say_failure_is_caught(vapp, caplog, error):
    v = vapp

    async def go():
        Game(v, error=error)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        with caplog.at_level(logging.DEBUG):
            await hear(v, "hold", "raid")  # no exception
        assert len(lines(v)) == 1  # the line is in the room all the same
    run(go())
    failed = [r for r in caplog.records if "voice_command_failed" in r.getMessage()]
    assert len(failed) == 1
    assert type(error).__name__ in failed[0].getMessage()
    assert SECRET not in caplog.text


def test_the_real_bridge_down_is_caught(vapp, caplog):
    v = vapp

    async def go():
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "hold", "raid")
    with caplog.at_level(logging.INFO):
        run(go())
    assert "voice_command_failed" in caplog.text and "NotConnected" in caplog.text


def test_a_refused_command_is_logged_by_its_reason(vapp, caplog):
    v = vapp

    async def go():
        Game(v, ok=False)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "hold", "raid")
    with caplog.at_level(logging.INFO):
        run(go())
    assert "voice_command_failed" in caplog.text and "human offline" in caplog.text


@pytest.mark.parametrize("text", [
    "follow me to the bank later", "hold on a second", "go ahead", "stop it you two",
    "I said pull", "pull when you're ready", "we should attack the adds", "stay here with me",
    "pool table", "the poll closes tonight", "Pull?", "Stop?"])
def test_casual_speech_never_reaches_chat(vapp, text):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await v.app.handle(DcStatus(v._ts(), 101, True, False, "STATUS"))
        await hear(v, text, "raid")
        assert not game.sent
        (row,) = lines(v)
        assert row["text"] == text and row["channel"] == "voice:raid"
        assert row["thread_id"] is not None  # a line in the room like any other
    run(go())


def test_command_gets_no_replies(vapp):
    """M27: heard by the room, answered by nobody."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        talks(v)
        at = v.clock.now() + 5
        await v.clock.advance(5, 1.0)
        await hear(v, "hold", "raid")
        (row,) = lines(v)
        assert (row["channel"], row["thread_id"], row["audience"]) == ("voice:raid", None,
                                                                      "[101, 102]")
        assert heard_rows(v)[0]["line_id"] == row["id"]  # stored like any line of his
        assert v.app.world.human_last_active == at  # he is active
        assert game.lines() == [("raid", "stay")]
        await v.clock.advance(30, 1.0)
        assert not speaks(v) and not model_calls(v)
    run(go())


def test_a_command_with_no_group_is_answered_by_nobody(vapp):
    """M27 goes by the words: "hold" with no group reaches no game and starts no talk."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102)
        talks(v)
        await hear(v, "hold")
        await v.clock.advance(30, 1.0)
        assert len(lines(v)) == 1 and lines(v)[0]["thread_id"] is None
        assert not speaks(v) and not game.sent
    run(go())


def test_his_stats_count_a_spoken_command_apart(vapp, caplog):
    v = vapp

    async def go():
        Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        v.app._stats_since = v.clock.now()
        await v.clock.advance(1, 0.5)
        await hear(v, "hold", "raid")
        await hear(v, "good pull everyone", "raid")
        await v.clock.advance(1, 0.5)
        v.app._log_stats(v.clock.now())
    with caplog.at_level(logging.INFO):
        run(go())
    assert "his_lines=1" in caplog.text and "his_commands=1" in caplog.text


# -- the soundalikes (Task 9: Parakeet hears "pull" as "poll" or "pool") -----------------------

@pytest.mark.parametrize("text", ["poll", "Pool.", "POLL!", " pool "])
def test_a_lone_soundalike_is_pull(vapp, text):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        talks(v)
        await hear(v, text, "raid", raw=text)
        assert game.lines() == [("raid", "pull")]
        await v.app.handle(DcStatus(v._ts(), 101, True, False, "STATUS"))
        await hear(v, text, "raid", raw=text)
        assert game.lines()[-1] == ("raid", "dc pause resume")
        rows = lines(v)
        assert [(r["text"], r["thread_id"]) for r in rows] == [("pull", None)] * 2  # a command
        assert [r["raw"] for r in heard_rows(v)] == [text, text]  # what he was heard saying
        await v.clock.advance(30, 1.0)
        assert not speaks(v)
    run(go())


@pytest.mark.parametrize("text", ["poll", "Pool."])
def test_a_soundalike_with_no_group_is_a_line(vapp, text):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102)
        talks(v)
        await hear(v, text)
        (row,) = lines(v)
        assert (row["text"], game.sent) == (text, [])
        assert row["thread_id"] is not None
    run(go())


@pytest.mark.parametrize("text", ["the pool is over there", "poll now", "Pool?", "pool pool"])
def test_a_soundalike_in_a_sentence_is_a_line(vapp, text):
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, text, "raid")
        (row,) = lines(v)
        assert (row["text"], game.sent) == (text, [])
        assert row["thread_id"] is not None
    run(go())


# -- the echo of a command: the game reports it as a line he typed ------------------------------

def test_the_echo_of_pull_gets_no_replies(vapp):
    """playerbots parses `pull`, but the typed rule (BOT_COMMANDS) never knew it, so its echo
    would be a raid line of his that the whole raid answers."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        talks(v)
        await hear(v, "pull", "raid")
        assert game.lines() == [("raid", "pull")]
        echo = Chat(v._ts(), "raid", HUMAN, HIS_NAME, 0, "pull", HIS_MAP, HIS_ZONE)
        await v.app.handle(echo)
        await v.clock.advance(30, 1.0)
        assert not speaks(v) and not model_calls(v)
        assert [(r["channel"], r["thread_id"]) for r in lines(v)] == [
            ("voice:raid", None), ("raid", None)]
    run(go())


def test_his_typed_pull_is_still_talk(vapp):
    """Only an echo: with nothing spoken, and once an echo was consumed, as in phase 1."""
    v = vapp

    async def go():
        Game(v)
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "pull", "raid")
        chat = Chat(v._ts(), "raid", HUMAN, HIS_NAME, 0, "pull", HIS_MAP, HIS_ZONE)
        await v.app.handle(chat)
        await v.app.handle(chat)
        rows = lines(v)
        assert [r["thread_id"] is None for r in rows] == [True, True, False]
        await v.clock.advance(60, 5.0)
        await hear(v, "pull", "raid")
        await v.clock.advance(60, 5.0)  # the game never reported it: it is forgotten
        await v.app.handle(Chat(v._ts(), "raid", HUMAN, HIS_NAME, 0, "pull", HIS_MAP, HIS_ZONE))
        assert lines(v)[-1]["thread_id"] is not None
    run(go())


def test_a_failed_command_leaves_no_echo_waiting(vapp):
    v = vapp

    async def go():
        Game(v, error=NotConnected("down"))
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        await hear(v, "pull", "raid")
        await v.app.handle(Chat(v._ts(), "raid", HUMAN, HIS_NAME, 0, "pull", HIS_MAP, HIS_ZONE))
        assert lines(v)[-1]["thread_id"] is not None
    run(go())


# -- a private room --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["stop", "hold", "follow me", "poll"])
def test_a_private_room_gives_the_game_no_command(vapp, text):
    """His words there are whispers between the two: a typed whisper is no command either."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 102, room="private1")
        await group(v, *RAID)
        await hear(v, text, "private1")
        (row,) = lines(v)
        assert (row["channel"], row["to_guid"], row["text"]) == ("whisper", 102, text)
        assert row["thread_id"] is not None  # talk to her, not an order
        assert not game.sent
    run(go())


@pytest.mark.parametrize("gone", [
    lambda rooms: rooms.move(102, "guild_hall"),  # dragged out during recognition
    lambda rooms: rooms.leave(102)])              # logged out then, or he was alone all along
@pytest.mark.parametrize("text", ["stop", "hold", "follow me", "poll"])
def test_no_private_room_ever_gives_the_game_a_command(vapp, gone, text):
    """Whoever is left in it when the words land: what he said there was said in private."""
    v = vapp

    async def go():
        game = Game(v)
        await on_air(v, 102, room="private1")
        await group(v, *RAID)
        await v.app.handle(DcStatus(v._ts(), 101, True, False, ""))
        gone(v.ext.rooms)
        await hear(v, text, "private1")
        (row,) = lines(v)
        assert (row["channel"], row["to_guid"], row["text"]) == ("voice:private1", 0, text)
        assert row["audience"] == "[]"
        assert not game.sent
    run(go())


# -- G5 and G6 ---------------------------------------------------------------------------------

def test_a_pushback_on_the_air_gets_her_plain_line(vapp):
    v = vapp

    async def go():
        await on_air(v, 101, 102)
        for g in (103, 104, 105):
            v.app.world.online.discard(g)
        talks(v, numbered("you are bankrupting the guild bank {n}"))
        await v.app.engine.speak(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        await v.clock.advance(15, 1.0)
        talks(v, "I meant the repair bill, my bad\nand another thing")
        await hear(v, "what are you talking about?")
        await v.clock.advance(45, 1.0)
        await v.app.engine._cancel_all()
    run(go())
    first = speaks(v)[1]
    assert (first.guid, first.text, first.channel) == (
        101, "I meant the repair bill, my bad", "guild_hall")
    assert v.rows("SELECT why FROM utterance WHERE text = ?",
                  (first.text,))[0]["why"] == "reply:pushback"


# -- failures and the log ----------------------------------------------------------------------

def test_a_fault_never_reaches_the_talk_loop(vapp, caplog):
    v = vapp

    async def go():
        await on_air(v, 102)

        def boom(*a, **k):
            raise RuntimeError(SECRET)
        v.app.bridge.inject = boom
        with caplog.at_level(logging.DEBUG):
            await v.ext.heard.on_heard(wire.Heard(SECRET, SECRET, 0.9, "guild_hall", 1, 1))
    run(go())
    assert "voice_handler_failed" in caplog.text and "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


def test_a_store_fault_after_the_row_never_reaches_the_talk_loop(vapp, caplog):
    v = vapp

    async def go():
        await on_air(v, 102)

        def boom(*a, **k):
            raise RuntimeError(SECRET)
        v.ext.store.add_heard = boom
        with caplog.at_level(logging.DEBUG):
            await hear(v, SECRET)
        assert len(lines(v)) == 1
    run(go())
    assert SECRET not in caplog.text


def test_no_spoken_word_reaches_the_log(vapp, caplog):
    """The whole path: a line, a reply, a command, a private line, a command that fails."""
    v = vapp

    async def go():
        Game(v, error=NotConnected("down"))
        await on_air(v, 101, 102, room="raid")
        await group(v, *RAID)
        talks(v)
        with caplog.at_level(logging.DEBUG):
            await hear(v, f"Belra, {SECRET}", "raid", raw=SECRET)
            await hear(v, "hold", "raid", raw=SECRET)
            await v.clock.advance(15, 1.0)
            v.ext.rooms.move(HUMAN, "private1")
            v.ext.rooms.move(102, "private1")
            await hear(v, SECRET, "private1", raw=SECRET)
            await v.clock.advance(15, 1.0)
            await v.app.engine._cancel_all()
    run(go())
    assert SECRET not in caplog.text


def test_voice_off_keeps_typed_chat(tmp_path, clock, voice_data):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_llm import FakeLLM
    (tmp_path / "mind.toml").write_text("", encoding="utf-8")
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
    assert app.voice.heard is None
    assert app.voice._on_chat not in app._handlers.get("chat", ())
    assert "dc_status" not in app._handlers
    assert app.voice.echoes_command(Chat(0, "raid", HUMAN, HIS_NAME, 0, "pull", 0, 0)) is False


# -- his key -----------------------------------------------------------------------------------

def reply_turn(speaker=102, room="guild_hall") -> Turn:
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel=f"voice:{room}",
                addressee=HUMAN)


async def press(v, down, room="guild_hall") -> None:
    await v.ext.on_message(wire.Ptt(down, room))


def test_ptt_marks_him_active(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        await v.clock.advance(50, 5.0)
        at = v.clock.now()
        await press(v, True)
        assert v.app.world.human_last_active == at
        await v.clock.advance(3, 1.0)
        await press(v, False)
        assert v.app.world.human_last_active == at + 3
    run(go())


def test_hold_while_he_talks(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await press(v, True)
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(5, 0.05)
        assert not model_calls(v) and not speaks(v)
        await press(v, False)
        await v.clock.advance(0.25, 0.05)
        assert not model_calls(v)  # the grace: he may only be drawing breath
        await v.clock.advance(0.15, 0.05)
        assert model_calls(v)
        await v.clock.advance(2, 0.5)
        assert task.done() and task.result() == ["on it"]
        assert [s.priority for s in speaks(v)] == ["p3"]
    run(go())


def test_a_press_in_the_grace_keeps_holding(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await press(v, True)
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(1, 0.05)
        await press(v, False)
        await v.clock.advance(0.1, 0.05)
        await press(v, True)
        await v.clock.advance(3, 0.05)
        assert not model_calls(v)
        await press(v, False)
        await v.clock.advance(1, 0.05)
        assert model_calls(v)
        await v.clock.advance(2, 0.5)
        await task
    run(go())


def test_the_grace_is_bounded_by_the_turns_deadline(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await press(v, True)
        turn = reply_turn()
        turn.deadline = v.clock.now() + 2
        task = asyncio.create_task(v.app.engine.speak(turn))
        await v.clock.advance(1.9, 0.05)
        await press(v, False)
        await v.clock.advance(1, 0.05)
        assert task.done() and task.result() == [] and not model_calls(v)
    run(go())
    assert v.rows("SELECT dropped FROM utterance")[-1]["dropped"] == "expired"


def test_still_down_past_the_deadline_is_expired(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await press(v, True)
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(35, 0.5)
        assert task.done() and task.result() == [] and not model_calls(v)
    run(go())
    assert v.rows("SELECT dropped FROM utterance")[-1]["dropped"] == "expired"


def test_hold_spares_calls(vapp):
    v = vapp

    async def go():
        await on_air(v, 101, 102, 103, 104, room="raid")
        await group(v, *RAID)
        talks(v, numbered("line {n}"))
        await press(v, True, "raid")
        urgent = asyncio.create_task(v.app.engine.speak(
            Turn(kind=TurnKind.URGENT, speaker=101, channel="raid")))
        await v.clock.advance(1, 0.05)
        assert urgent.done()
        assert [s.priority for s in speaks(v)] == ["p1"]
        assert await v.app.engine.say_as(102, "raid", "Pulling in three.",
                                         kind=TurnKind.CALLOUT) is True
        assert speaks(v)[-1].priority == "p0"
        cheer = asyncio.create_task(v.app.engine.speak(
            Turn(kind=TurnKind.GZ, speaker=103, channel="raid", template="gz")))
        await v.clock.advance(1, 0.05)
        assert cheer.done() and speaks(v)[-1].template is True
        count = len(speaks(v))
        calls = len(model_calls(v))
        banter = asyncio.create_task(v.app.engine.speak(
            Turn(kind=TurnKind.OPEN, speaker=104, channel="voice:raid")))
        await v.clock.advance(3, 0.05)
        assert not banter.done() and len(speaks(v)) == count and len(model_calls(v)) == calls
        await press(v, False, "raid")
        await v.clock.advance(1, 0.05)
        assert len(model_calls(v)) == calls + 1
        await v.clock.advance(3, 0.5)
        await banter
    run(go())


def test_ptt_cleared_when_the_service_drops(vapp):
    """NP5: a press the dead service can't end holds nothing after the reconnect."""
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v, numbered("line {n}"))
        await press(v, True)
        assert v.ext.comms.ptt_down
        await v.ext.on_connection(False)
        assert not v.ext.comms.ptt_down
        await v.ext.on_connection(True)
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(0.1, 0.05)
        assert model_calls(v)
        await v.clock.advance(3, 0.5)
        await task
        await press(v, True)
        v.ext.snapshot()  # one sent mid-press clears it too
        assert not v.ext.comms.ptt_down
    run(go())


def test_a_snapshot_ends_the_grace(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await press(v, True)
        await press(v, False)
        v.ext.snapshot()
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(0.05, 0.05)
        assert model_calls(v)
        await v.clock.advance(3, 0.5)
        await task
    run(go())


def test_a_press_off_comms_holds_nobody(vapp):
    """Task 11's carry: he is at the keyboard, but nobody is listening to him."""
    v = vapp

    async def go():
        await on_air(v, 102)
        talks(v)
        await v.clock.advance(5, 1.0)
        at = v.clock.now()
        await press(v, True, "")
        assert not v.ext.comms.ptt_down
        assert v.app.world.human_last_active == at
        task = asyncio.create_task(v.app.engine.speak(reply_turn()))
        await v.clock.advance(0.1, 0.05)
        assert model_calls(v)
        await press(v, False, "")
        await v.clock.advance(3, 0.5)
        await task
    run(go())


# -- moving rooms ------------------------------------------------------------------------------

def test_join(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        await v.ext.on_message(wire.Join("officers"))
        assert v.sent == [wire.Moved(HUMAN, "officers", HUMAN)]
        assert v.ext.rooms.human_room() == "officers"
        v.sent.clear()
        v.ext.rooms.lock("private2", {103, 104})
        await v.ext.on_message(wire.Join("private2"))
        assert v.sent == [wire.Refused("private2", "locked")]
        assert v.ext.rooms.human_room() == "officers"
    run(go())


def test_join_logs_the_stretch(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        v.ext.store.enter(HUMAN, "guild_hall", 0, v.clock.now(), v.app.session_id)
        await v.clock.advance(5, 1.0)
        await v.ext.on_message(wire.Join("kitchen"))
        rows = v.rows("SELECT room, by, until FROM voice_presence WHERE guid = ? ORDER BY id",
                      (HUMAN,))
        assert rows[-2]["until"] == v.clock.now() and rows[-2]["room"] == "guild_hall"
        assert (rows[-1]["room"], rows[-1]["by"], rows[-1]["until"]) == ("kitchen", HUMAN, None)
    run(go())


def test_join_where_he_already_sits_moves_nothing(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        before = v.rows("SELECT COUNT(*) AS n FROM voice_presence")[0]["n"]
        await v.ext.on_message(wire.Join("guild_hall"))
        assert v.sent == [wire.Moved(HUMAN, "guild_hall", 0)]
        assert v.rows("SELECT COUNT(*) AS n FROM voice_presence")[0]["n"] == before
    run(go())


@pytest.mark.parametrize("room, reason", [("nowhere", "unknown"), ("private1", "full")])
def test_join_refused(vapp, room, reason):
    v = vapp

    async def go():
        await on_air(v, 102)
        for g in (103, 104, 105):
            v.ext.rooms.move(g, "private1")
        await v.ext.on_message(wire.Join(room))
        assert v.sent == [wire.Refused(room, reason)]
        assert v.ext.rooms.human_room() == "guild_hall"
    run(go())


def test_join_while_offline_is_refused(vapp):
    v = vapp

    async def go():
        await v.ext.on_message(wire.Join("kitchen"))
        assert v.sent == [wire.Refused("kitchen", "offline")]
        assert v.ext.rooms.human_room() is None
    run(go())


def test_leaving_a_private_room_unlocks_it(vapp):
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        v.ext.rooms.lock("private1", {102, HUMAN})
        await v.ext.on_message(wire.Join("guild_hall"))
        assert v.ext.rooms.allowed("private1") is None
        assert v.sent[0] == wire.Moved(HUMAN, "guild_hall", HUMAN)
        (tree,) = [m for m in v.sent if isinstance(m, wire.Channels)]
        assert next(r for r in tree.channels if r.id == "private1").locked is False
    run(go())


def test_leaving_an_open_room_sends_no_tree(vapp):
    v = vapp

    async def go():
        await on_air(v, 102, room="private1")
        await v.ext.on_message(wire.Join("guild_hall"))
        assert v.types() == ["moved"]
    run(go())


def test_a_private_room_locked_to_him_lets_him_in(vapp):
    v = vapp

    async def go():
        await on_air(v, 102)
        v.ext.rooms.lock("private1", {102, HUMAN})
        await v.ext.on_message(wire.Join("private1"))
        assert v.sent == [wire.Moved(HUMAN, "private1", HUMAN)]
    run(go())


def test_a_fault_in_join_never_raises(vapp, caplog):
    v = vapp

    async def go():
        await on_air(v, 102)

        def boom(*a, **k):
            raise RuntimeError(SECRET)
        v.ext.store.enter = boom
        with caplog.at_level(logging.DEBUG):
            await v.ext.heard.on_join(wire.Join("kitchen"))
    run(go())
    assert "voice_handler_failed" in caplog.text and SECRET not in caplog.text
