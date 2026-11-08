"""Real scratch console, --no-control semantics, fake probes, no model or network service."""
import asyncio

import pytest

from conftest import seed_test_cast
from console.rig import make_app
from console.test_console_phone import browser_page, playwright
from console.test_console_server import served
from console.test_persona_insight import NOW, claim, report, save
from leeroy_mind.console.control import Controls


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_insight_phone_read_only(store, tmp_path, theme):
    seed_test_cast(store)
    claim(store)
    save(tmp_path, report())

    async def scenario():
        app = make_app(tmp_path, controls=Controls(enabled=False), clock=lambda:NOW)
        async with served(app):
            async with browser_page(app, 390, 844, theme) as page:
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.locator('#t-personas').click()
                await page.locator('#p-wall .tile[data-guid="101"] .tile-detail').click()
                await page.locator('#persona-insight h4').filter(has_text='Claims').wait_for()
                assert 'saurfang' in await page.locator('#insight-parts').inner_text()
                await page.get_by_label('Awareness token budget').select_option('small')
                assert 'Dropped at this budget:' in await page.locator('#insight-dropped').inner_text()
                await page.locator('#insight-line').fill('PRIVATE_BROWSER_LINE')
                await page.locator('#insight-submit').click()
                await playwright.expect(page.locator('#insight-result')).to_contain_text('not available')
                assert 'PRIVATE_BROWSER_LINE' not in await page.locator('#insight-result').inner_text()
                assert await page.locator('#persona-insight').evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert errors == []
                await page.locator('#persona-insight h3').evaluate('(e) => e.scrollIntoView({block:"center"})')
                await page.screenshot(path=tmp_path / f'insight-{theme}.png')
    asyncio.run(scenario())
