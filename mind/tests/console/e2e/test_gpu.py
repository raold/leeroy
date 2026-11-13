"""The actual top-bar control and endpoint, with only synthetic model residency."""
import asyncio

import pytest

from selfies.fakes import until
from console.rig import KEY
from .test_tabs import open_console, visit


class Models:
    def __init__(self):
        self.models = [{'name':'fake-chat','size_vram':16*1024**3},
                       {'name':'fake-embed','size_vram':round(1.1*1024**3)}]
        self.calls = []
        self.fail = False

    async def loaded(self):
        return list(self.models)

    async def loaded_models(self):
        return [m['name'] for m in self.models]

    async def unload(self, name):
        self.calls.append(('unload',name))
        if self.fail:
            raise OSError('synthetic unload failure')
        self.models = [m for m in self.models if m['name'] != name]

    async def preload(self, name, keep_alive, *, options=None):
        self.calls.append(('preload',name))


async def logout(r):
    await r.bridge.push({'type':'logout','guid':r.app.human_guid,'human':True})
    await until(lambda:not r.app.world.human_online and not r.app.night_scheduler.presence.online
                and 'human' not in r.app.arbiter.holders,10)
    await r.console.refresh(force=True)


@pytest.mark.parametrize('theme',['dark','light'])
def test_gpu_bar_refuses_in_game_then_pauses_and_resumes_on_every_tab(rehearsal,tmp_path,theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390,theme=theme) as r:
            admin = Models()
            r.app.arbiter.admin = admin
            r.expected_http_errors.add(('/api/gpu',409))
            await open_console(r)
            button = r.page.locator('#gpu')
            await expect(button).to_have_attribute('aria-disabled','false')
            await button.focus()
            await r.page.keyboard.press('Enter')
            await expect(r.page.locator('#toasts')).to_contain_text('Log out of WoW first')
            await expect(button).to_have_attribute('title','Log out of WoW first: your game keeps the model loaded')
            assert not r.app.gpu.paused and not admin.calls
            await logout(r)
            await button.click()
            await expect(button).to_have_text('GPU free · Resume')
            await expect(button).to_have_attribute('aria-busy','false')
            await expect(r.page.locator('#gpu-banner')).to_contain_text('Members are resting')
            await expect(r.page.locator('#toasts')).to_contain_text('Freed 17.1 GB')
            assert r.app.gpu.paused and r.app.store.get_meta('gpu:paused')
            assert admin.calls == [('unload','fake-chat'),('unload','fake-embed')]
            for tab in ('tonight','ops'):
                panel = await visit(r.page,tab)
                await expect(button).to_be_visible()
                await expect(panel).to_contain_text('GPU paused since')
                await expect(r.page.locator('#gpu-banner')).to_be_visible()
            await r.page.screenshot(path=str(tmp_path/f'gpu-{theme}-390.png'))
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await button.focus()
            await r.page.keyboard.press('Enter')
            await expect(button).to_have_text('Free GPU')
            await expect(button).to_have_attribute('aria-busy','false')
            await expect(r.page.locator('#gpu-banner')).to_be_hidden()
            assert not r.app.gpu.paused and r.app.store.get_meta('gpu:paused') is None
            assert not r.errors,r.errors
    asyncio.run(run())


def test_gpu_readonly_button_explains_without_posting(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.console.controls.enabled = False
            await open_console(r)
            button = r.page.locator('#gpu')
            await expect(button).to_have_attribute('aria-disabled','true')
            await button.focus()
            await r.page.keyboard.press('Enter')
            await expect(r.page.locator('#toasts')).to_contain_text('GPU pause and resume are disabled')
            posts = await r.page.evaluate("window.__answers.filter(x=>x.path==='/api/gpu')")
            assert posts == [] and not r.app.gpu.paused
            assert not r.errors,r.errors
    asyncio.run(run())


def test_partial_gpu_unload_keeps_a_truthful_retry(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            admin = Models(); admin.fail = True
            r.app.arbiter.admin = admin
            r.expected_http_errors.add(('/api/gpu',409))
            await open_console(r)
            await logout(r)
            button = r.page.locator('#gpu')
            await button.click()
            await expect(button).to_have_text('Retry Free GPU')
            await expect(button).to_have_attribute('aria-busy','false')
            await expect(r.page.locator('#gpu-banner')).to_contain_text('models may still be loaded')
            assert r.app.gpu.paused and admin.models
            admin.fail = False
            await button.click()
            await expect(button).to_have_text('GPU free · Resume')
            await expect(button).to_have_attribute('aria-busy','false')
            assert not admin.models and r.app.gpu.paused
            assert not r.errors,r.errors
    asyncio.run(run())


def test_gpu_bar_unloads_with_the_mind_stopped(rehearsal,monkeypatch):
    from leeroy_mind.console import gpuview
    admin = Models()
    actual = gpuview.stopped
    async def fake_admin(url,action):
        return await actual(url,action,admin_factory=lambda *a,**kw:admin)
    monkeypatch.setattr(gpuview,'stopped',fake_admin)
    async def run():
        from playwright.async_api import expect
        async with rehearsal(state='stopped') as r:
            await r.page.goto(r.url+'/#key='+KEY)
            await expect(r.page.locator('#s-mind')).to_contain_text('mind stopped')
            button = r.page.locator('#gpu')
            await expect(button).to_have_attribute('aria-disabled','false')
            await button.click()
            await expect(r.page.locator('#toasts')).to_contain_text('The mind is stopped; unloaded 2 models.')
            await expect(button).to_have_text('Free GPU')
            assert admin.calls == [('unload','fake-chat'),('unload','fake-embed')]
            assert not admin.models and not r.errors,r.errors
    asyncio.run(run())
