import asyncio
import hashlib
import json
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.guild.raid_night import RaidNight, RaidRun, State
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.protocol import Ack
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.training import service, store as ts
from leeroy_mind.training.applier import Applier
from leeroy_mind.training.cards import Directive
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ports import LivePorts

from guild.fakes import FakeArmory, FakePorts, member, persona
from .test_tg_applier import Fake, MEMBERS, RUN, card, vocabulary
from .test_tg_gate import _run_async
from .test_tg_service import enable


@pytest.fixture
def raid(tmp_path, clock):
    ports = FakePorts(tmp_path, clock)
    ports.cfg, ports.connection_epoch = TrainingConfig(enabled=True), 1
    ports.holder = lambda guid: None
    for guid in (101, 102, 103):
        ports.members[guid] = member(guid, f"Member{guid}", 1 if guid == 101 else 8, 2, map=631)
        ports.personas[guid] = persona(guid, f"Member{guid}")
    plan = RaidPlan("icc", 10, False, False, 101, 101, 103, [101, 102, 103], [], True)
    session = asyncio.run(RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    run = RaidRun(session.plan, session, True, False)
    ground = service.TrainingGround(SimpleNamespace(), ports)
    ground._connected = ground._presence_known = ground._startup_ready = True
    ground.vocab = vocabulary()
    ground.gate._phase = "passed"
    ground.ledger.first_pull_ts = lambda: clock.now() - 28 * 86400
    ground.listener.run = run
    try:
        yield ports, run, ground
    finally:
        ports.close()


def test_current_and_retained_state_have_same_exact_session_scope(raid):
    ports, run, ground = raid
    current = ground.state_for(None)
    assert current.run_key == ground.listener.run_key(run)
    assert current.body_owner is None and current.online == {101, 102, 103}
    ground.listener.invalidate(run)
    assert ground.state_for(None) is None
    assert ground.state_for(current.run_key) == current
    ports.conn.execute("UPDATE guild_raid SET ended=started+1 WHERE raid_id=?", (run.session.raid_id,))
    assert ground.state_for(current.run_key) is None


def test_installed_arbiter_without_session_claim_proof_fails_closed(raid):
    ports, run, ground = raid
    ground.app.bodies = SimpleNamespace(holder=lambda guid: "unrelated")
    assert ground.state_for(None) is None


def test_pre_call_uses_changed_stock_call_and_introduces_promoted_card_once(raid):
    ports, run, ground = raid
    baseline = replace(card(), call="wait for the tank", changed=ports.clock.now(), origin="stock")
    ident = ts.put_card(ports.conn, baseline)
    assert ground.pre_call(631, 0) == "wait for the tank"
    assert ground.pre_call(631, 0) == "wait for the tank"
    ports.conn.execute("UPDATE tg_card SET origin='coach' WHERE id=?", (ident,))
    assert ground.pre_call(631, 0).startswith("new plan on Lord Marrowgar, tested it yesterday:")
    assert ground.pre_call(631, 0) == "wait for the tank"
    ports.conn.execute("UPDATE tg_card SET call=? WHERE id=?", (service.BOSS_CALLS[(631, 0)].pre, ident))
    assert ground.pre_call(631, 0) is None


def test_gate_and_startup_readiness_revoke_new_application_not_cleanup_state(raid):
    ports, run, ground = raid
    assert ground.applier.acting(ports.clock.now())
    for field, value in (("_startup_ready", False), ("_presence_known", False)):
        old = getattr(ground, field)
        setattr(ground, field, value)
        assert not ground.applier.acting(ports.clock.now())
        setattr(ground, field, old)
    ground.gate._phase = "failed"
    assert not ground.applier.acting(ports.clock.now())
    assert ground.state_for(None) is not None


def test_pre_call_uses_selected_old_plan_and_does_not_call_progression_tested(raid):
    ports, run, ground = raid
    previous = replace(card(), status="retired", call="use the old formation", origin="coach")
    previous_id = ts.put_card(ports.conn, previous)
    current = replace(card(), version=2, call="use the new formation", origin="coach",
                      prev_champion=previous_id, changed=ports.clock.now())
    ts.put_card(ports.conn, current)
    ground.listener._old.add(current.context)
    assert ground.pre_call(631, 0) == previous.call
    ground.listener._old.clear()
    ground.listener._changes[current.context] = (Directive("threat", "+"), "wait for threat")
    assert ground.pre_call(631, 0) == "wait for threat"
    assert not ports.conn.execute("SELECT 1 FROM meta WHERE key GLOB 'tg_intro:*'").fetchone()


@_run_async
async def test_online_cleanup_barrier_retains_session_and_blocks_disband():
    events = []
    ready = False
    run = SimpleNamespace(lead_is_human=False)
    async def barrier(observed, outcome):
        assert observed is run
        events.append("barrier")
        return ready
    session = SimpleNamespace(close=AsyncMock(side_effect=lambda outcome: events.append("close")))
    night = RaidNight.__new__(RaidNight)
    night._closing = deque([(session, run, "called", deque(), True)])
    night._listeners = [SimpleNamespace(on_before_end=barrier)]
    night._votes, night.state = [], State.ENDED
    night._stop_dc = AsyncMock(side_effect=lambda run: events.append("stop"))
    night._drain_events = AsyncMock()
    night._notify = AsyncMock(side_effect=lambda *a, **k: events.append("end"))
    night._disband = (0, 0)
    await night._close_pending()
    await night._disband_due()
    assert night.active and list(night._closing) and night._disband == (0, 0)
    assert events == ["barrier"]
    ready = True
    await night._close_pending()
    assert events == ["barrier", "barrier", "stop", "close", "end"]
    assert not night.active


@_run_async
async def test_cleanup_exception_is_not_treated_as_notification_success():
    night = RaidNight.__new__(RaidNight)
    night._listeners = [SimpleNamespace(on_before_end=AsyncMock(side_effect=RuntimeError("synthetic")))]
    assert await night._before_end(object(), "ended") is False


@_run_async
async def test_offline_cleanup_barrier_retains_terminal_run_but_releases_gpu():
    offline = OfflineRaids.__new__(OfflineRaids)
    offline.run = object()
    offline.session = SimpleNamespace(close=AsyncMock())
    offline._owned = {"phase": "running", "terminal": {"reason": "", "state": "done"}}
    offline._listeners = [SimpleNamespace(on_before_end=AsyncMock(return_value=False))]
    offline._release = AsyncMock()
    await offline._finish()
    assert offline.active and offline.run is not None and offline.session is not None
    offline.session.close.assert_not_awaited()
    offline._release.assert_awaited_once()


@_run_async
async def test_startup_replays_before_refresh_and_partial_vocab_cannot_act(raid, monkeypatch):
    ports, run, ground = raid
    order = []
    async def replay():
        order.append("replay")
    async def refresh(ports):
        order.append("refresh")
        return replace(vocabulary(), complete=False)
    ground.applier.replay_on_hello = replay
    monkeypatch.setattr(service, "refresh", refresh)
    await ground._startup()
    assert order == ["replay", "refresh"] and not ground.application_allowed()


@_run_async
async def test_startup_reconnect_during_refresh_cannot_install_stale_vocabulary(raid, monkeypatch):
    ports, run, ground = raid
    ground.applier.replay_on_hello = AsyncMock()
    async def refresh(ports):
        ground._epoch += 1
        return vocabulary()
    monkeypatch.setattr(service, "refresh", refresh)
    await ground._startup()
    assert not ground._startup_ready


@_run_async
async def test_training_speech_uses_owned_guarded_pipeline_with_kind_and_target(app_stub):
    observed = []
    async def say_as(guid, channel, text, **kwargs):
        observed.append((guid, channel, text, kwargs))
        return True
    app_stub.engine.say_as = say_as
    ports = LivePorts(app_stub)
    assert await ports.say(101, "whisper", "assigned", kind=TurnKind.URGENT, to_guid=1)
    assert observed == [(101, "whisper", "assigned", {"kind": TurnKind.URGENT, "to_guid": 1})]
    assert not ports.guild._speech


def test_attach_coach_only_on_actual_runner_and_hooks_live_calls(app_stub):
    from leeroy_mind.nightly.runner import NightRunner, Stage
    async def nothing(ctx):
        pass
    enable(app_stub)
    app_stub.night_runner = NightRunner([Stage("reflect", nothing, "reflection")], lambda *args: None)
    listeners = []
    calls = SimpleNamespace(lust=AsyncMock())
    app_stub.guild = SimpleNamespace(ports=LivePorts(app_stub).guild, calls=calls,
        raid_night=SimpleNamespace(add_listener=listeners.append),
        offline=SimpleNamespace(add_listener=listeners.append))
    ground = service.attach_training(app_stub)
    assert [stage.name for stage in app_stub.night_runner.stages] == ["reflect", "coach"]
    assert calls.pre_call == ground.pre_call and calls.lust_held == ground.listener.lust_held
    assert ground.listener.lust_provider == calls.lust and listeners == [ground.listener, ground.listener]
    assert "chat" in app_stub.handlers and app_stub.hooks[0].change_for == ground.listener.change_for


def test_new_application_rechecks_gate_at_scheduled_dispatch_and_cleanup_survives_revocation(store, clock):
    fake = Fake(store, clock)
    permitted = True
    fake.applier.application_allowed = lambda: permitted
    async def exercise():
        nonlocal permitted
        assert await fake.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
        perform = fake.applier._perform
        async def revoked(receipt, command):
            nonlocal permitted
            if getattr(command, "param", None) == "+dps aoe":
                permitted = False
            await perform(receipt, command)
        fake.applier._perform = revoked
        assert not await fake.apply(card(Directive("dps aoe", "+")), members={102: MEMBERS[102]})
        assert "+dps aoe" not in [getattr(c, "param", "") for c in fake.sent]
        await fake.applier.end_run(RUN)
        assert not fake.applier.cleanup_pending(RUN)
        assert "threat" in [getattr(c, "param", "") for c in fake.sent]
    asyncio.run(exercise())


def test_resume_refuses_settled_postmutation_failed_hold_even_after_prior_success(store, clock):
    fake = Fake(store, clock)
    async def exercise():
        assert await fake.applier.hold(101)
        send = fake.send
        async def failed(command):
            await send(command)
            return Ack(0, command.id, False, "apply_failed")
        fake.send = failed
        assert not await fake.applier.hold(101)
        count = len(fake.sent)
        assert not await fake.applier.resume_held(101)
        assert len(fake.sent) == count and fake.applier.cleanup_pending(RUN)
    asyncio.run(exercise())


def test_application_permission_callback_exceptions_and_nonbool_fail_closed(store, clock):
    fake = Fake(store, clock)
    for callback in (lambda: 1, lambda: None, lambda: (_ for _ in ()).throw(ValueError("unknown"))):
        fake.applier.application_allowed = callback
        assert not fake.applier.acting(clock.now())


def test_cards_and_lessons_cli_are_read_only_and_omit_embeddings(fake, tmp_path, capsys):
    from leeroy_mind.training.cli import training_main
    ts.put_card(fake.conn, replace(card(), embedding=b"\0" * 2048))
    ts.put_lesson(fake.conn, "631:0:0", "2026-11-01", "Stay clear of the observed hazard.", ["pull:example"])
    path = tmp_path / "mind.db"
    before = hashlib.sha256(path.read_bytes()).digest()
    assert training_main(["cards", "--context", "631:0:0", "--data-dir", str(tmp_path)]) == 0
    cards = json.loads(capsys.readouterr().out)["cards"]
    assert len(cards) == 1 and "embedding" not in cards[0] and cards[0]["directives"] == []
    assert training_main(["lessons", "--context", "631:0:0", "--data-dir", str(tmp_path)]) == 0
    lessons = json.loads(capsys.readouterr().out)["lessons"]
    assert lessons[0]["evidence"] == ["pull:example"]
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not (tmp_path / "mind.lock").exists()
