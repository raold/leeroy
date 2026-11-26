"""Files dragged from the desktop onto a DM, pasted photos and his short videos (videos/README.md):
DataTransfer-synthesized drags and pastes on the real page, console and mind. Every image is
drawn by the page's canvas or ffmpeg's test pattern (media.py); ffmpeg is faked (harness)."""
import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from leeroy_mind.videos import files as video_files
from .media import WEBM
from .test_accessibility import AXE
from .test_tabs import choose, open_console

DM = '#dc-dms [data-view="dm:101"]'

# A drag as the browser starts one: a DataTransfer holding the files (and any text), then
# dragenter and dragover on `selector`. Returns whether each was taken, the overlay and what the
# page let the drop do. The drag stays in window.__drag for drop() and axe.
DRAG = """({ selector, files, text }) => {
  const dt = new DataTransfer();
  for (const f of files) {
    const bytes = Uint8Array.from(atob(f.b64), c => c.charCodeAt(0));
    dt.items.add(new File([bytes], f.name, { type: f.type }));
  }
  if (text !== null) { dt.setData('text/plain', text); }
  // A synthesized drag ignores dropEffect: keep what the page asks for, as a real drag would
  let effect = 'none';
  Object.defineProperty(dt, 'dropEffect', { configurable: true, get: () => effect, set: v => { effect = v; } });
  window.__drag = dt;
  const target = document.querySelector(selector);
  const enter = new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true });
  const over = new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true });
  target.dispatchEvent(enter); target.dispatchEvent(over);
  return { entered: enter.defaultPrevented, over: over.defaultPrevented, effect: dt.dropEffect,
           overlay: !document.getElementById('dc-drop').hidden };
}"""
# The drop lands on what is under the pointer: the overlay while it shows, else `selector`
DROP = """(selector) => {
  const overlay = document.getElementById('dc-drop');
  const target = overlay.hidden ? document.querySelector(selector) : overlay;
  const drop = new DragEvent('drop', { dataTransfer: window.__drag, bubbles: true, cancelable: true });
  target.dispatchEvent(drop);
  return { taken: drop.defaultPrevented, overlay: !overlay.hidden };
}"""
PASTE = """({ files, text }) => {
  const dt = new DataTransfer();
  for (const f of files) {
    dt.items.add(new File([Uint8Array.from(atob(f.b64), c => c.charCodeAt(0))], f.name, { type: f.type }));
  }
  if (text !== null) { dt.setData('text/plain', text); }
  const paste = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
  document.getElementById('dc-text').dispatchEvent(paste);
  return paste.defaultPrevented;
}"""
PNG = """() => {
  const c = document.createElement('canvas'); c.width = 640; c.height = 480;
  const g = c.getContext('2d'); g.fillStyle = '#fedc65'; g.fillRect(0, 0, 640, 480);
  g.fillStyle = '#3366aa'; g.fillRect(160, 120, 320, 240); return c.toDataURL('image/png').split(',')[1];
}"""
AXE_ON = """async (selector) => {
  const r = await axe.run(document.querySelector(selector), { runOnly: { type: 'tag',
    values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } });
  return r.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => n.target) }));
}"""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


async def drag(page, selector, files=(), text=None):
    return await page.evaluate(DRAG, {'selector': selector, 'files': list(files), 'text': text})


async def png(page):
    return {'name': 'drawn.png', 'type': 'image/png', 'b64': await page.evaluate(PNG)}


def messages(r):
    return r.app.store.conn.execute(
        "SELECT m.id, m.text, a.kind FROM discord_message m LEFT JOIN discord_attachment a ON a.message = m.id "
        "WHERE m.channel = 'dm:101' AND m.origin = 'human' ORDER BY m.id").fetchall()


async def axe_clean(r, selector, tmp_path, name):
    if not AXE.is_file():
        message = 'Install console accessibility checks: npm ci --prefix tests/console/e2e'
        if os.environ.get('LEEROY_CONSOLE_E2E_REQUIRED'):
            pytest.fail(message)
        pytest.skip(message)
    if not await r.page.evaluate('typeof axe !== "undefined"'):
        await r.page.evaluate(AXE.read_text(encoding='utf-8'))
    found = await r.page.evaluate(AXE_ON, selector)
    (tmp_path / f'axe-{name}.json').write_text(json.dumps(found, indent=2))
    assert not [v for v in found if v['impact'] in ('serious', 'critical')], found


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_a_photo_dropped_on_the_dm_attaches_and_goes_with_the_send(rehearsal, tmp_path, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(theme=theme, photos=True) as r:
            r.app.llm._script = lambda *_: 'A blue square on a yellow background.'
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            await r.page.locator('#dc-text').fill('Look what I found')
            photo = await png(r.page)
            dragged = await drag(r.page, '#feed', [photo])
            assert dragged == {'entered': True, 'over': True, 'effect': 'copy', 'overlay': True}
            await expect(r.page.locator('#dc-drop-title')).to_have_text('Drop to attach')
            await expect(r.page.locator('#dc-drop-hint')).to_have_text('A photo')
            await r.page.screenshot(path=str(tmp_path / f'drop-overlay-{theme}.png'))
            await axe_clean(r, '#dc-drop', tmp_path, f'overlay-{theme}')
            dropped = await r.page.evaluate(DROP, '#feed')
            assert dropped == {'taken': True, 'overlay': False}
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            await expect(r.page.locator('#dc-photo-preview-img')).to_have_js_property('naturalWidth', 640)
            await expect(r.page.locator('#dc-video-preview')).to_be_hidden()
            await expect(r.page.locator('#dc-text')).to_be_focused()
            await expect(r.page.locator('#dc-text')).to_have_value('Look what I found')
            await r.page.locator('#dc-send').click()
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            await expect(r.page.locator('#feed .dc-photo').last).to_be_visible()
            rows = messages(r)
            assert [(row['text'], row['kind']) for row in rows] == [('Look what I found', 'upload')]
            assert not r.errors, r.errors
    asyncio.run(run())


def test_text_and_links_drag_as_usual_and_files_never_open_over_the_console(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(photos=True) as r:
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            # dragged words: the page takes nothing, so the textarea's own drop puts them in
            words = await drag(r.page, '#dc-text', text='https://example.invalid/a-link')
            assert words == {'entered': False, 'over': False, 'effect': 'none', 'overlay': False}
            assert (await r.page.evaluate(DROP, '#dc-text'))['taken'] is False
            # files over a WoW channel: no overlay, and the page refuses the drop
            await choose(r.page, '#comms-left [data-view="guild"]')
            refused = await drag(r.page, '#feed', [await png(r.page)])
            assert refused['overlay'] is False and refused['over'] is True and refused['effect'] == 'none'
            assert (await r.page.evaluate(DROP, '#feed'))['taken'] is True
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert not r.errors, r.errors
    asyncio.run(run())


def test_unsupported_files_are_refused_in_words_and_one_file_attaches(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(photos=True) as r:
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            pdf = {'name': 'notes.pdf', 'type': 'application/pdf', 'b64': b64(b'%PDF-1.4 synthetic')}
            await drag(r.page, '#dc-composer', [pdf])
            await r.page.evaluate(DROP, '#dc-composer')
            await expect(r.page.locator('#dc-why')).to_have_text('Only photos can be attached here.')
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            # a video while videos are off says so
            clip = {'name': 'clip.webm', 'type': 'video/webm', 'b64': b64(WEBM)}
            await drag(r.page, '#feed', [clip])
            await r.page.evaluate(DROP, '#feed')
            await expect(r.page.locator('#dc-why')).to_contain_text('Videos are off in this mind')
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            # several at once: the first that can attach, and a note that the others were left
            photo = await png(r.page)
            await drag(r.page, '#feed', [pdf, photo, photo | {'name': 'second.png'}])
            await r.page.evaluate(DROP, '#feed')
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            await expect(r.page.locator('#dc-why')).to_have_text(
                'One photo or video per message: attached the first photo among them.')
            assert messages(r) == []
            assert not r.errors, r.errors
    asyncio.run(run())


def test_a_pasted_photo_attaches_and_pasted_text_stays_text(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(photos=True) as r:
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            assert await r.page.evaluate(PASTE, {'files': [], 'text': 'plain words'}) is False
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert await r.page.evaluate(PASTE, {'files': [await png(r.page)], 'text': None}) is True
            await expect(r.page.locator('#dc-photo-preview')).to_be_visible()
            await r.page.locator('#dc-send').click()
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert [row['kind'] for row in messages(r)] == ['upload']
            assert not r.errors, r.errors
    asyncio.run(run())


def test_with_photos_off_the_overlay_says_why_and_nothing_attaches(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            await expect(r.page.locator('#dc-photo-pick')).to_be_hidden()
            assert (await drag(r.page, '#feed', [await png(r.page)]))['overlay'] is True
            await expect(r.page.locator('#dc-drop-title')).to_have_text("Can't attach here")
            await expect(r.page.locator('#dc-drop-hint')).to_have_text('Photo uploads are off in this mind.')
            await r.page.evaluate(DROP, '#feed')
            await expect(r.page.locator('#dc-why')).to_have_text('Photo uploads are off in this mind.')
            await expect(r.page.locator('#dc-photo-preview')).to_be_hidden()
            assert not r.errors, r.errors
    asyncio.run(run())


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_a_dropped_video_is_sent_shown_and_played_inline(rehearsal, tmp_path, theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(theme=theme, photos=True, videos=True) as r:
            r.app.llm._script = lambda *_: 'A test card scrolls past with a counting clock.'
            queued = []
            r.app.engine._queue = lambda items: queued.extend(items)
            await open_console(r, timeout=15000)
            await choose(r.page, DM)
            await expect(r.page.locator('#dc-photo-pick')).to_have_text('Attach photo or video')
            assert 'video/webm' in await r.page.locator('#dc-photo-file').get_attribute('accept')
            clip = {'name': 'clip.webm', 'type': 'video/webm', 'b64': b64(WEBM)}
            assert (await drag(r.page, '#dc-composer', [clip]))['overlay'] is True
            await expect(r.page.locator('#dc-drop-hint')).to_have_text(
                'A photo, or an mp4, webm or mov video up to 50 MiB')
            await axe_clean(r, '#dc-drop', tmp_path, f'video-overlay-{theme}')
            await r.page.evaluate(DROP, '#dc-composer')
            preview = r.page.locator('#dc-video-preview')
            await expect(preview).to_be_visible()
            assert (await preview.get_attribute('src')).startswith('blob:')
            await expect(r.page.locator('#dc-photo-remove')).to_have_text('Remove video')
            await expect(r.page.locator('#dc-send')).to_be_enabled()  # a video needs no words
            await r.page.locator('#dc-send').click()
            await expect(preview).to_be_hidden()
            box = r.page.locator('#feed .dc-video').last
            play = box.locator('.dc-video-play')
            await expect(play).to_have_text('Play video · 0:04')
            await expect(play).to_have_attribute('aria-label', 'Play video from you, 0:04')
            # the send carried the token: the mind adopted the very bytes he dropped
            rows = messages(r)
            assert [(row['text'], row['kind']) for row in rows] == [('', 'upload')]
            clip_row = video_files.by_messages(r.app.store.conn, [rows[0]['id']])[rows[0]['id']]
            assert (r.data / clip_row['path']).read_bytes() == WEBM
            assert (clip_row['mime'], clip_row['ms'], clip_row['frames']) == ('video/webm', 4000, 3)
            await expect(box.locator('.dc-video-still')).to_have_js_property('naturalWidth', 204)
            await r.page.screenshot(path=str(tmp_path / f'video-poster-{theme}.png'))
            await axe_clean(r, '#feed', tmp_path, f'video-feed-{theme}')
            await play.click()
            player = box.locator('video.dc-video-player')
            await expect(player).to_be_visible()
            assert await player.evaluate('v => v.controls && v.src.startsWith("blob:")')
            await expect(player).to_be_focused()
            await expect(play).to_be_hidden()
            await r.page.screenshot(path=str(tmp_path / f'video-player-{theme}.png'))
            # the member's answer is told it is a short video she saw stills of
            for _ in range(100):
                if queued:
                    break
                await asyncio.sleep(.05)
            assert queued and 'sent a short video (4 seconds)' in queued[0][0].instruction
            assert 'counting clock' in queued[0][0].instruction
            assert not r.errors, r.errors
    asyncio.run(run())
