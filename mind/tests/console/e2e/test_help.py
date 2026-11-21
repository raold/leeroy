"""Help is available in every rehearsal panel and keeps the user's current place."""
import asyncio

import pytest

from console.rig import KEY
from .test_tabs import tab_names, visit


@pytest.mark.parametrize("width,state", [(1440, "running"), (360, "stopped"), (390, "running")])
def test_context_help_and_focus(rehearsal, tmp_path, width, state):
    async def run():
        from playwright.async_api import expect

        async with rehearsal(width=width, height=800, state=state) as r:
            page = r.page
            await page.goto(r.url + "/#key=" + KEY)
            await expect(page.locator("#s-mind")).to_contain_text("mind " + state)
            opener = page.get_by_role("button", name="Help for this tab")
            dialog = page.locator("#help-dialog")
            for tab in await tab_names(page):
                await visit(page, tab)
                await opener.focus()
                await page.keyboard.press("Enter")
                await expect(dialog).to_be_visible()
                await expect(dialog.get_by_role("heading")).to_have_text(tab.capitalize() + " help")
                await expect(page.locator("#help-full")).to_be_visible()
                words = await page.locator("#help-words").inner_text()
                assert len(words) >= 80 and "## " not in words
                await expect(page.locator("#help-about")).to_contain_text("Console version")
                if tab == "comms":
                    await page.screenshot(path=str(tmp_path / f"help-{width}.png"))
                    await page.locator("#help-full").click()
                    await expect(page.locator("#help-words")).to_contain_text("## Troubleshooting")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                if tab in ("comms", "mind"):
                    await dialog.get_by_role("button", name="Close").click()
                else:
                    await page.keyboard.press("Escape")
                await expect(opener).to_be_focused()
                await expect(page.locator("#tab-" + tab)).to_be_visible()
            for name in ("gpu", "render", "notify"):
                await expect(page.locator("#" + name)).to_be_visible()
            if state == "running":
                await page.evaluate("location.hash = '#personas/101'")
                await expect(page.locator("#tab-personas")).to_be_visible()
                await opener.click()
                await expect(dialog.get_by_role("heading")).to_have_text("Personas help")
                await page.keyboard.press("Escape")
                assert page.url.endswith("#personas/101")
                await page.route("**/api/guide", lambda route: route.fulfill(json={
                    "guide": "## Personas\n<img src=x onerror=alert(1)>",
                    "version": "fixture", "source_commit": None, "checkout_commit": None,
                }))
                await opener.click()
                await expect(page.locator("#help-words")).to_have_text("<img src=x onerror=alert(1)>")
                await expect(dialog.locator("img")).to_have_count(0)
            assert not r.errors, r.errors
    asyncio.run(run())
