"""The top-bar Render selfies button: the real mind, console and endpoint, the fake worker only."""
import asyncio
import json

import pytest

from leeroy_mind.selfies.manual import LAST
from leeroy_mind.selfies.store import queued_sets, ready_set
from leeroy_mind.selfies.worker import SelfieWorker
from selfies.fakes import fake_command, until
from .test_gpu import logout
from .test_tabs import open_console, visit


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_render_bar_refuses_in_game_then_confirms_renders_and_stops_on_every_tab(rehearsal, tmp_path, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390, theme=theme) as r:
            # The first set renders, then the fake worker never answers: a run to watch and stop.
            log = tmp_path / 'renders.log'
            command, env = fake_command(r.root, FAKE_SELFIE_HANG_AFTER='4', FAKE_SELFIE_LOG=str(log))
            r.app.selfie_manual.worker = lambda: SelfieWorker(command, env=env)
            r.expected_http_errors.add(('/api/selfies/render', 409))
            await open_console(r)
            button = r.page.locator('#render')
            await expect(button).to_be_visible()
            words = 'Log out of WoW first: photos render only while you are out of the game.'
            await expect(button).to_have_attribute('title', words)
            await expect(button).to_have_attribute('aria-disabled', 'true')
            await expect(button).to_have_text('Render selfies (7)')  # the backlog shows anyway
            await button.focus()
            await r.page.keyboard.press('Enter')
            await expect(r.page.locator('#toasts')).to_contain_text('Log out of WoW first')
            assert not r.app.selfie_manual.running
            await logout(r)
            # Seven members have looks and a photo kind but no photo yet: seven first sets.
            await expect(button).to_have_text('Render selfies (7)')
            await expect(button).to_have_attribute('aria-disabled', 'false')
            await button.focus()
            await r.page.keyboard.press('Enter')
            dialog = r.page.locator('#render-confirm')
            await expect(dialog).to_be_visible()
            await expect(dialog).to_contain_text('Renders 7 now: 7 first sets.')
            await expect(dialog).to_contain_text('Rough time: no estimate yet')  # nothing timed in the rehearsal
            await expect(dialog).to_contain_text('log into WoW or post in Discord')
            await r.page.screenshot(path=str(tmp_path / f'render-confirm-{theme}-390.png'))
            await r.page.keyboard.press('Escape')
            await expect(dialog).to_be_hidden()
            await expect(button).to_be_focused()
            assert not r.app.selfie_manual.running
            await button.click()
            await r.page.locator('#render-confirm-go').click()
            await expect(r.page.locator('#toasts')).to_contain_text('Rendering 7 now: 7 first sets.')
            await until(lambda: r.app.selfie_manual._progress and r.app.selfie_manual._progress['done'] == 1, 20)
            await r.console.refresh(force=True)
            await expect(button).to_have_text('Rendering 2 of 7 · Stop')
            for tab in ('tonight', 'ops', 'personas'):
                await visit(r.page, tab)
                await expect(button).to_be_visible()
                await expect(button).to_have_text('Rendering 2 of 7 · Stop')
            await r.page.screenshot(path=str(tmp_path / f'render-{theme}-390.png'))
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await button.click()
            await expect(r.page.locator('#toasts')).to_contain_text(
                'Selfie render stopped: 1 set rendered. The rest stays queued.')
            await expect(button).to_have_text('Render selfies (6)')
            rec = json.loads(r.app.store.get_meta(LAST))
            assert (rec['outcome'], rec['by'], rec['sets']) == ('stopped', 'owner', 1)
            assert not r.app.selfie_manual.running
            assert len([s for s in queued_sets(r.app.store)]) == 6
            panel = await visit(r.page, 'ops')
            # Ops polls every 15 s, and its panel may have been read while the run rendered.
            await expect(panel).to_contain_text('Selfie render stopped: 1 set rendered', timeout=40000)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_render_bar_is_disabled_with_its_reason_under_no_control(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.console.controls.enabled = False
            r.console.controls.reason = 'Control is off: this console runs with --no-control.'
            await open_console(r)
            button = r.page.locator('#render')
            await expect(button).to_have_attribute('aria-disabled', 'true')
            await expect(button).to_have_attribute('title', 'Control is off: this console runs with --no-control.')
            await button.focus()
            await r.page.keyboard.press('Enter')
            await expect(r.page.locator('#toasts')).to_contain_text('--no-control')
            assert not r.app.selfie_manual.running
            assert not r.errors, r.errors
    asyncio.run(run())
