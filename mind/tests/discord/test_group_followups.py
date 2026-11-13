import json
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.groups import store as groups
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.memory.threads import ThreadTracker
from leeroy_mind.protocol import Chat

ON=GroupsConfig(enabled=True)


def feed(store,tracker,channel,who,text,now):
    event=Chat(int(now*1000),channel,who,{1:'Testmage',101:'Arkon',102:'Belra',103:'Coros'}[who],0,text,0,0)
    line=store.insert_chat_line(event,session_id=None,origin='human' if who==1 else 'mind',
        ts=now,audience=groups.readers(store.conn,channel))
    tracker.add(line,event,now)
    return line


@pytest.mark.parametrize('change',['add','remove'])
def test_membership_epochs_keep_extraction_prompts_and_memories_separate(store,clock,change):
    from test_extract import Rig,mem
    seed_test_cast(store)
    now=clock.now()
    channel=groups.create(store,human=1,members=[101,102] if change=='add' else [101,102,103],
        title='Fixture',created_by=1,now=now,config=ON)
    tracker=ThreadTracker(store)
    before_who,after_who=(101,103) if change=='add' else (103,101)
    before=[feed(store,tracker,channel,1,'BEFORE_EPOCH_MARKER: I will gather herbs for our dungeon trip.',now),
        feed(store,tracker,channel,before_who,'I can bring a flask and healing supplies next weekend.',now)]
    (groups.add if change=='add' else groups.remove)(store,channel,103,human=1,now=now)
    after=[feed(store,tracker,channel,1,'AFTER_EPOCH_MARKER: We need a cook for the raid dinner.',now),
        feed(store,tracker,channel,after_who,'I can arrange food for everyone before the next raid.',now)]
    closed=tracker.flush_all()
    assert [t.line_ids for t in closed]==[tuple(before),tuple(after)]
    assert closed[0].key!=closed[1].key
    names={101:'Arkon',103:'Coros'}
    replies=[json.dumps({'memories':[mem(names[who],text=text,ev=tuple(f'L{i}' for i in ids))],
        'overheard':None}) for who,text,ids in (
            (before_who,'I offered healing supplies for our next expedition.',before),
            (after_who,'I agreed to arrange dinner for the next raid.',after))]
    rig=Rig(store,clock,replies)
    assert [run(rig.ex.extract(t)).written for t in closed]==[1,1]
    prompts=[json.dumps(c['messages']) for c in rig.llm.calls if 'messages' in c]
    assert len(prompts)==2
    assert 'BEFORE_EPOCH_MARKER' in prompts[0] and 'AFTER_EPOCH_MARKER' not in prompts[0]
    assert 'AFTER_EPOCH_MARKER' in prompts[1] and 'BEFORE_EPOCH_MARKER' not in prompts[1]
    rows=store.conn.execute('SELECT owner,evidence FROM memory ORDER BY id').fetchall()
    assert [(r['owner'],set(json.loads(r['evidence']))) for r in rows]==[
        (before_who,{f'chat:{i}' for i in before}),(after_who,{f'chat:{i}' for i in after})]


def test_same_clock_remove_rejoin_starts_another_epoch_even_with_same_readers(store,clock):
    now=clock.now()
    channel=groups.create(store,human=1,members=[101,102,103],title='Fixture',created_by=1,now=now,config=ON)
    tracker=ThreadTracker(store)
    first=feed(store,tracker,channel,1,'Before a member leaves',now)
    groups.remove(store,channel,103,human=1,now=now)
    groups.add(store,channel,103,human=1,now=now)
    second=feed(store,tracker,channel,1,'After she returns',now)
    assert [t.line_ids for t in tracker.flush_all()]==[(first,),(second,)]


@pytest.mark.parametrize('guid',[101,102])
def test_server_group_card_is_present_without_officer_bank(populated,clock,guid):
    from leeroy_mind.awareness import CardContext,CardSources,card
    from awareness.test_card import section
    from leeroy_mind.personas.io import save_persona
    if guid==102:
        save_persona(populated,make_persona(102,'Belra',guild_role='officer'),race='Night Elf',
            class_name='Druid',source='seed',ts=clock.now())
    source=CardSources(populated.conn,bank_snapshot={'money':87654321,'read_at':clock.now()})
    result=card(source,guid,context=CardContext(channel='gdm:1',readers=(1,101,102),server=True,
        purpose='Group fixture',self_line='You are online',here=('Testy',),now=clock.now()))
    assert 'Group fixture' in section(result,'context')
    assert 'You are online' in section(result,'here')
    assert not section(result,'bank') and '87654321' not in result.render(10000)
    if guid==102:
        officer=card(source,guid,context=CardContext(channel='discord:officer-chat',readers=(1,102),
            server=True,now=clock.now()))
        assert section(officer,'bank')


def test_replanned_drawn_group_answer_does_not_become_named(store,tmp_path):
    from discord.test_group_live import setup,ask
    rig,talk,desk=setup(store,tmp_path)
    try:
        channel=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=channel,text='Anyone have a spare recipe?')['id']
        assert not talk.plan(mid,group_chosen=[101])[0][1].discord_named
        store.conn.execute('UPDATE discord_message SET text=? WHERE id=?',('@Belra recipe?',mid))
        assert not talk.plan(mid,group_chosen=[101])[0][1].discord_named
        assert talk.plan(mid,group_chosen=[102])[0][1].discord_named
    finally:
        rig.sync(talk.close());rig.close()


def test_group_source_preserves_public_evidence_flag(store,clock,caplog):
    import logging
    from leeroy_mind.memory.privacy import derive_scope,GROUP
    from leeroy_mind.memory.writer import MemoryWriter
    channel=groups.create(store,human=1,members=[101,102],title='Fixture',created_by=1,now=clock.now(),config=ON)
    private=feed(store,ThreadTracker(store),channel,1,'A group observation',clock.now())
    public=store.insert_chat_line(Chat(0,'guild',1,'Testmage',0,'A public observation',0,0),
        session_id=None,origin='human')
    pure=derive_scope(store.conn,101,[f'chat:{private}'])
    mixed=derive_scope(store.conn,101,[f'chat:{private}',f'chat:{public}'])
    assert pure.scope_with==mixed.scope_with==GROUP and not pure.public and mixed.public
    with caplog.at_level(logging.INFO,logger='leeroy_mind'):
        scope=MemoryWriter(store,None,clock).scope_of(101,[f'chat:{private}',f'chat:{public}'],
            scope='secret')
    assert scope==('private',GROUP) and 'scope_secret_refused: owner=101' in caplog.text


def test_discord_nudge_covers_groups_without_widening_whisper_nudge():
    from leeroy_mind.tuning.nudges import in_scope
    assert in_scope(SimpleNamespace(scope='discord'),'gdm:1',1,1)
    assert not in_scope(SimpleNamespace(scope='whispers'),'gdm:1',1,1)


def test_group_changes_log_ids_without_names_or_titles(store,clock,caplog):
    import logging
    with caplog.at_level(logging.INFO,logger='leeroy_mind.groups.store'):
        channel=groups.create(store,human=1,members=[101,102],title='PRIVATE_TITLE',
            created_by=1,now=clock.now(),config=ON)
        groups.add(store,channel,103,human=1,now=clock.now())
        groups.rename(store,channel,'PRIVATE_RENAME')
        groups.remove(store,channel,103,human=1,now=clock.now())
    assert all(f'action={action}' in caplog.text for action in ('create','add','rename','remove'))
    assert 'group=1' in caplog.text and 'guid=103' in caplog.text
    assert 'PRIVATE' not in caplog.text


def test_group_photo_memory_uses_captured_readers(tmp_path):
    from discord.test_talk_photo import post_photo,photo_tasks
    from selfies.fakes import mind_app
    from leeroy_mind.memory.privacy import GROUP
    async def go():
        app=mind_app(tmp_path,toml='[photos]\nenabled=true\n[groups]\nenabled=true')
        app.llm._script=lambda *_:'A dog lies on a blue rug.'
        app.engine._queue=lambda items:None
        try:
            channel=groups.create(app.store,human=app.human_guid,members=[101,102],title='Fixture',
                created_by=app.human_guid,now=app.clock.now(),config=ON)
            reply=await post_photo(app,channel)
            assert reply['ok']
            await photo_tasks(app)
            groups.add(app.store,channel,103,human=app.human_guid,now=app.clock.now())
            for member in (101,103):
                app.discord.remember_photo(member,reply['id'])
            await photo_tasks(app)
            rows=app.store.conn.execute('SELECT * FROM memory WHERE key=?',
                (f"discord_photo:{reply['id']}",)).fetchall()
            assert len(rows)==1 and rows[0]['owner']==101 and rows[0]['scope_with']==GROUP
            assert 'blue rug' in rows[0]['text']
        finally:
            await app.stop()
    run(go())


from awareness.test_card import populated
