"""Poaching over a real Store: at most one offer a week, from a progression officer, to a member
with a grievance; a private fact for that guild, her private memory, and nothing sent."""
import json

import pytest

from conftest import run
from leeroy_mind.memory.writer import MemoryWriter, WriteResult
from leeroy_mind.realm.calendar_time import product_period
from leeroy_mind.realm.fact_store import FactStore
from test_realm_race import DAY, HUMAN, T0, FakeRealm, at, facts, seed_rivals

OFFICERS = ((201, 50, "Vexmor", "officer", "active"),   # The one who may ask.
            (202, 50, "Kaldrin", "officer", "active"),  # He kicked her once.
            (203, 60, "Drammo", "officer", "active"),   # A drama officer.
            (204, 70, "Gankle", "ganker", "active"),
            (205, 80, "Oldhand", "officer", "active"),  # Her guild retired.
            (206, 50, "Gonehand", "officer", "retired"),
            (103, 50, "Coros", "officer", "active"))    # A member of his guild.


def seed_officers(store):
    with store.transaction():
        for guid, guild, name, role, status in OFFICERS:
            store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added,status) VALUES (?,?,?,?,?,?)",
                               (guid, guild, name, role, 1.0, status))
        store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (202,1.0,3)")


@pytest.fixture
def realm(store, clock):
    seed_rivals(store)
    seed_officers(store)
    return FakeRealm(store, clock)


@pytest.fixture
def poach(realm):
    from leeroy_mind.realm.poach import Poach
    return Poach(realm)


def offers(store):
    return facts(store, "poach_offer_v1")


def test_one_poach_a_week(poach, realm, store, clock):
    realm.ports.streaks = {101: 2, 102: 3, 104: 2}
    first = poach.weekly(at(clock, T0))
    assert first is not None and first["member"] in (101, 102, 104) and first["officer"] == 201
    for hours in (1, 24, 72):
        assert poach.weekly(at(clock, T0 + hours * 3600)) is None
    assert len(offers(store)) == 1
    second = poach.weekly(at(clock, T0 + 7 * DAY))
    assert second is not None and second["period"] != first["period"]
    assert len(offers(store)) == 2
    assert {json.loads(store.get_meta("realm:poach:week"))["period"]} == {product_period(T0 + 7 * DAY).key}


def test_poach_needs_a_grievance(poach, realm, store, clock):
    at(clock, T0)
    assert poach.grievances(T0) == {} and poach.weekly(T0) is None
    realm.ports.streaks = {101: 1}  # One bench is not a grievance.
    assert poach.weekly(T0) is None
    assert offers(store) == [] and store.get_meta("realm:poach:week") is None
    realm.ports.losses = {105: 1}  # A close loot loss within 7 days is.
    offer = poach.weekly(T0)
    assert offer["member"] == 105 and offer["grievance"] == ["loot"]
    assert set(realm.ports.loss_since) == {T0 - 7 * DAY}


def test_grievance_without_stances(poach, realm, store):
    realm.ports.streaks = {101: 2}
    assert store.conn.execute("SELECT count(*) FROM realm_stance").fetchone()[0] == 0
    assert poach.grievances(T0) == {101: ("Arkon", ("bench",))}  # Bench and loot only, no error.
    with store.transaction():  # Once stances exist, the losing side of a settled issue counts.
        store.conn.execute("INSERT INTO realm_issue(id,key,title,opened,closed,outcome,status) "
                           "VALUES (1,'recruiting','Should we recruit?',1,2,1,'settled'),"
                           "(2,'dkp_decay','Is the weekly DKP decay fair?',1,NULL,NULL,'live')")
        store.conn.execute("INSERT INTO realm_stance(issue,guid,value,anchor,stubborn,updated) VALUES "
                           "(1,102,-0.7,-0.7,0.5,1),(1,103,-0.2,-0.2,0.5,1),(2,104,-0.9,-0.9,0.5,1)")
    assert poach.grievances(T0) == {101: ("Arkon", ("bench",)), 102: ("Belra", ("stance",))}


def test_poach_targets_never_a_former_member_or_him(poach, realm, store):
    realm.ports.streaks = {101: 4, 105: 2, HUMAN.guid: 9}
    with store.transaction():
        store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (105,2.0,3)")  # Not taken back.
    assert set(poach.grievances(T0)) == {101}


def test_poach_comes_from_a_progression_officer(poach, realm, store, clock):
    assert [o[0] for o in poach.officers()] == [201]  # Never kicked, a member, drama, ganker or retired.
    with store.transaction():
        store.conn.execute("UPDATE realm_actor SET status='retired' WHERE guid=201")
    realm.ports.streaks = {101: 2}
    assert poach.officers() == [] and poach.weekly(at(clock, T0)) is None
    assert offers(store) == []


def test_poach_is_private_and_refused(poach, realm, store, clock):
    writer = MemoryWriter(store, None, clock, human=lambda: HUMAN)  # A keyed memory: never embedded.
    realm.ports.write = lambda candidate: writer.write(candidate, enforce_limits=False)
    realm.ports.streaks = {104: 3}
    offer = poach.weekly(at(clock, T0))
    memory = run(poach.remember_pending())
    assert type(memory) is int and memory > 0
    row = store.conn.execute("SELECT * FROM memory WHERE id=?", (memory,)).fetchone()
    assert row["owner"] == 104 and row["text"] == (
        "Vexmor from Synthetic Progression whispered me an offer to join them. I said no.")
    assert row["text"].endswith("I said no.")
    assert (row["scope"], row["scope_with"]) == ("private", 201)
    assert (row["source"], row["importance"], row["valence"], row["kind"]) == ("game", 5, -0.1, "event")
    # The guild's own private fact: the rules may read it, a prompt never sees it.
    (fact,) = offers(store)
    assert fact["public"] == 0 and json.loads(fact["known_json"]) == [[50, "own"]]
    assert fact["text"] == "Vexmor from Synthetic Progression whispered Dunna an offer to join them."
    assert FactStore(store).known(50, human=HUMAN) == () and FactStore(store).known(21, human=HUMAN) == ()
    assert [r["id"] for r in FactStore(store).known_all(50)] == [fact["id"]] and offer["fact"] == fact["id"]
    # The answer is no by rule: nothing reaches the game, now or on a later tick.
    assert realm.ports.sent == []
    assert run(poach.remember_pending()) is None
    assert store.conn.execute("SELECT count(*) FROM memory WHERE owner=104").fetchone()[0] == 1
    assert json.loads(store.get_meta("realm:poach:week"))["memory"] == memory


def test_memory_retried_until_written(poach, realm, store, clock):
    from leeroy_mind.realm.ports import RealmDisabled
    realm.ports.streaks = {101: 2}
    poach.weekly(at(clock, T0))
    calls = []

    async def disabled(candidate):
        calls.append(candidate)
        raise RealmDisabled("realm disabled")
    realm.ports.write = disabled
    assert run(poach.remember_pending()) is None
    assert json.loads(store.get_meta("realm:poach:week"))["memory"] is None
    realm.ports.write = None  # The fake writer: memory 1.
    assert run(poach.remember_pending()) == 1
    assert len(calls) == 1 and realm.ports.remembered[0] == realm.ports.remembered[1]
    assert realm.ports.remembered[0].key == f"realm:poach:{product_period(T0).key}"

    async def rejected(candidate):
        return WriteResult(None, "rejected")
    poach.weekly(at(clock, T0 + 7 * DAY))
    realm.ports.write = rejected
    assert run(poach.remember_pending()) == 0  # Given up for good: never retried.
    assert run(poach.remember_pending()) is None


def test_tick_offers_and_remembers(poach, realm, store, clock):
    realm.ports.streaks = {101: 2}
    at(clock, T0)

    async def go():
        await poach.tick()
        for task in tuple(realm.spawned):
            await task
    run(go())
    assert len(offers(store)) == 1 and len(realm.ports.remembered) == 1
    assert json.loads(store.get_meta("realm:poach:week"))["memory"] == 1
    assert realm.ports.sent == []


def test_poach_never_while_disabled(poach, realm, store, clock):
    realm.ports.streaks = {101: 2}
    realm.enabled = False
    before = store.conn.total_changes
    assert poach.weekly(at(clock, T0)) is None
    run(poach.tick())
    assert store.conn.total_changes == before and realm.spawned == []


def test_attach_makes_no_io(store, clock):
    from leeroy_mind.realm import poach as poach_module
    seed_rivals(store)
    before = store.conn.total_changes
    part, events = poach_module.attach(FakeRealm(store, clock))
    assert type(part) is poach_module.Poach and events == () and store.conn.total_changes == before
