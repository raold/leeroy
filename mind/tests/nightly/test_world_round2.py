"""Manual/night promotion and roster errors, using isolated fixtures only."""
import json
import logging
import random
from types import SimpleNamespace as NS

import pytest
from conftest import run
from nightly.test_world_personas import member, observe, world, enabled
from nightly.test_night_newcomers import context, generate
from test_generate import _cast_llm
from leeroy_mind.__main__ import _doctor_unvoiced
from leeroy_mind.config import load_config
from leeroy_mind.personas.generate import generate_cast
from leeroy_mind.personas.io import load_persona
from leeroy_mind.selfies import files, store as selfies
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.world import targets


@pytest.mark.parametrize("writer",["manual","night"])
@pytest.mark.parametrize("seed_state",["valid","missing","invalid"])
def test_promoted_cast_can_get_persona_and_fresh_selfie_choices(h,writer,seed_state):
    observe(h,member(h))
    old=load_persona(h.store,101)
    world(h)
    with h.store.transaction():
        if seed_state=="missing":
            h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
        elif seed_state=="invalid":
            h.store.conn.execute("UPDATE persona_core SET json='invalid fixture' WHERE guid=101")
        h.store.conn.execute("INSERT INTO selfie_set(guid,set_no,reason,status,requested,rendered) VALUES(101,1,'first','ready',1,2)")
    retained=files.candidate(h.settings.data_dir,101,1,0)
    retained.parent.mkdir(parents=True,exist_ok=True)
    retained.write_bytes(b"synthetic image fixture")
    assert "101" in _doctor_unvoiced(h.store,1)[0][1]
    if writer=="manual":
        cfg=load_config(h.settings.data_dir/"mind.toml")  # auto_newcomers remains off
        llm=_cast_llm()
        planned=run(generate_cast(h.store,None,cfg,human_guid=1,data_dir=h.settings.data_dir,
                                 rng=random.Random(1),dry_run=True))
        assert [s.guid for s in planned.slots]==[101]
        assert load_persona(h.store,101) is None
        report=run(generate_cast(h.store,llm,cfg,human_guid=1,data_dir=h.settings.data_dir,rng=random.Random(1)))
        assert report.created==[101] and not report.failed
        calls=llm.calls
    else:
        ctx=context(h,"Nolan Alder")
        run(generate(ctx))
        calls=ctx.reasoner.llm.calls
    p=load_persona(h.store,101)
    assert p and p.identity.age >= 25 and p != old
    if seed_state=="valid":
        assert old.identity.real_name in json.dumps(calls)
    assert not _doctor_unvoiced(h.store,1)
    assert h.store.conn.execute("SELECT retired FROM world_member WHERE guid=101").fetchone()[0]==1
    assert selfies.counts(h.store.conn)["waiting"]==0
    assert selfies.ready_set(h.store,101,1).decided=="passed"
    assert h.store.get_meta('selfies:set-world:101:1') == '1'
    assert selfies.ensure_first_sets(h.store,[101],now=h.T)==1
    assert selfies.queued_sets(h.store)[0].set_no==2
    assert retained.read_bytes()==b"synthetic image fixture"


def test_roster_invalid_is_a_known_safe_refusal(h,monkeypatch,caplog):
    enabled(h)
    monkeypatch.setattr(targets,"_refusal_logged",False)
    async def scenario():
        async def send(cmd,**kw):
            return Ack(0,cmd.id,False,"world_roster_invalid")
        app=NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,
               is_backlog=lambda _:False,bridge=NS(send=send))
        service=targets.Targets(app)
        await service.hello(Hello(0,"test","1",21,1))
        assert not await service.refresh()
    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "code=world_roster_invalid" in caplog.text


@pytest.mark.parametrize("writer",["manual","night"])
@pytest.mark.parametrize("change",["seed_version","left_cast","save_failure"])
def test_promotion_does_not_overwrite_concurrent_changes_and_rolls_back(h,monkeypatch,writer,change):
    from leeroy_mind.personas import generate as manual, newcomers
    observe(h,member(h))
    old=load_persona(h.store,101)
    world(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO selfie_set(guid,set_no,reason,status,requested,rendered) VALUES(101,1,'first','ready',1,2)")
    retired=h.store.conn.execute("SELECT retired FROM world_member WHERE guid=101").fetchone()[0]
    module=manual if writer=="manual" else newcomers
    async def generated(*a,**kw):
        with h.store.transaction():
            if change=="seed_version":
                h.store.conn.execute("UPDATE persona_core SET version=version+1 WHERE guid=101")
            elif change=="left_cast":
                h.store.conn.execute("DELETE FROM cast_member WHERE guid=101")
        return old
    monkeypatch.setattr(module,"generate_persona",generated)
    if change=="save_failure":
        def refused(*a,**kw):
            raise ValueError("synthetic_save_failure")
        monkeypatch.setattr(module,"save_persona",refused)
    if writer=="manual":
        cfg=load_config(h.settings.data_dir/"mind.toml")
        report=run(generate_cast(h.store,None,cfg,human_guid=1,data_dir=h.settings.data_dir,rng=random.Random(1)))
        assert not report.created and 101 in report.failed
    else:
        run(generate(context(h,"Nolan Alder")))
    assert load_persona(h.store,101) is None
    assert load_persona(h.store,101,include_world=True)==old
    assert selfies.ready_set(h.store,101,1).decided is None
    assert h.store.conn.execute("SELECT retired FROM world_member WHERE guid=101").fetchone()[0]==retired


def test_generate_dry_run_shows_promoted_member_name(h,capsys):
    from leeroy_mind.__main__ import main
    observe(h,member(h))
    expected=h.store.cast()[101].name
    world(h)
    assert main(['personas','--data-dir',str(h.settings.data_dir),'generate','--dry-run']) == 0
    output=capsys.readouterr().out
    assert expected+':' in output
    assert load_persona(h.store,101) is None
