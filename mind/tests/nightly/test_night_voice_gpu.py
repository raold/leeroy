"""Voice GPU ownership prevents a night from entering while a remote worker owns VRAM."""
import asyncio
import logging
from types import SimpleNamespace

from conftest import run
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.voice import wire
from leeroy_mind.voice.attach import VOICE_GPU, VoiceExtension
from leeroy_mind.voice.config import VoiceCfg
from nightly.conftest import FakeAdmin


class Pins:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.gate = None

    async def pin(self, live, embedding):
        self.calls.append("pin")
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()

    async def unpin(self, live, embedding):
        self.calls.append("unpin")


def rig(h, *, arbiter=True):
    cfg = SimpleNamespace(ollama=SimpleNamespace(url=h.settings.ollama_url),
                          models=SimpleNamespace(live=h.LIVE, embedding=h.EMB))
    app = SimpleNamespace(clock=h.clock, cfg=cfg, human_guid=1)
    admin = FakeAdmin([h.REFL, h.EMB])
    if arbiter:
        app.arbiter = GpuArbiter(admin, h.settings)
    ext = VoiceExtension(app, VoiceCfg())
    ext.gpu_pins = Pins()
    return ext, admin


def status(gpu):
    return wire.Status(True, "ok", gpu, "parakeet", True, 0, ())


async def drain(ext):
    while tasks := set(ext._gpu_tasks) | set(ext._gpu_handbacks):
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)


def test_gpu_status_holds_inline_and_handback_does_not_block_receive(h):
    async def go():
        ext, admin = rig(h)
        started, finish = asyncio.Event(), asyncio.Event()
        async def preempt():
            started.set()
            await finish.wait()
        ext.app.arbiter.set_preempt(preempt)
        await asyncio.wait_for(ext.on_message(status("chatterbox")), 1)
        assert ext.app.arbiter.holders == {VOICE_GPU}
        assert not ext.app.arbiter.reflection_allowed()
        await started.wait()
        assert admin.calls == []
        await asyncio.wait_for(ext.on_message(status("chatterbox")), 1)
        assert len(ext._gpu_handbacks) == 1
        finish.set()
        await drain(ext)
        assert admin.loaded == [h.EMB, h.LIVE]
        await ext._close_gpu()
    run(go())


def test_cpu_status_does_not_take_a_gpu_lease_or_touch_models(h):
    async def go():
        ext, admin = rig(h)
        await ext.on_message(status("none"))
        assert ext.app.arbiter.reflection_allowed()
        assert not ext._gpu_tasks and not ext._gpu_handbacks
        assert admin.calls == [] and ext.gpu_pins.calls == []
    run(go())


def test_human_and_voice_leases_are_independent(h):
    async def go():
        ext, admin = rig(h)
        ext.app.arbiter.hold("human")
        await ext._on_gpu("whisper")
        assert ext.app.arbiter.holders == {"human", VOICE_GPU}
        await drain(ext)
        assert admin.calls == []
        await ext._on_gpu("none")
        await drain(ext)
        assert ext.app.arbiter.holders == {"human"}
        assert ext.gpu_pins.calls == ["pin", "unpin"]
    run(go())


def test_none_waits_for_pending_pin_and_repeated_none_does_not_queue_more_jobs(h):
    async def go():
        ext, _ = rig(h)
        ext.gpu_pins.gate = asyncio.Event()
        await ext._on_gpu("chatterbox")
        await ext.gpu_pins.started.wait()
        await ext._on_gpu("none")
        for _ in range(20):
            await ext._on_gpu("none")
        assert len(ext._gpu_tasks) == 2
        assert ext.app.arbiter.holders == {VOICE_GPU}
        ext.gpu_pins.gate.set()
        await drain(ext)
        assert ext.gpu_pins.calls == ["pin", "unpin"]
        assert ext.app.arbiter.reflection_allowed()
    run(go())


def test_disconnect_retains_lease_and_next_explicit_none_releases_it(h):
    async def go():
        ext, _ = rig(h)
        await ext._on_gpu("chatterbox")
        await drain(ext)
        await ext.on_connection(False)
        assert ext.app.arbiter.holders == {VOICE_GPU}
        assert not ext._gpu_held  # the next connection repins its confirmed GPU state
        await ext._on_gpu("none")
        await drain(ext)
        assert ext.app.arbiter.reflection_allowed()
        assert ext.gpu_pins.calls == ["pin", "unpin"]
    run(go())


def test_disconnect_invalidates_pending_release_until_a_new_status(h):
    async def go():
        ext, _ = rig(h)
        ext.gpu_pins.gate = asyncio.Event()
        await ext._on_gpu("chatterbox")
        await ext.gpu_pins.started.wait()
        await ext._on_gpu("none")
        await ext.on_connection(False)
        await asyncio.gather(*ext._gpu_tasks, return_exceptions=True)
        assert ext.app.arbiter.holders == {VOICE_GPU}
        ext.gpu_pins.gate.set()
        await ext._on_gpu("none")
        await drain(ext)
        assert ext.app.arbiter.reflection_allowed()
    run(go())


def test_gpu_reacquired_before_unpin_finishes_does_not_release_new_hold(h):
    async def go():
        ext, _ = rig(h)
        ext.gpu_pins.gate = asyncio.Event()
        await ext._on_gpu("chatterbox")
        await ext.gpu_pins.started.wait()
        await ext._on_gpu("none")
        await ext._on_gpu("whisper")
        ext.gpu_pins.gate.set()
        await drain(ext)
        assert ext.app.arbiter.holders == {VOICE_GPU}
        assert ext.gpu_pins.calls == ["pin", "unpin", "pin"]
        await ext._close_gpu()
    run(go())


def test_shutdown_cancels_admin_tasks_and_releases_only_its_own_lease(h):
    async def go():
        ext, _ = rig(h)
        started = asyncio.Event()
        async def preempt():
            started.set()
            await asyncio.Event().wait()
        ext.app.arbiter.set_preempt(preempt)
        ext.gpu_pins.gate = asyncio.Event()
        await ext._on_gpu("chatterbox")
        await started.wait()
        await ext.gpu_pins.started.wait()
        ext.app.arbiter.hold("human")
        tasks = set(ext._gpu_tasks) | set(ext._gpu_handbacks)
        await asyncio.wait_for(ext._close_gpu(), 1)
        assert all(t.done() for t in tasks)
        assert ext.app.arbiter.holders == {"human"}
        await ext._on_gpu("chatterbox")
        assert ext.app.arbiter.holders == {"human"}
        await ext._close_gpu()
    run(go())


def test_failed_handback_keeps_lease_and_logs_only_error_type(h, caplog):
    async def go():
        ext, _ = rig(h)
        async def fail():
            raise ValueError("private content")
        ext.app.arbiter.set_preempt(fail)
        await ext._on_gpu("chatterbox")
        while ext._gpu_handbacks:
            await asyncio.wait(ext._gpu_handbacks)
            await asyncio.sleep(0)
        assert ext.app.arbiter.holders == {VOICE_GPU}
        await ext._close_gpu()
    with caplog.at_level(logging.WARNING):
        run(go())
    assert "voice_handback_failed" in caplog.text and "ValueError" in caplog.text
    assert "private content" not in caplog.text


def test_standalone_voice_without_arbiter_preserves_pin_behavior(h):
    async def go():
        ext, _ = rig(h, arbiter=False)
        await ext._on_gpu("whisper")
        await drain(ext)
        await ext.on_connection(False)
        await ext._on_gpu("whisper")
        await drain(ext)
        await ext._on_gpu("none")
        await drain(ext)
        assert ext.gpu_pins.calls == ["pin", "pin", "unpin"]
        await ext._close_gpu()
    run(go())
