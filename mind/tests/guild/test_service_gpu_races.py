"""Real arbiter bookkeeping with synthetic, paused model admission; no model I/O."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.ports import LivePorts
from leeroy_mind.guild.session import RaidPlan
from leeroy_mind.nightly.gpu import GpuArbiter, NullAdmin

from .test_service import app, ready


class PausingAdmin(NullAdmin):
    def __init__(self):
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def preload(self, model, keep_alive, *, options=None):
        self.entered.set()
        await self.resume.wait()


def arbiter(admin=None):
    return GpuArbiter(admin or NullAdmin(), SimpleNamespace(
        live_model="synthetic-live", reflection_model="synthetic-reflection",
        embedding_model="synthetic-embedding", ollama_url="http://127.0.0.1:1"))


def test_disconnect_revokes_offline_request_queued_behind_speech_admission(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        service.offline._online = False
        service.offline._plan = lambda: RaidPlan(
            "icc", 10, False, True, 101, 101, 104, list(range(101, 111)), [], False)
        admin = PausingAdmin()
        app.arbiter = arbiter(admin)

        async def speaking():
            try:
                return await service.ports.acquire_gpu("guild_speech")
            finally:
                await service.ports.release_gpu("guild_speech")

        speech = asyncio.create_task(speaking())
        await asyncio.wait_for(admin.entered.wait(), 1)
        offline = asyncio.create_task(service.offline._start())
        await asyncio.sleep(0)
        assert service.offline._lease
        assert app.arbiter.holders == {"guild_speech"}

        await service.connection(False)
        assert not service.offline._lease
        admin.resume.set()
        await asyncio.wait_for(asyncio.gather(speech, offline), 1)

        assert service.offline._owned["phase"] == "rejected"
        assert not service.offline.active and not service.offline._lease
        assert app.arbiter.holders == frozenset()
        assert app.arbiter.reflection_allowed()
        app.bridge.send.assert_not_awaited()
    asyncio.run(go())


@pytest.mark.parametrize("pause_at", ["model", "engine"])
def test_release_during_admission_returns_false_without_recreating_holder(app, pause_at):
    async def go():
        admin = PausingAdmin()
        app.arbiter = arbiter(admin if pause_at == "model" else None)
        if pause_at == "engine":
            async def admission():
                admin.entered.set()
                await admin.resume.wait()
            app.engine.live_admission = admission
        ports = LivePorts(app)
        pending = asyncio.create_task(ports.acquire_gpu("offline_raid"))
        await asyncio.wait_for(admin.entered.wait(), 1)
        assert app.arbiter.holders == {"offline_raid"}
        await ports.release_gpu("offline_raid")
        admin.resume.set()
        assert await asyncio.wait_for(pending, 1) is False
        assert not app.arbiter.holders and not ports._gpu_requests
    asyncio.run(go())


def test_cancelling_model_admission_releases_actual_holder_without_caller_cleanup(app):
    async def go():
        admin = PausingAdmin()
        app.arbiter = arbiter(admin)
        ports = LivePorts(app)
        pending = asyncio.create_task(ports.acquire_gpu("offline_raid"))
        await asyncio.wait_for(admin.entered.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert not app.arbiter.holders and not ports._gpu_requests
    asyncio.run(go())


def test_old_cancelled_acquisition_cannot_release_replacement_generation(app):
    async def go():
        admin = PausingAdmin()
        app.arbiter = arbiter(admin)
        ports = LivePorts(app)
        old = asyncio.create_task(ports.acquire_gpu("offline_raid"))
        await asyncio.wait_for(admin.entered.wait(), 1)
        await ports.release_gpu("offline_raid")
        new = asyncio.create_task(ports.acquire_gpu("offline_raid"))
        await asyncio.sleep(0)
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(old, 1)
        admin.resume.set()
        assert await asyncio.wait_for(new, 1) is True
        assert app.arbiter.holders == {"offline_raid"}
        await ports.release_gpu("offline_raid")
        assert not app.arbiter.holders and not ports._gpu_requests
    asyncio.run(go())


def test_release_uses_original_arbiter_after_app_replacement(app):
    async def go():
        original, replacement = arbiter(), arbiter()
        app.arbiter = original
        ports = LivePorts(app)
        assert await ports.acquire_gpu("offline_raid") is True
        app.arbiter = replacement
        await replacement.acquire_live("offline_raid")
        await ports.release_gpu("offline_raid")
        assert not original.holders
        assert replacement.holders == {"offline_raid"}
        await replacement.release_live("offline_raid")
        assert await ports.acquire_gpu("guild_speech") is True
        assert replacement.holders == {"guild_speech"} and not original.holders
        await ports.release_gpu("guild_speech")
    asyncio.run(go())


def test_explicit_arbiter_denial_never_reaches_engine_admission(app):
    async def go():
        app.arbiter = SimpleNamespace(
            acquire_live=AsyncMock(return_value=False), release_live=AsyncMock())
        app.engine.live_admission = AsyncMock()
        ports = LivePorts(app)
        assert await ports.acquire_gpu("offline_raid") is False
        app.engine.live_admission.assert_not_awaited()
        app.arbiter.release_live.assert_awaited_once_with("offline_raid")
        assert not ports._gpu_requests
    asyncio.run(go())


def test_revoked_queued_speech_admission_never_calls_engine(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        admin = PausingAdmin()
        app.arbiter = arbiter(admin)
        first = asyncio.create_task(service.ports.acquire_gpu("offline_raid"))
        await asyncio.wait_for(admin.entered.wait(), 1)
        service.ports.propose(101, "guild", "A completed raid recap.")
        speech = asyncio.create_task(service.speech_tick())
        await asyncio.sleep(0)
        await service.ports.release_gpu("guild_speech")
        admin.resume.set()
        assert await asyncio.wait_for(first, 1) is True
        await asyncio.wait_for(speech, 1)
        app.engine.speak.assert_not_awaited()
        assert service.source.take(app.clock.now()) is None
        assert app.arbiter.holders == {"offline_raid"}
        await service.ports.release_gpu("offline_raid")
        assert not app.arbiter.holders
    asyncio.run(go())


def test_actual_disconnect_revokes_speech_queued_behind_offline_model_admission(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        service.offline._online = False
        service.offline._plan = lambda: RaidPlan(
            "icc", 10, False, True, 101, 101, 104, list(range(101, 111)), [], False)
        admin = PausingAdmin()
        admin.preload = AsyncMock(wraps=admin.preload)
        app.arbiter = arbiter(admin)
        offline = asyncio.create_task(service.offline._start())
        await asyncio.wait_for(admin.entered.wait(), 1)
        service.ports.propose(101, "guild", "A completed raid recap.")
        speech = asyncio.create_task(service.speech_tick())
        await asyncio.sleep(0)
        assert set(service.ports._gpu_requests) == {"offline_raid", "guild_speech"}
        await service.connection(False)
        assert not service.ports._gpu_requests and not app.arbiter.holders
        admin.resume.set()
        await asyncio.wait_for(asyncio.gather(offline, speech), 1)
        assert admin.preload.await_count == 1
        app.engine.speak.assert_not_awaited()
        app.bridge.send.assert_not_awaited()
        assert not app.arbiter.holders and not service.offline._lease
    asyncio.run(go())


def test_disconnect_attempts_offline_cleanup_even_if_speech_release_fails(app):
    async def go():
        service = ready(app)
        service.ports.release_gpu = AsyncMock(side_effect=RuntimeError("synthetic cleanup failure"))
        service.offline._release = AsyncMock()
        with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
            await service.connection(False)
        service.offline._release.assert_awaited_once()
    asyncio.run(go())
