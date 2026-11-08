import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from .harness import Rehearsal


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    try:
        import playwright.async_api as playwright
    except ImportError:
        message = "Console E2E needs Playwright: pip install -r tests/console/e2e/requirements.txt"
        if os.environ.get("LEEROY_CONSOLE_E2E_REQUIRED"):
            pytest.fail(message)
        pytest.skip(message)

    @asynccontextmanager
    async def start(*, width=1440, theme="dark", state="running"):
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
                await fixture.start(state)
                # Browser requests may reach ONLY this fixture's own console.
                async def own_origin(route):
                    if route.request.url.startswith(fixture.url + "/"):
                        await route.continue_()
                    else:
                        errors.append("unexpected browser destination: " + route.request.url)
                        await route.abort()
                await context.route("**/*", own_origin)
                await context.add_init_script(
                    "if (!localStorage.getItem('guild-console-theme')) "
                    "localStorage.setItem('guild-console-theme', " + repr(theme) + ");")
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
