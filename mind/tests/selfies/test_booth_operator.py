import pytest

from conftest import run
from .test_photo_booth import app, advance
from leeroy_mind.selfies.store import ready_set


def test_manual_booth_keeps_wait_and_renders_with_fake_worker(app):
    async def go():
        with pytest.raises(ValueError,match="ten minutes"): await app.selfie_booth.request()
        await advance(app,600)
        task = await app.selfie_booth.request()
        assert await task == "done"
        assert ready_set(app.store,101,1)
        with pytest.raises(ValueError,match="No eligible"): await app.selfie_booth.request()
    run(go())


@pytest.mark.parametrize("case,words", [("lease","lease"),("night","night"),("cooldown","cooling"),("off","off")])
def test_manual_booth_refusals(app, case, words):
    from dataclasses import replace
    async def go():
        await advance(app,600)
        if case == "lease": app.arbiter._holders.add("test")
        if case == "night": app.night_scheduler.due = lambda: "due"
        if case == "cooldown":
            with app.store.transaction(): app.store.set_meta("selfies:booth:cooldown_until",str(app.clock.now()+100))
        if case == "off": app.selfie_booth.cfg = replace(app.selfie_booth.cfg,booth=False)
        with pytest.raises(ValueError,match=words): await app.selfie_booth.request()
        assert not app.selfie_booth.running
    run(go())


@pytest.mark.parametrize("case", ["lease", "cancel_before_start"])
def test_ops_render_uses_requested_pass_after_a_prior_success(app, case):
    from types import SimpleNamespace
    from leeroy_mind.selfies.booth import LAST
    from leeroy_mind.selfies.store import queue_more
    from test_opscontrol import ep

    async def go():
        await advance(app, 600)
        await (await app.selfie_booth.request())
        previous = app.store.get_meta(LAST)
        assert '"outcome": "done"' in previous
        more = queue_more(app.store, 101, now=app.clock.now())
        await advance(app, 600)
        original = app.selfie_booth.request

        async def interrupted():
            work = await original()
            if case == "lease":
                app.arbiter._holders.add("synthetic-live")
            else:
                work.cancel()
            return work

        app.selfie_booth.request = interrupted
        app.bridge = SimpleNamespace(connected=True)
        app.hello = SimpleNamespace(server="synthetic", version="test", ts=1)
        endpoint = ep(app)
        reply = await endpoint.ops.answer({"action": "render"})
        assert reply["ok"]
        await endpoint.ops.task
        assert endpoint.ops.operation["state"] == "failed"
        assert "yielded" in endpoint.ops.operation["message"]
        assert not ready_set(app.store, 101, more.set_no)
        assert app.store.get_meta(LAST) == previous

    run(go())
