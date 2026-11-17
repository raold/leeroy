"""The console audit's fixes in a real browser, at a desktop, a small window and a phone, in both
themes: the member page that replaces the Personas list, the inline dial preview, Comms' first
screen and its left list, and the Lab's decisions. Each run ends with axe on what it opened."""
import asyncio
import os
from pathlib import Path

import pytest

from console.rig import KEY

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


MEASURE = """() => {
  const top = s => { const e = document.querySelector(s); return e && e.getClientRects().length ? Math.round(e.getBoundingClientRect().top) : null; };
  const bottom = s => { const e = document.querySelector(s); return e && e.getClientRects().length ? Math.round(e.getBoundingClientRect().bottom) : null; };
  const header = document.querySelector('header.top').getBoundingClientRect();
  return {header: Math.round(header.height), strip: Math.round(document.querySelector('.top-inner').getBoundingClientRect().height),
          tabs: Math.round(document.querySelector('.tabs').getBoundingClientRect().height),
          feed_top: top('#feed'), reply_bottom: bottom('#composer'), send_bottom: bottom('#say-send'),
          left_visible: !!document.querySelector('#comms-left').getClientRects().length,
          page_width: document.documentElement.scrollWidth, width: innerWidth, height: innerHeight};
}"""


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", SIZES)
def test_comms_first_screen_is_the_feed_and_the_reply_box(rehearsal, width, height, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=width, height=height, theme=theme) as r:
            page = r.page
            await open_console(r)
            await expect(page.locator("#feed")).to_contain_text("Rehearsal supplies are ready.")
            await page.wait_for_function("() => getComputedStyle(document.querySelector('.comms')).getPropertyValue('--comms-h') !== ''")
            m = await page.evaluate(MEASURE)
            print("measure", width, height, theme, m)
            # the feed and the reply box are on the first screen, nothing scrolled
            assert m["feed_top"] is not None and m["feed_top"] < height * 0.5, m
            assert m["reply_bottom"] is not None and m["reply_bottom"] <= height, m
            assert m["send_bottom"] <= height, m
            assert m["page_width"] <= m["width"] + 1, m
            await expect(page.locator("#say-text")).to_be_in_viewport(ratio=1)
            await shot(r, "comms", width, theme)
            if width > 900:
                assert m["left_visible"], m
                await expect(page.locator("#c-drawer")).to_be_hidden()
            else:
                # one status line, and only the tab bar stays when the page scrolls
                assert m["strip"] <= 60, m
                assert m["header"] <= 120, m
                assert not m["left_visible"], m
                await page.evaluate("window.scrollTo(0, 400)")
                stuck = await page.evaluate("""() => { const h = document.querySelector('header.top').getBoundingClientRect(),
                    t = document.querySelector('.tabs').getBoundingClientRect(); return {shown: Math.round(h.bottom), tabs_top: Math.round(t.top), tabs: Math.round(t.height)}; }""")
                assert abs(stuck["tabs_top"]) <= 1 and stuck["shown"] <= stuck["tabs"] + 2, stuck
                await page.evaluate("window.scrollTo(0, 0)")
                # the tab bar scrolls sideways on one line instead of wrapping
                tabs = await page.evaluate("""() => { const t = document.querySelector('.tabs'); return [t.scrollWidth > t.clientWidth,
                    new Set([...t.children].map(b => Math.round(b.getBoundingClientRect().top))).size]; }""")
                assert tabs == [True, 1], tabs
                # the channel list is a drawer
                drawer = page.locator("#c-drawer")
                await drawer.click()
                await expect(page.locator("#comms-left")).to_be_visible()
                await expect(drawer).to_have_attribute("aria-expanded", "true")
                await shot(r, "drawer", width, theme)
                assert not await serious(page)
                await page.keyboard.press("Escape")
                await expect(page.locator("#comms-left")).to_be_hidden()
                await expect(drawer).to_be_focused()
                await drawer.click()
                await page.locator('#comms-left [data-view="officer"]').click()
                await expect(page.locator("#comms-left")).to_be_hidden()
                await expect(page.locator("#feed-title")).to_have_text("# officer")
            assert not await serious(page)
            # a Discord channel keeps its own reply box in sight too
            if width <= 900:
                await page.locator("#c-drawer").click()
            await page.locator('#dc-channels [data-view^="d:"]').first.click()
            await expect(page.locator("#dc-composer")).to_be_visible()
            await expect(page.locator("#dc-send")).to_be_in_viewport(ratio=1)
            assert not await serious(page)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_unread_shows_in_the_title_and_icon_while_hidden(rehearsal):
    async def run():
        from playwright.async_api import expect
        from conftest import HUMAN
        async with rehearsal() as r:
            page = r.page
            await open_console(r)
            await expect(page.locator("#feed")).to_contain_text("Rehearsal supplies are ready.")
            assert await page.title() == "Guild Console"
            requests = []
            page.on("request", lambda q: requests.append(q.url))
            # out of sight: the stream closes, and the one light read counts what comes
            await page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                document.dispatchEvent(new Event('visibilitychange')); }""")
            await r.bridge.push({"type": "chat", "channel": "whisper", "from_guid": 102, "from_name": "Belra",
                                 "to_guid": HUMAN, "text": "Are you coming tonight?", "map": 0, "zone": 1519})
            await page.wait_for_function("() => document.title === '(1) Guild Console'", timeout=45000)
            assert "circle" in await page.evaluate("decodeURIComponent(document.querySelector('link[rel=icon]').href)")
            hidden = [u.split("/api/")[1].split("?")[0] for u in requests if "/api/" in u]
            assert set(hidden) <= {"unread"}, hidden
            # back in sight: cleared, and the in-page badges say it
            await page.evaluate("""() => { delete document.hidden; delete document.visibilityState;
                document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('focus')); }""")
            await page.bring_to_front()
            await page.wait_for_function("() => document.title === 'Guild Console'")
            assert "circle" not in await page.evaluate("decodeURIComponent(document.querySelector('link[rel=icon]').href)")
            await expect(page.locator("#notify")).to_have_attribute("aria-pressed", "false")
            assert not r.errors, r.errors
    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", SIZES)
def test_comms_list_is_his_conversations_first(rehearsal, width, height, theme):
    async def run():
        import time
        from playwright.async_api import expect
        from conftest import HUMAN
        from leeroy_mind.discord import store as discord
        from .test_tabs import choose
        async with rehearsal(width=width, height=height, theme=theme) as r:
            page = r.page
            await open_console(r)
            await expect(page.locator("#feed")).to_contain_text("Rehearsal supplies are ready.")
            narrow = width <= 900

            async def show_list():
                if narrow and not await page.locator("#comms-left").is_visible():
                    await page.locator("#c-drawer").click()
            await show_list()
            # nobody has written yet: no DM row, and the list says how to find a member
            await expect(page.locator("#conv-more")).to_contain_text("more members have no messages yet")
            assert await page.locator('#dc-dms [data-view^="dm:"]').count() == 0
            # the voice rooms are folded away until asked for
            assert not await page.locator("#rooms-fold").evaluate("e => e.open")
            await expect(page.locator("#tree")).to_be_hidden()
            # a DM, then a whisper: both listed, the newest unread first
            discord.post(r.app.store, channel="dm:102", author=102, origin="mind", text="Tea after the raid?", now=time.time())
            r.app.store.conn.commit()
            await page.evaluate("async () => (await import('/static/js/core.mjs')).emit('discord', {})")
            await expect(page.locator('#dc-dms [data-view="dm:102"]')).to_be_visible()
            await r.bridge.push({"type": "chat", "channel": "whisper", "from_guid": 103, "from_name": "Coros",
                                 "to_guid": HUMAN, "text": "Got a minute?", "map": 0, "zone": 1519})
            await expect(page.locator('#dc-dms [data-view="w:103"]')).to_be_visible()
            order = await page.locator("#dc-dms [data-view]").evaluate_all("xs => xs.map(x => x.dataset.view)")
            assert order[:2] == ["w:103", "dm:102"], order
            await expect(page.locator('#dc-dms [data-view="dm:102"] .badge')).to_have_text("1")
            await expect(page.locator('#dc-dms [data-view="w:103"]')).to_have_attribute("aria-label", "Coros, WoW whispers, 1 unread")
            await shot(r, "list", width, theme)
            assert not await serious(page)
            # read the whisper: the unread DM leads
            await page.locator('#dc-dms [data-view="w:103"]').click()
            await expect(page.locator("#feed-title")).to_have_text("Coros")
            await show_list()
            order = await page.locator("#dc-dms [data-view]").evaluate_all("xs => xs.map(x => x.dataset.view)")
            assert order[:2] == ["dm:102", "w:103"], order
            # a member with no messages is found by name; Enter opens the first match
            await page.locator("#conv-find").fill("fen")
            await expect(page.locator("#dc-dms [data-view]")).to_have_count(1)
            await expect(page.locator('#dc-dms [data-view="dm:106"]')).to_contain_text("Fennel")
            await page.locator("#conv-find").press("Enter")
            await expect(page.locator("#dc-to")).to_contain_text("Fennel")
            if narrow:
                await expect(page.locator("#comms-left")).to_be_hidden()
            # "/" is the reply box, Ctrl+K the filter (the drawer opens for it in a small window)
            await page.evaluate("document.activeElement.blur()")
            await page.keyboard.press("/")
            await expect(page.locator("#dc-text")).to_be_focused()
            assert await page.locator("#dc-text").input_value() == ""
            await page.keyboard.press("Control+k")
            await expect(page.locator("#conv-find")).to_be_focused()
            await page.locator("#conv-find").fill("")
            await page.locator('#comms-left [data-view="guild"]').click()
            await page.evaluate("document.activeElement.blur()")
            await page.keyboard.press("/")
            await expect(page.locator("#say-text")).to_be_focused()
            # typing a slash in a message is a slash
            await page.keyboard.type("a/b")
            assert await page.locator("#say-text").input_value() == "a/b"
            await page.locator("#say-text").fill("")
            # the page's first Tab stop skips to the reply box
            assert await page.evaluate("document.querySelector('a[href], button, input, select, textarea, [tabindex]').id") == "skip-reply"
            await expect(page.locator("#skip-reply")).not_to_be_in_viewport()
            await page.evaluate("window.scrollTo(0, 0)")
            await page.locator("#skip-reply").focus()
            await expect(page.locator("#skip-reply")).to_be_in_viewport()
            await page.keyboard.press("Enter")
            await expect(page.locator("#say-text")).to_be_focused()
            # the fold opens on request and is remembered
            await show_list()
            await page.locator("#rooms-fold > summary").click()
            assert await page.locator("#rooms-fold").evaluate("e => e.open")
            await page.wait_for_function("() => localStorage.getItem('guild-console-rooms') === '\"open\"'")  # toggle is async
            assert not await serious(page)
            assert not r.errors, r.errors
    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width,height", SIZES)
def test_lab_shows_what_needs_his_decision(rehearsal, width, height, theme):
    async def run():
        from playwright.async_api import expect
        from .test_flows import submit
        from .test_lab import seed_lab
        async with rehearsal(width=width, height=height, theme=theme, learning=True) as r:
            page = r.page
            ids = seed_lab(r)
            await open_console(r)
            # the Lab tab's badge counts them before the tab is opened
            await expect(page.locator("#t-lab .badge")).to_have_text("2", timeout=15000)
            await visit(page, "tonight")
            await expect(page.locator("#tonight-cards")).to_contain_text("Lab proposals to decide: 2")
            await visit(page, "lab")
            pending = page.locator("#lab-pending")
            await expect(page.locator("#lab-pending-title")).to_have_text("Needs your decision (2)")
            await expect(pending).to_contain_text("Arm " + ids["arm"])
            await expect(pending).to_contain_text("Lesson for Arkon · guild: She keeps replies brief.")
            top = await box(page, ".lab-pending")
            assert top["top"] < height, top    # on the first screen
            # the folded panels say what is inside
            await expect(page.locator('[data-detail="family-length"] > summary')).to_contain_text("1 proposed")
            await expect(page.locator("#lab-rated")).to_have_text("You rated 0 lines this week.")
            await shot(r, "lab", width, theme)
            assert not await serious(page)
            row = pending.locator(".lab-pending-row").filter(has_text="Lesson for Arkon")
            await row.get_by_role("button", name="Approve", exact=True).click()
            await submit(r, page.locator("#lab-confirm-save"), "/api/learn")
            await expect(page.locator("#lab-pending-title")).to_have_text("Needs your decision (1)")
            await expect(page.locator("#t-lab .badge")).to_have_text("1")
            assert r.app.store.conn.execute("SELECT status FROM talk_lesson WHERE id=?", (ids["lesson"],)).fetchone()[0] == "active"
            row = pending.locator(".lab-pending-row").filter(has_text="Arm " + ids["arm"])
            await row.get_by_role("button", name="Reject", exact=True).click()
            await submit(r, page.locator("#lab-confirm-save"), "/api/learn")
            await expect(pending).to_contain_text("Nothing is waiting for you.")
            await expect(page.locator("#t-lab .badge")).to_have_count(0)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_the_page_loads_compressed_and_reloads_from_cache(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            page = r.page
            seen = []

            async def note(response):
                path = response.url[len(r.url):].split("?")[0]
                if path == "/" or path.startswith("/static/"):
                    headers = await response.all_headers()
                    seen.append((path, response.status, headers.get("content-encoding", ""),
                                 int(headers.get("content-length", "0")), headers.get("cache-control", ""), headers.get("etag", "")))
            page.on("response", note)
            await open_console(r)
            await expect(page.locator("#feed")).to_contain_text("Rehearsal supplies are ready.")
            await page.wait_for_timeout(500)
            first = list(seen)
            assert first and all(status == 200 for _, status, *_ in first), [f for f in first if f[1] != 200]
            assert all(cache == "no-cache" and etag for *_, cache, etag in first)
            text_files = [f for f in first if not f[0].endswith(".woff2")]
            assert all(enc == "gzip" for path, _, enc, size, *_ in text_files if size > 1024), \
                [f for f in text_files if f[2] != "gzip"]
            wire = sum(size for _, _, _, size, *_ in first)
            print("load", {"requests": len(first), "wire_bytes": wire})
            assert wire < 450_000, wire
            # a reload revalidates: nothing is sent again
            seen.clear()
            await page.goto(r.url + "/#key=" + KEY)
            await page.reload()
            await expect(page.locator("#s-lost")).to_be_visible()   # a reload loses the key, as before
            await page.wait_for_timeout(500)
            again = [f for f in seen]
            # (Chromium reports the revalidated document itself as a 200 with nothing sent)
            assert again and all(status == 304 or (path == "/" and size == 0) for path, status, _, size, *_ in again),                 [f for f in again if f[1] != 304][:5]
            print("reload", {"requests": len(again), "wire_bytes": sum(f[3] for f in again)})
            # the page still runs only its own files under its CSP
            assert not [e for e in r.errors if "Content Security Policy" in e], r.errors
    asyncio.run(run())


def test_thumbs_show_on_hover_and_focus_on_every_member_line(rehearsal):
    async def run():
        from playwright.async_api import expect
        from .test_flows import submit
        from .test_lab import seed_lab
        from .test_tabs import choose
        async with rehearsal(learning=True) as r:
            page = r.page
            ids = seed_lab(r)
            await open_console(r)
            views = [("guild", ids["guild"]), ("w:101", ids["whisper"]), ("voice", ids["voice"]),
                     ("d:general", ids["discord"]["general"][0]), ("dm:101", ids["discord"]["dm:101"][0])]
            for view, lid in views:
                await page.evaluate("async view => (await import('/static/js/comms/feedview.mjs')).openView(view)", view)
                thumbs = page.locator(f'[data-feedback="{lid}"]')
                await expect(thumbs).to_have_count(1)
                row = thumbs.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' msg ')][1]")
                await page.mouse.move(2, 2)
                await expect(thumbs).to_have_css("opacity", "0")          # out of the way until asked for
                await row.hover()
                await expect(thumbs).to_have_css("opacity", "1")          # no extra click
                await expect(thumbs.get_by_role("button", name="Thumbs up", exact=False)).to_be_in_viewport()
                await page.mouse.move(2, 2)
                await thumbs.get_by_role("button", name="Thumbs up", exact=False).focus()
                await expect(thumbs).to_have_css("opacity", "1")          # the keyboard gets them too
                await page.evaluate("document.activeElement.blur()")
            # one click rates, and a rated line keeps its thumbs in sight
            await page.evaluate("async () => (await import('/static/js/comms/feedview.mjs')).openView('guild')")
            thumbs = page.locator(f'[data-feedback="{ids["guild"]}"]')
            await submit(r, thumbs.get_by_role("button", name="Thumbs up", exact=False), "/api/feedback")
            await page.mouse.move(2, 2)
            await page.evaluate("document.activeElement.blur()")
            await expect(thumbs).to_have_css("opacity", "1")
            await visit(page, "lab")
            await expect(page.locator("#lab-rated")).to_have_text("You rated 1 line this week.")
            assert not await serious(page)
            assert not r.errors, r.errors
    asyncio.run(run())
