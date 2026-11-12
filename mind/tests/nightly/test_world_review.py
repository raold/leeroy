"""World review regressions: synthetic rosters, isolated SQLite and fake models."""
import asyncio
import json
import logging
import time
import pytest
from dataclasses import replace
from types import SimpleNamespace as NS

from conftest import run
from nightly.test_world_personas import enabled, member, observe, world, APPEARANCE
from nightly.test_night_newcomers import context, generate
from leeroy_mind.personas.io import load_persona, save_persona, PersonaCache
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.world import targets, stage, photos


@pytest.mark.parametrize("seed_state",["valid","missing","invalid"])
def test_world_join_seeds_a_screened_newcomer_and_becomes_writable(h,seed_state):
    old = load_persona(h.store,101)
    world(h)
    with h.store.transaction():
        if seed_state == "missing":
            h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
        elif seed_state == "invalid":
            h.store.conn.execute("UPDATE persona_core SET json='invalid fixture json' WHERE guid=101")
    # A fresh authoritative cast snapshot admits this former outsider.
    h.store.replace_cast(list(h.store.cast().values()) + [replace(member_to_cast(h),level=80)],h.T)
    ctx = context(h,"Nolan Alder")
    run(generate(ctx))
    p = load_persona(h.store,101)
    assert p and p.identity.age >= 25 and p != old
    assert 101 in h.store.cast() and PersonaCache(h.store).get(101)
    assert ctx.reasoner.calls == 2
    if seed_state == "valid":
        assert old.identity.real_name in json.dumps(ctx.reasoner.llm.calls)
    row = h.store.conn.execute("SELECT source,version FROM persona_core WHERE guid=101").fetchone()
    assert row[0] == "generated" and row[1] >= (1 if seed_state=="missing" else 2)
    save_persona(h.store,p,source="generated",ts=h.T,race="",class_name="")


def member_to_cast(h):
    return h.store.cast(include_world=True)[101]


def test_roster_refusal_is_once_and_never_logs_ack_text(h,monkeypatch,caplog):
    enabled(h)
    monkeypatch.setattr(targets,"_refusal_logged",False,raising=False)
    async def scenario():
        for _ in range(2):
            app=NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,is_backlog=lambda _:False)
            async def send(cmd,**kw):
                return Ack(0,cmd.id,False,"PRIVATE_ACK_TEXT")
            app.bridge=NS(send=send)
            service=targets.Targets(app)
            await service.hello(Hello(0,"test","1",21,1))
            for _ in range(5):
                assert not await service.refresh()
    with caplog.at_level(logging.INFO):
        run(scenario())
    assert caplog.text.count("world_roster_refused:") == 1
    assert "PRIVATE_ACK_TEXT" not in caplog.text


def test_roster_polls_never_reserve_permanent_wire_ids(h):
    enabled(h)
    async def scenario():
        app=NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,is_backlog=lambda _:False)
        preserved=set()
        async def send(cmd,*,preserve_id=False):
            if preserve_id:
                preserved.add(cmd.id)
            assert cmd.request != ""
            return Ack(0,"new-wire-id",False,"realm_off")
        app.bridge=NS(send=send)
        service=targets.Targets(app)
        await service.hello(Hello(0,"test","1",21,1))
        for _ in range(1000):
            await service.refresh()
        assert preserved == set()
    run(scenario())


def test_refused_sampler_falls_back_without_losing_generation(h,monkeypatch):
    from leeroy_mind.config import CastCfg
    from nightly.test_world_personas import gen_context
    class Refused(ValueError):
        pass
    calls=[]
    def refuse(*a,**kw):
        calls.append(1)
        raise Refused("looks_no_distinct_candidate")
    monkeypatch.setattr(stage,"_sampler_api",lambda:(refuse,Refused),raising=False)
    enabled(h)
    observe(h,member(h))
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
    ctx=gen_context(h)
    run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
    p=load_persona(h.store,101,include_world=True)
    assert p and p.appearance == APPEARANCE
    assert stage.failures(h.store,101)[0] == 0
    assert calls == [1]


def populate(h,n=700):
    from leeroy_mind.personas.io import canonical_json
    enabled(h,"selfies=true\n")
    base=replace(load_persona(h.store,101),appearance=APPEARANCE)
    template=member(h)
    observe(h,*(replace(template,guid=10000+i,name=f"Fixture{i}") for i in range(n)))
    with h.store.transaction():
        for i in range(n):
            p=replace(base,guid=10000+i,character_name=f"Fixture{i}")
            h.store.conn.execute("INSERT INTO persona_tier VALUES(?,'world')",(p.guid,))
            h.store.put_persona(p.guid,canonical_json(p),"Synthetic card",5,"generated",h.T)


def test_700_member_photo_counters_are_linear(h,monkeypatch):
    populate(h)
    calls=[]
    original=photos.roster
    def counted(*a,**kw):
        calls.append(1)
        return original(*a,**kw)
    monkeypatch.setattr(photos,"roster",counted)
    began=time.monotonic()
    ctx=h.ctx()
    assert photos.photo_count(ctx) == ctx.settings.world_selfies_per_night
    assert photos.image_count(ctx) == ctx.settings.world_selfies_per_night
    assert len(calls) <= 3
    assert time.monotonic()-began < 1.5


def test_budget_world_counters_run_off_loop(h,monkeypatch):
    import threading
    from leeroy_mind.nightly.budget import plan_night
    populate(h)
    main=threading.get_ident()
    threads=[]
    from leeroy_mind.world import counting
    original=counting.profile
    def checked(ctx):
        threads.append(threading.get_ident())
        return original(ctx)
    monkeypatch.setattr(counting,"profile",checked)
    run(plan_night(h.ctx(),stages=frozenset({photos.PHOTO,photos.IMAGE,stage.STAGE})))
    assert threads and main not in threads


def test_realm_lines_use_world_names(h):
    from leeroy_mind.console.realmview import realm_view
    observe(h,member(h))
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO realm_line(ts,guid,guild,channel,kind,text,state,command_id) VALUES(?,101,50,'trade','rival','Synthetic line','sent','0123456789abcdef0123456789abcdef')",(h.T,))
    lines=realm_view(h.store.conn,now=h.T)["lines"]
    assert lines[0]["name"] == "Arkon"


def test_image_admission_needs_actual_work_and_remaining_allowance(h):
    from leeroy_mind.world.counting import image_work
    from selfies.fakes import PHOTO
    enabled(h,"selfies=true\n")
    observe(h,member(h))
    h.persona(101,appearance=APPEARANCE)
    world(h)
    ctx=h.ctx()
    ctx.settings=replace(ctx.settings,world_selfies_per_night=1)
    assert photos.image_count(ctx) == 1  # budget includes the kind that could be written
    assert not run(image_work(ctx))     # an unchosen kind cannot use the image worker
    p=load_persona(h.store,101,include_world=True)
    save_persona(h.store,replace(p,photo=PHOTO),source="seed",ts=h.T,race="",class_name="",include_world=True)
    assert run(image_work(ctx))
    ctx.skip(photos.IMAGE,"101:1")
    assert not run(image_work(ctx))
