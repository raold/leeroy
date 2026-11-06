"""Real App row writes receive only their active native dispatch receipt."""

import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import HUMAN, run, seed_test_cast
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.ingress import chat_row_binding, game_row_binding
from leeroy_mind.protocol import Chat, parse_event_with_origin, register_event
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM

MS = 1793304000123


@dataclasses.dataclass(frozen=True, slots=True)
class IngressExtra:
    ts: int
    guid: int
    label: str


register_event("test_ingress_extra", IngressExtra)


@asynccontextmanager
async def application(tmp_path, clock):
    config = tmp_path / "mind.toml"
    config.write_text("[talk]\nchattiness = 0\n", encoding="utf-8")
    seed = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
    seed_test_cast(seed)
    seed.set_meta("human_guid", str(HUMAN))
    seed.close()
    app = MindApp(load_config(config), tmp_path, clock=clock, llm=FakeLLM())
    app.engine.on_event = AsyncMock()
    app.engine.on_human_chat = AsyncMock()
    app._start_session = AsyncMock()
    try:
        yield app
    finally:
        await app.stop()


def chat(**extra):
    return dict(type="chat", channel="guild", from_guid=HUMAN, from_name="Rook",
                to_guid=0, text="ready for the next pull", map=0, zone=1519, **extra)


async def read(app, body, *, epoch=7):
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"v": 1, "ts": MS, **body}).encode() + b"\n")
    reader.feed_eof()
    await app.bridge._read_loop(reader, transport_epoch=epoch)


async def deliver(app, body, *, epoch=7):
    await read(app, body, epoch=epoch)
    app.bridge._queue.put_nowait(None)
    await app.bridge._dispatch()


def chat_binding(row):
    return chat_row_binding(row["id"], **{key: row[key] for key in (
        "channel", "from_guid", "from_name", "to_guid", "text", "map", "zone", "origin")})


def test_normal_human_threading_preserves_exact_insert_receipt_and_bookkeeping_changes(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen = []
            async def observe(ev):
                receipt = app.row_receipt(ev)
                row = app.store.conn.execute("SELECT * FROM chat_line WHERE id=?",
                                             (receipt.row_id,)).fetchone()
                seen.append((ev, receipt, row["thread_id"], chat_binding(row)))
                app.store.conn.execute("UPDATE chat_line SET audience='[]', session_id=NULL WHERE id=?",
                                       (receipt.row_id,))
                app.store.conn.commit()
                seen.append(app.row_receipt(ev))
            app.register_event_handler("chat", observe)
            await deliver(app, chat())
            assert len(seen) == 2
            ev, receipt, thread, digest = seen[0]
            assert thread is not None and receipt.row_kind == "chat_line"
            assert receipt.row_sha256 == digest and receipt is seen[1] and not receipt.backlog
            assert receipt.wire.transport_epoch == 7 and receipt.wire.server_ms == MS
            assert app.confirmed_human_guid == HUMAN
            assert app.row_receipt(ev) is None and app.bridge.delivery_origin(ev) is None
    run(scenario())


def test_changed_source_text_cannot_refresh_the_captured_binding(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            before, seen = [], []
            async def mutate(row):
                original = app.store.conn.execute("SELECT * FROM chat_line WHERE id=?", (row.id,)).fetchone()
                before.append(chat_binding(original))
                app.store.conn.execute("UPDATE chat_line SET text='changed source' WHERE id=?", (row.id,))
                app.store.conn.commit()
            app.engine.on_human_chat = mutate
            async def observe(ev):
                receipt = app.row_receipt(ev)
                current = app.store.conn.execute("SELECT * FROM chat_line WHERE id=?",
                                                 (receipt.row_id,)).fetchone()
                seen.append((receipt, app.row_receipt(ev), chat_binding(current)))
            app.register_event_handler("chat", observe)
            await deliver(app, chat())
            assert len(seen) == 1 and len(before) == 1
            receipt, again, changed = seen[0]
            assert receipt is again and receipt.row_sha256 == before[0]
            assert receipt.row_sha256 != changed
    run(scenario())


def test_changed_game_type_and_payload_cannot_reissue_a_valid_binding(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock) as app:
            before, seen = [], []
            real = app.world.apply
            def mutate(ev, now):
                row = app.store.conn.execute("SELECT * FROM game_event").fetchone()
                before.append(game_row_binding(row["id"], row["type"], json.loads(row["payload"])))
                app.store.conn.execute("UPDATE game_event SET type='resurrect', payload=? WHERE id=?",
                                       (json.dumps({"ts": MS, "guid": 999}), row["id"]))
                app.store.conn.commit()
                real(ev, now)
            monkeypatch.setattr(app.world, "apply", mutate)
            async def observe(ev):
                receipt = app.row_receipt(ev)
                row = app.store.conn.execute("SELECT * FROM game_event WHERE id=?", (receipt.row_id,)).fetchone()
                seen.append((receipt, app.row_receipt(ev), game_row_binding(
                    row["id"], row["type"], json.loads(row["payload"]))))
            app.register_event_handler("death", observe)
            await deliver(app, dict(type="death", guid=HUMAN, map=0, killer_entry=88))
            assert len(seen) == len(before) == 1
            receipt, repeated, mutated = seen[0]
            assert receipt is repeated and receipt.row_sha256 == before[0] != mutated
    run(scenario())


@pytest.mark.parametrize("body", [
    dict(type="death", guid=HUMAN, map=0, killer_entry=88),
    dict(type="afk", guid=HUMAN, on=True),
    dict(type="text_emote", guid=HUMAN, emote="wave", target_guid=0),
    dict(type="login", guid=HUMAN, human=True),
    dict(type="logout", guid=HUMAN, human=True),
    dict(type="login", guid=102, human=False),
    dict(type="test_ingress_extra", guid=102, label="native extension"),
])
def test_each_game_row_writing_branch_binds_exact_stored_payload(tmp_path, clock, body):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen = []
            async def observe(ev):
                receipt = app.row_receipt(ev)
                seen.append(receipt)
            app.register_event_handler(body["type"], observe)
            await deliver(app, body)
            assert len(seen) == 1 and seen[0] is not None
            receipt = seen[0]
            row = app.store.conn.execute("SELECT * FROM game_event WHERE id=?", (receipt.row_id,)).fetchone()
            assert receipt.row_kind == "game_event" and not receipt.backlog
            assert receipt.row_sha256 == game_row_binding(row["id"], row["type"], json.loads(row["payload"]))
    run(scenario())


@pytest.mark.parametrize("kind", ["chat", "game"])
def test_receipt_uses_actual_insert_return_not_a_later_row(tmp_path, clock, monkeypatch, kind):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen, inserted = [], []
            method = "insert_chat_line" if kind == "chat" else "insert_game_event"
            real = getattr(app.store, method)
            def insert_with_later_row(ev, **kwargs):
                row_id = real(ev, **kwargs)
                decoy = dataclasses.replace(ev, **({"text": "later decoy"} if kind == "chat" else {"guid": 999}))
                inserted.append((row_id, real(decoy, **kwargs)))
                return row_id
            monkeypatch.setattr(app.store, method, insert_with_later_row)
            body = chat() if kind == "chat" else dict(type="death", guid=HUMAN, map=0, killer_entry=88)
            async def observe(ev):
                seen.append(app.row_receipt(ev))
            app.register_event_handler(body["type"], observe)
            await deliver(app, body)
            assert len(seen) == 1 and len(inserted) == 1
            assert seen[0].row_id == inserted[0][0] < inserted[0][1]
    run(scenario())


@pytest.mark.parametrize("row_kind", ["game_event", "chat_line"])
def test_native_receipt_read_does_not_pin_wal_snapshot_before_another_connection_writes(
        tmp_path, clock, monkeypatch, row_kind):
    async def scenario():
        async with application(tmp_path, clock) as app:
            body = (dict(type="test_ingress_extra", guid=999, label="native extension")
                    if row_kind == "game_event" else {**chat(), "from_guid": 999})
            method = "insert_game_event" if row_kind == "game_event" else "insert_chat_line"
            real_insert = getattr(app.store, method)
            inserted, receipts = [], []
            def insert(ev, **kwargs):
                row_id = real_insert(ev, **kwargs)
                inserted.append(row_id)
                return row_id
            monkeypatch.setattr(app.store, method, insert)
            async def observe(ev):
                receipts.append(app.row_receipt(ev))
            # These short row paths return directly after capture. Keep the
            # observation hook free of reads so no later handler can release a
            # stale App snapshot and hide the regression.
            app._handlers[body["type"]] = [observe]
            await deliver(app, body)
            assert len(receipts) == len(inserted) == 1
            receipt = receipts[0]
            assert receipt is not None and receipt.row_id == inserted[0]
            assert receipt.row_kind == row_kind and receipt.wire.transport_epoch == 7

            other = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
            try:
                other.set_meta("ingress_other_connection", "committed")
                # No App Store read or commit is allowed between dispatch and
                # this write. A receipt SELECT left open at depth zero causes
                # SQLITE_BUSY_SNAPSHOT here after the other connection commits.
                app.store.set_meta("ingress_app_connection", "committed")
                assert other.get_meta("ingress_other_connection") == "committed"
                assert other.get_meta("ingress_app_connection") == "committed"
                row = other._one(f"SELECT * FROM {row_kind} WHERE id=?", (receipt.row_id,))
                digest = (game_row_binding(row["id"], row["type"], json.loads(row["payload"]))
                          if row_kind == "game_event" else chat_binding(row))
                assert receipt.row_sha256 == digest
                if row_kind == "chat_line":
                    assert row["origin"] == "game"
            finally:
                other.close()
    run(scenario())


@pytest.mark.parametrize("stage", ["world", "engine", "thread"])
def test_insert_then_row_failure_still_runs_ordinary_handlers_without_receipt(tmp_path, clock, monkeypatch, stage):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen = []
            body = dict(type="death", guid=HUMAN, map=0, killer_entry=88)
            if stage == "world":
                monkeypatch.setattr(app.world, "apply", Mock(side_effect=RuntimeError("row failed")))
            elif stage == "engine":
                body = chat()
                app.engine.on_human_chat = AsyncMock(side_effect=RuntimeError("row failed"))
            else:
                body = chat()
                monkeypatch.setattr(app.threads, "add", Mock(side_effect=RuntimeError("row failed")))
            async def observe(ev):
                seen.append((ev, app.row_receipt(ev)))
            app.register_event_handler(body["type"], observe)
            await deliver(app, body)
            table = "game_event" if stage == "world" else "chat_line"
            assert app.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
            assert len(seen) == 1 and seen[0][1] is None
            assert app.row_receipt(seen[0][0]) is None
    run(scenario())


def test_before_handlers_children_copies_and_outside_tasks_have_no_row_authority(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen, child = [], []
            entered, release = asyncio.Event(), asyncio.Event()
            async def before(ev):
                seen.append(("before", app.row_receipt(ev)))
            async def child_read(ev):
                child.append((app.row_receipt(ev), app.bridge.delivery_origin(ev)))
            async def after(ev):
                seen.append(("after", app.row_receipt(ev), app.row_receipt(dataclasses.replace(ev)), ev))
                await asyncio.create_task(child_read(ev))
                entered.set()
                await release.wait()
            app.register_event_handler("login", before, before=True)
            app.register_event_handler("login", after)
            await read(app, dict(type="login", guid=102, human=False))
            app.bridge._queue.put_nowait(None)
            dispatch = asyncio.create_task(app.bridge._dispatch())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                ev = seen[-1][-1]
                assert app.row_receipt(ev) is None and app.bridge.delivery_origin(ev) is None
                assert seen[0] == ("before", None)
                assert seen[1][0] == "after" and seen[1][1] is not None and seen[1][2] is None
                assert child == [(None, None)]
            finally:
                release.set()
                await dispatch
            assert app.row_receipt(ev) is None and app._row_delivery is None
    run(scenario())


@pytest.mark.parametrize("route", ["echo", "whisper", "injected", "stopping", "unbound"])
def test_non_source_routes_cannot_issue_receipt(tmp_path, clock, monkeypatch, route):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen, shutdown = [], []
            body = chat()
            if route == "echo":
                monkeypatch.setattr(app.engine, "is_echo", Mock(return_value=True))
            if route == "whisper":
                body.update(channel="whisper", to_guid=999)
            if route == "stopping":
                app.register_shutdown_receipt("chat", lambda ev: shutdown.append(app.row_receipt(ev)))
                app._stopping = True
            async def observe(ev):
                seen.append(app.row_receipt(ev))
            app.register_event_handler("chat", observe)
            if route == "injected":
                ev, origin = parse_event_with_origin(json.dumps({"v": 1, "ts": MS, **body}),
                                                     received_at=clock.now(), transport_epoch=7)
                assert origin is not None  # public construction itself is not authority
                app.bridge.inject(ev)
                item = app.bridge._queue.get_nowait()
                assert len(item) == 3 and item[2] is None
                app.bridge._queue.put_nowait(item)
                app.bridge._queue.put_nowait(None)
                await app.bridge._dispatch()
            else:
                if route == "unbound":
                    body["ts"] = 0
                await deliver(app, body)
            expected_seen = [] if route in ("whisper", "stopping") else [None]
            assert seen == expected_seen
            assert shutdown == ([None] if route == "stopping" else [])
            count = app.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0]
            assert count == (0 if route in ("echo", "whisper") else 1)
            assert app._row_delivery is None
    run(scenario())


def test_backlog_receipt_uses_server_time_and_captures_backlog_status(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            app._hello_ts = MS + 6000
            seen = []
            async def observe(ev):
                seen.append(app.row_receipt(ev))
            app.register_event_handler("chat", observe)
            await deliver(app, chat())
            assert len(seen) == 1 and seen[0].backlog
            row = app.store.conn.execute("SELECT ts FROM chat_line WHERE id=?", (seen[0].row_id,)).fetchone()
            assert row[0] == min(clock.now(), MS / 1000)
            app.engine.on_human_chat.assert_not_awaited()
    run(scenario())


def test_cancellation_after_insert_clears_both_bindings_without_exposing_receipt(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            entered = asyncio.Event()
            row_stage, ordinary = [], []
            async def blocked(row):
                row_stage.append(app._row_delivery.event)
                entered.set()
                await asyncio.Event().wait()
            app.engine.on_human_chat = blocked
            async def observe(ev):
                ordinary.append(app.row_receipt(ev))
            app.register_event_handler("chat", observe)
            await read(app, chat())
            dispatch = asyncio.create_task(app.bridge._dispatch())
            await asyncio.wait_for(entered.wait(), 2)
            ev = row_stage[0]
            assert app.row_receipt(ev) is None
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
            assert app.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == 1
            assert ordinary == [] and app._row_delivery is None
            assert app.row_receipt(ev) is None and app.bridge.delivery_origin(ev) is None
    run(scenario())
