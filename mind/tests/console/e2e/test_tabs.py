"""Real browser rendering, including stopped/older stores and both color themes."""
import asyncio

import pytest

from console.rig import KEY

TABS = ("tonight", "comms", "raid", "guild", "social", "personas", "board", "calendar",
        "bank", "realm", "training", "dojo", "library", "ops", "mind")
POPULATED = {
    "tonight": "Waiting for you",
    "comms": "Rehearsal supplies are ready.", "raid": "Icecrown Citadel",
    "guild": "Juniper", "social": "Belra", "personas": "Arkon",
    "board": "Bring tea for the rehearsal.", "calendar": "Rehearsal raid night",
    "bank": "Rehearsal supplies", "realm": "Lantern Company",
    "training": "Rehearsal tactic", "dojo": "rehearsal-episode", "ops": "Release drift",
    "library": "1 passage", "mind": "running",
}
OLDER = {tab: "Not set up yet" for tab in (
    "tonight", "social", "board", "calendar", "bank", "realm", "training", "library", "ops")}
OLDER.update({"raid": "No raid is open.",
              "guild": "The mind has no roster yet.",
              "personas": "This mind.db is older than some of what this tab reads",
              "dojo": "No dojo set up", "mind": "stopped"})


async def open_console(r, *, timeout=15000):
    from playwright.async_api import expect
    await r.page.goto(r.url + "/#key=" + KEY)
    # The parallel mind suite can keep the browser busy loading the module graph.
    # Wait for the real startup signal before testing a tab or action.
    await expect(r.page.locator("#s-mind")).to_contain_text("mind running", timeout=timeout)


async def tab_names(page):
    tabs = await page.locator('[role="tab"]').evaluate_all("xs => xs.map(x => x.dataset.tab)")
    assert set(tabs) == set(TABS), "Update coverage when a new console tab lands"
    return tabs


async def visit(page, tab):
    from playwright.async_api import expect
    await page.locator("#t-" + tab).click()
    panel = page.locator("#tab-" + tab)
    await expect(panel).to_be_visible()
    return panel


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("state", ["running", "stopped", "older"])
def test_every_tab(rehearsal, tmp_path, theme, state):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(theme=theme, state=state) as r:
            await r.page.goto(r.url + "/#key=" + KEY)
            await expect(r.page.locator("html")).to_have_attribute("data-theme", theme)
            await expect(r.page.locator("#s-mind")).to_contain_text(
                "mind running" if state == "running" else "mind stopped")
            for tab in await tab_names(r.page):
                panel = await visit(r.page, tab)
                if tab == "mind":
                    await expect(r.page.locator("#m-state")).to_have_text(
                        "running" if state == "running" else "stopped")
                elif state == "older" and tab == "comms":
                    await expect(r.page.locator("#say-text")).to_have_attribute(
                        "placeholder", "The mind is stopped, so nothing can be sent.")
                elif state != "older":
                    await expect(panel).to_contain_text(POPULATED[tab])
                else:
                    await expect(panel).to_contain_text(OLDER[tab])
                (tmp_path / f"{tab}.txt").write_text(await panel.inner_text(), encoding="utf-8")
                await expect(panel).not_to_contain_text("This tab could not be drawn")
                if tab == 'tonight':
                    await r.page.screenshot(path=str(tmp_path/'tonight.png'),full_page=True)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_tonight_keeps_comms_default_and_supports_a_deep_link(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await expect(r.page.locator('#tab-comms')).to_be_visible()
            assert (await tab_names(r.page))[0]=='tonight'
            await r.page.evaluate("location.hash='#tonight'")
            await expect(r.page.locator('#tonight-cards > article')).to_have_count(7)
            await expect(r.page.locator('#tab-comms')).to_be_hidden()
            assert not r.errors,r.errors
    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_phone_has_no_page_overflow(rehearsal, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390, theme=theme) as r:
            await open_console(r)
            overflowing = []
            for tab in await tab_names(r.page):
                panel = await visit(r.page, tab)
                await expect(panel).to_contain_text(POPULATED[tab])
                # Tables and rails can scroll within their containers; the page cannot.
                size = await r.page.evaluate("({width: innerWidth, scroll: document.documentElement.scrollWidth})")
                if size["scroll"] > size["width"] + 1:
                    overflowing.append((tab, size))
            assert not r.errors, r.errors
            assert not overflowing, overflowing
    asyncio.run(run())


@pytest.mark.parametrize("state", ["running", "older"])
def test_ops_tab_shows_health_lines_and_tolerates_an_older_database(rehearsal, state):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(state=state) as r:
            await r.page.goto(r.url + "/#key=" + KEY)
            panel = await visit(r.page, "ops")
            await expect(panel).to_contain_text("servers: ok (rehearsal)")
            await expect(panel).to_contain_text("Release drift")
            await expect(panel).to_contain_text("Not set up yet" if state == "older" else "held members")
            await expect(panel).not_to_contain_text("This tab could not be drawn")
            # Nothing needs a look in the rehearsal's health, and the badge appears only when it does.
            if state == "older":
                await expect(r.page.locator("#t-ops .badge")).to_have_count(0)
            assert not r.errors, r.errors
    asyncio.run(run())
