"""Optional browser check, confined to an owned synthetic console and fake probes."""
import asyncio
import sys

import pytest

from conftest import run
from console.test_console_ops_actions import served
from test_opscontrol import app
from leeroy_mind.console import opsview


def test_ops_preview_freezes_draft_discards_closed_dialog_and_saves_snapshot(app):
    pw = pytest.importorskip("playwright.async_api")
    async def go():
        async with served(app) as (c, endpoint), pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**({"channel":"msedge"} if sys.platform=="win32" else {}))
            except pw.Error as e:
                pytest.skip("Optional browser executable is unavailable: "+type(e).__name__)
            c._ops = opsview.OpsView(app.data_dir,app.data_dir,sources={name:lambda:opsview.panel() for name in opsview.PANELS})
            page = await browser.new_page(viewport={"width":390,"height":900})
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            seen, release = asyncio.Event(), asyncio.Event()
            delay = [False]
            async def guard(route):
                if not route.request.url.startswith(c.origin+"/"):
                    errors.append("outside fixture")
                    return await route.abort()
                if (delay[0] and route.request.method=="POST" and route.request.url.endswith("/api/ops/action")
                        and route.request.post_data_json.get("phase")=="preview"):
                    seen.set()
                    await release.wait()
                await route.continue_()
            await page.route("**/*",guard)
            try:
                await page.goto(c.url)
                await pw.expect(page.locator("#t-comms")).to_have_attribute("aria-selected","true")
                await page.locator("#t-ops").click()
                button = page.get_by_role("button",name="Take a snapshot",exact=True)
                await pw.expect(button).to_be_enabled(timeout=10000)
                await button.click()
                await page.locator("#ops-action-label").fill("first")
                delay[0] = True
                await page.locator("#ops-action-preview").click()
                await asyncio.wait_for(seen.wait(),5)
                await pw.expect(page.locator("#ops-action-label")).to_be_disabled()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("first")
                await page.keyboard.press("Escape")
                await pw.expect(page.locator("#ops-action-dialog")).not_to_be_visible()
                release.set()
                await pw.expect(button).to_be_enabled(timeout=10000)
                await button.click()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("")
                await pw.expect(page.locator("#ops-action-preview")).to_be_visible()
                await pw.expect(page.locator("#ops-action-confirm")).not_to_be_visible()
                delay[0] = False
                await page.locator("#ops-action-label").fill("bad/path")
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-words")).to_contain_text("Snapshot label")
                await pw.expect(page.locator("#ops-action-label")).to_be_enabled()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("bad/path")
                await page.locator("#ops-action-label").fill("browser")
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-confirm")).to_be_visible()
                await page.locator("#ops-action-confirm").click()
                await pw.expect(page.locator("#ops-action-message")).to_contain_text("snapshot was saved",timeout=10000)
                assert len(list((app.data_dir/"snapshots").glob("*.db"))) == 1
                for width in (360,390,1440):
                    await page.set_viewport_size({"width":width,"height":900})
                    for theme in ("dark","light"):
                        await page.evaluate("theme=>document.documentElement.dataset.theme=theme",theme)
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors
            finally:
                release.set()
                await browser.close()
    run(go())
