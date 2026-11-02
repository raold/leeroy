import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.protocol import Ack, DcStatus
from leeroy_mind.training import service
from leeroy_mind.training.listener import TrainingListener
from leeroy_mind.training.ports import EventWaiter, LivePorts
from leeroy_mind.training.protocol import GetStats, ServerStats, TG_EVENT_TYPES
from leeroy_mind.training.spells import SpellBook

from .test_tg_gate import _run_async, boss, run_for
from .test_tg_ledger import header, member


def enable(app):
    (app.data_dir / "mind.toml").write_text("[training]\nenabled = true\n", encoding="utf-8")
    app.skip_event_rows = set()
    app.hooks = []
    app.engine.add_instruction_hook = app.hooks.append
    return app


def test_attach_off_by_default(app_stub):
    assert service.attach_training(app_stub) is None
    assert not app_stub.handlers and not app_stub.backgrounds and not app_stub.connections


def test_attach_registers_without_tasks_or_data_load(app_stub, monkeypatch):
    enable(app_stub)
    def forbidden(*args, **kwargs):
        raise AssertionError("attachment must not start IO or tasks")
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    monkeypatch.setattr(service, "load_book", forbidden)
    ground = service.attach_training(app_stub)
    assert TG_EVENT_TYPES | {"hello", "boss", "login", "logout"} <= app_stub.handlers.keys()
    assert app_stub.backgrounds == [ground.run]
    assert app_stub.connections == [ground.on_connection]
    assert TG_EVENT_TYPES <= app_stub.skip_event_rows
    assert len(app_stub.hooks) == 1
    assert ground.book() is None and ground.watch_ids() == () and ground.training_members() == set()


def test_listener_added_after_existing_listeners_and_report_registered(app_stub, monkeypatch):
    import leeroy_mind.eval as evaluation
    enable(app_stub)
    lists = [[object(), object(), object()] for _ in range(2)]
    ports = LivePorts(app_stub).guild
    app_stub.guild = SimpleNamespace(ports=ports,
        raid_night=SimpleNamespace(add_listener=lists[0].append),
        offline=SimpleNamespace(add_listener=lists[1].append))
    registered = []
    monkeypatch.setattr(evaluation, "register_section", lambda *args: registered.append(args))
    ground = service.attach_training(app_stub)
    assert all(len(rows) == 5 and rows[-2:] == [ground.listener, ground.voice.lifecycle] for rows in lists)
    assert registered == [("training", service.report_section)]


@_run_async
async def test_extension_rows_skipped_in_running_and_stopping_app(app, fake):
    from leeroy_mind.guild.protocol import Job
    service.attach_training(enable(app))
    h = header(fake)
    await app.handle(member(h))
    assert app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='pull_member'").fetchone()[0] == 0
    job = Job(h.ts, "test-job", "raid", "done", "", "", (), (), 0, 0, 0, 0, 0, 0)
    await app.handle(job)
    assert app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='job'").fetchone()[0] == 1
    app._record_only(member(h), "pull_member")
    assert app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='pull_member'").fetchone()[0] == 0


@_run_async
async def test_checks_and_score_precede_downstream_and_rescore_restores(fake):
    ground = service.TrainingGround(SimpleNamespace(), fake)
    h = header(fake)
    await ground.ledger.on_event(member(h))
    await ground.ledger.on_event(h)
    row = fake.conn.execute("SELECT * FROM tg_pull").fetchone()
    assert row["checks_version"] == 0 and row["score"] is None
    assert await service.rescore_stored(fake, SpellBook()) == 1
    row = fake.conn.execute("SELECT * FROM tg_pull").fetchone()
    assert row["checks_version"] == 1 and row["score"] is not None and row["score_basis"] == "deaths_time"
    ground._book = SpellBook()
    seen = []
    async def after(rec, ctx):
        seen.append(dict(fake.conn.execute("SELECT * FROM tg_pull WHERE id=?", (rec.header.id,)).fetchone()))
    ground.ledger.after_store.append(after)
    h = header(fake, start=h.start_ms + 100_000)
    await ground.ledger.on_event(member(h))
    await ground.ledger.on_event(h)
    assert seen[0]["checks_version"] == 1 and seen[0]["score"] is not None


@_run_async
async def test_book_waits_for_offline_and_rescores_on_loader_lane(fake, tmp_path, monkeypatch):
    fake.online = True
    ground = service.TrainingGround(SimpleNamespace(data_dir=tmp_path), fake)
    ground._connected = ground._presence_known = True
    calls = []
    monkeypatch.setattr(service, "load_book", lambda path: calls.append(path) or SpellBook())
    task = asyncio.create_task(ground._load())
    await asyncio.sleep(0)
    assert not calls
    fake.online = False
    await fake.clock.advance(1)
    await asyncio.wait_for(task, 2)
    assert calls == [tmp_path] and isinstance(ground.book(), SpellBook)


@_run_async
async def test_listener_maps_dc_names_and_retries_only_valid_bound_fights(fake):
    gate = SimpleNamespace(on_start=AsyncMock(), on_muster=AsyncMock(), on_boss=AsyncMock(),
        on_end=AsyncMock(), invalidate=lambda run: None)
    ledger = SimpleNamespace(set_run=lambda run: None)
    listener = TrainingListener(fake, ledger, gate)
    run = run_for(fake)
    await listener.on_start(run)
    ev = DcStatus(int(fake.clock.now() * 1000), 101, True, False,
                  "STATUS\t0\t36612\tLord Marrowgar\t0\t0\tresting")
    await listener.on_dc_status(ev)
    await listener.on_dc_status(ev)
    gate.on_muster.assert_awaited_once_with(run, 0)
    await listener.on_boss(boss(fake, instance_id=999))
    assert gate.on_boss.await_count == 0
    await listener.on_boss(boss(fake))
    await listener.on_dc_status(ev)
    assert gate.on_muster.await_count == 1
    await listener.on_boss(boss(fake, state="wiped"))
    await listener.on_dc_status(ev)
    assert gate.on_muster.await_count == 2
    await listener.on_dc_status(replace(ev, payload=ev.payload.replace("Lord Marrowgar", "Unknown")))
    assert gate.on_muster.await_count == 2


@_run_async
async def test_hello_handler_returns_before_command_ack_and_disconnect_joins(app_stub):
    ground = service.attach_training(enable(app_stub))
    await ground.on_connection(True)
    entered, finished = asyncio.Event(), asyncio.Event()
    async def send(cmd):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    ground.ports.guild.send = send
    worker = asyncio.create_task(ground._commands())
    ground.submit("on_hello")
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(ground.on_connection(False), 1)
    assert finished.is_set() and not ground.gate.recording() and not ground._queue
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


@_run_async
async def test_old_run_queued_work_and_late_end_cannot_disarm_replacement(app_stub):
    ground = service.attach_training(enable(app_stub))
    await ground.on_connection(True)
    first, second = run_for(ground.ports), run_for(ground.ports, 2)
    await ground.listener.on_start(first)
    ground.listener.invalidate(first)
    await ground.listener.on_start(second)
    await ground.listener.on_end("cancelled", run=first)
    worker = asyncio.create_task(ground._commands())
    for _ in range(10):
        await asyncio.sleep(0)
    assert ground.gate._run is second and ground.listener.run is second
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


@_run_async
async def test_request_early_reply_wrong_ack_and_repeated_public_ids(app_stub):
    ports = LivePorts(app_stub)
    ids = []
    async def send(cmd):
        ids.append(cmd.id)
        ports.feed(ServerStats(0, cmd.id, 1, 1, False, 0))
        return Ack(0, cmd.id, True, "")
    ports.guild.send = send
    for _ in range(2):
        rows = await ports.request(GetStats("public"), "server_stats", until=lambda ev: ev.id == "public", timeout_s=1)
        assert len(rows) == 1 and rows[0].id == "public"
    assert ids[0] != ids[1]
    ports.guild.send = AsyncMock(return_value=Ack(0, "wrong", True, ""))
    assert await ports.request(GetStats("bad"), "server_stats", until=lambda ev: True, timeout_s=1) == []
    assert not ports._requests and not ports.waiter._streams


@_run_async
async def test_request_disconnect_cancels_and_cleans_mailbox(app_stub):
    ports = LivePorts(app_stub)
    entered = asyncio.Event()
    async def send(cmd):
        entered.set()
        return Ack(0, cmd.id, True, "")
    ports.guild.send = send
    task = asyncio.create_task(ports.request(GetStats("pending"), "server_stats", until=lambda ev: True, timeout_s=20))
    await entered.wait()
    await ports.disconnect()
    assert task.cancelled() and not ports._requests and not ports.waiter._streams


def test_optional_services_are_late_bound(app_stub):
    ports = LivePorts(app_stub)
    assert ports.claim("test", [101], until=5) == {101}
    assert ports.holder(101) is None and ports.benchmark_allowed(631, 0, 10, False) is None
    assert ports.on_comms(101) is False and ports.mentor_of(101) is None
    claims = []
    app_stub.bodies = SimpleNamespace(claim=lambda *args, **kwargs: claims.append((args, kwargs)) or set(),
                                     holder=lambda guid: "realm", release=lambda *args: None)
    assert ports.claim("test", [101], until=5) == set()
    assert claims == [(("test", [101]), {"rank": "training", "until": 5})]
    assert ports.holder(101) == "realm"


@_run_async
async def test_real_bridge_preserved_id_boundary_and_reconnect(app_stub):
    from leeroy_mind.bridge import BridgeClient
    from leeroy_mind.training.protocol import register
    register()
    bridge = BridgeClient("127.0.0.1", 1, AsyncMock(), clock=app_stub.clock)
    app_stub.bridge = bridge
    ports = LivePorts(app_stub)
    ids = []
    def write(line):
        cmd = json.loads(line)
        ids.append(cmd["id"])
        ports.feed(ServerStats(0, cmd["id"], 1, 1, False, 0))
        bridge._resolve(cmd["id"], Ack(0, cmd["id"], True, ""))
    bridge._writer = SimpleNamespace(write=write, drain=AsyncMock())
    bridge._connected = True
    for _ in range(2):
        rows = await ports.request(GetStats("caller"), "server_stats", until=lambda ev: True, timeout_s=1)
        assert len(rows) == 1
        await ports.disconnect()
    assert ids[0] != ids[1] and ports.connection_epoch == 2
    assert not bridge._pending


@_run_async
async def test_loader_requires_observed_presence_even_if_world_defaults_offline(app_stub, monkeypatch):
    from leeroy_mind.protocol import Hello
    ground = service.attach_training(enable(app_stub))
    app_stub.world.human_online = False
    calls = []
    monkeypatch.setattr(service, "load_book", lambda path: calls.append(path) or SpellBook())
    loader = asyncio.create_task(ground._load())
    await ground.on_connection(True)
    await app_stub.clock.advance(2)
    assert not calls
    epoch = ground.ports.connection_epoch
    await ground.on_event(Hello(int(app_stub.clock.now() * 1000), "test", "1", 21, 0))
    assert ground.ports.connection_epoch == epoch + 1
    await app_stub.clock.advance(1)
    await asyncio.wait_for(loader, 2)
    assert len(calls) == 1


@_run_async
async def test_request_pins_early_prefix_across_mailbox_pressure_before_ack(app_stub):
    ports = LivePorts(app_stub)
    async def send(cmd):
        ports.feed(ServerStats(0, cmd.id, 1, 1, False, 0))
        for index in range(EventWaiter.MAX_STREAMS * 3):
            ports.feed(ServerStats(0, f"unrelated_{index}", 9, 9, False, 0))
        ports.feed(ServerStats(0, cmd.id, 2, 2, False, 0))
        assert len(ports.waiter._streams) <= EventWaiter.MAX_STREAMS
        await asyncio.sleep(0)
        return Ack(0, cmd.id, True, "")
    ports.guild.send = send
    rows = await ports.request(GetStats("public"), "server_stats", until=lambda ev: ev.mean_ms == 2, timeout_s=1)
    assert [ev.mean_ms for ev in rows] == [1, 2]
    assert not ports._requests
    assert not any(stream.reserved for stream in ports.waiter._streams.values())


@_run_async
async def test_request_reserved_prefix_overflow_fails_after_ack(app_stub):
    ports = LivePorts(app_stub)
    async def send(cmd):
        for index in range(EventWaiter.MAX_EVENTS + 1):
            ports.feed(ServerStats(0, cmd.id, index, index, False, 0))
        return Ack(0, cmd.id, True, "")
    ports.guild.send = send
    with pytest.raises(BufferError):
        await ports.request(GetStats("overflow"), "server_stats", until=lambda ev: True, timeout_s=1)
    assert not ports._requests and not ports.waiter._streams


def test_event_reservations_are_bounded_and_released():
    waiter = EventWaiter()
    for index in range(waiter.MAX_STREAMS):
        waiter.reserve(str(index), "server_stats")
    with pytest.raises(BufferError):
        waiter.reserve("full", "server_stats")
    with pytest.raises(ValueError):
        waiter.reserve("0", "server_stats")
    waiter.release("0", "server_stats")
    waiter.reserve("available", "server_stats")
    assert len(waiter._streams) == waiter.MAX_STREAMS
