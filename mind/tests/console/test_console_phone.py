"""Optional real-layout checks; the Node rig has no layout engine.

Run with: pip install playwright; python -m playwright install chromium;
python -m pytest -q tests/console/test_console_phone.py
CI can install that extra/browser before this command. No browser/package means a clean skip.
Only the test's console, scratch database and fake probes are used; no live services or images.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

from conftest import seed_test_cast
from console.rig import make_app
from console.test_console_server import served
from leeroy_mind.console.control import Controls

playwright = pytest.importorskip("playwright.async_api", reason="optional browser layout checks need playwright")
TABS = "raid guild personas social board calendar bank realm training dojo library mind".split()
SIZES = [(390, 844), (360, 780), (768, 1024), (1280, 900)]


@asynccontextmanager
async def browser_page(app, width, height, theme):
    async with playwright.async_playwright() as pw:
        browser = None
        for channel in (None, "chrome", "msedge"):
            try:
                browser = await pw.chromium.launch(headless=True, channel=channel)
                break
            except playwright.Error as exc:
                if "Executable doesn't exist" not in str(exc) and "distribution" not in str(exc):
                    raise
        if browser is None:
            pytest.skip("no browser installed: python -m playwright install chromium")
        try:
            page = await browser.new_page(viewport={"width": width, "height": height},
                                          reduced_motion="reduce")
            # Browser traffic is also restricted to this test's ephemeral origin.
            await page.route("**/*", lambda route: route.continue_() if
                             route.request.url.startswith(app.origin + "/") else route.abort())
            await page.goto(app.url, wait_until="domcontentloaded")
            await playwright.expect(page.locator('#t-comms')).to_have_attribute('aria-selected', 'true')
            await page.evaluate("document.fonts.ready")
            await page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            yield page
        finally:
            await browser.close()


async def fits(page, *, touch=True):
    result = await page.evaluate("""() => {
      const tab = document.querySelector('.tab:not([hidden])');
      const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
      const label = e => e.id || e.tagName + '.' + e.className;
      const controls = [...document.querySelectorAll('.top button'),
        ...tab.querySelectorAll('button,input,select,summary')].filter(visible);
      const small = controls.filter(e => {
        if (e.matches('input[type="checkbox"]')) e = e.closest('label') || e;
        const r = e.getBoundingClientRect(); return r.width < 43.9 || r.height < 43.9;
      }).map(label);
      const zoom = [...tab.querySelectorAll('input:not([type="checkbox"]):not([type="range"]),select,textarea')]
        .filter(visible).filter(e => parseFloat(getComputedStyle(e).fontSize) < 16).map(label);
      return {width: innerWidth, body: document.documentElement.scrollWidth,
        main: document.querySelector('main').scrollWidth, small, zoom};
    }""")
    assert result["body"] <= result["width"] + 1, result
    assert result["main"] <= result["width"] + 1, result
    if touch:
        assert not result["small"], result
        assert not result["zoom"], result


@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_each_tab_shell_fits_viewport(store, tmp_path, width, height, theme):
    seed_test_cast(store)

    async def scenario():
        async with served(make_app(tmp_path, controls=Controls(enabled=False))) as app:
            async with browser_page(app, width, height, theme) as page:
                for tab in TABS:
                    await page.locator("#t-" + tab).click()
                    await fits(page, touch=width <= 900)
                if width <= 900:
                    # Hash/profile links also reveal the selected tab in the horizontal bar.
                    await page.evaluate("location.hash = '#raid'")
                    await playwright.expect(page.locator('#t-raid')).to_have_attribute('aria-selected', 'true')
                    rect = await page.locator("#t-raid").bounding_box()
                    assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width
                    assert (await page.locator(".tabs").bounding_box())["height"] < 65
    asyncio.run(scenario())


@pytest.mark.parametrize("width,height", SIZES[:2])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_phone_tables_bank_and_dialog_remain_usable(store, tmp_path, width, height, theme):
    seed_test_cast(store)
    model_name = 'a-model-with-a-long-name-and-digest-' + 'a' * 100

    def models(url, roles):
        return {"ok": True, "url": url, "models": [{"name": model_name, "role": "live",
                                                  "size": 10**10, "vram": 10**10}]}

    async def scenario():
        async with served(make_app(tmp_path, controls=Controls(enabled=False), ollama=models)) as app:
            async with browser_page(app, width, height, theme) as page:
                bank = {"bank": {"money": 123456789, "read_at": 1, "log": [],
                                 "tabs": [{"tab": 0, "name": "Long bank tab name", "slots": [[0, 42, 20]]}]},
                        "items": {"42": {"name": "A very long synthetic item name for touch readers", "quality": 4}},
                        "ledger": [], "missing": []}
                await page.route("**/api/bank", lambda route: route.fulfill(json=bank))
                await page.locator("#t-bank").click()
                await page.locator("#bank-main").wait_for(state="visible")
                await fits(page)
                assert await page.locator(".bank-scroll").evaluate("e => e.scrollWidth > e.clientWidth")
                assert await page.locator(".slot .w").evaluate("e => e.scrollHeight <= e.clientHeight + 1")
                await page.locator(".bank-scroll").focus()
                await page.keyboard.press("ArrowRight")
                for _ in range(20):
                    if await page.locator('.bank-scroll').evaluate('e => e.scrollLeft > 0'):
                        break
                    await asyncio.sleep(.05)
                assert await page.locator('.bank-scroll').evaluate('e => e.scrollLeft > 0')

                await page.locator("#t-mind").click()
                # Resident model names/IDs can have no spaces.
                await playwright.expect(page.locator('#h-models')).to_contain_text(model_name)
                await fits(page)
                first = page.locator("#h-models td").first
                assert await first.evaluate("e => getComputedStyle(e).position") == "sticky"

                await page.locator('#t-personas').click()
                await page.locator('.tile-detail').first.click()
                await page.locator('#p-detail').wait_for(state='visible')
                await fits(page)
                await page.locator('#p-close').click()
                await page.locator('#p-wall .gallery-opener').first.click()
                gallery = page.locator('.photo-gallery')
                await gallery.wait_for(state='visible')
                rect = await gallery.bounding_box()
                assert rect['width'] <= width and rect['height'] <= height
                assert await gallery.evaluate('e => e.scrollWidth <= e.clientWidth')
                await page.keyboard.press('Escape')
                await gallery.wait_for(state='detached')

                board = {"realm_on": True, "replies_on": True, "missing": [], "names": {"101": "Synthetic"},
                         "letters": [], "writeups": [], "forum": [{"topic": "Synthetic topic", "posts": [
                             {"id": 1, "author_kind": "member", "author": 101, "name": "Synthetic",
                              "text": "A synthetic post with a long reply target", "ts": 1, "replies": []}]}]}
                await page.route('**/api/board', lambda route: route.fulfill(json=board))
                await page.locator('#t-board').click()
                await page.locator('.replybtn').first.click()
                await page.locator('.replybox textarea').fill('A reply kept in the scratch page only.')
                await fits(page)

                await page.locator('#toasts').evaluate("""box => {
                  const toast=document.createElement('div'); toast.className='toast';
                  toast.textContent='A long notification with an unbroken identifier: ' + 'x'.repeat(100);
                  box.append(toast);
                }""")
                toast = await page.locator('.toast').bounding_box()
                assert 0 <= toast['x'] and toast['x'] + toast['width'] <= width

                await page.locator("#confirm").evaluate("""e => {
                  document.querySelector('#confirm-text').textContent = 'A long confirmation. '.repeat(100);
                  e.hidden = false;
                }""")
                dialog = await page.locator(".dialog").bounding_box()
                assert 0 <= dialog["x"] and dialog["x"] + dialog["width"] <= width
                assert 0 <= dialog["y"] and dialog["y"] + dialog["height"] <= height
                await page.locator("#confirm-no").scroll_into_view_if_needed()
                cancel = await page.locator("#confirm-no").bounding_box()
                assert cancel["height"] >= 44 and cancel["y"] + cancel["height"] <= height
    asyncio.run(scenario())
