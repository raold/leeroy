import random

import pytest

from banter.test_banter import rig
from banter.test_culture_wear import persist
from leeroy_mind.banter.service import choose_cast
from leeroy_mind.banter.topics import Topic, jokes
from leeroy_mind.culture.config import load_culture_config
from leeroy_mind.culture.seeds import culture_seeds
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import Relationship


def enable(rig):
    path = rig.engine.data_dir / 'mind.toml'
    path.write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    rig.banter.app.culture_config = load_culture_config(path)


def joke(rig):
    now = rig.clock.now()
    line = rig.store.insert_chat_line(Chat(0,'guild',101,'Member101',0,'the kettle escaped',0,0),
        session_id=None, origin='game', ts=now-5*86400, audience=(1,101,102,103))
    rig.store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used) "
        "VALUES(1,'joke','kettle escape',?,101,?,?)", (f'chat:{line}',now-5*86400,now-4*86400))
    return next(jokes(rig.banter.app,'guild',now))


@pytest.mark.parametrize('kind', ['birthday', 'loot'])
def test_non_teasing_target_keeps_cast_and_daily_allowance(rig, monkeypatch, kind):
    enable(rig)
    app, now = rig.banter.app, rig.clock.now()
    topic = Topic('test',kind,now,'Recorded celebration',102)
    for g in (101,103):
        rig.store.put_relationship(Relationship(g,102,affinity=0))
    assert set(choose_cast(app,[101,103],topic,random.Random(1))) == {101,103}
    monkeypatch.setattr(rig.banter,'valid',lambda *args: True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args: [101,103])
    calls=[]
    async def speak(turn,meta):
        assert meta.still_valid()
        calls.append(turn)
        persist(rig,turn,meta)
        return [1]
    monkeypatch.setattr(rig.engine,'_speak',speak)
    assert rig.banter.reserve(topic,now)
    rig.sync(rig.banter.thread('discord:general',topic,[101,103]))
    assert len(calls)>=3
    assert rig.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'culture:teased:%'").fetchone() is None
    assert rig.store.get_meta('banter:owner-evening') is None


@pytest.mark.parametrize('kind', ['quiet', 'wipe', 'joke'])
def test_untargeted_riff_never_defends_previous_speaker(rig, monkeypatch, kind):
    enable(rig)
    topic=Topic('test',kind,rig.clock.now(),'Recorded shared incident')
    monkeypatch.setattr(rig.banter,'valid',lambda *args: True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args: [101,102])
    calls=[]
    async def speak(turn,meta):
        calls.append(turn)
        persist(rig,turn,meta)
        return [1]
    monkeypatch.setattr(rig.engine,'_speak',speak)
    rig.sync(rig.banter.thread('discord:general',topic,[101,102]))
    assert len(calls)>=3
    assert all('Add a fresh playful angle' in t.instruction for t in calls)


def test_banter_rechecks_server_use_before_first_send_and_allows_own_replies(rig,monkeypatch):
    enable(rig)
    topic=joke(rig)
    now=rig.clock.now()
    monkeypatch.setattr(rig.banter,'allowed',lambda *a: True)
    assert rig.banter.reserve(topic,now)
    assert rig.banter.valid('guild',0,topic,now)
    rig.store.conn.execute('UPDATE lexicon SET last_used=? WHERE id=1',(now,))
    assert not rig.banter.valid('guild',0,topic,now)
    assert rig.banter.valid('guild',0,topic,now,True)


def test_banter_reservation_hides_server_seed_before_delivery(rig):
    enable(rig)
    topic=joke(rig)
    def collect():
        return culture_seeds(rig.store, now=rig.clock.now(), local=rig.clock.local(),
            personas={g:rig.engine.personas.get(g) for g in (101,102,103)},human=1)
    assert len(collect())==1
    assert rig.banter.reserve(topic,rig.clock.now())
    assert collect()==[]
    rig.banter.settle_reservation(topic,0)
    assert rig.store.conn.execute('SELECT uses_total FROM lexicon').fetchone()[0]==0


@pytest.mark.parametrize('fail', [False,True])
def test_banter_discord_wear_is_part_of_delivery_transaction(rig,monkeypatch,fail):
    enable(rig)
    topic=joke(rig)
    monkeypatch.setattr(rig.banter,'valid',lambda *args: True)
    monkeypatch.setattr(rig.banter,'candidates',lambda *args: [101,102])
    writer=rig.banter._record_delivery
    def record(*args):
        writer(*args)
        if fail:
            raise RuntimeError('synthetic receipt fault')
    monkeypatch.setattr(rig.banter,'_record_delivery',record)
    calls=[]
    async def speak(turn,meta):
        calls.append(meta)
        result=rig.engine._deliver_discord(turn,meta,rig.engine.personas.get(turn.speaker),'the kettle escaped',[])
        return [] if result else [1]
    monkeypatch.setattr(rig.engine,'_speak',speak)
    assert rig.banter.reserve(topic,rig.clock.now())
    floor=rig.store.conn.execute('SELECT max(id) FROM chat_line').fetchone()[0]
    rig.sync(rig.banter.thread('discord:general',topic,[101,102]))
    assert rig.store.conn.execute('SELECT uses_total FROM lexicon').fetchone()[0]==int(not fail)
    posts=rig.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]
    assert (posts>0) is not fail
    assert rig.store.conn.execute('SELECT count(*) FROM chat_line WHERE id>?',(floor,)).fetchone()[0]==posts
    assert calls[0].said==int(not fail)
    assert rig.banter._reservation is None
