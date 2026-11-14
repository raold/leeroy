"""The race for firsts over a real Store: canon, the weekly rules, real benchmarks and the
training ground's question, with a small Realm surface (the real one only for attachment)."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import WriteResult
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.calendar_time import product_period
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.ledger import Ledger
from leeroy_mind.realm.ports import BenchmarkComparison, BenchmarkPull, TrainingPull
from leeroy_mind.realm.records import FactRow
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.training.benchmark import allowed_bosses, pick_guild
from leeroy_mind.training.ports import LivePorts as TrainingPorts
from leeroy_mind.training.protocol import Rival
from realm.fakes import RealmApp, enabled
from test_realm_foundation_schema import _insert, _kill, _source

HUMAN = HumanIdentity(1, "Testmaster", "Boss", "Quentin Vaastrel", True)
GUILD = 21  # His guild.
DAY = 86400.0
T0 = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).timestamp()  # The conftest clock: a Thursday.
RIVALS = ((50, "progression", "horde", "Synthetic Progression", "active"),
          (60, "drama", "horde", "Synthetic Drama", "active"),
          (70, "gankers", "alliance", "Synthetic Gankers", "active"),
          (80, "progression", "horde", "Synthetic Retired", "retired"))


class FakePorts:
    def __init__(self, store, clock, human=HUMAN):
        self.store, self.clock, self.conn = store, clock, store.conn
        self._human = human
        self.benchmarks, self.streaks, self.losses = [], {}, {}
        self.loss_since, self.remembered, self.sent = [], [], []
        self.write = None

    def now(self):
        return self.clock.now()

    def human(self):
        return self._human

    def cast(self):
        return [m for m in self.store.cast().values() if m.guid != self._human.guid and m.level >= 80]

    def benchmark_results(self, since):
        return [b for b in self.benchmarks if since <= b.completed_s <= self.now()]

    def bench_streak(self, guid):
        return self.streaks.get(guid, 0)

    def close_loot_losses(self, guid, since):
        self.loss_since.append(since)
        return self.losses.get(guid, 0)

    async def remember(self, candidate):
        self.remembered.append(candidate)
        return await self.write(candidate) if self.write is not None else WriteResult(1, "updated")

    async def send(self, cmd):
        self.sent.append(("send", cmd))

    async def say(self, *args, **kwargs):
        self.sent.append(("say", args))

    def propose(self, *args, **kwargs):
        self.sent.append(("propose", args))


class FakeRealm:
    """The Realm surface a part uses: its app's store and clock, ports, the ledger, context,
    other parts and owned spawns."""

    def __init__(self, store, clock, *, guild=GUILD, on=True):
        self.app = SimpleNamespace(store=store, clock=clock)
        self.ports = FakePorts(store, clock)
        self.ledger = Ledger(store, self.ports, FactStore(store))
        self.enabled, self.guild, self.connected = on, guild, True
        self.parts, self.spawned = {}, []

    def action_context(self):
        return ActionContext(self.enabled, self.connected, self.connected, 1, "test-process", 1,
                             self.guild if self.connected else 0, HUMAN.guid if self.connected else 0)

    def part(self, name):
        return self.parts.get(name)

    def spawn(self, fn):
        if not self.enabled:
            return None
        task = asyncio.get_running_loop().create_task(fn())
        self.spawned.append(task)
        return task


def at(clock, ts):
    clock._t = float(ts)
    return clock._t


def seed_rivals(store):
    seed_test_cast(store)
    with store.transaction():
        for gid, role, faction, name, status in RIVALS:
            store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added,status) VALUES (?,?,?,?,?,?)",
                               (gid, role, faction, name, 1.0, status))
    return store


_N = {"kill": 0}


def guild_kill(store, when, *, map_=631, boss=0, difficulty=1, private=False):
    """A stored native guild kill (public or private), as realm_kill holds it."""
    _N["kill"] += 1
    n = 1000 + _N["kill"]
    size, heroic = (25 if difficulty & 1 else 10), int(difficulty >= 2)
    with store.transaction():
        _insert(store.conn, "game_event", dict(id=n, ts=when, type="guard", payload="{}"))
        _insert(store.conn, "realm_source", _source(n, guild=GUILD, human=HUMAN.guid))
        _insert(store.conn, "realm_kill", _kill(
            kill_id=f"{'a' * 32}:{n}", source=n, native_boot="a" * 32, guild=GUILD, human=HUMAN.guid,
            map=map_, instance_id=n, boss_id=boss, difficulty=difficulty, size=size, heroic=heroic,
            killed_ms=int(when * 1000), verified_ms=int(when * 1000) + 1, published_ms=int(when * 1000) + 2,
            human_present=int(not private)))


def guild_pull(store, pull_id, end, seconds, *, map_=631, boss=0, difficulty=1, kind="his",
               members=(1, 101, 102, 103), outcome="killed"):
    """A measured training-ground pull of his (or an offline) raid, with its members. Its
    overhead gate has passed (before it records the time), so stored pulls are every pull."""
    size = 25 if difficulty & 1 else 10
    with store.transaction():
        store.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('tg_gate',?)",
                           (json.dumps({"phase": "passed", "raids": 4, "verdict_ms": 0.2}),))
        store.conn.execute(
            "INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,start_ms,end_ms,"
            "members,assisted,boss_entry,upd_mean_ms,kind,complete,scoreable,received) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,1,1,?,1,1,?)",
            (pull_id, f"{map_}:{boss}:{difficulty}", map_, 7, boss, difficulty, size, outcome,
             int((end - seconds) * 1000), int(end * 1000), len(members), kind, end))
        for guid in members:
            store.conn.execute(
                "INSERT INTO tg_member(pull,guid,class,spec_tab,role,ilvl,damage,damage_boss,healing,overheal,"
                "taken,deaths,interrupts,active_ms,detail) VALUES (?,?,1,0,'melee',245,0,0,0,0,0,0,0,0,'{}')",
                (pull_id, guid))


def canon(store, map_, boss, difficulty, day, moment="M01"):
    with store.transaction():
        store.conn.execute("INSERT INTO realm_canon_kill(map,boss_id,difficulty,moment,day) VALUES (?,?,?,?,?)",
                           (map_, boss, difficulty, moment, day))


def benchmark(ident, end, *, guild=50, kills=((0, 250.0),), wipes=(), map_=631, difficulty=1):
    """A sealed benchmark as Realm ports publish it: its killed and wiped pulls."""
    size = 25 if difficulty & 1 else 10
    pulls, t = [], end
    for boss, seconds in kills:
        pulls.append(TrainingPull(f"b{ident}:{boss}", map_, 9000 + ident, boss, difficulty, size, "benchmark",
                                  "killed", t - seconds, t, (301, 302), False, "requested_pre_ai", "unknown"))
        t -= seconds + 60
    for boss in wipes:
        pulls.append(TrainingPull(f"b{ident}:{boss}:w", map_, 9000 + ident, boss, difficulty, size, "benchmark",
                                  "wiped", t - 100, t, (301, 302), False, "requested_pre_ai", "unknown"))
        t -= 160
    pulls.reverse()
    comparisons = tuple(BenchmarkComparison(p.boss_id, p.ended_s - p.started_s if p.outcome == "killed" else None,
                                            int(p.outcome == "wiped"), None, (p.id,), ()) for p in pulls)
    bosses = tuple(sorted({p.boss_id for p in pulls}))
    return BenchmarkPull(ident, "2026-W44", f"icc{size}", guild, (301, 302), bosses, "complete",
                         sum(p.outcome == "killed" for p in pulls), sum(p.outcome == "wiped" for p in pulls),
                         end, tuple(pulls), comparisons)


def progress(store, guild=None):
    sql = "SELECT guild,map,boss_id,difficulty,state,best_pct,killed_at,kill_s,source FROM realm_progress"
    rows = store.conn.execute(sql + (" WHERE guild=?" if guild else "") + " ORDER BY guild,map,boss_id,difficulty",
                              (guild,) if guild else ()).fetchall()
    return {(r["guild"], r["map"], r["boss_id"], r["difficulty"]): dict(r) for r in rows}


def row(store, guild, map_, boss, difficulty):
    return progress(store, guild).get((guild, map_, boss, difficulty))


def facts(store, policy):
    return store.conn.execute(
        "SELECT f.id,f.kind,f.text,f.public,f.ts,o.known_json FROM realm_fact f JOIN realm_rule_origin o "
        "ON o.fact=f.id WHERE o.policy=? ORDER BY f.id", (policy,)).fetchall()


@pytest.fixture
def realm(store, clock):
    seed_rivals(store)
    return FakeRealm(store, clock)


@pytest.fixture
def race(realm):
    from leeroy_mind.realm.race import Race
    return Race(realm)


def weeks(race, clock, start, n):
    for week in range(n):
        race.weekly(at(clock, start + week * 7 * DAY))


# -- the rules never kill first ----------------------------------------------------------------

def test_progression_never_kills_first(race, store, clock):
    weeks(race, clock, T0, 20)
    lk = row(store, 50, 631, 12, 1)
    assert lk["state"] == "progressing" and lk["best_pct"] == 2 and lk["source"] == "rule"
    # The guild has killed nothing: no rival has killed anything either, by rule.
    assert store.conn.execute("SELECT count(*) FROM realm_progress WHERE state='killed'").fetchone()[0] == 0
    assert all(r["best_pct"] >= 2 for r in progress(store, 50).values())
    assert all(r["best_pct"] >= 30 for r in progress(store, 60).values())
    assert progress(store, 70) == {}  # The gankers never progress; they only take old bosses.


def test_progression_trails_by_days(race, store, clock):
    guild_kill(store, T0, boss=4)  # Festergut 25, day 0.
    for day in range(16):
        now = at(clock, T0 + day * DAY + 60)
        race.weekly(now)
        festergut = row(store, 50, 631, 4, 1)
        if festergut["state"] == "killed":
            assert festergut["killed_at"] <= now
    festergut = row(store, 50, 631, 4, 1)
    assert festergut["state"] == "killed" and festergut["source"] == "rule"
    assert T0 + 3 * DAY <= festergut["killed_at"] <= T0 + 10 * DAY
    assert row(store, 50, 631, 5, 1)["state"] == "progressing"  # Rotface: the guild has not killed it.


def test_canon_bosses_killed_after_the_canon_day(race, store, clock):
    canon(store, 631, 11, 1, "2009-05-01", "M04")  # Sindragosa 25.
    canon(store, 631, 12, 1, "2009-06-02", "M05")  # The Lich King 25, the newest.
    assert race.seed_canon() > 0
    lk, sindragosa = row(store, 50, 631, 12, 1), row(store, 50, 631, 11, 1)
    assert lk["state"] == "killed" and lk["source"] == "canon"
    killed = datetime.fromtimestamp(lk["killed_at"], timezone.utc).date().isoformat()
    assert "2009-06-05" <= killed <= "2009-06-12"
    assert sindragosa["state"] == "killed"
    assert datetime.fromtimestamp(sindragosa["killed_at"], timezone.utc).date().isoformat() >= "2009-05-04"
    # Drama: everything up to the second-newest canon boss, never the newest.
    assert row(store, 60, 631, 11, 1)["state"] == "killed"
    drama_lk = row(store, 60, 631, 12, 1)
    assert drama_lk["state"] == "progressing" and drama_lk["best_pct"] >= 30 and drama_lk["source"] == "canon"
    # Gankers: only canon bosses more than four weeks older than the newest canon day.
    assert row(store, 70, 631, 11, 1)["state"] == "killed" and row(store, 70, 631, 12, 1) is None
    assert row(store, 80, 631, 12, 1) is None  # A retired guild is not in the race.
    before = progress(store)
    assert race.seed_canon() == 0 and progress(store) == before  # Idempotent.
    assert facts(store, "race_rule_kill_v1") == []  # Canon is history, never a rule fact.


def test_only_non_canon_bosses_progress(race, store, clock):
    canon(store, 631, 12, 1, "2009-06-02")
    race.seed_canon()
    seeded = {key: value for key, value in progress(store).items() if key[1:] == (631, 12, 1)}
    weeks(race, clock, T0, 3)
    assert {key: value for key, value in progress(store).items() if key[1:] == (631, 12, 1)} == seeded
    for guild, floor in ((50, 2), (60, 30)):
        halion = row(store, guild, 724, 6, 1)
        assert halion["state"] == "progressing" and floor <= halion["best_pct"] < 100
        assert row(store, guild, 603, 13, 1)["best_pct"] < 100  # Algalon too.


def test_drama_stalls(race, store, clock):
    for day, boss in ((0, 0), (1, 1), (2, 2)):
        guild_kill(store, T0 + day * DAY, boss=boss)
    weeks(race, clock, T0 + 3 * DAY, 8)
    drama = progress(store, 60)
    assert drama[(60, 631, 0, 1)]["state"] == "killed" and drama[(60, 631, 1, 1)]["state"] == "killed"
    third = drama[(60, 631, 2, 1)]  # The guild's newest kill: never passed.
    assert third["state"] == "progressing" and third["best_pct"] >= 30
    assert all(r["best_pct"] >= 30 for r in drama.values() if r["state"] == "progressing")
    assert drama[(60, 631, 0, 1)]["killed_at"] >= T0 and drama[(60, 631, 1, 1)]["killed_at"] >= T0 + DAY
    # The progression rival takes all three, each some days after the guild.
    assert all(row(store, 50, 631, boss, 1)["state"] == "killed" for boss in (0, 1, 2))


def test_gankers_take_one_old_boss_a_week(race, store, clock):
    for boss in (0, 1, 2):
        guild_kill(store, T0 - 40 * DAY + boss, boss=boss)
    guild_kill(store, T0 - 5 * DAY, boss=3)
    killed = []
    for week in range(5):
        race.weekly(at(clock, T0 + week * 7 * DAY))
        killed.append(sorted(k[2] for k, r in progress(store, 70).items() if r["state"] == "killed"))
    # One a week, the oldest first; the newer one only once it is more than four weeks old.
    assert killed == [[0], [0, 1], [0, 1, 2], [0, 1, 2], [0, 1, 2, 3]]
    assert all(r["state"] == "killed" for r in progress(store, 70).values())


def test_hard_modes_progress_only(race, store, clock):
    from leeroy_mind.realm.race import is_hard_mode, race_keys
    guild_kill(store, T0, map_=603, boss=10, difficulty=1)  # Mimiron 25, normal: hard modes are unseen.
    canon(store, 603, 9, 3, "2009-04-01")  # A hard mode in the canon never makes one killed either.
    weeks(race, clock, T0 + DAY, 20)
    assert row(store, 50, 603, 10, 1)["state"] == "killed"
    hard = row(store, 50, 603, 10, 3)
    assert hard["state"] == "progressing" and hard["best_pct"] == 2
    assert store.conn.execute("SELECT count(*) FROM realm_progress WHERE map=603 AND difficulty IN (2,3) "
                              "AND state='killed'").fetchone()[0] == 0
    keys = race_keys()
    assert {(603, 10, 2), (603, 10, 3), (603, 0, 3), (724, 6, 3), (603, 13, 1)} <= keys
    # Yogg-Saron's no-Keepers mode is canon, Razorscale has none, and Ulduar has no heroic.
    assert not {(603, 12, 2), (603, 12, 3), (603, 2, 3)} & keys
    assert is_hard_mode(603, 3) and not is_hard_mode(603, 1) and not is_hard_mode(631, 3)


def test_rule_kill_fact_knowers(race, store, clock):
    guild_kill(store, T0, boss=0)
    race.weekly(at(clock, T0 + 11 * DAY))
    rows = facts(store, "race_rule_kill_v1")
    assert len(rows) == 1  # The progression rival's; drama never passes the newest, gankers wait.
    fact = rows[0]
    assert fact["text"] == "Synthetic Progression killed Lord Marrowgar on 25-man." and fact["public"] == 1
    assert json.loads(fact["known_json"]) == [[21, "forum"], [50, "own"], [60, "forum"], [70, "forum"]]
    marrowgar = row(store, 50, 631, 0, 1)
    assert fact["ts"] == marrowgar["killed_at"]
    assert FactStore(store).known(GUILD, human=HUMAN)[0].id == fact["id"]
    race.weekly(at(clock, T0 + 18 * DAY))
    assert len(facts(store, "race_rule_kill_v1")) == 1  # Once.


def test_weekly_once_per_period(race, store, clock):
    assert race.weekly(at(clock, T0)) is True
    first = progress(store)
    assert race.weekly(at(clock, T0 + 3600)) is False and progress(store) == first
    assert store.get_meta("realm:race:week") == product_period(T0).key
    assert race.weekly(at(clock, T0 + 7 * DAY)) is True and progress(store) != first


def test_weekly_waits_for_rival_guilds(store, clock):
    from leeroy_mind.realm.race import Race
    seed_test_cast(store)
    race = Race(FakeRealm(store, clock))
    assert race.weekly(at(clock, T0)) is False and store.get_meta("realm:race:week") is None


# -- benchmarks ------------------------------------------------------------------------------

def test_benchmark_loss_is_a_story(race, realm, store, clock):
    boasts = []
    realm.parts["speech"] = SimpleNamespace(boast=lambda guild, fact: boasts.append((guild, fact)))
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    guild_pull(store, "his:2", T0 - 9 * DAY, 320.0, boss=0, members=(104, 105))
    pull = benchmark(1, at(clock, T0), kills=((0, 250.0),), wipes=(1,))
    assert race.on_benchmark(pull) == "counted"
    match = store.conn.execute("SELECT kind,winner,detail FROM realm_match").fetchall()
    assert [(m["kind"], m["winner"]) for m in match] == [("benchmark", "50")]
    assert json.loads(match[0]["detail"])["guild_s"] == 300 and json.loads(match[0]["detail"])["rival_s"] == 250
    loss = facts(store, "benchmark_loss_v1")
    assert [f["text"] for f in loss] == ["Synthetic Progression killed Lord Marrowgar in 4:10, faster than us"]
    assert loss[0]["public"] == 1 and json.loads(loss[0]["known_json"]) == [[21, "server"], [50, "own"]]
    # A grudge on the raiders it beat (the guild's best kill), never him.
    grudges = store.conn.execute("SELECT guild,guid,points,reason FROM realm_grudge ORDER BY guid").fetchall()
    assert [tuple(g) for g in grudges] == [(50, g, 1.0, "benchmark") for g in (101, 102, 103)]
    marrowgar = row(store, 50, 631, 0, 1)
    assert (marrowgar["state"], marrowgar["source"], marrowgar["kill_s"]) == ("killed", "benchmark", 250)
    assert marrowgar["killed_at"] == T0  # The kill's own end.
    assert row(store, 50, 631, 1, 1) is None  # A wipe is no progress the race can measure.
    assert len(boasts) == 1 and boasts[0][0] == 50 and type(boasts[0][1]) is FactRow
    assert boasts[0][1].text == loss[0]["text"]
    assert [tuple(r) for r in store.conn.execute("SELECT benchmark_id,guild FROM realm_benchmark_seen")] == [(1, 50)]
    # Counted once.
    assert race.on_benchmark(pull) == "seen"
    assert len(facts(store, "benchmark_loss_v1")) == 1 and len(boasts) == 1
    assert store.conn.execute("SELECT sum(points) FROM realm_grudge").fetchone()[0] == 3.0


@pytest.mark.parametrize("gate", [{"phase": "ab", "raids": 3, "verdict_ms": None},
                                  {"phase": "failed", "raids": 4, "verdict_ms": 1.5},
                                  {"phase": "passed", "raids": 4, "verdict_ms": 0.2, "passed_at": T0 - 5 * DAY}],
                         ids=["ab", "failed", "passed_since"])
def test_no_faster_than_us_from_the_gates_sampled_pulls(race, realm, store, clock, gate):
    # His kill of 300 s was stored while the gate measured: only every other pull was, so a
    # rival's 250 s cannot be called faster than the guild's best.
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    store.set_meta("tg_gate", json.dumps(gate))
    pull = benchmark(1, at(clock, T0), kills=((0, 250.0),))
    assert race.on_benchmark(pull) == "counted"
    assert facts(store, "benchmark_loss_v1") == []
    assert store.conn.execute("SELECT count(*) FROM realm_match").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM realm_grudge").fetchone()[0] == 0
    if gate["phase"] == "passed":  # a kill since the pass is every pull: the loss stands
        guild_pull(store, "his:2", T0 - 2 * DAY, 280.0, boss=0)
        assert race.on_benchmark(benchmark(2, at(clock, T0 + 60), kills=((0, 250.0),))) == "counted"
        assert [json.loads(m[0])["guild_s"] for m in store.conn.execute("SELECT detail FROM realm_match")] == [280]


def test_a_slower_benchmark_is_progress_only(race, realm, store, clock):
    boasts = []
    realm.parts["speech"] = SimpleNamespace(boast=lambda guild, fact: boasts.append(guild))
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    assert race.on_benchmark(benchmark(2, at(clock, T0), kills=((0, 350.0),))) == "counted"
    assert row(store, 50, 631, 0, 1)["kill_s"] == 350
    assert facts(store, "benchmark_loss_v1") == [] and boasts == []
    assert store.conn.execute("SELECT count(*) FROM realm_match").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM realm_grudge").fetchone()[0] == 0


def test_benchmark_ahead_is_only_logged(race, realm, store, clock, caplog):
    boasts = []
    realm.parts["speech"] = SimpleNamespace(boast=lambda guild, fact: boasts.append(guild))
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.race")
    canon(store, 631, 5, 1, "2009-03-01")  # A canon kill is not a real one.
    assert race.on_benchmark(benchmark(3, at(clock, T0), kills=((5, 200.0),))) == "counted"
    assert "realm_benchmark_ahead benchmark=3 guild=50 map=631 boss=5 difficulty=1" in caplog.text
    assert facts(store, "benchmark_loss_v1") == [] and boasts == []
    assert row(store, 50, 631, 5, 1) is None
    assert store.conn.execute("SELECT count(*) FROM realm_match").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM realm_benchmark_seen").fetchone()[0] == 1


def test_non_realm_guild_benchmark_ignored(race, store, clock):
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    before = store.conn.total_changes
    assert race.on_benchmark(benchmark(4, at(clock, T0), guild=7)) == "ignored"   # Training's own choice.
    assert race.on_benchmark(benchmark(5, at(clock, T0), guild=80)) == "ignored"  # A retired rival.
    assert store.conn.total_changes == before


def test_benchmarks_polled_from_ports_once(race, realm, store, clock):
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    realm.ports.benchmarks = [benchmark(6, T0 - DAY, kills=((0, 250.0),)), benchmark(7, T0 - DAY, guild=7)]
    at(clock, T0)
    assert race.benchmarks(T0) == 1 and race.benchmarks(T0) == 0
    assert len(facts(store, "benchmark_loss_v1")) == 1


# -- the training ground's question ------------------------------------------------------------

def test_benchmark_allowed_needs_a_guild_kill(race, store, clock):
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=12, difficulty=1)  # The Lich King 25 normal only.
    canon(store, 631, 11, 1, "2009-05-01")               # Sindragosa: canon only.
    guild_kill(store, T0 - DAY, boss=10, difficulty=1, private=True)  # A private kill is a real one.
    guild_pull(store, "his:1", T0 - DAY, 400.0, boss=8, difficulty=3)  # Lana'thel 25 heroic, measured.
    guild_pull(store, "bench:1", T0 - DAY, 400.0, boss=7, kind="benchmark")  # A rival's, not the guild's.
    guild_kill(store, T0 + DAY, boss=6)                   # Not yet.
    assert race.benchmark_allowed(631, 12, 25, False) is True
    assert race.benchmark_allowed(631, 12, 25, True) is None
    assert race.benchmark_allowed(631, 12, 10, False) is None
    assert race.benchmark_allowed(631, 11, 25, False) is None
    assert race.benchmark_allowed(631, 10, 25, False) is True
    assert race.benchmark_allowed(631, 8, 25, True) is True
    assert race.benchmark_allowed(631, 7, 25, False) is None
    assert race.benchmark_allowed(631, 6, 25, False) is None


@pytest.mark.parametrize("args", [
    (631, 12, 25, 0), (631, 12, 25, "yes"), (631, 12, True, False), (631, 12, 20, False), ("631", 12, 25, False),
    (631, None, 25, False), (631, 13, 25, False), (631, 2 ** 70, 25, False), (-1, 12, 25, False),
    (999, 0, 25, False), (624, 0, 25, False),
])
def test_benchmark_allowed_true_or_none(race, store, clock, args):
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=12)
    assert race.benchmark_allowed(*args) is None


def test_benchmark_allowed_never_false_and_never_raises(race, realm, store, clock):
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=12)
    assert race.benchmark_allowed(631, 12, 25, False) is True
    realm.enabled = False  # While the Realm is off Training keeps its own history.
    assert race.benchmark_allowed(631, 12, 25, False) is None
    realm.enabled = True

    def broken():
        raise RuntimeError("synthetic")
    realm.app.clock = SimpleNamespace(now=broken)
    assert race.benchmark_allowed(631, 12, 25, False) is None
    realm.app.clock = clock
    realm.app.store = SimpleNamespace(conn=SimpleNamespace(execute=lambda *a: broken()))  # A failing database.
    assert race.benchmark_allowed(631, 12, 25, False) is None
    realm.app.store = store
    assert race.benchmark_allowed(631, 12, 25, False) is True


def _training_ports(store, clock, realm):
    app = SimpleNamespace(clock=clock, store=store, guild=SimpleNamespace(ports=SimpleNamespace()), realm=realm)
    return TrainingPorts(app, cfg=SimpleNamespace())


def test_training_reaches_the_race_and_keeps_its_fallback(race, realm, store, clock):
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=0)
    guild_kill(store, T0 - DAY, boss=1)
    with_race = _training_ports(store, clock, SimpleNamespace(race=race))
    without = _training_ports(store, clock, None)
    assert with_race.benchmark_allowed(631, 0, 25, False) is True
    assert without.benchmark_allowed(631, 0, 25, False) is None
    assert allowed_bosses(with_race, "icc25") == (0, 1)
    assert allowed_bosses(without, "icc25") == ()  # Training's own history has none of them.
    guild_pull(store, "his:2", T0 - DAY, 300.0, boss=2)
    assert allowed_bosses(with_race, "icc25") == (0, 1, 2)
    realm.enabled = False  # The race answers None: exactly Training's own history.
    assert allowed_bosses(with_race, "icc25") == allowed_bosses(without, "icc25") == ()


def test_training_rival_choice_unchanged(race, store):
    """The race credits whichever Realm guild ran a benchmark; Training still picks it (R1)."""
    def rival(guid, guild):
        return Rival(guid, f"R{guid}", 1, 0, guild, 220, 0, 0)
    rivals = [rival(300 + n, 7) for n in range(4)] + [rival(400 + n, 50) for n in range(2)]
    assert pick_guild(rivals, cast_guild=GUILD)[0] == 7  # Not the Realm's progression guild.
    training_facing = {name for name in dir(race) if not name.startswith("_") and callable(getattr(race, name))}
    assert "benchmark_allowed" in training_facing
    assert not {"pick_guild", "benchmark_guild", "rival_candidates"} & training_facing


# -- the switch, attachment and the tick -------------------------------------------------------

def test_race_never_acts_while_disabled(race, realm, store, clock):
    guild_kill(store, T0 - 20 * DAY, boss=0)
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    realm.ports.benchmarks = [benchmark(8, T0 - DAY)]
    canon(store, 631, 12, 1, "2009-06-02")
    realm.enabled = False
    before = store.conn.total_changes
    at(clock, T0)
    assert race.weekly(T0) is False and race.on_benchmark(benchmark(8, T0 - DAY)) == "deferred"
    assert race.seed_canon() == 0
    run(race.tick())
    assert store.conn.total_changes == before
    realm.enabled, realm.connected = True, False  # No valid hello: his guild is unknown.
    assert race.weekly(T0) is False and race.on_benchmark(benchmark(8, T0 - DAY)) == "deferred"
    assert store.conn.total_changes == before


def test_tick_runs_weekly_and_benchmarks_and_never_raises(race, realm, store, clock, caplog):
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    realm.ports.benchmarks = [benchmark(9, T0 - DAY)]
    at(clock, T0)
    run(race.tick())
    assert store.get_meta("realm:race:week") == product_period(T0).key
    assert len(facts(store, "benchmark_loss_v1")) == 1

    def broken():
        raise RuntimeError("synthetic")
    realm.ports.benchmark_results = lambda since: broken()
    at(clock, T0 + 7 * DAY)
    run(race.tick())  # Logged by type, never the Realm loop's failure.
    assert "realm_race_failed" in caplog.text


def test_attach_makes_no_io(store, clock, tmp_path):
    from leeroy_mind.realm import race as race_module
    seed_rivals(store)
    fake = FakeRealm(store, clock)
    before = store.conn.total_changes
    part, events = race_module.attach(fake)
    assert type(part) is race_module.Race and events == () and store.conn.total_changes == before
    app = RealmApp(tmp_path, store, clock)
    enabled(tmp_path, True)
    realm = attach_realm(app)
    assert type(realm.part("race")) is race_module.Race and realm.part("race").realm is realm
    assert store.conn.total_changes == before
    run(realm.close())
