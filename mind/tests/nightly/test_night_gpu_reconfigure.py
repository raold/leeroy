"""Runtime model reloads use only their own old/new models and never a real server."""
import asyncio
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.nightly.gpu import GpuArbiter, norm
from nightly.conftest import FakeAdmin


def test_reload_retains_old_models_until_reconciled_and_keeps_foreign_and_embedding(h):
    settings = replace(h.settings, live_model="next-live", reflection_model="next-reflection")
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB, "unrelated:latest", "next-reflection:latest"])
    arb = GpuArbiter(admin, h.settings)
    arb.hold("human")
    assert arb.reconfigure(settings)
    assert admin.calls == [] and arb.holders == {"human"}
    run(arb.hand_over())
    assert admin.loaded == [h.EMB, "unrelated:latest", "next-live"]
    assert admin.calls == [("unload", h.LIVE), ("unload", h.REFL),
                           ("unload", "next-reflection:latest"),
                           ("preload", "next-live", -1)]
    assert set(arb._managed) == {"next-live:latest", "next-reflection:latest"}


def test_ensure_after_reload_clears_retired_models_without_loading_the_new_target(h):
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB, "unrelated:latest"])
    arb = GpuArbiter(admin, h.settings)
    arb.reconfigure(replace(h.settings, live_model="next-live", reflection_model="next-reflection"))
    run(arb.ensure_model("next-reflection"))
    assert admin.loaded == [h.EMB, "unrelated:latest"]
    assert all(call[0] == "unload" for call in admin.calls)
    assert set(arb._managed) == {"next-live:latest", "next-reflection:latest"}


@pytest.mark.parametrize("change", [{"embedding_model": "another-embedding"},
                                   {"ollama_url": "http://127.0.0.1:19999"}])
def test_endpoint_or_embedding_change_is_rejected_atomically(h, change):
    admin = FakeAdmin([h.REFL, h.EMB])
    arb = GpuArbiter(admin, h.settings)
    with pytest.raises(ValueError, match="requires a restart"):
        arb.reconfigure(replace(h.settings, live_model="next-live", **change))
    assert arb._revision == 0 and arb._live == h.LIVE
    assert arb._reflection == h.REFL and admin.calls == []
    assert set(arb._managed) == {norm(h.LIVE), norm(h.REFL)}


def test_alias_only_reload_is_noop_and_never_unloads_embedding_even_when_misconfigured(h):
    settings = replace(h.settings, live_model="live", reflection_model=h.EMB)
    admin = FakeAdmin(["live:latest", h.EMB, "unrelated:latest"])
    arb = GpuArbiter(admin, settings)
    assert not arb.reconfigure(replace(settings, live_model="live:latest",
                                      embedding_model=norm(h.EMB),
                                      ollama_url=settings.ollama_url.rstrip("/") + "/"))
    run(arb.ensure_model("some-target"))
    assert admin.loaded == [h.EMB, "unrelated:latest"]
    assert admin.calls == [("unload", "live:latest")]


def test_reload_during_preempt_hands_back_only_the_latest_live_model(h):
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB])
    arb = GpuArbiter(admin, h.settings)
    async def go():
        started, finish = asyncio.Event(), asyncio.Event()
        async def preempt():
            started.set()
            await finish.wait()
        arb.set_preempt(preempt)
        task = asyncio.create_task(arb.acquire_live("human"))
        await started.wait()
        arb.reconfigure(replace(h.settings, live_model="next-live"))
        finish.set()
        await task
    run(go())
    assert admin.loaded == [h.EMB, "next-live"]
    assert [c for c in admin.calls if c[0] == "preload"] == [("preload", "next-live", -1)]


def test_reload_during_old_preload_reconciles_again_before_handback_returns(h):
    async def go():
        started, finish = asyncio.Event(), asyncio.Event()
        class SlowAdmin(FakeAdmin):
            async def preload(self, model, keep_alive, **kwargs):
                if norm(model) == norm(h.LIVE):
                    started.set()
                    await finish.wait()
                await super().preload(model, keep_alive, **kwargs)
        admin = SlowAdmin([h.REFL, h.EMB, "unrelated:latest"])
        arb = GpuArbiter(admin, h.settings)
        task = asyncio.create_task(arb.acquire_live("human"))
        await started.wait()
        arb.reconfigure(replace(h.settings, live_model="next-live"))
        finish.set()
        await task
        assert admin.loaded == [h.EMB, "unrelated:latest", "next-live"]
        assert admin.calls[-2:] == [("unload", h.LIVE), ("preload", "next-live", -1)]
        assert arb.holders == {"human"}
    run(go())


def test_reload_during_residency_probe_never_unloads_the_new_live_target(h):
    async def go():
        started, finish = asyncio.Event(), asyncio.Event()
        class SlowAdmin(FakeAdmin):
            first = True
            async def loaded_models(self):
                if self.first:
                    self.first = False
                    started.set()
                    await finish.wait()
                return await super().loaded_models()
        admin = SlowAdmin([h.LIVE, h.REFL, h.EMB])
        arb = GpuArbiter(admin, h.settings)
        task = asyncio.create_task(arb.acquire_live("human"))
        await started.wait()
        arb.reconfigure(replace(h.settings, live_model=h.REFL, reflection_model=h.LIVE))
        finish.set()
        await task
        assert admin.loaded == [h.REFL, h.EMB]
        assert ("unload", h.REFL) not in admin.calls
    run(go())


def test_reload_during_ensure_rejects_the_stale_context(h):
    async def go():
        started, finish = asyncio.Event(), asyncio.Event()
        class SlowAdmin(FakeAdmin):
            async def loaded_models(self):
                started.set()
                await finish.wait()
                return await super().loaded_models()
        admin = SlowAdmin([h.LIVE, h.REFL, h.EMB])
        arb = GpuArbiter(admin, h.settings)
        task = asyncio.create_task(arb.ensure_model(h.REFL))
        await started.wait()
        arb.reconfigure(replace(h.settings, live_model="next-live"))
        finish.set()
        with pytest.raises(RuntimeError, match="models changed"):
            await task
        assert admin.calls == []
        await arb.ensure_model(h.REFL)
        assert admin.loaded == [h.REFL, h.EMB]
    run(go())


def test_failed_unload_retains_retired_ownership_for_retry(h):
    class FlakyAdmin(FakeAdmin):
        fail = True
        async def unload(self, model):
            if self.fail:
                raise RuntimeError("unavailable")
            await super().unload(model)
    admin = FlakyAdmin([h.LIVE, h.REFL, h.EMB])
    arb = GpuArbiter(admin, h.settings)
    arb.reconfigure(replace(h.settings, live_model="next-live", reflection_model="next-reflection"))
    with pytest.raises(RuntimeError, match="unavailable"):
        run(arb.acquire_live("human"))
    assert norm(h.LIVE) in arb._managed and norm(h.REFL) in arb._managed
    admin.fail = False
    run(arb.hand_over())
    assert admin.loaded == [h.EMB, "next-live"]
    assert set(arb._managed) == {"next-live:latest", "next-reflection:latest"}
