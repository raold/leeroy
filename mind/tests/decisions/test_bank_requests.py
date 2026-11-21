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

def test_overrule_before_late_job_keeps_the_confirmed_outflow(store, clock):
    import asyncio
    from types import SimpleNamespace as NS
    from uuid import uuid4
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger
    row = settled(store, clock)
    ref = f"decision:{row['id']}:withdraw"
    ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="p", generation=1,
                        connection_epoch=1, guild=8, human=1)
    direct = DirectLedger(store, process_id="p", clock=clock)
    cmd = protocol.BankWithdraw(uuid4().hex, 101, 46377, 20, 0, 102)
    direct.prepare(cmd, owner="decision", ref=ref, context=ctx, expects_job=True)
    direct.before_send(cmd, context=ctx)
    ledger.overrule(store, row["id"], now=clock.now())
    direct.on_job(protocol.RealmJob(0, cmd.id, "bank_withdraw", "done", "", 101, 20))
    app = NS(store=store, clock=clock, realm=NS(part=lambda _: direct))
    rules = bank.bank_rules()
    for _ in range(2):
        asyncio.run(rules.tick(app, clock.now()))
    assert store.conn.execute("SELECT count(*) FROM realm_bank WHERE reason='request'").fetchone()[0] == 1
    assert ledger.get(store.conn, row["id"])["status"] == "overruled"
    assert direct.state("decision", ref.replace("withdraw", "mail")) is None


def test_daily_cap_uses_midnight_offset_on_both_dst_transitions(store, clock, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger
    from uuid import uuid4
    ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="dst", generation=1,
                        connection_epoch=1, guild=8, human=1)
    direct = DirectLedger(store, process_id="dst", clock=clock)
    def ts(s):
        return datetime.fromisoformat(s).timestamp()
    def eastern(s):
        summer = ts("2026-03-08T07:00+00:00") <= s < ts("2026-11-01T06:00+00:00")
        return datetime.fromtimestamp(s, timezone(timedelta(hours=-4 if summer else -5)))
    monkeypatch.setattr(bank, "local_at", eastern)
    for when, check, expected in [("2026-03-07T23:30-05:00","2026-03-08T03:30-04:00",0),
                                  ("2026-11-01T00:30-04:00","2026-11-01T02:30-05:00",1)]:
        clock._t = ts(when)
        cmd = protocol.BankWithdraw(uuid4().hex, 101, 46377, 20, 0, 102)
        direct.prepare(cmd, owner="decision", ref="dst:"+cmd.id, context=ctx, expects_job=True)
        direct.before_send(cmd, context=ctx)
        assert bank.given_today(store.conn, ts(check)) == expected


def test_mail_hold_at_scheduled_realm_transport(store, clock):
    import asyncio
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from uuid import uuid4
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger
    from leeroy_mind.realm.ports import LivePorts, RealmDisabled
    from leeroy_mind.realm.service import Realm
    import pytest

    async def go():
        ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="p", generation=1,
                            connection_epoch=1, guild=8, human=1)
        d = DirectLedger(store, process_id="p", clock=clock)
        rules = bank.bank_rules()
        app = NS(clock=clock, store=store, human_guid=1, engine=NS(raid=None),
                 bridge=NS(send=AsyncMock()), decisions=NS(rules_for=lambda _: rules))
        realm = app.realm = NS(enabled=True, _stopping=False, parts={"direct":d}, part=lambda _: d,
                              queue=NS(owns_command=lambda cmd:False), app=app, action_context=lambda:ctx)
        realm.ports = LivePorts(app, on_send=lambda cmd: Realm.on_send(realm, cmd))
        realm.ports.set_enabled(True)
        # call_soon runs before the scheduled LivePorts settlement, after preparation.
        asyncio.get_running_loop().call_soon(setattr, app.engine, "raid", NS(phase=lambda _: "combat"))
        app.engine._in_boss_fight = lambda _: True
        mail = protocol.MailSend(uuid4().hex, 101, "request", items=(protocol.MailItem(46377,20,"bags"),), to_guid=102)
        with pytest.raises(RealmDisabled):
            await rules.send(app, mail, "decision:1:mail", lambda: True)
        app.bridge.send.assert_not_called()
        assert d.state("decision", "decision:1:mail")[0] == "abandoned"
    asyncio.run(go())


def test_only_equippable_proven_bind_on_equip_upgrades_are_requested(store, guild, clock):
    from dataclasses import replace
    from leeroy_mind.guild.armory import parse_items
    # The same inventory upgrade with each bind type, plus a high-level non-equippable token.
    item = parse_items([["60001","Invented item","4","277","2","4","0","-1",*(["0"]*10),"2"]])[60001]
    bank_read = {"tabs":[{"tab":3,"slots":[(0,60001,1)]}], "equipped":{g:{} for g in guild}}
    for info, authorized in [(item, True), (replace(item,bonding=None),False), (replace(item,bonding=0),False),
                             (replace(item,bonding=1),False), (replace(item,item_class=15,inventory_type=0),False)]:
        bank_read["items"] = {60001:info}
        due = bank.requests_due(store.conn, bank_read, store.cast(), guild, now=clock.now())
        assert bool(due) == authorized


def test_lineup_removed_after_withdrawal_preparation_stops_dispatch(store, guild, clock):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    import pytest
    from leeroy_mind.decisions.config import DecisionsConfig
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger
    from leeroy_mind.realm.ports import LivePorts, RealmDisabled
    from leeroy_mind.realm.service import Realm
    from .test_lineup import event

    async def go():
        store.set_meta("guild_id", "8")
        date = clock.now()
        event(store, date+86400)
        line = ledger.propose(store,kind="lineup",subject="raid:7",value={"members":[102]},positions=[],now=date)
        from leeroy_mind.discord import store as discord
        msg = discord.post(store,channel="raid-planning",author=101,text="Posted lineup",origin="mind",now=date)
        ledger.settle(store,line,settled_by=101,message=msg,now=date)
        row = settled(store,clock)
        cast = {g:replace(m, rank=1 if g == 101 else 3) for g,m in store.cast().items()}
        guild[101] = replace(guild[101],play=replace(guild[101].play,officer_title="Bank"))
        rules = bank.bank_rules()
        rules.features = {"member_bank_mail"}
        app = NS(store=store,clock=clock,human_guid=1,world=NS(members=cast),personas=guild,engine=NS(raid=None),
                 bridge=NS(send=AsyncMock()), decisions=NS(rules_for=lambda _:rules,config=DecisionsConfig()))
        app.guild = NS(armory=NS(bank=lambda _:snapshot((0,46377,20),(1,46377,20))),
                       raid_night=NS(active=False),offline=NS(active=False))
        ctx = ActionContext(enabled=True,connected=True,presence_known=True,process_id="p",
                            generation=1,connection_epoch=1,guild=8,human=1)
        direct = DirectLedger(store,process_id="p",clock=clock)
        realm = app.realm = NS(enabled=True,_stopping=False,parts={"direct":direct},part=lambda _:direct,
                              queue=NS(owns_command=lambda cmd:False),app=app,action_context=lambda:ctx)
        realm.ports = LivePorts(app,on_send=lambda cmd: Realm.on_send(realm,cmd))
        realm.ports.set_enabled(True)
        prepare = direct.prepare
        def prepared(*args, **kwargs):
            accepted = prepare(*args, **kwargs)
            assert accepted
            asyncio.get_running_loop().call_soon(lambda: ledger.overrule(store,line,now=date))
            return accepted
        direct.prepare = prepared
        with pytest.raises(RealmDisabled):
            await rules.advance(app,row)
        app.bridge.send.assert_not_called()
        assert direct.state("decision", f"decision:{row['id']}:withdraw")[0] == "abandoned"
    asyncio.run(go())


def test_backward_clock_step_cannot_reopen_today_allowance(store, clock):
    from datetime import timedelta
    from uuid import uuid4
    from leeroy_mind.realm.actions import ActionContext
    from leeroy_mind.realm.direct import DirectLedger

    noon = clock.local().replace(hour=12, minute=0, second=0)
    clock._t = noon.timestamp()
    ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="clock", generation=1,
                        connection_epoch=1, guild=8, human=1)
    direct = DirectLedger(store, process_id="clock", clock=clock)
    cmd = protocol.BankWithdraw(uuid4().hex, 101, 46377, 20, 0, 102)
    assert direct.prepare(cmd, owner="decision", ref="decision:1:withdraw", context=ctx, expects_job=True)
    direct.before_send(cmd, context=ctx)
    direct.on_job(protocol.RealmJob(0, cmd.id, "bank_withdraw", "done", "", 101, 20))
    clock._t = (noon - timedelta(minutes=1)).timestamp()
    given = bank.given_today(store.conn, clock.now())
    assert bank.approve(request(), snapshot((0, 46377, 20), (1, 46377, 20)), given_today=given, cap=1) == (False, "cap")
