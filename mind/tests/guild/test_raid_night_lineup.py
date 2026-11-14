"""RaidNight._form with the guild's posted lineup (decisions.lineup): its members seated first, its
bench a last resort, unavailable members filled as without one, and it is in effect once formed
from. A lineup posted after the raid formed changes nothing in it; the next formation uses its own."""
import asyncio
from functools import wraps
import json
import logging

import pytest

from leeroy_mind.decisions import ledger
from leeroy_mind.decisions.lineup import LineupRules, NIGHT_S, formed, subject_of, tonight
from leeroy_mind.guild.protocol import FormRaid
from leeroy_mind.guild.raid_night import State
from leeroy_mind.storage.store import Store
from .fakes import member
from .test_raid_night import chat, setup  # noqa: F401  (the raid night fixture)

HOUR = 3600.0
LINEUP = [101, 103, 102, 111, 112, 104, 121, 122, 123]   # a 10: two tanks, three healers, four damage


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def posting(setup):
    """The live ports' lineup hooks over the fake's own database, and a way to post a lineup."""
    ports, armory, night = setup
    store = Store(ports.conn, clock=ports.clock)
    ports.lineup = lambda: tonight(ports.conn, ports.clock.now())
    ports.lineup_formed = lambda lineup, raid_id: formed(store, lineup.decision, raid_id, now=ports.clock.now())

    def post(ident, at, members=LINEUP, bench=(124,)):
        with store.transaction():
            store.conn.execute("INSERT INTO realm_calendar(id,kind,creator,at,title,ref,updated,event_id,state) "
                               "VALUES (?,'raid',101,?,'Raid night','raid:x',?,?,'posted')", (ident, at, at, 50 + ident))
        decision = ledger.propose(store, kind='lineup', subject=subject_of(ident),
                                  value={'members': list(members), 'bench': list(bench)}, positions=[],
                                  now=ports.clock.now())
        ledger.settle(store, decision, settled_by=101, now=ports.clock.now())
        return decision
    return store, post


async def form10(setup):
    ports, _, night = setup
    if night.state in (State.IDLE, State.ENDED):
        night.state = State.CALLED
        night._call_deadline = ports.clock.now() + 600
    await night.on_chat(chat(ports, 'icc 10'))
    await night.tick()


def formed_members(ports):
    return [c for c in ports.sent if isinstance(c, FormRaid)][-1].members


@_run_async
async def test_form_follows_the_posted_lineup(setup, posting):
    ports, _, night = setup
    store, post = posting
    decision = post(7, ports.clock.now() + HOUR)
    await form10(setup)
    assert night.state is State.ASSEMBLING
    assert set(formed_members(ports)) == set(LINEUP) and 1 not in formed_members(ports)
    row = ledger.get(ports.conn, decision)
    assert row['status'] == 'in_effect' and json.loads(row['effect_ref']) == {'raid': night.session.raid_id}
    # The bench is the raid's bench too (standby DKP), and he is never on it
    assert 124 in night.run.plan.bench and 1 not in night.run.plan.bench


@_run_async
async def test_without_a_lineup_the_formation_is_todays(setup, posting):
    ports, _, night = setup
    await form10(setup)
    # Officers first, as always: the lineup's damage would not have been chosen
    assert {104, 108, 109, 120} <= set(formed_members(ports))
    assert not {121, 122, 123} & set(formed_members(ports))


@_run_async
async def test_unavailable_member_is_filled_and_logged(setup, posting, caplog):
    ports, _, night = setup
    store, post = posting
    decision = post(7, ports.clock.now() + HOUR)
    ports.members[122] = member(122, 'M122', 8, 1, online=False)
    with caplog.at_level(logging.INFO, logger='leeroy_mind.guild.raid_night'):
        await form10(setup)
    members = formed_members(ports)
    assert len(members) == 9 and 122 not in members and set(LINEUP) - {122} <= set(members)
    assert 124 not in members  # the bench only when nobody else can fill the seat
    assert f'lineup_partial id={decision} missing=1' in [r.getMessage() for r in caplog.records]


@_run_async
async def test_the_bench_fills_a_role_only_when_nobody_else_can(setup, posting):
    ports, _, night = setup
    store, post = posting
    post(7, ports.clock.now() + HOUR, bench=(105,))
    ports.members[103] = member(103, 'M103', 1, 2, online=False)
    await form10(setup)
    assert 105 not in formed_members(ports) and {101, 107} <= set(formed_members(ports))


@_run_async
async def test_the_bench_is_the_last_resort(setup, posting):
    ports, _, night = setup
    store, post = posting
    post(7, ports.clock.now() + HOUR, bench=(105,))
    for guid in (103, 106, 107):
        ports.members[guid] = member(guid, f'M{guid}', 1, 2, online=False)
    await form10(setup)
    assert {101, 105} <= set(formed_members(ports))


@_run_async
async def test_lineup_after_form_waits_for_the_next_night(setup, posting):
    ports, _, night = setup
    store, post = posting
    await form10(setup)
    first = list(formed_members(ports))
    sent = len(ports.sent)
    # Posted after the raid formed: the raid stands as formed, nothing is sent, nothing is in effect
    late = post(7, ports.clock.now() + HOUR)
    for _ in range(3):
        await night.tick()
    assert [c for c in ports.sent[sent:] if isinstance(c, FormRaid)] == []
    assert ledger.get(ports.conn, late)['status'] == 'settled'
    assert set(first) != set(LINEUP)
    # Its night ends: it was never formed from, so it stays talk
    night._end('called')
    await night.tick()
    await ports.clock.advance(HOUR + NIGHT_S, step=HOUR + NIGHT_S)
    LineupRules()._end(store, ports.clock.now())
    assert ledger.get(ports.conn, late)['status'] == 'talk_only'
    # The next night's lineup is the next formation's
    nxt = post(8, ports.clock.now() + HOUR)
    await form10(setup)
    assert set(formed_members(ports)) == set(LINEUP)
    assert ledger.get(ports.conn, nxt)['status'] == 'in_effect'


# -- his own add-built group (P2-1): the lineup takes effect through his add when the picker followed it

from dataclasses import replace as _replace  # noqa: E402

from leeroy_mind.protocol import Login, Zone  # noqa: E402

from .test_guild_group import GROUP, group_event  # noqa: E402


async def adopt(setup, followed):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.tick()
    await night.on_zone(Zone(0, 1, 4812, "Icecrown Citadel"))
    await night.on_guild_group(_replace(group_event(ports), roster_followed=followed))
    await night.tick()
    await night.tick()


@_run_async
async def test_his_add_that_followed_the_roster_puts_the_lineup_in_effect(setup, posting):
    ports, _, night = setup
    store, post = posting
    decision = post(7, ports.clock.now() + HOUR, members=GROUP, bench=(125, 126))
    ports.lineup_formed = lambda lineup, raid_id, **kw: formed(store, lineup.decision, raid_id,
                                                               now=ports.clock.now(), **kw)
    await adopt(setup, True)
    assert night.state is State.RUNNING
    row = ledger.get(ports.conn, decision)
    assert row['status'] == 'in_effect'
    assert json.loads(row['effect_ref']) == {'raid': night.session.raid_id, 'via': 'add'}
    # Its bench stands by, with standby DKP, as a formed raid's bench does; he is never on it
    raid_id = night.session.raid_id
    assert night.run.plan.bench == [125, 126]
    statuses = dict(ports.conn.execute('SELECT guid,status FROM guild_attendance WHERE raid_id=?', (raid_id,)))
    assert (statuses[125], statuses[126], statuses[1]) == ('benched', 'benched', 'raided')
    standby = {g for (g,) in ports.conn.execute("SELECT guid FROM dkp_ledger WHERE raid_id=? AND reason='standby'",
                                               (raid_id,))}
    assert standby == {125, 126}


@_run_async
async def test_his_add_that_did_not_follow_it_leaves_the_lineup_talk(setup, posting):
    ports, _, night = setup
    store, post = posting
    decision = post(7, ports.clock.now() + HOUR, members=GROUP, bench=(125,))
    await adopt(setup, False)
    assert night.state is State.RUNNING and night.run.plan.bench == []
    assert ledger.get(ports.conn, decision)['status'] == 'settled'
    await ports.clock.advance(HOUR + NIGHT_S, step=HOUR + NIGHT_S)
    LineupRules()._end(store, ports.clock.now())
    assert ledger.get(ports.conn, decision)['status'] == 'talk_only'


@_run_async
async def test_a_second_formation_the_same_night_ignores_a_lineup_posted_after_the_first(setup, posting):
    ports, _, night = setup
    store, post = posting
    await form10(setup)
    await ports.clock.advance(60, step=60)
    late = post(7, ports.clock.now() + HOUR)
    night._end('called')
    await night.tick()
    await form10(setup)
    assert set(formed_members(ports)) != set(LINEUP)
    assert ledger.get(ports.conn, late)['status'] == 'settled'
