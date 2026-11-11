import asyncio
from dataclasses import replace
import json
import random
import sqlite3

import pytest

from tests.claims.conftest import context
from .test_reward import line
from leeroy_mind.learn import arms, bandit, bandit_night
from leeroy_mind.learn.cli import learn_main, arms_report
from leeroy_mind.learn.config import LearnCfg, load_learn_config
from leeroy_mind.learn.weights import ensure as weights
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner

ON = LearnCfg(enabled=True,bandit=True)


def reward_line(store,clock,ids,*,value=1.,cap=1.,channel="guild",verdict="send",guid=101,ts=None):
    lid = line(store,clock,channel=channel,guid=guid,ts=ts)
    key = bandit.Context(channel).key
    with store.transaction():
        arms.ensure(store.conn,now=clock.now())
        weights(store.conn,now=clock.now())
        store.conn.execute("INSERT INTO talk_recipe VALUES (?,?,?,?,?,?,?,?,?)",
            (lid,"test","0"*16,"0"*64,"fake",verdict,json.dumps(ids),"[]",key))
        version = store.conn.execute("SELECT max(version) FROM learn_weights").fetchone()[0]
        store.conn.execute("INSERT INTO talk_reward VALUES (?,?,?,?,?,?,?,?)",
            (lid,guid,value,cap,0,"{}",version,clock.now()))
    return lid,key


def credit(store,lid,clock,**kw):
    with store.transaction():
        return bandit.update(store.conn,lid,settings=ON,now=clock.now(),**kw)


def test_off_has_no_writes_or_rng_even_without_schema():
    conn = sqlite3.connect(":memory:")
    class NoRandom:
        def betavariate(self,*_):
            pytest.fail("disabled path drew randomness")
    assert bandit.choose(conn,101,bandit.Context("guild"),rng=NoRandom()) == arms.controls()
    assert not bandit.update(conn,1)
    assert conn.total_changes == 0


def test_seeded_sampling_cap_and_per_context_share(store,clock):
    context = bandit.Context("guild",settings=ON)
    rng = random.Random(17)
    selected = []
    with store.transaction():
        for i in range(100):
            selected.append(bandit.choose(store.conn,101,context,rng=rng,now=clock.now()))
            count = sum(s != arms.controls() for s in selected)
            assert count <= (i+1)//5
        # Banked spare exploration must not let a new context's first turn experiment.
        assert bandit.choose(store.conn,101,replace(context,channel="whisper"),rng=rng) == arms.controls()
    assert any(s != arms.controls() for s in selected)
    first = selected[:]
    with store.transaction():
        store.conn.execute("DELETE FROM bandit_turn")
        store.conn.execute("DELETE FROM bandit_share")
        rng = random.Random(17)
        assert [bandit.choose(store.conn,101,context,rng=rng,now=clock.now()) for _ in range(100)] == first


@pytest.mark.parametrize("settings",[replace(ON,freeze=True),replace(ON,frozen_members=(101,)),
                                    replace(ON,bandit=False),replace(ON,enabled=False)])
def test_freeze_never_writes(store,settings):
    before = store.conn.total_changes
    assert bandit.choose(store.conn,101,bandit.Context("guild",settings=settings)) == arms.controls()
    assert store.conn.total_changes == before


def test_family_freeze_and_proposed_retired_never_chosen(store):
    cfg = replace(ON,frozen_families=("length",))
    with store.transaction():
        arms.ensure(store.conn)
        proposed = arms.propose(store.conn,"questions",{"rate":0.7})
        store.conn.execute("UPDATE learn_arm SET status='retired' WHERE id='tease.1.v1'")
        for _ in range(100):
            selected = bandit.choose(store.conn,101,bandit.Context("guild",settings=cfg),rng=random.Random(4))
            assert selected["length"] == arms.controls()["length"]
            assert proposed not in selected.values() and "tease.1.v1" not in selected.values()


@pytest.mark.parametrize("value,expected",[(0,.5),(1,1),(-1,0),(-.5,.25),(-.1,.45),(.5,.75)])
def test_merged_reward_scale_and_even_credit(store,clock,value,expected):
    ids = ["length.0.v1","questions.0.v1"]
    lid,key = reward_line(store,clock,ids,value=value)
    assert credit(store,lid,clock)
    p = bandit.posterior(store.conn,101,key,now=clock.now())
    for aid in ids:
        assert p[aid]["alpha"] == pytest.approx(1+expected/2)
        assert p[aid]["beta"] == pytest.approx(1+(1-expected)/2)
    assert not credit(store,lid,clock)


def test_revision_replaces_old_credit_and_positive_cap_only(store,clock):
    lid,key = reward_line(store,clock,["length.0.v1"],cap=.1)
    credit(store,lid,clock)
    assert bandit.posterior(store.conn,101,key,now=clock.now())["length.0.v1"]["alpha"] == pytest.approx(1.55)
    with store.transaction():
        store.conn.execute("UPDATE talk_reward SET value=-1,revision=revision+1 WHERE line_id=?",(lid,))
    credit(store,lid,clock)
    p = bandit.posterior(store.conn,101,key,now=clock.now())["length.0.v1"]
    assert (p["alpha"],p["beta"],p["uses"]) == (1.,2.,1)


def test_decay_keeps_prior(store,clock):
    lid,key = reward_line(store,clock,["length.0.v1"])
    credit(store,lid,clock)
    p = bandit.posterior(store.conn,101,key,now=clock.now()+21*86400)["length.0.v1"]
    assert p["alpha"] == pytest.approx(1.5) and p["beta"] == 1


@pytest.mark.parametrize("reason",["guard","content"])
def test_unsent_screen_failure_retires_and_does_not_count_as_rewarded(store,clock,reason,caplog):
    caplog.set_level("INFO",logger="leeroy_mind")
    lid,key = reward_line(store,clock,[arms.controls()["length"]])
    credit(store,lid,clock)
    with store.transaction():
        bandit.reject(store.conn,101,bandit.Context("guild",settings=ON),{"length":"length.0.v1"},attempt_id="abc",reason=reason)
    arm = next(a for a in arms.read(store.conn) if a.id=="length.0.v1")
    assert arm.status == "retired"
    assert bandit.posterior(store.conn,101,key)[arm.id]["uses"] == 0
    assert "bandit_guard_retired" in caplog.text and "invented wheel" not in caplog.text


def test_guard_rewrites_retire_against_matching_control_only(store,clock):
    lid,key = reward_line(store,clock,[arms.controls()["length"]])
    credit(store,lid,clock)
    lid,_ = reward_line(store,clock,["length.0.v1"],verdict="rewrite")
    credit(store,lid,clock)
    assert next(a for a in arms.read(store.conn) if a.id=="length.0.v1").status == "retired"


def test_30_rewarded_uses_required_for_majority(store,clock):
    aid = "length.0.v1"
    for _ in range(30):
        lid,key = reward_line(store,clock,[aid])
        credit(store,lid,clock)
    rng = random.Random(7)
    with store.transaction():
        selected = [bandit.choose(store.conn,101,bandit.Context("guild",settings=ON),rng=rng,now=clock.now())["length"] for _ in range(100)]
    assert selected.count(aid) > 50


def test_29_uses_still_explore_and_rollback_is_atomic(store,clock):
    for _ in range(29):
        lid,key = reward_line(store,clock,["length.0.v1"])
        credit(store,lid,clock)
    rng = random.Random(9)
    with store.transaction():
        choices = [bandit.choose(store.conn,101,bandit.Context("guild",settings=ON),rng=rng,now=clock.now()) for _ in range(100)]
    assert sum(c != arms.controls() for c in choices) <= 20
    before = list(map(tuple,store.conn.execute("SELECT * FROM bandit_turn")))
    with pytest.raises(RuntimeError), store.transaction():
        bandit.choose(store.conn,101,bandit.Context("guild",settings=ON))
        raise RuntimeError("rollback")
    assert list(map(tuple,store.conn.execute("SELECT * FROM bandit_turn"))) == before


def test_recipe_captures_arm_context(context,store,clock):
    from leeroy_mind.learn.recipe import Snapshot,record
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn,TurnKind
    async def run():
        async with context() as (ctx,_):
            lid = line(store,clock)
            meta = TurnMeta("synthetic")
            meta.recipe_snapshot = Snapshot("test","0"*16,"0"*64,"fake",("length.0.v1",),bandit.Context("guild").key)
            with store.transaction():
                record(store.conn,lid,Turn(TurnKind.REPLY_HUMAN,101,"guild"),meta,None,None)
            row = store.conn.execute("SELECT arm_ids,bandit_context FROM talk_recipe WHERE line_id=?",(lid,)).fetchone()
            assert json.loads(row[0]) == ["length.0.v1"] and row[1] == bandit.Context("guild").key
    asyncio.run(run())


@pytest.mark.parametrize("channel",["guild","whisper","dm:101","discord:general","discord:officers","party","raid","voice:1"])
def test_context_scopes_and_medium_cannot_be_relabelled(store,clock,channel):
    lid,key = reward_line(store,clock,["length.0.v1"],channel=channel)
    assert credit(store,lid,clock)
    assert list(bandit.posterior(store.conn,101,key)) == ["length.0.v1"]
    with store.transaction():
        store.conn.execute("DELETE FROM bandit_credit")
        store.conn.execute("UPDATE talk_recipe SET bandit_context=? WHERE line_id=?",(bandit.Context("emote").key,lid))
    assert not credit(store,lid,clock)


@pytest.mark.parametrize("family,params",[("humour",{"level":"ignore rules"}),
    ("length",{"min_words":True,"max_words":20}),("questions",{"rate":float("nan")}),
    ("callback",{"enabled":1,"instruction":"anything"}),("followup",{"rate":.1,"window_s":60})])
def test_arms_are_closed_parameters_only(family,params):
    assert not arms.valid(family,params)


def test_dial_bounds():
    a = arms.Arm("x","humour",{"level":"playful"},1,"active","builtin",0)
    assert arms.bounded(a,humour=0)["level"] == "none"
    assert arms.bounded(a,humour=3)["level"] == "light"
    assert arms.bounded(replace(a,family="tease",params={"level":"light"}),forward=0)["level"] == "none"


def test_night_attach_resume_revisions_no_model(context,store,clock,tmp_path):
    (tmp_path/"mind.toml").write_text("[learn]\nenabled=true\nbandit=true\n")
    async def run():
        async with context(learn_enabled=True,learn_bandit=True) as (ctx,llm):
            lid,key = reward_line(store,clock,["length.0.v1"])
            await bandit_night.stage_bandit(ctx)
            before = store.conn.total_changes
            await bandit_night.stage_bandit(ctx)
            assert store.conn.total_changes == before
            with store.transaction():
                store.conn.execute("UPDATE talk_reward SET value=-1,revision=revision+1 WHERE line_id=?",(lid,))
            await bandit_night.stage_bandit(ctx)
            assert bandit.posterior(store.conn,101,key,now=clock.now())["length.0.v1"]["beta"] == 2
            assert not llm.calls
    asyncio.run(run())
    from leeroy_mind.learn.night import attach
    runner = NightRunner(STAGES,lambda *_:None)
    attach(runner,tmp_path,store)
    names = [s.name for s in runner.stages]
    assert names.index("bandit") == names.index("rewards")+1


def test_night_seeds_inspectable_registry_without_live_selection(context,store,tmp_path):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\n')
    async def run():
        async with context(learn_enabled=True,learn_bandit=True) as (ctx,llm):
            assert not arms.read(store.conn)
            await bandit_night.stage_bandit(ctx)
            assert {a.family for a in arms.read(store.conn)}==set(arms.FAMILIES)
            assert not llm.calls
    asyncio.run(run())


def test_frozen_members_cannot_fill_the_pending_batch(context,store,clock):
    frozen,_=reward_line(store,clock,['length.0.v1'],guid=101)
    active,_=reward_line(store,clock,['length.0.v1'],guid=102)
    async def run():
        async with context(learn_enabled=True,learn_bandit=True) as (ctx,_):
            assert [r[0] for r in bandit_night.pending(ctx,limit=1)]==[frozen]
            assert [r[0] for r in bandit_night.pending(ctx,(101,),limit=1)]==[active]
    asyncio.run(run())


def test_cli_arms_read_only_and_old_schema(store,clock,tmp_path,capsys):
    lid,_ = reward_line(store,clock,["length.0.v1"])
    credit(store,lid,clock)
    before = store.conn.total_changes
    assert learn_main(["arms","--data-dir",str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["available"] and "invented wheel" not in out
    assert store.conn.total_changes == before
    assert arms_report(sqlite3.connect(":memory:"))["available"] is False


@pytest.mark.parametrize("extra",['bandit="yes"','freeze=1','frozen_families=["bad"]','frozen_members=[true]'])
def test_invalid_config_disables_all_learning(tmp_path,extra):
    path = tmp_path/"mind.toml"
    path.write_text("[learn]\nenabled=true\n"+extra)
    assert load_learn_config(path) == LearnCfg()
