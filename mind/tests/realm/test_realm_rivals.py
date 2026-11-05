"""Task 14 (W3-14): the rival guilds' plan, their officers' cards, the "rivals" step and the weekly
top-up. The step and top-up tests run over the real service, Store, direct ledger, action queue and
body ledger with the small RealmApp surface; the armory and the model are always fakes, and nothing
reads worldserver.conf or reaches MySQL."""
import asyncio
import json
import logging
import random
import threading

import pytest

from conftest import make_persona, mem, run, seed_test_cast
from leeroy_mind.bodies import BodyHold
from leeroy_mind.config import load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.library import mysql
from leeroy_mind.llm.presets import EXTRACT
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.roster import DbInfo
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import cli, protocol, rivals
from leeroy_mind.realm import steps as steps_module
from leeroy_mind.realm.armory import BotGuild, Candidate, RealmArmory
from leeroy_mind.realm.screen import Screen
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.realm.steps import StepError, approve, digest, write_plan
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import class_roles
from realm.fakes import RealmApp, enabled


@pytest.fixture(autouse=True)
def step_owner(monkeypatch):
    """Wave 4 wiring: F4's Progress names its direct-ledger owner "steps", but F1's ledger (and
    0016's CHECK) accept only "step", so every Progress.send raises against the real ledger.
    These tests run the real ledger with the one-word fix the controller applies at merge."""
    monkeypatch.setattr(steps_module, "OWNER", "step")


# -- the fake armory ----------------------------------------------------------------------------

HORDE_RACE = {1: 2, 2: 10, 3: 2, 4: 2, 5: 10, 6: 2, 7: 2, 8: 10, 9: 2, 11: 6}
ALLIANCE_RACE = {1: 1, 2: 1, 3: 3, 4: 1, 5: 1, 6: 1, 7: 11, 8: 1, 9: 1, 11: 4}
CYCLE = (1, 2, 3, 4, 5, 6, 7, 8, 9, 11)
ROSTER = frozenset({101, 102, 103, 104, 105, 150})  # guild 21; 150 is below 80, still a member
HUMAN = 1


def seed_roster(store):
    """Guild 21 as ROSTER says: the cast 101-105 at 80 and 150 below it."""
    seed_test_cast(store)
    store.upsert_cast([mem(150, online=False, level=70)], 1000.0)


def member(guid, rank, level=80, cls=1, faction="horde"):
    race = (HORDE_RACE if faction == "horde" else ALLIANCE_RACE)[cls]
    return rivals.GuildMember(guid, f"Bot{guid}", race, cls, level, rank)


def pool(faction, start, n):
    """n guildless level-80 bots, classes cycling; every ninth is under item level 200."""
    race = HORDE_RACE if faction == "horde" else ALLIANCE_RACE
    out = []
    for i in range(n):
        cls = CYCLE[i % len(CYCLE)]
        ilvl = 190.0 if i % 9 == 8 else 200.0 + (i % 25) + 0.5
        out.append(Candidate(start + i, f"Cand{start + i}", race[cls], cls, faction, ilvl))
    return out


class FakeArmory:
    def __init__(self, *, guilds=None, members=None, pools=None, gate=None):
        self.guilds = guilds if guilds is not None else [
            BotGuild(7, "Synthetic Seven", 700, "horde", {(80, "horde"): 6, (70, "horde"): 3}),
            BotGuild(8, "Synthetic Eight", 800, "horde", {(80, "horde"): 5}),
            BotGuild(9, "Synthetic Nine", 900, "horde", {(80, "horde"): 2, (60, "horde"): 30}),
            BotGuild(11, "Synthetic Eleven", 1100, "alliance", {(80, "alliance"): 5}),
            BotGuild(12, "Synthetic Twelve", 1200, "alliance", {(80, "alliance"): 1, (70, "alliance"): 20}),
        ]
        self.members = members if members is not None else {
            7: [member(700, 0), member(701, 1, cls=5), member(702, 1, cls=6), member(704, 1, level=70),
                member(703, 2, cls=7), member(705, 3, cls=8)],
            8: [member(800, 0), member(103, 1), member(801, 1, cls=2), member(802, 2, cls=3),
                member(803, 2, cls=4), member(804, 3, cls=9)],
            9: [member(900, 0, level=60), member(901, 1), member(902, 2)],
            11: [member(1100, 0, faction="alliance"), member(1101, 1, cls=5, faction="alliance"),
                 member(1102, 1, cls=7, faction="alliance"), member(1103, 2, cls=8, faction="alliance"),
                 member(1104, 4, cls=4, faction="alliance")],
        }
        self.pools = pools if pools is not None else {
            "horde": [Candidate(101, "Arkon", 2, 1, "horde", 260.0),     # a cast member
                      Candidate(150, "Lowbie", 2, 1, "horde", 260.0),    # guild 21, below 80
                      Candidate(HUMAN, "Hero", 2, 1, "horde", 260.0),    # him
                      *pool("horde", 5000, 70)],
            "alliance": pool("alliance", 6000, 45)}
        self.gate = gate
        self.threads = []

    def _seen(self):
        self.threads.append(threading.get_ident())
        if self.gate is not None:
            assert self.gate.wait(5)

    def bot_guilds(self):
        self._seen()
        return list(self.guilds)

    def guildless_80s(self, faction):
        self._seen()
        return list(self.pools[faction])

    def guild_members(self, guild_id):
        self._seen()
        return list(self.members.get(guild_id, ()))

    def guild_name(self, guild_id):
        return "Synthetic Guild" if guild_id == 21 else ""


def plan_of(armory=None, *, former=frozenset(), seed=3):
    return rivals.plan_rivals(armory or FakeArmory(), ROSTER, former, progression_name="Rival Name",
                              rng=random.Random(seed), human_guid=HUMAN)


def by_role(plan):
    return {g["role"]: g for g in plan["guilds"]}


def planned_guids(plan):
    return {x["guid"] for g in plan["guilds"] for x in (*g["actors"], *g["raiders"])}


def card(name, **over):
    return {"name": name, "role": "raid officer", "temperament": "dry and patient",
            "habits": ["short sentences", "counts every wipe"], "want": "a server first",
            "view_of_guild": "lucky more than good", **over}


def with_cards(plan):
    plan["cards"] = {str(a["guid"]): card(a["name"]) for g in plan["guilds"] for a in g["actors"]}
    return plan


# -- the plan -----------------------------------------------------------------------------------

def test_plan_picks_by_faction_and_size():
    plan = plan_of()
    roles = by_role(plan)
    assert [g["role"] for g in plan["guilds"]] == ["progression", "drama", "gankers"]
    # Horde by level-80 members (not by size: guild 9 is the biggest), Alliance the same.
    assert [(roles[r]["guild_id"], roles[r]["faction"]) for r in ("progression", "drama", "gankers")] == [
        (7, "horde"), (8, "horde"), (11, "alliance")]
    assert (roles["progression"]["name"], roles["progression"]["old_name"]) == ("Rival Name", "Synthetic Seven")
    assert roles["drama"]["name"] == roles["drama"]["old_name"] == "Synthetic Eight"
    assert roles["gankers"]["name"] == roles["gankers"]["old_name"] == "Synthetic Eleven"
    # Never guild 21, even when it is the biggest guild a bot leads.
    ours = FakeArmory()
    ours.guilds.append(BotGuild(21, "Synthetic Guild", 101, "horde", {(80, "horde"): 90}))
    assert by_role(plan_of(ours))["progression"]["guild_id"] == 7
    # Too few bot guilds of a faction: no plan.
    few = FakeArmory(guilds=[g for g in FakeArmory().guilds if g.guild_id in (7, 11, 12)])
    with pytest.raises(StepError, match="not enough"):
        plan_of(few)
    # The name is checked, and never echoed.
    for bad in ("", "x" * 25, "Rival  Name", "Rival-Name!", " Rival", "Synthetic Eight"):
        with pytest.raises(StepError) as caught:
            rivals.plan_rivals(FakeArmory(), ROSTER, frozenset(), progression_name=bad,
                               rng=random.Random(1), human_guid=HUMAN)
        assert bad.strip() == "" or bad not in str(caught.value)


def test_plan_never_takes_cast_or_guild_21():
    plan = plan_of()
    assert not planned_guids(plan) & (ROSTER | {HUMAN})
    # 103 is a cast member listed in guild 8 at rank 1: never one of its officers.
    assert 103 not in {a["guid"] for a in by_role(plan)["drama"]["actors"]}
    # And everyone on the plan is distinct.
    guids = [x["guid"] for g in plan["guilds"] for x in (*g["actors"], *g["raiders"])]
    assert len(guids) == len(set(guids))


def test_plan_builds_a_raid_comp():
    for seed in range(6):
        plan = plan_of(seed=seed)
        for guild in plan["guilds"]:
            raiders = guild["raiders"]
            assert 25 <= guild["target"] <= 30
            assert guild["level80"] + len(raiders) == guild["target"] and len(raiders) <= 30
            assert all(r["avg_ilvl"] >= 200 for r in raiders)  # the 190s are never taken
            assert sum("tank" in class_roles(r["class"]) for r in raiders) >= 2
            assert sum("healer" in class_roles(r["class"]) for r in raiders) >= 6
            assert sum(r["role"] == "tank" for r in raiders) == 2
            assert sum(r["role"] == "healer" for r in raiders) == 6
            assert all(r["role"] in {"tank", "healer", "melee", "ranged"} for r in raiders)
            assert all(r["role"] != "tank" or "tank" in class_roles(r["class"]) for r in raiders)
            assert all(r["role"] != "healer" or "healer" in class_roles(r["class"]) for r in raiders)
            assert {r["guid"] for r in raiders} <= {c.guid for c in FakeArmory().pools[guild["faction"]]}


def test_plan_names_ganker_actors():
    for seed in range(8):
        roles = by_role(plan_of(seed=seed))
        prog, drama, gank = (roles[r]["actors"] for r in ("progression", "drama", "gankers"))
        # Officers: the leader and 2-3 more of the highest rank at 80, never a level-70 member.
        assert 3 <= len(prog) <= 4 and prog[0]["guid"] == 700 and 704 not in {a["guid"] for a in prog}
        assert [a["guid"] for a in prog] == [700, 701, 702, 703][:len(prog)]
        assert 3 <= len(drama) <= 4 and drama[0]["guid"] == 800
        assert {a["role"] for a in prog + drama} == {"officer"}
        # Gankers: 2-3 level-80 members, the leader first.
        assert 2 <= len(gank) <= 3 and gank[0]["guid"] == 1100 and {a["role"] for a in gank} == {"ganker"}
        assert len(prog) + len(drama) + len(gank) <= 12
    # A leader below 80 is passed over for the next of the highest rank.
    nine = FakeArmory()
    nine.guilds = [g for g in nine.guilds if g.guild_id != 7]  # Guild 9 becomes the drama farm.
    drama = by_role(plan_of(nine))["drama"]
    assert drama["guild_id"] == 9 and [a["guid"] for a in drama["actors"]] == [901, 902]


def test_plan_never_takes_a_former_member(tmp_path, store, clock, capsys, monkeypatch):
    """A guildless 80 with a stored persona (a member he kicked) and a realm_kick guid are never
    planned, as raider or officer, now or in the weekly top-up."""
    seed_roster(store)
    save_persona(store, make_persona(5000, "Gone"), source="seed", ts=1.0, race="Orc", class_name="Warrior")
    with store.transaction():
        for guid in (5001, 801):
            store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (?,1.0,3)", (guid,))
    armory = FakeArmory()
    rest = [c for c in armory.pools["horde"] if c.guid not in (5000, 5001)]
    armory.pools["horde"] = [Candidate(5000, "Gone", 2, 1, "horde", 280.0),
                             Candidate(5001, "Kicked", 2, 6, "horde", 280.0), *rest]
    ready_cli(tmp_path, store, monkeypatch, armory, FakeLLM(lambda m, msgs: json.dumps(card("x"))))
    assert cli.realm_main(["plan", "rivals", "--progression-name", "Rival Name",
                           "--data-dir", str(tmp_path)], clock=clock) == 0
    capsys.readouterr()
    [path] = (tmp_path / "realm" / "plans").glob("rivals-*.json")
    plan = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert not planned_guids(plan) & {5000, 5001, 801}
    assert 801 in {m.guid for m in armory.members[8]}  # An officer of rank 1 at 80, passed over.
    # The top-up reads the same exclusions when it fills a guild that fell below 25.
    app, realm = live_realm(tmp_path, store, clock)
    rows_for_topup(store, clock, plan)
    unplanned = {"horde": [c for c in armory.pools["horde"] if c.guid not in planned_guids(plan)],
                 "alliance": armory.pools["alliance"]}
    realm.ports.armory = lambda: FakeArmory(guilds=topup_guilds(prog=23), pools=unplanned)

    async def go():
        await connect(app)
        await realm.part("rivals").tick()
        await settle(realm)
        realm.queue.tick()
        await settle(realm)
    run(go())
    [grow] = [c for c in app.sent if type(c) is protocol.GuildGrow]
    assert grow.guild_id == 7 and len(grow.guids) == 2 and not set(grow.guids) & {5000, 5001, 801}


# -- the cards ----------------------------------------------------------------------------------

def screen_at(root, guild="Synthetic Guild"):
    (root / "realm").mkdir(parents=True, exist_ok=True)
    (root / "realm" / "blocklist.txt").write_text("forbiddenword\n", encoding="utf-8")
    return Screen(root, lambda: guild, lambda: HumanIdentity(HUMAN, "Hero", "Boss", "", True))


def test_cards_are_screened_and_short(tmp_path):
    plan = {"guilds": [{"role": "progression", "guild_id": 7, "faction": "horde", "name": "Rival Name",
                        "actors": [{"guid": 700, "name": "Aaa", "class": 1, "rank": 0, "role": "officer"},
                                   {"guid": 701, "name": "Bbb", "class": 5, "rank": 1, "role": "officer"},
                                   {"guid": 702, "name": "Ccc", "class": 6, "rank": 1, "role": "officer"}]}]}
    tries = {}

    def script(model, messages):
        who = next(n for n in ("Aaa", "Bbb", "Ccc") if n in messages[-1]["content"])
        tries[who] = tries.get(who, 0) + 1
        if who == "Aaa":  # Names his guild both times: rewritten once, then dropped.
            return json.dumps(card("Aaa", view_of_guild="Synthetic Guild got lucky"))
        if who == "Bbb" and tries[who] == 1:  # Too long the first time, short the second.
            return json.dumps(card("Bbb", want="to be remembered " * 40))
        return json.dumps(card("Someone Else"))  # The card keeps the character's own name.
    llm = FakeLLM(script)
    cards = run(rivals.write_cards(plan, llm, model="live-model", screen=screen_at(tmp_path)))
    assert tries == {"Aaa": 2, "Bbb": 2, "Ccc": 1} and len(llm.calls) == 5
    assert sorted(cards) == ["701", "702"]
    assert [cards[k]["name"] for k in ("701", "702")] == ["Bbb", "Ccc"]
    for kept in cards.values():
        assert set(kept) == {"name", "role", "temperament", "habits", "want", "view_of_guild"}
        assert 2 <= len(kept["habits"]) <= 3
        text = " ".join([kept["name"], kept["role"], kept["temperament"], *kept["habits"],
                         kept["want"], kept["view_of_guild"]])
        assert estimate_tokens(text) <= rivals.CARD_TOKENS == 120
    for call in llm.calls:  # P1b's extract options on the live model, never thinking.
        assert call["model"] == "live-model" and call["think"] is False
        assert call["options"] == json.loads(json.dumps(dict(EXTRACT.options)))
        assert call["format"] == rivals.CARD_SCHEMA
        assert "Synthetic Guild" not in json.dumps(call["messages"])  # His guild is never named.
    # A screen that is not ready drops every card without asking the model.
    llm = FakeLLM(script)
    blind = Screen(tmp_path / "nowhere", lambda: "Synthetic Guild", lambda: None)
    assert run(rivals.write_cards(plan, llm, model="live-model", screen=blind)) == {}
    assert llm.calls == []


# -- the command line ---------------------------------------------------------------------------

def ready_cli(root, store, monkeypatch, armory, llm, *, guild_name="Synthetic Guild", night="done"):
    """The plan's inputs; tonight's night run is done (or, night="running", still holds the GPU)."""
    from leeroy_mind.clock import night_of
    clock = store.clock
    if guild_name:
        store.set_meta("realm:guild_name", json.dumps({"guild": 21, "name": guild_name, "at": 1.0}))
    with store.conn:
        store.conn.execute("INSERT INTO night_run(night,window_start,window_end,status,started,finished) "
                           "VALUES (?,0,?,?,?,?)", (night_of(clock.local()), clock.now(), night, clock.now(),
                                                    clock.now() if night == "done" else None))
    screen_at(root)
    opened = []
    monkeypatch.setattr(rivals, "_open_armory", lambda root: opened.append("armory") or armory)
    monkeypatch.setattr(rivals, "_open_llm", lambda cfg: opened.append("llm") or llm)
    return opened


def test_plan_cli_writes_its_plan_and_nothing_else(tmp_path, store, clock, capsys, monkeypatch):
    seed_roster(store)
    llm = FakeLLM(lambda m, msgs: json.dumps(card("x")))
    ready_cli(tmp_path, store, monkeypatch, FakeArmory(), llm)
    db = tmp_path / "mind.db"
    before = db.stat().st_mtime_ns, db.stat().st_size
    argv = ["plan", "rivals", "--progression-name", "Rival Name", "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 0
    output = capsys.readouterr().out
    summary = json.loads(output)
    [path] = (tmp_path / "realm" / "plans").glob("rivals-*.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert summary["plan"] == path.name and digest(doc) == doc["sha256"] and doc["step"] == "rivals"
    plan = doc["payload"]
    actors = [a for g in plan["guilds"] for a in g["actors"]]
    assert sorted(plan["cards"]) == sorted(str(a["guid"]) for a in actors) and summary["cards"] == len(actors)
    assert [(g["role"], g["name"], g["adding"]) for g in summary["guilds"]] == [
        (g["role"], g["name"], len(g["raiders"])) for g in plan["guilds"]]
    assert summary["guilds"][0]["actors"] == [a["name"] for a in plan["guilds"][0]["actors"]]
    assert str(tmp_path) not in output
    live = load_config(tmp_path / "mind.toml").models.live
    assert len(llm.calls) == len(actors) and {c["model"] for c in llm.calls} == {live}
    assert {c["think"] for c in llm.calls} == {False}
    # Read-only: mind.db unchanged, no approval, no step row.
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
    assert not (tmp_path / "realm" / "approved").exists()
    assert store.conn.execute("SELECT count(*) FROM realm_step").fetchone()[0] == 0
    # Without a name, with rivals already chosen, or with no screen: refused, no plan, no model.
    for args in ([], ["--progression-name"], ["--other", "x"]):
        assert cli.realm_main(["plan", "rivals", *args, "--data-dir", str(tmp_path)], clock=clock) == 1
    (tmp_path / "realm" / "blocklist.txt").unlink()
    calls = len(llm.calls)
    assert cli.realm_main(argv, clock=clock) == 1
    assert "screen" in capsys.readouterr().out and len(llm.calls) == calls
    screen_at(tmp_path)
    with store.transaction():
        store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (7,'progression','horde','X',1)")
    assert cli.realm_main(argv, clock=clock) == 1
    assert "already" in capsys.readouterr().out and len(llm.calls) == calls
    assert len(list((tmp_path / "realm" / "plans").glob("rivals-*.json"))) == 1


def test_plan_refused_in_the_night_window(tmp_path, store, clock, capsys, monkeypatch):
    opened = ready_cli(tmp_path, store, monkeypatch, FakeArmory(), FakeLLM(), night="running")
    argv = ["plan", "rivals", "--progression-name", "Rival Name", "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 2
    assert "night_window" in capsys.readouterr().out
    assert opened == [] and not (tmp_path / "realm" / "plans").exists()


# -- the step -----------------------------------------------------------------------------------

async def connect(app, guild=21):
    await app.connection(True)
    await app.event(Hello(0, "test", "1", guild, app.human_guid))


async def settle(realm):
    for _ in range(50):
        children = tuple(realm._children) + tuple(realm.queue._tasks)
        if not children:
            return
        await asyncio.gather(*children, return_exceptions=True)


async def pump(realm, clock, rounds=3):
    """Let the action queue dispatch what is enrolled, two body commands a second."""
    for _ in range(rounds):
        realm.queue.tick()
        await settle(realm)
        await clock.advance(1, step=1)


def grows(app, only=None):
    return [c for c in app.sent if type(c) is protocol.GuildGrow and (only is None or c.guild_id in only)]


async def finish(app, clock, cmds):
    """Native's job for each of these guild_grow commands, delivered to this app's mind."""
    for cmd in cmds:
        await app.event(protocol.RealmJob(int(clock.now() * 1000), cmd.id, "guild_grow", "done",
                                          "", 0, len(cmd.guids)))


def live_realm(root, store, clock, *, on=True):
    enabled(root, on)
    app = RealmApp(root, store, clock)
    return app, attach_realm(app)


def approved(root, clock, payload):
    path = write_plan(root, "rivals", payload, local=clock.local())
    approve(root, path, now=clock.now())
    return path, json.loads(path.read_text(encoding="utf-8"))["sha256"]


def kinds(app):
    return [type(c).__name__ for c in app.sent]


def step_row(store):
    row = store.conn.execute("SELECT applied,result FROM realm_step WHERE name='rivals'").fetchone()
    return None if row is None else (row[0], json.loads(row[1]))


def counts(store):
    return tuple(store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("realm_guild", "realm_actor", "realm_raider"))


def test_plan_applies_once(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    approved(tmp_path, clock, plan)
    steps = realm.part("steps")

    async def go():
        await connect(app)
        await steps.tick()             # rows, then the rename and the actor set; grows enrolled
        assert kinds(app) == ["GuildRename", "RealmActors"]
        await pump(realm, clock)       # the action queue sends the three grows
        await finish(app, clock, grows(app))
        await clock.advance(60, step=60)
        await steps.tick()             # everything settled: the step is applied
        sent = list(app.sent)
        for _ in range(2):             # a second tick, and the queue, send nothing
            await clock.advance(60, step=60)
            await steps.tick()
            await pump(realm, clock, rounds=1)
        assert app.sent == sent
    run(go())
    roles = by_role(plan)
    assert kinds(app) == ["GuildRename", "RealmActors", "GuildGrow", "GuildGrow", "GuildGrow"]
    rename, actors, *grown = app.sent
    assert (rename.guild_id, rename.name) == (7, "Rival Name")
    every = [a["guid"] for g in plan["guilds"] for a in g["actors"]]
    assert sorted(actors.guids) == sorted(every)
    assert sorted(actors.gankers) == sorted(a["guid"] for a in roles["gankers"]["actors"])
    assert sorted(actors.guilds) == [7, 8, 11]
    assert [(g.guild_id, g.guids) for g in grown] == [
        (g["guild_id"], tuple(r["guid"] for r in g["raiders"])) for g in plan["guilds"]]
    applied, result = step_row(store)
    assert applied is not None
    assert result["rename"] == "acked" and result["actors"] == "acked"
    assert result["grow"] == {"7": "acked", "8": "acked", "11": "acked"}
    assert counts(store) == (3, len(every), sum(len(g["raiders"]) for g in plan["guilds"]))
    assert store.conn.execute("SELECT name FROM realm_guild WHERE id=7").fetchone()[0] == "Rival Name"
    cards = tmp_path / "realm" / "cards"
    assert sorted(p.stem for p in cards.glob("*.json") if not p.stem.startswith("guild-")) == sorted(plan["cards"])
    assert sorted(p.stem for p in cards.glob("guild-*.json")) == ["guild-11", "guild-7", "guild-8"]
    assert json.loads((cards / f"{every[0]}.json").read_text(encoding="utf-8")) == plan["cards"][str(every[0])]


def test_actor_rows_before_realm_actors(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    approved(tmp_path, clock, plan)
    seen = []
    send = app.send

    async def spy(cmd, *, preserve_id=False):
        actors = {row[0] for row in store.conn.execute("SELECT guid FROM realm_actor WHERE status='active'")}
        seen.append((type(cmd).__name__, counts(store), actors))
        return await send(cmd, preserve_id=preserve_id)
    app.send = spy

    async def go():
        await connect(app)
        await realm.part("steps").tick()
        await pump(realm, clock)
    run(go())
    every = {a["guid"] for g in plan["guilds"] for a in g["actors"]}
    raiders = sum(len(g["raiders"]) for g in plan["guilds"])
    assert [name for name, _, _ in seen] == ["GuildRename", "RealmActors", "GuildGrow", "GuildGrow", "GuildGrow"]
    for name, rows, actors in seen:  # Every row is written before the first command goes out.
        assert rows == (3, len(every), raiders) and actors == every
    sent_actors = next(c for c in app.sent if type(c) is protocol.RealmActors)
    assert set(sent_actors.guids) == every


def refusing(app, kind, error):
    """Native refuses every command of this type with `error`."""
    send = app.send

    async def refuse(cmd, *, preserve_id=False):
        if type(cmd) is kind:
            app.sent.append(cmd)
            return Ack(0, cmd.id, False, error)
        return await send(cmd, preserve_id=preserve_id)
    app.send = refuse


def test_refused_rename_keeps_the_old_name_and_the_rest_goes(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    refusing(app, protocol.GuildRename, "name_taken")
    approved(tmp_path, clock, with_cards(plan_of()))

    async def go():
        await connect(app)
        await realm.part("steps").tick()
        await pump(realm, clock)
        await finish(app, clock, grows(app))
        await clock.advance(60, step=60)
        await realm.part("steps").tick()
    run(go())
    assert kinds(app) == ["GuildRename", "RealmActors", "GuildGrow", "GuildGrow", "GuildGrow"]
    assert store.conn.execute("SELECT name FROM realm_guild WHERE id=7").fetchone()[0] == "Synthetic Seven"
    _, result = step_row(store)
    assert result["rename"] == "refused" and result["actors"] == "acked"


def test_refused_actor_set_sends_no_grow(tmp_path, store, clock):
    """Native refuses a grow into a guild its realm guild set does not name, and the action
    journal would keep such a refusal's bodies held: without the actor set no grow goes."""
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    refusing(app, protocol.RealmActors, "pin_rejected")
    approved(tmp_path, clock, with_cards(plan_of()))

    async def go():
        await connect(app)
        await realm.part("steps").tick()
        await pump(realm, clock)
    run(go())
    assert kinds(app) == ["GuildRename", "RealmActors"]
    applied, result = step_row(store)
    assert applied is not None and result["actors"] == "refused"
    assert result["grow"] == {"7": "skipped", "8": "skipped", "11": "skipped"}
    assert store.conn.execute("SELECT count(*) FROM realm_action").fetchone()[0] == 0


def test_apply_restart_no_duplicate_rename_and_grow_resumes(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    _, sha = approved(tmp_path, clock, plan)

    async def before_restart():
        await connect(app)
        await realm.part("steps").tick()
        realm.queue.tick()              # two of the three grows go out (two a second) ...
        await settle(realm)
        await finish(app, clock, grows(app, {7}))  # ... and only guild 7's job comes back
    run(before_restart())
    assert kinds(app) == ["GuildRename", "RealmActors", "GuildGrow", "GuildGrow"]
    assert step_row(store)[0] is None
    # A new mind on the same mind.db: guild 11's grow never reached the wire, guild 8's did.
    app2, realm2 = live_realm(tmp_path, store, clock)

    async def after_restart():
        await connect(app2)
        await realm2.part("steps").tick()
        await pump(realm2, clock)
        assert kinds(app2) == ["GuildGrow"]   # Only guild 11's grow, never a rename or actor set again.
        await finish(app2, clock, grows(app, {8}) + grows(app2))
        await clock.advance(60, step=60)
        await realm2.part("steps").tick()
    run(after_restart())
    [resumed] = app2.sent
    assert resumed.guild_id == 11 and not grows(app, {11})
    assert resumed.guids == tuple(r["guid"] for r in by_role(plan)["gankers"]["raiders"])
    assert resumed.id == rivals.grow_id(sha, 11, 1)  # A new attempt: attempt 0 never left
    applied, result = step_row(store)
    assert applied is not None and result["grow"] == {"7": "acked", "8": "acked", "11": "acked"}
    refs = store.conn.execute("SELECT ref,state FROM realm_direct WHERE owner='step' ORDER BY ref").fetchall()
    assert [tuple(r) for r in refs] == [(f"step:rivals:{sha}:0", "acked"), (f"step:rivals:{sha}:1", "acked")]


def test_grow_waits_for_the_body_ledger(tmp_path, store, clock):
    """Ruling 1: never gated on raid_holds_all; a held body waits for the body ledger, which
    decides, and the other guilds grow meanwhile."""
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    _, sha = approved(tmp_path, clock, plan)
    held = {by_role(plan)["progression"]["raiders"][0]["guid"]}
    realm.bodies.add_holder_source("test_raid", lambda: (
        (BodyHold("test:raid", "raid", 0, frozenset(held), False),) if held else ()))
    steps = realm.part("steps")

    async def go():
        await connect(app)
        await steps.tick()
        await pump(realm, clock)
        assert [c.guild_id for c in grows(app)] == [8, 11]
        await finish(app, clock, grows(app))
        await clock.advance(60, step=60)
        await steps.tick()
        assert step_row(store)[0] is None   # guild 7 still waits; nothing was burned
        held.clear()
        await clock.advance(60, step=60)
        await steps.tick()
        await pump(realm, clock)
        await finish(app, clock, grows(app, {7}))
        await clock.advance(60, step=60)
        await steps.tick()
    run(go())
    assert [g.guild_id for g in grows(app)] == [8, 11, 7]
    assert grows(app)[-1].id == rivals.grow_id(sha, 7, 0)
    assert store.conn.execute("SELECT count(*) FROM realm_action WHERE kind='guild_grow'").fetchone()[0] == 3
    assert step_row(store)[1]["grow"] == {"7": "acked", "8": "acked", "11": "acked"}


def test_approval_binds_the_hash(tmp_path, store, clock, caplog):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    path, sha = approved(tmp_path, clock, with_cards(plan_of()))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["payload"]["guilds"][0]["name"] = "Other Name"
    doc["sha256"] = digest(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")

    async def go():
        await connect(app)
        await realm.part("steps").tick()
        await pump(realm, clock, rounds=1)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.steps"):
        run(go())
    assert app.sent == [] and counts(store) == (0, 0, 0)
    assert step_row(store) == (None, {"refused": "realm_step_hash_mismatch"})


def test_invalid_or_conflicting_plan_is_refused_once(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    with store.transaction():
        store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (40,'progression','horde','Old',1)")
    approved(tmp_path, clock, with_cards(plan_of()))

    async def go():
        await connect(app)
        await realm.part("steps").tick()
    run(go())
    assert app.sent == [] and counts(store) == (1, 0, 0)
    assert step_row(store)[1] == {"refused": "rivals_already_chosen"}


def test_disabled_realm_never_applies_or_tops_up(tmp_path, store, clock, monkeypatch):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock, on=False)
    armory = FakeArmory()
    realm.ports.armory = lambda: armory
    _, sha = approved(tmp_path, clock, with_cards(plan_of()))
    rows_for_topup(store, clock, plan_of())  # A top-up would be due, were the Realm enabled.

    async def go():
        await connect(app)
        for _ in range(3):
            await clock.advance(60, step=60)
            await realm.tick()
            await settle(realm)
    run(go())
    assert app.sent == [] and armory.threads == [] and store.get_meta(rivals.TOPUP_KEY) is None
    assert store.conn.execute("SELECT count(*) FROM realm_step WHERE sha256=?", (sha,)).fetchone()[0] == 0


# -- the weekly top-up --------------------------------------------------------------------------

def topup_guilds(prog=23, drama=27, gank=25):
    return [BotGuild(7, "Rival Name", 700, "horde", {(80, "horde"): prog, (79, "horde"): 2}),
            BotGuild(8, "Synthetic Eight", 800, "horde", {(80, "horde"): drama}),
            BotGuild(11, "Synthetic Eleven", 1100, "alliance", {(80, "alliance"): gank})]


def rows_for_topup(store, clock, plan):
    """The three rivals as an applied plan left them, two days ago."""
    now = clock.now()
    with store.transaction():
        for g in plan["guilds"]:
            store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (?,?,?,?,?)",
                               (g["guild_id"], g["role"], g["faction"], g["name"], now - 2 * 86400))
            for a in g["actors"]:
                store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES (?,?,?,?,?)",
                                   (a["guid"], g["guild_id"], a["name"], a["role"], now - 2 * 86400))
            for r in g["raiders"]:
                store.conn.execute("INSERT INTO realm_raider(guild,guid,class,role,avg_ilvl,checked) "
                                   "VALUES (?,?,?,?,?,?)", (g["guild_id"], r["guid"], r["class"], r["role"],
                                                            r["avg_ilvl"], now - 2 * 86400))
        store.conn.execute("INSERT INTO realm_step(name,sha256,approved,applied) VALUES ('rivals',?,?,?)",
                           ("a" * 64, now - 3 * 86400, now - 2 * 86400))


def test_weekly_top_up_only_adds(tmp_path, store, clock, caplog):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = plan_of()
    rows_for_topup(store, clock, plan)
    # Two of the progression rival's members were re-rolled below 80; the others are full.
    armory = FakeArmory(guilds=topup_guilds(prog=23))
    armory.pools["horde"] = [c for c in armory.pools["horde"] if c.guid not in planned_guids(plan)]
    realm.ports.armory = lambda: armory
    before = {t: set(map(tuple, store.conn.execute(f"SELECT * FROM {t}").fetchall()))
              for t in ("realm_guild", "realm_actor", "realm_raider")}
    part = realm.part("rivals")

    async def go():
        await connect(app)
        await part.tick()              # Thursday: this week's top-up is due
        await settle(realm)
        await pump(realm, clock, rounds=1)
        for days in (1, 1):            # Friday and Saturday: once a week
            await clock.advance(days * 86400, step=days * 86400)
            await part.tick()
            await settle(realm)
    with caplog.at_level(logging.INFO, logger="leeroy_mind.realm.rivals"):
        run(go())
    [grow] = app.sent
    assert type(grow) is protocol.GuildGrow and grow.guild_id == 7 and len(grow.guids) == 2
    best = sorted((c for c in armory.pools["horde"] if c.avg_ilvl >= 200 and c.guid not in ROSTER | {HUMAN}),
                  key=lambda c: (-c.avg_ilvl, c.guid))
    assert set(grow.guids) == {c.guid for c in best[:2]}
    after = {t: set(map(tuple, store.conn.execute(f"SELECT * FROM {t}").fetchall())) for t in before}
    assert before["realm_guild"] == after["realm_guild"] and before["realm_actor"] == after["realm_actor"]
    assert before["realm_raider"] < after["realm_raider"]  # Only adds.
    assert {row[1] for row in after["realm_raider"] - before["realm_raider"]} == set(grow.guids)
    assert sum("realm_rivals_topped guild=7 added=2" in r.getMessage() for r in caplog.records) == 1
    assert len(armory.threads) == 2  # bot_guilds, then the Horde pool: no Alliance read, no repeat
    # Sunday starts a new week; a restarted mind reads the week it already did.
    assert store.get_meta(rivals.TOPUP_KEY) == json.dumps("2026-10-25")
    app2, realm2 = live_realm(tmp_path, store, clock)
    realm2.ports.armory = lambda: armory

    async def restarted():
        await connect(app2)
        await realm2.part("rivals").tick()
        await settle(realm2)
    run(restarted())
    assert app2.sent == [] and len(armory.threads) == 2


def test_armory_never_on_loop(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    rows_for_topup(store, clock, plan_of())
    gate = threading.Event()
    armory = FakeArmory(guilds=topup_guilds(prog=24), gate=gate)
    realm.ports.armory = lambda: armory
    loop_thread = threading.get_ident()

    async def go():
        await connect(app)
        await asyncio.wait_for(realm.part("rivals").tick(), 1)  # returns while the read is blocked
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert armory.threads and app.sent == []
        await realm.tick()             # the Realm's loop keeps running meanwhile
        gate.set()
        await settle(realm)
        await pump(realm, clock, rounds=1)
    run(go())
    assert armory.threads and loop_thread not in armory.threads
    assert [len(c.guids) for c in grows(app)] == [1]


# -- the member query -----------------------------------------------------------------------------

def test_armory_queries_are_selects():
    for name, sql in rivals.MEMBER_QUERIES.items():
        mysql.check_select(sql.format(guild_id=7, ids="1,2,3"))
        assert sql.lstrip().upper().startswith("SELECT")


def test_member_query_parses_and_keeps_random_bots_only(monkeypatch):
    calls = []

    def query(db, sql, exe, **kwargs):
        calls.append((db.name, sql, kwargs))
        if "FROM account" in sql:
            return [["31"]]
        return [["700", "Leader", "2", "1", "80", "0", "31"], ["701", "Human", "2", "5", "80", "1", "32"]]
    monkeypatch.setattr(mysql, "query", query)
    db = lambda name: DbInfo("localhost", 3306, "reader", "synthetic-secret", name)  # noqa: E731
    armory = rivals.RivalArmory(RealmArmory(db("characters"), db("world"), db("auth"), "synthetic-mysql"))
    assert armory.guild_members(7) == [rivals.GuildMember(700, "Leader", 2, 1, 80, 0)]
    assert [(name, kwargs) for name, _, kwargs in calls] == [
        ("characters", dict(timeout=10, max_ms=5000)), ("auth", dict(timeout=10, max_ms=5000))]
    assert "guildid = 7" in calls[0][1] and "IN (31,32)" in calls[1][1]
    monkeypatch.setattr(mysql, "query", lambda *a, **k: [["x", "y"]])
    with pytest.raises(rivals.RealmArmoryUnavailable):
        armory.guild_members(7)
    with pytest.raises(ValueError):
        armory.guild_members(0)
