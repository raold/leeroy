"""Lab and thumbs cross the real browser, HTTP endpoint and synthetic mind stores."""
import asyncio
import json

import pytest

from console.rig import KEY
from leeroy_mind.console import feed
from leeroy_mind.discord import store as discord
from leeroy_mind.learn import arms, control, weights
from leeroy_mind.protocol import Chat
from leeroy_mind.voice.store import VoiceStore
from .test_tabs import open_console, visit
from .test_flows import submit


def seed_lab(r, *, history=False):
    store=r.app.store; now=r.app.clock.now(); sid=store.start_session(now-100000)
    def line(channel,text,ts=None):
        return store.insert_chat_line(Chat(0,channel,101,'Arkon',1 if channel in ('whisper','dm:101') else 0,
            text,0,10),session_id=sid if not channel.startswith(('discord:','dm:')) else None,
            origin='mind',ts=now-5 if ts is None else ts)
    old=line('guild','The oldest synthetic member line.',now-90000)
    if history:
        for i in range(205): line('guild',f'Synthetic archive line {i}.',now-1000+i)
    guild=line('guild','Synthetic learning guild line.')
    whisper=line('whisper','Synthetic learning whisper.')
    voice=line('voice:general','Synthetic learning voice.')
    VoiceStore(store).add_line('lab.voice',voice,'general','done',now-5)
    dms={}
    for channel in ('general','dm:101'):
        text='Synthetic Discord '+channel+'.'
        lid=line(channel if channel.startswith('dm:') else 'discord:'+channel,text)
        mid=discord.post(store,channel=channel,author=101,origin='mind',text=text,now=now-5)
        # Deliberately no receipt: historical messages must still resolve uniquely.
        dms[channel]=(lid,mid)
    with store.transaction():
        version=weights.ensure(store.conn,now=now-10).version
        store.conn.execute('INSERT INTO talk_reward VALUES (?,?,?,1,0,?,?,?)',(guild,101,.7,'{}',version,now))
        arms.ensure(store.conn,now=now)
        arm=arms.propose(store.conn,'length',{'min_words':5,'max_words':15},now=now)
        lesson=store.conn.execute("INSERT INTO talk_lesson(guid,scope,text,status,expires,origin_night) "
            "VALUES (101,'guild','She keeps replies brief.','proposed',?,'synthetic')",(now+86400,)).lastrowid
    return dict(old=old,guild=guild,whisper=whisper,voice=voice,discord=dms,arm=arm,lesson=lesson)


async def sync_feed(r):
    # Signals are committed by a separate writer; match the console's fresh read.
    rows=feed.read(r.data/'mind.db',lambda conn:feed.feed(conn,now=r.app.clock.now()))
    await r.page.evaluate("async lines => (await import('/static/js/core.mjs')).emit('chat',{lines})",rows)


def test_thumbs_game_whisper_voice_discord_and_paged_history(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(learning=True,width=390) as r:
            ids=seed_lab(r,history=True); await open_console(r)
            for view,lid in [('guild',ids['guild']),('w:101',ids['whisper']),('voice',ids['voice'])]:
                await r.page.evaluate("async view => (await import('/static/js/comms/feedview.mjs')).openView(view)",view)
                box=r.page.locator(f'[data-feedback="{lid}"]')
                await expect(box).to_be_visible()
                up=box.get_by_role('button',name='Thumbs up',exact=False)
                if view=='guild':
                    await box.get_by_text('Reasons (optional)',exact=True).click()
                    await box.get_by_label('funny',exact=True).check()
                    await sync_feed(r)
                    await expect(box.get_by_label('funny',exact=True)).to_be_checked()
                await up.focus(); await up.press('Enter')
                await expect(up).to_have_attribute('aria-pressed','true')
                stored=feed.read(r.data/'mind.db',lambda conn:conn.execute(
                    "SELECT value FROM talk_signal WHERE line_id=? AND kind='explicit'",(lid,)).fetchone())
                assert stored[0]==1
                if view=='guild':
                    assert feed.read(r.data/'mind.db',lambda conn:conn.execute(
                        "SELECT 1 FROM talk_signal WHERE line_id=? AND kind='reason_funny'",(lid,)).fetchone())
            for channel,(lid,mid) in ids['discord'].items():
                view=channel if channel.startswith('dm:') else 'd:'+channel
                await r.page.locator(f'[data-view="{view}"]').click()
                box=r.page.locator(f'[data-feedback="{lid}"]'); await expect(box).to_be_visible()
                await submit(r,box.get_by_role('button',name='Thumbs down',exact=False),'/api/feedback')
                await expect(box.get_by_role('button',name='Thumbs down',exact=False)).to_have_attribute('aria-pressed','true')
            await r.page.evaluate("async () => (await import('/static/js/comms/feedview.mjs')).openView('guild')")
            await r.page.get_by_role('button',name='Load earlier',exact=True).click()
            old=r.page.locator(f'[data-feedback="{ids["old"]}"]')
            await expect(old).to_be_visible()
            await submit(r,old.get_by_role('button',name='Thumbs down',exact=False),'/api/feedback')
            await old.get_by_text('Reasons (optional)',exact=True).click()
            await old.get_by_label('wrong facts',exact=True).check()
            await expect(old.locator('.feedback-status')).to_have_text('Feedback saved.')
            # A second console changes a saved vote; the next stored page must win.
            await r.app.signals.feedback({'line_id':ids['guild'],'value':-1,'reasons':['weird']})
            await sync_feed(r)
            guild=r.page.locator(f'[data-feedback="{ids["guild"]}"]')
            await expect(guild.get_by_role('button',name='Thumbs down',exact=False)).to_have_attribute('aria-pressed','true')
            await r.page.goto(r.url+'/#key='+KEY)
            await expect(r.page.locator(f'[data-feedback="{ids["guild"]}"] [data-vote="-1"]')).to_have_attribute('aria-pressed','true')
            assert not r.errors,r.errors
    asyncio.run(run())


@pytest.mark.parametrize('theme',['dark','light'])
def test_lab_review_controls_stable_poll_and_phone_layout(rehearsal,tmp_path,theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(learning=True,width=390,theme=theme) as r:
            ids=seed_lab(r); await open_console(r); await visit(r.page,'lab')
            await expect(r.page.locator('#lab-weights-note')).to_contain_text('Current version 1')
            await r.page.locator('[data-detail="reward-member-101"] > summary').click()
            await r.page.get_by_role('button',name=f'Read line {ids["guild"]}',exact=False).first.click()
            await expect(r.page.locator('#lab-rewards blockquote').first).to_contain_text('Synthetic learning guild line.')
            await r.page.locator('[data-detail="family-length"] > summary').click()
            arm=r.page.locator('[data-detail="arm-'+ids['arm']+'"]'); await arm.locator(':scope > summary').click()
            accept=arm.get_by_role('button',name='Accept',exact=True); await accept.focus()
            # Trigger the tab's ordinary next poll by hide/show; stable IDs preserve focus and reading state.
            await r.page.evaluate("async () => {const {S}=await import('/static/js/state.mjs'); const {showTab}=await import('/static/js/tabs.mjs'); showTab(S.tab);}")
            await expect(accept).to_be_focused()
            await expect(r.page.locator('#lab-rewards blockquote').first).to_contain_text('Synthetic learning guild line.')
            await accept.click(); await submit(r,r.page.locator('#lab-confirm-save'),'/api/learn')
            assert r.app.store.conn.execute('SELECT status FROM learn_arm WHERE id=?',(ids['arm'],)).fetchone()[0]=='active'
            await r.page.locator('#lab-weight-explicit').fill('0.5'); await r.page.locator('#lab-weight-engagement').fill('0.15')
            await r.page.locator('#lab-weights-save').click()
            await expect(r.page.locator('#lab-confirm')).to_be_visible()
            assert weights.read(r.app.store.conn,at=r.app.clock.now()).version==1
            await r.page.locator('#lab-confirm-cancel').click()
            assert weights.read(r.app.store.conn,at=r.app.clock.now()).version==1
            await r.page.locator('#lab-weights-save').click(); await submit(r,r.page.locator('#lab-confirm-save'),'/api/learn')
            assert weights.read(r.app.store.conn,at=r.app.clock.now()).version==2
            await r.page.locator('#lab-freeze').click(); await submit(r,r.page.locator('#lab-confirm-save'),'/api/learn')
            assert control.frozen(r.app.store.conn)
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            await r.page.screenshot(path=str(tmp_path/f'lab-{theme}-390.png'),full_page=True)
            for section in ('arms','lessons','weights'):
                if section=='lessons':
                    await r.page.locator('#lab-lessons details > summary').first.click()
                target=r.page.locator('#lab-'+section if section!='weights' else '#lab-weights-form')
                await target.scroll_into_view_if_needed()
                await r.page.screenshot(path=str(tmp_path/f'lab-{theme}-{section}-390.png'))
            # One real server refusal even if a stale page tries to save while control is off.
            r.console.controls.enabled=False; r.expected_http_errors.add(('/api/learn',403))
            response=await r.context.request.post(r.url+'/api/learn',data={'action':'freeze','frozen':False},headers={'X-Console-Key':KEY,'Origin':r.url})
            assert response.status==403 and control.frozen(r.app.store.conn)
            assert not r.errors,r.errors
    asyncio.run(run())


@pytest.mark.parametrize('initial',[False,True],ids=['freeze','unfreeze'])
def test_freeze_confirmation_keeps_its_target_when_another_console_changes_state(rehearsal,initial):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(learning=True,width=390) as r:
            seed_lab(r)
            control.change(r.app.store,{'action':'freeze','frozen':initial},now=r.app.clock.now())
            await open_console(r); await visit(r.page,'lab')
            await expect(r.page.locator('#lab-freeze')).to_have_attribute('aria-pressed',str(initial).lower())
            await r.page.locator('#lab-freeze').click()
            target=not initial
            await expect(r.page.locator('#lab-confirm-words')).to_contain_text('Freeze learning?' if target else 'Unfreeze learning?')
            # A different console saves the desired state while this confirmation is open.
            control.change(r.app.store,{'action':'freeze','frozen':target},now=r.app.clock.now())
            async with r.page.expect_response(lambda response:response.url.endswith('/api/lab') and response.status==200):
                await r.page.evaluate("async () => {const {showTab}=await import('/static/js/tabs.mjs'); showTab('lab');}")
            await expect(r.page.locator('#lab-freeze')).to_have_attribute('aria-pressed',str(target).lower())
            async with r.page.expect_request(lambda req:req.url.endswith('/api/learn') and req.method=='POST') as requested:
                await submit(r,r.page.locator('#lab-confirm-save'),'/api/learn')
            assert (await requested.value).post_data_json=={'action':'freeze','frozen':target}
            assert control.frozen(r.app.store.conn) is target
            assert not r.errors,r.errors
    asyncio.run(run())


def test_newer_feedback_event_survives_a_delayed_older_http_reply(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(learning=True,width=390) as r:
            ids=seed_lab(r); await open_console(r)
            lid=ids['guild']; box=r.page.locator(f'[data-feedback="{lid}"]')
            await expect(box).to_be_visible()
            saved,release=asyncio.Event(),asyncio.Event()
            async def delay_reply(route):
                response=await route.fetch()
                saved.set()
                await asyncio.wait_for(release.wait(),15)
                await route.fulfill(response=response)
            await r.page.route('**/api/feedback',delay_reply)
            try:
                await box.get_by_role('button',name='Thumbs up',exact=False).click()
                await asyncio.wait_for(saved.wait(),10)
                # Context requests bypass this page's route; the real console sends its SSE receipt.
                response=await r.context.request.post(r.url+'/api/feedback',data={
                    'line_id':lid,'value':-1,'reasons':['weird']},headers={'X-Console-Key':KEY,'Origin':r.url})
                assert response.status==200
                newer=await response.json()
                assert newer['revision']==2
                await expect(box.get_by_role('button',name='Thumbs down',exact=False)).to_have_attribute('aria-pressed','true')
                release.set()
                await expect(box.locator('.feedback-status')).to_have_text('Newer feedback was saved in another console.')
                await expect(box.get_by_role('button',name='Thumbs down',exact=False)).to_have_attribute('aria-pressed','true')
                stored=feed.read(r.data/'mind.db',lambda conn:conn.execute(
                    "SELECT value FROM talk_signal WHERE line_id=? AND kind='explicit'",(lid,)).fetchone())
                assert stored[0]==-1
                assert not r.errors,r.errors
            finally:
                release.set()
                await r.page.unroute_all(behavior='wait')
    asyncio.run(run())


def test_weekly_report_and_rollback_through_the_page(rehearsal):
    async def run():
        from playwright.async_api import expect
        from leeroy_mind.learn import cycle
        async with rehearsal(learning=True,width=390) as r:
            store=r.app.store; now=r.app.clock.now()
            with store.transaction():
                arms.ensure(store.conn,now=now); weights.ensure(store.conn,now=now)
                version=cycle.save_version(store.conn,cycle.state(store.conn,now=now),reason='cycle',now=now,week='2026-W44')
                store.conn.execute("UPDATE learn_arm SET status='retired' WHERE id='length.1.v1'")
            store.set_meta(control.REPORT_KEY,json.dumps({'version':1,'week':'2026-W44','previous_week':'2026-W43',
                'state_version':version,'rates':{'week':{'lines':12,'repetition':.25},'previous':{'lines':10,'repetition':.1}},
                'measured':{'contradiction':0},'diversity':{'ok':0,'repetition_rose':1,'stock_rose':0},
                'promoted':[],'retired':[],'rolled_back':[{'kind':'arm','id':'tease.1.v1','context':['guild','none','natural'],
                'reasons':['repetition']}],'held':{'count':0,'items':[]}}))
            await open_console(r); await visit(r.page,'lab')
            report=r.page.locator('#lab-report')
            await expect(report).to_contain_text('Week 2026-W44, compared with 2026-W43')
            await expect(report).to_contain_text('Repetition 25.0% (week before 10.0%)')
            await report.locator('[data-detail="report-rolled_back"] > summary').click()
            await expect(report).to_contain_text('arm tease.1.v1 in guild / none / natural')
            await report.locator('[data-detail="report-versions"] > summary').click()
            await report.get_by_role('button',name=f'Roll back to version {version}').click()
            await expect(r.page.locator('#lab-confirm-words')).to_contain_text('vetoes and freezes stay')
            await submit(r,r.page.locator('#lab-confirm-save'),'/api/learn')
            await expect(r.page.locator('#lab-result')).to_contain_text(f'Learning restored to version {version}')
            assert store.conn.execute("SELECT status FROM learn_arm WHERE id='length.1.v1'").fetchone()[0]=='active'
            assert not r.errors,r.errors
    asyncio.run(run())
