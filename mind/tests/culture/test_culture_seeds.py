import asyncio
import random
import pytest
from nightly.test_night_lexicon import term
from leeroy_mind.culture.seeds import culture_seeds
from leeroy_mind.server.seeds import Seed
from leeroy_mind.server.rhythm import choose_cast
from leeroy_mind.server.runtime import snapshot as real_snapshot


def collect(h):
    return culture_seeds(h.store.conn, now=h.T+4*86400, local=h.clock.local(),
                         personas={g:h.ctx().personas.get(g) for g in h.ctx().members()}, human=1)


def test_joke_seed_fact_has_its_origin(h):
    ident=term(h)
    seeds=collect(h)
    assert len(seeds)==1
    assert seeds[0].key==f'joke:{ident}'
    assert 'floor inspector' in seeds[0].fact and 'said:' in seeds[0].fact
    assert seeds[0].channels==('general','memes')
    assert seeds[0].about==(0,101)


@pytest.mark.parametrize('channel,to',[('whisper',102),('dm:101',1),('officer',0),('voice:officers',0),('discord:officer-chat',0),('discord:unknown',0)])
def test_private_origin_never_seeds(h,channel,to):
    ident=term(h)
    row=h.store.conn.execute('SELECT origin_ref FROM lexicon WHERE id=?',(ident,)).fetchone()
    h.store.conn.execute('UPDATE chat_line SET channel=?,to_guid=? WHERE id=?',(channel,to,int(row[0][5:])))
    assert collect(h)==[]


def test_butt_cast_only_through_the_gate():
    seed=Seed('joke:1','joke',('general',),'A joke',(102,101))
    assert choose_cast('general',seed,{}, {},officers=frozenset(),human=1,
                       in_play=frozenset(),ties=lambda a,b:1,rng=random.Random(1))==[]
from types import SimpleNamespace
from conftest import make_persona, run
from server.test_engine import app, finished
from leeroy_mind.server import seeds as server_seeds
from leeroy_mind.storage.rows import Relationship


def test_cast_gate_checks_starter_and_rejects_self(monkeypatch):
    from leeroy_mind.server import rhythm
    people={g:make_persona(g,f'Member{g}') for g in (101,102,103)}
    states={g:SimpleNamespace(presence=SimpleNamespace(state='online')) for g in people}
    seed=Seed('joke:1','joke',('general',),'A joke',(102,101))
    monkeypatch.setattr(rhythm,'pick_speakers',lambda weights,n,rng:[next(iter(weights))] if weights else [])
    args=dict(officers=frozenset(),human=1,in_play=frozenset(),ties=lambda a,b:1,rng=random.Random(1))
    calls=[]
    assert choose_cast('general',seed,states,people,**args,tease=lambda a,b:calls.append((a,b)) or False)==[]
    assert calls==[(101,102)]
    assert len(choose_cast('general',seed,states,people,**args,tease=lambda a,b:True))>=2
    assert choose_cast('general',Seed('joke:2','joke',('general',),'A joke',(101,102)),states,people,**args,tease=lambda a,b:True)==[]


@pytest.mark.parametrize('update',["status='vetoed'","status='dormant'","retired=1","last_used=9999999999"])
def test_inactive_retired_and_recent_do_not_seed(h,update):
    ident=term(h)
    h.store.conn.execute(f'UPDATE lexicon SET {update} WHERE id=?',(ident,))
    assert collect(h)==[]


def test_gather_off_never_reads_culture(h,monkeypatch):
    import leeroy_mind.culture.seeds as module
    def forbidden(*args,**kwargs):
        raise AssertionError('culture consulted while off')
    monkeypatch.setattr(module,'culture_seeds',forbidden)
    ctx=h.ctx()
    server_seeds.gather(h.store.conn,now=h.T,local=h.clock.local(),cast={g:ctx.personas.get(g) for g in ctx.members()},officers=frozenset(),human=1,human_name='',trial_rank=4)


def joke_app(app,monkeypatch):
    from leeroy_mind.culture import seeds as module
    seed=Seed('joke:1','joke',('general',),'Running joke with real origin',(102,101))
    (app.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    app.observed.seeds=[seed]
    monkeypatch.setattr('leeroy_mind.server.rhythm.choose_cast',lambda *a,**kw:[101,103,102])
    app.store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used,butt) VALUES(1,'joke','floor inspector','chat:1',101,0,0,102)")
    for g in app.observed.personas:
        app.store.put_relationship(Relationship(g,102,affinity=60))
    monkeypatch.setattr(module,'_culture_seeds',lambda *a,**kw:[seed])
    monkeypatch.setattr(app.server.engine,'_tease',lambda *a,**kw:True)
    return seed


def test_instruction_and_mark_only_after_first_delivery(app,monkeypatch):
    from leeroy_mind.culture.tease import TEASED_KEY
    joke_app(app,monkeypatch)
    async def go():
        await app.server.engine.tick(app.clock.now())
        assert not app.store.get_meta(TEASED_KEY.format(date=app.clock.local().date(),guid=102))
        await finished(app.server.engine)
    run(go())
    assert app.turns
    assert 'Tease ' in app.turns[0][0].instruction and 'keep it kind.' in app.turns[0][0].instruction
    assert app.store.get_meta(TEASED_KEY.format(date=app.clock.local().date(),guid=102))
    assert app.store.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0]==1


def test_dropped_joke_does_not_mark_or_spend_seed(app,monkeypatch):
    seed=joke_app(app,monkeypatch)
    async def drop(turn,meta):
        return []
    monkeypatch.setattr(app.engine,'speak_meta',drop)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert app.store.get_meta('server:seed:'+seed.key) is None
    assert app.store.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0]==0

def test_retained_private_source_vetoes_public_origin(h):
    ident=term(h)
    private=h.say(h.T,101,'A private invented line',channel='dm:101',to=1)
    h.store.set_meta(f'nightly:lexicon-sources:{ident}',f'["chat:{private}"]')
    assert collect(h)==[]


def test_deleted_origin_is_never_reconstructed_from_label(h):
    ident=term(h)
    h.store.conn.execute('DELETE FROM chat_line WHERE id=(SELECT CAST(substr(origin_ref,6) AS INTEGER) FROM lexicon WHERE id=?)',(ident,))
    assert collect(h)==[]


def test_event_seed_uses_revalidated_event_fact(h):
    from culture.test_origins import wipes
    from leeroy_mind.culture.origins import event_origins
    from leeroy_mind.nightly import lexicon
    from nightly.test_lexicon_wear import enable
    enable(h)
    wipes(h)
    origin=event_origins(h.store.conn,(h.T-200,h.T))[0]
    lexicon.create_term(h.store,h.ctx(),kind='joke',term='floor inspector',origin_ref=origin.refs[0],
        originator=origin.originator,now=origin.when,butt=origin.butt,origin_kind='event',
        origin_refs=origin.refs,refs=origin.refs)
    assert 'died first in 2 wipes' in collect(h)[0].fact
    h.store.conn.execute('DELETE FROM game_event WHERE id=?',(int(origin.refs[-1][6:]),))
    assert collect(h)==[]


def test_send_hook_rechecks_kindness_and_scope(app,monkeypatch):
    from leeroy_mind.server.engine import Conversation
    from leeroy_mind.talk.pipeline import TurnMeta, TurnWithdrawn
    from leeroy_mind.talk.turns import Turn, TurnKind
    seed=joke_app(app,monkeypatch)
    e=app.server.engine
    c=Conversation('test','general',seed,[101,103],app.clock.now(),5)
    e._live[c.id]=c
    turn=Turn(TurnKind.OPEN,101,'discord:general')
    meta=TurnMeta('',server_conversation=c.id)
    assert e._instruction(turn,meta,'instruction')=='instruction'
    monkeypatch.setattr(e,'_tease',lambda *a,**kw:False)
    with pytest.raises(TurnWithdrawn):
        e._instruction(turn,meta,'instruction')
    monkeypatch.setattr(e,'_tease',lambda *a,**kw:True)
    monkeypatch.setattr('leeroy_mind.culture.seeds._culture_seeds',lambda *a,**kw:[])
    with pytest.raises(TurnWithdrawn):
        e._instruction(turn,meta,'instruction')

def test_each_teasing_speaker_is_checked_and_target_can_reply(app,monkeypatch):
    from leeroy_mind.server.engine import Conversation
    seed=joke_app(app,monkeypatch)
    e=app.server.engine
    c=Conversation('test','general',seed,[101,103,102],app.clock.now(),5,teaser=101,teased=True)
    monkeypatch.setattr(e,'_tease',lambda a,b,**kw:a==101)
    assert e._joke_valid(c,101)
    assert not e._joke_valid(c,103)
    assert e._joke_valid(c,102)


def test_runtime_gathers_public_joke_in_read_only_worker(app):
    from leeroy_mind.protocol import Chat
    (app.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=app.clock.now()
    line=app.store.insert_chat_line(Chat(ts=int((now-5*86400)*1000),channel='guild',
        from_guid=101,from_name='Member101',to_guid=0,text='floor inspector',map=0,zone=0),
        session_id=None,origin='game')
    app.store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used) VALUES('joke','floor inspector',?,101,?,?)",
                           (f'chat:{line}',now-5*86400,now-4*86400))
    observed=run(real_snapshot(app,now,include_seeds=True))
    assert any(s.kind=='joke' and 'floor inspector' in s.fact for s in observed.seeds)

@pytest.mark.parametrize('failure',[asyncio.CancelledError,RuntimeError])
@pytest.mark.parametrize('persisted',[True,False])
def test_interrupted_delivery_settles_only_real_first_post(app,monkeypatch,failure,persisted):
    from leeroy_mind.server.engine import Conversation
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.culture.tease import TEASED_KEY
    seed=joke_app(app,monkeypatch)
    conversation=Conversation('interrupted','general',seed,[101,103],app.clock.now(),1)
    e=app.server.engine
    e._live[conversation.id]=conversation
    captured=[]
    async def interrupted(turn,meta):
        meta.turn_id='interrupted-turn'
        captured.append(meta)
        if persisted:
            dstore.post(app.store,channel='general',author=turn.speaker,origin='mind',
                        text='Invented joke delivery',turn=meta.turn_id,now=app.clock.now())
        raise failure()
    monkeypatch.setattr(app.engine,'speak_meta',interrupted)
    async def go():
        if failure is asyncio.CancelledError:
            with pytest.raises(failure):
                await e._conversation(conversation)
        else:
            await e._conversation(conversation)
    run(go())
    assert bool(app.store.get_meta('server:seed:'+seed.key)) is persisted
    assert bool(app.store.get_meta(TEASED_KEY.format(date=app.clock.local().date(),guid=102))) is persisted
    assert app.store.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0]==int(persisted)
    conversation.teased=False  # durable receipt still prevents a second charge
    e._settle_joke(conversation,captured[0])
    assert app.store.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0]==int(persisted)


def test_pre_delivery_competing_use_refuses_but_own_delivery_continues(app,monkeypatch):
    from leeroy_mind.protocol import Chat
    from leeroy_mind.server.engine import Conversation
    (app.data_dir/'mind.toml').write_text('[server]\nenabled=true\n[culture]\nenabled=true\n')
    now=app.clock.now()
    line=app.store.insert_chat_line(Chat(ts=int((now-5*86400)*1000),channel='guild',
        from_guid=101,from_name='Member101',to_guid=0,text='floor inspector',map=0,zone=0),
        session_id=None,origin='game')
    app.store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used) VALUES('joke','floor inspector',?,101,?,?)",
                           (f'chat:{line}',now-5*86400,now-4*86400))
    people=app.observed.personas
    seed=culture_seeds(app.store.conn,now=now,local=app.clock.local(),personas=people,human=1)[0]
    c=Conversation('cooldown','general',seed,[101,103],now,5)
    assert app.server.engine._joke_valid(c,101)
    app.store.conn.execute('UPDATE lexicon SET last_used=?',(now,))
    assert not app.server.engine._joke_valid(c,101)
    c.teased=True
    assert app.server.engine._joke_valid(c,101)
