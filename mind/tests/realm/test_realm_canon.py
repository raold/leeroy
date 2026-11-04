"""Canon grants (plan Task 16): the history gate, the grant plan, its step and the grants, over
the real Realm service, direct ledger and Store with the small RealmApp surface."""
import asyncio
import hashlib
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind import eval as weekly
from leeroy_mind.protocol import Ack, Hello, Login, Logout
from leeroy_mind.realm import canon, cli, protocol
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.realm.steps import approve, write_plan
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.turns import TurnKind
from realm.fakes import RealmApp, enabled

HUMAN = 1
CAST = frozenset({101, 102, 103, 104, 105})
REALM_FIRSTS = (3117, 4078, 4576)
GRANTS = {"3117": "M35", "4078": "M36", "4576": "M37"}
KILLS = [
    {"map": 533, "boss_id": 0, "difficulty": 0, "moment": "M05", "day": "2008-12-03"},
    {"map": 533, "boss_id": 0, "difficulty": 1, "moment": "M05", "day": "2008-12-03"},
    {"map": 603, "boss_id": 13, "difficulty": 1, "moment": "M35", "day": "2009-06-20"},
    {"map": 649, "boss_id": 4, "difficulty": 3, "moment": "M36", "day": "2009-09-30"},
    {"map": 631, "boss_id": 12, "difficulty": 3, "moment": "M37", "day": "2009-12-30"},
]
LEGENDS = {"2186": {"moment": "M28", "cast": [101, 102], "human": True},
           "3007": {"moment": "M33", "cast": [103], "human": False}}


class Armory:
    """The armory's three canon reads; nothing else may be asked."""

    def __init__(self, holders=None, *, gm=0, rbac_8=False):
        self.holders, self.gm, self.rbac_8, self.asked = holders or {}, gm, rbac_8, []

    def achievement_holders(self, ids):
        self.asked.append(("holders", tuple(sorted(ids))))
        return {a: list(self.holders.get(a, ())) for a in ids}

    def account_gm_level(self, guid):
        self.asked.append(("gm", guid))
        return self.gm

    def account_has_permission(self, guid, perm):
        self.asked.append(("perm", guid, perm))
        return self.rbac_8 and perm == 8


def remember(store, guid, moment):
    with store.transaction():
        store.conn.execute(
            "INSERT INTO memory(owner,kind,text,t_event,t_created,t_accessed,importance,valence,source,"
            "evidence,key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (guid, "story", "we finally got it done", 1, 1, 1, 6, 0.0, "owner",
             json.dumps([f"owner:{moment}"]), f"owner:{moment}:{guid}"))


def history_files(root, store, *, memories=3, canon_doc=None):
    """The extension seed (M35-M37), its canon sidecar and his seeded owner memories."""
    private = root / "private"
    private.mkdir(exist_ok=True)
    entries = [{"guid": 101 + i, "text": f"{moment} at last", "moment": moment, "kind": "moment"}
               for moment in ("M35", "M36", "M37") for i in range(3)]
    seed = private / "guild-history-extension-seed.json"
    seed.write_text(json.dumps(entries), encoding="utf-8")
    sidecar = private / "guild-history-extension-canon.json"
    sidecar.write_text(json.dumps(canon_doc or {"grants": GRANTS, "kills": KILLS}), encoding="utf-8")
    remember(store, 104, "M05")  # The seeded history's own moment, for the Naxxramas kills.
    for moment in ("M35", "M36", "M37"):
        for i in range(memories if moment == "M35" else 3):
            remember(store, 101 + i, moment)
    return seed, sidecar


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approved_plan(root, store, clock, *, recipients=None, armory=None, approve_history=True):
    """His history OK, then the canon plan and his OK on it; the plan file's path."""
    seed, sidecar = history_files(root, store)
    canon.approve_history(root / "realm", seed, sidecar, now=clock.now())
    history = canon.check_history(root / "realm", store.conn)
    assert history.ok, history.reason
    payload = canon.plan_canon(armory or Armory(), LEGENDS if recipients is None else recipients,
                               history, human=HUMAN, cast=CAST)
    path = write_plan(root, "canon", payload, local=clock.local())
    approve(root, path, now=clock.now())
    if not approve_history:
        for approval in (root / "realm" / "approved").glob("history-*.json"):
            approval.unlink()
    return path, seed, sidecar


class Harness:
    """The real Realm over RealmApp: a bridge that answers each grant, a raid phase, a historian."""

    def __init__(self, tmp_path, store, clock, monkeypatch):
        seed_test_cast(store)
        self.root, self.store, self.clock = tmp_path, store, clock
        self.app = RealmApp(tmp_path, store, clock)
        self.phase = RaidPhase.NONE
        self.app.raid = SimpleNamespace(phase=lambda now: self.phase)
        for guid, title in ((101, ""), (102, ""), (103, ""), (104, ""), (105, "Historian")):
            self.app.personas[guid] = make_persona(guid, f"Member{guid}", officer_title=title)
        self.answers = {}   # (achievement, guid) or achievement -> native's error ("" is ok)
        self.hold = None    # an asyncio.Event the grant acks wait for
        self.fail = None    # an exception type the next grant's transport raises
        self.times = []
        self.app.send = self.bridge
        # The weekly report's registry is process-wide: never leak this test's section.
        monkeypatch.setattr(weekly, "_sections", dict(weekly._sections))
        self.realm = None

    async def bridge(self, cmd, *, preserve_id):
        assert preserve_id is True
        self.app.sent.append(cmd)
        if type(cmd) is not protocol.GrantAchievement:
            return Ack(0, cmd.id, True, "")
        self.times.append(self.clock.now())
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error("synthetic transport loss")
        if self.hold is not None:
            await self.hold.wait()
        key = (cmd.achievement_id, cmd.guid)
        error = self.answers.get(key, self.answers.get(cmd.achievement_id, self.answers.get("*", "")))
        return Ack(0, cmd.id, error == "", error)

    def grants(self, *, guid=None):
        return [(c.achievement_id, c.guid) for c in self.app.sent
                if type(c) is protocol.GrantAchievement and (guid is None or c.guid == guid)]

    def ms(self):
        return int(self.clock.now() * 1000)

    async def start(self, *, online=False):
        enabled(self.root, True)
        self.realm = attach_realm(self.app)
        await self.app.connection(True)
        await self.app.event(Hello(self.ms(), "test", "1", 21, HUMAN if online else 0))
        await self.tick()
        return self.realm

    async def settle(self):
        for _ in range(20):
            children = tuple(self.realm._children)
            if not children:
                return
            await asyncio.gather(*children, return_exceptions=True)

    async def tick(self, *, settle=True):
        await self.realm.tick()
        if settle:
            await self.settle()
        else:
            for _ in range(5):
                await asyncio.sleep(0)

    async def drive(self, seconds, *, step=1.0, settle=True):
        elapsed = 0.0
        while elapsed < seconds:
            await self.clock.advance(step, step=step)
            elapsed += step
            await self.tick(settle=settle)

    async def login(self):
        await self.app.event(Login(self.ms(), HUMAN, True))

    async def logout(self):
        await self.app.event(Logout(self.ms(), HUMAN, True))

    def rows(self):
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in self.store.conn.execute(
            "SELECT achievement,guid,state,reason,tries FROM realm_canon")}

    def refs(self):
        return [tuple(r) for r in self.store.conn.execute(
            "SELECT ref,state FROM realm_direct WHERE owner='canon' ORDER BY rowid")]

    def whispers(self):
        return [call for call in self.app.engine.say_as.await_args_list]

    async def close(self):
        await self.realm.close()


@pytest.fixture
def h(tmp_path, store, clock, monkeypatch):
    return Harness(tmp_path, store, clock, monkeypatch)


# -- the history gate --------------------------------------------------------------------------

def test_history_ok_binds_both_files(tmp_path, store, clock):
    realm_dir = tmp_path / "realm"
    seed, sidecar = history_files(tmp_path, store)
    assert canon.history_ok(tmp_path, store) is False  # Not approved yet.
    assert canon.check_history(realm_dir, store.conn).reason == "no_history_approval"
    target = canon.approve_history(realm_dir, seed, sidecar, now=clock.now())
    assert target.name == f"history-{sha(seed)}-{sha(sidecar)}.json"
    assert target.parent == realm_dir / "approved"
    history = canon.check_history(realm_dir, store.conn)
    assert history.ok and (history.seed_sha256, history.canon_sha256) == (sha(seed), sha(sidecar))
    assert history.grants == {3117: "M35", 4078: "M36", 4576: "M37"}
    assert canon.history_ok(tmp_path, store) is True
    original = sidecar.read_bytes()
    edited = json.loads(original)
    edited["kills"] = edited["kills"][:-1]
    sidecar.write_text(json.dumps(edited), encoding="utf-8")
    assert canon.history_ok(tmp_path, store) is False
    assert canon.check_history(realm_dir, store.conn).reason == "history_changed"
    sidecar.write_bytes(original)
    assert canon.history_ok(tmp_path, store) is True
    seed.write_text(seed.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # Even a byte.
    assert canon.history_ok(tmp_path, store) is False


def test_grant_needs_seeded_memories(tmp_path, store, clock):
    seed, sidecar = history_files(tmp_path, store, memories=2)
    canon.approve_history(tmp_path / "realm", seed, sidecar, now=clock.now())
    history = canon.check_history(tmp_path / "realm", store.conn)
    assert not history.ok and history.reason == "memories_missing"
    remember(store, 104, "M35")
    assert canon.history_ok(tmp_path, store) is True
    # A memory that was retired, or not his own seeded kind, does not count.
    store.conn.execute("UPDATE memory SET status='archived' WHERE key='owner:M35:104'")
    store.conn.commit()
    assert canon.check_history(tmp_path / "realm", store.conn).reason == "memories_missing"


def test_every_moment_must_exist(tmp_path, store, clock):
    doc = {"grants": GRANTS, "kills": KILLS + [
        {"map": 616, "boss_id": 0, "difficulty": 1, "moment": "M99", "day": "2009-02-02"}]}
    seed, sidecar = history_files(tmp_path, store, canon_doc=doc)
    canon.approve_history(tmp_path / "realm", seed, sidecar, now=clock.now())
    assert canon.check_history(tmp_path / "realm", store.conn).reason == "moments_missing"
    remember(store, 105, "M99")  # Now in the seeded history.
    assert canon.history_ok(tmp_path, store) is True


def test_history_files_are_strict(tmp_path, store, clock):
    seed, sidecar = history_files(tmp_path, store)
    entries = json.loads(seed.read_text(encoding="utf-8"))
    bad_seed = tmp_path / "private" / "bad-seed.json"
    bad_seed.write_text(json.dumps(entries + [{"guid": 101, "text": "x", "moment": "M35",
                                               "kind": "moment", "canon": {}}]), encoding="utf-8")
    bad_grants = tmp_path / "private" / "bad-grants.json"
    bad_grants.write_text(json.dumps({"grants": {"3117": "M35"}, "kills": KILLS}), encoding="utf-8")
    late = tmp_path / "private" / "late.json"
    late.write_text(json.dumps({"grants": GRANTS, "kills": [dict(KILLS[0], day="2010-02-03")]}),
                    encoding="utf-8")
    for s, c in ((bad_seed, sidecar), (seed, bad_grants), (seed, late)):
        with pytest.raises(canon.CanonError):
            canon.approve_history(tmp_path / "realm", s, c, now=clock.now())
    assert not (tmp_path / "realm" / "approved").exists()


def test_history_ok_cli_writes_only_its_approval(tmp_path, store, clock, capsys):
    seed, sidecar = history_files(tmp_path, store)
    db = tmp_path / "mind.db"
    before = db.stat().st_mtime_ns, db.stat().st_size
    argv = ["canon", "history-ok", str(seed), str(sidecar), "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["approved"] == f"history-{sha(seed)}-{sha(sidecar)}.json"
    assert result["history_ok"] is True and str(tmp_path) not in out
    approval = json.loads((tmp_path / "realm" / "approved" / result["approved"]).read_text(encoding="utf-8"))
    assert (approval["seed_sha256"], approval["canon_sha256"]) == (sha(seed), sha(sidecar))
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
    assert cli.realm_main(["canon", "history-ok", str(seed), "--data-dir", str(tmp_path)], clock=clock) == 1
    missing = tmp_path / "private" / "nowhere.json"
    assert cli.realm_main(["canon", "history-ok", str(seed), str(missing), "--data-dir", str(tmp_path)],
                          clock=clock) == 1
    assert str(tmp_path) not in capsys.readouterr().out


# -- the plan ----------------------------------------------------------------------------------

@pytest.fixture
def history(tmp_path, store, clock):
    seed, sidecar = history_files(tmp_path, store)
    canon.approve_history(tmp_path / "realm", seed, sidecar, now=clock.now())
    return canon.check_history(tmp_path / "realm", store.conn)


def plan_for(history, armory=None, recipients=None):
    return canon.plan_canon(armory or Armory(), LEGENDS if recipients is None else recipients,
                            history, human=HUMAN, cast=CAST)


def grants_of(plan):
    return {(g["achievement"], g["guid"]): (g["state"], g["reason"]) for g in plan["grants"]}


def test_realm_firsts_go_to_him_only(history):
    armory = Armory()
    plan = plan_for(history, armory)
    for a in REALM_FIRSTS:
        entry = plan["achievements"][str(a)]
        assert entry["recipients"] == [HUMAN] and entry["kind"] == "realm_first"
        assert "playerbots' guard" in entry["note"]
    assert plan["achievements"]["2186"]["recipients"] == [101, 102, HUMAN]
    assert plan["achievements"]["3007"]["recipients"] == [103]
    assert plan["achievements"]["1723"]["recipients"] == [] == plan["achievements"]["1786"]["recipients"]
    assert grants_of(plan) == {**{(a, HUMAN): ("planned", "") for a in REALM_FIRSTS},
                               (2186, 101): ("planned", ""), (2186, 102): ("planned", ""),
                               (2186, HUMAN): ("planned", ""), (3007, 103): ("planned", "")}
    assert plan["account"] == {"gm_level": 0, "rbac_8": False}
    assert plan["kill_counts"] == {"533": 2, "603": 1, "631": 1, "649": 1} and plan["kills"] == KILLS
    assert armory.asked == [("holders", (1723, 1786, 2186, 3007, 3117, 4078, 4576)), ("gm", HUMAN),
                            ("perm", HUMAN, 8)]


def test_holder_outside_guild_blocks(history):
    holders = {4078: [(5001, "Outsider", 7)], 3117: [(HUMAN, "Him", 21)], 3007: [(103, "Coros", 21)]}
    plan = plan_for(history, Armory(holders))
    assert plan["achievements"]["4078"]["blocked"] == "holder_outside_guild"
    assert plan["achievements"]["4078"]["holders"] == [{"guid": 5001, "name": "Outsider", "guild": 7}]
    grants = grants_of(plan)
    assert grants[(4078, HUMAN)] == ("blocked", "holder_outside_guild")
    assert all(state == ("planned", "") for key, state in grants.items() if key[0] != 4078)
    # Guildless (an orphaned row) is outside the guild too.
    plan = plan_for(history, Armory({2186: [(6001, "", 0)]}))
    assert {k: v for k, v in grants_of(plan).items() if k[0] == 2186} == {
        (2186, g): ("blocked", "holder_outside_guild") for g in (101, 102, HUMAN)}


def test_rbac_8_blocks_his_grants(history):
    plan = plan_for(history, Armory(gm=3, rbac_8=True))
    grants = grants_of(plan)
    assert all(grants[(a, HUMAN)] == ("blocked", "rbac_8") for a in REALM_FIRSTS)
    assert all(grants[key] == ("planned", "") for key in ((2186, 101), (2186, 102), (2186, HUMAN), (3007, 103)))
    assert plan["account"] == {"gm_level": 3, "rbac_8": True}
    assert any("GM mode" in note for note in plan["notes"])


def test_legend_recipients_schema(history):
    plan = plan_for(history, recipients={"1786": {"moment": "M34", "cast": [], "human": True}})
    assert plan["achievements"]["1786"]["recipients"] == [HUMAN]
    for bad, named in [({"2186": {"moment": "M28", "cast": [999], "human": False}}, "guid 999 is not in the cast"),
                       ({"2186": {"moment": "M28", "cast": [HUMAN], "human": False}}, "guid 1 is him"),
                       ({"2186": {"moment": "M28", "cast": [101, 101], "human": False}}, "guid 101 is listed twice"),
                       ({"2186": {"moment": "M28", "cast": [101]}}, "legend 2186"),
                       ({"2186": {"moment": "M28", "cast": [101], "human": "yes"}}, "legend 2186"),
                       ({"2186": {"moment": "", "cast": [101], "human": False}}, "legend 2186"),
                       ([], "recipients")]:
        with pytest.raises(canon.CanonError, match=named):
            plan_for(history, recipients=bad)
    with pytest.raises(canon.CanonError):  # A plan needs an approved history.
        canon.plan_canon(Armory(), {}, canon.History(False, "no_history_approval"), human=HUMAN, cast=CAST)


def test_only_whitelisted_ids(history):
    with pytest.raises(canon.CanonError, match="1402"):
        plan_for(history, recipients={"1402": {"moment": "M28", "cast": [101], "human": False}})
    with pytest.raises(canon.CanonError, match="3117"):
        plan_for(history, recipients={"3117": {"moment": "M35", "cast": [101], "human": False}})
    with pytest.raises(canon.CanonError, match="not a number"):
        plan_for(history, recipients={"cheese": {"moment": "M28", "cast": [], "human": True}})


def test_plan_cli_reads_only_and_writes_its_plan(tmp_path, store, clock, capsys, monkeypatch):
    seed_test_cast(store)
    seed, sidecar = history_files(tmp_path, store)
    store.set_meta("human_guid", str(HUMAN))
    recipients = tmp_path / "private" / "legend-recipients.json"
    recipients.write_text(json.dumps(LEGENDS), encoding="utf-8")
    armory = Armory()
    monkeypatch.setattr(canon, "armory_from_config", lambda path: armory)
    argv = ["plan", "canon", "--recipients", str(recipients), "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 1  # No history OK yet: no plan.
    assert "history" in capsys.readouterr().out and not (tmp_path / "realm" / "plans").exists()
    canon.approve_history(tmp_path / "realm", seed, sidecar, now=clock.now())
    db = tmp_path / "mind.db"
    before = db.stat().st_mtime_ns, db.stat().st_size
    assert cli.realm_main(argv, clock=clock) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    plan = tmp_path / "realm" / "plans" / result["plan"]
    doc = json.loads(plan.read_text(encoding="utf-8"))
    assert doc["step"] == "canon" and result["sha256"] == doc["sha256"]
    assert grants_of(doc["payload"])[(2186, HUMAN)] == ("planned", "")
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before and str(tmp_path) not in out
    monkeypatch.setattr(canon, "armory_from_config", lambda path: None)
    assert cli.realm_main(argv, clock=clock) == 1
    assert "armory" in capsys.readouterr().out


# -- the step ----------------------------------------------------------------------------------

def test_grant_refused_without_history_ok(h, caplog):
    approved_plan(h.root, h.store, h.clock, approve_history=False)

    async def go():
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.start()
            await h.login()
            await h.drive(40)
        assert h.grants() == [] and h.rows() == {}
        [(result,)] = h.store.conn.execute("SELECT result FROM realm_step WHERE name='canon'").fetchall()
        assert json.loads(result)["reason"] == "history_not_ok"
        assert "realm_canon_refused reason=history_not_ok" in caplog.text
        await h.close()
    run(go())


def test_grant_refused_when_seed_changed(h, caplog):
    _, seed, sidecar = approved_plan(h.root, h.store, h.clock)
    seed.write_text(seed.read_text(encoding="utf-8").replace("at last", "at long last"), encoding="utf-8")

    async def go():
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.start(online=True)
            await h.drive(40)
        assert h.grants() == [] and h.rows() == {}
        assert "realm_canon_refused reason=history_not_ok" in caplog.text
        await h.close()
    run(go())


def test_a_fresh_history_ok_does_not_carry_an_old_plan(h, caplog):
    _, seed, sidecar = approved_plan(h.root, h.store, h.clock)
    seed.write_text(seed.read_text(encoding="utf-8").replace("at last", "at long last"), encoding="utf-8")
    canon.approve_history(h.root / "realm", seed, sidecar, now=h.clock.now() + 1)  # His OK on the edit.

    async def go():
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.start(online=True)
            await h.drive(40)
        assert h.grants() == [] and h.rows() == {}
        assert "realm_canon_refused reason=history_hash_changed" in caplog.text
        await h.close()
    run(go())


def test_grants_stop_when_the_history_changes_after_apply(h, caplog):
    _, seed, _ = approved_plan(h.root, h.store, h.clock, recipients={})
    original = seed.read_bytes()

    async def go():
        await h.start()
        assert {key: row[0] for key, row in h.rows().items()} == {(a, HUMAN): "planned" for a in REALM_FIRSTS}
        seed.write_bytes(original + b" ")
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.login()
            await h.drive(40)
        assert h.grants() == [] and "realm_canon_refused reason=history_not_ok" in caplog.text
        seed.write_bytes(original)  # The approved content again: the gate opens.
        await h.drive(2)
        assert h.grants() == [(3117, HUMAN)]
        await h.close()
    run(go())


def test_canon_kills_seeded(h):
    approved_plan(h.root, h.store, h.clock)
    seeded = []

    class Race:
        def seed_canon(self):
            seeded.append([tuple(r) for r in h.store.conn.execute(
                "SELECT map,boss_id,difficulty,moment,day FROM realm_canon_kill ORDER BY map,boss_id,difficulty")])
            return True

    async def go():
        enabled(h.root, True)
        h.realm = attach_realm(h.app)
        h.realm.parts["race"] = Race()
        await h.app.connection(True)
        await h.app.event(Hello(h.ms(), "test", "1", 21, HUMAN))
        await h.tick()
        expected = sorted((k["map"], k["boss_id"], k["difficulty"], k["moment"], k["day"]) for k in KILLS)
        assert seeded == [expected]
        assert {key: row[0] for key, row in h.rows().items()} == {
            **{(a, HUMAN): "planned" for a in REALM_FIRSTS}, (2186, 101): "planned", (2186, 102): "planned",
            (2186, HUMAN): "planned", (3007, 103): "planned"}
        [(result,)] = h.store.conn.execute("SELECT result FROM realm_step WHERE name='canon'").fetchall()
        assert json.loads(result)["race_seeded"] is True
        await h.drive(120)  # The step applied once: nothing seeds again.
        assert len(seeded) == 1
        await h.close()
    run(go())


def test_apply_rechecks_the_plan(h, caplog):
    """An approved plan file that grants a realm first to the cast, or names 1402, stores nothing."""
    history_files(h.root, h.store)
    canon.approve_history(h.root / "realm", h.root / "private" / "guild-history-extension-seed.json",
                          h.root / "private" / "guild-history-extension-canon.json", now=h.clock.now())
    history = canon.check_history(h.root / "realm", h.store.conn)
    payload = canon.plan_canon(Armory(), {}, history, human=HUMAN, cast=CAST)
    for forged in ({"achievement": 3117, "guid": 101, "state": "planned", "reason": ""},
                   {"achievement": 1402, "guid": HUMAN, "state": "planned", "reason": ""}):
        bad = dict(payload, grants=payload["grants"] + [forged])
        approve(h.root, write_plan(h.root, "canon", bad, local=h.clock.local()), now=h.clock.now())

    async def go():
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.start(online=True)
            await h.drive(40)
        assert h.grants() == [] and h.rows() == {}
        assert caplog.text.count("realm_canon_refused reason=invalid_plan") == 2
        await h.close()
    run(go())


def test_a_new_plan_never_touches_a_granted_row(h):
    approved_plan(h.root, h.store, h.clock, recipients={}, armory=Armory({4078: [(5001, "Outsider", 7)]}))

    async def go():
        await h.start()
        await h.login()
        await h.drive(36)
        assert h.grants() == [(3117, HUMAN), (4576, HUMAN)]
        assert h.rows()[(4078, HUMAN)] == ("blocked", "holder_outside_guild", 0)
        granted = h.store.conn.execute("SELECT * FROM realm_canon WHERE state='granted' ORDER BY achievement").fetchall()
        # His word on 4078: a fresh plan, approved, without the outsider.
        history = canon.check_history(h.root / "realm", h.store.conn)
        payload = canon.plan_canon(Armory(), {}, history, human=HUMAN, cast=CAST)
        approve(h.root, write_plan(h.root, "canon", payload, local=h.clock.local()), now=h.clock.now())
        await h.drive(62)
        assert h.grants() == [(3117, HUMAN), (4576, HUMAN), (4078, HUMAN)]
        assert all(h.rows()[(a, HUMAN)] == ("granted", "", 0) for a in REALM_FIRSTS)
        assert len(h.refs()) == 3
        assert [tuple(r) for r in granted] == [tuple(r) for r in h.store.conn.execute(
            "SELECT * FROM realm_canon WHERE achievement IN (3117,4576) ORDER BY achievement")]
        await h.close()
    run(go())


# -- the grants --------------------------------------------------------------------------------

def test_legends_only_while_he_is_offline(h):
    approved_plan(h.root, h.store, h.clock)

    async def go():
        await h.start(online=True)
        await h.drive(60)
        assert [g for g in h.grants() if g[1] != HUMAN] == []  # His own go; the cast's wait.
        await h.logout()
        before = len(h.times)
        await h.drive(5, step=0.5)  # Ticks faster than the pace: still one a second.
        cast = [g for g in h.grants() if g[1] != HUMAN]
        assert cast == [(2186, 101), (2186, 102), (3007, 103)]
        sent = h.times[before:]
        assert [b - a for a, b in zip(sent, sent[1:])] == [1.0, 1.0]
        assert all(h.rows()[g][0] == "granted" for g in cast)
        await h.close()
    run(go())


def test_first_grant_then_ians_whisper(h):
    approved_plan(h.root, h.store, h.clock, recipients={})

    async def go():
        await h.start()
        await h.login()
        await h.drive(29)
        assert h.grants() == []
        h.hold = asyncio.Event()
        await h.drive(1, settle=False)
        assert h.grants() == [(3117, HUMAN)]
        await h.drive(3, settle=False)  # Waiting for its ack: nothing else, and no whisper.
        assert h.grants() == [(3117, HUMAN)] and h.whispers() == []
        h.hold.set()
        h.hold = None
        await h.settle()
        [call] = h.whispers()
        assert call.args == (105, "whisper", canon.WHISPER)
        assert call.kwargs["to_guid"] == HUMAN and call.kwargs["kind"] is TurnKind.WHISPER
        assert callable(call.kwargs["before_transport"])
        acked = h.clock.now()
        await h.drive(6)
        assert h.grants() == [(a, HUMAN) for a in REALM_FIRSTS]
        assert h.times[1:] == [acked + 2, acked + 4]
        assert len(h.whispers()) == 1
        assert all(h.rows()[(a, HUMAN)] == ("granted", "", 0) for a in REALM_FIRSTS)
        assert [state for _, state in h.refs()] == ["acked"] * 3
        await h.close()
    run(go())


def test_the_whisperer_is_the_standing_lead_without_a_historian(h):
    approved_plan(h.root, h.store, h.clock, recipients={})
    h.app.personas[105] = make_persona(105, "Member105")
    h.app.personas[102] = make_persona(102, "Member102", officer_title="Raid Lead")

    async def go():
        await h.start()
        await h.login()
        await h.drive(32)
        [call] = h.whispers()
        assert call.args[0] == 102
        await h.close()
    run(go())


def test_no_whisper_when_nothing_new(h):
    approved_plan(h.root, h.store, h.clock, recipients={})
    h.answers["*"] = "has_it"

    async def go():
        await h.start()
        await h.login()
        await h.drive(40)
        assert h.grants() == [(a, HUMAN) for a in REALM_FIRSTS]
        assert all(h.rows()[(a, HUMAN)] == ("granted", "has_it", 0) for a in REALM_FIRSTS)
        assert h.whispers() == []
        await h.close()
    run(go())


def test_gm_mode_retries_every_ten_minutes(h, caplog):
    approved_plan(h.root, h.store, h.clock, recipients={})
    h.answers["*"] = "gm_mode"

    async def go():
        with caplog.at_level(logging.INFO, logger="leeroy_mind.realm.canon"):
            await h.start()
            await h.login()
            await h.drive(30)
            assert h.grants() == [(3117, HUMAN)]
            assert h.rows()[(3117, HUMAN)] == ("planned", "gm_mode", 1)
            await h.drive(599, step=599)  # His whole queue waits ten minutes.
            assert len(h.grants()) == 1
            await h.drive(1)
            assert h.grants() == [(3117, HUMAN)] * 2 and h.rows()[(3117, HUMAN)] == ("planned", "gm_mode", 2)
            await h.logout()
            await h.drive(60)
            await h.login()  # The next login tries again, 30 s in.
            await h.drive(29)
            assert len(h.grants()) == 2
            await h.drive(1)
            assert len(h.grants()) == 3 and h.rows()[(3117, HUMAN)] == ("planned", "gm_mode", 3)
        assert h.whispers() == [] and h.refs() == [
            (f"canon:3117:{HUMAN}:{n}", "refused") for n in range(3)]
        assert "realm_grant_deferred reason=gm_mode" in caplog.text
        h.answers.clear()  # He left GM mode.
        await h.drive(600, step=600)
        await h.drive(4)
        assert h.grants()[3:] == [(a, HUMAN) for a in REALM_FIRSTS] and len(h.whispers()) == 1
        assert h.rows()[(3117, HUMAN)] == ("granted", "", 3)
        await h.close()
    run(go())


def test_busy_refusal_defers_like_gm_mode(h):
    approved_plan(h.root, h.store, h.clock)
    h.answers["*"] = "busy:training_cleanup"
    h.answers[(2186, 101)] = "busy:raid_owner"
    h.answers[(2186, 102)] = ""
    h.answers[(3007, 103)] = ""

    async def go():
        await h.start()  # He is offline: the cast's legends go first.
        await h.drive(3)
        assert h.grants() == [(2186, 101), (2186, 102), (3007, 103)]
        assert h.rows()[(2186, 101)] == ("planned", "busy:raid_owner", 1)  # Native's own reason.
        await h.drive(590, step=590)
        assert len(h.grants()) == 3  # That member waits ten minutes; the others went on.
        h.answers[(2186, 101)] = ""
        await h.drive(10)
        assert h.grants()[3:] == [(2186, 101)] and h.rows()[(2186, 101)] == ("granted", "", 1)
        await h.login()
        await h.drive(30)
        assert h.grants(guid=HUMAN) == [(3117, HUMAN)]
        assert h.rows()[(3117, HUMAN)] == ("planned", "busy:training_cleanup", 1)
        await h.drive(599, step=599)
        assert h.grants(guid=HUMAN) == [(3117, HUMAN)] and h.whispers() == []
        await h.drive(1)
        assert h.grants(guid=HUMAN) == [(3117, HUMAN)] * 2
        await h.close()
    run(go())


def test_grant_sent_during_waiting_call(h):
    approved_plan(h.root, h.store, h.clock, recipients={})
    night = SimpleNamespace(state=SimpleNamespace(name="WAITING_CALL"), session=None, run=None, _closing=[])
    h.app.guild = SimpleNamespace(raid_night=night)

    async def go():
        await h.start()
        await h.login()
        await h.drive(30)
        assert h.realm.ports.raid_holds_all() is True
        assert h.grants() == [(3117, HUMAN)] and h.rows()[(3117, HUMAN)][0] == "granted"
        await h.close()
    run(go())


def test_no_grant_during_his_fights(h):
    approved_plan(h.root, h.store, h.clock, recipients={})

    async def go():
        await h.start()
        await h.login()
        h.phase = RaidPhase.COMBAT
        await h.drive(40)
        h.phase = RaidPhase.WIPE_DEBRIEF
        await h.drive(10)
        assert h.grants() == []
        h.phase = RaidPhase.TRASH
        await h.drive(1)
        assert h.grants() == [(3117, HUMAN)]
        await h.close()
    run(go())


def test_refused_is_final(h, caplog):
    approved_plan(h.root, h.store, h.clock, recipients={})
    h.answers[3117] = "refused"

    async def go():
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.canon"):
            await h.start()
            await h.login()
            await h.drive(36)
        assert h.grants() == [(a, HUMAN) for a in REALM_FIRSTS]
        assert h.rows()[(3117, HUMAN)] == ("refused", "refused", 0)
        await h.logout()
        await h.login()
        await h.drive(40)
        assert len(h.grants()) == 3  # Never tried again.
        assert "realm_grant_refused achievement=3117" in caplog.text
        await h.close()
    run(go())


def test_unknown_grant_retried_next_login_safely(h):
    approved_plan(h.root, h.store, h.clock, recipients={})

    async def go():
        await h.start()
        await h.login()
        await h.drive(29)
        h.fail = ConnectionResetError  # Lost after the bridge may have written it.
        await h.drive(1)
        assert h.refs() == [(f"canon:3117:{HUMAN}:0", "unknown")]
        assert h.rows()[(3117, HUMAN)] == ("planned", "unknown", 1)
        await h.drive(700, step=10)
        assert h.grants() == [(a, HUMAN) for a in REALM_FIRSTS]  # The others went; it waits.
        await h.logout()
        await h.login()
        h.answers[3117] = "has_it"  # It had landed after all.
        await h.drive(30)
        assert h.grants()[3:] == [(3117, HUMAN)]
        assert h.refs()[0] == (f"canon:3117:{HUMAN}:0", "unknown")  # Never resent on its own ref.
        assert (f"canon:3117:{HUMAN}:1", "refused") in h.refs()
        assert h.rows()[(3117, HUMAN)] == ("granted", "has_it", 1)
        await h.close()
    run(go())


def test_an_unknown_found_at_restart_takes_a_fresh_ref(h):
    """An earlier mind dispatched a grant and died: it is unknown, never resent on its ref."""
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger
    approved_plan(h.root, h.store, h.clock, recipients={})

    async def go():
        await h.start()  # Applies the step: his rows are planned.
        await h.close()
        earlier = DirectLedger(h.store, process_id="earlier", clock=h.clock)
        context = ActionContext(True, True, True, 1, "earlier", 1, 21, HUMAN)
        lost = protocol.GrantAchievement("a" * 32, HUMAN, 3117)
        earlier.prepare(lost, owner="canon", ref=f"canon:3117:{HUMAN}:0", context=context, expects_job=False)
        earlier.before_send(lost, context=context)
        h.app.sent.clear()
        await h.start()  # A new mind: recovery makes it unknown.
        await h.login()
        h.answers[3117] = "has_it"
        await h.drive(36)
        assert h.grants() == [(a, HUMAN) for a in REALM_FIRSTS]
        assert h.refs()[0] == (f"canon:3117:{HUMAN}:0", "unknown")
        assert h.rows()[(3117, HUMAN)] == ("granted", "has_it", 1)
        await h.close()
    run(go())


def test_deferrals_in_the_weekly_report(h):
    approved_plan(h.root, h.store, h.clock, recipients={})
    h.answers["*"] = "gm_mode"

    async def go():
        await h.start()
        assert weekly._sections["realm_canon"] is canon.report_section
        await h.login()
        await h.drive(30)
        await h.drive(600, step=600)
        await h.close()
    run(go())
    with h.store.conn:
        h.store.conn.execute("UPDATE realm_canon SET state='blocked',reason='rbac_8' WHERE achievement=4576")
    ctx = SimpleNamespace(store=h.store, tz=h.clock.local().tzinfo, clock=h.clock)
    section = canon.report_section(ctx, "2026-W44")
    assert section["status"] == "complete"
    assert section["deferred"] == [{"achievement": 3117, "guid": HUMAN, "reason": "gm_mode", "tries": 2}]
    assert section["blocked"] == [{"achievement": 4576, "guid": HUMAN, "reason": "rbac_8", "tries": 0}]
    assert section["planned"] == 2 and section["granted"] == 0 and section["refused"] == []
    assert canon.report_section(ctx, "2026-W40")["deferred"] == []  # Another week's.


def test_attach_is_quiet_and_registers_its_step(tmp_path, store, clock, monkeypatch):
    monkeypatch.setattr(weekly, "_sections", {})
    app = RealmApp(tmp_path, store, clock)

    def forbidden(*args, **kwargs):
        raise AssertionError("canon's attach did I/O or started work")
    before = store.conn.total_changes
    with monkeypatch.context() as quiet:
        for target, name in ((asyncio, "create_task"), (asyncio, "to_thread"), (sqlite3, "connect")):
            quiet.setattr(target, name, forbidden)
        realm = attach_realm(app)  # Disabled: the part is there, the report section is not.
    part = realm.part("canon")
    assert type(part) is canon.Canon and "canon" in realm.part("steps")._applies
    assert any(p is part for p, names in realm._parts if names == frozenset({"login", "logout"}))
    assert store.conn.total_changes == before and app.sent == [] and not (tmp_path / "realm").exists()
    assert weekly._sections == {}
    run(realm.close())
    monkeypatch.setattr(weekly, "_sections", {})
    enabled(tmp_path, True)
    realm = attach_realm(RealmApp(tmp_path, store, clock))
    assert weekly._sections == {"realm_canon": canon.report_section}
    run(realm.close())


def test_disabled_never_grants(h):
    approved_plan(h.root, h.store, h.clock, recipients={})

    async def go():
        await h.start()
        enabled(h.root, False)
        h.realm.reload()
        await h.login()
        await h.drive(60)
        assert h.grants() == []
        await h.close()
    run(go())
