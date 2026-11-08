"""The new tuning controls in a real browser; isolated console and fake-model mind only."""
import asyncio
from pathlib import Path

import pytest

from console.rig import KEY, make_app, running
from leeroy_mind.endpoint import tune_args
from tuning.test_tuning_preview import _app


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_nudge_controls_at_phone_width(tmp_path, theme):
    pw = pytest.importorskip("playwright.async_api", reason="Install Playwright for browser checks")
    async def go():
        async with pw.async_playwright() as driver:
            if not Path(driver.chromium.executable_path).is_file():
                pytest.skip("Install Chromium with python -m playwright install chromium")
            mind = _app(tmp_path, loaded=False)
            loop = asyncio.get_running_loop()
            class Link:
                def locate(self, pids):
                    return {"pid": 101, "port": 1, "key": "synthetic"}
                def call(self, pids, op, args=None, *, timeout=3):
                    if op == "tune":
                        return asyncio.run_coroutine_threadsafe(
                            mind.tuning.handle(tune_args(args)), loop).result(timeout)
                    return {"ok": True}
            console = make_app(tmp_path, mind=running(tmp_path), link=Link(), clock=mind.clock.now)
            await console.start()
            browser = await driver.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(f"localStorage.setItem('guild-console-theme', '{theme}'); Date.now = () => {int(mind.clock.now() * 1000)};")
            try:
                await page.goto(f"http://127.0.0.1:{console.s.port}/#key={KEY}")
                await page.locator("#t-personas").click()
                await page.locator("#p-wall .tile-detail[data-guid='102']").click()
                panel = page.locator("#p-dials .nudges")
                await panel.get_by_label("Nudge (1–200 characters)").fill("Be excited about the raid")
                await panel.get_by_label("Where it applies").select_option("whispers")
                await panel.get_by_role("button", name="Add nudge", exact=True).click()
                await page.locator("#p-wall .tile[data-guid='102'] .nudge-badge").wait_for()
                await panel.get_by_role("button", name="Retire", exact=True).click()
                await pw.expect(page.locator("#p-wall .tile[data-guid='102'] .nudge-badge")).to_have_count(0)
                await panel.locator("summary").click()
                await panel.get_by_role("button", name="Undo", exact=True).first.click()
                await page.locator("#p-wall .tile[data-guid='102'] .nudge-badge").wait_for()
                await panel.get_by_label("Nudge (1–200 characters)").fill("Think about tonight")
                await panel.get_by_role("button", name="Preview a line", exact=True).click()
                await pw.expect(page.locator("#t-preview")).to_contain_text("isn't loaded")
                assert await panel.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                assert not errors
                assert not mind.arbiter.admin.calls
            finally:
                await browser.close()
                await console.close()
                mind.store.close()
    asyncio.run(go())
