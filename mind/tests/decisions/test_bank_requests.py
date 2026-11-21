from leeroy_mind.decisions import bank, ledger
from leeroy_mind.realm import protocol


def snapshot(*slots):
    return {"tabs": [{"tab": 0, "slots": list(slots)}]}


def request(**changes):
    return dict({"guid": 102, "item": 46377, "tab": 0, "count": 20, "kind": "raid_flasks",
                 "member": True, "human": 1}, **changes)


def test_never_the_last_stack_or_an_empty_tab():
    check = lambda *slots: bank.approve(request(), snapshot(*slots), given_today=0, cap=10)
    assert check((0, 46377, 20)) == (False, "empty_tab")
    assert check((0, 46377, 20), (1, 34753, 10)) == (False, "last_stack")
    assert check((0, 46377, 20), (1, 46377, 20)) == (True, "approved")
    assert check((0, 34753, 10)) == (False, "not_in_bank")


def test_never_him_never_outside_the_guild():
    read = snapshot((0, 46377, 20), (1, 46377, 20))
    assert bank.approve(request(guid=1), read, given_today=0, cap=10) == (False, "him")
    assert bank.approve(request(member=False), read, given_today=0, cap=10) == (False, "non_member")


def test_no_fresh_bank_read_approves_nothing():
    assert bank.approve(request(), None, given_today=0, cap=10) == (False, "no_fresh_bank")


def test_daily_cap_survives_restart(store, clock):
    from leeroy_mind.realm.direct import DirectLedger
    from leeroy_mind.realm.actions import ActionContext
    from uuid import uuid4
    cmd = protocol.BankWithdraw(uuid4().hex, 101, 46377, 20, 0)
    ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="first", generation=1,
                        connection_epoch=1, guild=8, human=1)
    direct = DirectLedger(store, process_id="first", clock=clock)
    assert direct.prepare(cmd, owner="decision", ref="decision:5:withdraw", context=ctx, expects_job=True)
    direct.before_send(cmd, context=ctx)
    DirectLedger(store, process_id="second", clock=clock).recover(process_id="second")
    count = bank.given_today(store.conn, clock.now())
    assert count == 1
    assert bank.approve(request(), snapshot(), given_today=count, cap=1) == (False, "cap")


def test_unknown_withdraw_is_never_resent(store, clock):
    row = settled(store, clock)
    step = bank.next_step(row, ("unknown", "transport_unknown", None), None)
    assert step == "unknown"


def settled(store, clock):
    ident = ledger.propose(store, kind="bank", subject="bank:102:1", value=request(), positions=[], now=clock.now())
    ledger.settle(store, ident, settled_by=101, now=clock.now())
    return ledger.get(store.conn, ident)


def test_mail_after_withdraw_once(store, clock):
    row = settled(store, clock)
    assert bank.next_step(row, None, None) == "withdraw"
    assert bank.next_step(row, ("acked", None, None), None) == "wait"
    assert bank.next_step(row, ("acked", None, 20), None) == "mail"
    assert bank.next_step(row, ("acked", None, 20), ("dispatched", None, None)) == "wait"
    assert bank.next_step(row, ("acked", None, 20), ("acked", None, 55)) == "done"
