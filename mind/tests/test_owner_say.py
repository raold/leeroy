"""The owner's own line from the console (leeroy_mind.owner_say): the native owner_say command as
the server reads it (the shared fixtures under modules/mod-leeroy/tests/fixtures), the text rule
both sides apply, the say op's strict arguments, and OwnerSpeech's checks and words.

A fake app: the real WorldView in a SimpleNamespace with a session, a stopping flag and a bridge
stub that records commands and answers with a scripted Ack. Invented names only."""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind import endpoint, protocol
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import AckTimeout, ConnectionLost, NotConnected
from leeroy_mind.config import load_config
from leeroy_mind.endpoint import KEY_FILE, BadRequest, read_key_file, say_args
from leeroy_mind.owner_say import OwnerSay, OwnerSpeech, text_verdict
from leeroy_mind.protocol import (MAX_TEXT_BYTES, Ack, Group, Member, clean_say_text,
                                  encode_command, sanitize_say_text)
from leeroy_mind.talk.world import WorldView
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from test_app_lifecycle import CAST, LIVE, App, fake_clock, hello, presence, reply, seed_db, toml

FIXTURES = Path(__file__).resolve().parents[2] / "modules/mod-leeroy/tests/fixtures"
HUMAN, BRISA, CORWEN, DALLA = 1, 1101, 1102, 1103
TOO_LONG = "That's too long for WoW chat (255 bytes at most)."
NOT_CAST = "That member isn't in the guild's cast."
GUILD_SHAPE = "A guild line takes exactly channel and text."


def rows(name: str) -> list[dict]:
    return [json.loads(text) for text in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
            if text.strip()]


def member(guid, name, *, level=80, online=True):
    return Member(guid=guid, name=name, race=2, class_=1, gender=0, level=level, rank=3,
                  online=online, map=571, zone=4395, spec_tab=0)


class StubBridge:
    def __init__(self):
        self.sent, self.error, self.raises, self.greeted = [], "", None, True

    async def send(self, cmd):
        self.sent.append(cmd)
        if self.raises is not None:
            raise self.raises
        return Ack(ts=0, id="m1", ok=not self.error, error=self.error)


def fake_app():
    world = WorldView(is_officer=lambda guid: False)
    world.human_guid = HUMAN
    world.members = {m.guid: m for m in (
        member(HUMAN, "Hrodric"), member(BRISA, "Brisa"), member(CORWEN, "Corwen", online=False),
        member(DALLA, "Dalla", level=70))}
    world.online = {BRISA}
    world.human_online = True
    return SimpleNamespace(world=world, session_id=7, human_guid=HUMAN, stopping=False,
                           bridge=StubBridge())


def say(app, **args):
    return run(OwnerSpeech(app).handle({"text": "hi", **args}))


def refused(words):
    return {"ok": False, "error": words}


# -- the wire and the text -----------------------------------------------------------------------

def test_owner_say_encodes_as_the_server_reads_it():
    OwnerSpeech(fake_app())  # building it registers owner_say, as the app does
    valid = [row["msg"] for row in rows("owner_say_protocol.jsonl") if row["valid"]]
    assert len(valid) == 5
    for msg in valid:
        cmd = OwnerSay(id=msg["id"], channel=msg["channel"], text=msg["text"],
                       to_guid=msg["to_guid"])
        assert json.loads(encode_command(cmd, 0)) == msg


def test_say_text_rules():
    table = rows("owner_say_text.jsonl")
    assert len(table) == 17
    for row in table:  # a refused row leaves sent out: it expects ""
        assert text_verdict(row["text"]) == (row["verdict"], row.get("sent", "")), row["name"]
    # Only the mind's JSON parser lets a lone surrogate through (nlohmann refuses the line)
    assert text_verdict("\ud800") == ("bad_text", "")
    assert text_verdict("omw \udc00 5 min") == ("bad_text", "")
    # The clean is today's sanitize without its cut; the sanitize is it plus the cut
    assert clean_say_text(" a" * 200) == " ".join("a" * 200)
    assert sanitize_say_text(" a" * 200) == clean_say_text(" a" * 200)[:MAX_TEXT_BYTES].rstrip()


def test_say_args_are_exact():
    assert say_args({"channel": "guild", "text": "  omw,   5 min "}) == {
        "channel": "guild", "text": "omw, 5 min"}
    assert say_args({"channel": "officer", "text": "hi"}) == {"channel": "officer", "text": "hi"}
    assert say_args({"channel": "group", "text": "pull in 10"}) == {
        "channel": "group", "text": "pull in 10"}
    assert say_args({"channel": "whisper", "text": "hi", "to": BRISA}) == {
        "channel": "whisper", "text": "hi", "to": BRISA}
    assert say_args({"channel": "guild", "text": "a" + " " * 1023}) == {
        "channel": "guild", "text": "a"}  # 1024 characters is the most it reads
    # Every to_guid the server refuses, the op refuses before it is sent
    for row in rows("owner_say_protocol.jsonl"):
        if row.get("error") == "bad field: to_guid":
            msg = row["msg"]
            args = {"channel": msg["channel"], "text": msg["text"]}
            if "to_guid" in msg:
                args["to"] = msg["to_guid"]
            with pytest.raises(BadRequest):
                say_args(args)


@pytest.mark.parametrize("args, words", [
    ({"channel": "whisper", "text": "hi"}, "A whisper takes exactly channel, text and to."),
    ({"channel": "guild", "text": "hi", "to": BRISA}, GUILD_SHAPE),
    ({"channel": "group", "text": "hi", "to": 0}, "A group line takes exactly channel and text."),
    ({"channel": "guild", "text": "hi", "as": "x"}, GUILD_SHAPE),
    ({"channel": "guild"}, GUILD_SHAPE),
    ({"channel": "say", "text": "hi"}, "A line goes to guild, officer, group or whisper."),
    ({"channel": "party", "text": "hi"}, "A line goes to guild, officer, group or whisper."),
    ({"text": "hi"}, "A line goes to guild, officer, group or whisper."),
    ("hi", "A line goes to guild, officer, group or whisper."),
    ({"channel": "guild", "text": 5}, "A line is text."),
    ({"channel": "guild", "text": None}, "A line is text."),
    ({"channel": "guild", "text": " " * 1024 + "a"}, TOO_LONG),
    ({"channel": "guild", "text": "a" * 1025}, TOO_LONG),
    ({"channel": "guild", "text": "a" * 256}, TOO_LONG),
    ({"channel": "guild", "text": " | \n "}, "There's nothing to send."),
    ({"channel": "guild", "text": "  .gm on"}, "Commands can't be sent from the console."),
    ({"channel": "guild", "text": "hi \ud800"}, "That line has a character WoW chat can't carry."),
    ({"channel": "whisper", "text": "hi", "to": True}, "A whisper goes to a member's guid."),
    ({"channel": "whisper", "text": "hi", "to": "1101"}, "A whisper goes to a member's guid."),
    ({"channel": "whisper", "text": "hi", "to": 1 << 32}, "A whisper goes to a member's guid."),
])
def test_say_args_refuse_everything_else_in_words(args, words):
    with pytest.raises(BadRequest) as e:
        say_args(args)
    assert str(e.value) == words


# -- OwnerSpeech ---------------------------------------------------------------------------------

def test_a_line_goes_to_the_game_as_his():
    app = fake_app()
    assert say(app, channel="guild") == {"ok": True, "message": "sent"}
    assert say(app, channel="officer", text="who has the summon") == {"ok": True, "message": "sent"}
    assert app.bridge.sent == [OwnerSay(id="", channel="guild", text="hi", to_guid=0),
                               OwnerSay(id="", channel="officer", text="who has the summon")]


def test_group_is_raid_or_party_and_needs_one():
    app = fake_app()
    assert say(app, channel="group") == refused("You're not in a group.")
    app.world.group = Group(ts=0, leader_guid=BRISA, raid=False, members=(CORWEN,))  # not his
    assert say(app, channel="group") == refused("You're not in a group.")
    assert app.bridge.sent == []
    app.world.group = Group(ts=0, leader_guid=HUMAN, raid=False, members=(BRISA,))
    assert say(app, channel="group")["ok"] is True
    app.world.group = Group(ts=0, leader_guid=BRISA, raid=True, members=(HUMAN, CORWEN))
    assert say(app, channel="group")["ok"] is True
    assert [c.channel for c in app.bridge.sent] == ["party", "raid"]


def test_a_whisper_goes_only_to_an_online_cast_member():
    app = fake_app()
    assert say(app, channel="whisper", to=HUMAN) == refused("You can't whisper yourself.")
    assert say(app, channel="whisper", to=4242) == refused(NOT_CAST)
    assert say(app, channel="whisper", to=DALLA) == refused(NOT_CAST)
    assert say(app, channel="whisper", to=CORWEN) == refused("Corwen is offline.")
    assert app.bridge.sent == []
    assert say(app, channel="whisper", to=BRISA) == {"ok": True, "message": "sent"}
    assert app.bridge.sent == [OwnerSay(id="", channel="whisper", text="hi", to_guid=BRISA)]


def test_not_in_game_is_refused_before_the_bridge():
    app = fake_app()
    app.world.human_online = False
    assert say(app, channel="guild") == refused("You're not in game.")
    app.world.human_online, app.session_id = True, None
    assert say(app, channel="whisper", to=BRISA) == refused("You're not in game.")
    app.session_id, app.stopping = 7, True
    assert say(app, channel="guild") == refused("The mind is stopping.")
    assert app.bridge.sent == []


def test_a_bridge_that_is_down_is_named_never_his_absence():
    """The app marks him offline when the bridge drops: nobody can tell, so it says the bridge."""
    app = fake_app()
    app.bridge.greeted, app.world.human_online = False, False
    assert say(app, channel="guild") == refused("The game bridge is down.")
    assert app.bridge.sent == []
    app.bridge.greeted, app.world.human_online = True, True
    for raised, words in (
            (NotConnected("x"), "The game bridge is down."),
            (AckTimeout("x"), "The game didn't answer in time. Check WoW before sending it again."),
            (ConnectionLost("x"), "The game bridge went down before the game answered. Check WoW "
                                  "before sending it again.")):
        app.bridge.raises = raised
        assert say(app, channel="guild") == refused(words)


@pytest.mark.parametrize("code, words", [
    ("human_offline", "You're not in the world right now (loading or offline)."),
    ("not_in_guild", "You're not in the guild."),
    ("not_in_group", "You're not in a group."),
    ("not_in_raid", "You're not in a raid."),
    ("not_cast", "That member isn't in the guild's cast."),
    ("target_offline", "Brisa is offline."),
    ("self", "You can't whisper yourself."),
    ("rate_limited", "Too fast: wait a moment before the next line."),
    ("too_long", "That's too long for WoW chat (255 bytes at most)."),
    ("empty_text", "There's nothing to send."),
    ("command_prefix", "Commands can't be sent from the console."),
    ("disabled", "mod-leeroy is off on the game server."),
    ("busy", "The game refused it (busy)."),
    ("bad field: to_guid", "The game refused it (bad field: to_guid)."),
    ("x" * 100, "The game refused it (" + "x" * 60 + ")."),
])
def test_native_refusals_come_back_in_words(code, words):
    app = fake_app()
    app.bridge.error = code
    assert say(app, channel="whisper", to=BRISA) == refused(words)
    assert len(app.bridge.sent) == 1


def test_an_older_server_without_owner_say_is_named():
    """Console and mind released before the worldserver rebuild: the old server answers an
    error for a type it does not know, which the bridge turns into a failed ack."""
    app = fake_app()
    app.bridge.error = "unknown type: owner_say"
    assert say(app, channel="guild") == refused(
        "The game server needs the rebuild that adds console replies.")


class OldServer:
    """The command types a worldserver from before owner_say knows."""
    def __contains__(self, type_name):
        return type_name != "owner_say" and type_name in protocol._commands


@pytest.mark.parametrize("server", ["rebuilt", "older"])
def test_the_running_mind_sends_it_as_the_console_asks(tmp_path, server):
    """The real app over FakeBridge, asked through its own control endpoint: the line goes out
    as owner_say, cleaned; a server from before the rebuild answers an error the bridge turns
    into a failed ack, and that reads as the rebuild."""
    async def go():
        clock = fake_clock()
        bridge = FakeBridge([hello(0), CAST, presence("login")],
                            known_types=OldServer() if server == "older" else None)
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path,
                      llm=FakeLLM(reply, models={LIVE}), clock=clock, rng_seed=1)
        task = asyncio.create_task(app.run())
        try:
            assert isinstance(app.speech, OwnerSpeech)
            await App(app, bridge, None, clock).wait(
                lambda: app.session_id is not None and app.world.human_online)
            await asyncio.wait_for(app.control.listening.wait(), 5)
            record = read_key_file(tmp_path / KEY_FILE)
            answer = await asyncio.to_thread(endpoint.request, record["port"], record["key"],
                                             "say", {"channel": "guild", "text": " omw,  5 min "})
            return answer, [c for c in bridge.commands if c.get("type") == "owner_say"]
        finally:
            await app.stop()
            await asyncio.wait({task}, timeout=5)
            await bridge.stop()

    answer, lines = run(go())
    assert [{k: v for k, v in c.items() if k not in ("id", "ts")} for c in lines] == [
        {"v": 1, "type": "owner_say", "channel": "guild", "text": "omw, 5 min", "to_guid": 0}]
    assert answer == ({"ok": True, "message": "sent"} if server == "rebuilt" else refused(
        "The game server needs the rebuild that adds console replies."))


def test_the_log_never_carries_the_text_or_a_name(caplog):
    app = fake_app()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert say(app, channel="whisper", to=BRISA, text="meet me by the anvil")["ok"] is True
        assert say(app, channel="whisper", to=CORWEN, text="meet me by the anvil")["ok"] is False
        app.bridge.error = "target_offline"
        assert say(app, channel="whisper", to=BRISA, text="meet me by the anvil")["ok"] is False
        app.bridge.error = "unknown type: owner_say"
        assert say(app, channel="guild", text="meet me by the anvil")["ok"] is False
        app.bridge.error = "Brisa refused meet me"  # whatever an unknown error carries
        assert say(app, channel="guild", text="meet me by the anvil")["ok"] is False
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("owner_say")]
    assert lines == ["owner_say: channel=whisper outcome=sent",
                     "owner_say: channel=whisper outcome=target_offline",
                     "owner_say: channel=whisper outcome=target_offline",
                     "owner_say: channel=guild outcome=unknown_type",
                     "owner_say: channel=guild outcome=refused"]
    for private in ("anvil", "Brisa", "Corwen", "Hrodric"):
        assert private not in caplog.text
