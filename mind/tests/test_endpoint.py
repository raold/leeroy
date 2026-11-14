"""The mind's control endpoint (leeroy_mind.endpoint): its config, its private key file, the
strict request format and every refusal, the raid actions handed to guild life one at a time,
and the clean stop, which must leave the mind's state as Ctrl+C leaves it.

A real endpoint on 127.0.0.1:0 with a fake app, and, for the stop, the real app over FakeBridge
with a FakeLLM and a FakeClock. Scratch data dirs only."""
import asyncio
import contextlib
import json
import logging
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind import endpoint
from leeroy_mind.app import MindApp
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.endpoint import (KEY_FILE, MAX_CONNECTIONS, MAX_LINE, BadRequest, ControlCfg,
                                  ControlEndpoint, Forbidden, attach_control, load_control_config,
                                  parse_request, raid_args, read_key_file, write_key_file)
from leeroy_mind.guild.raid_night import ConsoleRefused
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from test_app_lifecycle import (CAST, LIVE, App, chat, fake_clock, hello, presence, reopen, reply,
                                seed_db, toml)

KEY = "K" * 43  # the shape of secrets.token_urlsafe(32)


def line(**body) -> bytes:
    return (json.dumps(body) + "\n").encode()


# -- [control] in mind.toml ----------------------------------------------------------------------

def test_the_endpoint_is_on_by_default_on_any_free_port(tmp_path, caplog):
    assert load_control_config(tmp_path / "missing.toml") == ControlCfg(True, 0)
    path = tmp_path / "mind.toml"
    path.write_text("[bridge]\nport = 7878\n", encoding="utf-8")
    assert load_control_config(path) == ControlCfg(True, 0)
    path.write_text("[control]\nenabled = false\nport = 17999\nother = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        assert load_control_config(path) == ControlCfg(False, 17999)
        load_config(path)
    assert "unknown key control.other" in caplog.text
    assert "unknown section [control]" not in caplog.text  # config.py leaves it to endpoint.py
    for bad in ('enabled = "yes"', "port = -1", "port = 70000", "port = true", "port = 1.5"):
        path.write_text(f"[control]\n{bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="control"):
            load_control_config(path)


def test_turned_off_it_registers_nothing(tmp_path, caplog):
    (tmp_path / "mind.toml").write_text("[control]\nenabled = false\n", encoding="utf-8")
    registered = []
    app = type("App", (), {"data_dir": tmp_path, "register_background": registered.append})()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert attach_control(app) is None
    assert registered == [] and "control_off" in caplog.text
    (tmp_path / "mind.toml").write_text("[control]\nport = 'x'\n", encoding="utf-8")
    ep = attach_control(app)  # a bad table: the defaults, and a warning
    assert ep is not None and ep.cfg == ControlCfg() and registered == [ep.serve]


# -- the key file --------------------------------------------------------------------------------

def _dacl(path: Path) -> str:
    """The file's DACL as SDDL (Windows)."""
    import ctypes
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, ctypes.c_int, w.DWORD,
                                             *([ctypes.POINTER(ctypes.c_void_p)] * 5)]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.ULONG)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    DACL = 4
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    assert advapi.GetNamedSecurityInfoW(str(path), 1, DACL, None, None, ctypes.byref(dacl), None,
                                        ctypes.byref(sd)) == 0
    text = w.LPWSTR()
    try:
        assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd, 1, DACL, ctypes.byref(text), None)
        return text.value
    finally:
        kernel.LocalFree(text)
        kernel.LocalFree(sd)


def _private(path: Path) -> bool:
    if os.name == "nt":
        return _dacl(path) == endpoint.private_sddl()
    return stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_key_file_is_readable_by_this_user_only_and_replaces_a_leftover(tmp_path):
    path = tmp_path / KEY_FILE
    path.write_text('{"v":1,"pid":1,"port":1,"key":"old"}', encoding="utf-8")  # a crash's
    assert not _private(path)
    record = {"v": 1, "pid": os.getpid(), "port": 40000, "key": KEY}
    write_key_file(path, record)
    assert read_key_file(path) == {"pid": os.getpid(), "port": 40000, "key": KEY}
    assert _private(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [KEY_FILE]  # no temporary left
    if os.name == "nt":
        sddl = _dacl(path)
        assert sddl.startswith("D:P(") and sddl.count("(") == 1  # protected, one entry: him


def test_a_key_file_not_exactly_the_endpoints_reads_as_none(tmp_path):
    path = tmp_path / KEY_FILE
    good = {"v": 1, "pid": 4242, "port": 40000, "key": KEY}
    assert read_key_file(path) is None  # missing
    for bad in ({**good, "extra": 1}, {k: v for k, v in good.items() if k != "key"},
                {**good, "v": 2}, {**good, "v": True}, {**good, "pid": 0}, {**good, "pid": "4242"},
                {**good, "port": 0}, {**good, "port": 65536}, {**good, "port": True},
                {**good, "key": "short"}, {**good, "key": "K" * 42 + "!"}, [good]):
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert read_key_file(path) is None, bad
    path.write_bytes(b"\xff\xfe")
    assert read_key_file(path) is None
    path.write_text(json.dumps(good) + " " * 2000, encoding="utf-8")
    assert read_key_file(path) is None  # too long to be one
    path.write_text(json.dumps(good), encoding="utf-8")
    assert read_key_file(path) == {"pid": 4242, "port": 40000, "key": KEY}


# -- the request ---------------------------------------------------------------------------------

def test_the_three_ops_in_their_one_strict_form():
    assert parse_request(line(v=1, key=KEY, op="status"), KEY) == ("status", None)
    assert parse_request(line(v=1, key=KEY, op="stop"), KEY) == ("stop", None)
    assert parse_request(line(v=1, key=KEY, op="raid", args={"action": "end"}), KEY) == (
        "raid", {"action": "end"})


def test_the_say_op_in_its_one_strict_form():
    assert parse_request(line(v=1, key=KEY, op="say", args={"channel": "guild", "text": " omw "}),
                         KEY) == ("say", {"channel": "guild", "text": "omw"})
    assert parse_request(line(v=1, key=KEY, op="say",
                              args={"channel": "whisper", "text": "hi", "to": 1101}), KEY) == (
        "say", {"channel": "whisper", "text": "hi", "to": 1101})
    with pytest.raises(BadRequest, match="say needs args"):
        parse_request(line(v=1, key=KEY, op="say"), KEY)
    with pytest.raises(BadRequest, match="^There's nothing to send.$"):  # say_args' own words
        parse_request(line(v=1, key=KEY, op="say", args={"channel": "guild", "text": "  "}), KEY)
    with pytest.raises(BadRequest, match="^A whisper takes exactly channel, text and to.$"):
        parse_request(line(v=1, key=KEY, op="say", args={"channel": "whisper", "text": "hi"}), KEY)


def test_the_board_op_in_its_one_strict_form():
    assert endpoint.BOARD_ACTIONS == ("reply", "withdraw")
    assert endpoint.BOARD_SHAPES == {"reply": {"action", "kind", "to", "text"},
                                     "withdraw": {"action", "id"}}
    assert "board" in endpoint.OPS
    assert parse_request(line(v=1, key=KEY, op="board", args={
        "action": "reply", "kind": "post", "to": 7, "text": "  see  you "}), KEY) == (
        "board", {"action": "reply", "kind": "post", "to": 7, "text": "see you"})
    assert parse_request(line(v=1, key=KEY, op="board", args={"action": "withdraw", "id": 3}),
                         KEY) == ("board", {"action": "withdraw", "id": 3})
    with pytest.raises(BadRequest, match="board needs args"):
        parse_request(line(v=1, key=KEY, op="board"), KEY)
    with pytest.raises(BadRequest, match="^There's nothing to send.$"):  # board_args' own words
        parse_request(line(v=1, key=KEY, op="board", args={
            "action": "reply", "kind": "letter", "to": 7, "text": " | "}), KEY)
    with pytest.raises(BadRequest):
        parse_request(line(v=1, key=KEY, op="board", args={"action": "withdraw", "id": "3"}), KEY)


@pytest.mark.parametrize("raw, words", [
    (b'{"v":1,"key":"%s","op":"status"}' % KEY.encode(), "bad request"),  # no newline
    (b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", "not a JSON line"),
    (b'["status"]\n', "exactly v, key, op"),
    (b'{"key":"%s","op":"status"}\n' % KEY.encode(), "exactly v, key, op"),
    (b'{"v":1,"key":"%s","op":"status","as":"x"}\n' % KEY.encode(), "exactly v, key, op"),
    (b'{"v":1,"op":"status"}\n', "exactly v, key, op"),
    (b'{"v":NaN,"key":"%s","op":"status"}\n' % KEY.encode(), "not a JSON line"),
    (b'\xff{"v":1}\n', "not a JSON line"),
    (b'{"v":2,"key":"%s","op":"status"}\n' % KEY.encode(), "v is 1"),
    (b'{"v":true,"key":"%s","op":"status"}\n' % KEY.encode(), "v is 1"),
    (b'{"v":"1","key":"%s","op":"status"}\n' % KEY.encode(), "v is 1"),
    (b'{"v":1,"key":"%s","op":"restart"}\n' % KEY.encode(), "op is one of"),
    (b'{"v":1,"key":"%s","op":"stop","args":{}}\n' % KEY.encode(), "takes no args"),
    (b'{"v":1,"key":"%s","op":"raid"}\n' % KEY.encode(), "raid needs args"),
    (b"{" + b" " * MAX_LINE + b"}\n", "bad request"),
])
def test_anything_else_is_a_bad_request(raw, words):
    with pytest.raises(BadRequest, match=words) as refused:
        parse_request(raw, KEY)
    assert not isinstance(refused.value, Forbidden)


def test_the_shape_refusal_names_every_op_that_takes_args():
    takes = []
    for op in endpoint.OPS:
        try:
            parse_request(line(v=1, key=KEY, op=op), KEY)
        except BadRequest as e:
            assert str(e) == f"bad request: {op} needs args"
            takes.append(op)
    assert "library" in takes
    with pytest.raises(BadRequest) as refused:
        parse_request(line(key=KEY, op="status"), KEY)
    assert all(op in str(refused.value) for op in takes), (takes, str(refused.value))


@pytest.mark.parametrize("key", ["", "k" * 43, KEY[:-1], KEY + "K", 42, None, ["K"]])
def test_a_wrong_key_is_forbidden_before_anything_else_is_said(key):
    with pytest.raises(Forbidden):
        parse_request(line(v=99, key=key, op="nothing", args=1), KEY)


FORM = {"action": "form", "raid": "icc", "size": 25, "heroic": True, "lead": None}


def test_raid_args_are_exact():
    for action in ("call", "summon", "start", "stop", "end"):
        assert raid_args({"action": action}) == {"action": action}
    assert raid_args(FORM) == FORM
    for lead in ("me", "rotate", 108):
        assert raid_args({**FORM, "lead": lead})["lead"] == lead
    assert raid_args({**FORM, "size": None, "heroic": False, "raid": "naxx"})["size"] is None


@pytest.mark.parametrize("args, words", [
    ({"action": "disband"}, "an action, one of"),
    ({}, "an action, one of"),
    ("end", "an action, one of"),
    ({"action": "end", "now": True}, "takes no arguments"),
    ({"action": "form", "raid": "icc"}, "exactly raid, size, heroic and lead"),
    ({**FORM, "extra": 1}, "exactly raid, size, heroic and lead"),
    ({**FORM, "raid": "ICC"}, "raid is one of"),
    ({**FORM, "raid": "icc25"}, "raid is one of"),
    ({**FORM, "size": 40}, "size is null, 10 or 25"),
    ({**FORM, "size": "25"}, "size is null, 10 or 25"),
    ({**FORM, "size": True}, "size is null, 10 or 25"),
    ({**FORM, "heroic": 1}, "heroic is true or false"),
    ({**FORM, "raid": "naxx"}, "Naxxramas has no heroic mode"),
    ({**FORM, "lead": "Aster"}, "lead is null"),
    ({**FORM, "lead": True}, "lead is null"),
    ({**FORM, "lead": 0}, "lead is null"),
    ({**FORM, "lead": 1 << 32}, "lead is null"),
])
def test_raid_args_refuse_everything_else(args, words):
    with pytest.raises(BadRequest, match=words):
        raid_args(args)


# -- the server ----------------------------------------------------------------------------------

class FakeGuild:
    def __init__(self):
        self.actions, self.refuse, self.gate = [], None, None

    def console_status(self):
        return {"state": "waiting_call", "actions": {"call": None}}

    async def console_action(self, args):
        self.actions.append(args)
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse:
            raise ConsoleRefused(self.refuse)
        return f"did {args['action']}"


class FakeSpeech:
    def __init__(self):
        self.lines, self.gate = [], None

    async def handle(self, args):
        self.lines.append(args)
        if self.gate is not None:
            await self.gate.wait()
        return {"ok": True, "message": "sent"}


class FakeBoard:
    def __init__(self):
        self.asks, self.gate = [], None

    async def handle(self, args):
        self.asks.append(args)
        if self.gate is not None:
            await self.gate.wait()
        return {"ok": True, "message": "Saved. The guild answers tonight.", "id": len(self.asks)}


class FakeDiscord:
    def __init__(self):
        self.asks, self.gate = [], None

    async def handle(self, args):
        self.asks.append(args)
        if self.gate is not None:
            await self.gate.wait()
        return {"ok": True, "id": len(self.asks)}


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stopping = False
        self.stops = 0
        self.guild = FakeGuild()
        self.speech = FakeSpeech()
        self.board = FakeBoard()
        self.discord = FakeDiscord()

    def request_stop(self):
        self.stops += 1
        self.stopping = True


@contextlib.asynccontextmanager
async def served(tmp_path, **kw):
    app = FakeApp(tmp_path)
    ep = ControlEndpoint(app, ControlCfg(), key=KEY, **kw)
    task = asyncio.create_task(ep.serve())
    await asyncio.wait_for(ep.listening.wait(), 5)
    try:
        yield app, ep
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(ep, op, args=None, key=KEY):
    return await asyncio.to_thread(endpoint.request, ep.port, key, op, args, timeout=5)


async def raw(ep, data: bytes, *, close=False) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", ep.port)
    writer.write(data)
    await writer.drain()
    if close:
        writer.write_eof()
    out = await asyncio.wait_for(reader.read(), 5)  # the endpoint closes after its answer
    writer.close()
    return out


def test_it_listens_on_loopback_only_and_its_key_file_names_it(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            record = read_key_file(tmp_path / KEY_FILE)
            assert record == {"pid": os.getpid(), "port": ep.port, "key": KEY}
            assert _private(tmp_path / KEY_FILE)
            with socket.create_connection(("127.0.0.1", ep.port)) as conn:
                assert conn.getpeername()[0] == "127.0.0.1"
            hosts = [ip for ip in _own_addresses() if not ip.startswith("127.")]
            for ip in hosts[:1]:  # another of this machine's addresses: nothing there
                with pytest.raises(OSError):
                    socket.create_connection((ip, ep.port), timeout=1).close()
            port = ep.port
        assert not (tmp_path / KEY_FILE).exists()  # closed: removed
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    run(go())


def _own_addresses() -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None,
                                                                  socket.AF_INET)})
    except OSError:
        return []


def test_status_answers_and_a_wrong_key_is_forbidden_without_logging_it(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            reply = await ask(ep, "status")
            assert reply == {"ok": True, "v": 1, "pid": os.getpid(), "stopping": False,
                             "raid": FakeGuild().console_status(), "bridge_connected": None,
                             "cast": None, "schema_version": None,  # this app cannot tell
                             "world": None, "typing": [], "gpu": None}
            assert await ask(ep, "status", key="x" * 43) == {"ok": False, "error": "forbidden"}
            assert await ask(ep, "stop", key="x" * 43) == {"ok": False, "error": "forbidden"}
            assert app.stops == 0
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "control_refused: reason=key" in caplog.text
    assert KEY not in caplog.text and "x" * 43 not in caplog.text


def test_status_reports_the_bridge_the_cast_and_the_schema_for_the_health_check(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            conn = sqlite3.connect(":memory:")
            conn.execute("PRAGMA user_version = 17")
            app.bridge = SimpleNamespace(greeted=True)
            app.world = SimpleNamespace(cast_guids=lambda: {101, 102, 103})
            app.store = SimpleNamespace(conn=conn)
            reply = await ask(ep, "status")
            assert (reply["bridge_connected"], reply["cast"], reply["schema_version"]) == (
                True, 3, 17)
            app.bridge = SimpleNamespace(greeted=False)
            conn.close()  # a closed store: unknown, and status still answers
            reply = await ask(ep, "status")
            assert reply["ok"] is True and reply["bridge_connected"] is False
            assert reply["schema_version"] is None and reply["cast"] == 3
    run(go())


def test_status_carries_the_world(tmp_path):
    from leeroy_mind.protocol import Hello

    async def go():
        async with served(tmp_path) as (app, ep):
            assert (await ask(ep, "status"))["world"] is None  # no hello yet
            app.hello = Hello(ts=1793304000000, server="Acme", version="3.3.5", guild_id=1,
                              human_guid=9)
            app.bridge = SimpleNamespace(connected=True, greeted=True)
            app.world = SimpleNamespace(members={i: object() for i in range(1, 6)},
                                        online={1, 2, 3}, human_online=True,
                                        cast_guids=lambda: {1, 2, 3, 4, 5})
            world = (await ask(ep, "status"))["world"]
            assert world == {"connected": True, "server": "Acme", "version": "3.3.5",
                             "hello_at": 1793304000.0, "cast": 5, "cast_online": 3,
                             "human_online": True}
            app.bridge = SimpleNamespace(connected=False, greeted=False)  # stale view: unknown
            assert (await ask(ep, "status"))["world"] == {"connected": False}
            app.bridge = SimpleNamespace(connected=True, greeted=True)
            app.world = SimpleNamespace()  # unreadable: status still answers, world unknown
            reply = await ask(ep, "status")
            assert reply["ok"] is True and reply["world"] is None
    run(go())


def test_status_carries_who_is_typing_and_never_their_text(tmp_path):
    """Comms' dots: the talk engine's turns being composed or typed, as it gives them (ids, the
    place and when); none without an engine, while the mind stops or when the engine fails."""
    entry = {"guid": 101, "name": "Arkon", "channel": "whisper", "to": 9, "since": 1793304000.5}

    async def go():
        async with served(tmp_path) as (app, ep):
            app.engine = SimpleNamespace(typing_now=lambda: [entry])
            reply = await ask(ep, "status")
            assert reply["typing"] == [entry]
            app.stopping = True
            assert (await ask(ep, "status"))["typing"] == []
            app.stopping = False

            def broken():
                raise RuntimeError("Arkon: secret words")
            app.engine = SimpleNamespace(typing_now=broken)
            reply = await ask(ep, "status")
            assert reply["ok"] is True and reply["typing"] == []
            del app.engine
            assert (await ask(ep, "status"))["typing"] == []
    run(go())


def test_a_bridge_down_after_a_hello_says_so(tmp_path):
    """The Mind tab's "unknown (bridge down)": after a hello, a bridge that is down is said as
    such, and the stale world view is never read."""
    from leeroy_mind.protocol import Hello

    async def go():
        async with served(tmp_path) as (app, ep):
            app.hello = Hello(ts=1793304000000, server="Acme", version="3.3.5", guild_id=1,
                              human_guid=9)
            app.bridge = SimpleNamespace(connected=False, greeted=False)
            app.world = SimpleNamespace()  # stale and unreadable: not asked
            reply = await ask(ep, "status")
            assert reply["ok"] is True and reply["world"] == {"connected": False}
            app.hello = None
            assert (await ask(ep, "status"))["world"] is None  # before any hello: unknown
    run(go())


def test_only_one_strict_line_is_read_and_anything_else_is_refused(tmp_path):
    async def go():
        async with served(tmp_path, read_s=0.3) as (app, ep):
            out = await raw(ep, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert json.loads(out) == {"ok": False, "error": "bad request: not a JSON line"}
            out = await raw(ep, b"{" + b" " * (MAX_LINE + 10))
            assert json.loads(out)["ok"] is False
            out = await raw(ep, b"")  # nothing within read_s
            assert json.loads(out) == {"ok": False, "error": "bad request"}
            out = await raw(ep, b'{"v":1', close=True)  # ended mid-line
            assert json.loads(out) == {"ok": False, "error": "bad request"}
            # A status line, then a stop line in the same write: only the first is read
            both = line(v=1, key=KEY, op="status") + line(v=1, key=KEY, op="stop")
            out = await raw(ep, both)
            assert out.count(b"\n") == 1 and json.loads(out)["ok"] is True
            assert app.stops == 0
    run(go())


def test_too_many_connections_at_once_are_closed_unanswered(tmp_path):
    async def go():
        async with served(tmp_path, read_s=2) as (app, ep):
            idle = [await asyncio.open_connection("127.0.0.1", ep.port)
                    for _ in range(MAX_CONNECTIONS)]
            await asyncio.sleep(0.05)
            reader, writer = await asyncio.open_connection("127.0.0.1", ep.port)
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
            for _, w in idle:
                w.close()
    run(go())


def test_stop_is_answered_then_the_minds_own_shutdown_begins_once(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            assert await ask(ep, "stop") == {"ok": True, "stopping": True}
            for _ in range(200):  # begun once the answer has gone and the connection closed
                if app.stops:
                    break
                await asyncio.sleep(0.01)
            assert app.stops == 1
            assert await ask(ep, "stop") == {"ok": True, "stopping": True}
            assert app.stops == 1  # already stopping: not begun again
            status = await ask(ep, "status")
            assert status["stopping"] is True and status["raid"] is None
            assert await ask(ep, "raid", {"action": "end"}) == {
                "ok": False, "error": "the mind is stopping"}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert caplog.text.count("control_stop_requested") == 2


def test_raid_actions_go_to_guild_life_one_at_a_time_and_its_refusal_comes_back(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            assert await ask(ep, "raid", {"action": "summon"}) == {"ok": True,
                                                                   "message": "did summon"}
            assert await ask(ep, "raid", FORM) == {"ok": True, "message": "did form"}
            assert app.guild.actions == [{"action": "summon"}, FORM]
            app.guild.refuse = "you're not in game: guild life runs raid nights only while you are"
            assert await ask(ep, "raid", {"action": "call"}) == {"ok": False,
                                                                 "error": app.guild.refuse}
            app.guild.refuse, app.guild.gate = None, asyncio.Event()
            first = asyncio.create_task(ask(ep, "raid", {"action": "stop"}))
            while len(app.guild.actions) < 4:
                await asyncio.sleep(0.01)
            assert await ask(ep, "raid", {"action": "start"}) == {
                "ok": False, "error": "another raid action is still running"}
            app.guild.gate.set()
            assert await first == {"ok": True, "message": "did stop"}
            assert len(app.guild.actions) == 4
            assert (await ask(ep, "raid", {"action": "disband"}))["ok"] is False
            assert len(app.guild.actions) == 4  # never reached guild life
            app.guild = None
            assert await ask(ep, "raid", {"action": "end"}) == {
                "ok": False, "error": "guild life is not running in this mind"}
    run(go())


def test_a_slow_raid_action_is_answered_in_time_but_never_cancelled(tmp_path):
    async def go():
        async with served(tmp_path, raid_s=0.2) as (app, ep):
            app.guild.gate = asyncio.Event()
            reply = await ask(ep, "raid", {"action": "stop"})
            assert reply["ok"] is False and "still waiting on the game" in reply["error"]
            assert (await ask(ep, "raid", {"action": "end"}))["error"] == (
                "another raid action is still running")
            app.guild.gate.set()
            await asyncio.sleep(0.05)
            app.guild.gate = None
            assert await ask(ep, "raid", {"action": "end"}) == {"ok": True, "message": "did end"}
            assert [a["action"] for a in app.guild.actions] == ["stop", "end"]
    run(go())


def test_a_second_line_while_one_is_sending_is_refused(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            app.speech.gate = asyncio.Event()
            first = asyncio.create_task(ask(ep, "say", {"channel": "guild", "text": "omw"}))
            for _ in range(500):
                if app.speech.lines or first.done():
                    break
                await asyncio.sleep(0.01)
            assert app.speech.lines, await first
            for _ in range(2):  # a double-clicked Send, Enter held down
                assert await ask(ep, "say", {"channel": "guild", "text": "again"}) == {
                    "ok": False, "error": "Another line is still being sent."}
            app.speech.gate.set()
            assert await first == {"ok": True, "message": "sent"}
            assert app.speech.lines == [{"channel": "guild", "text": "omw"}]  # never reached it
            app.speech.gate = None
            assert await ask(ep, "say", {"channel": "guild", "text": "again"}) == {
                "ok": True, "message": "sent"}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert caplog.text.count("control_request: op=say channel=guild outcome=refused") == 2
    assert caplog.text.count("control_request: op=say channel=guild outcome=ok") == 2
    assert "omw" not in caplog.text and "again" not in caplog.text


REPLY = {"action": "reply", "kind": "post", "to": 7, "text": "see you thursday"}


def test_a_second_reply_while_one_is_saving_is_refused(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            app.board.gate = asyncio.Event()
            first = asyncio.create_task(ask(ep, "board", REPLY))
            for _ in range(500):
                if app.board.asks or first.done():
                    break
                await asyncio.sleep(0.01)
            assert app.board.asks, await first
            for args in (REPLY, {"action": "withdraw", "id": 1}):  # a double click, a withdraw
                assert await ask(ep, "board", args) == {
                    "ok": False, "error": "Another reply is still being saved."}
            app.board.gate.set()
            assert await first == {"ok": True, "message": "Saved. The guild answers tonight.",
                                   "id": 1}
            assert app.board.asks == [REPLY]  # the refused ones never reached the desk
            app.board.gate = None
            assert (await ask(ep, "board", {"action": "withdraw", "id": 1}))["ok"] is True
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert caplog.text.count("control_request: op=board action=reply outcome=refused") == 1
    assert caplog.text.count("control_request: op=board action=withdraw outcome=refused") == 1
    assert caplog.text.count("control_request: op=board action=reply outcome=ok") == 1
    assert "thursday" not in caplog.text


def test_the_board_ops_words_and_a_long_letter_fits_one_line(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            # 400 astral characters, each sent as two escapes: the longest reply there is
            letter = {"action": "reply", "kind": "letter", "to": (1 << 63) - 1,
                      "text": "\U0001F389" * 400}
            assert len(endpoint.encode({"v": 1, "key": KEY, "op": "board", "args": letter})) > 4096
            assert (await ask(ep, "board", letter))["ok"] is True
            assert app.board.asks == [letter]
            app.stopping = True
            assert await ask(ep, "board", REPLY) == {"ok": False, "error": "The mind is stopping."}
            app.stopping, app.board = False, None
            assert await ask(ep, "board", REPLY) == {
                "ok": False, "error": "Board replies are not running in this mind."}

            async def broken(args):
                raise RuntimeError("private reply text")
            app.board = FakeBoard()
            app.board.handle = broken
            assert await ask(ep, "board", REPLY) == {
                "ok": False, "error": "The reply could not be saved (RuntimeError)."}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "private reply text" not in caplog.text and "thursday" not in caplog.text
    assert "control_board_failed: error=RuntimeError" in caplog.text


def test_a_slow_line_is_answered_in_time_and_holds_the_next_off(tmp_path):
    assert endpoint.SAY_S == 10.0
    assert endpoint.SAY_CHANNELS == ("guild", "officer", "group", "whisper")

    async def go():
        async with served(tmp_path, say_s=0.2) as (app, ep):
            app.speech.gate = asyncio.Event()
            assert await ask(ep, "say", {"channel": "guild", "text": "omw"}) == {
                "ok": False,
                "error": "The game didn't answer in time. Check WoW before sending it again."}
            assert await ask(ep, "say", {"channel": "guild", "text": "omw"}) == {
                "ok": False, "error": "Another line is still being sent."}
            app.speech.gate.set()  # it was never cancelled: it ends, and lets the next one go
            await asyncio.sleep(0.05)
            app.speech.gate = None
            assert (await ask(ep, "say", {"channel": "guild", "text": "omw"}))["ok"] is True
            assert len(app.speech.lines) == 2
            app.stopping = True
            assert await ask(ep, "say", {"channel": "guild", "text": "omw"}) == {
                "ok": False, "error": "The mind is stopping."}
            app.stopping, app.speech = False, None
            assert await ask(ep, "say", {"channel": "guild", "text": "omw"}) == {
                "ok": False, "error": "Replies are not running in this mind."}
    run(go())


def test_a_line_that_fails_shows_only_its_class(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            async def broken(args):
                raise RuntimeError("private game text")
            app.speech.handle = broken
            assert await ask(ep, "say", {"channel": "guild", "text": "omw"}) == {
                "ok": False, "error": "The line could not be sent (RuntimeError)."}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "private game text" not in caplog.text and "omw" not in caplog.text
    assert "control_say_failed: error=RuntimeError" in caplog.text


def test_a_raid_action_that_fails_shows_only_its_class(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            async def broken(args):
                raise RuntimeError("private game text")
            app.guild.console_action = broken
            assert await ask(ep, "raid", {"action": "end"}) == {
                "ok": False, "error": "the raid action failed (RuntimeError)"}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "private game text" not in caplog.text


# -- the clean stop, against Ctrl+C ------------------------------------------------------------------

async def _live_mind(tmp_path: Path, how: str):
    """The real app over FakeBridge: his login and two lines open a session and a thread, then
    it is stopped by `how`: "ctrl_c" cancels run() as asyncio.run cancels its main task on
    Ctrl+C; "endpoint" sends stop through the control endpoint, as the console does."""
    clock = fake_clock()
    events = [hello(0), CAST, presence("login"), chat("evening all"), chat("who is around")]
    bridge = FakeBridge(events)
    port = await bridge.start()
    (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
    seed_db(tmp_path / "mind.db", clock)
    llm = FakeLLM(reply, models={LIVE})
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=llm, clock=clock, rng_seed=1)
    task = asyncio.create_task(app.run())
    h = App(app, bridge, llm, clock)
    try:
        await h.wait(lambda: app.session_id is not None)
        await h.wait(lambda: h.count("chat_line") >= 2)
        await asyncio.wait_for(app.control.listening.wait(), 5)
        record = read_key_file(tmp_path / KEY_FILE)
        assert record is not None and record["pid"] == os.getpid()
        if how == "ctrl_c":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 20)
        else:
            reply_ = await asyncio.to_thread(endpoint.request, record["port"], record["key"],
                                             "stop")
            assert reply_ == {"ok": True, "stopping": True}
            await asyncio.wait_for(task, 20)  # run() returns by itself
            assert task.exception() is None
    finally:
        if not task.done():
            await app.stop()
        await bridge.stop()
    assert app._closed and not app.running
    return record


def _state(tmp_path: Path) -> dict:
    with contextlib.closing(reopen(tmp_path)) as c:
        def rows(sql):
            return [tuple(r) for r in c.execute(sql).fetchall()]
        return {"session": rows("SELECT id, started, ended FROM session ORDER BY id"),
                "threads": rows("SELECT id, key, opened, closed, status FROM chat_thread ORDER BY id"),
                "lines": rows("SELECT id, ts, channel, from_guid, text, session_id, thread_id, origin "
                              "FROM chat_line ORDER BY id"),
                "events": rows("SELECT ts, type, guid FROM game_event ORDER BY id"),
                "memories": rows("SELECT owner, kind, text FROM memory ORDER BY id"),
                "utterances": rows("SELECT count(*) FROM utterance"),
                "cast": rows("SELECT guid, name, online FROM cast_member ORDER BY guid"),
                "raids": rows("SELECT * FROM guild_raid ORDER BY raid_id"),
                "dkp": rows("SELECT * FROM dkp_ledger ORDER BY id"),
                "meta": rows("SELECT key, value FROM meta WHERE key NOT LIKE 'code_%' ORDER BY key")}


def _shutdown_events(records) -> list[str]:
    events = [r.getMessage().split(":", 1)[0] for r in records if r.name.startswith("leeroy_mind")]
    start = events.index("shutdown_started")
    return events[start:events.index("shutdown_complete") + 1]


def test_the_clean_stop_leaves_the_minds_state_as_ctrl_c_leaves_it(tmp_path, caplog):
    states, shutdowns = {}, {}
    for how in ("ctrl_c", "endpoint"):
        data = tmp_path / how
        data.mkdir()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            run(_live_mind(data, how))
        states[how] = _state(data)
        shutdowns[how] = [e for e in _shutdown_events(caplog.records)
                          if e != "control_stop_requested"]
        assert not (data / KEY_FILE).exists()  # the endpoint took its key file with it
        assert sorted(p.name for p in data.iterdir() if p.name.startswith(".control")) == []
    assert states["ctrl_c"] == states["endpoint"]
    state = states["endpoint"]
    # Not vacuous: his session was open, his two lines in a thread, one turn queued
    assert state["session"] and all(s[2] is not None for s in state["session"])
    assert len(state["lines"]) == 2 and state["threads"] and state["utterances"][0][0] >= 1
    assert {t[4] for t in state["threads"]} == {"extracted"}  # the shutdown extracted it
    assert shutdowns["ctrl_c"] == shutdowns["endpoint"]
    assert shutdowns["endpoint"][0] == "shutdown_started"
    assert "session_ended" in shutdowns["endpoint"]


def test_run_returns_0_when_the_endpoint_stops_it(tmp_path):
    """`run`'s own coroutine (__main__.serve, inside asyncio.run in cmd_run): a stop through the
    endpoint ends it with 0, as a Ctrl+C does (cmd_run returns 0 on KeyboardInterrupt)."""
    from leeroy_mind.__main__ import serve

    async def go():
        bridge = FakeBridge([hello(0), CAST, presence("login")])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", fake_clock())
        apps = []
        task = asyncio.create_task(serve(tmp_path, make_llm=lambda cfg: FakeLLM(reply, models={LIVE}),
                                         ready=apps.append))
        try:
            while not apps or apps[0].control is None or not apps[0].control.listening.is_set():
                await asyncio.sleep(0.02)
            record = read_key_file(tmp_path / KEY_FILE)
            assert (await asyncio.to_thread(endpoint.request, record["port"], record["key"],
                                            "stop"))["ok"] is True
            assert await asyncio.wait_for(task, 30) == 0
        finally:
            if not task.done():
                task.cancel()
            await bridge.stop()
        assert apps[0]._closed and not (tmp_path / KEY_FILE).exists()
    run(go())
    with contextlib.closing(reopen(tmp_path)) as c:
        assert c.execute("SELECT count(*) FROM session WHERE ended IS NULL").fetchone()[0] == 0


def test_the_running_minds_status_says_its_bridge_cast_and_schema(tmp_path):
    """The real app over FakeBridge: once the bridge has greeted it and sent the cast, status
    says so, with mind.db's schema version."""
    from leeroy_mind.__main__ import serve
    from leeroy_mind.storage.db import latest_version

    async def go():
        bridge = FakeBridge([hello(0), CAST])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", fake_clock())
        apps = []
        task = asyncio.create_task(serve(tmp_path, make_llm=lambda cfg: FakeLLM(reply, models={LIVE}),
                                         ready=apps.append))
        try:
            while not apps or apps[0].control is None or not apps[0].control.listening.is_set():
                await asyncio.sleep(0.02)
            record = read_key_file(tmp_path / KEY_FILE)
            for _ in range(500):
                status = await asyncio.to_thread(endpoint.request, record["port"],
                                                 record["key"], "status")
                if status["bridge_connected"] and status["cast"]:
                    break
                await asyncio.sleep(0.02)
            assert status["bridge_connected"] is True
            assert status["cast"] == len(apps[0].world.cast_guids()) > 0
            assert status["schema_version"] == latest_version()
            await asyncio.to_thread(endpoint.request, record["port"], record["key"], "stop")
            assert await asyncio.wait_for(task, 30) == 0
        finally:
            if not task.done():
                task.cancel()
            await bridge.stop()
    run(go())


def test_the_running_minds_board_op_answers_from_its_store(tmp_path):
    """The real app over FakeBridge: its board desk is wired to the endpoint and to mind.db (the
    Realm is off in this mind.toml, so a reply is refused in its words and nothing is stored)."""
    from leeroy_mind.__main__ import serve

    async def go():
        bridge = FakeBridge([hello(0), CAST])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", fake_clock())
        apps = []
        task = asyncio.create_task(serve(tmp_path, make_llm=lambda cfg: FakeLLM(reply, models={LIVE}),
                                         ready=apps.append))
        try:
            while not apps or apps[0].control is None or not apps[0].control.listening.is_set():
                await asyncio.sleep(0.02)
            record = read_key_file(tmp_path / KEY_FILE)

            def board(args):
                return endpoint.request(record["port"], record["key"], "board", args)
            assert await asyncio.to_thread(board, {"action": "withdraw", "id": 1}) == {
                "ok": False, "error": "There is no such reply."}
            refused = await asyncio.to_thread(board, {"action": "reply", "kind": "post", "to": 7,
                                                      "text": "hi"})
            assert refused["ok"] is False and refused["error"].startswith("The Realm is off")
            count = apps[0].store.conn.execute("SELECT count(*) FROM board_reply").fetchone()[0]
            apps[0].store.conn.commit()
            assert count == 0
            await asyncio.to_thread(endpoint.request, record["port"], record["key"], "stop")
            assert await asyncio.wait_for(task, 30) == 0
        finally:
            if not task.done():
                task.cancel()
            await bridge.stop()
    run(go())


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows descriptor")
def test_the_private_descriptor_names_only_this_user():
    sddl = endpoint.private_sddl()
    assert sddl.startswith("D:P(A;;FA;;;S-1-") and sddl.endswith(")") and sddl.count(";") == 5


def test_the_key_file_waits_for_a_reader_to_let_go(tmp_path, monkeypatch):
    path, real, tries = tmp_path / KEY_FILE, os.replace, []

    def busy(src, dst):
        tries.append(dst)
        if len(tries) < 3:
            raise PermissionError(32, "in use by another process")  # a console reading it
        real(src, dst)
    monkeypatch.setattr(endpoint.os, "replace", busy)
    write_key_file(path, {"v": 1, "pid": 4242, "port": 40000, "key": KEY})
    assert len(tries) == 3 and read_key_file(path)["key"] == KEY

    def never(src, dst):
        raise PermissionError(32, "in use by another process")
    monkeypatch.setattr(endpoint.os, "replace", never)
    with pytest.raises(PermissionError):
        write_key_file(path, {"v": 1, "pid": 4242, "port": 40001, "key": KEY})
    assert read_key_file(path)["port"] == 40000  # the old one stays whole
    assert sorted(p.name for p in tmp_path.iterdir()) == [KEY_FILE]


def test_an_unexpected_failure_answers_with_its_class_only(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            async def broken(line):
                raise RuntimeError("private text")
            ep._answer = broken
            assert await ask(ep, "status") == {"ok": False, "error": "internal error (RuntimeError)"}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "control_failed: error=RuntimeError" in caplog.text and "private text" not in caplog.text


# -- the bank op ---------------------------------------------------------------------------------

BANK = {"money": 1234567, "tabs": [{"tab": 0, "name": "Food", "slots": [[0, 2589, 20]]}],
        "log": [[1793304000, 4, 1101, 50000]]}


class FakeArmory:
    def __init__(self, *, fail=None, available=True, delay=0.0):
        self.calls, self.fail, self.available, self.delay = [], fail, available, delay

    def bank(self, guild_id):
        self.calls.append(guild_id)
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return json.loads(json.dumps(BANK))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _bank_app(app, armory, guild_id="21"):
    app.guild = SimpleNamespace(armory=armory)
    app.store = SimpleNamespace(get_meta=lambda k: guild_id if k == "guild_id" else None)
    return armory


def test_the_bank_op_takes_no_args():
    assert parse_request(line(v=1, key=KEY, op="bank"), KEY) == ("bank", None)
    with pytest.raises(BadRequest, match="bank takes no args"):
        parse_request(line(v=1, key=KEY, op="bank", args={}), KEY)
    assert "bank" in endpoint.OPS


def test_the_bank_op_answers_a_snapshot(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            armory = _bank_app(app, FakeArmory())
            reply = await ask(ep, "bank")
            assert reply["ok"] is True and reply["guild"] == 21
            assert {k: reply[k] for k in BANK} == BANK and isinstance(reply["read_at"], float)
            assert armory.calls == [21]
    run(go())


def test_the_bank_is_read_once_per_thirty_seconds(tmp_path):
    clock = Clock()

    async def go():
        async with served(tmp_path, clock=clock) as (app, ep):
            armory = _bank_app(app, FakeArmory())
            first = await ask(ep, "bank")
            clock.now += 10
            assert await ask(ep, "bank") == first and len(armory.calls) == 1
            clock.now += 21
            await ask(ep, "bank")
            assert len(armory.calls) == 2
    run(go())


def test_two_asks_at_once_share_one_read(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            armory = _bank_app(app, FakeArmory(delay=0.3))
            a, b = await asyncio.gather(ask(ep, "bank"), ask(ep, "bank"))
            assert a["ok"] and a == b and len(armory.calls) == 1
    run(go())


def test_the_banks_words(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            app.guild = None
            assert (await ask(ep, "bank"))["error"] == "Guild life is not running in this mind."
            _bank_app(app, FakeArmory(), guild_id="0")
            assert (await ask(ep, "bank"))["error"] == (
                "The mind doesn't know the guild yet (no game hello since it started).")
            _bank_app(app, FakeArmory(available=False))
            assert (await ask(ep, "bank"))["error"] == (
                "The mind can't read the game database (worldserver.conf or mysql.exe not found).")
            _bank_app(app, FakeArmory(fail=ValueError("slot twice")))
            assert (await ask(ep, "bank"))["error"] == "The game database didn't answer (ValueError)."
    run(go())


def test_a_slow_database_is_named_and_the_read_goes_on(tmp_path):
    async def go():
        async with served(tmp_path, bank_s=0.1) as (app, ep):
            armory = _bank_app(app, FakeArmory(delay=0.5))
            reply = await ask(ep, "bank")
            assert reply == {"ok": False, "error":
                             "The game database is slow to answer. Try again in a moment."}
            await asyncio.sleep(0.6)
            assert (await ask(ep, "bank"))["ok"] is True and len(armory.calls) == 1
    run(go())


def test_a_mysql_failure_names_no_secret(tmp_path, caplog, monkeypatch):
    from leeroy_mind.guild.config import GuildConfig
    from leeroy_mind.guild.ports import LazyArmory
    from leeroy_mind.library.mysql import QueryError
    from leeroy_mind.personas import roster

    def boom(db, sql, exe):
        raise QueryError("access denied for hunter2")
    monkeypatch.setattr(roster, "run_mysql", boom)
    armory = LazyArmory(GuildConfig(worldserver_conf=Path(__file__)))
    armory._backend = SimpleNamespace(chars="db")

    async def go():
        async with served(tmp_path) as (app, ep):
            _bank_app(app, armory)
            return await ask(ep, "bank")
    with caplog.at_level(logging.DEBUG):
        reply = run(go())
    assert reply == {"ok": False, "error": "The game database didn't answer (QueryError)."}
    assert "hunter2" not in json.dumps(reply) + caplog.text
    assert "op=bank outcome=refused" in caplog.text


def test_a_missing_mysql_exe_gets_the_unreadable_words(tmp_path):
    from leeroy_mind.personas.roster import RosterError

    async def go():
        async with served(tmp_path) as (app, ep):
            _bank_app(app, FakeArmory(fail=RosterError("mysql did not run: FileNotFoundError")))
            return await ask(ep, "bank")
    assert run(go()) == {"ok": False, "error": "The mind can't read the game database "
                                               "(worldserver.conf or mysql.exe not found)."}


def test_a_stopping_mind_reads_nothing(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            armory = _bank_app(app, FakeArmory())
            app.stopping = True
            assert (await ask(ep, "bank"))["error"] == "the mind is stopping"
            assert armory.calls == []
    run(go())


# -- the library op ------------------------------------------------------------------------------

LIB_SECRET = "xyzzyplugh"


class FakeAdmin:
    def __init__(self, loaded):
        self.loaded, self.calls = loaded, []

    async def loaded_models(self):
        self.calls.append("ps")
        return list(self.loaded)

    async def preload(self, *a, **kw):
        self.calls.append("preload")

    async def unload(self, *a, **kw):
        self.calls.append("unload")


async def _library_app(app, tmp_path, *, loaded=("qwen3-embedding:0.6b",), gate=None, vectors=True):
    from leeroy_mind.library.chunks import ChunkDraft
    from leeroy_mind.library.core import DEFAULT_EMBED_MODEL, Library
    from leeroy_mind.library.search_worker import LibrarySearchWorker
    from test_library_core import ListSource
    path = tmp_path / "library.db"
    llm = FakeLLM(gate=gate)
    lib = Library(path, clock=SimpleNamespace(now=lambda: 1.0))
    lib.build([ListSource("world", [
        ChunkDraft("c:1", "command", ".setskill", ".setskill #skill #level\nSets a weapon skill."),
        ChunkDraft("t:1", "teleport", "Hyjal", "Mount Hyjal: .tele hyjal"),
        ChunkDraft("i:1", "item", "Rune of Fate", "Rune of Fate: epic trinket.")])])
    if vectors:
        await lib.embed_pending(FakeLLM())
    lib.close()
    app.library = Library(path, clock=SimpleNamespace(now=lambda: 1.0), readonly=True)
    app.library_search = LibrarySearchWorker(path, embed_model=DEFAULT_EMBED_MODEL)
    app.scheduler = llm
    app.cfg = SimpleNamespace(models=SimpleNamespace(embedding=DEFAULT_EMBED_MODEL))
    app.arbiter = SimpleNamespace(admin=FakeAdmin(loaded))
    return llm


def test_the_library_op_takes_a_query_and_k():
    assert parse_request(line(v=1, key=KEY, op="library", args={"query": " a  b ", "k": 3}),
                         KEY) == ("library", {"query": "a b", "k": 3})
    assert parse_request(line(v=1, key=KEY, op="library", args={"query": "a"}),
                         KEY)[1] == {"query": "a", "k": 12}
    for bad in ({"query": ""}, {"query": "a" * 201}, {"query": "a", "k": 0},
                {"query": "a", "k": 13}, {"query": "a", "k": True}, {"query": 5},
                {"query": "a", "extra": 1}, {}, [], None):
        with pytest.raises(BadRequest):
            parse_request(line(v=1, key=KEY, op="library", args=bad), KEY)
    with pytest.raises(BadRequest):
        parse_request(line(v=1, key=KEY, op="library"), KEY)


def test_the_library_op_embeds_once_and_never_loads_a_model(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            llm = await _library_app(app, tmp_path)
            try:
                llm.calls.clear()
                reply = await ask(ep, "library", {"query": "weapon skill", "k": 5})
                assert reply["ok"] is True and 1 <= len(reply["hits"]) <= 5
                assert reply["hits"][0]["kind"] in ("command", "teleport", "item")
                assert set(reply["hits"][0]) == {"id", "kind", "title", "section", "text",
                                                 "source", "cite", "score"}
                assert len(llm.calls) == 1 and "input" in llm.calls[0]  # one embed, no chat
                assert app.arbiter.admin.calls == ["ps"]  # looked, never loaded or unloaded
            finally:
                await app.library_search.aclose()
                app.library.close()
    run(go())


def test_the_library_op_says_why_it_cannot(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            assert (await ask(ep, "library", {"query": "x"}))["ok"] is False  # no library at all
            llm = await _library_app(app, tmp_path, loaded=("some-chat:latest",))
            try:
                llm.calls.clear()
                r = await ask(ep, "library", {"query": "x"})
                assert r["ok"] is False and "embedding model" in r["error"] and llm.calls == []
                assert app.arbiter.admin.calls == ["ps"]
                app.arbiter.admin.loaded = ["qwen3-embedding:0.6b"]
                app.arbiter = None  # cannot tell
                r = await ask(ep, "library", {"query": "x"})
                assert r["ok"] is False and llm.calls == []
                app.stopping = True
                assert (await ask(ep, "library", {"query": "x"}))["error"] == "the mind is stopping"
            finally:
                await app.library_search.aclose()
                app.library.close()
    run(go())


def test_the_library_op_without_vectors_says_so(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            llm = await _library_app(app, tmp_path, vectors=False)
            try:
                r = await ask(ep, "library", {"query": "x"})
                assert r["ok"] is False and "embeddings" in r["error"] and llm.calls == []
            finally:
                await app.library_search.aclose()
                app.library.close()
    run(go())


def test_the_library_op_runs_one_call_at_a_time(tmp_path):
    async def go():
        gate = asyncio.Event()
        async with served(tmp_path) as (app, ep):
            await _library_app(app, tmp_path, gate=gate)
            try:
                first = asyncio.create_task(ask(ep, "library", {"query": "weapon"}))
                await asyncio.sleep(0.3)
                busy = await ask(ep, "library", {"query": "weapon"})
                assert busy == {"ok": False, "error": "Another library search is running."}
                gate.set()
                assert (await first)["ok"] is True
            finally:
                gate.set()
                await app.library_search.aclose()
                app.library.close()
    run(go())


def test_the_library_op_never_logs_the_query(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            llm = await _library_app(app, tmp_path)
            try:
                await ask(ep, "library", {"query": LIB_SECRET})
                llm.fail_next(1)
                await ask(ep, "library", {"query": LIB_SECRET})
            finally:
                await app.library_search.aclose()
                app.library.close()
    with caplog.at_level(logging.DEBUG):
        run(go())
    assert "control_request: op=library" in caplog.text and LIB_SECRET not in caplog.text


def test_a_failed_embed_is_not_answered_as_a_deeper_search(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            llm = await _library_app(app, tmp_path)
            try:
                llm.fail_next(1)
                r = await ask(ep, "library", {"query": "weapon skill"})
                assert r == {"ok": False, "error": "The mind couldn't run a deeper search just now."}
                assert (await ask(ep, "library", {"query": "weapon skill"}))["ok"] is True
            finally:
                await app.library_search.aclose()
                app.library.close()
    run(go())


def test_discord_is_an_op_and_its_args_are_checked():
    assert "discord" in endpoint.OPS
    assert set(endpoint.DISCORD_SHAPES) == {"post", "react", "unreact", "edit", "delete", "pin",
                                            "unpin", "thread", "group_create", "group_add",
                                            "group_remove", "group_leave", "group_rename"}
    assert parse_request(line(v=1, key=KEY, op="discord", args={
        "action": "react", "message": 4, "emoji": "\U0001F44D"}), KEY) == (
        "discord", {"action": "react", "message": 4, "emoji": "\U0001F44D"})
    with pytest.raises(BadRequest, match="discord needs args"):
        parse_request(line(v=1, key=KEY, op="discord"), KEY)
    with pytest.raises(BadRequest):
        parse_request(line(v=1, key=KEY, op="discord", args={
            "action": "react", "message": 4, "emoji": "\U0001F44D\U0001F44D"}), KEY)


def test_the_discord_op_runs_one_action_at_a_time(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    post = {"action": "post", "channel": "general", "text": "hi"}

    async def go():
        async with served(tmp_path) as (app, ep):
            app.discord.gate = asyncio.Event()
            first = asyncio.create_task(ask(ep, "discord", post))
            for _ in range(100):
                if app.discord.asks or first.done():
                    break
                await asyncio.sleep(0.02)
            assert app.discord.asks, await first
            busy = await ask(ep, "discord", post)
            assert busy["ok"] is False and busy["error"]
            app.discord.gate.set()
            assert (await first)["ok"] is True
            app.discord.gate = None
            app.stopping = True
            assert await ask(ep, "discord", post) == {"ok": False,
                                                      "error": "The mind is stopping."}
            app.stopping, app.discord = False, None
            assert (await ask(ep, "discord", post))["ok"] is False

    asyncio.run(go())
    assert "control_request: op=discord action=post outcome=ok" in caplog.text


# -- his decisions: the decision op ---------------------------------------------------------------

class Undoable:
    kind = "loot_rule"

    def __init__(self):
        self.undone = []

    def reversible(self, row):
        return True

    async def undo(self, app, row):
        self.undone.append(row["id"])


def decisions_service(store, clock):
    from leeroy_mind.decisions.config import DecisionsConfig
    from leeroy_mind.decisions.service import DecisionsService
    store.set_meta("human_guid", "1")
    return DecisionsService(SimpleNamespace(store=store, clock=clock, human_guid=1), DecisionsConfig(enabled=True))


def test_the_decision_op_in_its_one_strict_form():
    assert "decision" in endpoint.OPS
    assert parse_request(line(v=1, key=KEY, op="decision", args={"action": "overrule", "id": 3}), KEY) == (
        "decision", {"action": "overrule", "id": 3})
    for bad in ({"action": "vote", "poll": 1}, {"action": "vote", "poll": 0, "option": "dkp"},
                {"action": "vote", "poll": 1, "option": "DKP!"}, {"action": "overrule", "id": True},
                {"action": "undo", "id": 1}, {"action": "overrule", "id": 1, "why": "x"}):
        with pytest.raises(BadRequest):
            parse_request(line(v=1, key=KEY, op="decision", args=bad), KEY)


def test_undo_only_where_reversible(store, clock, tmp_path, caplog):
    from leeroy_mind.decisions import apply, ledger
    caplog.set_level(logging.INFO)
    now = clock.now()
    made = {}
    for kind, subject in (("loot_rule", "loot"), ("strategy", "strategy:631:9:0"), ("rank", "rank:105")):
        made[kind] = ledger.propose(store, kind=kind, subject=subject, value={}, positions=[], now=now)
        if kind != "rank":
            ledger.settle(store, made[kind], settled_by=102, now=now)
            ledger.take_effect(store, made[kind], effect_ref={"refs": [f"decision:{made[kind]}"]}, now=now)
    rules = Undoable()
    apply.register(rules)

    async def go():
        async with served(tmp_path) as (app, ep):
            app.decisions = decisions_service(store, clock)
            undone = await ask(ep, "decision", {"action": "overrule", "id": made["loot_rule"]})
            future = await ask(ep, "decision", {"action": "overrule", "id": made["strategy"]})
            early = await ask(ep, "decision", {"action": "overrule", "id": made["rank"]})
            again = await ask(ep, "decision", {"action": "overrule", "id": made["strategy"]})
            app.decisions = None
            off = await ask(ep, "decision", {"action": "overrule", "id": 1})
            return undone, future, early, again, off
    try:
        undone, future, early, again, off = asyncio.run(go())
    finally:
        apply.unregister("loot_rule")
    assert undone["ok"] and undone["undone"] is True and rules.undone == [made["loot_rule"]]
    assert future["ok"] and future["undone"] is False and "can't be undone in game" in future["message"]
    assert early["ok"] and early["message"] == "Overruled before it took effect."
    assert again == {"ok": False, "error": "That decision is no longer live."}
    assert off == {"ok": False, "error": "Decisions are off in this mind."}
    rows = {r["id"]: r for r in store.conn.execute("SELECT * FROM guild_decision")}
    assert all(r["status"] == "overruled" and r["overruled_by_owner"] == 1 for r in rows.values())
    assert f"decision_op action=overrule id={made['loot_rule']} outcome=undone" in caplog.text
    assert f"decision_op action=overrule id={made['strategy']} outcome=overruled" in caplog.text


def test_his_vote_counts_once(store, clock, tmp_path):
    from leeroy_mind.decisions import polls
    now = clock.now()
    store.set_meta("human_guid", "1")
    poll = polls.open_poll(store, channel="officer-chat", question="Loot rules?", options=("dkp", "loot_council"),
                           voters=(102, 104), closes=now + 3600, now=now, kind="loot_rule", subject="loot")

    async def go():
        async with served(tmp_path) as (app, ep):
            app.decisions = decisions_service(store, clock)
            first = await ask(ep, "decision", {"action": "vote", "poll": poll, "option": "dkp"})
            second = await ask(ep, "decision", {"action": "vote", "poll": poll, "option": "loot_council"})
            wrong = await ask(ep, "decision", {"action": "vote", "poll": poll + 1, "option": "dkp"})
            return first, second, wrong
    first, second, wrong = asyncio.run(go())
    assert first == {"ok": True, "outcome": "voted", "message": "Your vote is in."}
    assert second == {"ok": False, "error": "You already voted in this poll; a vote never changes."}
    assert wrong == {"ok": False, "error": "There is no such poll."}
    assert polls.tally(store.conn, poll) == {"dkp": 1, "loot_council": 0}
    assert polls.voters_of(polls.get_poll(store.conn, poll)) == {1, 102, 104}
