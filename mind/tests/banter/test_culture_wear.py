import pytest
from banter.test_banter import rig
from leeroy_mind.banter.topics import Topic


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
