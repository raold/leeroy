from dataclasses import replace
from types import SimpleNamespace
import pytest

from discord.test_discord_talk import rig_for, talk_for, answers
from discord.test_own_time import at, own_time, run_for, hand
from leeroy_mind.discord.desk import DiscordDesk
from leeroy_mind.endpoint import discord_args, BadRequest
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups import store as groups


def setup(store,tmp_path,hour=20):
    rig=rig_for(store,at(2026,10,29,hour),tmp_path)
    talk=talk_for(rig)
    app=talk.app
    app.data_dir=tmp_path
    app.on_his_discord_line=lambda *args:None
    app.discord_talk=talk
    desk=DiscordDesk(app)
    app.groups.config=GroupsConfig(enabled=True)
    return rig,talk,desk


def ask(rig,desk,**args):
    return rig.sync(desk.handle(discord_args(args)))


def test_group_create_post_readers_answers_and_membership_change(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path)
    try:
        result=ask(rig,desk,action='group_create',members=[101,102],name='Fixture group')
        assert result['ok'],result
        ch=result['channel']
        result=ask(rig,desk,action='post',channel=ch,text='@'+rig.engine.personas.get(101).character_name+' did you find the recipe?')
        assert result['ok'],result
        mid=result['id']
        from leeroy_mind.groups.audience import message_readers
        assert message_readers(store.conn,mid)=={1,101,102}
        rig.sync(rig.advance(160))
        assert [m['author'] for m in answers(rig,ch,mid)]==[101]
        assert ask(rig,desk,action='group_add',channel=ch,guid=103)['ok']
        assert not talk.instruction(103,ch,mid,None)
        assert not talk.context_lines(store.conn,ch,None)
        assert talk.plan(mid)==[]
        assert ask(rig,desk,action='group_leave',channel=ch)['ok']
        assert not ask(rig,desk,action='post',channel=ch,text='closed')['ok']
    finally:
        rig.sync(talk.close()); rig.close()


def test_group_own_time_is_per_member_and_off_releases_deferred(store,tmp_path,monkeypatch):
    rig,talk,desk=setup(store,tmp_path)
    try:
        own_time(rig)
        from leeroy_mind.groups import service
        now=rig.clock.now()
        monkeypatch.setattr(service.owntime,'due_at',lambda persona,**kw: now+(600 if persona.guid==102 else 0))
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        text=' '.join('@'+rig.engine.personas.get(g).character_name for g in (101,102))+' hello friends'
        mid=ask(rig,desk,action='post',channel=ch,text=text)['id']
        run_for(rig,talk,150)
        assert [m['author'] for m in answers(rig,ch,mid)]==[101]
        own_time(rig,False)
        run_for(rig,talk,160)
        assert {m['author'] for m in answers(rig,ch,mid)}=={101,102}
    finally:
        rig.sync(talk.close()); rig.close()


def test_group_ops_off_are_worded_and_arguments_strict(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path)
    try:
        talk.app.groups.config=GroupsConfig()
        result=ask(rig,desk,action='group_create',members=[101,102])
        assert not result['ok'] and 'turned off' in result['error']
        for args in [dict(action='group_create',members=[101]),dict(action='group_add',channel='general',guid=101),
                     dict(action='group_create',members=[101,101]),dict(action='group_rename',channel='gdm:1',name='x'*41)]:
            with pytest.raises(BadRequest):
                discord_args(args)
    finally:
        rig.sync(talk.close()); rig.close()


def test_group_old_pin_and_reaction_do_not_reach_added_members_card(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path)
    try:
        from leeroy_mind.discord import store as dstore
        from leeroy_mind.discord.transport import DiscordTarget
        from leeroy_mind.talk.pipeline import TurnMeta
        from leeroy_mind.talk.turns import Turn,TurnKind
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        old=ask(rig,desk,action='post',channel=ch,text='PRIVATE_PIN_FIXTURE')['id']
        dstore.pin(store,old,now=rig.clock.now())
        groups.add(store,ch,103,human=1,now=rig.clock.now())
        mid=ask(rig,desk,action='post',channel=ch,text='New conversation fixture')['id']
        turn=Turn(TurnKind.REPLY_HUMAN,103,ch,addressee=1)
        meta=TurnMeta('fixture',discord=DiscordTarget(mid,None),review_required=True)
        review=rig.engine.live_review
        review.config=replace(review.config,enabled=True)
        block=review.block(turn,meta,close=False)
        assert 'New conversation fixture' in block and 'PRIVATE_PIN_FIXTURE' not in block
        assert 'PRIVATE_PIN_FIXTURE' not in talk.recall_query(mid)
    finally:
        rig.sync(talk.close()); rig.close()


def test_group_romance_and_pair_timeline_exclude_group_material(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path)
    try:
        import random
        from leeroy_mind.talk.pipeline import TurnMeta
        from leeroy_mind.talk.turns import Turn,TurnKind
        from leeroy_mind.talk.budget import DayPhase
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        ask(rig,desk,action='post',channel=ch,text='PRIVATE_GROUP_FIXTURE')
        turn=Turn(TurnKind.FLIRT,101,ch,to_guid=1,addressee=1)
        assert rig.engine._romance(turn,TurnMeta('fixture'),rig.engine.personas.get(101),
            store.get_relationship(101,1),DayPhase.EVENING,rig.clock.now(),random.Random(1),dial=10) is None
        turn.channel='whisper'
        assert rig.engine._pair_lines(turn,[],rig.clock.now())==[]
    finally:
        rig.sync(talk.close()); rig.close()


def test_receipt_uses_historical_readers_with_separately_sampled_clocks(store,tmp_path):
    rig,talk,desk=setup(store,tmp_path)
    try:
        from leeroy_mind.groups.audience import message_readers
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        mid=ask(rig,desk,action='post',channel=ch,text='Fixture clocks')['id']
        line=int(store.get_meta(f'discord:line:{mid}'))
        store.conn.execute('UPDATE chat_line SET ts=ts+0.002 WHERE id=?',(line,))
        groups.add(store,ch,103,human=1,now=rig.clock.now())
        assert message_readers(store.conn,mid)=={1,101,102}
        store.conn.execute("UPDATE chat_line SET channel='discord:general' WHERE id=?",(line,))
        assert not message_readers(store.conn,mid)
    finally:
        rig.sync(talk.close()); rig.close()


@pytest.mark.parametrize('stance',['drawn','none'])
def test_group_composed_prompt_has_friendship_without_romance_or_private_notes(store,tmp_path,stance):
    rig,talk,desk=setup(store,tmp_path)
    try:
        from engine_rig import save_member
        from leeroy_mind.storage.rows import Relationship
        from leeroy_mind.talk.pipeline import TurnMeta
        from leeroy_mind.talk.turns import Turn,TurnKind
        save_member(store,101,player_gender='female',stance=stance)
        rig.engine.personas.invalidate(101)
        assert rig.engine.personas.get(101).romance.stance==stance
        store.put_relationship(Relationship(a=101,b=1,affinity=65,familiarity=80,
            attraction=90,romance_stage='whispers',notes='PRIVATE_ROMANTIC_NOTE_FIXTURE'))
        ch=ask(rig,desk,action='group_create',members=[101,102])['channel']
        turn=Turn(TurnKind.REPLY_HUMAN,101,ch,addressee=1)
        composed=rig.sync(rig.engine._compose(turn,TurnMeta('fixture'),live=False))
        text='\n'.join(m['content'] for m in composed.messages)
        assert "you're close" in text
        assert all(s not in text for s in ("you want him","you're into him",
            'PRIVATE_ROMANTIC_NOTE_FIXTURE','LOCAL-OVERLAY'))
    finally:
        rig.sync(talk.close()); rig.close()
