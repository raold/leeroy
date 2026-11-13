"""DM audio through the real console with invented WAVs, microphone and transcription."""
import asyncio
import io
import math
import struct
import time
import uuid
import wave

import pytest

from console.rig import KEY
from leeroy_mind.console import feed
from leeroy_mind.discord import store as discord
from leeroy_mind.voice import wire
from .test_tabs import open_console
from .test_flows import submit


def audio_inventory(r):
    return {p:p.read_bytes() for p in (r.data/'discord').rglob('*') if p.suffix in ('.wav','.ogg') and p.is_file()}


def retained(before):
    assert all(path.is_file() and path.read_bytes()==data for path,data in before.items())


def seed_note(r):
    raw=io.BytesIO()
    with wave.open(raw,'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h',int(300*math.sin(i*math.tau*220/16000))) for i in range(48000)))
    path=r.data/'discord'/'voice'/(uuid.uuid4().hex+'.wav'); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(raw.getvalue())
    with r.app.store.transaction():
        mid=discord.post(r.app.store,channel='dm:101',author=101,origin='mind',
            text='I packed the tea for our next adventure.',now=r.app.clock.now())
        r.app.store.conn.execute('INSERT INTO discord_voice VALUES (?,?,?,?,?,1)',
            (mid,'mind',path.relative_to(r.data).as_posix(),3000,path.stat().st_size))
    return mid


async def open_dm(r):
    from playwright.async_api import expect
    await r.page.locator('#dc-dms [data-view="dm:101"]').click()
    await expect(r.page.locator('#dc-to')).to_contain_text('Arkon')


async def fake_microphone(page):
    # A browser-generated sine wave, never an OS microphone or an external file.
    await page.evaluate('''() => {
        window.micStops = 0;
        navigator.mediaDevices.getUserMedia = async () => {
            const context = new AudioContext(), oscillator = context.createOscillator();
            const destination = context.createMediaStreamDestination();
            oscillator.frequency.value = 220; oscillator.connect(destination); oscillator.start();
            await context.resume();
            for (const track of destination.stream.getTracks()) {
                const stop = track.stop.bind(track);
                track.stop = () => { window.micStops++; stop(); oscillator.stop(); context.close(); };
            }
            return destination.stream;
        };
    }''')


@pytest.mark.parametrize('theme',['dark','light'])
def test_voice_player_and_record_preview_send(rehearsal,tmp_path,theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390,theme=theme,voice_notes=True) as r:
            mid=seed_note(r); before=audio_inventory(r)
            async def transcript(message):
                return wire.Transcript(message.id,True,'The tea is ready for the rehearsal.','ok')
            r.app.voice_notes.request=transcript
            await open_console(r); await open_dm(r)
            bubble=r.page.locator(f'.dc-message[data-id="{mid}"] .dc-voice-note')
            await expect(bubble).to_be_visible()
            await expect(bubble.locator('details')).not_to_have_attribute('open','')
            await expect(bubble.locator('.body')).to_be_hidden()
            await bubble.get_by_role('button',name='Play voice note from Arkon').click()
            await expect(bubble.get_by_role('button',name='Pause voice note from Arkon')).to_be_visible()
            await bubble.get_by_role('button',name='Pause voice note from Arkon').click()
            await bubble.get_by_role('slider').fill('1.2')
            await expect(bubble.locator('.mono')).to_have_text('0:01 / 0:03')
            await bubble.get_by_text('Show transcript',exact=True).click()
            await expect(bubble.locator('.body')).to_have_text('I packed the tea for our next adventure.')
            # Polling preserves the media element, seek position, transcript fold and keyboard focus.
            await bubble.get_by_role('slider').focus()
            await bubble.locator('audio').evaluate('audio => {window.keptNote = audio;}')
            await r.page.evaluate("async () => (await import('/static/js/comms/discordstate.mjs')).refreshDiscord()")
            assert await bubble.locator('audio').evaluate('audio => audio === window.keptNote')
            await expect(bubble.get_by_role('slider')).to_be_focused()
            await expect(bubble.locator('.body')).to_be_visible()
            await r.page.get_by_role('button',name='Hide photos',exact=True).click()
            await expect(bubble).to_be_visible()
            await bubble.scroll_into_view_if_needed()
            await r.page.screenshot(path=str(tmp_path/f'voice-player-{theme}-390.png'))
            await fake_microphone(r.page)
            await r.page.locator('#dc-text').fill('Keep this typed draft.')
            record=r.page.locator('#dc-voice-record')
            await record.focus(); await record.press('Enter')
            await expect(record).to_have_text('Stop recording')
            await expect(r.page.locator('#dc-send')).to_be_disabled()
            await expect(r.page.locator('#dc-voice-timer')).to_be_visible()
            await r.page.wait_for_timeout(400)  # capture actual synthetic samples, at least the 200 ms minimum
            await record.click()
            await expect(r.page.locator('#dc-voice-preview')).to_be_visible()
            assert await r.page.evaluate('window.micStops')==1
            await expect(r.page.locator('#dc-send')).to_be_enabled()
            preview=r.page.locator('#dc-voice-preview-audio')
            await preview.evaluate('audio => audio.play()')
            assert not await preview.evaluate('audio => audio.paused')
            await preview.evaluate('audio => audio.pause()')
            await r.page.screenshot(path=str(tmp_path/f'voice-note-{theme}-390.png'))
            r.expected_http_errors.add(('/api/discord/voice',429))
            r.console._voice_uploads[:]=[time.monotonic()]*4
            await r.page.locator('#dc-send').click()
            await expect(r.page.locator('#dc-why')).to_contain_text('Wait a minute')
            await expect(r.page.locator('#dc-voice-preview')).to_be_visible()
            assert audio_inventory(r)==before
            r.console._voice_uploads.clear()
            await submit(r,r.page.locator('#dc-send'),'/api/discord')
            await expect(r.page.locator('#dc-voice-preview')).to_be_hidden()
            await expect(r.page.locator('#dc-text')).to_have_value('Keep this typed draft.')
            own=r.page.locator('.dc-message.mine .dc-voice-note').last
            await expect(own).to_be_visible()
            await expect(own.locator('.body')).to_have_text('The tea is ready for the rehearsal.')
            await expect(own.locator('details')).to_have_attribute('open','')
            rows=feed.read(r.data/'mind.db',lambda c:c.execute("SELECT count(*) FROM discord_voice WHERE origin='human' AND transcript_ok=1").fetchone()[0])
            assert rows==1
            uploaded=audio_inventory(r)
            own_row=r.page.locator('.dc-message.mine').filter(has=r.page.locator('.dc-voice-note')).last
            own_id=await own_row.get_attribute('data-id')
            await own_row.locator('[data-action="delete"]').click()
            await submit(r,r.page.locator('#dc-dialog-submit'),'/api/discord')
            await expect(r.page.locator(f'.dc-message[data-id="{own_id}"]')).to_contain_text('Message deleted')
            response=await r.context.request.get(r.url+'/api/discord/voice?id='+own_id,headers={'X-Console-Key':KEY})
            assert response.status==404
            retained(uploaded)
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await r.page.locator('#dc-channels [data-view="d:general"]').click()
            await expect(record).to_be_hidden()
            retained(before); assert not r.errors,r.errors
    asyncio.run(run())


def test_recording_cancels_and_permission_refusal_is_worded(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390,voice_notes=True) as r:
            before=audio_inventory(r); await open_console(r); await open_dm(r); await fake_microphone(r.page)
            record=r.page.locator('#dc-voice-record'); await record.click()
            await expect(record).to_have_text('Stop recording')
            await r.page.keyboard.press('Escape')
            await expect(record).to_have_text('Record voice note')
            await expect(r.page.locator('#dc-voice-preview')).to_be_hidden()
            assert await r.page.evaluate('window.micStops')==1
            await record.click(); await expect(record).to_have_text('Stop recording')
            await r.page.locator('#dc-channels [data-view="d:general"]').click()
            assert await r.page.evaluate('window.micStops')==2
            await open_dm(r)
            await r.page.evaluate("() => {navigator.mediaDevices.getUserMedia = async () => {throw new DOMException('Denied','NotAllowedError');};}")
            await record.click()
            await expect(r.page.locator('#dc-why')).to_contain_text('Microphone permission was refused')
            assert audio_inventory(r)==before
            retained(before); assert not r.errors,r.errors
    asyncio.run(run())


def test_voice_switch_off_has_no_record_control_and_refuses_upload(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            before=audio_inventory(r); await open_console(r); await open_dm(r)
            await expect(r.page.locator('#dc-voice-record')).to_be_hidden()
            response=await r.context.request.post(r.url+'/api/discord/voice',data=b'RIFF',headers={
                'Content-Type':'audio/wav','X-Console-Key':KEY,'Origin':r.url,'Sec-Fetch-Site':'same-origin'})
            assert response.status==403
            assert 'off' in (await response.json())['error'].lower()
            assert audio_inventory(r)==before; retained(before)
            assert not r.errors,r.errors
    asyncio.run(run())
