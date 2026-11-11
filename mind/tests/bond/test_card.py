from dataclasses import replace
import threading

import pytest

from conftest import run, make_mem
from guards.test_live import rig, turn
from leeroy_mind.talk.pipeline import TurnMeta


@pytest.mark.parametrize('enabled',[False,True])
def test_persona_and_dials_are_captured_before_read_only_worker(rig,monkeypatch,enabled):
    engine=rig.engine
    engine.cfg=replace(engine.cfg,talk=replace(engine.cfg.talk,aware=True),
                       romance=replace(engine.cfg.romance,activities=enabled))
    caller=threading.get_ident()
    original=engine.personas.get
    def caller_only(guid):
        assert threading.get_ident()==caller, 'persona_cache_accessed_in_worker'
        return original(guid)
    monkeypatch.setattr(engine.personas,'get',caller_only)
    text=run(engine.live_review.block_async(turn('whisper'),TurnMeta('bond-test'),close=True))
    assert '<awareness_observations>' in text


@pytest.mark.parametrize('enabled,channel,shown',[(False,'guild',False),(True,'guild',False),
    (False,'whisper',False),(True,'whisper',True),(True,'dm:101',False)])
@pytest.mark.parametrize('kind',['first_kill','gift'])
def test_event_backed_bond_memory_stays_out_of_public_lore_and_disabled_pins(rig,enabled,channel,shown,kind):
    from leeroy_mind.personas.io import save_persona, load_persona
    e=rig.engine
    p=load_persona(rig.store,101)
    p=replace(p,identity=replace(p.identity,player_gender='female'),romance=replace(p.romance,stance='fond'))
    save_persona(rig.store,p,source='seed',ts=rig.clock.now(),race='Orc',class_name='Warrior')
    e.cfg=replace(e.cfg,romance=replace(e.cfg.romance,activities=enabled))
    mid=rig.store.insert_memory(make_mem(owner=101,text='PRIVATE_BOND_SENTINEL first battle',
        key=f'bond:1:{kind}:first',scope='private',scope_with=1,actors=(1,101),
        t_event=rig.clock.now(),t_created=rig.clock.now(),evidence=('event:1',)))
    assert not e.recall.holders('first battle')
    composed=rig.sync(e._compose(turn(channel),TurnMeta('history',lore_memory=mid),live=False))
    assert (mid in composed.memory_ids) is shown
    assert ('PRIVATE_BOND_SENTINEL' in str(composed.messages)) is shown
