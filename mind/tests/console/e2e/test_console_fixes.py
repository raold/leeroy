"""The console audit's fixes in a real browser, at a desktop, a small window and a phone, in both
themes: the member page that replaces the Personas list, the inline dial preview, Comms' first
screen and its left list, and the Lab's decisions. Each run ends with axe on what it opened."""
import asyncio
import os
from pathlib import Path

import pytest

from .test_accessibility import AXE
from .test_tabs import open_console, visit

SIZES = [(1440, 900), (1024, 768), (390, 844)]
SHOTS = os.environ.get("LEEROY_E2E_SHOTS")


async def shot(r, name, width, theme):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        await r.page.screenshot(path=str(Path(SHOTS) / f"{name}-{width}-{theme}.png"))


async def serious(page):
    if not AXE.is_file():
        message = "Install console accessibility checks: npm ci --prefix tests/console/e2e"
        if os.environ.get("LEEROY_CONSOLE_E2E_REQUIRED"):
            pytest.fail(message)
        return []
    await page.evaluate(AXE.read_text(encoding="utf-8"))
    report = await page.evaluate("""async () => {
        const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}});
        return r.violations.filter(v => ['serious', 'critical'].includes(v.impact))
          .map(v => ({id: v.id, nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))}));
    }""")
    return report


async def box(page, selector):
    return await page.locator(selector).first.evaluate(
        "e => { const b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom, left: b.left, right: b.right, height: b.height}; }")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", SIZES)
def test_member_page_replaces_the_list_and_back_returns(rehearsal, width, height, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=width, height=height, theme=theme, godmode=True) as r:
            page = r.page
            await open_console(r)
            await visit(page, "personas")
            profile = page.get_by_role("button", name="Profile of Arkon", exact=True)
            await profile.scroll_into_view_if_needed()
            before = await page.evaluate("document.querySelector('main').scrollTop")
            await profile.click()
            # one click: her page is the view, at the top of the screen, the list gone
            await expect(page.locator("#p-detail")).to_be_visible()
            await expect(page.locator("#p-list")).to_be_hidden()
            await expect(page.locator("#p-name")).to_have_text("Arkon")
            await expect(page.locator("#p-detail")).not_to_have_class("pending")
            assert page.url.endswith("#personas/101")
            head = await box(page, "#p-detail .member-head")
            assert 0 <= head["top"] < height * 0.6, head
            for name in ("Back to the cast", "Message", "Whisper", "Strings", "Nudge"):
                button = page.locator("#p-detail .member-head").get_by_role("button", name=name, exact=True)
                await expect(button).to_be_visible()
                assert (await box(page, f"#p-detail .member-head button:text-is('{name}')"))["bottom"] <= height, name
            await shot(r, "member", width, theme)
            assert not await serious(page)

            # Nudge and Strings land on their panels
            await page.locator("#p-act-nudge").click()
            await expect(page.locator("#p-dials .nudges textarea")).to_be_focused()
            await page.locator("#p-act-strings").click()
            assert await page.evaluate("document.querySelector('#p-strings').contains(document.activeElement)")

            # the browser's Back is the list again: same members, same scroll, her tile marked
            await page.go_back()
            await expect(page.locator("#p-list")).to_be_visible()
            await expect(page.locator("#p-detail")).to_be_hidden()
            assert page.url.endswith("#personas")
            await expect(page.locator("#p-wall .tile.sel")).to_have_attribute("data-guid", "101")
            await expect(profile).to_be_focused()
            after = await page.evaluate("document.querySelector('main').scrollTop")
            assert abs(after - before) <= 2, (before, after)
            # forward again, then the page's own Back
            await page.go_forward()
            await expect(page.locator("#p-name")).to_have_text("Arkon")
            await page.get_by_role("button", name="Back to the cast", exact=True).click()
            await expect(page.locator("#p-list")).to_be_visible()
            assert page.url.endswith("#personas")

            # the table keeps its view and selection too
            await page.locator('#p-view button[data-view="table"]').click()
            await page.locator('#p-roster tr[data-guid="102"]').click()
            await expect(page.locator("#p-name")).to_have_text("Belra")
            await page.go_back()
            await expect(page.locator("#p-table")).to_be_visible()
            await expect(page.locator('#p-roster tr.sel')).to_have_attribute("data-guid", "102")
            await page.locator('#p-view button[data-view="wall"]').click()

            # a face on another tab is one click from her page; Back returns to that tab
            await visit(page, "social")
            await page.locator('#tab-social [data-profile="102"]').first.click()
            await expect(page.locator("#tab-personas")).to_be_visible()
            await expect(page.locator("#p-name")).to_have_text("Belra")
            await page.go_back()
            await expect(page.locator("#tab-social")).to_be_visible()

            # Whisper and Message open Comms on her
            await page.go_forward()
            await expect(page.locator("#p-name")).to_have_text("Belra")
            await page.locator("#p-act-whisper").click()
            await expect(page.locator("#tab-comms")).to_be_visible()
            await expect(page.locator("#feed-title")).to_have_text("Belra")
            await page.go_back()
            await expect(page.locator("#p-name")).to_have_text("Belra")
            await page.locator("#p-act-dm").click()
            await expect(page.locator("#tab-comms")).to_be_visible()
            await expect(page.locator("#dc-composer")).to_be_visible()
            await expect(page.locator("#dc-to")).to_contain_text("Belra")

            # a link straight to a member opens her page; an unknown one falls back to the list
            await page.evaluate("location.hash = '#personas/103'")
            await expect(page.locator("#p-detail")).to_be_visible()
            await expect(page.locator("#p-name")).not_to_have_text("Belra")
            await page.get_by_role("button", name="Back to the cast", exact=True).click()
            await expect(page.locator("#p-list")).to_be_visible()
            r.expected_http_errors.add(("/api/persona", 404))
            await page.evaluate("location.hash = '#personas/999999'")
            await expect(page.locator("#p-list")).to_be_visible()
            await expect(page.locator("#toasts")).to_contain_text("not in mind.db")
            assert page.url.endswith("#personas")
            assert not r.errors, r.errors
    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", SIZES)
def test_dial_preview_shows_under_its_dial(rehearsal, width, height, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=width, height=height, theme=theme) as r:
            page = r.page
            await open_console(r)
            await visit(page, "personas")
            await page.get_by_role("button", name="Profile of Arkon", exact=True).click()
            dial = page.locator("#p-dials .dial").filter(has=page.get_by_role("slider", name="argumentative", exact=True))
            slider = dial.get_by_role("slider")
            await slider.fill("10" if int(await slider.input_value()) <= 5 else "1")
            r.app.arbiter.admin.loaded.clear()
            r.expected_http_errors.add(("/api/tune", 409))
            await dial.get_by_role("button", name="Preview", exact=True).click()
            inline = dial.locator(".pv-inline")
            await expect(inline).to_contain_text("isn't loaded")
            assert await page.locator(".pv-inline").count() == 1
            placed = await inline.evaluate("e => { const b = e.getBoundingClientRect(); return [b.top, b.bottom]; }")
            assert 0 <= placed[0] and placed[1] <= height + 1, placed       # on screen, no scrolling
            row = await dial.evaluate("e => e.getBoundingClientRect().top")
            assert placed[0] >= row
            await shot(r, "preview", width, theme)
            assert not await serious(page)
            # a redraw of the dials (a tuning event, as the 30 s poll) keeps it under its dial
            other = page.locator("#p-dials .dial").filter(has=page.get_by_role("slider", name="talkative", exact=True))
            await other.get_by_role("slider").fill("7")
            await other.get_by_role("button", name="Set", exact=True).click()
            await expect(other).to_contain_text("your value 7")
            await expect(dial.locator(".pv-inline")).to_contain_text("isn't loaded")
            # the nudge panel's preview shows under the nudge panel
            panel = page.locator("#p-dials .nudges")
            await panel.get_by_label("Nudge (1–200 characters)").fill("Think about tonight")
            await panel.get_by_role("button", name="Preview a line", exact=True).click()
            await expect(panel.locator(".pv-inline")).to_contain_text("isn't loaded")
            assert await page.locator(".pv-inline").count() == 1
            await panel.get_by_role("button", name="Close preview", exact=True).click()
            assert await page.locator(".pv-inline").count() == 0
            assert not r.errors, r.errors
    asyncio.run(run())
