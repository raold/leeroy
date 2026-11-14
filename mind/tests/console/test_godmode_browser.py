"""Godmode edits in a real browser, beside the nudges; isolated console and fake-model mind only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from console.rig import KEY, make_app, running
from leeroy_mind.endpoint import tune_args
from tuning.test_tuning_preview import _app


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_godmode_edits_are_their_own_region_at_phone_width(tmp_path, theme):
    pw = pytest.importorskip("playwright.async_api", reason="Install Playwright for browser checks")
    async def go():
        async with pw.async_playwright() as driver:
            if not Path(driver.chromium.executable_path).is_file():
                pytest.skip("Install Chromium with python -m playwright install chromium")
            mind = _app(tmp_path, loaded=False)
            mind.godmode_config = SimpleNamespace(enabled=True)
            with (tmp_path / "mind.toml").open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n[godmode]\nenabled = true\n")  # the console reads it at its start
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
                await pw.expect(page.locator("#p-dials .nudges")).to_have_count(1)
                edits = page.get_by_role("region", name="Godmode edits")
                await pw.expect(edits).to_have_count(1)
                await pw.expect(edits).to_contain_text("No edits in force.")
                await edits.get_by_label("Kind").select_option("mood")
                await edits.get_by_label("Mood shift (-1 to 1)").fill("0.4")
                await edits.get_by_label("For hours (1 to 336)").fill("2")
                await edits.get_by_role("button", name="Make it so", exact=True).click()
                await pw.expect(edits.locator(".godmode-row")).to_contain_text("Mood +0.4")
                await pw.expect(page.locator("#p-dials .nudges")).to_have_count(1)
                await edits.get_by_role("button", name="Retire", exact=True).click()
                await pw.expect(edits).to_contain_text("No edits in force.")
                assert await edits.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                assert not errors
                assert not mind.arbiter.admin.calls
            finally:
                await browser.close()
                await console.close()
                mind.store.close()
    asyncio.run(go())
