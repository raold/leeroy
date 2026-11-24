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
        (f"loot_close:{ledger}", "Arkon beat Belra to [Invented Blade] by 1 DKP", ("loot-and-dkp",), (101, 102))]
    assert not any(s.kind == "loot" for s in on)  # one subject a drop, not two
    assert not any(s.officers_only for s in close)


@pytest.mark.parametrize("case", ["not_close", "no_debit", "too_old", "talked_about", "runner_gone", "his", "joke"])
def test_close_loot_needs_a_true_recent_close_award(store, cast, case):
    ledger = receipt(store, close=case != "not_close", debit=case != "no_debit",
                     age=8 * 86400 if case == "too_old" else 3600, runner=999 if case == "runner_gone" else 102)
    if case == "talked_about":
        store.set_meta(f"server:seed:loot:{ledger}", "1")
    if case == "his":  # never a subject about him, even as the runner-up
        record = json.loads(store.get_meta(KEY))
        record["award"]["runner_up"] = 1
        store.set_meta(KEY, json.dumps(record))
    if case == "joke":  # Part A made this receipt a running joke: one subject, not two
        store.conn.execute("INSERT INTO lexicon(kind,term,target,origin_ref,originator,born,uses,heat,last_used,status) "
                           "VALUES ('joke','invented label',0,?,101,0,0,1,0,'active')", ("loot:close_loot:" + KEY,))
    assert not any(s.kind == "loot_close"
                   for s in gather(store, cast, culture_enabled=True, culture_store=store, culture_loot=True))


class FakeLibrary:
    def __init__(self):
        self.asked = []

    def drops_from(self, boss):
        self.asked.append(boss)
        def chunk(map_id, section, *ids, title=boss):
            return SimpleNamespace(map_id=map_id, section=section, title=title,
                                   ref_ids=tuple(f"item:{i}" for i in ids))
        return {"Lord Marrowgar": [chunk(631, "10-player", 1001, 1002), chunk(631, "25-player", 1003),
                                   chunk(0, "10-player", 1004)],
                "Lady Deathwhisper": [chunk(631, "10-player (part 2)", 1005)],
                # a word-match fallback can hand back another source of the same raid
                "Rotface": [chunk(631, "10-player", 1006, title="Invented Other Chest")]}.get(boss, [])


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
            return {"computed": at, "event": 1, "raid_key": None, "wishes": [],
                    "version": loot.WISH_VERSION}
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


def test_close_loot_casts_the_runner_up_only_through_the_kindness_gate():
    import random
    from leeroy_mind.server.rhythm import choose_cast
    from conftest import make_persona
    people = {g: make_persona(g, f"Member{g}") for g in (101, 102, 103)}
    states = {g: SimpleNamespace(presence=SimpleNamespace(state="online")) for g in people}
    seed = seeds.Seed("loot_close:1", "loot_close", ("loot-and-dkp",), "A close one", (101, 102))
    args = dict(officers=frozenset(), human=1, in_play=frozenset(), ties=lambda a, b: 1, rng=random.Random(3))
    asked = []
    assert choose_cast("loot-and-dkp", seed, states, people, **args,
                       tease=lambda a, b: asked.append((a, b)) or False) == []
    assert {b for _, b in asked} == {102}  # the runner-up is the one the gate protects
    for _ in range(20):
        cast = choose_cast("loot-and-dkp", seed, states, people, **args, tease=lambda a, b: True)
        assert cast and cast[0] != 102


def test_a_delivered_close_thread_spends_the_runner_ups_allowance_once(tmp_path):
    from leeroy_mind.culture.tease import TEASED_KEY
    from leeroy_mind.server.engine import Conversation
    from selfies.fakes import mind_app
    app = mind_app(tmp_path, toml="[server]\nenabled=true\n[culture]\nenabled=true\nloot=true\n")
    try:
        seed = seeds.Seed("loot_close:1", "loot_close", ("loot-and-dkp",), "A close one", (101, 102))
        talk = Conversation("c1", "loot-and-dkp", seed, [101, 103], app.clock.now(), 5)
        engine = app.server.engine
        engine._record_close(talk, 1, 1, app.clock.now())
        engine._record_close(talk, 2, 2, app.clock.now() + 60)
        key = TEASED_KEY.format(date=app.clock.local().date().isoformat(), guid=102)
        assert app.store.get_meta(key) == str(app.clock.now())
        engine._settle_joke(talk, None)
        assert talk.teased
    finally:
        run(app.stop())


def test_wishes_follow_his_raids_before_the_members_offline_ones(store, cast):
    raid_ahead(store)  # his ICC 10, three days ago
    now = store.clock.now()
    store.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,"
                       "tank_guid,loot_guid,kills,wipes) VALUES (10,?,?,'ulduar',25,0,1,103,103,104,9,1)",
                       (now - 86400, now - 80000))
    store.conn.execute("INSERT INTO guild_attendance VALUES (10,104,'raided')")
    assert loot.target_raid(store.conn, now).raid_key == "icc"
    assert loot.raiders(store.conn, 1) == [101, 102]
    store.conn.execute("UPDATE guild_raid SET offline=1 WHERE raid_id=9")
    assert loot.target_raid(store.conn, now).raid_key == "ulduar"


class TrinketArmory(FakeArmory):
    """Lord Marrowgar drops a trinket of crit rating alone (a physical damage trinket whose
    attack power is a proc, as the live wishes handed to healers and casters, 2026-11-20)."""

    def items(self, ids):
        found = super().items([i for i in ids if i != 1002])
        if 1002 in ids:
            found[1002] = ItemInfo(1002, "Invented Honed Scale", 4, 264, 12, 4, 0, -1, frozenset({32}))
        return found

    def equipped(self, guids):
        return {101: {4: 200, 0: 245, 12: 200, 13: 200}}


def test_a_trinket_whose_stats_say_nothing_is_nobodys_wish(store, cast):
    raid_ahead(store)
    roles = {g: p.play.raid_role for g, p in cast.items()}
    value = loot.compute_wishes(store.conn, FakeLibrary(), TrinketArmory(), now=store.clock.now(),
                                raid_roles=roles, human=1)
    # +64 on a trinket slot would have won; it is no item of hers by its stats, so the chest is
    assert value["wishes"] == [{"guid": 101, "item_id": 1001, "boss": "Lord Marrowgar", "gain": 51}]


# -- her talents decide, and a cache other code computed is never read (2026-11-24) ---------------

KRYSS = ItemInfo(2001, "Invented Kryss", 4, 277, 13, 2, 15, -1, frozenset({7, 38}))
GREATAXE = ItemInfo(2002, "Invented Greataxe", 4, 264, 17, 2, 1, -1, frozenset({4, 7, 32}))
AGI_LEGS = ItemInfo(2003, "Invented Leggings", 4, 277, 7, 4, 2, -1, frozenset({3, 7, 38}))
INT_LEGS = ItemInfo(2004, "Invented Kilt", 4, 277, 7, 4, 2, -1, frozenset({5, 6, 7, 45}))
TANK_NECK = ItemInfo(2005, "Invented Gorget", 4, 277, 2, 4, 0, -1, frozenset({7, 12, 13}))
CASTER_NECK = ItemInfo(2006, "Invented Pendant", 4, 277, 2, 4, 0, -1, frozenset({5, 7, 45}))
SPOT_CHECK = (KRYSS, GREATAXE, AGI_LEGS, INT_LEGS, TANK_NECK, CASTER_NECK)


class SpotLibrary:
    def drops_from(self, boss):
        if boss != "Lord Marrowgar":
            return []
        return [SimpleNamespace(map_id=631, section="10-player", title=boss,
                                ref_ids=tuple(f"item:{i.item_id}" for i in SPOT_CHECK))]


class SpotArmory:
    """Arkon (a warrior) wields a two-hander; Belra and Coros are druids. Their talents: Arms,
    Restoration and Balance (`tabs`); None: an armory without the read."""

    def __init__(self, tabs=None, fail=False):
        self.tabs, self.fail = tabs, fail
        if tabs is None:
            self.spec_tabs = None

    def items(self, ids):
        return {i.item_id: i for i in SPOT_CHECK if i.item_id in ids}

    def equipped(self, guids):
        return {101: {15: 245, 1: 277, 6: 277}, 102: {6: 251, 1: 277}, 103: {1: 251, 6: 277}}

    def spec_tabs(self, guids):
        if self.fail:
            raise OSError("no dbc files")
        return self.tabs


def spot_check(store):
    raid_ahead(store)
    store.conn.execute("UPDATE guild_attendance SET status='raided' WHERE raid_id=9 AND guid=103")
    store.conn.execute("UPDATE cast_member SET class=11 WHERE guid=103")  # Coros, a druid here
    store.conn.execute("UPDATE cast_member SET spec_tab=1 WHERE guid=102")  # a stale Feral tree
    return {101: "melee", 102: "melee", 103: "tank"}  # their personas' raid roles


def wished(store, armory, roles):
    value = loot.compute_wishes(store.conn, SpotLibrary(), armory, now=store.clock.now(),
                                raid_roles=roles, human=1)
    return {w["guid"]: w["item_id"] for w in value["wishes"]}


def test_wishes_follow_her_talents_not_her_persona_or_a_stale_tree(store, cast):
    roles = spot_check(store)
    # before: Arkon a one-hand dagger, Belra agility legs, Coros a tank's neck
    assert wished(store, SpotArmory(), roles) == {101: KRYSS.item_id, 102: AGI_LEGS.item_id,
                                                  103: TANK_NECK.item_id}
    talents = SpotArmory({101: 0, 102: 2, 103: 0})
    assert wished(store, talents, roles) == {101: GREATAXE.item_id, 102: INT_LEGS.item_id,
                                             103: CASTER_NECK.item_id}


def test_a_failed_talent_read_keeps_the_snapshots_tree(store, cast):
    roles = spot_check(store)
    assert wished(store, SpotArmory({}, fail=True), roles) == wished(store, SpotArmory(), roles)
    assert loot.spec_tabs(SpotArmory({101: 0, 102: 7, "x": 1}), [101, 102]) == {101: 0}


def test_a_cache_other_code_computed_is_never_read_and_is_due(store, cast):
    event = raid_ahead(store)
    value = loot.compute_wishes(store.conn, FakeLibrary(), FakeArmory(), now=store.clock.now(),
                                raid_roles={g: p.play.raid_role for g, p in cast.items()}, human=1)
    assert value["version"] == loot.WISH_VERSION and loot.load_wishes(json.dumps(value))
    old = {k: v for k, v in value.items() if k != "version"}
    assert loot.load_wishes(json.dumps(old)) is None and loot.wish_version(json.dumps(old)) == ""
    older = {**value, "version": "0123456789abcdef"}
    assert loot.load_wishes(json.dumps(older)) is None
    assert loot.wish_due(loot.load_wishes(json.dumps(older)), event, store.clock.now())
    store.set_meta(loot.WISH_KEY, json.dumps(older))  # the seeds never offer its wishes
    assert not any(s.kind == "loot_wish" for s in gather(store, cast, culture_enabled=True,
                                                         culture_store=store, culture_loot=True))


def test_the_wish_tick_recomputes_an_outdated_cache_at_once(tmp_path, caplog):
    from selfies.fakes import mind_app
    app = mind_app(tmp_path, toml="[server]\nenabled=true\n[culture]\nenabled=true\nloot=true\n")
    try:
        live = app.server.culture
        now = app.clock.now()
        event = app.store.conn.execute(
            "INSERT INTO realm_calendar(kind,creator,at,title,updated,state) "
            "VALUES ('raid',102,?,'Raid night',?,'posted')", (now + 86400, now)).lastrowid
        app.store.set_meta(loot.WISH_KEY, json.dumps(
            {"computed": now, "event": event, "raid_key": None, "wishes": [],
             "version": "an-older-release"}))
        calls = []

        def compute(at, roles, human):
            calls.append(at)
            return {"computed": at, "event": event, "raid_key": None, "wishes": [],
                    "version": loot.WISH_VERSION}
        live._compute = compute
        app.world.human_online = False
        with caplog.at_level(logging.INFO):
            run(live.wish_tick(now + 60))  # fresh by its clock, but another release's
        assert calls == [now + 60] and f"culture_loot_wish_outdated event={event}" in caplog.text
        run(live.wish_tick(now + 120))
        assert calls == [now + 60]  # this code's now: kept for the night
    finally:
        run(app.stop())
