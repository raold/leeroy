import asyncio
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from .harness import Rehearsal

# Each POST's answer as the page itself read it, for test_flows.submit(). Reading the body
# through Playwright's network layer (Response.json) depends on Chromium keeping it, and on
# CI it now and then had not ("No data found for resource"), or the request never reported
# finished at all. A clone of the page's own response does not depend on either.
ANSWERS = """
(() => {
  const real = window.fetch;
  window.__answers = [];
  window.fetch = function (input, init) {
    const work = real.apply(this, arguments);
    const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    if (method !== "POST") { return work; }
    const path = new URL(typeof input === "string" ? input : input.url, location.href).pathname;
    return work.then((response) => {
      response.clone().text().then(
        (text) => window.__answers.push({ path, status: response.status, text }),
        () => window.__answers.push({ path, status: response.status, text: null }));
      return response;
    });
  };
})();
"""


@pytest.fixture
def rehearsal(tmp_path, monkeypatch, pytestconfig):
    try:
        import playwright.async_api as playwright
    except ImportError:
        message = "Console E2E needs Playwright: pip install -r tests/console/e2e/requirements.txt"
        if os.environ.get("LEEROY_CONSOLE_E2E_REQUIRED"):
            pytest.fail(message)
        pytest.skip(message)

    @asynccontextmanager
    async def start(*, width=1440, theme="dark", state="running", photos=False, learning=False):
        # A rehearsal that runs past 75 s prints every asyncio task's stack once, so a
        # wait with no deadline names itself before the job's timeout cancels it. Capture
        # is suspended for it: a job killed mid-test never shows captured output.
        loop = asyncio.get_running_loop()
        capture = pytestconfig.pluginmanager.getplugin("capturemanager")
        def dump():
            with capture.global_and_fixture_disabled():
                print("\n--- asyncio tasks after 75 s ---", file=sys.stderr)
                for task in asyncio.all_tasks(loop):
                    task.print_stack(file=sys.stderr)
                sys.stderr.flush()
        def fire():
            try:
                loop.call_soon_threadsafe(dump)
            except RuntimeError:  # the loop closed before the timer was cancelled
                pass
        watchdog = threading.Timer(75, fire)
        watchdog.daemon = True
        watchdog.start()
        try:
            async with _browser(width, theme, state, photos, learning) as fixture:
                yield fixture
        finally:
            watchdog.cancel()

    @asynccontextmanager
    async def _browser(width, theme, state, photos, learning):
        async with playwright.async_playwright() as pw:
            if not Path(pw.chromium.executable_path).is_file():
                message = "Chromium is not installed; run python -m playwright install chromium"
                if os.environ.get("LEEROY_CONSOLE_E2E_REQUIRED"):
                    pytest.fail(message)
                pytest.skip(message)
            browser = await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": width, "height": 1000},
                                                reduced_motion="reduce")
            fixture = Rehearsal(tmp_path, monkeypatch)
            errors = []
            fixture.expected_http_errors = set()
            page = await context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            # faces.mjs deliberately probes for optional photos: 404 means initials.
            # Do not suppress other 404s, JavaScript errors, or failed API responses.
            def console_message(msg):
                if msg.type != "error":
                    return
                if (msg.text == "Failed to load resource: the server responded with a status of 404 (Not Found)"
                        and re.fullmatch(re.escape(fixture.url) + r"/api/face\?guid=\d+&size=(sm|md|lg)",
                                         msg.location.get("url", ""))):
                    return
                status = re.fullmatch(r"Failed to load resource: the server responded with a status of (\d+) \(.+\)", msg.text)
                if status and (urlsplit(msg.location.get("url", "")).path, int(status[1])) in fixture.expected_http_errors:
                    return
                errors.append(f"{msg.location}: {msg.text}")
            page.on("console", console_message)
            def response_seen(response):
                path = urlsplit(response.url).path
                if response.status < 400 or (path, response.status) in fixture.expected_http_errors:
                    return
                if response.status == 404 and re.fullmatch(
                        re.escape(fixture.url) + r"/api/face\?guid=\d+&size=(sm|md|lg)", response.url):
                    return
                errors.append(f"HTTP {response.status}: {response.url}")
            page.on("response", response_seen)
            fixture.page, fixture.context, fixture.errors = page, context, errors
            try:
                await fixture.start(state, photos=photos, learning=learning)
                # Browser requests may reach ONLY this fixture's own console: the console's
                # CSP ('self') confines the page, and any other destination fails the test.
                # Observed, not intercepted: with context.route() interception Chromium now
                # and then never finished a routed POST's response (no requestfinished, body
                # unreadable), which hung or failed the rehearsal.
                own = fixture.url + "/"
                def own_origin(request):
                    # blob: and data: URLs never leave the browser (faces load as blobs).
                    if not (request.url.startswith((own, "blob:" + own, "data:"))):
                        errors.append("unexpected browser destination: " + request.url)
                context.on("request", own_origin)
                await context.add_init_script(
                    "if (!localStorage.getItem('guild-console-theme')) "
                    "localStorage.setItem('guild-console-theme', " + repr(theme) + ");")
                await context.add_init_script(ANSWERS)
                yield fixture
            except BaseException:
                if not page.is_closed():
                    await page.screenshot(path=str(tmp_path / "failure.png"), full_page=True)
                    (tmp_path / "failure.html").write_text(await page.content(), encoding="utf-8")
                raise
            finally:
                try:
                    await context.close()
                finally:
                    try:
                        await browser.close()
                    finally:
                        await fixture.close()
    return start
