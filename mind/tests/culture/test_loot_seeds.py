"""Culture Part B: loot talk from the guild's own receipts and the loot system's upgrade rule.
Fake armories and libraries stand in for MySQL and library.db; every name is invented."""
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.culture import loot
from leeroy_mind.groups import store as groups
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups.conversations import seeds as group_seeds
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.server import seeds

KEY = "guild:loot:7:abc123"


@pytest.fixture
def cast(store):
    people = seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.execute("ATTACH DATABASE ':memory:' AS library")
    store.conn.execute("CREATE TABLE library.item(id INTEGER, name TEXT)")
    store.conn.execute("CREATE TABLE library.chunks(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, text TEXT)")
    store.conn.executemany("INSERT INTO library.item VALUES (?,?)",
                           [(123, "Invented Blade"), (1001, "Invented Chestguard"), (1005, "Invented Helm")])
    return people


def gather(store, cast, **flags):
    return seeds.gather(store.conn, now=store.clock.now(), local=store.clock.local(), cast=cast,
                        officers=frozenset({102, 104}), human=1, human_name='', trial_rank=4, **flags)


def receipt(store, *, close=True, age=3600.0, debit=True, runner=102, key=KEY):
    now = store.clock.now()
    award = dict(guid=101, paid=47.0, bid=50.0, priority=0, runner_up=runner, runner_up_bid=46.0, close=close)
    store.set_meta(key, json.dumps(dict(state="charged", item_id=123, name="Invented Blade", award=award,
                                        human_claimed=False, bidders=[])))
    if debit:
        return store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) "
                                  "VALUES (?,101,-47,'item',7,123,?)", (now - age, key)).lastrowid
    return None


def test_close_loot_seed_from_receipts(store, cast):
    ledger = receipt(store)
    off = gather(store, cast, culture_enabled=True, culture_store=store)
    assert [s.key for s in off if s.kind in ("loot", "loot_close")] == [f"loot:{ledger}"]
    on = gather(store, cast, culture_enabled=True, culture_store=store, culture_loot=True)
    close = [s for s in on if s.kind == "loot_close"]
    assert [(s.key, s.fact, s.channels, s.about) for s in close] == [
        (f"loot_close:{ledger}", "Arkon beat Belra to [Invented Blade] by 4 DKP", ("loot-and-dkp",), (101, 102))]
    assert not any(s.kind == "loot" for s in on)  # one subject a drop, not two
    assert not any(s.officers_only for s in close)


@pytest.mark.parametrize("case", ["not_close", "no_debit", "too_old", "talked_about", "runner_gone"])
def test_close_loot_needs_a_true_recent_close_award(store, cast, case):
    ledger = receipt(store, close=case != "not_close", debit=case != "no_debit",
                     age=8 * 86400 if case == "too_old" else 3600, runner=999 if case == "runner_gone" else 102)
    if case == "talked_about":
        store.set_meta(f"server:seed:loot:{ledger}", "1")
    assert not any(s.kind == "loot_close"
                   for s in gather(store, cast, culture_enabled=True, culture_store=store, culture_loot=True))


class FakeLibrary:
    def __init__(self):
        self.asked = []

    def drops_from(self, boss):
        self.asked.append(boss)
        chunk = lambda map_id, section, *ids: SimpleNamespace(map_id=map_id, section=section,
                                                             ref_ids=tuple(f"item:{i}" for i in ids))
        return {"Lord Marrowgar": [chunk(631, "10-player", 1001, 1002), chunk(631, "25-player", 1003),
                                   chunk(0, "10-player", 1004)],
                "Lady Deathwhisper": [chunk(631, "10-player (part 2)", 1005)]}.get(boss, [])


class FakeArmory:
    def __init__(self):
        self.asked = []

    def items(self, ids):
        self.asked.append(tuple(ids))
        plate = dict(quality=4, item_class=4, item_subclass=4, allowable_class=-1, stats=frozenset({4}))
        known = {1001: ItemInfo(1001, "Invented Chestguard", item_level=251, inventory_type=5, **plate),
                 1002: ItemInfo(1002, "Invented Robe", 4, 251, 5, 4, 1, -1, frozenset({5})),
                 1003: ItemInfo(1003, "Invented Big Chest", item_level=264, inventory_type=5, **plate),
                 1005: ItemInfo(1005, "Invented Helm", item_level=258, inventory_type=1, **plate)}
        return {i: known[i] for i in ids if i in known}

    def equipped(self, guids):
        return {101: {4: 200, 0: 245}, 102: {}}  # 102's equipment could not be read


def raid_ahead(store):
    now = store.clock.now()
    store.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,"
                       "tank_guid,loot_guid,kills,wipes) VALUES (9,?,?,'icc',10,0,0,101,101,102,3,1)",
                       (now - 3 * 86400, now - 3 * 86400 + 7200))
    store.conn.executemany("INSERT INTO guild_attendance VALUES (9,?,?)",
                           [(101, "raided"), (102, "raided"), (103, "benched")])
    return store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state) "
                              "VALUES ('raid',102,?,'Raid night',?,'posted')", (now + 86400, now)).lastrowid


def test_wish_uses_real_drops_and_gain(store, cast):
    event = raid_ahead(store)
    library, armory = FakeLibrary(), FakeArmory()
    roles = {g: p.play.raid_role for g, p in cast.items()}
    value = loot.compute_wishes(store.conn, library, armory, now=store.clock.now(), raid_roles=roles, human=1)
    assert value["event"] == event and value["raid_key"] == "icc" and value["size"] == 10
    # 1001 (+51 on the chest) beats 1005 (+13 on the helm); 25-player and other-map drops are never asked
    assert value["wishes"] == [{"guid": 101, "item_id": 1001, "boss": "Lord Marrowgar", "gain": 51}]
    assert armory.asked == [(1001, 1002, 1005)]
    assert "Lord Marrowgar" in library.asked and "Sindragosa" in library.asked
    store.set_meta(loot.WISH_KEY, json.dumps(value))
    found = [s for s in gather(store, cast, culture_enabled=True, culture_store=store, culture_loot=True)
             if s.kind == "loot_wish"]
    assert [(s.fact, s.channels, s.about, s.openers, s.weight) for s in found] == [
        ("Arkon needs [Invented Chestguard] from Lord Marrowgar", ("loot-and-dkp", "warrior"), (101,), (101,), 1.0)]
    assert [s for s in gather(store, cast, culture_enabled=True, culture_store=store) if s.kind == "loot_wish"] == []
    store.conn.execute("UPDATE realm_calendar SET state='failed' WHERE id=?", (event,))
    assert not any(s.kind == "loot_wish"
                   for s in gather(store, cast, culture_enabled=True, culture_store=store, culture_loot=True))


def test_no_wish_without_a_posted_raid_or_a_known_raid(store, cast):
    roles = {g: None for g in cast}
    assert loot.compute_wishes(store.conn, FakeLibrary(), FakeArmory(), now=store.clock.now(),
                               raid_roles=roles, human=1) is None
    now = store.clock.now()
    store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state) "
                       "VALUES ('raid',102,?,'Raid night',?,'posted')", (now + 86400, now))
    armory = FakeArmory()
    value = loot.compute_wishes(store.conn, FakeLibrary(), armory, now=now, raid_roles=roles, human=1)
    assert value["wishes"] == [] and value["raid_key"] is None and armory.asked == []


def test_new_subjects_never_reach_group_dms_or_officer_rooms(store, cast):
    on = GroupsConfig(enabled=True)
    now = datetime(2026, 11, 13, 20, tzinfo=timezone.utc)
    groups.create(store, human=1, members=[101, 102], title='Fixture', created_by=1, now=now.timestamp(), config=on)
    source = [seeds.Seed('wow_event:1:1:on', 'wow_event', ('general',), 'Invented Faire is on now'),
              seeds.Seed('loot_close:1', 'loot_close', ('loot-and-dkp',), 'A close one', (101, 102)),
              seeds.Seed('loot_wish:1:101:5', 'loot_wish', ('loot-and-dkp',), 'A wish', (101,))]
    out, _ = group_seeds(store.conn, source, now=now.timestamp(), local=now, human=1, config=on)
    assert out == []
    from leeroy_mind.discord import layout
    assert all(not layout.channel(c).officers_only for s in source for c in s.channels)


def test_wish_tick_waits_while_he_plays_and_logs_only_codes(tmp_path, caplog):
    from selfies.fakes import mind_app
    app = mind_app(tmp_path, toml="[server]\nenabled=true\n[culture]\nenabled=true\nloot=true\n")
    try:
        live = app.server.culture
        assert live is not None and live.loot and not live.calendar
        assert live.on_game_event not in app._handlers.get("game_event", ())
        now = app.clock.now()
        app.store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state) "
                               "VALUES ('raid',102,?,'Raid night',?,'posted')", (now + 86400, now))
        calls = []
        def compute(at, roles, human):
            calls.append(at)
            if len(calls) == 2:
                raise RuntimeError("invented failure text")
            return {"computed": at, "event": 1, "raid_key": None, "wishes": []}
        live._compute = compute
        app.world.human_online = True
        run(live.wish_tick(now))
        assert calls == [] and app.store.get_meta(loot.WISH_KEY) is None
        app.world.human_online = False
        run(live.wish_tick(now))
        run(live.wish_tick(now + 60))  # cached for this raid night: not asked again
        assert calls == [now] and json.loads(app.store.get_meta(loot.WISH_KEY))["event"] == 1
        with caplog.at_level(logging.INFO):
            run(live.wish_tick(now + loot.WISH_REFRESH_S + 1))
        assert "culture_loot_wish_failed code=RuntimeError" in caplog.text and "invented" not in caplog.text
    finally:
        run(app.stop())
