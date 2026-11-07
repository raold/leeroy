"""Actual native reader -> App row -> dispatcher capture -> owned Realm reducer.

Only the byte stream/writer and model are test boundaries. Observation spies and
race gates delegate to the real methods and never issue receipts or approvals.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
import json
import logging
import sqlite3

import pytest

from conftest import run as run_loop, seed_test_cast
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.ingress import game_row_binding
from leeroy_mind.protocol import parse_event
from leeroy_mind.realm import fact_store
from leeroy_mind.realm.kill_retention import PrivateKillRecord
from leeroy_mind.realm.records import FactRow, RealmObservation, Rejected
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM
from test_realm_live_proof import EXPECTED, PAYLOADS, TYPES, insert

TABLES = ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill')
KILL = 'verified_raid_kill_v1'
PRIVATE = dict(human_present=False, cast_members=[101, 102, 103, 104])


def run(scenario):
    # A gate left shut by a failed assertion must fail the test, not hang the suite.
    return run_loop(asyncio.wait_for(scenario, 60))


@contextmanager
def spied(native):
    """App logs and swallows a handler's failure, so keep a spy's for the test."""
    try:
        yield
    except AssertionError as error:
        native.spy_errors.append(error)
        raise


def envelope(policy='bg_result_v1', **changes):
    if policy == KILL and changes.get('human_present') is False:
        changes = {'fight_started_at_ms': 0, 'fight_ms': -1, **changes}
    return dict(v=1, type=TYPES[policy][0], **{**PAYLOADS[policy], **changes})


def counts(store):
    with store.transaction():
        return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in TABLES)


def state(store):
    with store.transaction():
        return tuple(tuple(tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1'))
                     for table in TABLES)


async def reached(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError('native test boundary did not become ready')


class Writer:
    def __init__(self):
        self.closed = False
        self.sent = []

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass


class Native:
    def __init__(self, app):
        self.app = app
        self.bridge = app.bridge
        self.session = None
        self.writers = []
        self.captures = []
        self.results = []
        self.failures = []
        self.spy_errors = []

    async def connect(self):
        self.reader = asyncio.StreamReader()
        self.writer = Writer()
        self.writers.append(self.writer)
        self.session = asyncio.create_task(self.bridge._session(self.reader, self.writer))
        await reached(lambda: self.bridge._writer is self.writer)

    async def disconnect(self):
        self.reader.feed_eof()
        await self.session
        self.session = None

    async def read(self, body):
        raw = body if isinstance(body, str) else json.dumps(body)
        queued = self.bridge._queue.qsize()
        self.reader.feed_data(raw.encode('utf-8') + b'\n')
        await reached(lambda: self.bridge._queue.qsize() > queued)

    async def drain(self):
        self.bridge._queue.put_nowait(None)
        await self.bridge._dispatch()

    async def deliver(self, body):
        await self.read(body)
        await self.drain()

    async def hello(self, *, human=0, guild=50, ts=1000000):
        await self.deliver(dict(v=1, type='hello', ts=ts, server='Synthetic', version='1',
                                guild_id=guild, human_guid=human))


@asynccontextmanager
async def application(tmp_path, clock, monkeypatch, *, enabled=True, confirmed=True,
                      establish=True):
    path = tmp_path / 'mind.toml'
    path.write_text('[talk]\nchattiness = 0\n[realm]\nenabled = '
                    + str(enabled).lower() + '\n', encoding='utf-8')
    seed = Store(open_migrated(tmp_path / 'mind.db'), clock=clock)
    seed_test_cast(seed)
    with seed.transaction():
        insert(seed, 'cast_member', guid=10, name='Testmaster', race=2, **{'class': 1},
               gender=0, level=80, rank=0, online=0, updated=1000.0)
        for guild in (50, 60):
            insert(seed, 'realm_guild', id=guild, role='progression', faction='horde',
                   name='Synthetic Guild', added=1000.0, status='active')
        insert(seed, 'realm_actor', guid=201, guild=60, name='Rivalname', role='officer',
               added=1000.0, status='active')
        if confirmed:
            seed.set_meta('human_guid', '10')
    seed.close()
    llm = FakeLLM()
    app = MindApp(load_config(path), tmp_path, clock=clock, llm=llm)
    native = Native(app)
    native.llm = llm
    realm = app.realm
    capture = realm._capture_source_observation
    reduce = realm._on_source_observation

    def spy_capture(ev):
        result = capture(ev)
        if type(result) is RealmObservation:
            with spied(native):
                assert app.row_receipt(ev) is result.receipt
                assert app.bridge.delivery_origin(ev) is result.receipt.wire
            native.captures.append((result, asyncio.current_task()))
        return result

    async def spy_reduce(observation):
        with spied(native):
            assert app.row_receipt(observation.event) is None
            assert native.bridge.delivery_origin(observation.event) is None
            assert asyncio.current_task() in realm._children
            assert asyncio.current_task() is not native.captures[-1][1]
        try:
            result = await reduce(observation)
        except Exception as error:
            native.failures.append(type(error))
            raise
        native.results.append(result)
        return result

    monkeypatch.setattr(realm, '_capture_source_observation', spy_capture)
    monkeypatch.setattr(realm, '_on_source_observation', spy_reduce)
    # Distinct namespaces: actual connection begins at Bridge 7, Realm 1;
    # the native kill fixture retains native configuration epoch 29.
    app.bridge._transport_epoch = 6
    try:
        await native.connect()
        await native.drain()
        if establish:
            await native.hello()
        yield native
    except BaseException as error:
        if native.spy_errors:
            error.add_note(f'spy assertions failed: {native.spy_errors!r}')
        raise
    else:
        assert not native.spy_errors, native.spy_errors
    finally:
        async with asyncio.timeout(20):
            if native.session is not None:
                await native.disconnect()
            if not app.stopping:
                await native.drain()
            await app.stop()


def pause_source(native, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = native.app.realm._on_source_observation

    async def gate(observation):
        entered.set()
        await release.wait()
        return await original(observation)

    monkeypatch.setattr(native.app.realm, '_on_source_observation', gate)
    return entered, release


@pytest.mark.parametrize('policy', TYPES)
def test_six_native_policies_bind_actual_row_and_write_only_original_fact_state(
        tmp_path, clock, monkeypatch, policy):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            assert not app.model_ready and not app.world.human_online
            await native.deliver(envelope(policy))
            assert len(native.captures) == len(native.results) == 1
            observation = native.captures[0][0]
            result = native.results[0]
            assert type(result) is FactRow and result.text == EXPECTED[policy][0]
            assert observation.context.connection_epoch == 1
            assert observation.receipt.wire.transport_epoch == observation.qualification.transport_epoch == 7
            with app.store.transaction():
                source = app.store.conn.execute('SELECT * FROM realm_source').fetchone()
                row = app.store.conn.execute('SELECT * FROM game_event WHERE id=?',
                                             (observation.receipt.row_id,)).fetchone()
                origin = app.store.conn.execute('SELECT * FROM realm_fact_origin').fetchone()
                assert source['game_event_id'] == row['id']
                assert source['source_key'] == observation.receipt.wire.envelope_sha256
                assert observation.receipt.row_sha256 == game_row_binding(
                    row['id'], row['type'], json.loads(row['payload']))
                assert origin['source'] == source['id'] and origin['policy'] == policy
                assert json.loads(origin['qualification_json'])['known_by'] == EXPECTED[policy][1]
                assert [list(r) for r in app.store.conn.execute(
                    'SELECT guild,how FROM realm_knows ORDER BY guild')] == EXPECTED[policy][1]
                for table in ('memory', 'utterance', 'chat_line'):
                    assert app.store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
                if policy == KILL:
                    kill = app.store.conn.execute('SELECT * FROM realm_kill').fetchone()
                    assert kill['native_epoch'] == '29' and kill['fact'] == result.id
            assert counts(app.store) == (1, 1, 1, len(EXPECTED[policy][1]), int(policy == KILL))
            assert app._handlers['bg_end'].count(app.realm.on_event) == 1
            assert not native.llm.calls and not any(w.sent for w in native.writers)
            assert not app.realm._children
            # BgEnd's pre-existing ordinary App memory queue is unchanged.
            assert app._memory_queue.qsize() == int(policy == 'bg_result_v1')
    run(scenario())


@pytest.mark.parametrize('cast,human,public', [([101], False, False),
    ([101, 102, 103, 104], False, False), ([101, 102, 103, 104, 105], False, True),
    ([], True, True)])
def test_native_kill_initial_route_and_complete_private_capture(
        tmp_path, clock, monkeypatch, cast, human, public):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            await native.deliver(envelope(KILL, cast_members=cast, human_present=human))
            result = native.results[-1]
            if public:
                assert type(result) is FactRow
                assert counts(native.app.store) == (1, 1, 1, 2, 1)
            else:
                assert type(result) is PrivateKillRecord
                obs = native.captures[-1][0]
                assert result.event == obs.event
                assert result.capture.native_present == tuple(cast)
                assert result.capture.qualified_cast == tuple(cast)
                assert result.capture.snapshot == obs.qualification
                assert result.capture.context == obs.context
                with native.app.store.transaction():
                    row = native.app.store.conn.execute('SELECT * FROM realm_kill').fetchone()
                    assert row['fact'] is None and row['qualification_json']
                    assert native.app.realm._private_kills.get_in_unit(obs.event.kill_id) == result
                assert counts(native.app.store) == (1, 0, 0, 0, 1)
    run(scenario())


@pytest.mark.parametrize('original_public', [False, True])
def test_retained_kill_replay_preserves_original_route_after_qualification_change(
        tmp_path, clock, monkeypatch, original_public):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store = native.app.store
            body = envelope(KILL, human_present=False)
            if not original_public:
                with store.transaction():
                    store.conn.execute('UPDATE cast_member SET level=79 WHERE guid=105')
            await native.deliver(body)
            first, original = native.results[-1], state(store)
            assert type(first) is (FactRow if original_public else PrivateKillRecord)
            with store.transaction():
                store.conn.execute('UPDATE cast_member SET level=? WHERE guid>=101',
                                   (79 if original_public else 80,))
                store.conn.execute("UPDATE realm_actor SET status='retired'")
            # A later hello makes the same native event backlog; key order and
            # receive time changes do not manufacture an alternate native key.
            await native.hello(ts=1010000)
            await clock.advance(1)
            await native.deliver(json.dumps(dict(reversed(list(body.items()))), indent=2).replace('\n', ' '))
            assert native.captures[-1][0].receipt.backlog
            assert native.captures[0][0].receipt.wire.envelope_sha256 == native.captures[-1][0].receipt.wire.envelope_sha256
            assert native.results[-1] == first and state(store) == original
    run(scenario())


@pytest.mark.parametrize('policy', TYPES)
def test_first_backlog_rejected_but_retained_public_replay_reaches_original(
        tmp_path, clock, monkeypatch, policy):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            body = envelope(policy)
            await native.deliver(body)
            first, original = native.results[-1], state(native.app.store)
            await native.hello(ts=1010000)
            await native.deliver(body)
            assert native.results[-1] == first and state(native.app.store) == original
            # Same exact policy at a different server timestamp is a fresh key.
            await native.deliver(envelope(policy, ts=1000001))
            assert type(native.results[-1]) is Rejected
            assert state(native.app.store) == original
    run(scenario())


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('change', [dict(native_epoch=30), dict(guild_id=51), dict(human_guid=11),
    dict(killed_at_ms=999989), dict(verified_at_ms=999998), dict(fight_started_at_ms=998999),
    dict(fight_ms=2346), dict(map=533), dict(instance_id=10), dict(boss_id=1),
    dict(difficulty=-1), dict(size=25, difficulty=1), dict(heroic=True, difficulty=2),
    dict(human_present=False, fight_started_at_ms=0, fight_ms=-1),
    dict(cast_members=[101, 102]), dict(ts=1000001)])
def test_same_native_id_changed_immutable_field_never_claims_another_source(
        tmp_path, clock, monkeypatch, private, change):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            body = envelope(KILL, **(PRIVATE if private else {}))
            await native.deliver(body)
            original = state(native.app.store)
            changed = {**body, **change}
            if changed == body:
                changed['human_present'] = not body['human_present']
            if private and ('fight_ms' in change or 'fight_started_at_ms' in change) and 'human_present' not in change:
                changed.update(human_present=True, fight_started_at_ms=999000, fight_ms=2345)
                changed.update(change)
            before = len(native.results)
            await native.deliver(changed)
            assert len(native.results) == before + 1
            assert type(native.results[-1]) is Rejected
            assert state(native.app.store) == original
    run(scenario())


@pytest.mark.parametrize('private,mutation', [
    (True, 'DELETE FROM realm_kill'),
    (False, 'DELETE FROM realm_kill'),
    (True, 'UPDATE realm_kill SET qualification_json=NULL'),
    (True, "UPDATE realm_kill SET qualification_json='{}'"),
    (True, "UPDATE realm_kill SET qualification_sha256='" + '0' * 64 + "'"),
    (False, "UPDATE realm_fact_origin SET state='revoked'"),
    (False, "UPDATE game_event SET payload='{}' WHERE type='realm_kill'"),
    (True, "UPDATE game_event SET payload='{}' WHERE type='realm_kill'"),
])
def test_retained_tombstones_and_corruption_never_repair_or_promote(
        tmp_path, clock, monkeypatch, private, mutation):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            body = envelope(KILL, **(PRIVATE if private else {}))
            await native.deliver(body)
            with native.app.store.transaction():
                native.app.store.conn.execute(mutation)
                native.app.store.conn.execute('UPDATE cast_member SET level=80')
            original = state(native.app.store)
            await native.deliver(body)
            assert type(native.results[-1]) is Rejected
            assert state(native.app.store) == original
    run(scenario())


@pytest.mark.parametrize('route', ['injected', 'copied', 'outside', 'chat', 'skipped',
                                  'insert_failure', 'row_failure', 'echo'])
def test_missing_exact_native_receipt_has_no_source_authority(
        tmp_path, clock, monkeypatch, route):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            body = envelope('channel_raid_topic_v1')
            if route in {'injected', 'copied', 'outside'}:
                ev = parse_event(json.dumps(body))
                if route == 'injected':
                    app.bridge.inject(ev)
                    await native.drain()
                elif route == 'copied':
                    await app.handle(replace(ev))
                else:
                    await app.realm.on_event(ev)
            elif route in {'chat', 'echo'}:
                receipts = []
                async def observe_chat(ev):
                    receipts.append(app.row_receipt(ev))
                app.register_event_handler('chat', observe_chat)
                await native.deliver(dict(v=1, type='chat', ts=1000000, channel='guild',
                    from_guid=10, from_name='Testmaster', to_guid=0, text='Icecrown Citadel',
                    map=0, zone=0, via_bridge=route == 'echo'))
                assert len(receipts) == 1
                if route == 'echo':
                    assert receipts == [None]
            else:
                if route == 'skipped':
                    app.skip_event_rows.add('channel_chat')
                elif route == 'insert_failure':
                    def broken(*args, **kwargs):
                        raise sqlite3.OperationalError('injected')
                    monkeypatch.setattr(app.store, 'insert_game_event', broken)
                elif route == 'row_failure':
                    original = app._row
                    async def broken(ev, name):
                        await original(ev, name)
                        raise RuntimeError('after raw commit')
                    monkeypatch.setattr(app, '_row', broken)
                await native.deliver(body)
            assert counts(app.store) == (0, 0, 0, 0, 0)
            assert not native.results or type(native.results[-1]) is Rejected
            assert not app.realm._children
            if route in {'row_failure', 'injected', 'copied'}:
                with app.store.transaction():
                    assert app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='channel_chat'").fetchone()[0] == 1
    run(scenario())


@pytest.mark.parametrize('stage', ['earlier_handler', 'before_reducer'])
@pytest.mark.parametrize('mutation', ['type', 'payload', 'event'])
def test_mutated_original_row_or_event_cannot_refresh_receipt(
        tmp_path, clock, monkeypatch, stage, mutation):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            def mutate(ev, row_id):
                if mutation == 'event':
                    object.__setattr__(ev, 'winner', 'alliance')
                else:
                    with app.store.transaction():
                        if mutation == 'type':
                            app.store.conn.execute("UPDATE game_event SET type='pvp_kill' WHERE id=?", (row_id,))
                        else:
                            app.store.conn.execute("UPDATE game_event SET payload='{}' WHERE id=?", (row_id,))
            if stage == 'earlier_handler':
                async def before(ev):
                    receipt = app.row_receipt(ev)
                    mutate(ev, receipt.row_id)
                app._handlers['bg_end'].insert(0, before)
                await native.deliver(envelope())
            else:
                entered, release = pause_source(native, monkeypatch)
                dispatch = asyncio.create_task(native.deliver(envelope()))
                await reached(entered.is_set)
                observation = native.captures[-1][0]
                mutate(observation.event, observation.receipt.row_id)
                release.set()
                await dispatch
                assert type(native.results[-1]) is Rejected
            assert counts(app.store) == (0, 0, 0, 0, 0)
    run(scenario())


def test_later_decoy_insert_does_not_steal_actual_returned_row(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app, inserted = native.app, []
            original = app.store.insert_game_event
            def insert_decoy(ev, **kwargs):
                real = original(ev, **kwargs)
                later = original(replace(ev, winner='alliance'), **kwargs)
                inserted.append((real, later))
                return real
            monkeypatch.setattr(app.store, 'insert_game_event', insert_decoy)
            await native.deliver(envelope())
            assert type(native.results[-1]) is FactRow
            assert native.captures[-1][0].receipt.row_id == inserted[0][0] < inserted[0][1]
            with app.store.transaction():
                assert app.store.conn.execute('SELECT game_event_id FROM realm_source').fetchone()[0] == inserted[0][0]
    run(scenario())


@pytest.mark.parametrize('change', ['writer_closed', 'writer_epoch', 'writer_replaced', 'disconnect', 'disable_enable',
    'process', 'generation', 'human', 'unconfirmed', 'guild', 'hello', 'presence', 'connection', 'app_owner',
    'realm_owner', 'store_owner', 'bridge_owner', 'fact_store_owner', 'private_store_owner'])
def test_captured_child_rechecks_original_owners_and_complete_current_context(
        tmp_path, clock, monkeypatch, change):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app, realm = native.app, native.app.realm
            entered, release = pause_source(native, monkeypatch)
            dispatch = asyncio.create_task(native.deliver(envelope(KILL, **PRIVATE)))
            await reached(entered.is_set)
            original_store, original_bridge = app.store, app.bridge
            if change == 'writer_closed':
                native.writer.close()
            elif change == 'writer_epoch':
                app.bridge._transport_epoch += 1
            elif change == 'writer_replaced':
                generation = realm._generation
                await native.disconnect()
                await native.connect()
                assert realm._generation == generation
            elif change == 'disconnect':
                app.bridge._connected = False
            elif change == 'disable_enable':
                realm._set_enabled(False)
                realm._set_enabled(True)
            elif change == 'process':
                realm._process_id = 'replacement-process'
            elif change == 'generation':
                realm._generation += 1
            elif change == 'human':
                app._note_human(11)
            elif change == 'unconfirmed':
                app._human_named = False
            elif change == 'guild':
                realm._guild = 51
            elif change == 'hello':
                # The actual before-hello hook invalidates the captured child;
                # its native data still came through the single dispatcher.
                await realm.on_presence(parse_event(json.dumps(dict(v=1, type='hello',
                    ts=1000001, server='Synthetic', version='1', guild_id=50, human_guid=0))))
            elif change == 'presence':
                realm._presence_known = False
            elif change == 'connection':
                realm._connection_epoch += 1
            elif change == 'app_owner':
                realm.app = object()
            elif change == 'realm_owner':
                app.realm = object()
            elif change == 'store_owner':
                app.store = Store(original_store.conn, clock=clock)
            elif change == 'bridge_owner':
                app.bridge = BridgeClient('unused', 1, app.handle, clock=clock)
                app.bridge._transport_epoch = original_bridge._transport_epoch
                app.bridge._connected, app.bridge._writer = True, native.writer
            elif change == 'fact_store_owner':
                realm._facts._store = Store(original_store.conn, clock=clock)
            else:
                realm._private_kills._store = Store(original_store.conn, clock=clock)
            try:
                release.set()
                await dispatch
                assert counts(original_store) == (0, 0, 0, 0, 0)
                assert not realm._children
                assert not native.failures
                if change not in {'disable_enable', 'hello'}:
                    assert native.results[-1] == Rejected('proof_changed')
            finally:
                realm.app, app.realm, app.store, app.bridge = app, realm, original_store, original_bridge
                realm._facts._store = realm._private_kills._store = original_store
    run(scenario())


@pytest.mark.parametrize('stage', ['row', 'handler'])
@pytest.mark.parametrize('replace_writer', [False, True])
def test_actual_writer_change_fences_old_dispatch_before_queued_connection_hooks(
        tmp_path, clock, monkeypatch, stage, replace_writer):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app, realm = native.app, native.app.realm
            entered, release = asyncio.Event(), asyncio.Event()
            if stage == 'row':
                original = app._row
                async def pause(ev, name):
                    result = await original(ev, name)
                    if name == 'bg_end':
                        entered.set()
                        await release.wait()
                    return result
                monkeypatch.setattr(app, '_row', pause)
            else:
                async def pause(ev):
                    entered.set()
                    await release.wait()
                app._handlers['bg_end'].insert(0, pause)
            dispatch = asyncio.create_task(native.deliver(envelope()))
            await reached(entered.is_set)
            generation = realm._generation
            await native.disconnect()
            if replace_writer:
                await native.connect()
            assert realm._generation == generation
            release.set()
            await dispatch
            assert counts(app.store) == (0, 0, 0, 0, 0)
            assert not native.captures
            if not replace_writer:
                await native.connect()
            await native.drain()
            await native.hello()
            await native.deliver(envelope(ts=1000001))
            assert type(native.results[-1]) is FactRow
            assert realm.action_context().connected
    run(scenario())


@pytest.mark.parametrize('condition', ['disabled', 'unconfirmed', 'no_hello', 'no_connection', 'first_backlog'])
@pytest.mark.parametrize('private', [False, True])
def test_unavailable_context_and_first_backlog_keep_only_raw_rows(
        tmp_path, clock, monkeypatch, condition, private):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch,
                enabled=condition != 'disabled', confirmed=condition != 'unconfirmed',
                establish=condition != 'no_hello') as native:
            app = native.app
            if condition == 'unconfirmed':
                assert app.human_guid == 10 and app.confirmed_human_guid == 0
            if condition == 'no_connection':
                app.realm._connected = False
            if condition == 'first_backlog':
                await native.hello(ts=1010000)
            await native.deliver(envelope(KILL, **(PRIVATE if private else {})))
            assert counts(app.store) == (0, 0, 0, 0, 0)
            with app.store.transaction():
                assert app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='realm_kill'").fetchone()[0] == 1
    run(scenario())


@pytest.mark.parametrize('enabled', [False, True])
def test_channel_raw_retention_includes_policy_rejection_and_local_disable(
        tmp_path, clock, monkeypatch, enabled):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch, enabled=enabled) as native:
            await native.deliver(envelope('channel_raid_topic_v1', text='unrelated public chatter'))
            assert counts(native.app.store) == (0, 0, 0, 0, 0)
            with native.app.store.transaction():
                row = native.app.store.conn.execute("SELECT payload FROM game_event WHERE type='channel_chat'").fetchone()
                assert json.loads(row['payload'])['text'] == 'unrelated public chatter'
    run(scenario())


def test_offline_confirmed_identity_survives_human_logout(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            assert native.app.realm.action_context().presence_known
            assert not native.app.world.human_online
            await native.deliver(dict(v=1, type='logout', ts=1000000, guid=10, human=True))
            await native.deliver(envelope(KILL))
            assert type(native.results[-1]) is FactRow
            assert native.app.confirmed_human_guid == 10
            assert native.app.realm.action_context().presence_known
    run(scenario())


def test_begin_stop_fences_captured_child_before_realm_close(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app, realm = native.app, native.app.realm
            entered, release = pause_source(native, monkeypatch)
            drain_entered, drain_release = asyncio.Event(), asyncio.Event()
            async def early_drain():
                drain_entered.set()
                await drain_release.wait()
            app._shutdown_drains.insert(0, early_drain)
            dispatch = asyncio.create_task(native.deliver(envelope()))
            await reached(entered.is_set)
            shutdown = app._begin_stop()
            assert app.stopping and not realm._stopping
            await reached(drain_entered.is_set)
            try:
                release.set()
                await dispatch
                assert native.results[-1] == Rejected('proof_changed')
                assert counts(app.store) == (0, 0, 0, 0, 0)
                assert not realm._stopping and not realm._children
                with pytest.raises(AttributeError):
                    app.stopping = False
            finally:
                # Settle the actual stream while SQL is still held open.
                await native.disconnect()
                drain_release.set()
                await shutdown
            assert app.stopping and app._closed and realm._stopping
    run(scenario())


@pytest.mark.parametrize('when', ['before_admission', 'while_child_waits', 'after_commit'])
def test_parent_cancellation_preserves_commit_boundary_and_child_ownership(
        tmp_path, clock, monkeypatch, when):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            realm = native.app.realm
            if when == 'before_admission':
                original = realm._capture_source_observation
                def cancel_after_capture(ev):
                    observation = original(ev)
                    if type(observation) is RealmObservation:
                        asyncio.current_task().cancel()
                    return observation
                monkeypatch.setattr(realm, '_capture_source_observation', cancel_after_capture)
            elif when == 'after_commit':
                original = realm._on_source_observation
                async def cancel_after_commit(observation):
                    result = await original(observation)
                    assert type(result) is FactRow
                    native.captures[-1][1].cancel()
                    return result
                monkeypatch.setattr(realm, '_on_source_observation', cancel_after_commit)
            else:
                entered, release = pause_source(native, monkeypatch)
            dispatch = asyncio.create_task(native.deliver(envelope()))
            if when == 'while_child_waits':
                await reached(entered.is_set)
                assert realm._children
                dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
            assert not realm._children
            assert counts(native.app.store) == ((1, 1, 1, 2, 0) if when == 'after_commit' else (0, 0, 0, 0, 0))
            with native.app.store.transaction():
                assert native.app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='bg_end'").fetchone()[0] == 1
    run(scenario())


@pytest.mark.parametrize('private,table,condition', [
    (False, 'realm_source', '1'), (False, 'realm_fact', '1'),
    (False, 'realm_fact_origin', '1'), (False, 'realm_knows', 'NEW.guild=50'),
    (False, 'realm_knows', 'NEW.guild=60'), (False, 'realm_kill', '1'),
    (True, 'realm_source', '1'), (True, 'realm_kill', '1'),
])
def test_integrated_sql_write_failure_rolls_back_all_realm_rows_keeps_raw(
        tmp_path, clock, monkeypatch, private, table, condition):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store = native.app.store
            with store.transaction():
                store.conn.execute(f'CREATE TEMP TRIGGER fail_source BEFORE INSERT ON {table} '
                    f"WHEN {condition} BEGIN SELECT RAISE(ABORT,'injected'); END")
            await native.deliver(envelope(KILL, **(PRIVATE if private else {})))
            assert native.failures == [sqlite3.IntegrityError]
            assert not native.results
            assert counts(store) == (0, 0, 0, 0, 0)
            with store.transaction():
                assert store.conn.execute("SELECT count(*) FROM game_event WHERE type='realm_kill'").fetchone()[0] == 1
    run(scenario())


def test_source_sql_fault_still_reaches_generic_parts_then_fails(tmp_path, clock, monkeypatch, caplog):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store, seen = native.app.store, []

            class Part:
                async def on_event(self, ev):
                    seen.append(ev)

            native.app.realm.add_part(Part(), event_types=('realm_kill',))
            with store.transaction():
                store.conn.execute('CREATE TEMP TRIGGER fail_source BEFORE INSERT ON realm_source '
                                   "BEGIN SELECT RAISE(ABORT,'injected'); END")
            with caplog.at_level(logging.ERROR):
                await native.deliver(envelope(KILL))
            assert native.failures == [sqlite3.IntegrityError]
            assert len(seen) == 1 and counts(store) == (0, 0, 0, 0, 0)
            assert 'event_handler_failed: type=realm_kill' in caplog.text
    run(scenario())


def test_final_barrier_through_commit_never_yields_the_loop(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            realm, store, seen = native.app.realm, native.app.store, []
            barrier = realm._source_context_current

            def probe(context, transport_epoch):
                current = barrier(context, transport_epoch)
                if current and store.in_unit:
                    asyncio.get_running_loop().call_soon(lambda: seen.append(store.in_unit))
                return current

            monkeypatch.setattr(realm, '_source_context_current', probe)
            await native.deliver(envelope())
            assert type(native.results[0]) is FactRow
            assert seen == [False, False]  # capture's check, then the reducer's final one
    run(scenario())


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('failure', ['final_rejection', 'final_sql', 'commit'])
def test_integrated_final_read_and_commit_failures_escape_entire_unit(
        tmp_path, clock, monkeypatch, private, failure):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store, realm = native.app.store, native.app.realm
            if failure == 'commit':
                with store.transaction():
                    store.conn.execute('CREATE TABLE source_deferred (fact INTEGER REFERENCES realm_fact(id) '
                                       'DEFERRABLE INITIALLY DEFERRED)')
                    store.conn.execute('CREATE TEMP TRIGGER fail_commit AFTER INSERT ON realm_kill '
                                       'BEGIN INSERT INTO source_deferred(fact) VALUES (999999); END')
            else:
                def fail_read(*args, **kwargs):
                    assert store.in_unit
                    assert store.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 1
                    assert store.conn.execute('SELECT count(*) FROM realm_kill').fetchone()[0] == 1
                    if failure == 'final_sql':
                        raise sqlite3.OperationalError('injected final read')
                    return Rejected('proof_changed')
                if private:
                    monkeypatch.setattr(realm._private_kills, 'get_in_unit', fail_read)
                else:
                    monkeypatch.setattr(fact_store, 'public_fact_in_unit', fail_read)
            await native.deliver(envelope(KILL, **(PRIVATE if private else {})))
            assert counts(store) == (0, 0, 0, 0, 0)
            if failure == 'final_rejection':
                assert native.results == [Rejected('proof_changed')]
                assert not native.failures
            else:
                assert native.failures == [sqlite3.IntegrityError if failure == 'commit' else sqlite3.OperationalError]
                assert not native.results
            with store.transaction():
                assert store.conn.execute("SELECT count(*) FROM game_event WHERE type='realm_kill'").fetchone()[0] == 1
                if failure == 'commit':
                    assert store.conn.execute('SELECT count(*) FROM source_deferred').fetchone()[0] == 0
    run(scenario())


@pytest.mark.parametrize('overflow', ['actors', 'guilds', 'snapshot_bytes', 'extra_key', 'envelope_bytes'])
def test_complete_schema_and_qualification_overflow_never_leave_partial_state(
        tmp_path, clock, monkeypatch, overflow):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store = native.app.store
            body = envelope(KILL, **PRIVATE)
            with store.transaction():
                if overflow in {'actors', 'snapshot_bytes'}:
                    for guid in range(1000, 1000 + (129 if overflow == 'actors' else 70)):
                        insert(store, 'realm_actor', guid=guid, guild=60, name='Synthetic',
                               role='officer', added=1.0, status='active')
                elif overflow == 'guilds':
                    for guild in range(1000, 1129):
                        insert(store, 'realm_guild', id=guild, role='progression', faction='horde',
                               name='Synthetic', added=1.0, status='active')
            if overflow == 'extra_key':
                body['unrecognized'] = 1
            elif overflow == 'envelope_bytes':
                # Native parsing retains the ordinary event; complete-origin
                # canonical expansion exceeds the provenance byte limit.
                body = json.dumps(body)[:-1] + ',"extra":[' + ','.join(['1e20'] * 12000) + ']}'
            await native.deliver(body)
            assert counts(store) == (0, 0, 0, 0, 0)
            assert not native.captures and not native.results
            with store.transaction():
                assert store.conn.execute("SELECT count(*) FROM game_event WHERE type='realm_kill'").fetchone()[0] == 1
    run(scenario())


@pytest.mark.parametrize('policy', ['bg_result_v1', 'arena_result_v1', 'pvp_result_v1', 'channel_raid_topic_v1'])
def test_retained_public_replay_reaches_original_after_actor_retirement(
        tmp_path, clock, monkeypatch, policy):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            changes = dict(from_guid=201, from_name='Rivalname') if policy == 'channel_raid_topic_v1' else {}
            body = envelope(policy, **changes)
            await native.deliver(body)
            first, original = native.results[-1], state(native.app.store)
            assert type(first) is FactRow
            with native.app.store.transaction():
                native.app.store.conn.execute("UPDATE realm_actor SET status='retired'")
                native.app.store.conn.execute('UPDATE cast_member SET level=79 WHERE guid>=101')
            await native.hello(ts=1010000)
            await native.deliver(body)
            assert native.results[-1] == first and state(native.app.store) == original
            if policy == 'channel_raid_topic_v1':
                assert native.captures[-1][0].qualification.speaker_kind == 'none'
    run(scenario())


def test_complete_private_source_capture_overflow_rolls_back_claim(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store = native.app.store
            for index in range(1, 71):
                with store.transaction():
                    insert(store, 'realm_actor', guid=1000 + index, guild=60, name='Synthetic',
                           role='officer', added=1.0, status='active')
                original = state(store)
                previous = len(native.captures)
                await native.deliver(envelope(KILL, kill_id='a' * 32 + ':' + str(index), **PRIVATE))
                # Capture itself still fits: the source-bound private codec is
                # the later, stricter limit and must undo its already-made claim.
                assert len(native.captures) == previous + 1
                result = native.results[-1]
                if type(result) is Rejected:
                    assert result == Rejected('invalid_input')
                    assert state(store) == original
                    break
                assert type(result) is PrivateKillRecord
            else:
                pytest.fail('expected a complete source-bound private capture overflow')
    run(scenario())


@pytest.mark.parametrize('private', [False, True])
def test_current_privacy_identity_is_rechecked_after_capture(tmp_path, clock, monkeypatch, private):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            entered, release = pause_source(native, monkeypatch)
            policy = KILL if private else 'channel_raid_topic_v1'
            body = envelope(policy, **(PRIVATE if private else {}))
            dispatch = asyncio.create_task(native.deliver(body))
            await reached(entered.is_set)
            if private:
                # Raw native kill fields carry no prose; privacy still uses the
                # real, current identity and does not invent speaker text.
                native.app.store.set_meta('human_name', 'Changedname')
            else:
                native.app.cfg = replace(native.app.cfg,
                    human=replace(native.app.cfg.human, real_name='Icecrown Citadel'))
            release.set()
            await dispatch
            if private:
                assert type(native.results[-1]) is PrivateKillRecord
            else:
                assert type(native.results[-1]) is Rejected
                assert counts(native.app.store) == (0, 0, 0, 0, 0)
    run(scenario())
