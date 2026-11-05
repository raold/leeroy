"""The bank's two data steps and its ledger, over the real Realm, Steps and DirectLedger with a
small app whose bridge answers as mod-leeroy does. Nothing here reads a live file or server."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import logging

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import bank, cli, protocol, steps
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.testing.fake_clock import FakeClock
from realm.fakes import RealmApp, enabled

DKP = 104  # Dunna, the guild's DKP officer in these tests.
CORE = (1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000)  # the core's defaults
NAMES = ["raid consumables", "gems & enchants", "mats", "BoEs", "officers", "vanity"]


@pytest.fixture
def clock():
    # Loop yields instead of real sleeps: the bank paces its sends on this clock.
    return FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc), yields=20)


def kind(cmd):
    return next(k for k, cls in protocol.COMMANDS.items() if type(cmd) is cls)


class Native(RealmApp):
    """The small app, with a bridge that answers as mod-leeroy: one ack per command (acks[kind],
    one error per send, "" for ok) and, for an accepted bank_stock, its realm_job just after
    (jobs[kind], one reason per job, "" for done with the stack's count)."""

    def __init__(self, root, store, clock):
        super().__init__(root, store, clock)
        self.acks, self.jobs, self.at = {}, {}, []
        self.held = None  # (kind, future): a send of that kind waits for the future
        self.after = None  # called with each command once its ack is decided
        self.realm = None

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id is True
        self.sent.append(cmd)
        self.at.append(self.clock.now())
        name = kind(cmd)
        if self.held is not None and name == self.held[0]:
            await self.held[1]
        error = self.acks[name].pop(0) if self.acks.get(name) else ""
        if name == "bank_stock" and not error:
            reason = self.jobs[name].pop(0) if self.jobs.get(name) else ""
            job = protocol.RealmJob(int(self.clock.now() * 1000), cmd.id, name, "failed" if reason else "done",
                                    reason, cmd.officer_guid, 0 if reason else cmd.count)
            asyncio.get_running_loop().call_soon(self.realm.on_job, job)
        if self.after is not None:
            self.after(cmd)
        return Ack(0, cmd.id, not error, error)


def seed(store):
    seed_test_cast(store)
    save_persona(store, make_persona(DKP, "Dunna", officer_title="Bank and DKP"), source="seed",
                 ts=1001.0, race="Human", class_name="Warrior")


def native(tmp_path, store, clock):
    enabled(tmp_path, True)
    app = Native(tmp_path, store, clock)
    app.realm = attach_realm(app)
    return app


async def live(app):
    await app.connection(True)
    await app.event(Hello(0, "test", "1", 21, app.human_guid))


def approve(app, step, payload):
    path = steps.write_plan(app.data_dir, step, payload, local=app.clock.local())
    steps.approve(app.data_dir, path, now=app.clock.now())
    return json.loads(path.read_text(encoding="utf-8"))["sha256"]


def applied(store, step, sha):
    row = store.conn.execute("SELECT applied,result FROM realm_step WHERE name=? AND sha256=?",
                             (step, sha)).fetchone()
    return None if row is None or row[0] is None else json.loads(row[1])


async def drive(app, step, sha, *, looks=6):
    """Steps' looks, a minute apart with the clock running between them, until the step is applied."""
    part = app.realm.part("steps")
    for _ in range(looks):
        await part.tick()
        result = applied(app.store, step, sha)
        if result is not None:
            return result
        await app.clock.advance(60, step=0.25)
    return None


def ledger(store):
    return [tuple(row) for row in store.conn.execute(
        "SELECT owner,ref,kind,expects_job,state,reason,value FROM realm_direct ORDER BY rowid")]


# -- the plans --------------------------------------------------------------------------------

def test_bank_plan_six_tabs_and_gold():
    plan = bank.plan_bank(DKP, prices=CORE)
    assert plan == json.loads(json.dumps(plan))  # what the plan file reads back as
    assert plan["officer"] == DKP
    assert [(t["tab"], t["name"]) for t in plan["tabs"]] == list(enumerate(NAMES))
    assert [t["price"] for t in plan["tabs"]] == [100, 250, 500, 1_000, 2_500, 5_000]
    assert plan["money"] == 50_000 + 9_350
    # Native takes a tab name of 1-16 bytes and an icon of at most 32.
    assert all(1 <= len(t["name"].encode()) <= 16 and len(t["icon"].encode()) <= 32 for t in plan["tabs"])
    # The rights he sets in the guild control panel (H5b), listed for him; none is ever sent.
    officers, members = plan["rights"]
    assert officers["ranks"].startswith("1") and members["ranks"].startswith("2")
    assert officers["view"] == officers["deposit"] == NAMES
    assert officers["withdraw_slots"] == members["withdraw_slots"] == ["mats", "BoEs"]
    assert members["view"] == members["deposit"] == ["mats"]
    # What is left after an unknown outcome: no money, the tabs from the first one still to buy.
    rest = bank.plan_bank(DKP, prices=CORE, money=False, first_tab=2)
    assert rest["money"] == 0 and [t["tab"] for t in rest["tabs"]] == [2, 3, 4, 5]
    assert bank.plan_bank(DKP, prices=CORE, first_tab=4)["money"] == 50_000 + 2_500 + 5_000
    for bad in ({"officer_guid": 0}, {"officer_guid": DKP, "first_tab": 6},
                {"officer_guid": DKP, "prices": CORE[:5]}):
        with pytest.raises(steps.StepError):
            bank.plan_bank(**{"prices": CORE, **bad})


def test_tab_price_from_conf(tmp_path):
    conf = tmp_path / "worldserver.conf"
    conf.write_text('LoginDatabaseInfo = "127.0.0.1;3306;acore;hunter2;acore_auth"\n'
                    "Guild.BankTabCost0 = 2000000\n"
                    "# Guild.BankTabCost1 = 1\n"
                    'Guild.BankTabCost3 = "12345678"\n', encoding="utf-8")
    # A key the conf leaves out is the core's compiled-in default, as native reads it.
    prices = bank.tab_prices(conf)
    assert prices == (2_000_000, 2_500_000, 5_000_000, 12_345_678, 25_000_000, 50_000_000)
    plan = bank.plan_bank(DKP, prices=prices)
    assert [t["price"] for t in plan["tabs"]] == [200, 250, 500, 1_235, 2_500, 5_000]  # whole gold, up
    assert plan["money"] == 50_000 + 200 + 250 + 500 + 1_235 + 2_500 + 5_000
    # A cost that is not a number, or a total one bank_money cannot carry, is a plan error.
    conf.write_text("Guild.BankTabCost2 = lots\n", encoding="utf-8")
    with pytest.raises(steps.StepError, match="BankTabCost2"):
        bank.tab_prices(conf)
    conf.write_text("Guild.BankTabCost5 = 200000000\n", encoding="utf-8")
    with pytest.raises(steps.StepError, match="60000"):
        bank.plan_bank(DKP, prices=bank.tab_prices(conf))
    assert bank.plan_bank(DKP, prices=bank.tab_prices(conf), money=False)["money"] == 0
    with pytest.raises(steps.StepError) as missing:
        bank.tab_prices(tmp_path / "absent.conf")
    assert str(tmp_path) not in str(missing.value)


def test_bank_plan_refuses_unknown_items(tmp_path):
    plan = bank.plan_bank_stock(bank.ITEMS_DIST, DKP)
    assert plan == json.loads(json.dumps(plan)) and plan["officer"] == DKP
    stacks = plan["stacks"]
    # About twenty stacks a stocked tab; the officers' tab is theirs to fill.
    assert Counter(tab for tab, _, _ in stacks) == {0: 20, 1: 20, 2: 20, 3: 20, 5: 20}
    limits = bank.load_items(bank.ITEMS_DIST)
    assert all(1 <= count <= limits[item] for _, item, count in stacks)
    assert stacks[0] == [0, 43015, 10]  # the probe: one stack of Fish Feasts
    # A stock id the items file has no bank row for is a plan error that names it.
    edited = tmp_path / "leeroy_items.csv"
    dist = bank.ITEMS_DIST.read_text(encoding="utf-8")
    edited.write_text(dist.replace("40133,bank,40\n", "40133,gift,40\n"), encoding="utf-8")
    with pytest.raises(steps.StepError, match="40133"):
        bank.plan_bank_stock(edited, DKP)
    # A file native would refuse whole is refused here too, by its line, never its path.
    for text, line in (("item_id,use,max_count\n43015,bank,1001\n", "line 2"),
                       ("item_id,use\n43015,bank,10\n", "line 1"),
                       ("item_id,use,max_count\n43015,bank,10\n43015,bank,20\n", "line 3")):
        edited.write_text(text, encoding="utf-8")
        with pytest.raises(steps.StepError, match=line) as refused:
            bank.plan_bank_stock(edited, DKP)
        assert str(tmp_path) not in str(refused.value)


# -- the bank step ----------------------------------------------------------------------------

def test_bank_apply_order(tmp_path, store, clock):
    seed(store)
    officer, name = bank.dkp_officer(store.conn)
    assert (officer, name) == (DKP, "Dunna")
    app = native(tmp_path, store, clock)

    async def go():
        await live(app)
        sha = approve(app, "bank", bank.plan_bank(officer, prices=CORE))
        result = await drive(app, "bank", sha)
        assert result == {"state": "done", "done": 7, "items": 7, "at": None}
        # The money first, then tabs 0..5 by the DKP officer, and no other command at all.
        assert [kind(c) for c in app.sent] == ["bank_money"] + ["bank_tab_buy"] * 6
        assert app.sent[0].amount == 59_350
        assert [(c.officer_guid, c.tab, c.name) for c in app.sent[1:]] == [
            (DKP, tab, tab_name) for tab, tab_name in enumerate(NAMES)]
        assert all(later - earlier >= 1 for earlier, later in zip(app.at, app.at[1:]))  # paced
        # Each through the direct ledger, once, under the step's own refs; neither emits a job.
        assert ledger(store) == [("step", f"step:bank:{sha}:{i}", "bank_money" if i == 0 else "bank_tab_buy",
                                  0, "acked", None, None) for i in range(7)]
        # Applied for good: later looks, and a new mind, send nothing.
        await drive(app, "bank", sha, looks=2)
        await app.clock.advance(120, step=0.25)
        assert len(app.sent) == 7
        await app.realm.close()
    run(go())


def test_bank_money_never_twice_across_restart(tmp_path, store, clock):
    """The money is on the wire when the mind dies. The next mind finds it unknown: it never
    credits again, buys no tab on a credit nobody saw, and leaves the step to the controller."""
    seed(store)
    first = native(tmp_path, store, clock)

    async def go():
        first.held = ("bank_money", asyncio.get_running_loop().create_future())
        await live(first)
        sha = approve(first, "bank", bank.plan_bank(DKP, prices=CORE))
        ref = f"step:bank:{sha}:0"
        await first.realm.part("steps").tick()
        await clock.advance(5, step=0.25)
        assert [kind(c) for c in first.sent] == ["bank_money"]
        assert first.realm.part("direct").state("step", ref) == ("dispatched", None, None)
        # The mind dies here, with no answer. The next one recovers the ledger before it sends.
        second = native(tmp_path, store, clock)
        assert second.realm.part("direct").state("step", ref) == ("unknown", "recovered_dispatched", None)
        await live(second)
        result = await drive(second, "bank", sha)
        assert result == {"state": "needs_controller", "done": 0, "items": 7,
                          "at": {"index": 0, "kind": "bank_money", "state": "unknown",
                                 "reason": "recovered_dispatched"}}
        assert second.sent == []
        await drive(second, "bank", sha, looks=3)
        assert second.sent == []
        # The first mind's late answer settles nothing, and it sends nothing more either.
        first.held[1].set_result(None)
        await clock.advance(5, step=0.25)
        assert second.realm.part("direct").state("step", ref)[0] == "unknown"
        assert len(first.sent) == 1 and second.sent == []
        await second.realm.close()
        await first.realm.close()
    run(go())


def test_outcome_unknown_refusal_never_resent(tmp_path, store, clock):
    """Native refuses with bank_outcome_unknown when it cannot prove what it did: the direct
    ledger frees that ref, but the bank never asks again. A refusal from before anything moved
    is asked again at the step's next look, once."""
    seed(store)
    app = native(tmp_path, store, clock)
    app.acks["bank_money"] = ["bank_outcome_unknown"]

    async def go():
        await live(app)
        sha = approve(app, "bank", bank.plan_bank(DKP, prices=CORE))
        result = await drive(app, "bank", sha)
        assert result["state"] == "needs_controller" and result["at"] == {
            "index": 0, "kind": "bank_money", "state": "refused", "reason": "bank_outcome_unknown"}
        assert app.realm.part("direct").state("step", f"step:bank:{sha}:0") == (
            "refused", "bank_outcome_unknown", None)
        assert [kind(c) for c in app.sent] == ["bank_money"]
        # The controller's fresh plan for what is left. Tab 2's officer was away once.
        app.acks["bank_tab_buy"] = ["", "", "not_officer"]
        rest = approve(app, "bank", bank.plan_bank(DKP, prices=CORE, money=False))
        result = await drive(app, "bank", rest)
        assert result == {"state": "done", "done": 6, "items": 6, "at": None}
        assert [c.tab for c in app.sent[1:]] == [0, 1, 2, 2, 3, 4, 5]
        assert [(row[1], row[4], row[5]) for row in ledger(store) if row[1].endswith(":3")] == [
            (f"step:bank:{rest}:3", "refused", "not_officer"), (f"step:bank:{rest}:3", "acked", None)]
        await app.realm.close()
    run(go())


def test_a_plan_native_would_refuse_fails_as_the_step_and_sends_nothing(tmp_path, store, clock):
    """The design's own tab name, "gems and enchants", is 17 bytes; native takes 16. A plan with
    it (approved after an edit) is the step's error at each look, never a send."""
    seed(store)
    app = native(tmp_path, store, clock)
    plan = bank.plan_bank(DKP, prices=CORE)
    plan["tabs"][1]["name"] = "gems and enchants"

    async def go():
        await live(app)
        sha = approve(app, "bank", plan)
        assert await drive(app, "bank", sha, looks=2) is None
        row = store.conn.execute("SELECT applied,result FROM realm_step WHERE sha256=?", (sha,)).fetchone()
        assert row[0] is None and json.loads(row[1]) == {"attempts": 2, "error": "ValueError"}
        assert app.sent == [] and ledger(store) == []
        await app.realm.close()
    run(go())


# -- the stock step ---------------------------------------------------------------------------

def test_stock_waits_for_rights(tmp_path, store, clock, caplog):
    """The probe comes back no_rights (native has destroyed the stack again): nothing more is
    sent, and the step's result says why."""
    seed(store)
    app = native(tmp_path, store, clock)
    app.jobs["bank_stock"] = ["no_rights"]

    async def go():
        await live(app)
        sha = approve(app, "bank_stock", bank.plan_bank_stock(bank.ITEMS_DIST, DKP))
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.bank"):
            result = await drive(app, "bank_stock", sha)
        assert result == {"state": "no_rights", "done": 0, "items": 100,
                          "at": {"index": 0, "kind": "bank_stock", "state": "refused", "reason": "no_rights"}}
        assert [(kind(c), c.officer_guid, c.tab, c.item_id, c.count) for c in app.sent] == [
            ("bank_stock", DKP, 0, 43015, 10)]
        assert "realm_bank_no_rights" in caplog.text
        assert ledger(store) == [("step", f"step:bank_stock:{sha}:0", "bank_stock", 1, "refused", "no_rights", None)]
        await drive(app, "bank_stock", sha, looks=2)
        assert len(app.sent) == 1 and bank.BankLedger(store, clock).balance(43015) == 0
        await app.realm.close()
    run(go())


def test_stock_after_rights(tmp_path, store, clock):
    """The probe is stocked: the rest follow one at a time, each after its own job, at most two
    a second. A run cut short resumes where it stopped, and the ledger records each stack once."""
    seed(store)
    app = native(tmp_path, store, clock)
    stacks = [[0, 43015, 10], [0, 34753, 10], [1, 40111, 20], [2, 34054, 20], [5, 8490, 1]]
    payload = {"officer": DKP, "tabs": NAMES, "stacks": stacks}

    def cut(cmd):  # His logout, say, just after the second stack: the run is cancelled.
        if len(app.sent) == 2:
            asyncio.get_running_loop().call_soon(app.realm._invalidate)
    app.after = cut

    async def go():
        await live(app)
        sha = approve(app, "bank_stock", payload)
        result = await drive(app, "bank_stock", sha)
        assert result == {"state": "done", "done": 5, "items": 5, "at": None}
        assert [[c.tab, c.item_id, c.count] for c in app.sent] == stacks  # each stack once, in order
        assert {c.officer_guid for c in app.sent} == {DKP}
        assert all(later - earlier >= 0.5 for earlier, later in zip(app.at, app.at[1:]))
        assert app.at[2] - app.at[1] >= 30  # the cut run was resumed at a later look
        assert [row[2:6] for row in ledger(store)] == [("bank_stock", 1, "acked", None)] * 5
        assert [int(row[6]) for row in ledger(store)] == [10, 10, 20, 20, 1]  # each job's count
        book = bank.BankLedger(store, clock)
        assert [book.balance(item) for _, item, _ in stacks] == [10, 10, 20, 20, 1]
        assert {tuple(r) for r in store.conn.execute("SELECT dir,reason,guid FROM realm_bank")} == {
            ("in", "stock", DKP)}
        # Asked again (a second mind's look), the step records nothing twice and sends nothing.
        part = app.realm.part("bank")
        again = await part.apply_bank_stock(payload, steps.Progress(app.realm.part("steps"), "bank_stock", sha))
        assert again == result and len(app.sent) == 5
        assert store.conn.execute("SELECT count(*) FROM realm_bank").fetchone()[0] == 5
        await app.realm.close()
    run(go())


# -- the ledger, the part and the command -----------------------------------------------------

def test_ledger_balance_and_low(store, clock):
    book = bank.BankLedger(store.conn, clock)
    book.record("in", "stock", DKP, 43015, 10)
    book.record("out", "consumables", DKP, 43015, 7)
    assert book.balance(43015) == 3 and book.low(43015, 5) and not book.low(43015, 3)
    assert book.balance(34753) == 0
    assert [tuple(r) for r in store.conn.execute(
        "SELECT ts,dir,reason,guid,item_id,count,money FROM realm_bank ORDER BY id")] == [
        (clock.now(), "in", "stock", DKP, 43015, 10, 0), (clock.now(), "out", "consumables", DKP, 43015, 7, 0)]
    assert set(bank.REASONS) == {"stock", "gathering", "donation", "prize", "craft", "gift", "consumables"}
    for bad in ({"dir": "sideways"}, {"reason": "theft"}, {"count": -1}, {"count": 0},
                {"item_id": 0}, {"guid": True}):
        with pytest.raises(ValueError):
            book.record(**{"dir": "in", "reason": "gift", "guid": DKP, "item_id": 43015, "count": 1, **bad})
    # Through the mind's Store it joins the caller's unit, and rolls back with it.
    shared = bank.BankLedger(store, clock)
    with pytest.raises(LookupError):
        with store.transaction():
            shared.record("in", "donation", 101, 34054, 5)
            raise LookupError("synthetic")
    assert shared.balance(34054) == 0 and book.balance(43015) == 3


def test_attach_registers_both_steps_without_io(tmp_path, store, clock):
    app = RealmApp(tmp_path, store, clock)  # disabled, as the Realm ships
    before = store.conn.total_changes
    realm = attach_realm(app)
    part = realm.part("bank")
    assert type(part) is bank.Bank and [p for p, _ in realm._parts].count(part) == 1
    assert {"bank", "bank_stock"} <= set(realm.part("steps")._applies)
    assert store.conn.total_changes == before and app.sent == [] and realm._children == set()
    run(realm.close())


def test_plan_cli_writes_only_its_plan(tmp_path, store, clock, capsys):
    seed(store)
    conf = tmp_path / "worldserver.conf"
    conf.write_text('CharacterDatabaseInfo = "127.0.0.1;3306;acore;hunter2;acore_characters"\n', encoding="utf-8")
    db = tmp_path / "mind.db"
    before = db.stat().st_mtime_ns, db.stat().st_size
    assert cli.realm_main(["plan", "bank", "--conf", str(conf), "--data-dir", str(tmp_path)], clock=clock) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["payload"] == bank.plan_bank(DKP, prices=CORE) and out["officer_name"] == "Dunna"
    doc = steps.read_plan(steps.plans_dir(tmp_path) / out["plan"], name="bank")
    assert doc["sha256"] == out["sha256"] == steps.digest(doc)
    items = tmp_path / "leeroy_items.csv"
    items.write_text(bank.ITEMS_DIST.read_text(encoding="utf-8"), encoding="utf-8")
    argv = ["plan", "bank-stock", "--items", str(items), "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["payload"] == bank.plan_bank_stock(items, DKP)
    assert steps.read_plan(steps.plans_dir(tmp_path) / out["plan"], name="bank_stock")["sha256"] == out["sha256"]
    # Two DKP title holders: refused until he names one; a name off the list is refused too.
    save_persona(store, make_persona(102, "Belra", officer_title="DKP"), source="seed", ts=1002.0,
                 race="Human", class_name="Warrior")
    assert cli.realm_main(argv, clock=clock) == 1
    assert "--officer" in capsys.readouterr().out
    assert cli.realm_main([*argv, "--officer", "101"], clock=clock) == 1
    capsys.readouterr()
    assert cli.realm_main([*argv, "--officer", "102"], clock=clock) == 0
    assert json.loads(capsys.readouterr().out)["payload"]["officer"] == 102
    # An argument it does not know is refused without echoing it; no output names a local path.
    assert cli.realm_main(["plan", "bank", "--frobnicate", "privatevalue", "--data-dir", str(tmp_path)],
                          clock=clock) == 1
    output = capsys.readouterr().out
    assert "privatevalue" not in output and str(tmp_path) not in output
    assert sorted(p.name.split("-")[0] for p in steps.plans_dir(tmp_path).iterdir()) == [
        "bank", "bank_stock", "bank_stock"]
    assert not steps.approved_dir(tmp_path).exists()
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
