"""World admission and isolation, using invented members and fake models only."""
import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from conftest import run
from test_generate import _persona_json
from nightly.test_night_enrich import ENRICHED, APPEARANCE
from leeroy_mind.config import CastCfg
from leeroy_mind.personas.io import PersonaCache, load_persona
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.testing.fake_llm import FakeLLM, script_from
from leeroy_mind.world import config, targets, stage, view, photos
from leeroy_mind.world.protocol import WorldMember, WorldRoster, WorldSay


@pytest.fixture(autouse=True)
def fallback_looks(monkeypatch):
    # These scripted generation checks exercise enrichment, even after task 46 merges.
    monkeypatch.setattr(stage,"_looks_sampler",lambda:None)


def enabled(h,extra=""):
    (h.settings.data_dir/"mind.toml").write_text("[world]\nenabled=true\n"+extra,encoding="utf-8")


def member(h,guid=101,guild=50,level=24):
    m = h.store.cast()[guid]
    return WorldMember(guid,2,guild,"Test rivals",m.name,m.race,m.class_,m.gender,level,3,True,1,1637)


def observe(h,*members):
    targets.save(h.store,members,own=21,human=1,now=h.T)


def world(h,guid=101):
    with h.store.transaction():
        h.store.conn.execute("INSERT OR REPLACE INTO persona_tier VALUES(?,'world')",(guid,))


def gen_context(h,*,cap=2,night="2026-10-29"):
    ctx = h.ctx(night=night)
    ctx.settings = replace(ctx.settings,world_per_night=cap)
    names = ("Nolan Alder","Petra Flint","Silas Wren") if night=="2026-10-29" else ("Silas Wren",)
    ctx.reasoner.llm = FakeLLM(script_from([v for name in names
        for v in ("synthetic world profile",_persona_json(name),"synthetic looks",json.dumps(ENRICHED))]))
    return ctx


@pytest.mark.parametrize("text",["", "[world]\nenabled=false", "[world]\nenabled=1", "[world]\nenabled=true\nrealm='yes'",
    "[world]\nenabled=true\nunknown=true", "[world]\nenabled=true\nworld_per_night=0", "[world]\nenabled=true\nworld_selfies_per_night=51"])
def test_world_invalid_and_missing_are_off(h,text):
    path = h.settings.data_dir/"mind.toml"
    path.write_text(text,encoding="utf-8")
    assert not config.load(path).enabled
    ctx = h.ctx()
    run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
    assert ctx.reasoner.calls == 0


def test_world_generation_any_level_cap_resume_and_no_private_logs(h,caplog):
    enabled(h)
    observe(h,member(h,101),member(h,102),member(h,103))
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid IN (101,102,103)")
    ctx = gen_context(h)
    with caplog.at_level(logging.INFO):
        run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
    assert set(h.store.persona_versions(tier="world")) == {101,102}
    assert set(h.store.persona_versions()) == {104,105}
    p = load_persona(h.store,101,include_world=True)
    assert p.identity.age >= 25 and p.appearance is not None and p.romance.stance == "none"
    assert "Character level: 24" in h.store.get_card(101,include_world=True)
    assert all(c["model"] == ctx.settings.reflection_model for c in ctx.reasoner.llm.calls)
    assert not any(s in caplog.text for s in ("Nolan","Petra","Arkon","synthetic world","early shifts"))
    resumed = gen_context(h)
    run(stage.make_stage(lambda:CastCfg(.7,.7))(resumed))
    assert resumed.reasoner.calls == 0
    tomorrow = gen_context(h,night="2026-10-30")
    run(stage.make_stage(lambda:CastCfg(.7,.7))(tomorrow))
    assert set(h.store.persona_versions(tier="world")) == {101,102,103}


def test_rivals_first_then_rank_and_missing_account_retires_without_deletion(h):
    a,b = member(h,101,60),member(h,102,50)
    observe(h,a,b)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO realm_guild(id,name,role,faction,status,added) VALUES(50,'Test rivals','drama','horde','active',0)")
    assert stage.priority(h.store,targets.roster(h.store)) == [102,101]
    world(h,101)
    before = h.store.get_persona_json(101,include_world=True)
    observe(h,b)
    assert 101 not in targets.roster(h.store)
    assert targets.roster(h.store,active=False)[101]["retired"] == 1
    assert h.store.get_persona_json(101,include_world=True) == before
    assert sum(g["retired"] for g in view.status(h.store.conn)["guilds"]) == 1


def test_cast_cache_excludes_an_outsider(h):
    cache = PersonaCache(h.store)
    assert cache.get(101)
    world(h)
    h.store.conn.execute("DELETE FROM cast_member WHERE guid=101")
    assert cache.get(101) is None and cache.base(101) is None
    assert 101 not in cache.all() and 101 not in h.store.cast()
    assert h.store.get_card(101) is None and load_persona(h.store,101) is None


@pytest.mark.parametrize("subsystem",["night","talk","guild_life","voice","console_personas","console_tuning","console_raid","names","claims","awareness","guards","fingerprint","selfies","realm"])
def test_world_excluded_from_each_default_subsystem_even_with_old_roster(h,subsystem):
    world(h)
    conn,now = h.store.conn,h.T
    if subsystem == "night":
        assert 101 not in h.ctx().members(include_away=True)
    elif subsystem == "talk":
        assert 101 not in PersonaCache(h.store).all()
    elif subsystem == "guild_life":
        from leeroy_mind.guild.ports import LivePorts
        ports = LivePorts(NS(clock=h.clock,store=h.store,personas=PersonaCache(h.store)))
        assert 101 not in ports.cast() and ports.persona(101) is None
    elif subsystem == "voice":
        from leeroy_mind.voice.brief import brief_entries
        assert 101 not in {r["guid"] for r in brief_entries(h.store)}
    elif subsystem == "console_personas":
        from leeroy_mind.console.personaview import roster_view,member_view
        assert 101 not in {r["guid"] for r in roster_view(conn,now=now)["members"]}
        assert member_view(conn,101,now=now) is None
    elif subsystem == "console_tuning":
        from leeroy_mind.console.tuneview import tuning_view
        assert 101 not in {r["guid"] for r in tuning_view(conn,now=now)["members"]}
    elif subsystem == "console_raid":
        from leeroy_mind.console.raidview import people
        assert 101 not in people(conn)
    elif subsystem == "names":
        from leeroy_mind.console.views import names
        assert 101 not in names(conn)
    elif subsystem == "claims":
        from leeroy_mind.claims.extract import recent
        from leeroy_mind.claims.store import live_claims
        h.say(now,101,"I like tea",channel="dm:101",to=1)
        assert recent(conn,101,before=now+1) == [] and live_claims(conn,101,now=now) == []
    elif subsystem == "awareness":
        from leeroy_mind.awareness import card,CardContext
        c = card(conn,101,context=CardContext(channel="guild",now=now))
        assert c.parts == () and c.missing == ("world_excluded",)
    elif subsystem == "guards":
        from leeroy_mind.guards import review,Draft,GuardStores
        from leeroy_mind.guards.config import GuardsCfg
        assert review(GuardStores(conn,GuardsCfg(enabled=True)),Draft(101,"guild","hello",now=now)).codes == ("world_excluded",)
    elif subsystem == "fingerprint":
        from leeroy_mind.fingerprint import fingerprint
        assert not fingerprint(conn,101).available
    elif subsystem == "selfies":
        from leeroy_mind.selfies import render,store
        store.ensure_first_sets(h.store,[101],now=now)
        assert render.todo(h.ctx()) == []
        assert store.counts(conn)["queued"] == 0
    elif subsystem == "realm":
        from leeroy_mind.world.realm import voices
        from leeroy_mind.realm.ledger import former_members_in_unit
        assert voices(h.store,h.settings.data_dir,now) == {}
        with h.store.transaction():
            assert 101 not in former_members_in_unit(h.store,human_guid=1)


def test_older_database_is_cast_and_world_reports_do_not_migrate(h):
    with h.store.transaction():
        h.store.conn.execute("DROP TABLE persona_tier")
        h.store.conn.execute("DROP TABLE world_member")
    before = h.store.conn.total_changes
    assert set(h.store.persona_versions()) == {101,102,103,104,105}
    assert view.cards(h.store.conn)["members"] == []
    assert view.status(h.store.conn)["guilds"] == [] and view.plan(h.store.conn) == []
    assert h.store.conn.total_changes == before


@pytest.mark.parametrize("change",[{"guild_id":21},{"guid":1}])
def test_snapshot_owner_and_own_guild_refused_atomically(h,change):
    original = member(h,102)
    observe(h,original)
    with pytest.raises(ValueError):
        observe(h,replace(member(h),**change))
    assert list(targets.roster(h.store)) == [102]


@pytest.mark.parametrize("online",[True,False])
def test_snapshot_pages_identity_ack_and_disconnect(h,online):
    enabled(h)
    async def scenario():
        app = NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,is_backlog=lambda _:False)
        service = targets.Targets(app)
        async def send(cmd,*,preserve_id=False):
            assert not preserve_id
            await service.event(WorldRoster(0,cmd.request,21,1,0,1,1,(member(h),)))
            return Ack(0,cmd.id,True,"")
        app.bridge = NS(send=send)
        await service.hello(Hello(0,"test","1",21,1 if online else 0))
        assert await service.refresh()
        assert list(targets.roster(h.store)) == [101]
        await service.connection(False)
        assert not await service.refresh()
        assert list(targets.roster(h.store)) == [101]
    run(scenario())


def test_world_realm_requires_optin_correct_guild_and_current_membership(h):
    enabled(h,"realm=true\n")
    observe(h,member(h))
    world(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO realm_guild(id,name,role,faction,status,added) VALUES(50,'Test rivals','drama','horde','active',0)")
    from leeroy_mind.world.realm import voices,allowed
    assert allowed(h.store,h.settings.data_dir,h.T,101,50)
    assert not allowed(h.store,h.settings.data_dir,h.T,101,60)
    card = voices(h.store,h.settings.data_dir,h.T)[101]["card"]
    assert "Character level: 24" in card and "typing" in card
    observe(h)
    assert voices(h.store,h.settings.data_dir,h.T) == {}
    with pytest.raises(ValueError):
        WorldSay("test",101,50,"whisper","hi",1)


def test_world_selfie_queue_is_independent_and_capped(h):
    enabled(h,"selfies=true\n")
    from leeroy_mind.personas.schema import ProfilePhoto
    from leeroy_mind.personas.io import save_persona
    from leeroy_mind.selfies import render,store
    for guid in (101,102):
        p = h.persona(guid,appearance=APPEARANCE)
        save_persona(h.store,replace(p,photo=ProfilePhoto("selfie","head_and_shoulders","a cafe","a blue shirt","a smile")),
                     source="seed",ts=h.T,race="",class_name="")
    observe(h,member(h))
    world(h)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings,world_selfies_per_night=1)
    store.ensure_first_sets(h.store,[101,102],now=h.T)
    assert render.todo(ctx) == ["102:1"]
    scoped = photos.WorldPhotos(ctx)
    assert render.todo(scoped,photos.policy(ctx)) == ["101:1"]
    ctx.skip(photos.IMAGE,"101:1")
    assert render.todo(scoped,photos.policy(ctx)) == []
    enabled(h,"selfies=false\n")
    assert scoped.members() == []


def test_world_photo_fake_worker_renders_only_world_allowance_and_serves_gated_face(h,tmp_path):
    from selfies.fakes import SELFIE_CFG, fake_worker, renderable_cast
    from leeroy_mind.selfies import store
    renderable_cast(h.store,101,102)
    enabled(h,"selfies=true\n")
    observe(h,member(h))
    world(h)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings,world_selfies_per_night=1)
    store.ensure_first_sets(h.store,[101,102],now=h.T)
    run(photos.image_stage(SELFIE_CFG,worker=fake_worker(tmp_path))(ctx))
    assert store.ready_set(h.store,101,1) is not None
    assert store.ready_set(h.store,102,1) is None
    assert view.face(h.store.conn,h.settings.data_dir,101).startswith(b"\xff\xd8\xff")
    assert photos.image_count(ctx) == 0


def test_world_stage_write_failure_is_bounded_and_never_logs_persona(h,monkeypatch,caplog):
    enabled(h)
    observe(h,member(h))
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
    def broken(*a,**kw):
        raise ValueError("PRIVATE_PERSONA_MARKER")
    monkeypatch.setattr(stage,"save_persona",broken)
    with caplog.at_level(logging.INFO):
        for day in range(29,32):
            ctx = gen_context(h,night=f"2026-10-{day}")
            run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
            assert ctx.done(stage.STAGE,"101")
    assert stage.failures(h.store,101)[0] == 3
    assert "PRIVATE_PERSONA_MARKER" not in caplog.text
    assert not h.store.persona_versions(tier="world")


def test_world_cancel_does_not_consume_unit_or_cap(h,monkeypatch):
    enabled(h)
    observe(h,member(h))
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
    async def cancelled(*a,**kw):
        raise asyncio.CancelledError()
    monkeypatch.setattr(stage,"generate_persona",cancelled)
    ctx = gen_context(h)
    with pytest.raises(asyncio.CancelledError):
        run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
    assert not ctx.done(stage.STAGE,"101") and stage.failures(h.store,101)[0] == 0


def test_discord_dm_cannot_target_world_persona(h):
    from leeroy_mind.discord.desk import DiscordDesk, _Refused
    world(h)
    desk = DiscordDesk(NS(store=h.store))
    with pytest.raises(_Refused):
        desk._check_dm("dm:101",1)


def test_world_forum_author_has_own_guild_voice_and_never_cast_membership(h):
    from leeroy_mind.realm.night import _Night
    enabled(h,"realm=true\n")
    observe(h,member(h))
    world(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO realm_guild(id,name,role,faction,status,added) VALUES(50,'Test rivals','drama','horde','active',0)")
    night = _Night(h.ctx())
    author = night._as_actor(night._actors()[101])
    assert author[:3] == (101,"actor","Arkon") and "typing" in author[3]["card"]
    assert "Test rivals" in author[3]["who"]
    enabled(h,"realm=false\n")
    assert 101 not in night._actors()


def test_cli_status_plan_are_read_only_and_name_free(h,capsys):
    from leeroy_mind.__main__ import main
    observe(h,member(h))
    world(h)
    before = h.store.conn.total_changes
    for action in ("status","plan"):
        assert main(["personas","world",action,"--data-dir",str(h.settings.data_dir)]) == 0
        text = capsys.readouterr().out
        assert "Arkon" not in text and "Test rivals" not in text
        assert json.loads(text)["guilds"][0]["generated"] == 1
    assert h.store.conn.total_changes == before


@pytest.mark.parametrize("problem",["looks like a famous singer in a big jumper", "a teenager in a hoodie", "naked at the keyboard", "Belra's cardigan"])
def test_world_looks_screen_refuses_without_private_logs(h,caplog,problem):
    from nightly.test_night_enrich import LOOKS
    enabled(h)
    observe(h,member(h))
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
    ctx = gen_context(h)
    bad = {**ENRICHED,"appearance":{**LOOKS,"at_the_pc":problem}}
    ctx.reasoner.llm = FakeLLM(script_from(["synthetic profile",_persona_json("Nolan Alder"),
                                         "synthetic looks",json.dumps(bad)]))
    with caplog.at_level(logging.INFO):
        run(stage.make_stage(lambda:CastCfg(.7,.7))(ctx))
    assert not h.store.persona_versions(tier="world")
    assert stage.failures(h.store,101)[0] == 1
    assert problem not in caplog.text and "Nolan" not in caplog.text


@pytest.mark.parametrize("refusal",["ack","duplicate","disconnect"])
def test_incomplete_or_refused_roster_does_not_retire_members(h,refusal):
    enabled(h)
    observe(h,member(h))
    before = targets.roster(h.store)
    async def scenario():
        app = NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,is_backlog=lambda _:False)
        service = targets.Targets(app)
        async def send(cmd,*,preserve_id=False):
            assert not preserve_id
            empty = WorldRoster(0,cmd.request,21,1,0,1,0,())
            await service.event(empty)
            if refusal == "duplicate":
                await service.event(empty)
            elif refusal == "disconnect":
                await service.connection(False)
                await service.event(empty)
            return Ack(0,cmd.id,refusal != "ack","")
        app.bridge = NS(send=send)
        await service.hello(Hello(0,"test","1",21,0))
        assert not await service.refresh()
    run(scenario())
    assert targets.roster(h.store) == before


def test_roster_refresh_preserves_generation_identity_but_rejects_account_or_guild_change(h):
    observe(h,member(h))
    before = targets.roster(h.store)[101]
    assert targets.same_member(before,{**before,"observed":h.T+300,"online":0,"zone":33})
    for field,value in (("account",99),("guild_id",60),("level",25),("retired",1)):
        assert not targets.same_member(before,{**before,field:value})


def test_world_observation_never_records_activity_and_images_follow_cast(h):
    from leeroy_mind.app import MindApp
    from leeroy_mind.world.attach import attach_world
    from leeroy_mind.nightly.runner import NightRunner
    from selfies.fakes import SELFIE_CFG
    runner = NS(stages=tuple(NS(name=n) for n in ("regress","selfies","snaps")))
    runner.add_stage = lambda *a,**kw: NightRunner.add_stage(runner,*a,**kw)
    registered = []
    app = NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,skip_event_rows=set(),
             is_backlog=lambda _:False,night_runner=runner,cfg=NS(cast=CastCfg(.7,.7)),
             selfies=NS(cfg=replace(SELFIE_CFG,snaps=True)),
             register_connection_hook=lambda fn:registered.append(fn),
             register_event_handler=lambda *args:registered.append(args),
             register_background=lambda fn:registered.append(fn))
    attach_world(app)
    assert not registered and [s.name for s in runner.stages] == ["regress","selfies","snaps"]
    event = WorldRoster(0,"test",21,1,0,1,0,())
    before = h.store.conn.total_changes
    run(MindApp._row(app,event,"world_roster"))
    MindApp._record_only(app,event,"world_roster")
    assert h.store.conn.total_changes == before
    enabled(h,"selfies=true\n")
    attach_world(app)
    assert [s.name for s in runner.stages] == ["regress","world_targets","world_personas",
        "world_photos","selfies","snaps","world_selfies"]


def test_world_uses_diversity_sampler_without_enrichment_when_installed(h,monkeypatch):
    observed = []
    def sample(p,existing,*,ident,names):
        observed.append((p.guid,tuple(existing),ident,tuple(names)))
        return APPEARANCE
    monkeypatch.setattr(stage,"_looks_sampler",lambda:sample)
    observe(h,member(h))
    ctx = h.ctx()
    p = load_persona(h.store,101)
    result = run(stage.looks(ctx,p,targets.roster(h.store),[],("Arkon","Belra")))
    assert result.appearance == APPEARANCE and ctx.reasoner.calls == 0
    assert observed == [(101,(),ctx.ident,("Arkon","Belra"))]


def test_world_empty_database_is_read_only_and_has_no_targets():
    conn = sqlite3.connect(":memory:")
    try:
        assert view.status(conn) == {"guilds":[],"observed":None}
        assert view.plan(conn) == [] and view.cards(conn)["members"] == []
        assert not conn.execute("SELECT name FROM sqlite_master").fetchall()
    finally:
        conn.close()


def test_roster_correlates_real_bridge_wire_id_while_live_model_is_away(h):
    from dataclasses import asdict
    from leeroy_mind.protocol import Hello
    from leeroy_mind.world.protocol import register
    from test_bridge import session
    register()
    enabled(h)
    async def scenario():
        app = NS(data_dir=h.settings.data_dir,store=h.store,clock=h.clock,
                 is_backlog=lambda _:False,_model_ready=False)
        service = targets.Targets(app)
        async def event(client,ev):
            if isinstance(ev,WorldRoster):
                await service.event(ev)
        async with session(handler=event) as s:
            app.bridge = s.client
            original = s.bridge._command
            async def command(writer,cmd):
                if cmd["type"] != "get_world_roster":
                    return await original(writer,cmd)
                row = asdict(member(h))
                row["class"] = row.pop("class_")
                await s.bridge._write(writer,{"type":"world_roster","request":cmd["request"],
                    "own_guild":21,"human":1,"page":0,"pages":1,"total":1,"members":[row]})
                await s.bridge._write(writer,{"type":"ack","id":cmd["id"],"ok":True,"error":""})
            s.bridge._command = command
            await service.hello(Hello(0,"test","1",21,0))
            assert await asyncio.wait_for(service.refresh(),2)
            assert list(targets.roster(h.store)) == [101]
            assert not s.client._preserved_ids
    run(scenario())


def test_existing_forum_actor_keeps_card_admission_when_world_voice_is_off(h):
    from leeroy_mind.realm.night import _Night
    enabled(h)
    observe(h,member(h))
    world(h)
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM cast_member WHERE guid=101")
        h.store.conn.execute("INSERT INTO realm_guild(id,name,role,faction,status,added) VALUES(50,'Test rivals','drama','horde','active',0)")
        h.store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,status,added) VALUES(101,50,'Arkon','officer','active',0)")
    night = _Night(h.ctx())
    author = night._as_actor(night._actors()[101])
    assert author[:3] == (101,"actor","Arkon") and "card" not in author[3]
    assert "Test rivals" in author[3]["who"]
    enabled(h,"realm=true\n")
    assert night._as_actor(night._actors()[101]) == author
