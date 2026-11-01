"""Opt-in live CPU Turbo: fake engines/devices, real service scheduling, no models."""
import asyncio
from dataclasses import replace
import threading

import numpy as np
import pytest

from conftest import eventually, run
from leeroy_voice import __main__ as cli
from leeroy_voice import service
from leeroy_voice.config import ConfigError, load_config
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.expressive import ExpressiveEngine
from leeroy_voice.wire import Status
from test_expressive import Rig22, Stand, TURBO
from test_gpu import World, until


class Cpu(Stand):
    def __init__(self):
        super().__init__(up=False)
        self.budgets = []

    def synth(self, text, spec, style, speed, *, timeout_s=None):
        self.budgets.append(timeout_s)
        return super().synth(text, spec, style, speed)


class CpuRig(Rig22):
    def __init__(self, tmp_path, bank, cpu, *, enabled=True, world=None):
        if world is None:
            world = World()
            world.free = 2.0
        super().__init__(tmp_path, bank, Stand(up=False), world=world)
        self.cfg = replace(self.cfg, tts_cpu_fallback=enabled)
        self.service._cfg = self.cfg
        self.service.attach_expressive(self.expressive, cpu_fallback=cpu)


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    monkeypatch.setattr(service, "available_ram_gib", lambda: 20.0)


def go(tmp_path, bank, cpu, body, **kwargs):
    async def main():
        async with CpuRig(tmp_path, bank, cpu, **kwargs) as rig:
            await body(rig)
    run(main(), 20.0)


def test_default_still_refuses_cpu_when_gpu_does_not_fit(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.world.asked >= 2)
        await rig.mind.send(rig.speak("plain"))
        await rig.sees("plain", "started")
        assert cpu.loads == 0 and rig.engine.live() == ["hello there everyone"]
    go(tmp_path, fake_bank, cpu, body, enabled=False)


def test_cpu_preserves_two_sentences_without_gpu_lease_or_segment_gap(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        await rig.mind.send(rig.speak("reply", text="We made it. Let us try again.", ttl=30))
        await rig.sees("reply", "started")
        assert [text for text, _ in cpu.said] == ["We made it. Let us try again."]
        assert len(rig.service._jobs["reply"].line.segments) == 1
        assert rig.engine.live() == [] and rig.guard.holders == {}
        assert rig.expressive.loads == 0
        assert rig.service._status().gpu == "none"
        assert rig.service.state()["expressive_device"] == "cpu"
        assert all(status.gpu == "none" for status in rig.got(Status))
        assert cpu.budgets == [15.0]
    go(tmp_path, fake_bank, cpu, body)
    assert not cpu.up


def test_cpu_budget_leaves_time_for_fallback_and_urgent_lines_skip_it(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        await rig.mind.send(rig.speak("short", ttl=11))
        await rig.sees("short", "started")
        assert 3.5 < cpu.budgets[0] <= 4.0
        await rig.play_out()
        await rig.tick(.5)
        await rig.mind.send(rig.speak("urgent", text="A sudden threat approaches", priority="p1", ttl=3))
        await rig.sees("urgent", "started")
        assert len(cpu.said) == 1
        assert rig.engine.live() == ["A sudden threat approaches"]
    go(tmp_path, fake_bank, cpu, body)


def test_cpu_failure_falls_back_once_for_the_whole_reply(tmp_path, fake_bank):
    cpu = Cpu()
    cpu.fail = EngineError("CPU worker: no answer")

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        await rig.mind.send(rig.speak("failed", text="We made it. Let us try again.", ttl=20))
        await rig.sees("failed", "started")
        assert len(cpu.budgets) == 1
        assert rig.engine.live() == ["We made it. Let us try again."]
        assert rig.service._jobs["failed"].fell_back
    go(tmp_path, fake_bank, cpu, body)


def test_kokoro_member_keeps_all_sentences_while_cpu_turbo_is_warm(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(102)  # snapshot's member101 remains Kokoro
        await until(rig, lambda: rig.service._cpu_active)
        await rig.mind.send(rig.speak("baseline", 101, "We made it. Let us try again.", ttl=20))
        await rig.sees("baseline", "started")
        await eventually(lambda: len(rig.engine.live()) == 2)
        assert rig.engine.live() == ["We made it.", "Let us try again."]
        assert not rig.service._jobs["baseline"].cpu
        assert cpu.budgets == []
    go(tmp_path, fake_bank, cpu, body)


def test_cold_load_does_not_block_fallback_and_logout_discards_it(tmp_path, fake_bank):
    cpu, release, entered = Cpu(), threading.Event(), threading.Event()
    start = cpu.start

    def slow_start():
        entered.set()
        assert release.wait(5)
        start()
    cpu.start = slow_start

    async def body(rig):
        try:
            await rig.seat(101)
            await until(rig, entered.is_set)
            await rig.mind.send(rig.speak("cold", priority="p1", ttl=3))
            await rig.sees("cold", "started")
            assert rig.engine.live() == ["hello there everyone"]
            await rig.snapshot(online=False)
            await eventually(lambda: not rig.service.online)
        finally:
            release.set()
        await until(rig, lambda: rig.service._expressive_task is None)
        assert not cpu.up and not rig.service._cpu_active
        assert rig.guard.holders == {}
    go(tmp_path, fake_bank, cpu, body)


def test_cpu_unloads_on_logout_and_reloads_next_session(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        await rig.snapshot(online=False)
        await until(rig, lambda: not cpu.up)
        assert rig.service.state()["expressive_device"] == "none"
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        assert cpu.loads == 2
    go(tmp_path, fake_bank, cpu, body)


def test_gpu_still_wins_when_admitted(tmp_path, fake_bank):
    cpu, world = Cpu(), World()
    world.free = 8.0

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.expressive.up and TURBO in rig.guard.holders
                    and rig.guard.holders[TURBO].confirmed)
        assert cpu.loads == 0 and rig.guard.holders[TURBO].confirmed
        assert rig.service.state()["expressive_device"] == "cuda"
    go(tmp_path, fake_bank, cpu, body, world=world)


@pytest.mark.parametrize("available", [None, 11.0])
def test_insufficient_host_memory_keeps_cpu_unloaded(tmp_path, fake_bank, monkeypatch, available):
    monkeypatch.setattr(service, "available_ram_gib", lambda: available)
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.world.asked >= 2)
        assert cpu.loads == 0 and rig.guard.holders == {}
    go(tmp_path, fake_bank, cpu, body)


def test_host_memory_pressure_releases_cpu_without_touching_gpu(tmp_path, fake_bank, monkeypatch):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        monkeypatch.setattr(service, "available_ram_gib", lambda: 3.0)
        await until(rig, lambda: not cpu.up)
        assert not rig.service._cpu_active and rig.guard.holders == {}
    go(tmp_path, fake_bank, cpu, body)


def test_busy_cpu_does_not_delay_urgent_or_build_another_cpu_job(tmp_path, fake_bank):
    cpu, entered, release = Cpu(), threading.Event(), threading.Event()
    synth = cpu.synth

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return synth(*args, **kwargs)
    cpu.synth = slow

    async def body(rig):
        try:
            await rig.seat(101, 102)
            await until(rig, lambda: rig.service._cpu_active)
            await rig.mind.send(rig.speak("slow", 101, "A quiet conversation continues", ttl=30))
            await eventually(entered.is_set)
            await rig.mind.send(rig.speak("urgent", 102, "A sudden threat approaches",
                                          priority="p1", ttl=3))
            await rig.sees("urgent", "started")
            assert not release.is_set()
            assert rig.engine.live() == ["A sudden threat approaches"]
            assert rig.service._cpu_job.speak.id == "slow"
            overflow, _ = rig.service._job(rig.speak("overflow", 102, "Another thought", ttl=20))
            assert not overflow.cpu and not overflow.spoken
        finally:
            release.set()
        await until(rig, lambda: rig.service._cpu_job is None)
        assert len(cpu.budgets) == 1
    go(tmp_path, fake_bank, cpu, body)


def test_expired_cpu_handoff_is_discarded_without_synthesis(tmp_path, fake_bank):
    cpu = Cpu()

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: rig.service._cpu_active)
        job, _ = rig.service._job(rig.speak("stale", ttl=20))
        job.ended = True
        rig.service._cpu_job = job
        rig.service._cpu_queue.put(job)
        await eventually(lambda: rig.service._cpu_job is None)
        assert cpu.budgets == []
    go(tmp_path, fake_bank, cpu, body)


def test_live_loader_has_separate_limits_and_hides_cuda(tmp_path, monkeypatch):
    python = tmp_path / "engines" / TURBO / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(cli, "_require", lambda *args: None)
    cfg = replace(load_config(tmp_path / "absent"), tts_cpu_fallback=True, tts_cpu_threads=24)
    decision = Decision(engine=TURBO)
    live = cli.load_expressive(tmp_path, cfg, decision, device="cpu", refs_dir=tmp_path, live_cpu=True)
    offline = cli.load_expressive(tmp_path, cfg, decision, device="cpu", refs_dir=tmp_path)
    assert live._args[-1] == 24 and live._timeout_s == 15 and live._start_s == 60
    assert live._env["CUDA_VISIBLE_DEVICES"] == "" and live._below_normal
    assert offline._args[-1] == cli.CPU_THREADS and offline._timeout_s == cli.CPU_LINE_TIMEOUT_S


@pytest.mark.parametrize("key,value", [("cpu_fallback", "1"), ("cpu_threads", "25"),
                                      ("cpu_threads", "0"), ("cpu_timeout_s", "16"),
                                      ("cpu_timeout_s", "nan"), ("cpu_timeout_s", "0")])
def test_cpu_config_limits(tmp_path, key, value):
    path = tmp_path / "voice.toml"
    path.write_text(f"[tts]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_config(path)


def test_per_call_timeout_can_only_shorten_engine_limit(tmp_path, monkeypatch):
    engine = ExpressiveEngine(TURBO, tmp_path / "python", need_gb=3, timeout_s=15)
    budgets = []

    class Worker:
        up = True

        def ask(self, *args, timeout):
            budgets.append(timeout)
            return np.ones(240, dtype=np.float32), 24000
    engine.worker = Worker()
    monkeypatch.setattr(engine, "_reference", lambda spec: tmp_path / "reference.wav")
    engine.synth("hello", None, None, 1.0, timeout_s=4)
    engine.synth("hello", None, None, 1.0, timeout_s=99)
    assert budgets == [4, 15]
