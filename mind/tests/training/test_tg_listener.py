import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from leeroy_mind.protocol import Chat, DcStatus
from leeroy_mind.training import store
from leeroy_mind.training.cards import Card, Directive
from leeroy_mind.training.checks import CheckResult, Failure
from leeroy_mind.training.ledger import PullContext, PullRecord
from leeroy_mind.training.listener import TrainingListener, held_at_boss
from leeroy_mind.training.protocol import PullPulse

from .test_tg_gate import _run_async, boss, run_for
from .test_tg_ledger import header, member


def setup(fake, *, acting=True, human=False):
    gate = SimpleNamespace(on_start=AsyncMock(), on_muster=AsyncMock(), on_boss=AsyncMock(),
                           on_end=AsyncMock(), invalidate=Mock(), recording=lambda: True)
    ledger = SimpleNamespace(set_run=Mock(), set_in_play=Mock(), mark_unscoreable=Mock())
    applier = SimpleNamespace(acting=lambda now: acting, hold=AsyncMock(return_value=True),
        release=AsyncMock(return_value=True), resume_held=AsyncMock(return_value=True),
        standing_conflicts=Mock(return_value=False), apply=AsyncMock(return_value=True),
        apply_standing=AsyncMock(return_value=0), revert=AsyncMock(return_value=0),
        end_run=AsyncMock(return_value=0), cleanup_pending=Mock(return_value=False))
    listener = TrainingListener(fake, ledger, gate, applier, lust_provider=AsyncMock(),
                                members_provider=lambda run: {})
    return listener, run_for(fake, offline=not human)


def status(fake, *, state="resting", reason="", name="Festergut", entry=36626, paused=False):
    return DcStatus(int(fake.clock.now() * 1000), 101, True, paused,
                    f"STATUS\t631\t{entry}\t{name}\t0\t0\t{state}\t{reason}")


def card(fake, *, context="631:4:0", value=35, status="champion", previous=None, version=1):
    value = Card(None, context, version, status, "progression", (Directive("lust_at", value),),
                 prev_champion=previous)
    ident = store.put_card(fake.conn, value.to_row())
    return replace(value, id=ident)


def trial_arm(fake, card, lever="lust_at", scope=None):
    trial = store.open_trial(fake.conn, scope or card.context, lever, "raid",
                             opened=fake.clock.now(), arms=({"card": card.id, "champion": False},))
    return fake.conn.execute("SELECT id FROM tg_arm WHERE trial=?", (trial,)).fetchone()[0]


async def record(fake, run, *, outcome="wiped", change="", complete=True, scoreable=True, basis="deaths_time"):
    h = header(fake, boss=4, instance_id=100, outcome=outcome, duration=1000)
    h = replace(h, id=f"{h.start_ms}-100-4")
    rec = PullRecord(h, (member(h),), (), {}, (), complete)
    ctx = PullContext("his" if run.human_in_raid else "offline", 101, run.session.raid_id,
                      None, None, change, {})
    store.put_pull(fake.conn, rec, ctx, {101: "ranged"}, received=fake.clock.now(), scoreable=scoreable)
    if complete and scoreable:
        store.set_score(fake.conn, h.id, -100 if outcome == "wiped" else 0, basis=basis)
    await fake.clock.advance(2, step=2)
    return rec, ctx


def check(kind="spread"):
    return CheckResult((Failure(kind, kind, 101, 5, 0, 0),), ("unknown",), 1)


@_run_async
async def test_observation_month_applies_nothing(fake):
    listener, run = setup(fake, acting=False)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    await listener.on_boss(boss(fake, 4))
    await listener.on_boss(boss(fake, 4, "wiped"))
    rec, ctx = await record(fake, run)
    await listener.after_pull(rec, ctx, check())
    await listener.on_end("ended", run=run)
    listener.applier.apply.assert_not_awaited()
    listener.applier.hold.assert_not_awaited()
    listener.applier.resume_held.assert_not_awaited()
    assert listener.change_for("631:4:0") == ""
    assert fake.sent == []


@_run_async
async def test_card_at_resting_release_only_at_waiting_pause(fake):
    listener, run = setup(fake)
    chosen = card(fake)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    listener.applier.resume_held.assert_not_awaited()
    assert listener.applier.apply.await_args.args[3] == chosen
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=chosen.id, arm_id=None, change="")
    for reason in ("holding position", "waiting for door", "door closed"):
        await listener.on_dc_status(status(fake, state="paused", reason=reason, paused=True))
    listener.applier.resume_held.assert_not_awaited()
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    listener.applier.resume_held.assert_awaited_once_with(101)
    listener.applier.release.assert_not_awaited()


@_run_async
async def test_waiting_pause_can_prepare_before_resting(fake):
    listener, run = setup(fake)
    card(fake)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    listener.applier.apply.assert_awaited_once()
    listener.applier.resume_held.assert_awaited_once()


@_run_async
async def test_failed_and_timed_out_apply_never_resume_or_claim_applied(fake):
    for slow in (False, True):
        listener, run = setup(fake)
        listener.APPLY_TIMEOUT = .001
        if slow:
            async def blocked(*args, **kwargs):
                await asyncio.Event().wait()
            listener.applier.apply.side_effect = blocked
        else:
            listener.applier.apply.return_value = False
        await listener.on_start(run)
        await listener.on_dc_status(status(fake))
        await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
        listener.applier.resume_held.assert_not_awaited()
        listener.ledger.mark_unscoreable.assert_called_with("631:4:0")
        assert listener.ledger.set_in_play.call_args.kwargs == {}
        assert not listener.lust_held(631, 4)


@_run_async
async def test_apply_ack_from_old_connection_never_sets_in_play(fake):
    listener, run = setup(fake)
    started, finish = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        started.set()
        await finish.wait()
        return True
    listener.applier.apply.side_effect = delayed
    await listener.on_start(run)
    task = asyncio.create_task(listener.on_dc_status(status(fake)))
    await started.wait()
    fake.connection_epoch = 1
    finish.set()
    await task
    listener.ledger.set_in_play.assert_not_called()
    listener.applier.resume_held.assert_not_awaited()


@_run_async
async def test_his_raid_uses_previous_after_two_wipes(fake):
    listener, run = setup(fake, human=True)
    previous = card(fake, status="retired", value=0)
    champion = card(fake, previous=previous.id, version=2)
    for _ in range(3):
        await record(fake, run, outcome="killed")
    await listener.on_start(run)
    for _ in range(2):
        assert listener.card_for("631:4:0", run)[0].id == champion.id
        await listener.on_dc_status(status(fake))
        await listener.on_boss(boss(fake, 4))
        await listener.on_boss(boss(fake, 4, "wiped"))
        rec, ctx = await record(fake, run)
        await listener.after_pull(rec, ctx, check())
        assert listener.change_for("631:4:0") == ""
    assert not listener._farm("631:4:0")
    assert listener.card_for("631:4:0", run)[0].id == previous.id


@_run_async
async def test_old_plan_only_from_human_in_his_raid_and_queues_speech(fake):
    listener, run = setup(fake, human=True)
    previous = card(fake, status="retired", value=0)
    champion = card(fake, previous=previous.id, version=2)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    chat = Chat(int(fake.clock.now() * 1000), "raid", fake.human_guid, "", 0,
                "lets do the old plan", 631, 0)
    for foreign in (replace(chat, from_guid=102), replace(chat, channel="guild"),
                    replace(chat, map=649), replace(chat, via_bridge=True), replace(chat, to_guid=101)):
        await listener.on_human_chat(foreign)
    assert listener.card_for("631:4:0", run)[0].id == champion.id
    await listener.on_human_chat(chat)
    assert fake.said == []
    assert listener.card_for("631:4:0", run)[0].id == previous.id
    await listener.tick()
    assert fake.said[0][2] == "ok, old plan on Festergut"


@_run_async
async def test_progression_changes_replace_and_only_stored_qualifying_kill_drafts(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    rec, ctx = await record(fake, run)
    await listener.after_pull(rec, ctx, check())
    assert listener.change_for("631:4:0") == "ranged spread out more"
    rec, ctx = await record(fake, run)
    await listener.after_pull(rec, ctx, check("aggro"))
    assert listener.change_for("631:4:0") == "dps wait longer on the pull"
    await listener.on_dc_status(status(fake))
    await listener.on_boss(boss(fake, 4))
    await listener.on_boss(boss(fake, 4, "killed"))
    assert store.card_rows(fake.conn, "631:4:0", "candidate") == []
    rec, ctx = await record(fake, run, outcome="killed", change=listener.change_for("631:4:0"), scoreable=False)
    await listener.after_pull(rec, ctx, check())
    assert store.card_rows(fake.conn, "631:4:0", "candidate") == []
    rec, ctx = await record(fake, run, outcome="killed", change=listener.change_for("631:4:0"))
    await listener.after_pull(rec, ctx, check())
    await listener.after_pull(rec, ctx, check())
    rows = store.card_rows(fake.conn, "631:4:0", "candidate")
    assert len(rows) == 1
    assert Card.from_row(rows[0]).directives == (Directive("wait for attack time", 6),)
    assert Card.from_row(rows[0]).evidence == (rec.header.id,)


@_run_async
async def test_late_or_foreign_results_do_not_change_current_plan(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    old, oldctx = await record(fake, run)
    new, newctx = await record(fake, run)
    await listener.after_pull(new, newctx, check("aggro"))
    await listener.after_pull(old, oldctx, check())
    assert listener.change_for("631:4:0") == "dps wait longer on the pull"
    await listener.on_start(run_for(fake, 2))
    await listener.after_pull(new, newctx, check())
    assert listener.change_for("631:4:0") == ""


@_run_async
async def test_cleanup_barrier_retains_exact_run_until_confirmed(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    listener.applier.cleanup_pending.return_value = True
    assert await listener.on_before_end(run, "human_login") is False
    assert listener.retained_run(listener.run_key(run)) is run
    assert await listener.on_end("human_login", run=run) is False
    assert listener.run is run
    listener.applier.cleanup_pending.return_value = False
    assert await listener.on_before_end(run, "human_login") is True
    assert await listener.on_end("human_login", run=run) is True
    assert listener.run is None
    assert listener.retained_run(listener.run_key(run)) is None


@_run_async
async def test_cleanup_timeout_retains_barrier_for_retry(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    listener.CLEANUP_TIMEOUT = .001
    async def unresolved(*args):
        await asyncio.Event().wait()
    listener.applier.end_run.side_effect = unresolved
    assert await listener.on_before_end(run) is False
    assert listener.retained_run(listener.run_key(run)) is run
    listener.applier.end_run.side_effect = None
    assert await listener.on_before_end(run) is True


@_run_async
async def test_late_terminal_cleanup_never_targets_replacement(fake):
    listener, old = setup(fake)
    await listener.on_start(old)
    replacement = run_for(fake, 2)
    await listener.on_start(replacement)
    listener.applier.apply.reset_mock()
    await listener.on_dc_status(status(fake))
    listener.applier.apply.assert_not_awaited()
    assert await listener.on_end("ended", run=old)
    listener.applier.end_run.assert_awaited_with(listener.run_key(old))
    assert listener.run is replacement


@_run_async
async def test_standing_reapply_waits_fifteen_seconds_and_is_cancelled_by_replacement(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    listener.applier.apply_standing.assert_awaited_once_with(run.plan.members, run_key=listener.run_key(run))
    await listener.on_end("ended", run=run)
    listener.applier.apply_standing.reset_mock()
    await fake.clock.advance(14, step=14)
    await listener.tick()
    listener.applier.apply_standing.assert_not_awaited()
    await fake.clock.advance(1, step=1)
    await listener.tick()
    listener.applier.apply_standing.assert_awaited_once_with(tuple(run.plan.members))
    replacement = run_for(fake, 2)
    await listener.on_start(replacement)
    await listener.on_end("ended", run=replacement)
    await listener.on_start(run_for(fake, 3))
    listener.applier.apply_standing.reset_mock()
    await fake.clock.advance(15, step=15)
    await listener.tick()
    listener.applier.apply_standing.assert_not_awaited()


async def ready_lust(fake):
    listener, run = setup(fake)
    card(fake)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    engaged = boss(fake, 4)
    await listener.on_boss(engaged)
    return listener, run, engaged


@_run_async
async def test_lust_threshold_once_and_inline_pulses_do_not_wait_for_ack(fake):
    listener, run, engaged = await ready_lust(fake)
    for hp in (50, 40, 34, 30):
        await fake.clock.advance(1, step=1)
        ev = PullPulse(int(fake.clock.now() * 1000), f"{engaged.ts}-100-4", 631, 100, 4,
                       int(fake.clock.now() * 1000) - engaged.ts, hp, 2)
        await listener.on_pulse(ev)
    listener.lust_provider.assert_not_awaited()
    await listener.tick()
    await listener.tick()
    listener.lust_provider.assert_awaited_once_with(run)


@_run_async
async def test_lust_fallback_at_ten_seconds_and_foreign_pulse_cannot_cancel(fake):
    listener, run, engaged = await ready_lust(fake)
    await fake.clock.advance(9, step=9)
    ev = PullPulse(int(fake.clock.now() * 1000), f"{engaged.ts-1}-100-4", 631, 100, 4, 9000, 30, 2)
    await listener.on_pulse(ev)
    await listener.tick()
    listener.lust_provider.assert_not_awaited()
    await fake.clock.advance(1, step=1)
    await listener.tick()
    await listener.tick()
    listener.lust_provider.assert_awaited_once_with(run)


@_run_async
async def test_pulse_pins_recorder_start_after_separate_event_timestamp(fake):
    listener, run, engaged = await ready_lust(fake)
    await fake.clock.advance(5, step=5)
    ev = PullPulse(int(fake.clock.now() * 1000), f"{engaged.ts+1}-100-4", 631, 100, 4, 4999, 50, 2)
    await listener.on_pulse(ev)
    await listener.on_pulse(replace(ev, pull=f"{engaged.ts+2}-100-4", boss_hp=30))
    await listener.tick()
    listener.lust_provider.assert_not_awaited()
    await listener.on_pulse(replace(ev, boss_hp=30))
    await listener.tick()
    listener.lust_provider.assert_awaited_once_with(run)


@_run_async
async def test_lust_timer_and_queued_action_are_fenced_by_end_epoch_and_close(fake):
    for boundary in ("end", "epoch", "close"):
        listener, run, engaged = await ready_lust(fake)
        await listener.on_pulse(PullPulse(engaged.ts, f"{engaged.ts}-100-4", 631, 100, 4, 0, 30, 2))
        if boundary == "end":
            await listener.on_before_end(run)
        elif boundary == "epoch":
            fake.connection_epoch = getattr(fake, "connection_epoch", 0) + 1
        else:
            await listener.close()
        await fake.clock.advance(10, step=10)
        await listener.tick()
        listener.lust_provider.assert_not_awaited()
        with fake.conn:
            fake.conn.execute("DELETE FROM tg_card")


@_run_async
async def test_boss_layers_revert_after_each_pull(fake):
    listener, run = setup(fake)
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    await listener.on_boss(boss(fake, 4))
    listener.applier.revert.reset_mock()
    await listener.on_boss(boss(fake, 4, "wiped"))
    assert [(c.args, c.kwargs) for c in listener.applier.revert.await_args_list] == [
        ((listener.run_key(run), "arm"), {}),
        ((listener.run_key(run), "boss"), {"context": "631:4:0"})]


@_run_async
async def test_experimental_arm_is_not_used_in_his_raid_and_benched_after_wipe(fake):
    listener, run = setup(fake)
    champion = card(fake)
    arm = card(fake, status="trial", version=2, value=20)
    for _ in range(3):
        await record(fake, run, outcome="killed")
    arm_id = trial_arm(fake, arm)
    experimenter = SimpleNamespace(arm_for=Mock(return_value=(arm, arm_id)), arm_valid=Mock(return_value=True))
    listener.set_experimenter(experimenter)
    await listener.on_start(run)
    assert listener.card_for("631:4:0", replace(run, human_in_raid=True)) == (champion, None)
    await listener.on_dc_status(status(fake))
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=arm.id, arm_id=arm_id, change="")
    await listener.on_boss(boss(fake, 4))
    await listener.on_boss(boss(fake, 4, "wiped"))
    rec, ctx = await record(fake, run)
    await listener.after_pull(rec, ctx, check())
    assert not listener._farm("631:4:0")
    assert listener.change_for("631:4:0") == ""
    assert listener.card_for("631:4:0", run) == (champion, None)


@_run_async
async def test_farm_never_mixes_score_bases(fake):
    listener, run = setup(fake)
    await record(fake, run, outcome="killed", basis="deaths_time")
    await record(fake, run, outcome="killed", basis="deaths_time")
    await record(fake, run, outcome="killed", basis="mechanics_deaths_time")
    assert not listener._farm("631:4:0")
    await record(fake, run, outcome="killed", basis="deaths_time")
    assert listener._farm("631:4:0")


def test_held_at_boss_checks_both_status_fields(fake):
    assert held_at_boss(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    assert not held_at_boss(status(fake, state="resting", reason="waiting at Festergut", paused=True))
    assert not held_at_boss(status(fake, state="paused", reason="waiting at Festergut", paused=False))
    assert not held_at_boss(status(fake, state="paused", reason="holding position", paused=True))


def real_listener(fake):
    from .test_tg_applier import Fake as ApplierPorts
    listener, run = setup(fake)
    ports = ApplierPorts(SimpleNamespace(conn=fake.conn), fake.clock)
    ports.state = replace(ports.state, run_key=listener.run_key(run), instance_id=100)
    listener.ports, listener.applier = ports, ports.applier
    listener.members_provider = lambda run: {g: ports.state.members[g] for g in run.plan.members}
    return listener, run, ports


def plan_card(fake, *directives, context="631:4:0", status="champion", version=1):
    value = Card(None, context, version, status, "progression", tuple(directives))
    return replace(value, id=store.put_card(fake.conn, value.to_row()))


class JournalEffects:
    """Small command emulator; production Applier owns all receipts/layers."""
    def __init__(self):
        self.values, self.saved = {}, {}

    async def __call__(self, command):
        from leeroy_mind.guild.protocol import BotDo
        from leeroy_mind.training.protocol import DcSetting
        if isinstance(command, BotDo):
            for guid in command.guids:
                if command.action == "co" and command.param.startswith("+"):
                    key = (guid, command.param[1:])
                    self.saved.setdefault(key, self.values.get(key, False))
                    self.values[key] = True
                elif command.action == "restore":
                    keys = ([k for k in self.saved if k[0] == guid] if command.param == "*"
                            else [(guid, command.param)])
                    for key in keys:
                        self.values[key] = self.saved.pop(key, False)
        elif isinstance(command, DcSetting):
            key = ("dc", command.key)
            if command.op in {"scale", "set"}:
                default = 0 if command.key == "WaitAtBoss" else 1
                self.saved.setdefault(key, self.values.get(key, default))
                self.values[key] = (command.centi / 100 if command.op == "set" else
                                    self.values.get(key, default) * command.centi / 100)
            elif command.op == "reset":
                self.values[key] = self.saved.pop(key, 0 if command.key == "WaitAtBoss" else 1)
            elif command.op == "clear":
                for saved in [k for k in self.saved if k[0] == "dc"]:
                    self.values[saved] = self.saved.pop(saved)


@_run_async
async def test_real_applier_resume_preserves_bot_dc_effects_and_journals_until_terminal(fake):
    from leeroy_mind.guild.protocol import RaidControl
    listener, run, ports = real_listener(fake)
    effects = ports.hook = JournalEffects()
    chosen = plan_card(fake, Directive("dps aoe", "+"), Directive("HealRepositionMaxRange", 75))
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    assert effects.values[102, "dps aoe"] is True
    assert effects.values["dc", "HealRepositionMaxRange"] == .75
    before = {r["id"] for r in ports.open()}
    sent = len(ports.sent)
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    assert len(ports.sent) == sent + 1 and isinstance(ports.sent[-1], RaidControl)
    assert ports.sent[-1].param == "resume"
    assert {r["id"] for r in ports.open()} == before
    assert effects.values[102, "dps aoe"] is True
    assert effects.values["dc", "HealRepositionMaxRange"] == .75
    assert effects.values["dc", "WaitAtBoss"] == 1
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=chosen.id, arm_id=None, change="")
    await listener.on_boss(boss(fake, 4))
    await listener.on_boss(boss(fake, 4, "wiped"))
    assert effects.values[102, "dps aoe"] is False
    assert effects.values["dc", "HealRepositionMaxRange"] == 1
    assert not [r for r in ports.open() if r["layer"] == "boss"]
    assert await listener.on_before_end(run)
    assert effects.values["dc", "WaitAtBoss"] == 0
    assert not ports.open() and not ports.applier.cleanup_pending(listener.run_key(run))
    assert await ports.applier.close()


@_run_async
async def test_real_startup_applies_desired_standing_after_confirmed_hold(fake):
    from leeroy_mind.guild.protocol import BotDo
    from leeroy_mind.training.protocol import DcSetting
    listener, run, ports = real_listener(fake)
    effects = ports.hook = JournalEffects()
    store.put_setup(fake.conn, dict(guid=102, since=fake.clock.now(), rotation=None,
        toggles=["+threat"], vals={}, premade=None, copied_from=None, explored="", applied=None))
    plan_card(fake, Directive("dps aoe", "+"), context="631:*:0")
    await listener.on_start(run)
    assert isinstance(ports.sent[0], DcSetting) and ports.sent[0].key == "WaitAtBoss"
    bot = [c for c in ports.sent if isinstance(c, BotDo)]
    assert [c.param for c in bot] == ["+threat", "+dps aoe"]
    assert effects.values[102, "threat"] and effects.values[102, "dps aoe"]
    assert {e.layer for e in ports.applier._entries} == {"standing", "pooled"}
    assert await listener.on_before_end(run)
    assert not effects.values[102, "threat"] and not effects.values[102, "dps aoe"]
    assert await ports.applier.close()


async def pooled_stock_trial(fake, listener, run):
    pooled = plan_card(fake, Directive("threat", "+"), context="631:*:0")
    champion = plan_card(fake, Directive("dps aoe", "+"))
    stock = plan_card(fake, context="631:*:0", status="trial", version=2)
    arm_id = trial_arm(fake, stock, "threat")
    effective = replace(stock, context="631:4:0", directives=champion.directives)
    listener.set_experimenter(SimpleNamespace(arm_for=Mock(return_value=(effective, arm_id)),
                                              arm_valid=Mock(return_value=True)))
    for _ in range(3):
        await record(fake, run, outcome="killed")
    return pooled, champion, stock, arm_id


@_run_async
async def test_real_applier_pooled_stock_arm_omits_tested_lever_preserves_other_baseline(fake):
    listener, run, ports = real_listener(fake)
    effects = ports.hook = JournalEffects()
    _, champion, stock, arm_id = await pooled_stock_trial(fake, listener, run)
    await listener.on_start(run)
    assert effects.values[102, "threat"] is True
    await listener.on_dc_status(status(fake))
    assert effects.values[102, "threat"] is False
    assert effects.values[102, "dps aoe"] is True
    assert {e.layer for e in ports.applier._entries} == {"arm"}
    assert not [r for r in ports.open() if r["layer"] in {"pooled", "boss"}]
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=stock.id, arm_id=arm_id, change="")
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    assert effects.values[102, "threat"] is False
    assert effects.values[102, "dps aoe"] is True
    await listener.on_boss(boss(fake, 4))
    await listener.on_boss(boss(fake, 4, "wiped"))
    assert effects.values[102, "dps aoe"] is False
    # The normal champion path restores pooled plus boss after the arm is benched.
    await listener.on_dc_status(status(fake))
    assert effects.values[102, "threat"] is True
    assert effects.values[102, "dps aoe"] is True
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=champion.id, arm_id=None, change="")
    assert await listener.on_before_end(run)
    assert await ports.applier.close()


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("standing_lever", ["threat", "dps aoe"])
@_run_async
async def test_real_applier_desired_or_applied_standing_conflict_declines_stock_comparison(fake, applied, standing_lever):
    listener, run, ports = real_listener(fake)
    effects = ports.hook = JournalEffects()
    _, champion, _, _ = await pooled_stock_trial(fake, listener, run)
    def desired():
        store.put_setup(fake.conn, dict(guid=102, since=fake.clock.now(), rotation=None,
            toggles=["+"+standing_lever], vals={}, premade=None, copied_from=None, explored="", applied=None))
    await listener.on_start(run)
    if applied:
        standing = Card(None, "0:0:0", 1, "champion", "stock", (Directive(standing_lever, "+"),))
        assert await ports.applier.apply(listener.run_key(run), "standing", standing.context,
            standing, {102: ports.state.members[102]}, tank_guid=101, lead_is_human=False)
        assert any(e.layer == "standing" for e in ports.applier._entries)
    else:
        desired()
    await listener.on_dc_status(status(fake))
    assert effects.values[102, "threat"] is True
    assert effects.values[102, "dps aoe"] is True
    assert not [e for e in ports.applier._entries if e.layer == "arm"]
    listener.ledger.set_in_play.assert_called_with("631:4:0", card_id=champion.id, arm_id=None, change="")
    assert await listener.on_before_end(run)
    assert await ports.applier.close()


@_run_async
async def test_real_applier_arm_baseline_change_during_apply_reverts_and_declines_receipt(fake):
    from leeroy_mind.guild.protocol import BotDo
    listener, run, ports = real_listener(fake)
    effects = JournalEffects()
    await pooled_stock_trial(fake, listener, run)
    async def changed(command):
        await effects(command)
        if isinstance(command, BotDo) and command.action == "co" and command.param == "+dps aoe":
            listener.experimenter.arm_valid.return_value = False
    ports.hook = changed
    await listener.on_start(run)
    await listener.on_dc_status(status(fake))
    assert effects.values[102, "threat"] is False
    assert effects.values[102, "dps aoe"] is False
    assert not [r for r in ports.open() if r["layer"] == "arm"]
    listener.ledger.set_in_play.assert_called_with("631:4:0")
    listener.ledger.mark_unscoreable.assert_called_with("631:4:0")
    sent = len(ports.sent)
    await listener.on_dc_status(status(fake, state="paused", reason="waiting at Festergut", paused=True))
    assert len(ports.sent) == sent
    assert await listener.on_before_end(run)
    assert await ports.applier.close()
