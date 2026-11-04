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
import stat
import sys
from pathlib import Path

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


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stopping = False
        self.stops = 0
        self.guild = FakeGuild()

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
                             "raid": FakeGuild().console_status()}
            assert await ask(ep, "status", key="x" * 43) == {"ok": False, "error": "forbidden"}
            assert await ask(ep, "stop", key="x" * 43) == {"ok": False, "error": "forbidden"}
            assert app.stops == 0
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "control_refused: reason=key" in caplog.text
    assert KEY not in caplog.text and "x" * 43 not in caplog.text


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
    assert states["endpoint"]["session"] and all(s[2] is not None for s in states["endpoint"]["session"])
    assert {t[4] for t in states["endpoint"]["threads"]} <= {"extracted", "skipped", "pending"}
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


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows descriptor")
def test_the_private_descriptor_names_only_this_user():
    sddl = endpoint.private_sddl()
    assert sddl.startswith("D:P(A;;FA;;;S-1-") and sddl.endswith(")") and sddl.count(";") == 5
