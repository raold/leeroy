"""Kill announcements over actual Store rows, the real service and the real bridge reader."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol
from leeroy_mind.realm.announce import MAX_OUTSTANDING, AnnouncementRefused, Announcer
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.kill_retention import PrivateKillStore
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.storage import db
from realm.fakes import RealmApp, enabled
from test_realm_foundation_schema import _announcement, _insert, _kill, _source
from test_realm_live_proof import CONTEXT, HUMAN, insert, observe, roster
from test_realm_source_admission import KILL, application, envelope, reached, run as run_native

NY = ZoneInfo('America/New_York')
THU = datetime(2026, 10, 29, 16, 0, tzinfo=NY).timestamp()  # The conftest clock: a Thursday.
RESET = datetime(2026, 11, 3, 4, 0, tzinfo=NY).timestamp()  # The next product week starts.
WEEK = 7 * 86400
CAST = (101, 102, 103, 104, 105)
COLUMNS = 'first,period_key,state,safe_reason'


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


def payload(at, *, n, instance, boss=0, difficulty=1, human=True, cast=CAST, fight_ms=251_900, attempts=0):
    at_ms = int(at * 1000)
    size, heroic = (25, False) if difficulty == -1 else (25 if difficulty % 2 else 10, difficulty >= 2)
    measured = human and fight_ms is not None
    return dict(ts=at_ms + 20, kill_id=f'{"a" * 32}:{n}', native_epoch=29, guild_id=50, human_guid=10,
                killed_at_ms=at_ms, verified_at_ms=at_ms + 10,
                fight_started_at_ms=at_ms - fight_ms if measured else 0,
                fight_ms=fight_ms if measured else -1, attempts=attempts, map=631, instance_id=instance, boss_id=boss,
                difficulty=difficulty, size=size, heroic=heroic, human_present=human, cast_members=list(cast))


class Herald:
    """Kills recorded and planned in one unit, as the service does, over the roster store."""

    def __init__(self, store):
        self.store = store
        self.clock = Clock(THU)
        self.announcer = Announcer(store, process_id=CONTEXT.process_id, clock=self.clock)
        self.n = 0

    def kill(self, at, *, private=False, plan=True, now=None, context=None, instance=None,
             human=None, cast=None, **kw):
        self.n += 1
        self.clock.t = at + 1 if now is None else now
        data = payload(at, n=self.n, instance=100 + self.n if instance is None else instance,
                       human=not private if human is None else human,
                       cast=(CAST[:4] if private else CAST) if cast is None else cast, **kw)
        with self.store.transaction():
            observation = observe(self.store, KILL, payload=data)
            if private:
                record = PrivateKillStore(self.store).record_in_unit(observation, human=HUMAN)
            else:
                record = FactStore(self.store).record_verified_kill_in_unit(observation, human=HUMAN)
            if not plan:
                return record
            if context is not None:
                observation = replace(observation, context=context)
            return self.announcer.prepare_in_unit(observation, record)

    def boss_row(self, at, *, instance, state='killed', boss=0):
        body = dict(ts=int(at * 1000), map=631, instance_id=instance, boss_id=boss, boss_name='',
                    state=state, members=[10, 101])
        with self.store.transaction():
            insert(self.store, 'game_event', ts=at, type='boss', guid=None, payload=json.dumps(body),
                   session_id=None)

    def canon(self, boss, difficulty):
        with self.store.transaction():
            insert(self.store, 'realm_canon_kill', map=631, boss_id=boss, difficulty=difficulty,
                   moment='canon', day='2026-01-01')

    def rows(self):
        return [tuple(r) for r in self.store.conn.execute(
            f'SELECT {COLUMNS} FROM realm_announcement ORDER BY prepared_at, kill_id')]

    def row(self, plan):
        return tuple(self.store.conn.execute(f'SELECT {COLUMNS} FROM realm_announcement WHERE kill_id=?',
                                             (plan.kill_id,)).fetchone())

    def send(self, plan, context=CONTEXT):
        cmd = self.announcer.command(plan)
        self.announcer.before_send(cmd, context=context)
        return cmd


@pytest.fixture
def h(roster):
    return Herald(roster)


# -- first: ever or week ---------------------------------------------------------------------

def test_announce_first_kill_ever(h):
    # (631, 0, 1): 25-man normal, nothing stored, not canon; native counted three pulls.
    plan = h.kill(THU, difficulty=1, attempts=3)
    cmd = h.announcer.command(plan)
    assert cmd == protocol.Announce(plan.command_id, f'{"a" * 32}:1', 29, 50, 10, 631, 101, 0, 1, 25,
                                    False, 'Lord Marrowgar', attempts=3, fight_s=251, first='ever')
    assert h.rows() == [('ever', '2026-10-27', 'prepared', 'none')]
    stored = h.store.conn.execute('SELECT payload_json,expires_at,process_id,generation,connection_epoch '
                                  'FROM realm_announcement').fetchone()
    assert {k: json.loads(stored['payload_json'])[k] for k in ('first', 'attempts')} == dict(first='ever', attempts=3)
    assert stored['expires_at'] == THU + 30
    assert tuple(stored)[2:] == (CONTEXT.process_id, CONTEXT.generation, str(CONTEXT.connection_epoch))
    assert h.announcer.owns_command(cmd)


def test_announce_first_kill_of_the_week(h):
    h.kill(THU - 3 * WEEK, difficulty=1)  # The guild killed 25-man Marrowgar three weeks ago.
    plan = h.kill(THU, difficulty=1)
    assert plan.first == 'week' and h.row(plan) == ('week', '2026-10-27', 'prepared', 'none')


def test_first_ever_takes_precedence_and_holds_the_week_slot(h):
    plan = h.kill(THU, difficulty=0)  # Qualifies as both the first ever and the first this week.
    assert plan.first == 'ever' and h.rows() == [('ever', '2026-10-27', 'prepared', 'none')]
    # One kill, one send: the same week has no second slot for this mode.
    assert h.kill(THU + 3600, difficulty=0) is None
    assert len(h.rows()) == 1
    row = dict(h.store.conn.execute('SELECT * FROM realm_announcement').fetchone())
    other = h.kill(THU + 7200, difficulty=0, plan=False)
    assert other is not None
    for first, period in (('week', row['period_key']), ('ever', '2026-11-03')):
        with pytest.raises(sqlite3.IntegrityError), h.store.transaction():
            h.store.conn.execute(f'INSERT INTO realm_announcement({",".join(row)}) VALUES '
                                 f'({",".join("?" for _ in row)})', tuple({**row, 'command_id': 'other',
                                 'kill_id': f'{"a" * 32}:3', 'first': first, 'period_key': period}.values()))


def test_announce_canon_boss_weekly_only(h):
    h.canon(12, 3)  # The Lich King 25-man heroic is canon: the guild's own history.
    plan = h.kill(THU, boss=12, difficulty=3)
    assert plan.first == 'week' and plan.boss_name == 'the Lich King'
    assert h.announcer.command(plan).first == 'week'
    # Canon is matched on its mode: a 10-man normal kill can still be a true first.
    assert h.kill(THU + 60, boss=12, difficulty=0).first == 'ever'
    # An unknown-mode canon row overlaps every mode.
    h.canon(5, -1)
    assert h.kill(THU + 120, boss=5, difficulty=2).first == 'week'


def test_prior_kill_suppresses_ever_forever(h):
    h.kill(THU - 40 * WEEK, difficulty=0, plan=False)  # Stored long ago, never announced.
    assert h.kill(THU, difficulty=0).first == 'week'
    assert h.kill(THU + 30 * WEEK, difficulty=0).first == 'week'
    h.kill(THU - 20 * WEEK, private=True, boss=1, difficulty=2)  # A private kill counts too.
    assert h.kill(THU, boss=1, difficulty=2).first == 'week'
    # So does a core Boss kill from before the Realm existed (another instance, any mode).
    h.boss_row(THU - 52 * WEEK, instance=7, boss=2)
    assert h.kill(THU, boss=2, difficulty=3).first == 'week'
    assert [r[0] for r in h.rows()] == ['week', 'week', 'week', 'week']
    assert not any(r[0] == 'ever' for r in h.rows())


def test_announce_once_a_week(h):
    assert h.kill(THU, difficulty=1).first == 'ever'
    assert h.kill(THU + 2 * 86400, difficulty=1) is None  # Saturday, the same product week.
    assert h.kill(RESET - 1, difficulty=1) is None  # Tuesday 03:59:59 New York.
    plan = h.kill(RESET, difficulty=1)  # Tuesday 04:00 New York, after the reset.
    assert plan.first == 'week' and plan.period_key == '2026-11-03'
    assert [r[:2] for r in h.rows()] == [('ever', '2026-10-27'), ('week', '2026-11-03')]


# -- what is said ------------------------------------------------------------------------------

def test_offline_raid_announcement_omits_attempts(h):
    # Five cast members without him: a guild kill, but an offline raid has no time, and no
    # attempts when native could not count them (0 is unknown, never a number).
    plan = h.kill(THU, human=False, cast=CAST)
    assert h.store.conn.execute('SELECT fact FROM realm_kill WHERE kill_id=?',
                                (plan.kill_id,)).fetchone()[0] is not None
    cmd = h.announcer.command(plan)
    assert (cmd.attempts, cmd.fight_s) == (0, 0)
    # A cast-only kill carries native's count; its time stays his raid's only.
    counted = h.announcer.command(h.kill(THU, boss=4, human=False, cast=CAST, attempts=2))
    assert (counted.attempts, counted.fight_s) == (2, 0)
    # His raid's measured fight is whole seconds, rounded down as native checks it.
    measured = h.announcer.command(h.kill(THU, boss=1, fight_ms=251_999))
    assert (measured.attempts, measured.fight_s) == (0, 251)
    unmeasured = h.announcer.command(h.kill(THU, boss=3, fight_ms=None, attempts=7))
    assert (unmeasured.attempts, unmeasured.fight_s) == (7, 0)


def test_no_announcement_for_a_wipe(h):
    # A wipe is no kill: its core Boss row, even from another instance this week, holds nothing.
    h.boss_row(THU - 600, instance=7, state='wiped')
    h.boss_row(THU - 300, instance=7, state='engaged')
    assert h.kill(THU, difficulty=1).first == 'ever'


def test_no_announcement_for_a_wipe_through_the_service(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            at = int(clock.now() * 1000)
            await native.deliver(dict(v=1, type='boss', ts=at, map=631, instance_id=9, boss_id=0,
                                      boss_name='', state='wiped', members=[10, *CAST]))
            assert announces(native) == [] and announcement_rows(native) == []
    run_native(scenario())


def test_private_kill_never_announces(h):
    assert h.kill(THU, private=True) is None
    public = h.kill(THU, boss=1, plan=False)
    with h.store.transaction():
        # Another kill's public fact does not make a private kill public.
        observation = observe(h.store, KILL, payload=payload(THU, n=99, instance=99, boss=2,
                              human=False, cast=CAST[:4]))
        record = PrivateKillStore(h.store).record_in_unit(observation, human=HUMAN)
        assert h.announcer.prepare_in_unit(observation, record) is None
        assert h.announcer.prepare_in_unit(observation, public) is None
    assert h.rows() == []


def test_prior_private_kill_suppresses(h):
    h.kill(THU - 3600, private=True, difficulty=1)
    assert h.kill(THU, difficulty=1) is None
    # Only an overlapping mode: a private 25-man kill leaves the 10-man slot free.
    assert h.kill(THU, difficulty=0).first == 'ever'
    assert h.rows() == [('ever', '2026-10-27', 'prepared', 'none')]


def test_core_boss_row_of_same_kill_does_not_suppress_itself(h):
    h.boss_row(THU - 1, instance=55)  # The core row of this very kill: its instance, its week.
    assert h.kill(THU, instance=55, difficulty=1).first == 'ever'
    # The same instance id in an earlier week is another kill (a reused id).
    h.boss_row(THU - 2 * WEEK, instance=56, boss=1)
    assert h.kill(THU, instance=56, boss=1, difficulty=1).first == 'week'
    # Another instance this week is another kill.
    h.boss_row(THU - 60, instance=57, boss=2)
    assert h.kill(THU, instance=58, boss=2, difficulty=1) is None


def test_unknown_difficulty_overlaps_every_mode(h):
    h.kill(THU - 3600, difficulty=-1, plan=False)
    assert h.kill(THU, difficulty=1) is None  # A -1 kill this week holds every mode's slot.
    assert h.kill(THU, difficulty=2) is None
    h.kill(THU - 3600, boss=1, difficulty=0, plan=False)
    assert h.kill(THU, boss=1, difficulty=-1) is None  # A -1 kill overlaps the stored 10-man.
    plan = h.kill(THU, boss=3, difficulty=-1)
    assert plan.first == 'week'  # Alone, a -1 kill is still never "ever".
    assert h.rows() == [('week', '2026-10-27', 'prepared', 'none')]
    with pytest.raises(sqlite3.IntegrityError), h.store.transaction():
        h.store.conn.execute("UPDATE realm_announcement SET first='ever'")


# -- the barrier, settlement and recovery --------------------------------------------------------

def test_expired_or_over_capacity_never_sends(h):
    assert h.kill(THU, now=THU + 30) is None  # The 30 s window closed before the decision.
    assert h.kill(THU + 3600, boss=1, now=THU + 3600 - 31) is None  # A clock that disagrees.
    assert h.rows() == []
    plans = [h.kill(THU, boss=boss) for boss in (2, 3, 4, 5)]
    assert all(plans) and len(plans) == MAX_OUTSTANDING
    assert h.kill(THU, boss=6) is None  # Over capacity: nothing is planned.
    sent = h.send(plans[0])
    h.announcer.on_ack(sent, Ack(0, sent.id, True, ''), now=THU + 2)
    assert h.kill(THU, boss=7) is not None  # A settled row frees its place.
    h.clock.t = THU + 30
    with pytest.raises(AnnouncementRefused, match='expired'):
        h.send(plans[1])
    assert h.row(plans[1]) == ('ever', '2026-10-27', 'expired', 'expired')
    assert h.store.conn.execute('SELECT dispatched_at FROM realm_announcement WHERE kill_id=?',
                                (plans[1].kill_id,)).fetchone()[0] is None
    with pytest.raises(AnnouncementRefused, match='settled'):
        h.send(plans[1])
    assert h.announcer.abandon_unsent(h.announcer.command(plans[2]), now=THU + 31)
    assert h.row(plans[2])[2:] == ('expired', 'expired')


def test_one_send_per_slot_and_no_retry(h):
    plans = [h.kill(THU, boss=boss) for boss in (1, 2, 3, 4)]
    first = h.send(plans[0])
    assert h.row(plans[0])[2] == 'dispatched'
    with pytest.raises(AnnouncementRefused, match='settled'):
        h.send(plans[0])
    assert h.announcer.on_ack(first, Ack(0, first.id, False, 'quiet'), now=THU + 2)
    assert h.row(plans[0])[2:] == ('refused', 'native_refused')
    assert not h.announcer.on_ack(first, Ack(0, first.id, True, ''), now=THU + 3)
    assert not h.announcer.on_send_error(first, 'AckTimeout', now=THU + 3)
    for plan, error, outcome in ((plans[1], 'NotConnected', ('refused', 'pretransport_refused')),
                                 (plans[2], 'AckTimeout', ('unknown', 'transport_unknown'))):
        cmd = h.send(plan)
        assert h.announcer.on_send_error(cmd, error, now=THU + 2)
        assert h.row(plan)[2:] == outcome
        with pytest.raises(AnnouncementRefused):
            h.send(plan)
    # A different payload under the same id is not this row; nor is another context.
    forged = replace(h.announcer.command(plans[3]), first='week')
    with pytest.raises(AnnouncementRefused, match='not_owned'):
        h.announcer.before_send(forged, context=CONTEXT)
    assert not h.announcer.owns_command(forged) and h.row(plans[3])[2] == 'prepared'
    with pytest.raises(AnnouncementRefused, match='context_unavailable'):
        h.send(plans[3], context=replace(CONTEXT, generation=CONTEXT.generation + 1))
    assert h.row(plans[3])[2:] == ('abandoned', 'context_unavailable')


def test_disabled_context_plans_and_sends_nothing(h):
    off = replace(CONTEXT, enabled=False)
    assert h.kill(THU, context=off) is None
    plan = h.kill(THU, boss=1)
    with pytest.raises(AnnouncementRefused, match='context_unavailable'):
        h.send(plan, context=off)
    assert h.rows() == [('ever', '2026-10-27', 'abandoned', 'context_unavailable')]


def test_recovery_prepared_abandoned_dispatched_unknown(tmp_path, roster, clock):
    h = Herald(roster)
    prepared, dispatched, sent = h.kill(THU), h.kill(THU, boss=1), h.kill(THU, boss=2)
    h.send(dispatched)
    done = h.send(sent)
    h.announcer.on_ack(done, Ack(0, done.id, True, ''), now=THU + 2)
    assert h.announcer.recover(process_id=CONTEXT.process_id, now=THU + 3) == (0, 0)
    assert Announcer(roster, process_id='next', clock=h.clock).recover(process_id='next', now=THU + 4) == (1, 1)
    assert [h.row(p)[2:] for p in (prepared, dispatched, sent)] == [
        ('abandoned', 'recovered_prepared'), ('unknown', 'recovered_dispatched'), ('sent', 'none')]
    # The service recovers before anything can send, enabled or not.
    roster.conn.execute("UPDATE realm_announcement SET state='prepared',safe_reason='none',settled_at=NULL "
                        "WHERE kill_id=?", (prepared.kill_id,))
    roster.conn.commit()
    realm = attach_realm(RealmApp(tmp_path, roster, clock))
    assert not realm.enabled and h.row(prepared)[2:] == ('abandoned', 'recovered_prepared')
    with pytest.raises(AnnouncementRefused):
        h.send(prepared)
    run_native(realm.close())


def test_unowned_announce_refused(tmp_path, roster, clock):
    app = RealmApp(tmp_path, roster, clock)
    enabled(tmp_path, True)
    realm = attach_realm(app)
    cmd = protocol.Announce('announce_x', f'{"a" * 32}:1', 29, 50, 10, 631, 9, 0, 1, 25, False,
                            'Lord Marrowgar', first='ever')

    async def go():
        await app.connection(True)
        await app.event(Hello(0, 'test', '1', 50, 1))
        with pytest.raises(AnnouncementRefused, match='not_owned'):
            realm.on_send(cmd)
        realm.queue.submit(cmd, owner='realm_day')  # Even an enrolled one never reaches the wire.
        with pytest.raises(AnnouncementRefused, match='not_owned'):
            realm.on_send(cmd)
        with pytest.raises(AnnouncementRefused):
            await realm.ports.send(cmd)
        await realm.close()
    run_native(go())
    assert app.sent == [] and not realm._pending


# -- the service: one plan in the kill's unit, sent after commit, settled by its ack ------------

def fresh_kill(clock, **changes):
    at = int(clock.now() * 1000) - 2000
    return envelope(KILL, **{'ts': at + 20, 'killed_at_ms': at, 'verified_at_ms': at + 10,
                             'fight_started_at_ms': at - 251_000, 'fight_ms': 251_000,
                             'difficulty': 1, 'size': 25, **changes})


def announces(native):
    return [json.loads(line) for writer in native.writers for line in writer.sent
            if json.loads(line).get('type') == 'announce']


def announcement_rows(native):
    with native.app.store.transaction():
        return [tuple(r) for r in native.app.store.conn.execute(
            f'SELECT command_id,{COLUMNS} FROM realm_announcement ORDER BY prepared_at')]


async def ack(native, command_id, ok=True, error=''):
    native.reader.feed_data(json.dumps(dict(v=1, type='ack', ts=1, id=command_id, ok=ok,
                                            error=error)).encode('utf-8') + b'\n')
    await reached(lambda: announcement_rows(native)[0][3] != 'dispatched')


def test_service_announces_a_kill_once_and_settles_on_its_ack(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            body = fresh_kill(clock)
            await native.deliver(body)
            await reached(lambda: announces(native))
            (sent,) = announces(native)
            assert announcement_rows(native) == [(sent['id'], 'ever', '2026-10-27', 'dispatched', 'none')]
            assert {k: sent[k] for k in ('kill_id', 'map', 'instance_id', 'boss_id', 'difficulty', 'size',
                                         'heroic', 'boss_name', 'attempts', 'fight_s', 'first')} == dict(
                kill_id=body['kill_id'], map=631, instance_id=9, boss_id=0, difficulty=1, size=25,
                heroic=False, boss_name='Lord Marrowgar', attempts=0, fight_s=251, first='ever')
            await ack(native, sent['id'])
            assert announcement_rows(native)[0][3:] == ('sent', 'none')
            with native.app.store.transaction():
                receipt = native.app.store.conn.execute(
                    "SELECT kind,stage,ok FROM realm_receipt WHERE command_id=?", (sent['id'],)).fetchone()
            assert tuple(receipt) == ('announce', 'ack', 1)
            # The same kill again, and another kill of the same mode this week: nothing more.
            await native.deliver(body)
            await native.deliver(fresh_kill(clock, kill_id='b' * 32 + ':2', instance_id=10))
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(announces(native)) == 1 and len(announcement_rows(native)) == 1
            assert not native.app.realm._children
    run_native(scenario())


def test_native_attempt_count_reaches_the_command_and_its_stored_payload(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            await native.deliver(fresh_kill(clock, attempts=4))
            await reached(lambda: announces(native))
            (sent,) = announces(native)
            assert (sent['attempts'], sent['fight_s']) == (4, 251)
            with native.app.store.transaction():
                stored = native.app.store.conn.execute(
                    'SELECT payload_json FROM realm_announcement WHERE command_id=?', (sent['id'],)).fetchone()
            assert json.loads(stored['payload_json'])['attempts'] == 4
            assert {k: v for k, v in sent.items() if k not in ('v', 'type', 'ts')} == json.loads(stored['payload_json'])
    run_native(scenario())


def test_ack_after_disable_settles(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            await native.deliver(fresh_kill(clock))
            await reached(lambda: announces(native))
            (sent,) = announces(native)
            (tmp_path / 'mind.toml').write_text('[talk]\nchattiness = 0\n[realm]\nenabled = false\n',
                                                encoding='utf-8')
            native.app.realm.reload()
            assert not native.app.realm.enabled
            await ack(native, sent['id'], ok=False, error='quiet')
            assert announcement_rows(native)[0][3:] == ('refused', 'native_refused')
    run_native(scenario())


def test_disabled_announces_nothing(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch, enabled=False) as native:
            await native.deliver(fresh_kill(clock))
            for _ in range(20):
                await asyncio.sleep(0)
            assert announces(native) == [] and announcement_rows(native) == []
            with native.app.store.transaction():
                assert native.app.store.conn.execute('SELECT count(*) FROM realm_kill').fetchone()[0] == 0
    run_native(scenario())


def test_private_kill_through_the_service_never_announces(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            await native.deliver(fresh_kill(clock, human_present=False, cast_members=[101, 102, 103, 104],
                                            fight_started_at_ms=0, fight_ms=-1))
            for _ in range(20):
                await asyncio.sleep(0)
            assert announces(native) == [] and announcement_rows(native) == []
            with native.app.store.transaction():
                assert native.app.store.conn.execute('SELECT fact FROM realm_kill').fetchone()[0] is None
    run_native(scenario())


# -- the schema --------------------------------------------------------------------------------

def _v12(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 12])
        conn = db.open_db(tmp_path / 'v12.db')
    with conn:
        for identifier in (1, 2, 3):
            _insert(conn, 'game_event', dict(id=identifier, ts=1000.0, type='guard', payload='{}'))
            _insert(conn, 'realm_source', _source(identifier))
            _insert(conn, 'realm_kill', _kill(kill_id='a' * 32 + f':{identifier}', source=identifier))
        _insert(conn, 'realm_announcement', _announcement(kill_id='a' * 32 + ':1', state='sent',
                dispatched_at=1001.0, settled_at=1002.0))
    return conn, files


def _schema(conn, *, announcement=False):
    rows = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    return [tuple(r) for r in rows if (r[2] == 'realm_announcement') == announcement]


def test_v12_upgrade_keeps_announcements_and_allows_ever(tmp_path, monkeypatch):
    conn, files = _v12(tmp_path, monkeypatch)
    # This historical upgrade stops at v13 even when newer migrations exist.
    monkeypatch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 13])
    try:
        others = _schema(conn)
        columns = conn.execute('PRAGMA table_info(realm_announcement)').fetchall()
        keys = conn.execute('PRAGMA foreign_key_list(realm_announcement)').fetchall()
        before = [tuple(r) for r in conn.execute('SELECT * FROM realm_announcement')]
        assert db.migrate(conn) == 13 == db.latest_version()
        assert db.migrate(conn) == 13
        assert [tuple(r) for r in conn.execute('SELECT * FROM realm_announcement')] == before
        assert _schema(conn) == others
        assert [tuple(r) for r in conn.execute('PRAGMA table_info(realm_announcement)')] == [tuple(r) for r in columns]
        assert [tuple(r) for r in conn.execute('PRAGMA foreign_key_list(realm_announcement)')] == [tuple(r) for r in keys]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with conn:
            _insert(conn, 'realm_announcement', _announcement(command_id='announce_2', kill_id='a' * 32 + ':2',
                    first='ever', period_key='2026-11-10'))
        for overrides in (dict(period_key='2026-11-17'), dict(difficulty=-1, period_key='2026-11-17'),
                          dict(first='month', period_key='2026-11-17')):
            with pytest.raises(sqlite3.IntegrityError), conn:
                _insert(conn, 'realm_announcement', _announcement(**{'command_id': 'announce_3',
                        'kill_id': 'a' * 32 + ':3', 'first': 'ever', **overrides}))
        with conn:  # A week row in another mode is still free.
            _insert(conn, 'realm_announcement', _announcement(command_id='announce_3', kill_id='a' * 32 + ':3',
                    difficulty=-1, period_key='2026-11-17'))
    finally:
        conn.close()


def test_failed_0013_rolls_back_the_rebuild(tmp_path, monkeypatch):
    conn, files = _v12(tmp_path, monkeypatch)
    try:
        schema = _schema(conn) + _schema(conn, announcement=True)
        rows = [tuple(r) for r in conn.execute('SELECT * FROM realm_announcement')]
        migration = next(path for version, path in files if version == 13)
        broken = tmp_path / '0013_realm_announce_ever.sql'
        broken.write_text(migration.read_text(encoding='utf-8') + '\nCREATE TABLE realm_kill(x);', encoding='utf-8')
        monkeypatch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 12] + [(13, broken)])
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 12
        assert _schema(conn) + _schema(conn, announcement=True) == schema
        assert [tuple(r) for r in conn.execute('SELECT * FROM realm_announcement')] == rows
    finally:
        conn.close()

