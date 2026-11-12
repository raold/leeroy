import asyncio
import base64
import time

import pytest

from .test_tabs import open_console


def test_preparing_photo_blocks_caption_send(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(photos=True) as r:
            r.app.llm._script = lambda *_: 'A blue square.'
            await open_console(r, timeout=15000)
            await r.page.locator('#dc-channels [data-view="d:pics"]').click()
            await r.page.locator('#dc-text').fill('Caption with my photo')
            data = await r.page.evaluate('''() => {
                const c = document.createElement('canvas'); c.width = c.height = 32;
                c.getContext('2d').fillRect(0, 0, 32, 32);
                const image = c.toDataURL('image/png');
                const original = HTMLCanvasElement.prototype.toBlob;
                HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
                    window.finishPhoto = () => original.call(this, callback, ...args);
                    document.documentElement.dataset.photoWaiting = 'true';
                };
                return image;
            }''')
            await r.page.locator('#dc-photo-file').set_input_files({
                'name':'fixture.png', 'mimeType':'image/png', 'buffer':base64.b64decode(data.split(',')[1])})
            await expect(r.page.locator('html')).to_have_attribute('data-photo-waiting', 'true')
            await expect(r.page.locator('#dc-send')).to_be_disabled()
            await expect(r.page.locator('#dc-why')).to_contain_text('photo is being prepared')
            await r.page.locator('#dc-text').press('Enter')
            await r.page.locator('#dc-send').evaluate('(button) => button.click()')
            assert r.app.store.conn.execute("SELECT count(*) FROM discord_message WHERE text = ?",
                ('Caption with my photo',)).fetchone()[0] == 0
            await r.page.evaluate('window.finishPhoto()')
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            await expect(r.page.locator('#dc-send')).to_be_enabled()
            await r.page.locator('#dc-text').press('Enter')
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert r.app.store.conn.execute('''SELECT count(*) FROM discord_message m
                JOIN discord_attachment a ON a.message = m.id WHERE m.text = ?''',
                ('Caption with my photo',)).fetchone()[0] == 1
            assert not r.errors, r.errors
    asyncio.run(run())


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_photo_pick_remove_retry_post_and_keyboard_viewer(rehearsal, tmp_path, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390, theme=theme, photos=True) as r:
            r.app.llm._script = lambda *_: 'A blue square on a yellow background.'
            await open_console(r, timeout=15000)
            await r.page.locator('#dc-channels [data-view="d:pics"]').click()
            await expect(r.page.locator('#dc-photo-pick')).to_be_visible()
            # A synthetic, decodable image, made locally by the browser; no real photos.
            data = await r.page.evaluate('''() => {
                const c = document.createElement('canvas'); c.width = 2000; c.height = 1000;
                const g = c.getContext('2d'); g.fillStyle = '#fedc65'; g.fillRect(0,0,2000,1000);
                g.fillStyle = '#3366aa'; g.fillRect(500,200,600,600); return c.toDataURL('image/png');
            }''')
            picked = {'name':'fixture.png', 'mimeType':'image/png', 'buffer':base64.b64decode(data.split(',')[1])}
            picker = r.page.locator('#dc-photo-file')
            await picker.set_input_files(picked)
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            await r.page.locator('#dc-photo-remove').click()
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            await expect(r.page.locator('#dc-send')).to_be_disabled()
            await picker.set_input_files(picked)
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            # A real refusal from the scratch console preserves the photo draft.
            # Avoid intercepting POST responses (the existing E2E hang regression).
            r.expected_http_errors.add(('/api/discord/upload', 429))
            r.console._photo_uploads[:] = [time.monotonic()] * 4
            await r.page.locator('#dc-send').click()
            await expect(r.page.locator('#dc-why')).to_contain_text('minute')
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            r.console._photo_uploads.clear()
            # Adoption can finish before the SQL transaction fails. Retrying
            # must stage a fresh token, while preserving the orphan image.
            from leeroy_mind.discord.store import DiscordRefused
            original = r.app.discord._his_line
            def refuse(*args):
                raise DiscordRefused('missing')
            r.app.discord._his_line = refuse
            r.expected_http_errors.add(('/api/discord', 409))
            try:
                await r.page.locator('#dc-send').click()
                await expect(r.page.locator('#dc-why')).to_contain_text('no such message')
                await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            finally:
                r.app.discord._his_line = original
            orphan = {p: p.read_bytes() for p in (r.data / 'discord').rglob('*') if p.is_file()}
            await r.page.locator('#dc-send').click()
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert all(p.read_bytes() == data for p, data in orphan.items())
            photo = r.page.locator('#feed .dc-photo').first
            await expect(photo).to_be_visible()
            await expect(photo.locator('img')).to_have_js_property('naturalWidth', 1600)
            attachment = r.app.store.conn.execute('SELECT * FROM discord_attachment').fetchone()
            assert (attachment['width'], attachment['height']) == (1600, 800)
            inventory = {p.relative_to(r.data): p.read_bytes() for p in (r.data / 'discord').rglob('*') if p.is_file()}
            assert any(str(p).endswith('.adopted') for p in inventory)
            await photo.focus()
            await r.page.keyboard.press('Enter')
            viewer = r.page.locator('#dc-photo-view')
            await expect(viewer).to_be_visible()
            await expect(r.page.locator('#dc-photo-view-img')).to_have_js_property('naturalWidth', 1600)
            await expect(r.page.locator('#dc-photo-view-close')).to_be_focused()
            await r.page.keyboard.press('Tab')
            await r.page.keyboard.press('Tab')
            assert await r.page.evaluate("document.querySelector('#dc-photo-view').contains(document.activeElement)")
            await r.page.screenshot(path=str(tmp_path / f'photo-viewer-{theme}.png'))
            await r.page.keyboard.press('Escape')
            await expect(viewer).to_be_hidden()
            await expect(photo).to_be_focused()
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            await r.page.screenshot(path=str(tmp_path / f'photo-feed-{theme}.png'))
            assert inventory == {p.relative_to(r.data): p.read_bytes() for p in (r.data / 'discord').rglob('*') if p.is_file()}
            assert not r.errors, r.errors
    asyncio.run(run())
