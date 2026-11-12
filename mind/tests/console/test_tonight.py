import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
import asyncio

from leeroy_mind.console import tonightview as tv
from leeroy_mind.discord.store import post


def test_every_section_survives_a_bare_database(tmp_path):
    conn = sqlite3.connect(':memory:')
    try:
        view = tv.tonight_view(conn,tmp_path,now=0,status=None,kit=None,kit_why=None)
        assert tuple(view['sections']) == tv.SECTIONS
        assert {v['status'] for v in view['sections'].values()} == {'not_set_up'}
    finally:
        conn.close()


def test_waiting_dms_count_only_unanswered_threads(store,tmp_path,clock):
    store.set_meta('human_guid','1')
    now = clock.now()
    for channel,author,ts in [('dm:101',101,1),('dm:101',1,2),('dm:102',102,1),
                              ('dm:103',1,1),('dm:103',103,2),('dm:104',104,1)]:
        mid = post(store,channel=channel,author=author,origin='human' if author==1 else 'mind',text='PRIVATE DM SENTINEL',now=now-10+ts)
    store.conn.execute('UPDATE discord_message SET deleted=? WHERE id=?',(now,mid))
    result = tv.tonight_view(store.conn,tmp_path,now=now,status=None,kit=None,kit_why='mind_down')
    waiting = result['sections']['waiting']
    assert waiting['status'] == 'ok' and waiting['dms'] == 2
    assert 'PRIVATE DM SENTINEL' not in json.dumps(result)
    assert 'text' not in json.dumps(result)


def test_bank_shortage_is_under_half_of_stock_target(store,tmp_path,clock):
    from leeroy_mind.realm.bank import STOCK, BankLedger
    ledger = BankLedger(store,clock)
    for values in STOCK.values():
        for item,target in values:
            ledger.record('in','stock',101,item,target)
    ledger.record('out','consumables',101,46376,11)
    ledger.record('out','consumables',101,46377,10)
    result = tv.tonight_view(store.conn,tmp_path,now=clock.now(),status=None,kit=None,kit_why='mind_down')
    assert [(x['id'],x['have'],x['target']) for x in result['sections']['bank']['items']] == [(46376,9,20)]


def test_a_failing_section_is_unavailable_alone(store,tmp_path,clock,monkeypatch):
    def fail(*args,**kw):
        raise RuntimeError('PRIVATE FAILURE')
    monkeypatch.setattr(tv.raidview,'dkp_stats',fail)
    view = tv.tonight_view(store.conn,tmp_path,now=clock.now(),status=None,kit=None,kit_why='mind_down')
    assert view['sections']['loot'] == {'status':'unavailable','why':'read_failed'}
    assert view['sections']['bank']['status'] == 'ok'
    assert 'PRIVATE FAILURE' not in json.dumps(view)


def test_raid_tonight_lists_signups_from_the_calendar(store,tmp_path,clock):
    from conftest import seed_test_cast
    seed_test_cast(store)
    now = clock.now()
    store.conn.execute("INSERT INTO realm_calendar(kind,title,at,creator,state,event_id,updated) VALUES ('raid','Naxxramas',?,101,'posted',7,?)",(now+60,now))
    store.conn.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (7,101,'signed_up',?),(7,102,'declined',?)",(now,now))
    view = tv.tonight_view(store.conn,tmp_path,now=now,status=None,kit=None,kit_why='mind_down')
    raid = view['sections']['raid']['events'][0]
    assert raid['title'] == 'Naxxramas'
    assert raid['signed_up'][0]['guid'] == 101 and raid['declined'][0]['guid'] == 102
    assert raid['signed_up'][0]['name'] != '#101'


def test_api_tonight_needs_the_key_and_answers_json(store):
    from console.rig import http, make_app
    from console.test_console_bank import served
    data = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    async def go():
        async with served(make_app(data)) as app:
            assert (await http(app,'GET','/api/tonight',key=False)).status==403
            answer = await http(app,'GET','/api/tonight')
            assert answer.status==200 and set(answer.json()['sections'])==set(tv.SECTIONS)
    asyncio.run(go())


def test_kit_without_the_mind_says_so(store):
    from console.rig import http, make_app
    from console.test_console_bank import served, FakeLink
    data = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    link = FakeLink()
    async def go():
        async with served(make_app(data,link=link)) as app:
            view = (await http(app,'GET','/api/tonight')).json()
            assert view['sections']['kit']['why']=='mind_down'
            assert view['sections']['bank']['status']=='ok'
    asyncio.run(go())
    assert not link.calls


def test_slow_section_is_bounded_and_single_flight(tmp_path):
    import threading
    release = threading.Event()
    calls = []
    service = tv.TonightView(tmp_path)
    service.budgets = {n:.03 for n in tv.SECTIONS}
    def source(name,deadline,runtime):
        calls.append(name)
        if name=='raid':
            release.wait(2)
        return tv.result()
    service._source = source
    async def go():
        start = time.monotonic()
        a = await service.snapshot({'now':1})
        b = await service.snapshot({'now':2})
        assert time.monotonic()-start<.5
        for view in (a,b):
            assert view['sections']['raid']['why']=='slow'
            assert view['sections']['bank']['status']=='ok'
        assert calls.count('raid')==1
    try:
        asyncio.run(go())
    finally:
        release.set()


def test_waiting_never_carries_message_text(store,tmp_path,clock):
    store.set_meta('human_guid','1')
    now = clock.now()
    for number in range(1,4):
        store.conn.execute("INSERT INTO realm_mail(id,ts,sender,kind,subject,body,state) VALUES (?, ?,101,'letter','PRIVATE SUBJECT','PRIVATE LETTER','sent')",(number,now))
    store.conn.execute("INSERT INTO board_reply(ts,kind,parent,to_guid,text,state) VALUES (?,'letter',1,101,'PRIVATE REPLY','waiting')",(now,))
    store.conn.execute("INSERT INTO board_reply(ts,kind,parent,to_guid,text,state,settled) VALUES (?,'letter',2,101,'PRIVATE REPLY','withdrawn',?)",(now,now))
    store.conn.execute('CREATE TABLE guild_poll(status TEXT, question TEXT)')
    store.conn.execute("INSERT INTO guild_poll VALUES ('open','PRIVATE POLL'),('closed','PRIVATE POLL')")
    view = tv.tonight_view(store.conn,tmp_path,now=now,status=None,kit=None,kit_why=None)
    assert view['sections']['waiting']['letters']==2 and view['sections']['waiting']['polls']==1
    assert 'PRIVATE' not in json.dumps(view)


def test_kit_reply_projects_only_consumable_fields():
    from leeroy_mind.guild.kitread import kit_view
    source = kit_view({46376:2})
    source['text'] = source['groups'][0]['text'] = source['items'][0]['text'] = 'PRIVATE CONTENT'
    assert 'PRIVATE' not in json.dumps(tv.clean_kit(source))
    source['groups'][0]['have'] = True
    assert tv.clean_kit(source) is None


def test_cached_guild_presence_becomes_unknown_when_live_status_disappears(tmp_path):
    service = tv.TonightView(tmp_path)
    service._source = lambda *args:tv.result(human_online=True,cast_online=5)
    async def go():
        live = {'world':{'connected':True,'human_online':True,'cast_online':5}}
        first = await service.snapshot({'now':1,'status':live})
        assert first['sections']['guild']['human_online'] is True
        stopped = await service.snapshot({'now':2,'status':None})
        assert stopped['sections']['guild']['human_online'] is None
        assert stopped['sections']['guild']['cast_online'] is None
    asyncio.run(go())
