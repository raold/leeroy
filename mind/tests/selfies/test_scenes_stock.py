from dataclasses import replace
from conftest import run
from nightly.conftest import h
from nightly.test_night_scenes import cast, SNAPS_CFG, SCENE_REPLY
from leeroy_mind.selfies.scenes import make_scenes_stage
from leeroy_mind.selfies.snaps import planned


def test_night_stocks_both_outlets_without_changing_member_or_night_caps(h):
    cast(h,101,102,103,104)
    ctx=h.ctx({'scene':[SCENE_REPLY]*4})
    run(make_scenes_stage(SNAPS_CFG,dial=lambda:1,outlets=lambda:True)(ctx))
    rows=planned(h.store,20)
    assert len(rows)==4 and sum(r.audience=='him' for r in rows)==2
    assert sum(r.audience=='anyone' for r in rows)==2
    assert all(r.scene['audience']==r.audience for r in rows)
    assert max(sum(r.guid==g for r in rows) for g in (101,102,103,104))<=2
    assert ctx.reasoner.calls==4
    # The second run cannot repeat either outlet's calls.
    run(make_scenes_stage(SNAPS_CFG,dial=lambda:1,outlets=lambda:True)(ctx))
    assert ctx.reasoner.calls==4 and len(planned(h.store,20))==4


def test_night_cap_counts_declined_calls_across_both_outlets(h):
    cast(h,101,102,103,104)
    ctx=h.ctx({'scene':[{**SCENE_REPLY,'photo':False}]*8})
    ctx.settings=replace(ctx.settings,scenes_per_night=2)
    run(make_scenes_stage(SNAPS_CFG,dial=lambda:10,outlets=lambda:True)(ctx))
    assert ctx.reasoner.calls==2 and not planned(h.store,20)


def test_dial_zero_never_plans_either_outlet(h):
    cast(h,101,102)
    ctx=h.ctx({'scene':[SCENE_REPLY]*4})
    run(make_scenes_stage(SNAPS_CFG,dial=lambda:0,outlets=lambda:True)(ctx))
    assert ctx.reasoner.calls==0 and not planned(h.store,20)
