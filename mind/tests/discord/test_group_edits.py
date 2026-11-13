"""An edit changes pending members' timing without choosing their recipients again."""
import json

import pytest

from discord.test_group_live import ask, setup
from discord.test_discord_talk import answers
from discord.test_own_time import own_time, run_for
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.talk import RETRY_S, TRIES
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups.service import OWED


def pending(store,mid):
    return json.loads(store.get_meta(OWED+str(mid)))


def addressed(rig,guids,text):
    return ' '.join('@'+rig.engine.personas.get(g).character_name for g in guids)+' '+text


@pytest.mark.parametrize('urgent_first',[False,True])
def test_edit_recomputes_urgency_for_same_selected_members(store,tmp_path,monkeypatch,urgent_first):
    rig,talk,desk=setup(store,tmp_path,hour=3)
    try:
        own_time(rig)
        ch=ask(rig,desk,action='group_create',members=[101,102,103])['channel']
        first='urgent, need help' if urgent_first else 'did you find that recipe'
        mid=ask(rig,desk,action='post',channel=ch,text=addressed(rig,[101,102],first))['id']
        before=pending(store,mid)
        assert set(before)=={'101','102'}
        text='did you find that recipe' if urgent_first else 'urgent, need help'
        with monkeypatch.context() as patch:
            patch.setattr(talk,'plan',lambda *args,**kwargs:pytest.fail('an edit must not choose recipients'))
            assert ask(rig,desk,action='edit',message=mid,text=addressed(rig,[103],text))['ok']
        after=pending(store,mid)
        assert set(after)==set(before)
        for guid,receipt in after.items():
            assert receipt['urgent'] is not urgent_first
            assert (receipt['due']>before[guid]['due']) is urgent_first
        run_for(rig,talk,500)
        assert {m['author'] for m in answers(rig,ch,mid)}==(set() if urgent_first else {101,102})
    finally:
        rig.sync(talk.close()); rig.close()


def test_edit_changes_only_pending_recipients_and_deleted_replies_stay_delivered(store,tmp_path,monkeypatch):
    from leeroy_mind.groups import service
    rig,talk,desk=setup(store,tmp_path)
    try:
        own_time(rig)
        now=rig.clock.now()
        monkeypatch.setattr(service.owntime,'due_at',lambda persona,**kw:
            now if persona.guid==101 or kw['urgent'] else now+1800)
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=ch,text=addressed(rig,[101,102],'hello friends'))['id']
        run_for(rig,talk,150)
        sent=answers(rig,ch,mid)
        assert [m['author'] for m in sent]==[101]
        dstore.delete(store,sent[0]['id'],author=101,now=rig.clock.now())
        assert ask(rig,desk,action='edit',message=mid,text=addressed(rig,[101,102],'urgent, need help'))['ok']
        assert set(pending(store,mid))=={'102'}
        run_for(rig,talk,200)
        assert [m['author'] for m in answers(rig,ch,mid) if m['deleted'] is None]==[102]
        assert store.conn.execute("SELECT count(*) FROM discord_message WHERE reply_to=? AND author=101",(mid,)).fetchone()[0]==1
    finally:
        rig.sync(talk.close()); rig.close()


def test_delete_clears_pending_without_reviving_on_later_action(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path,hour=3)
    try:
        own_time(rig)
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=ch,text=addressed(rig,[101,102],'hello friends'))['id']
        assert pending(store,mid)
        assert ask(rig,desk,action='delete',message=mid)['ok']
        assert pending(store,mid)=={}
        talk.on_his_action()
        run_for(rig,talk,500)
        assert pending(store,mid)=={} and answers(rig,ch,mid)==[]
    finally:
        rig.sync(talk.close()); rig.close()


def test_edit_preserves_retry_counters_and_never_revives_exhausted_recipient(store,tmp_path,monkeypatch):
    rig,talk,desk=setup(store,tmp_path,hour=3)
    try:
        own_time(rig)
        queued=[]
        monkeypatch.setattr(rig.engine,'_queue',lambda items:queued.extend(items))
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=ch,text=addressed(rig,[101],'urgent, need help'))['id']
        run_for(rig,talk,330)
        assert len(queued)==1
        counters=dict(talk.app.groups._queued)
        assert ask(rig,desk,action='edit',message=mid,text=addressed(rig,[102],'urgent, need help again'))['ok']
        assert talk.app.groups._queued==counters and set(pending(store,mid))=={'101'}
        run_for(rig,talk,(TRIES+1)*RETRY_S)
        assert len(queued)==TRIES and pending(store,mid)=={}
        assert ask(rig,desk,action='edit',message=mid,text=addressed(rig,[102],'urgent, need help again now'))['ok']
        run_for(rig,talk,RETRY_S)
        assert len(queued)==TRIES and pending(store,mid)=={}
    finally:
        rig.sync(talk.close()); rig.close()


def test_disabled_groups_leave_persisted_timing_untouched_on_action(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path,hour=3)
    try:
        own_time(rig)
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=ch,text=addressed(rig,[101],'hello friends'))['id']
        before=store.get_meta(OWED+str(mid))
        talk.app.groups.config=GroupsConfig()
        dstore.edit(store,mid,author=1,text='urgent, need help',now=rig.clock.now())
        talk.on_his_action()
        run_for(rig,talk,500)
        assert store.get_meta(OWED+str(mid))==before and answers(rig,ch,mid)==[]
    finally:
        rig.sync(talk.close()); rig.close()
