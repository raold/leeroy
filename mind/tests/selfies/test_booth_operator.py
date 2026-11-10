import pytest

from conftest import run
from .test_photo_booth import app, advance
from leeroy_mind.selfies.store import ready_set


def test_manual_booth_keeps_wait_and_renders_with_fake_worker(app):
    async def go():
        with pytest.raises(ValueError,match="ten minutes"): await app.selfie_booth.request()
        await advance(app,600)
        task = await app.selfie_booth.request()
        await task
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
