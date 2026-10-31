import asyncio
import contextlib
import dataclasses
import json
import logging
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind.bridge import (AckTimeout, Backoff, BridgeClient, ConnectionLost,
                                NotConnected)
from leeroy_mind.protocol import (Ack, Chat, Error, GetCast, Hello, Ping, Say,
                                  register_command)
from leeroy_mind.testing.fake_bridge import CORE_COMMAND_TYPES, FakeBridge, load_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "session_basic.jsonl"
FAST = dict(initial=0.05, maximum=0.1, jitter=0)


@dataclasses.dataclass(frozen=True, slots=True)
class UnknownProbe:
    id: str


register_command("test_unknown", UnknownProbe)


def chat_event(text: str) -> dict:
    return {"type": "chat", "channel": "guild", "from_guid": 1, "from_name": "Testmage",
            "to_guid": 0, "text": text, "map": 0, "zone": 1519}


class Session:
    def __init__(self, bridge: FakeBridge, client: BridgeClient, events: list, flags: list):
        self.bridge, self.client, self.events, self.flags = bridge, client, events, flags

    async def wait_for(self, predicate, timeout: float = 2.0):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), timeout)

    def types(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


@contextlib.asynccontextmanager
async def session(*, events=None, wait=True, bridge_kw=None, client_kw=None, handler=None):
    bridge = FakeBridge(events, **(bridge_kw or {}))
    port = await bridge.start()
    got: list = []
    flags: list = []

    holder: list = []

    async def on_event(ev):
        got.append(ev)
        if handler is not None:
            await handler(holder[0], ev)

    async def on_connection(up):
        flags.append(up)

    kw = dict(backoff=Backoff(**FAST), on_connection=on_connection)
    kw.update(client_kw or {})
    client = BridgeClient("127.0.0.1", port, on_event, **kw)
    holder.append(client)
    task = asyncio.create_task(client.run())
    try:
        if wait:
            await client.wait_connected(2.0)
        yield Session(bridge, client, got, flags)
    finally:
        await client.close()
        await asyncio.wait_for(task, 2.0)
        await bridge.stop()


def say(text="hi", guid=101):
    return Say(id="", guid=guid, channel="guild", text=text)


def test_receives_hello_cast_and_replayed_events():
    async def go():
        async with session(events=load_fixture(FIXTURE)) as s:
            await s.wait_for(lambda: len(s.events) >= 4)
            assert [type(e).__name__.lower() for e in s.events] == ["hello", "cast", "login", "chat"]
            assert s.events[0].human_guid == 0 and s.client.human_guid == 0
            assert [m.name for m in s.events[1].members] == ["Arkon", "Belra", "Coros", "Dunna", "Ebbo"]
            assert s.events[3].via_bridge is False
    run(go())


def test_send_resolves_with_ack():
    async def go():
        async with session() as s:
            ack = await s.client.send(say())
            assert ack.ok and s.bridge.commands[-1]["id"] == ack.id and ack.id.startswith("m")
            assert s.bridge.commands[-1]["type"] == "say"
            assert not any(isinstance(e, Ack) for e in s.events)
    run(go())


def test_failed_ack_is_returned_not_raised():
    async def go():
        async with session(bridge_kw=dict(ack=lambda c: (False, "offline"))) as s:
            ack = await s.client.send(say())
            assert (ack.ok, ack.error) == (False, "offline")
    run(go())


def test_ack_timeout():
    async def go():
        async with session(bridge_kw=dict(ack=lambda c: None), client_kw=dict(ack_timeout=0.2)) as s:
            with pytest.raises(AckTimeout):
                await s.client.send(say())
            assert s.client.connected and not s.client._pending
    run(go())


def test_reconnects_after_drop_and_fails_pending():
    async def go():
        async with session(bridge_kw=dict(ack=lambda c: None), client_kw=dict(ack_timeout=5.0)) as s:
            sender = asyncio.create_task(s.client.send(say()))
            await s.bridge.wait_for_command("say")
            await s.bridge.drop_client()
            with pytest.raises(ConnectionLost):
                await asyncio.wait_for(sender, 2.0)
            await s.wait_for(lambda: s.bridge.connections == 2 and s.client.connected)
            await s.wait_for(lambda: s.types().count("Hello") == 2)
            assert s.types().count("Cast") == 2
            assert s.flags == [True, False, True]
    run(go())


def test_second_mind_is_refused():
    async def go():
        async with session() as s:
            got2: list = []

            async def on_event2(ev):
                got2.append(ev)

            second = BridgeClient("127.0.0.1", s.bridge._server.sockets[0].getsockname()[1],
                                  on_event2, backoff=Backoff(5, 5, jitter=0))
            task = asyncio.create_task(second.run())
            try:
                async def poll():
                    while not got2:
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(poll(), 2.0)
                assert isinstance(got2[0], Error) and got2[0].id == ""
                assert got2[0].message == "another mind is connected"
                assert s.client.connected and s.bridge.connections == 1
                assert (await s.client.send(say())).ok
            finally:
                await second.close()
                await asyncio.wait_for(task, 2.0)
    run(go())


def test_oversized_line_is_skipped():
    async def go():
        async with session() as s:
            await s.wait_for(lambda: len(s.events) >= 2)
            await s.bridge.push_raw(b"x" * 70000 + b"\n")
            await s.bridge.push(chat_event("after the big one"))
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            assert [e.text for e in s.events if isinstance(e, Chat)] == ["after the big one"]
            assert s.client.connected
            await s.wait_for(lambda: s.flags == [True])
    run(go())


def test_malformed_and_unknown_lines_are_skipped():
    async def go():
        async with session() as s:
            await s.bridge.push_raw(b"not json\n")
            await s.bridge.push({"type": "bogus"})
            await s.bridge.push(chat_event("only me"))
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            assert s.types() == ["Hello", "Cast", "Chat"]
    run(go())


def test_send_when_disconnected_raises():
    async def go():
        client = BridgeClient("127.0.0.1", 1, lambda ev: asyncio.sleep(0))
        assert not client.connected
        with pytest.raises(NotConnected):
            await client.send(say())
    run(go())


def test_unknown_command_type_fails_fast():
    async def go():
        async with session(bridge_kw=dict(known_types=CORE_COMMAND_TYPES),
                           client_kw=dict(ack_timeout=5.0)) as s:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            ack = await s.client.send(UnknownProbe(id=""))
            assert (ack.ok, ack.error) == (False, "unknown type: test_unknown")
            assert loop.time() - t0 < 1.0
            assert not any(isinstance(e, Error) for e in s.events)
    run(go())


def test_say_echo_carries_via_bridge():
    async def go():
        async with session() as s:
            ack = await s.client.send(say("hello there", guid=101))
            assert ack.ok
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            echo = next(e for e in s.events if isinstance(e, Chat))
            assert echo.via_bridge is True and echo.text == "hello there"
            assert (echo.from_guid, echo.from_name) == (101, "Arkon")
    run(go())


def test_connection_callback():
    async def go():
        async with session() as s:
            await s.wait_for(lambda: s.flags == [True])
            await s.bridge.drop_client()
            await s.wait_for(lambda: s.flags == [True, False, True])
    run(go())


def test_backoff_sequence():
    b = Backoff(0.5, 30, jitter=0)
    assert [b.next() for _ in range(8)] == [0.5, 1, 2, 4, 8, 16, 30, 30]
    b.reset()
    assert b.next() == 0.5
    assert 0.8 <= Backoff(1, 30, jitter=0.2).next() <= 1.2


def test_ping_timeout_triggers_reconnect():
    async def go():
        async with session(bridge_kw=dict(ack=lambda c: None),
                           client_kw=dict(ping_interval=0.1, ack_timeout=0.1)) as s:
            await s.wait_for(lambda: s.bridge.connections == 2, timeout=2.0)
    run(go())


def test_get_cast_resolves_on_next_cast_and_cast_still_delivered():
    async def go():
        async with session() as s:
            await s.wait_for(lambda: "Cast" in s.types())
            ack = await s.client.send(GetCast(id=""))
            assert ack.ok and ack.id.startswith("m")
            await s.wait_for(lambda: s.types().count("Cast") == 2)
            assert s.bridge.commands[-1]["type"] == "get_cast"
    run(go())


def test_ping_returns_ack_from_pong_and_runs_on_the_clock(clock):
    async def go():
        async with session(client_kw=dict(clock=clock, ping_interval=30.0)) as s:
            await asyncio.sleep(0.1)
            assert not [c for c in s.bridge.commands if c["type"] == "ping"]
            await clock.advance(31)
            ping = await s.bridge.wait_for_command("ping")
            assert ping["id"].startswith("m")
            assert not any(type(e).__name__ in ("Pong", "Ack") for e in s.events)
            ack = await s.client.send(Ping(id=""))
            assert ack.ok
    run(go())


def test_ack_and_error_before_hello_are_accepted():
    async def go():
        got: list = []

        async def on_event(ev):
            got.append(ev)

        async def handle(reader, writer):
            for line in (
                {"type": "error", "message": "bad field"},
                {"type": "ack", "id": "stale", "ok": True, "error": ""},
                {"type": "hello", "server": "mod-leeroy", "version": "0.1.0", "guild_id": 1,
                 "human_guid": 0},
            ):
                writer.write(json.dumps({"v": 1, "ts": 1, **line}).encode() + b"\n")
            await writer.drain()
            await reader.read()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = BridgeClient("127.0.0.1", port, on_event, backoff=Backoff(**FAST))
        task = asyncio.create_task(client.run())
        try:
            await client.wait_connected(2.0)
            assert client.connected  # open before any hello
            async def poll():
                while len(got) < 2:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(poll(), 2.0)
            assert [type(e) for e in got] == [Error, Hello]
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            server.close()
            await server.wait_closed()
    run(go())


def test_fake_bridge_closes_on_non_json_first_line():
    async def go():
        bridge = FakeBridge()
        port = await bridge.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"hello there\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 2.0)  # greeting, then EOF
            lines = data.splitlines()
            assert len(lines) == 3
            err = json.loads(lines[2])
            assert err["type"] == "error" and err["message"] == "bad json" and "id" not in err
            writer.close()
        finally:
            await bridge.stop()
    run(go())


def test_handler_can_send_and_get_its_ack():
    async def go():
        acks: list = []

        async def handler(client, ev):
            if isinstance(ev, Hello):
                acks.append(await client.send(say("from handler")))

        async with session(handler=handler) as s:
            await s.wait_for(lambda: acks)
            assert acks[0].ok
    run(go())


def test_send_in_reconnect_gap_raises_not_connected():
    async def go():
        async with session(client_kw=dict(backoff=Backoff(0.4, 0.4, jitter=0))) as s:
            await s.bridge.drop_client()
            await s.wait_for(lambda: not s.client.connected)
            with pytest.raises(NotConnected):
                await s.client.send(say())
            await s.wait_for(lambda: s.client.connected)
    run(go())


def test_slow_handler_does_not_delay_reconnect():
    async def go():
        async def handler(client, ev):
            if isinstance(ev, Chat):
                await asyncio.sleep(1.5)

        async with session(handler=handler) as s:
            await s.bridge.push(chat_event("slow"))
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            await s.bridge.drop_client()
            await s.wait_for(lambda: s.bridge.connections == 2 and s.client.connected, timeout=1.0)
    run(go())


def _dispatchers() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks()
            if not t.done() and t.get_coro().__qualname__.endswith("._dispatch")]


def test_a_second_close_during_the_drain_only_waits():
    """Task 3 N1: a second close() while run() drains must not cancel it again, or the
    dispatcher is never stopped and outlives the client."""
    async def go():
        async def handler(client, ev):
            if isinstance(ev, Chat):
                await asyncio.Event().wait()  # stuck: the drain has to give up on it

        async with session(handler=handler) as s:
            await s.bridge.push(chat_event("stuck"))
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            run_task = s.client._run_task
            first = asyncio.create_task(s.client.close())
            await asyncio.sleep(0.05)  # inside the drain
            await asyncio.wait_for(s.client.close(), 3.0)
            await asyncio.wait_for(first, 3.0)
            assert run_task.done() and not run_task.cancelled()
            assert _dispatchers() == []
    run(go())


async def _stuck_client(handler) -> tuple[FakeBridge, BridgeClient, asyncio.Task, list]:
    """A connected client whose handler is busy with a chat event."""
    bridge = FakeBridge()
    port = await bridge.start()
    got: list = []

    async def on_event(ev):
        got.append(ev)
        await handler(ev)

    client = BridgeClient("127.0.0.1", port, on_event, backoff=Backoff(**FAST))
    task = asyncio.create_task(client.run())
    await client.wait_connected(2.0)
    await bridge.push(chat_event("stuck"))

    async def poll():
        while not any(isinstance(e, Chat) for e in got):
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), 2.0)
    return bridge, client, task, got


def test_an_outside_cancel_during_the_drain_still_stops_the_dispatcher():
    async def go():
        async def handler(ev):
            if isinstance(ev, Chat):
                await asyncio.Event().wait()

        bridge, client, task, _ = await _stuck_client(handler)
        try:
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0.05)  # inside the drain
            task.cancel()  # someone else cancels run() in the middle of it
            await asyncio.wait({closing}, timeout=3.0)
            assert closing.done() and task.done() and _dispatchers() == []
        finally:
            await bridge.stop()
    run(go())


def test_a_handler_that_swallows_the_cancel_still_ends_the_dispatcher():
    async def go():
        swallowed: list[bool] = []

        async def handler(ev):
            if isinstance(ev, Chat):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    swallowed.append(True)  # and returns as if nothing happened

        bridge, client, task, _ = await _stuck_client(handler)
        try:
            task.cancel()  # not a close(): no drain, the dispatcher is cancelled at once
            await asyncio.wait({task}, timeout=3.0)
            assert task.done() and swallowed and _dispatchers() == []
        finally:
            await client.close()
            await bridge.stop()
    run(go())


def test_a_handler_that_will_not_stop_delays_close_only_so_long(monkeypatch, caplog):
    monkeypatch.setattr(BridgeClient, "DRAIN_S", 0.1)
    monkeypatch.setattr(BridgeClient, "STOP_S", 0.2)

    async def go():
        release = asyncio.Event()

        async def handler(ev):
            if isinstance(ev, Chat):
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        pass  # refuses to stop until the test lets it

        bridge, client, task, _ = await _stuck_client(handler)
        loop = asyncio.get_running_loop()
        try:
            t0 = loop.time()
            await asyncio.wait_for(client.close(), 2.0)
            assert loop.time() - t0 < 1.0 and task.done()
            assert "bridge_dispatcher_stuck" in caplog.text
        finally:
            release.set()  # the handler ends, and the dispatcher with it (the sentinel)
            stuck = _dispatchers()
            if stuck:
                await asyncio.wait(stuck, timeout=1.0)
            assert _dispatchers() == []
            await bridge.stop()
    with caplog.at_level(logging.ERROR, logger="leeroy_mind.bridge"):
        run(go())


def test_the_backlog_warning_fires_once_per_crossing_and_again_when_it_doubles(caplog):
    """Task 3 N2: no flapping at exactly 1000 and a new warning when the queue keeps growing,
    with the size logged."""
    async def go():
        client = BridgeClient("127.0.0.1", 1, lambda ev: asyncio.sleep(0))

        def warnings() -> list[str]:
            return [r.getMessage() for r in caplog.records if "queue_backlog" in r.getMessage()]
        for _ in range(999):
            client._enqueue(("event", None))
        for _ in range(100):  # hovering at the mark
            client._enqueue(("event", None))
            client._queue.get_nowait()
        assert warnings() == ["bridge_event_queue_backlog: size=1000"]
        for _ in range(1001):
            client._enqueue(("event", None))
        assert warnings()[1:] == ["bridge_event_queue_backlog: size=2000"]
        while client._queue.qsize() > 400:  # the dispatcher catches up
            client._queue.get_nowait()
        for _ in range(600):
            client._enqueue(("event", None))
        assert warnings()[2:] == ["bridge_event_queue_backlog: size=1000"]
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.bridge"):
        run(go())


def test_handler_raising_cancelled_does_not_stop_later_events():
    async def go():
        async def handler(client, ev):
            if isinstance(ev, Chat) and ev.text == "boom":
                raise asyncio.CancelledError

        async with session(handler=handler) as s:
            await s.bridge.push(chat_event("boom"))
            await s.bridge.push(chat_event("after"))
            await s.wait_for(lambda: [e.text for e in s.events if isinstance(e, Chat)] == ["boom", "after"])
    run(go())


class _Waits(Backoff):
    """A Backoff that records every wait it hands out."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.waits: list[float] = []

    def next(self) -> float:
        self.waits.append(super().next())
        return self.waits[-1]


def test_reconnects_after_the_server_was_down_and_resets_the_backoff(clock, caplog):
    """Tests lens I2: a refused connect (the worldserver loading after a restart) is retried
    on the backoff, never raised out of run(), and the hello resets the backoff."""
    async def go():
        bridge = FakeBridge()
        port = await bridge.start()
        got: list = []

        async def on_event(ev):
            got.append(ev)

        async def until(pred, timeout=8.0):  # a refused connect costs about 2 s on Windows
            async def poll():
                while not pred():
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(poll(), timeout)

        backoff = _Waits(0.5, 30, jitter=0)
        client = BridgeClient("127.0.0.1", port, on_event, backoff=backoff, clock=clock)
        task = asyncio.create_task(client.run())
        hellos = lambda: sum(isinstance(e, Hello) for e in got)  # noqa: E731
        try:
            await until(lambda: hellos() == 1)
            await bridge.go_down()
            await until(lambda: backoff.waits == [0.5])  # the drop: waiting on the clock
            await clock.advance(0.5)
            await until(lambda: backoff.waits == [0.5, 1.0])  # one refused attempt
            assert "bridge_connect_failed" in caplog.text
            assert not client.connected and not task.done()
            assert await bridge.come_up() == port
            await clock.advance(1.0)
            await until(lambda: hellos() == 2)
            assert bridge.connections == 2 and not task.done()
            await bridge.drop_client()
            await until(lambda: len(backoff.waits) == 3)
            assert backoff.waits == [0.5, 1.0, 0.5]  # the hello reset it
            await clock.advance(0.5)
            await until(lambda: hellos() == 3)
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            await bridge.stop()
    with caplog.at_level(logging.INFO, logger="leeroy_mind.bridge"):
        run(go())


def test_the_outbox_holds_events_while_no_mind_is_connected():
    """FakeBridge follows mod-leeroy's outbox: each event goes out once, after a greeting;
    what was published while no mind was connected waits for the next one."""
    async def go():
        async with session(events=[chat_event("scripted")]) as s:
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            await s.bridge.queue(chat_event("live"))
            await s.wait_for(lambda: [e.text for e in s.events if isinstance(e, Chat)]
                             == ["scripted", "live"])
            await s.bridge.drop_client()
            await s.bridge.queue(chat_event("while away 1"))
            await s.bridge.queue(chat_event("while away 2"))
            await s.wait_for(lambda: s.types().count("Hello") == 2)
            await s.wait_for(lambda: len([e for e in s.events if isinstance(e, Chat)]) == 4)
            after = s.types()[s.types().index("Hello", 1):]
            assert after == ["Hello", "Cast", "Chat", "Chat"]  # greeted first, then the outbox
            assert [e.text for e in s.events if isinstance(e, Chat)] == \
                ["scripted", "live", "while away 1", "while away 2"]  # each one once
    run(go())


def test_a_restarted_server_has_a_fresh_outbox():
    async def go():
        async with session() as s:
            await s.wait_for(lambda: s.types() == ["Hello", "Cast"])
            await s.bridge.go_down()
            await s.bridge.queue(chat_event("published by the new process"))
            await s.bridge.come_up(hello={"type": "hello", "server": "mod-leeroy",
                                          "version": "0.1.0", "guild_id": 1, "human_guid": 0})
            await s.wait_for(lambda: s.types().count("Chat") == 1)
            hellos = [e for e in s.events if isinstance(e, Hello)]
            assert [h.human_guid for h in hellos] == [1, 0] and s.bridge.connections == 2
    run(go())


def test_the_echo_can_arrive_before_the_ack():
    """Tests lens M11: mod-leeroy writes echoes and acks from separate queues."""
    async def go():
        async with session(bridge_kw=dict(echo_first=True)) as s:
            ack = await s.client.send(say("hello there", guid=101))
            await s.wait_for(lambda: any(isinstance(e, Chat) for e in s.events))
            assert ack.ok and [e.text for e in s.events if isinstance(e, Chat)] == ["hello there"]
            assert s.bridge.written[-2:] == ["chat", "ack"]  # the echo went first
    run(go())


def test_one_missed_pong_is_forgiven_and_two_reconnect():
    """Correctness M4: mod-leeroy answers pings on the world thread, so one slow world update
    must not drop the connection (and the lines in flight with it)."""
    async def go():
        pings = []

        def ack(cmd):
            if cmd["type"] == "ping":
                pings.append(cmd)
                return None if len(pings) == 1 else (True, "")  # the first pong never comes
            return True, ""
        async with session(bridge_kw=dict(ack=ack),
                           client_kw=dict(ping_interval=0.1, ack_timeout=0.15)) as s:
            await s.wait_for(lambda: len(pings) >= 3, timeout=3.0)
            assert s.bridge.connections == 1 and s.client.connected
    run(go())


def test_only_two_missed_pongs_in_a_row_drop_the_connection():
    """Correctness M4: a missed pong now and then (a slow world update each time) never drops
    the connection, however many there are; the second miss in a row does, at once."""
    async def go():
        pongs = [False, True, False, True, False, True, False, False]  # then always answered
        seen: list[int] = []  # the connection each ping came in on
        holder: list[FakeBridge] = []

        def ack(cmd):
            if cmd["type"] != "ping":
                return True, ""
            seen.append(holder[0].connections if holder else 1)
            answered = pongs[len(seen) - 1] if len(seen) <= len(pongs) else True
            return (True, "") if answered else None
        async with session(bridge_kw=dict(ack=ack),
                           client_kw=dict(ping_interval=0.1, ack_timeout=0.15)) as s:
            holder.append(s.bridge)
            await s.wait_for(lambda: len(seen) > len(pongs), timeout=8.0)
            assert seen[:len(pongs)] == [1] * len(pongs)  # three single misses: kept
            assert seen[len(pongs)] == 2 and s.bridge.connections == 2  # two in a row: dropped
    run(go())


def test_greeted_and_refused_say_whether_the_mind_is_served():
    """What the app's start banner reports: greeted once this connection's hello is in (and
    not between connections), refused when another mind holds the bridge."""
    async def go():
        async with session(client_kw=dict(backoff=Backoff(0.4, 0.4, jitter=0))) as s:
            await s.client.wait_greeted(2.0)
            assert s.client.greeted and not s.client.refused and s.client.queued >= 0
            await s.bridge.drop_client()
            await s.wait_for(lambda: not s.client.connected)
            assert not s.client.greeted
            await s.client.wait_greeted(2.0)  # the next connection's hello
            assert s.client.greeted
            second = BridgeClient("127.0.0.1", s.bridge.port, lambda ev: asyncio.sleep(0),
                                  backoff=Backoff(5, 5, jitter=0))
            task = asyncio.create_task(second.run())
            try:
                await s.wait_for(lambda: second.refused)
                assert not second.greeted and s.client.greeted
            finally:
                await second.close()
                await asyncio.wait_for(task, 2.0)
    run(go())


def test_a_second_mind_backs_off_for_minutes_and_says_so_once(clock, caplog):
    """Liveops M7: a mind refused because another holds the bridge (another data dir: the
    lock cannot see it) waits minutes between tries, not the reconnect backoff."""
    class Sleeps(type(clock)):
        def __init__(self, start):
            super().__init__(start)
            self.slept: list[float] = []

        async def sleep(self, s):
            self.slept.append(s)
            await super().sleep(s)

    async def go():
        bridge = FakeBridge()
        port = await bridge.start()
        first = BridgeClient("127.0.0.1", port, lambda ev: asyncio.sleep(0),
                             backoff=Backoff(**FAST))
        t1 = asyncio.create_task(first.run())
        await first.wait_connected(2.0)
        sleeps = Sleeps(clock.local())
        second = BridgeClient("127.0.0.1", port, lambda ev: asyncio.sleep(0),
                              backoff=Backoff(0.5, 30, jitter=0), clock=sleeps,
                              ping_interval=3600)
        t2 = asyncio.create_task(second.run())
        try:
            async def until(pred):
                async def poll():
                    while not pred():
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(poll(), 3.0)
            retries = lambda: [s for s in sleeps.slept if s != 3600]  # noqa: E731
            await until(lambda: retries())
            assert retries() == [BridgeClient.REFUSED_RETRY_S] and BridgeClient.REFUSED_RETRY_S >= 120
            await sleeps.advance(BridgeClient.REFUSED_RETRY_S, step=30)
            await until(lambda: len(retries()) == 2)
            assert retries()[1] == BridgeClient.REFUSED_RETRY_S
            assert caplog.text.count("bridge_refused") == 1  # once, not every try
            assert first.connected and bridge.connections == 1
        finally:
            await second.close()
            await first.close()
            await asyncio.wait_for(asyncio.gather(t1, t2), 3.0)
            await bridge.stop()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.bridge"):
        run(go())


def test_the_fake_bridge_never_listens_on_a_live_port():
    """Task 17 re-review: an ephemeral port can be 7878 or 11434 while the worldserver or
    Ollama is down; FakeBridge then binds again."""
    class FirstIsLive:
        def __init__(self):
            self.seen: list[int] = []

        def __contains__(self, port):
            self.seen.append(port)
            return len(self.seen) == 1

    async def go():
        bridge = FakeBridge()
        bridge.AVOID_PORTS = avoid = FirstIsLive()
        try:
            port = await bridge.start()
            assert len(avoid.seen) == 2 and port == avoid.seen[1] != avoid.seen[0]
        finally:
            await bridge.stop()
        assert {7878, 11434} <= set(FakeBridge.AVOID_PORTS)
    run(go())
