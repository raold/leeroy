"""CUDA readiness races on fake workers/devices; no GPU, model or live process access."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from conftest import eventually, run
from leeroy_voice import gpu, service
from leeroy_voice.engines import EngineError
from leeroy_voice.engines import expressive
from leeroy_voice.wire import Session
from test_expressive import Rig22, Stand, TURBO, roomy
from test_gpu import until


class Gated(Stand):
    def __init__(self, phase="warmup"):
        super().__init__(up=False)
        self.phase = phase
        self.entered = threading.Event()
        self.release = threading.Event()
        self.after_start = self.after_warmup = lambda: None

    def _gate(self, phase):
        if self.phase == phase:
            self.entered.set()
            if not self.release.wait(5):
                raise EngineError("fake preparation wait expired")

    def start(self, *, cancelled=None):
        self._gate("load")
        super().start(cancelled=cancelled)
        self.after_start()

    def warmup(self, spec, *, timeout_s=30):
        super().warmup(spec, timeout_s=timeout_s)
        self._gate("warmup")
        self.after_warmup()

    def stop(self):
        self.release.set()  # production stop also wakes a blocked worker protocol wait
        super().stop()


@pytest.mark.parametrize("phase", ["load", "warmup"])
def test_not_ready_until_warmup_and_urgent_fallback_never_waits(tmp_path, fake_bank, phase):
    made = Gated(phase)

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            assert rig.service._live_expressive() is None
            assert rig.service.state()["expressive_device"] == "none"
            assert not rig.guard.holders[TURBO].confirmed
            if phase == "warmup":
                assert rig.service._status().gpu == TURBO  # resident, though not ready
            await rig.mind.send(rig.speak("urgent", text="Move away from that danger.",
                                          priority="p1", ttl=30))
            await rig.sees("urgent", "started")
            assert made.said == [] and rig.engine.live() == ["Move away from that danger."]
            assert not made.release.is_set()
            await rig.play_out()
            made.release.set()
            await until(rig, lambda: rig.service._cuda_ready)
            assert rig.guard.holders[TURBO].confirmed
            assert len(made.warmups) == 1 and made.warmups[0][0] == 101
            assert 0 < made.warmups[0][1] <= service.CUDA_WARMUP_BUDGET_S
            await rig.mind.send(rig.speak("ready", text="We can speak naturally now."))
            await rig.sees("ready", "started")
            assert made.said == [("We can speak naturally now.", {"attitude": "none"})]
    run(exercise())
    assert not made.up


@pytest.mark.parametrize("phase", ["load", "warmup"])
@pytest.mark.parametrize("change", ["logout", "disconnect", "identity", "model"])
def test_changed_session_revokes_preparation_without_late_resurrection(tmp_path, fake_bank,
                                                                     phase, change):
    made = Gated(phase)

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            old = rig.guard.holders[TURBO]
            if change == "logout":
                rig.service._on_session(Session(False, 1, "orion", "embed"))
            elif change == "disconnect":
                await rig.mind.close()
            elif change == "identity":
                rig.service._on_session(Session(True, 2, "orion", "embed"))
            else:
                rig.service._on_session(Session(True, 1, "new-model", "embed"))
            await eventually(lambda: made.stops and not made.up and not rig.service._cuda_phases)
            assert not old.confirmed
            assert not rig.service._cuda_ready and rig.service._live_expressive() is None
            assert TURBO not in rig.guard.holders
            assert made.said == []
    run(exercise())


def test_logout_relogin_does_not_adopt_previous_load(tmp_path, fake_bank):
    made = Gated("load")

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            old = rig.guard.holders[TURBO]
            rig.service._on_session(Session(False, 1, "orion", "embed"))
            rig.service._on_session(Session(True, 1, "orion", "embed"))
            await eventually(lambda: not rig.service._cuda_phases and not made.up)
            assert not old.confirmed and not rig.service._cuda_ready
            await until(rig, lambda: rig.service._cuda_ready)
            assert rig.guard.holders[TURBO] is not old
            assert made.loads == 1 and len(made.warmups) == 1  # revoked load never starts
    run(exercise())


@pytest.mark.parametrize("phase", ["load", "warmup"])
def test_one_deadline_bounds_loading_and_warmup(tmp_path, fake_bank, monkeypatch, phase):
    monkeypatch.setattr(service, "CUDA_STARTUP_BUDGET_S", 0.05)
    made = Gated(phase)

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            await eventually(lambda: made.stops and not rig.service._cuda_phases
                             and rig.service._expressive_task is None)
            assert not made.up and not rig.service._cuda_ready
            assert TURBO not in rig.guard.holders
            assert "deadline" in rig.service._expressive_why
    run(exercise())


@pytest.mark.parametrize("stage", ["after_load", "after_warmup"])
@pytest.mark.parametrize("loss", ["split", "missing", "wow", "margin"])
def test_fresh_residency_check_before_and_after_warmup(tmp_path, fake_bank, stage, loss):
    made, world = Gated("none"), roomy()

    def change():
        if loss == "split":
            world.ps = {"orion": (16, 12), "embed": (1, 1)}
        elif loss == "missing":
            world.ps = {"embed": (1, 1)}
        elif loss == "wow":
            world.wow = False
        else:
            world.free = 0.5

    if stage == "after_load":
        made.after_start = change
    else:
        made.after_warmup = change

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made, world=world) as rig:
            await rig.seat(101)
            await until(rig, lambda: made.stops and rig.service._expressive_task is None)
            assert not made.up and not rig.service._cuda_ready and TURBO not in rig.guard.holders
            assert len(made.warmups) == (0 if stage == "after_load" else 1)
    run(exercise())


def test_expired_reservation_cannot_be_confirmed_by_warmup(tmp_path, fake_bank):
    made = Gated()

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            holder = rig.guard.holders[TURBO]
            await rig.clock.advance(gpu.RESERVE_S + 1)
            await eventually(lambda: made.stops and not rig.service._cuda_phases)
            assert not holder.confirmed and not made.up and not rig.service._cuda_ready
            assert TURBO not in rig.guard.holders
    run(exercise())


def test_cancelled_admission_releases_just_acquired_reservation(tmp_path, fake_bank):
    made = Gated("none")

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            admitted = asyncio.Event()
            wait = asyncio.Event()
            original = rig.guard.acquire

            async def acquire(*args, **kwargs):
                result = await original(*args, **kwargs)
                admitted.set()
                await wait.wait()
                return result

            rig.guard.acquire = acquire
            await rig.seat(101)
            await until(rig, admitted.is_set)
            task = rig.service._expressive_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert TURBO not in rig.guard.holders and not rig.service._cuda_ready
            assert made.loads == 0
    run(exercise())


def test_cancelled_warmup_is_stopped_and_its_future_still_observed(tmp_path, fake_bank):
    made = Gated()

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, made.entered.is_set)
            task = rig.service._expressive_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await eventually(lambda: not rig.service._cuda_phases)
            assert not made.up and not rig.service._cuda_ready and TURBO not in rig.guard.holders
    run(exercise())


def test_stale_completion_cannot_clear_a_new_start_task(tmp_path, fake_bank):
    async def exercise():
        async with Rig22(tmp_path, fake_bank, Stand(up=False)) as rig:
            old = asyncio.create_task(asyncio.sleep(0))
            await old
            current = asyncio.create_task(asyncio.sleep(60))
            rig.service._expressive_task = current
            rig.service._expressive_settled(old)
            assert rig.service._expressive_task is current
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)
            rig.service._expressive_task = None
    run(exercise())


def test_old_holder_cannot_confirm_drop_or_release_replacement(tmp_path, fake_bank):
    async def exercise():
        async with Rig22(tmp_path, fake_bank, Stand(up=False)) as rig:
            await rig.snapshot()
            calls = []
            assert await rig.guard.acquire("owned", 1, lambda: calls.append("old")) is None
            old = rig.guard.holders["owned"]
            rig.guard.hold("owned", lambda: calls.append("new"))
            current = rig.guard.holders["owned"]
            assert not rig.guard.confirm("owned", expected=old)
            rig.guard.drop("owned", expected=old)
            rig.guard.release("owned", "old release", expected=old)
            assert rig.guard.holders["owned"] is current and not calls
            assert await rig.guard.validate("owned", old) == "reservation lost"
    run(exercise())


def test_preemption_cannot_reserve_after_session_changes_during_unload(tmp_path, fake_bank):
    async def exercise():
        world = roomy()
        world.free = 2.6
        async with Rig22(tmp_path, fake_bank, Stand(up=False), world=world) as rig:
            await rig.snapshot()
            unloading, release = asyncio.Event(), asyncio.Event()

            async def unload():
                unloading.set()
                await release.wait()

            rig.guard.hold("lower", unload, need_gb=1.5, priority=0)
            request = asyncio.create_task(rig.guard.acquire("new", 3, lambda: None))
            await unloading.wait()
            rig.service._on_session(Session(False, 1, "orion", "embed"))
            release.set()
            assert await request == "session changed"
            assert "new" not in rig.guard.holders
    run(exercise())


def test_guard_timeout_cannot_hide_in_progress_worker_cleanup(tmp_path, fake_bank, monkeypatch):
    monkeypatch.setattr(gpu, "UNLOAD_TIMEOUT_S", 0.02)
    made = Gated("none")
    stopping, release = threading.Event(), threading.Event()
    original = made.stop

    def stop():
        stopping.set()
        if not release.wait(5):
            raise EngineError("fake stop wait expired")
        original()

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            await rig.seat(101)
            await until(rig, lambda: rig.service._cuda_ready)
            made.stop = stop
            try:
                rig.guard.release_all("test revoke")
                await rig.guard.settle()
                assert stopping.is_set()
                assert not rig.service._cuda_stop_task.done()
                assert not rig.service._cuda_stop_task.cancelled()
                loads = made.loads
                await rig.clock.advance(gpu.RETRY_S + 1)
                assert made.loads == loads and not rig.service._cuda_ready
            finally:
                release.set()
            await eventually(lambda: rig.service._cuda_stop_task.done() and not made.up)
    run(exercise())


def test_final_validation_cannot_publish_readiness_after_deadline(tmp_path, fake_bank, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    made = Gated("none")

    async def exercise():
        async with Rig22(tmp_path, fake_bank, made) as rig:
            original, checks = rig.guard.validate, []

            async def validate(*args):
                result = await original(*args)
                checks.append(1)
                if len(checks) == 2:
                    # The await completes successfully, but the publication boundary is late.
                    now[0] += service.CUDA_STARTUP_BUDGET_S + 1
                return result

            rig.guard.validate = validate
            await rig.seat(101)
            await until(rig, lambda: made.stops and rig.service._expressive_task is None)
            assert len(checks) == 2 and len(made.warmups) == 1
            assert not rig.service._cuda_ready and TURBO not in rig.guard.holders
            assert "deadline" in rig.service._expressive_why
    run(exercise())


@pytest.mark.parametrize("reason", sorted(service.CUDA_MISSING_MODELS))
def test_missing_model_grace_is_not_renewed_and_resets_only_for_new_context(
        tmp_path, fake_bank, monkeypatch, reason):
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def exercise():
        async with Rig22(tmp_path, fake_bank, Stand(up=False)) as rig:
            s, context = rig.service, (1, 1, "orion", "embed")
            assert s._defer_cpu_for_missing_models(reason, context)
            assert s._cuda_missing_until == 120.0
            now[0] = 119.0
            assert s._defer_cpu_for_missing_models(reason, context)
            assert s._cuda_missing_until == 120.0
            now[0] = 120.0
            assert not s._defer_cpu_for_missing_models(reason, context)
            now[0] = 200.0
            assert not s._defer_cpu_for_missing_models(reason, context)
            new_context = (2, 1, "orion", "embed")
            assert s._defer_cpu_for_missing_models(reason, new_context)
            assert s._cuda_missing_until == 220.0
            assert not s._defer_cpu_for_missing_models("2.0 GB free, need 4.0", new_context)
            assert not s._defer_cpu_for_missing_models(reason, new_context)
    run(exercise())


def test_models_arriving_during_grace_use_cuda_without_starting_cpu(tmp_path, fake_bank,
                                                                 monkeypatch):
    from test_cpu_fallback import Cpu, CpuRig
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service, "available_ram_gib", lambda: 20.0)
    world, cpu = roomy(), Cpu()
    world.ps = {"embed": (1, 1)}

    async def exercise():
        async with CpuRig(tmp_path, fake_bank, cpu, world=world) as rig:
            await rig.seat(101)
            await until(rig, lambda: rig.service._cuda_missing_until is not None)
            assert cpu.loads == rig.expressive.loads == 0
            await rig.mind.send(rig.speak("waiting", text="Still here while things load."))
            await rig.sees("waiting", "started")
            assert rig.engine.live() == ["Still here while things load."]
            now[0] = 110.0
            world.ps = {"orion": (16, 16), "embed": (1, 1)}
            await until(rig, lambda: rig.service._cuda_ready)
            assert cpu.loads == 0 and rig.expressive.loads == 1
            assert rig.service._expressive_device() == "cuda"
    run(exercise())


@pytest.mark.parametrize("exhaust", [True, False])
def test_cpu_starts_after_grace_exhaustion_or_real_vram_refusal_without_promotion(
        tmp_path, fake_bank, monkeypatch, exhaust):
    from test_cpu_fallback import Cpu, CpuRig
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(service, "available_ram_gib", lambda: 20.0)
    world, cpu = roomy(), Cpu()
    world.ps = {"embed": (1, 1)}

    async def exercise():
        async with CpuRig(tmp_path, fake_bank, cpu, world=world) as rig:
            await rig.seat(101)
            await until(rig, lambda: rig.service._cuda_missing_until is not None)
            assert cpu.loads == 0
            now[0] = 120.0 if exhaust else 105.0
            if not exhaust:
                world.ps = {"orion": (16, 16), "embed": (1, 1)}
                world.free = 2.0
            await until(rig, lambda: rig.service._cpu_active)
            assert cpu.loads == 1 and rig.expressive.loads == 0
            assert rig.service._cuda_missing_closed
            world.ps, world.free, now[0] = {"orion": (16, 16), "embed": (1, 1)}, 8.0, 200.0
            await rig.clock.advance(60)
            assert rig.service._cpu_active and rig.expressive.loads == 0
    run(exercise())


@pytest.mark.parametrize("resource", ["vram", "unreadable", "wow"])
def test_missing_model_does_not_hide_an_independent_resource_failure(tmp_path, fake_bank,
                                                                   monkeypatch, resource):
    from test_cpu_fallback import Cpu, CpuRig
    monkeypatch.setattr(service, "available_ram_gib", lambda: 20.0)
    world, cpu = roomy(), Cpu()
    world.ps = {"embed": (1, 1)}
    if resource == "vram":
        world.free = 2.0
    elif resource == "unreadable":
        world.free = None
    else:
        world.wow = False

    async def exercise():
        async with CpuRig(tmp_path, fake_bank, cpu, world=world) as rig:
            await rig.seat(101)
            await until(rig, lambda: rig.service._cpu_active)
            assert rig.service._cuda_missing_until is None
            assert cpu.loads == 1 and rig.expressive.loads == 0
    run(exercise())


@pytest.mark.parametrize("when", ["before_start", "during_previous_stop"])
def test_revoked_engine_start_never_constructs_a_worker(tmp_path, monkeypatch, when):
    cancelled = threading.Event()
    made = expressive.ExpressiveEngine(TURBO, tmp_path / "python", need_gb=3)
    monkeypatch.setattr(expressive, "WorkerProcess",
                        lambda *a, **kw: pytest.fail("cancelled start constructed a worker"))
    if when == "before_start":
        cancelled.set()
    else:
        monkeypatch.setattr(made, "stop", cancelled.set)
    with pytest.raises(EngineError, match="startup cancelled"):
        made.start(cancelled=cancelled)


def test_stop_before_worker_assignment_prevents_late_process_start(tmp_path, monkeypatch):
    cancelled, constructing, release = threading.Event(), threading.Event(), threading.Event()
    workers = []

    class Worker:
        up = False

        def __init__(self, *args, **kwargs):
            self.starts = self.stops = 0
            workers.append(self)
            constructing.set()
            assert release.wait(5)

        def start(self):
            self.starts += 1
            self.up = True

        def stop(self):
            self.stops += 1
            self.up = False

    monkeypatch.setattr(expressive, "WorkerProcess", Worker)
    made = expressive.ExpressiveEngine(TURBO, tmp_path / "python", need_gb=3)

    async def exercise():
        task = asyncio.create_task(asyncio.to_thread(made.start, cancelled=cancelled))
        try:
            await eventually(constructing.is_set)
            cancelled.set()
            made.stop()  # exact race: the new worker has not been assigned yet
            assert made.worker is None
        finally:
            release.set()
        outcome, = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, EngineError) and "startup cancelled" in str(outcome)
        assert made.worker is None and workers[0].starts == 0 and workers[0].stops == 1
    run(exercise())


def test_stop_after_worker_publication_uses_captured_stopped_worker(tmp_path, monkeypatch):
    cancelled, entering, release = threading.Event(), threading.Event(), threading.Event()
    workers = []

    class Worker:
        up = False

        def __init__(self, *args, **kwargs):
            self.stopped = False
            workers.append(self)

        def start(self):
            entering.set()
            assert release.wait(5)
            if self.stopped:
                raise EngineError("fake worker stopped before spawning")
            self.up = True

        def stop(self):
            self.stopped, self.up = True, False

    monkeypatch.setattr(expressive, "WorkerProcess", Worker)
    made = expressive.ExpressiveEngine(TURBO, tmp_path / "python", need_gb=3)

    async def exercise():
        task = asyncio.create_task(asyncio.to_thread(made.start, cancelled=cancelled))
        try:
            await eventually(entering.is_set)
            assert made.worker is workers[0]
            cancelled.set()
            made.stop()  # clears self.worker while its captured start is still in flight
        finally:
            release.set()
        outcome, = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, EngineError)
        assert made.worker is None and workers[0].stopped and not workers[0].up
    run(exercise())
