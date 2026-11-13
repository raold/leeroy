import pytest
from banter.test_banter import rig
from leeroy_mind.banter.topics import Topic


def persist(rig,turn,meta):
    from leeroy_mind.protocol import Chat
    from leeroy_mind.discord import store as dstore
    now=rig.clock.now()
    line=rig.store.insert_chat_line(Chat(0,turn.channel,turn.speaker,'Member',0,'the kettle escaped',0,0),session_id=None,origin='mind',ts=now,audience=(101,102))
    meta.said=1
    if turn.channel.startswith('discord:'):
        message=dstore.post(rig.store,channel='general',author=turn.speaker,origin='mind',text='the kettle escaped',turn=meta.turn_id,now=now)
        rig.store.set_meta(f'discord:line:{message}',str(line))



def test_culture_banter_counts_delivery_and_leaves_dropped_allowance(rig):
    path=rig.engine.data_dir / "mind.toml"
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\n")
    now=rig.clock.now()
    rig.store.conn.execute("INSERT INTO lexicon(id,kind,term,target,origin_ref,originator,born,last_used) "
        "VALUES(1,'joke','floor inspector',0,'chat:1',101,?,?)",(now-10*86400,now-10*86400))
    topic=Topic('joke:1','joke',now,'recorded')
    assert rig.banter.reserve(topic,now)
    rig.banter.settle_reservation(topic,0)
    assert rig.store.conn.execute("SELECT uses_total FROM lexicon").fetchone()[0]==0
    rig.sync(rig.advance(86401,86401))
    now=rig.clock.now()
    assert rig.banter.reserve(topic,now)
    rig.banter.settle_reservation(topic,1,17)
    row=rig.store.conn.execute("SELECT uses_total,last_used FROM lexicon").fetchone()
    assert tuple(row)==(1,now)
    assert rig.store.get_meta('culture:wear-counted:joke:1:17')=='1'
    assert rig.store.get_meta('banter:joke-use:joke:1') is None

@pytest.mark.parametrize('channel',['guild','discord:general'])
def test_banter_culture_cast_and_delivery_receipt(rig,monkeypatch,channel):
    from leeroy_mind.banter.service import choose_cast, role
    from leeroy_mind.banter.topics import Topic
    from leeroy_mind.culture.tease import TEASED_KEY, local_at
    from leeroy_mind.storage.rows import Relationship
    import random
    app=rig.banter.app
    (rig.engine.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=rig.clock.now()
    topic=Topic('test','death',now,'Recorded event',1,True)
    for guid in (101,102):
        rig.store.put_relationship(Relationship(guid,1,affinity=30))
    assert set(choose_cast(app,[101,102,103],topic,random.Random(1)))=={101,102}
    assert role(app,103,1)=='defend'
    monkeypatch.setattr(rig.banter,'valid',lambda *args:True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args:[101,102])
    calls=[]
    async def speak(turn,meta):
        assert meta.still_valid()
        calls.append(turn)
        persist(rig,turn,meta)
        return [1]
    monkeypatch.setattr(rig.engine,'_speak',speak)
    key=TEASED_KEY.format(date=local_at(now).date().isoformat(),guid=1)
    assert rig.banter.reserve(topic,now)
    assert rig.store.get_meta('banter:owner-evening') is None
    rig.sync(rig.banter.thread(channel,topic,[101,102]))
    assert len(calls)>=3  # its own receipt does not stop this bounded thread
    assert rig.store.get_meta(key) is not None
    assert rig.store.get_meta('banter:owner-evening') is not None
    assert choose_cast(app,[101,102],topic,random.Random(1))==[]


@pytest.mark.parametrize('sent',[False,True])
def test_banter_receipt_requires_delivery_and_rechecks_mood(rig,monkeypatch,sent):
    from dataclasses import replace
    from leeroy_mind.banter.topics import Topic
    from leeroy_mind.culture.tease import TEASED_KEY, local_at
    from leeroy_mind.storage.rows import Relationship
    app=rig.banter.app
    (rig.engine.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=rig.clock.now()
    topic=Topic('test','death',now,'Recorded event',102)
    for guid in (101,103):
        rig.store.put_relationship(Relationship(guid,102,affinity=30))
    monkeypatch.setattr(rig.banter,'valid',lambda *args:True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args:[101,103])
    calls=[]
    async def speak(turn,meta):
        calls.append(turn)
        rig.store.put_state(replace(rig.store.get_state(102),mood=-.8))
        if sent:
            persist(rig,turn,meta)
        return [1] if sent else []
    monkeypatch.setattr(rig.engine,'_speak',speak)
    rig.sync(rig.banter.thread('guild',topic,[101,103]))
    assert len(calls)==1
    key=TEASED_KEY.format(date=local_at(now).date().isoformat(),guid=102)
    assert (rig.store.get_meta(key) is not None)==sent


@pytest.mark.parametrize('config',['','[culture]\nenabled=true\n','[server]\nenabled=true\n[culture]\nenabled=false\n'])
def test_tease_gate_off_keeps_legacy_roles_and_reservations(rig,config):
    from leeroy_mind.banter.service import role
    from leeroy_mind.storage.rows import Relationship
    (rig.engine.data_dir/'mind.toml').write_text(config)
    rig.store.put_relationship(Relationship(101,102,affinity=-20))
    assert role(rig.banter.app,101,102)=='needle'
    topic=Topic('test','death',rig.clock.now(),'Recorded event',1,True)
    assert rig.banter.reserve(topic,rig.clock.now())
    assert rig.store.get_meta('banter:owner-evening') is not None
    assert rig.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'culture:teased:%'").fetchone() is None

@pytest.mark.parametrize('channel',['guild','discord:general'])
@pytest.mark.parametrize('posted',[False,True])
@pytest.mark.parametrize('error',[__import__('asyncio').CancelledError,RuntimeError])
def test_interrupted_delivery_settles_only_persisted_post(rig,monkeypatch,channel,posted,error):
    from leeroy_mind.protocol import Chat
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.culture.tease import TEASED_KEY, local_at
    from leeroy_mind.clock import night_of
    from leeroy_mind.storage.rows import Relationship
    app=rig.banter.app
    (rig.engine.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=rig.clock.now()
    posted_at=now-86400
    rig.store.conn.execute("INSERT INTO lexicon(id,kind,term,target,origin_ref,originator,born,last_used) VALUES(1,'joke','test kettle',1,'chat:1',101,?,?)",(now-10*86400,now-10*86400))
    topic=Topic('joke:1','joke',now,'Recorded event',1,True)
    for g in (101,102):
        rig.store.put_relationship(Relationship(g,1,affinity=30))
    monkeypatch.setattr(rig.banter,'valid',lambda *args:True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args:[101,102])
    lines=[]
    async def speak(turn,meta):
        if posted:
            line=rig.store.insert_chat_line(Chat(0,channel,turn.speaker,'Member',0,'the kettle escaped',0,0),session_id=None,origin='mind',ts=posted_at,audience=(101,102))
            lines.append(line)
            meta.said = 1
            if channel.startswith('discord:'):
                message=dstore.post(rig.store,channel='general',author=turn.speaker,origin='mind',text='the kettle escaped',turn=meta.turn_id,now=posted_at)
                rig.store.set_meta(f'discord:line:{message}',str(line))
        raise error()
    monkeypatch.setattr(rig.engine,'_speak',speak)
    assert rig.banter.reserve(topic,now)
    with pytest.raises(error):
        rig.sync(rig.banter.thread(channel,topic,[101,102]))
    key=TEASED_KEY.format(date=local_at(posted_at).date().isoformat(),guid=1)
    assert (rig.store.get_meta(key) is not None)==posted
    assert rig.store.get_meta('banter:owner-evening')==(night_of(local_at(posted_at)) if posted else None)
    row=rig.store.conn.execute('SELECT uses_total,last_used FROM lexicon WHERE id=1').fetchone()
    assert row['uses_total']==int(posted)
    if posted:
        assert row['last_used']==posted_at
        assert rig.store.get_meta(f'culture:wear-counted:joke:1:{lines[0]}')=='1'

@pytest.mark.parametrize('channel',['guild','discord:general'])
def test_cancelled_turn_never_claims_another_turns_post(rig,monkeypatch,channel):
    import asyncio
    from leeroy_mind.protocol import Chat
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.storage.rows import Relationship
    (rig.engine.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=rig.clock.now()
    topic=Topic('test','death',now,'Recorded event',1,True)
    for g in (101,102):
        rig.store.put_relationship(Relationship(g,1,affinity=30))
    monkeypatch.setattr(rig.banter,'valid',lambda *args:True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args:[101,102])
    async def speak(turn,meta):
        rig.store.insert_chat_line(Chat(0,channel,turn.speaker,'Member',0,'another turn',0,0),session_id=None,origin='mind',ts=now,audience=(101,102))
        if channel.startswith('discord:'):
            dstore.post(rig.store,channel='general',author=turn.speaker,origin='mind',text='another turn',turn='unrelated',now=now)
        raise asyncio.CancelledError()
    monkeypatch.setattr(rig.engine,'_speak',speak)
    assert rig.banter.reserve(topic,now)
    with pytest.raises(asyncio.CancelledError):
        rig.sync(rig.banter.thread(channel,topic,[101,102]))
    assert rig.store.get_meta('banter:owner-evening') is None
    assert rig.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'culture:teased:%'").fetchone() is None
