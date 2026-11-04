"""The realm's ledger reads, former members, guard alerts and the way back in over actual Store rows."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast, unit
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.memory.writer import MemoryWriter
from leeroy_mind.nightly.knowledge import ROOT_PREFIXES, roots
from leeroy_mind.personas.io import save_persona
from leeroy_mind.realm import protocol
from leeroy_mind.realm.calendar_time import local_write_day
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.ledger import Ledger, former_members_in_unit, put_notable_in_unit
from leeroy_mind.realm.memory_eligibility import realm_inputs_in_unit
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.provenance import (audit_realm_reads_in_unit, public_fact_in_unit,
                                          stored_forum_candidate_in_unit)
from leeroy_mind.realm.records import (BackIn, FactRow, ForumCandidate, PreparedBackIn, RealmInputs,
                                       Rejected)
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.wow import CLASSES, RACES
from realm.fakes import RealmApp, enabled
from test_realm_live_proof import HUMAN, insert, observe, roster
from test_realm_provenance import event as boss_event, memory as source_memory, rumor
from test_realm_source_admission import TABLES, application, counts, run as run_native

QUILLON = replace(HUMAN, real_name='Quillon')


def ports(store, human=HUMAN):
    return SimpleNamespace(human=lambda: human, now=store.clock.now)


def ledger(store, human=HUMAN):
    return Ledger(store, ports(store, human), FactStore(store))


def record(store, policy='bg_result_v1', *, human=HUMAN, **kw):
    with store.transaction():
        observation = observe(store, policy, human=human, **kw)
    return FactStore(store).record(observation, human=human)


def persona(store, guid, name='Fenwick', **game):
    save_persona(store, make_persona(guid, name, **game), source='seed', ts=1000.0,
                 race=RACES[2], class_name=CLASSES[1])


def realm_rows(store):
    with store.transaction():
        return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES)


def forum_rumor(store, identifier, *, scope='open', last_told=1000.0):
    """One stored game-topic rumor: boss event, open source memory and live rumor."""
    with store.transaction():
        boss_event(store, identifier)
        source_memory(store, identifier, evidence=json.dumps([f'event:{identifier}']), scope=scope)
        rumor(store, identifier, root=f'event:{identifier}', source=identifier, last_told=last_told)


# -- known(): the prompt's facts ---------------------------------------------------------

def test_prompt_facts_are_public_only(roster):
    public = record(roster)
    revoked = record(roster, payload={'ts': 1002000}, captured=1011.0)
    assert type(public) is FactRow and type(revoked) is FactRow
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (revoked.id,))
        poach = insert(roster, 'realm_fact', kind='poach', text='Synthetic Guild took a raider.',
                       source='rule', ref='', public=0, ts=1003.0)
        insert(roster, 'realm_knows', fact=poach, guild=50, since=1003.0, how='own')
    facts = ledger(roster)
    assert facts.known(50) == (public,)
    assert facts.known(60) == (public,)
    assert {row['id'] for row in facts.known_all(50)} == {public.id, revoked.id, poach}
    assert {row['public'] for row in facts.known_all(50)} == {0, 1}


def test_guild_kill_is_server_wide_fact(roster):
    with roster.transaction():
        for guild in (80, 90):
            insert(roster, 'realm_guild', id=guild, role='gankers', faction='alliance',
                   name='Synthetic Rival', added=1000.0, status='active')
    kill = record(roster, 'verified_raid_kill_v1')
    assert type(kill) is FactRow
    assert (kill.kind, kill.text, kill.public) == ('kill', 'The guild killed Lord Marrowgar.', True)
    with roster.transaction():
        assert [tuple(row) for row in roster.conn.execute('SELECT kind,public FROM realm_fact')] == [('kill', 1)]
        assert [tuple(row) for row in roster.conn.execute(
            'SELECT guild,how FROM realm_knows ORDER BY guild')] == [
            (50, 'own'), (60, 'server'), (80, 'server'), (90, 'server')]
    facts = ledger(roster)
    for guild in (50, 60, 80, 90):
        assert facts.known(guild) == (kill,)


@pytest.mark.parametrize('channel', ['whisper', 'guild', 'officer'])
def test_whispers_and_guild_chat_never_become_facts(tmp_path, clock, monkeypatch, channel):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            line = dict(v=1, ts=1000000, channel=channel, from_guid=10, from_name='Testmaster',
                        text='Ready for Icecrown Citadel.', zone=0)
            await native.deliver(dict(line, type='chat', to_guid=101 if channel == 'whisper' else 0, map=0))
            with app.store.transaction():
                assert app.store.conn.execute('SELECT channel FROM chat_line').fetchall()[-1][0] == channel
            assert native.captures == []  # Ordinary chat is never a Realm source.
            await native.deliver(dict(line, type='channel_chat', via_bridge=False))
            assert len(native.results) == 1 and type(native.results[0]) is Rejected
            assert counts(app.store) == (0, 0, 0, 0, 0)
            assert app.realm.ledger.known(50) == ()
            # The same text on a public channel is a fact, so only the channel kept it out.
            await native.deliver(dict(line, type='channel_chat', channel='trade', via_bridge=False))
            assert [row.text for row in app.realm.ledger.known(50)] == [
                'A public trade message discussed Icecrown Citadel.']
    run_native(scenario())


def test_real_name_never_reaches_a_fact(roster):
    for text in ('Quillon: Ready for Icecrown Citadel.', 'Ready for Icecrown Citadel, QUILLON.'):
        dropped = record(roster, 'channel_raid_topic_v1', human=QUILLON, payload={'text': text})
        assert dropped == Rejected('real_name')
    assert realm_rows(roster) == (0, 0, 0, 0, 0)
    assert ledger(roster, QUILLON).known(50) == ()
    kept = record(roster, 'channel_raid_topic_v1', human=QUILLON)
    assert type(kept) is FactRow and 'quillon' not in kept.text.casefold()


# -- forum_candidates(): what the forum may talk about -------------------------------------

def test_game_rumor_is_forum_material(store):
    forum_rumor(store, 44)
    with store.transaction():
        expected = stored_forum_candidate_in_unit(store, 44, human=HUMAN)
    assert type(expected) is ForumCandidate and expected.root == 'event:44'
    assert ledger(store).forum_candidates() == (expected,)
    assert expected.safe_text == 'A rumor concerns the raid encounter with Lord Marrowgar.'


@pytest.mark.parametrize('scope', ['private', 'secret'])
def test_private_rumor_is_not_forum_material(store, scope):
    forum_rumor(store, 44, scope=scope)
    forum_rumor(store, 45)
    candidates = ledger(store).forum_candidates()
    assert [c.rumor_id for c in candidates] == [45]
    with store.transaction():
        store.conn.execute("UPDATE rumor SET status='faded' WHERE id=45")
    assert ledger(store).forum_candidates() == ()


def test_forum_candidates_skip_invalid_without_prefix_cap(store):
    forum_rumor(store, 1, last_told=1000.0)
    for identifier in (2, 3, 4):  # Told more recently, so they come first, but all invalid.
        forum_rumor(store, identifier, scope='private', last_told=2000.0 + identifier)
    forum_rumor(store, 5, last_told=1500.0)
    facts = ledger(store)
    assert [c.rumor_id for c in facts.forum_candidates(limit=1)] == [5]
    assert [c.rumor_id for c in facts.forum_candidates(limit=2)] == [5, 1]
    assert [c.rumor_id for c in facts.forum_candidates()] == [5, 1]
    for limit in (0, 21, True, 1.0):
        with pytest.raises(ValueError):
            facts.forum_candidates(limit=limit)


# -- former members ------------------------------------------------------------------------

def test_former_member_is_never_added(roster):
    persona(roster, 106, motivation='social')  # A stored persona whose row left the roster.
    persona(roster, 10, 'Testmaster', motivation='social')
    with roster.transaction():
        roster.conn.execute('UPDATE cast_member SET level=79 WHERE guid=105')
        insert(roster, 'realm_kick', guid=10, ts=1.0, rank=0)  # Never the human, even kicked.
    facts = ledger(roster)
    assert facts.former_members() == frozenset({106})
    with roster.transaction():
        insert(roster, 'cast_member', guid=106, name='Fenwick', race=2, **{'class': 1},
               gender=0, level=80, rank=3, online=0, updated=1000.0)
        insert(roster, 'realm_kick', guid=106, ts=2.0, rank=3)
        roster.conn.execute('DELETE FROM cast_member WHERE guid=10')
        roster.conn.execute("UPDATE persona_core SET json='{' WHERE guid=104")
        roster.conn.execute('DELETE FROM cast_member WHERE guid=104')
        assert former_members_in_unit(roster, human_guid=10) == frozenset({104, 106})
        assert former_members_in_unit(roster, human_guid=0) == frozenset({10, 104, 106})
        for bad in (-1, 1 << 32, True, '10'):
            with pytest.raises(ValueError):
                former_members_in_unit(roster, human_guid=bad)
    with pytest.raises(RuntimeError):
        former_members_in_unit(roster, human_guid=10)
    # A social former member is never chosen for the way back in, kicked or off the roster.
    fact = record(roster)
    assert 106 not in {plan.owner for plan in facts.back_in(fact.id, forum=True)}


@pytest.mark.parametrize('on', [False, True])
def test_his_kick_makes_a_former_member(tmp_path, store, clock, on):
    seed_test_cast(store)
    app = RealmApp(tmp_path, store, clock)
    enabled(tmp_path, on)
    realm = attach_realm(app)
    assert realm.enabled is on
    assert realm.ledger.store is store and realm.ledger.former_members() == frozenset()
    run(app.event(protocol.Guard(1000, 103, 'his_kick', 4, '')))
    assert realm.ledger.former_members() == frozenset({103})
    assert not app.sent
    app.engine.say_as.assert_not_called()
    run(realm.close())


# -- back_in(): the way back in, a plan only ------------------------------------------------

def social_cast(store, **game):
    for guid, name in ((101, 'Arkon'), (102, 'Belra'), (103, 'Coros'), (104, 'Dunna'), (105, 'Ebbo')):
        persona(store, guid, name, **game.get(str(guid), {'motivation': 'social'}))


def test_back_in_forum_audience(roster):
    social_cast(roster, **{'101': {'motivation': 'social'}, '102': {'motivation': 'competitor'},
                           '103': {'thinks_as': 'social'}, '104': {}, '105': {'motivation': 'explorer'}})
    fact = record(roster)
    before = realm_rows(roster)
    plans = ledger(roster).back_in(fact.id, forum=True)
    assert [plan.owner for plan in plans] == [101, 102, 103]
    with roster.transaction():
        _, proof = public_fact_in_unit(roster, fact.id, human=HUMAN)
        (stamp,) = audit_realm_reads_in_unit(roster, (f'realm:fact:{fact.id}',), human=HUMAN)[1]
        participants = tuple(json.loads(proof.qualification_json)['participants'])
    assert participants == (10, 101, 201)
    assert plans[0] == BackIn(fact.id, 1, stamp.fact_sha256, 101, 'forum', fact.ts, fact.text,
                              participants, proof)
    assert all(replace(plan, owner=101) == plans[0] for plan in plans)
    assert realm_rows(roster) == before
    with roster.transaction():
        for table in ('memory', 'realm_notable', 'realm_memory_dependency'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_back_in_skips_former_human_capped(roster):
    social_cast(roster)
    persona(roster, 10, 'Testmaster', motivation='social')
    fact = record(roster)
    today = local_write_day(roster.clock.now())
    with roster.transaction():
        insert(roster, 'realm_kick', guid=101, ts=1.0, rank=3)
        insert(roster, 'realm_notable', day=today, guid=102, memory=999999)
        roster.conn.execute('DELETE FROM cast_member WHERE guid=103')
        insert(roster, 'realm_notable', day='2000-01-01', guid=104, memory=999998)
    facts = ledger(roster)
    assert [plan.owner for plan in facts.back_in(fact.id, forum=True)] == [104, 105]
    # Without his guid he cannot be told apart from the audience, so nobody is chosen.
    assert ledger(roster, replace(HUMAN, guid=0)).back_in(fact.id, forum=True) == ()
    # A fact without a current active public origin reaches nobody.
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact.id,))
    assert facts.back_in(fact.id, forum=True) == ()


def test_channel_back_in_unsupported(roster):
    social_cast(roster)
    fact = record(roster, 'channel_raid_topic_v1')
    assert fact.source == 'channel'
    facts = ledger(roster)
    assert facts.back_in(fact.id) == ()
    assert facts.back_in(fact.id, place_zone=0) == ()
    assert [plan.owner for plan in facts.back_in(fact.id, forum=True)] == [101, 102, 103, 104, 105]
    for bad in (dict(place_zone=0, forum=True), dict(forum=1), dict(place_zone=-1),
                dict(place_zone=True)):
        with pytest.raises(ValueError):
            facts.back_in(fact.id, **bad)
    for bad in (0, True, '1'):
        with pytest.raises(ValueError):
            facts.back_in(bad, forum=True)


# -- remember_back() and put_notable_in_unit(): the way back in, written ----------------------

VECTOR = pack_vec(unit(3))


class Embed:
    """The writer's embedder seam: one fixed vector, an outage, or work while it is awaited."""
    def __init__(self, during=None, outage=False):
        self.during, self.outage, self.calls = during, outage, []

    async def embed(self, texts, priority):
        self.calls.append(tuple(texts))
        if self.during is not None:
            await self.during()
        if self.outage:
            raise LLMError('offline')
        return [VECTOR for _ in texts]


class NotablePorts:
    """LivePorts.remember_notable's contract: embed under the ticket, then write at once."""
    def __init__(self, store, embedder, human=HUMAN):
        self.store, self.embedder, self._human, self.enabled = store, embedder, human, True
        self.writer = MemoryWriter(store, embedder, store.clock, human=lambda: human)

    def human(self):
        return self._human

    def now(self):
        return self.store.clock.now()

    async def remember_notable(self, text, write):
        if not self.enabled:
            raise RealmDisabled('realm disabled')
        try:
            vector = (await self.embedder.embed([text], Priority.EXTRACTION))[0]
        except LLMError:
            vector = None
        if not self.enabled:
            raise RealmDisabled('realm disabled')
        return write(self.writer, vector)


def notable_ledger(store, embedder=None, human=HUMAN):
    ports = NotablePorts(store, embedder or Embed(), human)
    return Ledger(store, ports, FactStore(store)), ports


def notables(store):
    with store.transaction():
        return [tuple(row) for row in store.conn.execute(
            'SELECT day,guid,memory FROM realm_notable ORDER BY day, guid')]


def memories(store):
    with store.transaction():
        rows = [dict(row) for row in store.conn.execute(
            'SELECT id,owner,kind,text,t_event,t_created,actors,importance,source,evidence,'
            'scope,scope_with,embedding FROM memory ORDER BY id')]
    for row in rows:
        row['actors'], row['evidence'] = json.loads(row['actors']), json.loads(row['evidence'])
    return rows


def prepared(plan, embedding=VECTOR, text=None):
    digest = hashlib.sha256((plan.text if text is None else text).encode('utf-8')).hexdigest()
    return PreparedBackIn(plan, embedding, digest)


def advance(store, seconds):
    run(store.clock.advance(seconds, step=seconds))


def test_way_back_in_one_notable_a_day(roster):
    social_cast(roster)
    first, second = record(roster), record(roster, payload={'ts': 1002000}, captured=1011.0)
    facts, ports = notable_ledger(roster)
    written = run(facts.remember_back(first.id, forum=True))
    owners = (101, 102, 103, 104, 105)
    assert len(written) == 5
    today = local_write_day(roster.clock.now())
    assert notables(roster) == [(today, guid, mid) for guid, mid in zip(owners, written)]
    rows = memories(roster)
    assert [row['id'] for row in rows] == list(written)
    for guid, row in zip(owners, rows):
        assert row == dict(id=row['id'], owner=guid, kind='event', text=first.text,
                           t_event=int(first.ts), t_created=int(roster.clock.now()),
                           actors=[10, 101, 201], importance=4, source='game',
                           evidence=[f'realm:fact:{first.id}'], scope='open', scope_with=0,
                           embedding=VECTOR)
    with roster.transaction():
        assert [tuple(r) for r in roster.conn.execute(
            'SELECT memory,fact FROM realm_memory_dependency ORDER BY memory')] == [
            (mid, first.id) for mid in written]
        # Each is an ordinary memory that the Realm reader also accepts as written.
        for mid in written:
            assert type(realm_inputs_in_unit(roster, (f'memory:{mid}',), human=HUMAN,
                                             mode='memory')) is RealmInputs
    assert ports.embedder.calls == [(first.text,)]  # One vector for the whole audience.
    # A second fact the same day reaches nobody: every slot is taken.
    assert facts.back_in(second.id, forum=True) == ()
    assert run(facts.remember_back(second.id, forum=True)) == ()
    assert len(memories(roster)) == 5 and ports.embedder.calls == [(first.text,)]


def test_notable_holders_never_get_the_same_fact_again(roster):
    social_cast(roster)
    fact = record(roster)
    facts, ports = notable_ledger(roster)
    with roster.transaction():
        insert(roster, 'realm_notable', day=local_write_day(roster.clock.now()), guid=102,
               memory=999999)
    assert len(run(facts.remember_back(fact.id, forum=True))) == 4
    assert [row['owner'] for row in memories(roster)] == [101, 103, 104, 105]
    advance(roster, 86400)  # The next local day: every slot is free again.
    (plan,) = facts.back_in(fact.id, forum=True)
    assert plan.owner == 102
    with roster.transaction():  # 101 already holds this fact.
        assert put_notable_in_unit(roster, ports.writer, prepared(replace(plan, owner=101)),
                                   human=HUMAN, now=roster.clock.now()) == Rejected('ineligible')
    assert len(run(facts.remember_back(fact.id, forum=True))) == 1
    assert sorted(row['owner'] for row in memories(roster)) == [101, 102, 103, 104, 105]


def test_notable_write_revalidates_the_fact(roster):
    social_cast(roster)
    fact = record(roster)

    async def revoke():
        with roster.transaction():
            roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?",
                                (fact.id,))

    facts, ports = notable_ledger(roster, Embed(during=revoke))
    assert run(facts.remember_back(fact.id, forum=True)) == ()
    assert ports.embedder.calls == [(fact.text,)]
    assert memories(roster) == [] and notables(roster) == []


def test_notable_write_rechecks_each_owner_and_slot(roster):
    social_cast(roster)
    fact = record(roster)
    today = local_write_day(roster.clock.now())

    async def change():
        with roster.transaction():
            insert(roster, 'realm_kick', guid=101, ts=1.0, rank=3)        # Now a former member.
            roster.conn.execute('DELETE FROM cast_member WHERE guid=102')  # Left the roster.
            insert(roster, 'realm_notable', day=today, guid=103, memory=999999)  # Slot taken.
        persona(roster, 104, 'Dunna', motivation='explorer')  # No longer reads the forum.

    facts, _ = notable_ledger(roster, Embed(during=change))
    written = run(facts.remember_back(fact.id, forum=True))
    assert [row['owner'] for row in memories(roster)] == [105] and len(written) == 1
    assert notables(roster) == [(today, 103, 999999), (today, 105, written[0])]


def test_notable_refused_for_the_human_former_and_changed_plans(roster):
    social_cast(roster)
    persona(roster, 10, 'Testmaster', motivation='social')
    persona(roster, 106, 'Fenwick', motivation='social')  # Stored persona, off the roster.
    fact = record(roster)
    facts, ports = notable_ledger(roster)
    plan = facts.back_in(fact.id, forum=True)[0]
    now = roster.clock.now()

    def put(item, human=HUMAN):
        return put_notable_in_unit(roster, ports.writer, item, human=human, now=now)

    with roster.transaction():
        for owner in (10, 106):
            assert put(prepared(replace(plan, owner=owner))) == Rejected('ineligible')
        assert put(prepared(replace(plan, reason='channel'))) == Rejected('unsupported_policy')
        # Without his guid he could be the owner, so nobody is written.
        assert put(prepared(plan), replace(HUMAN, guid=0)) == Rejected('ineligible')
        # A plan that is not the stored fact's own: text, actors, time, revision or node.
        for changed in (dict(text='The guild killed Lord Marrowgar.'), dict(actors=(10, 101)),
                        dict(source_at=fact.ts + 1), dict(fact_revision=2),
                        dict(fact_sha256='b' * 64)):
            assert put(prepared(replace(plan, **changed))) == Rejected('proof_changed')
        # A vector for another text is never stored with this one.
        assert put(prepared(plan, text='Another text.')) == Rejected('invalid_input')
    assert memories(roster) == [] and notables(roster) == []
    with pytest.raises(RuntimeError):  # Only inside the caller's unit.
        put(prepared(plan))
    with roster.transaction():
        for bad in (plan, None):
            with pytest.raises(ValueError):
                put(bad)
        with pytest.raises(ValueError):  # Another store's writer.
            put_notable_in_unit(roster, MemoryWriter(None, ports.embedder, roster.clock),
                                prepared(plan), human=HUMAN, now=now)


def test_notable_slot_clash_rolls_back_both(roster):
    social_cast(roster)
    fact = record(roster)
    facts, ports = notable_ledger(roster)
    plan = facts.back_in(fact.id, forum=True)[0]
    now = roster.clock.now()
    day = local_write_day(now)

    class Racing(MemoryWriter):
        def put(self, row, **kw):  # Another writer takes the same slot inside this unit.
            mid = super().put(row, **kw)
            insert(roster, 'realm_notable', day=day, guid=row.owner, memory=mid)
            return mid

    racing = Racing(roster, ports.embedder, roster.clock, human=lambda: HUMAN)
    with roster.transaction():
        roster.set_meta('outer', 'kept')
        assert put_notable_in_unit(roster, racing, prepared(plan), human=HUMAN,
                                   now=now) == Rejected('capped')
    assert roster.get_meta('outer') == 'kept'  # Only the notable's own writes are undone.
    assert memories(roster) == [] and notables(roster) == []
    with roster.transaction():
        for table in ('realm_memory_dependency', 'memory_vec', 'memory_actor'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_notable_day_is_the_local_write_day(roster):
    social_cast(roster)
    fact = record(roster)
    # 23:59:50 in New York on 2026-10-29 is 03:59:50 UTC on 2026-10-30.
    advance(roster, datetime(2026, 10, 30, 3, 59, 50, tzinfo=timezone.utc).timestamp()
            - roster.clock.now())
    with roster.transaction():
        insert(roster, 'realm_notable', day='2026-10-29', guid=101, memory=999999)

    async def midnight():
        await roster.clock.advance(20, step=20)

    facts, _ = notable_ledger(roster, Embed(during=midnight))
    assert [plan.owner for plan in facts.back_in(fact.id, forum=True)] == [102, 103, 104, 105]
    written = run(facts.remember_back(fact.id, forum=True))
    # Planned on the 29th, written on the 30th: the slot is the write's local day.
    assert notables(roster) == [('2026-10-29', 101, 999999)] + [
        ('2026-10-30', guid, mid) for guid, mid in zip((102, 103, 104, 105), written)]
    assert {row['t_created'] for row in memories(roster)} == {int(roster.clock.now())}


def test_notable_without_a_vector_is_still_written(roster):
    social_cast(roster)
    fact = record(roster)
    facts, _ = notable_ledger(roster, Embed(outage=True))
    assert len(run(facts.remember_back(fact.id, forum=True))) == 5
    # Stored without one, as the writer stores any memory it cannot embed; the backfill
    # embeds it later.
    assert {row['embedding'] for row in memories(roster)} == {None}


def test_remember_back_writes_nothing_while_disabled(roster):
    social_cast(roster)
    fact = record(roster)
    facts, ports = notable_ledger(roster)
    ports.enabled = False
    assert run(facts.remember_back(fact.id, forum=True)) == ()
    assert ports.embedder.calls == []

    async def disable():
        ports.enabled = False

    facts, ports = notable_ledger(roster, Embed(during=disable))
    assert run(facts.remember_back(fact.id, forum=True)) == ()
    assert ports.embedder.calls == [(fact.text,)]
    assert memories(roster) == [] and notables(roster) == []
    assert run(facts.remember_back(fact.id)) == ()  # A channel audience stays unsupported.
    assert ports.embedder.calls == [(fact.text,)]


def test_remember_back_through_the_live_ports(tmp_path, clock, monkeypatch):
    def switch(on):
        (tmp_path / 'mind.toml').write_text('[talk]\nchattiness = 0\n[realm]\nenabled = '
                                            + str(on).lower() + '\n', encoding='utf-8')

    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            social_cast(app.store)
            await native.deliver(dict(v=1, type='channel_chat', ts=1000000, channel='trade',
                                      from_guid=10, from_name='Testmaster',
                                      text='Ready for Icecrown Citadel.', zone=0, via_bridge=False))
            (fact,) = app.realm.ledger.known(50)
            embeds = lambda: [call['input'] for call in native.llm.calls if 'dimensions' in call]
            before = len(embeds())
            switch(False)
            app.realm.reload()
            assert app.realm.enabled is False
            assert await app.realm.ledger.remember_back(fact.id, forum=True) == ()
            assert len(embeds()) == before and memories(app.store) == [] and notables(app.store) == []
            switch(True)
            app.realm.reload()
            written = await app.realm.ledger.remember_back(fact.id, forum=True)
            assert embeds()[before:] == [[fact.text]]
            assert [(row['owner'], row['text'], row['evidence']) for row in memories(app.store)] == [
                (guid, fact.text, [f'realm:fact:{fact.id}']) for guid in (101, 102, 103, 104, 105)]
            today = local_write_day(clock.now())
            assert notables(app.store) == [(today, guid, mid) for guid, mid in
                                           zip((101, 102, 103, 104, 105), written)]
            assert await app.realm.ledger.remember_back(fact.id, forum=True) == ()
    run_native(scenario())


# -- guard alerts ----------------------------------------------------------------------------

@pytest.mark.parametrize('on', [False, True])
def test_guard_alert_logs_ids_only(tmp_path, store, clock, caplog, on):
    seed_test_cast(store)
    app = RealmApp(tmp_path, store, clock)
    enabled(tmp_path, on)
    realm = attach_realm(app)
    caplog.set_level(logging.DEBUG, logger='leeroy_mind')
    for action in ('gone', 'blocked'):
        run(app.event(protocol.Guard(1000, 103, action, 7, 'Quillon left in a huff')))
    for ignored in (protocol.Guard(1000, 104, 'readd', 7, ''), protocol.Guard(1000, 104, 'failed', 7, 'x'),
                    protocol.Guard(1000, 0, 'gone', 7, ''), protocol.Guard(1000, 1 << 32, 'gone', 7, ''),
                    protocol.Guard(1000, True, 'gone', 7, ''), protocol.Guard(1000, '105', 'gone', 7, ''),
                    protocol.Guard(1000, 105, ['gone'], 7, '')):
        run(app.event(ignored))
    alerts = [r for r in caplog.records if 'realm_guard_alert' in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in alerts] == [
        (logging.WARNING, 'realm_guard_alert guid=103 action=gone'),
        (logging.WARNING, 'realm_guard_alert guid=103 action=blocked')]
    assert 'Quillon' not in caplog.text and 'huff' not in caplog.text
    assert realm.ledger.former_members() == frozenset()
    with store.transaction():
        assert store.conn.execute('SELECT count(*) FROM realm_kick').fetchone()[0] == 0
    assert not app.sent
    run(realm.close())


# -- the knowledge ledger -----------------------------------------------------------------------

def test_realm_prefix_is_a_knowledge_root(store):
    assert 'realm:' in ROOT_PREFIXES
    with store.transaction():
        fact = insert(store, 'realm_fact', kind='kill', text='The guild killed Lord Marrowgar.',
                      source='event', ts=1000.0)
        memory = insert(store, 'memory', owner=101, kind='event', text='We killed Marrowgar.',
                        t_event=1000, t_created=1000, t_accessed=1000, importance=4, valence=0.0,
                        source='game', evidence=json.dumps([f'realm:fact:{fact}']))
    # A Realm memory is ordinary: the ledger follows it to its Realm root like any other.
    assert roots(store, [f'memory:{memory}']) == {f'realm:fact:{fact}'}
    assert roots(store, [f'realm:fact:{fact}', 'owner:canon']) == {f'realm:fact:{fact}'}
