from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import make_persona
from leeroy_mind.clock import night_of
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.tuning.effective import effective, MemberTuning
from leeroy_mind.culture.tease import tease_ok, mark_teased, TEASED_KEY


@pytest.fixture
def gate(store, monkeypatch):
    from leeroy_mind.culture import tease
    monkeypatch.setattr(tease, 'local_at', lambda ts: datetime.fromtimestamp(ts, timezone.utc))
    now = datetime(2026, 11, 12, 21, tzinfo=timezone.utc).timestamp()
    people = {g: make_persona(g, f'Member{g}') for g in (101,102,103)}
    store.put_relationship(Relationship(101,102,affinity=30))
    store.put_relationship(Relationship(101,1,affinity=30))
    def check(target=102):
        return tease_ok(store.conn,101,target,personas=people,human=1,now=now)
    return SimpleNamespace(store=store, now=now, people=people, check=check)


def test_only_people_who_like_her_tease(gate):
    assert gate.check() == (True, 'ok')
    gate.store.put_relationship(Relationship(101,102,affinity=29.9))
    assert gate.check() == (False, 'dislikes')
    assert tease_ok(gate.store.conn,103,102,personas=gate.people,human=1,now=gate.now)==(False,'dislikes')


def test_cruel_persona_never_teases(gate):
    p=gate.people[101]
    p=replace(p,big_five=replace(p.big_five,agreeableness=replace(p.big_five.agreeableness,score=3)))
    gate.people[101]=effective(p, MemberTuning({'drama':7}))
    assert gate.check()==(False,'cruel')


def test_hurt_target_is_never_teased(gate):
    s=gate.store
    s.put_state(replace(s.get_state(102),mood=-.31))
    assert gate.check()==(False,'hurt_mood')
    s.put_state(replace(s.get_state(102),mood=-.3))
    assert gate.check()[0]
    night=night_of(datetime.fromtimestamp(gate.now,timezone.utc))
    for kind,target,intensity in [('anger',101,1),('shame',0,2),('distress',103,3)]:
        s.conn.execute("INSERT INTO emotion(guid,night,type,target,intensity,source,ts) VALUES(102,?,?,?,?, 'test',?)",(night,kind,target,intensity,gate.now))
        assert gate.check()==(False,'hurt_emotion')
        s.conn.execute('DELETE FROM emotion')
    s.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(101,102,?,'test')",(gate.now,))
    assert gate.check()==(False,'feud')


def test_once_a_day_per_target(gate):
    mark_teased(gate.store,102,now=gate.now)
    assert gate.check()==(False,'teased_today')
    assert gate.store.get_meta(TEASED_KEY.format(date='2026-11-12',guid=102)) is not None
    assert tease_ok(gate.store.conn,101,102,personas=gate.people,human=1,now=gate.now+86400)[0]


def test_him_once_an_evening_and_never_when_hurt(gate):
    s=gate.store
    s.conn.execute("INSERT INTO attraction_state(guid,stance,version,hurt_until,updated) VALUES(101,'none',1,?,?)",(gate.now+1,gate.now))
    assert gate.check(1)==(False,'his_hurt')
    s.conn.execute('DELETE FROM attraction_state')
    s.set_meta('banter:owner-evening','2026-11-12')
    assert gate.check(1)==(False,'his_evening')

@pytest.mark.parametrize('change',['dislikes','feud','hurt_emotion'])
def test_own_allowance_never_bypasses_kindness(gate,change):
    from leeroy_mind.culture.tease import _tease_ok
    s=gate.store
    mark_teased(s,102,now=gate.now)
    if change=='dislikes':
        s.put_relationship(Relationship(101,102,affinity=29))
    elif change=='feud':
        s.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(101,102,?,'test')",(gate.now,))
    else:
        s.conn.execute("INSERT INTO emotion(guid,night,type,intensity,source,ts) VALUES(102,'2026-11-12','shame',2,'test',?)",(gate.now,))
    assert _tease_ok(s.conn,101,102,personas=gate.people,human=1,now=gate.now,allowance=True)==(False,change)


def test_day_and_evening_have_different_boundaries(gate):
    s=gate.store
    midnight=datetime(2026,11,13,0,tzinfo=timezone.utc).timestamp()
    mark_teased(s,102,now=midnight-1)
    assert tease_ok(s.conn,101,102,personas=gate.people,human=1,now=midnight)[0]
    s.set_meta('banter:owner-evening','2026-11-12')
    assert tease_ok(s.conn,101,1,personas=gate.people,human=1,now=midnight)==(False,'his_evening')
    assert tease_ok(s.conn,101,1,personas=gate.people,human=1,now=midnight+6*3600)[0]
