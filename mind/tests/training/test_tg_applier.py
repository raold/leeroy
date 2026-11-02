import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.guild.protocol import BotDo, Job, JobSkip, RaidControl
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Ack
from leeroy_mind.training import store as ts
from leeroy_mind.training.applier import Applier, ApplierState
from leeroy_mind.training.cards import CLASSES, LEVERS, Card, Directive, Targets
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.levers import MemberInfo
from leeroy_mind.training.protocol import CustomStrategy, DcKey, DcSetting
from leeroy_mind.training.vocab import ClassVocab, Vocab


CTX = "631:0:0"
RUN = "raid:1"
MEMBERS = {101: MemberInfo(101, "tank", 1, 2), 102: MemberInfo(102, "ranged", 8, 2),
           103: MemberInfo(103, "healer", 5, 0), 104: MemberInfo(104, "ranged", 9, 0)}


def card(*directives, **changes):
    return Card(**(dict(id=None, context=CTX, version=1, status="champion", origin="stock",
                        directives=directives) | changes))


def vocabulary():
    strategies = frozenset({"threat", "dps aoe", "save mana", "wait for attack", "focus heal targets"})
    return Vocab({c: ClassVocab(strategies, {"frostbolt"}, class_=c) for c in CLASSES}, {},
        {n: DcKey(n, 0, 0, 0, 0, 0) for n, v in LEVERS.items() if v.kind == "dc"}, strategies, 1,
        complete=True, rotations={"tg_frost": (8, 2), "tg_other": (8, 2)})


class Fake:
    def __init__(self, store, clock):
        self.conn, self.clock = store.conn, clock
        self.cfg, self.human_guid = TrainingConfig(enabled=True), 1
        self.state = ApplierState(RUN, "session:1", 77, 1, 101, False, MEMBERS,
                                  frozenset(MEMBERS), vocabulary())
        self.first = clock.now() - 28 * 86400
        self.sent, self.jobs, self.unscoreable, self.claims, self.released = [], {}, [], [], []
        self.holders = {}
        self.fail_ack, self.fail_job = {}, {}
        self.hook = None
        self.applier = Applier(self, self, state_provider=self.snapshot)

    def snapshot(self, run):
        return self.state if run is None or self.state and self.state.run_key == run else None

    def first_pull_ts(self):
        return self.first

    def mark_unscoreable(self, context):
        self.unscoreable.append(context)

    def human(self):
        return HumanIdentity(1, "Examplechar", "Examplecaller", "Example Person", True)

    def cast(self):
        return {g: SimpleNamespace(name=f"Unit{g}") for g in MEMBERS}

    def holder(self, guid):
        return self.holders.get(guid)

    def claim(self, owner, guids, *, until):
        granted = {g for g in guids if self.holder(g) in {None, owner}}
        self.claims.append((owner, granted, until))
        if self.state.body_owner is not None:
            self.holders.update({g: owner for g in granted})
        return granted

    def release(self, owner, guids=None):
        self.released.append((owner, set(guids or ())))
        for guid in guids or ():
            if self.holder(guid) == owner:
                self.holders.pop(guid)

    async def send(self, command):
        assert any(json.loads(r["command"])["wire"]["data"]["id"] == command.id
                   for r in ts.open_applied(self.conn, 0)), "journal must precede dispatch"
        self.sent.append(command)
        self.jobs[command.id] = command
        if self.hook:
            await self.hook(command)
        error = self.fail_ack.pop(getattr(command, "param", ""), "")
        return Ack(0, command.id, not error, error)

    async def wait_job(self, ident, timeout_s):
        assert timeout_s == 5.0
        cmd = self.jobs[ident]
        reason = self.fail_job.pop(cmd.param, "")
        if reason == "missing":
            return None
        return Job(0, ident, "bot_do", "done", "", "", () if reason else cmd.guids,
                   tuple(JobSkip(g, reason) for g in cmd.guids) if reason else (), 0, 0, 77, 0, 0, 0)

    async def apply(self, c, layer="boss", members=None):
        return await self.applier.apply(RUN, layer, c.context, c, MEMBERS if members is None else members,
                                        tank_guid=101, lead_is_human=False)

    def open(self):
        return ts.open_applied(self.conn, 0)


@pytest.fixture
def fake(store, clock):
    return Fake(store, clock)


def run(coro):
    return asyncio.run(coro)


def test_apply_logs_before_send_and_reverts_newest_first(fake):
    async def exercise():
        assert await fake.apply(card(Directive("threat", "+"), Directive("dps aoe", "+")),
                                members={102: MEMBERS[102]})
        assert await fake.applier.revert(RUN, "boss") == 2
        assert [c.param for c in fake.sent] == ["+threat", "+dps aoe", "dps aoe", "threat"]
        assert not fake.open()
    run(exercise())


def test_refused_command_reverts_card_and_suspends_drift(fake):
    async def exercise():
        c = card(Directive("threat", "+"), Directive("dps aoe", "+"))
        c = replace(c, id=ts.put_card(fake.conn, c))
        fake.fail_job["+dps aoe"] = "not_primed"
        assert not await fake.apply(c, members={102: MEMBERS[102]})
        assert [c.param for c in fake.sent][-2:] == ["dps aoe", "threat"]
        assert not fake.open()
        assert fake.conn.execute("SELECT status FROM tg_card WHERE id=?", (c.id,)).fetchone()[0] == "suspended"
    run(exercise())


def test_layers_are_per_guid_and_restore_reapplies_lower(fake):
    async def exercise():
        assert await fake.apply(card(Directive("threat", "+")), "pooled")
        assert await fake.apply(card(Directive("threat", "+"), Directive("dps aoe", "+")), "arm",
                                {102: MEMBERS[102]})
        before = len(fake.sent)
        await fake.applier.revert(RUN, "arm")
        assert [c.param for c in fake.sent[before:]] == ["dps aoe", "threat", "+threat"]
        assert all(c.guids == (102,) for c in fake.sent[before:])
        assert any(e.key == (104, "threat") and e.layer == "pooled" for e in fake.applier._entries)
    run(exercise())


def test_hidden_lower_layer_never_sends_or_restores(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "circle")), "standing")
        n = len(fake.sent)
        assert await fake.apply(card(Directive("formation", "near")), "pooled")
        assert len(fake.sent) == n
        await fake.applier.revert(RUN, "pooled")
        assert len(fake.sent) == n
    run(exercise())


def test_context_revert_does_not_touch_other_context(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "circle")), members={102: MEMBERS[102]})
        assert await fake.apply(card(Directive("formation", "near"), context="631:1:0"), members={104: MEMBERS[104]})
        before = len(fake.sent)
        await fake.applier.revert(RUN, "boss", context=CTX)
        assert [c.guids for c in fake.sent[before:]] == [(102,)]
        assert any(r["context"] == "631:1:0" for r in fake.open())
    run(exercise())


def test_failed_restore_retains_receipt_and_blocks_apply_until_retry(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "circle")), members={102: MEMBERS[102]})
        fake.fail_job["formation"] = "failed"
        assert await fake.applier.revert(RUN, "boss") == 0
        assert fake.open()
        before = len(fake.sent)
        assert not await fake.apply(card(Directive("threat", "+")))
        assert len(fake.sent) == before
        await fake.applier.end_run(RUN)
        assert not fake.open()
        assert any(isinstance(c, BotDo) and c.param == "*" for c in fake.sent)
        assert isinstance(fake.sent[-1], DcSetting) and fake.sent[-1].op == "clear"
    run(exercise())


def test_restore_offline_is_not_successful_undo(fake):
    async def exercise():
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        fake.fail_job["threat"] = "offline"
        await fake.applier.revert(RUN, "boss")
        assert fake.open()
        assert not await fake.apply(card(Directive("dps aoe", "+")))
    run(exercise())


def test_offline_apply_skips_only_that_member(fake):
    async def exercise():
        fake.fail_job["+threat"] = "offline"
        assert await fake.apply(card(Directive("threat", "+")))
        assert [e.key for e in fake.applier._entries] == [(104, "threat")]
        assert len(fake.open()) == 1
    run(exercise())


def test_training_off_stops_run_without_suspending_and_cleanup_still_works(fake):
    async def exercise():
        c = card(Directive("threat", "+"), Directive("dps aoe", "+"))
        c = replace(c, id=ts.put_card(fake.conn, c))
        fake.fail_ack["+dps aoe"] = "training_off"
        assert not await fake.apply(c, members={102: MEMBERS[102]})
        assert not fake.open()
        assert fake.conn.execute("SELECT status FROM tg_card WHERE id=?", (c.id,)).fetchone()[0] == "champion"
        assert not await fake.apply(card(Directive("threat", "+")))
        fake.cfg = replace(fake.cfg, act=False)
        await fake.applier.end_run(RUN)
    run(exercise())


def test_acting_requires_real_28_days_even_if_config_shorter(fake):
    now = fake.clock.now()
    fake.first = now - 27 * 86400
    assert not fake.applier.acting(now)
    fake.cfg = replace(fake.cfg, observe_days=0)
    assert not fake.applier.acting(now)
    fake.first = now - 28 * 86400
    assert fake.applier.acting(now)
    fake.first = float("nan")
    assert not fake.applier.acting(now)
    fake.first = now - 29 * 86400
    fake.cfg = replace(fake.cfg, act=False)
    assert not fake.applier.acting(now)


@pytest.mark.parametrize("change", ["no_provider", "wrong_instance", "wrong_session", "stale_member", "vocab"])
def test_missing_or_changed_state_refuses_before_mutation(fake, change):
    async def exercise():
        if change == "no_provider":
            fake.applier.state_provider = None
        elif change == "vocab":
            fake.state = replace(fake.state, vocab=replace(fake.state.vocab, complete=False))
        elif change == "stale_member":
            fake.state = replace(fake.state, members={102: replace(MEMBERS[102], spec_tab=1)})
        else:
            assert await fake.apply(card(Directive("threat", "+")))
            fake.state = replace(fake.state, **({"instance_id": 78} if change == "wrong_instance" else {"session_key": "new"}))
        before = len(fake.sent)
        assert not await fake.apply(card(Directive("threat", "+")))
        assert len(fake.sent) == before
    run(exercise())


def test_body_owner_rechecked_after_every_await(fake):
    async def exercise():
        fake.state = replace(fake.state, body_owner="raid-owner:1")
        fake.holders = {g: "raid-owner:1" for g in MEMBERS}
        async def change_owner(command):
            if getattr(command, "param", "") == "+threat":
                fake.holders[102] = "new-owner"
        fake.hook = change_owner
        assert not await fake.apply(card(Directive("threat", "+"), Directive("dps aoe", "+")),
                                    members={102: MEMBERS[102]})
        assert [c.param for c in fake.sent] == ["+threat"]
        assert fake.open()
    run(exercise())


def test_epoch_change_after_ack_blocks_followup_and_keeps_cleanup(fake):
    async def exercise():
        async def reconnect(command):
            fake.state = replace(fake.state, connection_epoch=2)
        fake.hook = reconnect
        assert not await fake.apply(card(Directive("threat", "+"), Directive("dps aoe", "+")),
                                    members={102: MEMBERS[102]})
        assert not any(getattr(c, "param", "") == "+dps aoe" for c in fake.sent)
    run(exercise())


def test_missing_terminal_job_survives_hello_and_blocks_new_apply(fake):
    async def exercise():
        fake.fail_job["+threat"] = "missing"
        assert not await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        before = len(fake.sent)
        assert await fake.applier.replay_on_hello() == 0
        assert len(fake.sent) == before
        assert fake.open()
        assert not await fake.apply(card(Directive("dps aoe", "+")))
    run(exercise())


def test_cancellation_retains_late_mutation_receipt_until_confirmed_cleanup(fake):
    async def exercise():
        sent, finish = asyncio.Event(), asyncio.Event()
        async def delayed(command):
            if getattr(command, "param", "") == "+threat":
                sent.set()
                await finish.wait()
        fake.hook = delayed
        task = asyncio.create_task(fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]}))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.open()
        assert not await fake.apply(card(Directive("dps aoe", "+")))
        finish.set()
        await asyncio.gather(*(r.task for r in fake.applier._receipts.values()))
        await fake.applier.end_run(RUN)
        assert not fake.open()
    run(exercise())


def test_recovery_restores_before_marking_and_does_not_expire_old_rows(fake):
    async def exercise():
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        fake.conn.execute("UPDATE tg_applied SET ts=?", (fake.clock.now() - 3 * 86400,))
        fake.conn.commit()
        restarted = Applier(fake, fake, state_provider=fake.snapshot)
        fake.cfg = replace(fake.cfg, act=False)
        assert await restarted.replay_on_hello() == 1
        assert CTX in fake.unscoreable
        assert not fake.open()
        assert fake.sent[-1].param == "threat"
    run(exercise())


def test_recovery_never_restores_old_session_into_new_raid(fake):
    async def exercise():
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        fake.state = replace(fake.state, session_key="session:2", instance_id=88)
        before = len(fake.sent)
        assert await fake.applier.replay_on_hello() == 0
        assert len(fake.sent) == before and fake.open()
        assert not fake.unscoreable
    run(exercise())


def add_rotation(fake, name="tg_frost"):
    return ts.put_rotation(fake.conn, dict(name=name, class_=8, spec_tab=2,
        lines=["often>frostbolt!6"], origin="test", status="champion", created=1))


def test_rotation_layer_ownership_groups_different_names(fake):
    async def exercise():
        add_rotation(fake)
        add_rotation(fake, "tg_other")
        rotation = lambda n: card(Directive("rotation", n, Targets(specs=((8, 2),))))
        assert await fake.apply(rotation("tg_frost"), "standing")
        before = len(fake.sent)
        assert await fake.apply(rotation("tg_other"), "pooled")
        assert not any(isinstance(c, BotDo) for c in fake.sent[before:])
        assert await fake.apply(rotation("tg_other"), "arm")
        params = [c.param for c in fake.sent if isinstance(c, BotDo)]
        assert params[-2:] == ["custom::tg_frost", "+custom::tg_other"]
        await fake.applier.revert(RUN, "arm")
        params = [c.param for c in fake.sent if isinstance(c, BotDo)]
        assert params[-2:] == ["custom::tg_other", "+custom::tg_frost"]
    run(exercise())


def test_prime_requires_online_matching_spec(fake):
    async def exercise():
        add_rotation(fake)
        fake.state = replace(fake.state, online=frozenset({101, 103, 104}))
        assert not await fake.applier.prime(["tg_frost"])
        assert not fake.sent
        fake.state = replace(fake.state, online=frozenset(MEMBERS))
        assert await fake.applier.prime(["tg_frost"]) == {"tg_frost"}
        assert fake.sent[-1].probe == 102
    run(exercise())


def test_hello_removes_saved_names_before_priming(fake):
    async def exercise():
        add_rotation(fake)
        fake.state = replace(fake.state, run_key=None, instance_id=None)
        await fake.applier.replay_on_hello()
        assert [c.action for c in fake.sent[:2]] == ["co", "nc"]
        assert all(c.param == "-custom::tg_frost,-custom::tg_smoke_v1" for c in fake.sent[:2])
        assert isinstance(fake.sent[2], CustomStrategy)
        assert fake.released and not fake.open()
    run(exercise())


def test_focus_group_restore_consumes_server_snapshot_only_once(fake):
    async def exercise():
        assert await fake.apply(card(Directive("focus tanks", "on")), members={103: MEMBERS[103]})
        await fake.applier.revert(RUN, "boss")
        assert len([c for c in fake.sent if isinstance(c, BotDo) and c.action == "restore"]) == 1
        assert not fake.open()
    run(exercise())


def test_hold_release_clear_journal_and_restore_before_resume(fake):
    async def exercise():
        effective = {"WaitAtBoss": 0.0}
        async def dc_semantics(command):
            if isinstance(command, DcSetting) and command.key == "WaitAtBoss":
                if command.op == "scale":
                    effective[command.key] *= command.centi / 100.0
                elif command.op == "set":
                    effective[command.key] = command.centi / 100.0
                elif command.op == "reset":
                    effective[command.key] = 0.0
        fake.hook = dc_semantics
        assert await fake.applier.hold(101)
        assert fake.sent[-1] == DcSetting(fake.sent[-1].id, 101, "set", "WaitAtBoss", 100)
        assert effective["WaitAtBoss"] == 1.0  # scale100 would incorrectly leave the zero baseline disabled.
        assert await fake.applier.release(101)
        assert effective["WaitAtBoss"] == 0.0
        assert isinstance(fake.sent[-1], RaidControl) and fake.sent[-1].param == "resume"
        assert isinstance(fake.sent[-2], DcSetting) and fake.sent[-2].op == "clear"
        assert not fake.open()
        assert await fake.applier.clear_run(101)
    run(exercise())


def test_standing_outside_raid_defers_until_durable_defaults_integration(fake):
    async def exercise():
        fake.state = replace(fake.state, run_key=None, instance_id=None)
        ts.put_setup(fake.conn, dict(guid=102, since=1, toggles=["+threat"], vals={}))
        assert await fake.applier.apply_standing([102]) == 0
        assert not fake.sent and not fake.claims
        assert ts.latest_setups(fake.conn)[102]["applied"] is None
        fake.state = replace(fake.state, run_key=RUN, instance_id=77)
        assert await fake.applier.apply_standing([102], run_key=RUN) == 1
        assert fake.sent[-1].param == "+threat"
    run(exercise())


def test_invalid_card_is_rejected_before_any_mutation(fake):
    async def exercise():
        assert not await fake.apply(card(Directive("boost", "+")))
        assert not await fake.apply(card(Directive("threat", "+"), call="Unit102 attack"))
        assert not fake.sent and not fake.open()
    run(exercise())


def test_failed_restore_after_epoch_change_is_consumed_once_on_retry(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "circle")), members={102: MEMBERS[102]})
        fake.state = replace(fake.state, connection_epoch=2)
        fake.fail_job["formation"] = "failed"
        await fake.applier.end_run(RUN)
        before = len(fake.sent)
        await fake.applier.end_run(RUN)
        assert [c.param for c in fake.sent[before:] if isinstance(c, BotDo)].count("formation") == 1
        assert not fake.open()
    run(exercise())


def test_hello_cannot_turn_untrusted_rotation_name_into_strategy_command(fake):
    async def exercise():
        add_rotation(fake, "foreign,+boost")
        fake.state = replace(fake.state, run_key=None, instance_id=None)
        await fake.applier.replay_on_hello()
        assert all(c.param == "-custom::tg_smoke_v1" for c in fake.sent)
    run(exercise())


def test_no_resume_when_final_clear_cannot_be_confirmed(fake):
    async def exercise():
        assert await fake.applier.hold(101)
        async def lose_owner(command):
            if isinstance(command, DcSetting) and command.op == "reset":
                fake.holders[101] = "new-owner"
        fake.hook = lose_owner
        assert not await fake.applier.release(101)
        assert not any(isinstance(c, RaidControl) for c in fake.sent)
    run(exercise())


def test_all_generated_command_ids_pass_real_bridge_preservation(fake):
    from test_bridge import session
    from leeroy_mind.guild import protocol as guild_protocol
    from leeroy_mind.training import protocol as training_protocol

    async def exercise():
        guild_protocol.register()
        training_protocol.register()
        kinds = {"bot_do", "custom_strategy", "dc_setting", "raid_control"}
        async with session(bridge_kw={"known_types": kinds}) as wire:
            original_send = fake.send
            async def real_send(command):
                await original_send(command)  # Keep the journal assertion and synthetic terminal jobs.
                return await wire.client.send(command, preserve_id=True)
            fake.send = real_send
            add_rotation(fake)
            assert await fake.applier.prime(["tg_frost"]) == {"tg_frost"}
            assert await fake.applier.hold(101)
            assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
            assert await fake.applier.release(101)
            sent = [c for c in wire.bridge.commands if c["type"] in kinds]
            assert {c["type"] for c in sent} == kinds
            assert [c["id"] for c in sent] == [c.id for c in fake.sent]
            assert all(c["id"].startswith("tg_") for c in sent)
    run(exercise())


def test_ack_wait_is_bounded_and_preserves_unresolved_receipt(fake, monkeypatch):
    import leeroy_mind.training.applier as module
    monkeypatch.setattr(module, "_WAIT_S", 0.02)
    async def exercise():
        async def never_ack(command):
            await asyncio.Event().wait()
        fake.hook = never_ack
        assert not await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        assert await fake.applier.close()
        assert fake.open()
        assert all(r.task.done() for r in fake.applier._receipts.values())
    run(exercise())


def test_close_drains_owned_waiters_without_claiming_successful_cleanup(fake):
    async def exercise():
        sent = asyncio.Event()
        async def never_ack(command):
            sent.set()
            await asyncio.Event().wait()
        fake.hook = never_ack
        task = asyncio.create_task(fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]}))
        await sent.wait()
        assert await fake.applier.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.open() and not fake.released
        assert not await fake.apply(card(Directive("dps aoe", "+")))
    run(exercise())


def test_dc_apply_failed_after_mutation_requires_confirmed_inverse(fake):
    async def exercise():
        original_send = fake.send
        effective = {"PartyMaxSpread": 100.0}
        mutation_receipt = []
        async def dc_send(command):
            ack = await original_send(command)
            if isinstance(command, DcSetting) and command.op == "scale":
                effective[command.key] *= command.centi / 100.0
                mutation_receipt.extend(r["id"] for r in fake.open())
                return Ack(0, command.id, False, "apply_failed")
            if isinstance(command, DcSetting) and command.op == "reset":
                assert effective[command.key] == 125.0
                assert all(fake.conn.execute("SELECT undone FROM tg_applied WHERE id=?", (ident,)).fetchone()[0]
                           is None for ident in mutation_receipt)
                effective[command.key] = 100.0
            return ack
        fake.send = dc_send
        assert not await fake.apply(card(Directive("PartyMaxSpread", 125)))
        assert [(c.op, c.key) for c in fake.sent] == [("scale", "PartyMaxSpread"), ("reset", "PartyMaxSpread")]
        assert effective["PartyMaxSpread"] == 100.0 and not fake.open()
    run(exercise())


def test_dc_restore_failed_after_partial_mutation_retains_both_receipts(fake):
    async def exercise():
        original_send = fake.send
        effective, resets = {"PartyMaxSpread": 100.0}, 0
        async def dc_send(command):
            nonlocal resets
            ack = await original_send(command)
            if isinstance(command, DcSetting) and command.op == "scale":
                effective[command.key] *= command.centi / 100.0
            if isinstance(command, DcSetting) and command.op == "reset":
                effective[command.key] = 100.0
                resets += 1
                if resets == 1:
                    return Ack(0, command.id, False, "restore_failed")
            return ack
        fake.send = dc_send
        assert await fake.apply(card(Directive("PartyMaxSpread", 125)))
        assert await fake.applier.revert(RUN, "boss") == 0
        assert effective["PartyMaxSpread"] == 100.0 and len(fake.open()) == 2
        assert fake.applier.cleanup_pending(RUN)
        assert not await fake.apply(card(Directive("threat", "+")))
        await fake.applier.end_run(RUN)
        assert resets == 2 and not fake.open()
        assert not fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_dc_partial_clear_failure_is_journaled_until_successful_retry(fake):
    async def exercise():
        original_send = fake.send
        # Clear owns the server's existing exact-run journal, including a hold
        # whose resume can fail after other keys were already restored.
        server_journal = {"PartyMaxSpread", "WaitAtBoss"}
        first = True
        async def dc_send(command):
            nonlocal first
            ack = await original_send(command)
            if isinstance(command, DcSetting) and command.op == "clear":
                server_journal.discard("PartyMaxSpread")
                if first:
                    first = False
                    return Ack(0, command.id, False, "restore_failed")
                server_journal.clear()
            return ack
        fake.send = dc_send
        assert not await fake.applier.clear_run(101)
        assert server_journal == {"WaitAtBoss"} and len(fake.open()) == 1
        assert fake.applier.cleanup_pending(RUN)
        assert await fake.applier.clear_run(101)
        assert not server_journal and not fake.open()
        assert not fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_unknown_negative_ack_is_not_proof_of_nonmutation(fake):
    async def exercise():
        fake.fail_ack["+threat"] = "unrecognized_failure"
        assert not await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        assert len(fake.open()) == 1
        assert json.loads(fake.open()[0]["command"])["settled"] is False
        assert not await fake.apply(card(Directive("dps aoe", "+")))
    run(exercise())


@pytest.mark.parametrize("change", ["holder", "session", "membership", "provider"])
def test_scheduled_dispatch_rechecks_authority_after_receipt_creation(fake, change):
    async def exercise():
        perform = fake.applier._perform
        async def preempted(receipt, command):
            assert fake.open()  # The receipt exists, but no wire command has left.
            if change == "holder":
                fake.holders[102] = "replacement-owner"
            elif change == "session":
                fake.state = replace(fake.state, session_key="opaque-replacement-session")
            elif change == "membership":
                fake.state = replace(fake.state, members={g: m for g, m in MEMBERS.items() if g != 102})
            else:
                fake.applier.state_provider = lambda run: fake.snapshot(run)
            await perform(receipt, command)
        fake.applier._perform = preempted
        assert not await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        assert fake.state.connection_epoch == 1
        assert not fake.sent and not fake.open()
    run(exercise())


def test_no_resume_after_holder_replacement_during_final_clear_ack(fake):
    async def exercise():
        assert await fake.applier.hold(101)
        async def final_clear_preemption(command):
            if isinstance(command, DcSetting) and command.op == "clear":
                fake.holders[101] = "replacement-owner"
        fake.hook = final_clear_preemption
        assert not await fake.applier.release(101)
        assert not any(isinstance(c, RaidControl) for c in fake.sent)
        assert fake.applier.cleanup_pending(RUN)
    run(exercise())


@pytest.mark.parametrize("skip", ["offline", "dead"])
def test_partial_wait_bundle_retained_when_skipped_value_cannot_be_restored(fake, skip):
    async def exercise():
        fake.fail_job["wait for attack time 6"] = skip
        fake.fail_job["wait for attack"] = skip
        assert not await fake.apply(card(Directive("wait for attack time", 6)), members={102: MEMBERS[102]})
        assert [c.param for c in fake.sent] == ["+wait for attack", "wait for attack time 6", "wait for attack"]
        assert len(fake.applier._entries) == 1 and fake.open()
        assert fake.applier.cleanup_pending(RUN)
        # A layer-specific retry can still find the successful first command.
        await fake.applier.revert(RUN, "boss")
        assert not fake.open() and not fake.applier._entries
    run(exercise())


def test_partial_wait_skip_restores_successful_strategy_before_omitting_member(fake):
    async def exercise():
        fake.fail_job["wait for attack time 6"] = "dead"
        assert await fake.apply(card(Directive("wait for attack time", 6)), members={102: MEMBERS[102]})
        assert [c.param for c in fake.sent] == ["+wait for attack", "wait for attack time 6", "wait for attack"]
        assert not fake.open() and not fake.applier._entries
    run(exercise())


def test_skipped_replacement_reactivates_previous_lower_layer(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "near")), "standing", {102: MEMBERS[102]})
        before = len(fake.sent)
        fake.fail_job["formation circle"] = "dead"
        assert await fake.apply(card(Directive("formation", "circle")), "arm", {102: MEMBERS[102]})
        assert [c.param for c in fake.sent[before:]] == ["formation", "formation circle", "formation near"]
        assert [e.layer for e in fake.applier._entries] == ["standing"]
        assert len(fake.open()) == 1
    run(exercise())


def test_skipped_lower_reactivation_remains_blocked_and_owned(fake):
    async def exercise():
        assert await fake.apply(card(Directive("formation", "near")), "standing", {102: MEMBERS[102]})
        fake.fail_job.update({"formation circle": "dead", "formation near": "dead"})
        assert not await fake.apply(card(Directive("formation", "circle")), "arm", {102: MEMBERS[102]})
        assert [e.layer for e in fake.applier._entries] == ["standing"]
        assert fake.applier.cleanup_pending(RUN)
        assert not await fake.apply(card(Directive("threat", "+")))
        await fake.applier.end_run(RUN)
        assert not fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_cleanup_pending_is_exact_run_and_not_inferred_from_end_count(fake):
    async def exercise():
        assert not fake.applier.cleanup_pending(RUN)
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        assert fake.applier.cleanup_pending(RUN)
        assert not fake.applier.cleanup_pending("unrelated-run")
        original = fake.state
        fake.state = replace(original, session_key="replacement-session")
        assert await fake.applier.end_run(RUN) == 0
        assert fake.applier.cleanup_pending(RUN)
        fake.state = original
        await fake.applier.end_run(RUN)
        assert not fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_cleanup_pending_includes_waiting_operation_before_any_journal(fake):
    async def exercise():
        await fake.applier._lock.acquire()
        task = asyncio.create_task(fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]}))
        await asyncio.sleep(0)
        assert not fake.open() and fake.applier.cleanup_pending(RUN)
        assert not fake.applier.cleanup_pending("unrelated-run")
        fake.applier._lock.release()
        assert await task
        await fake.applier.end_run(RUN)
        assert not fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_resume_held_preserves_installed_card_and_future_hold_until_end_run(fake):
    async def exercise():
        effective = {"WaitAtBoss": 0.0, "threat": False}
        async def server_semantics(command):
            if isinstance(command, DcSetting):
                if command.key == "WaitAtBoss" and command.op == "set":
                    effective["WaitAtBoss"] = command.centi / 100.0
                elif command.op in {"reset", "clear"}:
                    effective["WaitAtBoss"] = 0.0
            elif isinstance(command, BotDo):
                if command.param == "+threat":
                    effective["threat"] = True
                elif command.action == "restore" and command.param in {"threat", "*"}:
                    effective["threat"] = False
            elif isinstance(command, RaidControl):
                assert effective == {"WaitAtBoss": 1.0, "threat": True}
        fake.hook = server_semantics
        assert await fake.applier.hold(101)
        c = card(Directive("threat", "+"))
        c = replace(c, id=ts.put_card(fake.conn, c))
        assert await fake.apply(c, members={102: MEMBERS[102]})
        before, owned = len(fake.sent), {row["id"] for row in fake.open()}
        layers = tuple(fake.applier._entries)
        assert await fake.applier.resume_held(101)
        assert len(fake.sent) == before + 1
        assert isinstance(fake.sent[-1], RaidControl) and fake.sent[-1].param == "resume"
        assert {row["id"] for row in fake.open()} == owned
        assert tuple(fake.applier._entries) == layers
        assert effective == {"WaitAtBoss": 1.0, "threat": True}
        assert fake.applier.cleanup_pending(RUN)
        await fake.applier.end_run(RUN)
        assert effective == {"WaitAtBoss": 0.0, "threat": False}
        assert not fake.open() and not fake.applier.cleanup_pending(RUN)
    run(exercise())


@pytest.mark.parametrize("change", ["acting", "holder", "session", "membership", "provider"])
def test_resume_held_revalidates_authority_at_actual_scheduled_dispatch(fake, change):
    async def exercise():
        assert await fake.applier.hold(101)
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        owned = {row["id"] for row in fake.open()}
        perform = fake.applier._perform
        async def preempted(receipt, command):
            if isinstance(command, RaidControl):
                if change == "acting":
                    fake.cfg = replace(fake.cfg, act=False)
                elif change == "holder":
                    fake.holders[101] = "replacement-owner"
                elif change == "session":
                    fake.state = replace(fake.state, session_key="replacement-session")
                elif change == "membership":
                    fake.state = replace(fake.state, members={g: m for g, m in MEMBERS.items() if g != 101})
                else:
                    fake.applier.state_provider = lambda run: fake.snapshot(run)
            await perform(receipt, command)
        fake.applier._perform = preempted
        assert not await fake.applier.resume_held(101)
        assert not any(isinstance(c, RaidControl) for c in fake.sent)
        assert {row["id"] for row in fake.open()} == owned
        assert fake.applier._entries and fake.applier.cleanup_pending(RUN)
    run(exercise())


@pytest.mark.parametrize("failure", ["missing", "failed_restore"])
def test_resume_held_refuses_unresolved_or_blocked_mutation(fake, failure):
    async def exercise():
        assert await fake.applier.hold(101)
        if failure == "missing":
            fake.fail_job["+threat"] = "missing"
            assert not await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        else:
            assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
            fake.fail_job["threat"] = "failed"
            await fake.applier.revert(RUN, "boss")
        before = len(fake.sent)
        assert not await fake.applier.resume_held(101)
        assert len(fake.sent) == before and fake.applier.cleanup_pending(RUN)
    run(exercise())


def test_resume_held_requires_a_confirmed_hold_and_retains_uncertain_resume(fake):
    async def exercise():
        assert not await fake.applier.resume_held(101)
        assert not fake.sent
        assert await fake.applier.hold(101)
        fake.fail_ack["resume"] = "dc_refused"
        assert not await fake.applier.resume_held(101)
        assert len(fake.open()) == 2
        resume = next(row for row in fake.open() if json.loads(row["command"])["wire"]["type"] == "RaidControl")
        assert json.loads(resume["command"])["settled"] is False
        assert json.loads(resume["inverse"]) is None
        assert fake.applier.cleanup_pending(RUN)
        assert not await fake.applier.resume_held(101)
    run(exercise())
