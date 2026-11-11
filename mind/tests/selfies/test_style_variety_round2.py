"""Review probes use the real worker fitter, synthetic personas and fake models."""
from dataclasses import replace
import json
import re
import pytest
from conftest import run
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import kind, photo_extra
from leeroy_mind.selfies.prompt import build_prompt
from leeroy_mind.selfies.style import LUST, PLAIN
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_kind import KIND
from nightly.conftest import IDENT
from selfies.test_selfie_prompt import _worker_fit


@pytest.mark.parametrize("style",[LUST,PLAIN])
def test_lighting_fits_real_75_cap_without_promoting_other_phrases(h,style):
    looks=replace(APPEARANCE,build="curvy",face=("small freckles across the cheeks",
        "thin round glasses","a soft oval face","a dimple on one cheek"))
    p=h.woman(101,"lust" if style==LUST else "fond",appearance=looks)
    photo=ProfilePhoto("snapshot","three_quarter","a city street at night",
        "a red satin midi dress","a confident smirk") if style==LUST else ProfilePhoto(
        "headshot","upper_body","in a quiet sunlit garden beside a wooden fence and flowering apple trees",
        "a loose blue cotton shirt with a long green jacket and comfortable trousers","a smile")
    p=replace(p,photo=photo)
    light="neon light" if style==LUST else "golden hour"
    base=build_prompt(p,kind_style=style)
    lit=build_prompt(p,kind_style=style,light=light)
    assert (light,9) in lit.phrases
    priorities=dict(base.phrases)
    assert all(priorities[t]==pr for t,pr in lit.phrases if t in priorities)
    # The reviewer's CLIP pre-tokenization probe, with the production fitter and cap.
    # No model or image worker is loaded; BPE/tokenizer integration belongs to the worker.
    count=lambda text:len(re.findall(r"[a-z]+|[0-9]|[^\sa-z0-9]+",text.casefold()))
    kept,dropped=_worker_fit()(lit.phrases,count,75)
    assert count(kept)<=75 and light in dropped
    assert all(t in kept for t,pr in base.phrases if pr==0)


@pytest.mark.parametrize("receipt",[False,True])
def test_unrecorded_and_lust_vocabulary_never_feed_a_plain_man(h,receipt):
    p=replace(h.woman(102,"fond",appearance=APPEARANCE),photo=ProfilePhoto(**{
        **KIND,"outfit":"a low-cut red cocktail dress","setting":"a nightclub"}))
    save_persona(h.store,p,source="owner" if not receipt else "generated",ts=h.T,race="",class_name="")
    if receipt:
        kind._record(h.store,102,PLAIN,p.photo)  # defence even against a mislabelled old record
    photo_extra.seed(h.store,{102:p},h.T)
    photo_extra.record(h.store,p,None,h.T)
    if not receipt:
        assert {r[0] for r in h.store.conn.execute("SELECT style FROM photo_style_history")}=={"unknown"}
    h.persona(101,appearance=APPEARANCE)
    ctx=h.ctx({"photo":[KIND]})
    run(kind.stage_photo(ctx,variety=True))
    prompt=json.dumps(ctx.reasoner.llm.calls)
    assert "low-cut" not in prompt and "cocktail dress" not in prompt and "nightclub" not in prompt


@pytest.mark.parametrize("photo_kind",kind._PROPS["kind"]["enum"])
@pytest.mark.parametrize("framing",kind._PROPS["framing"]["enum"])
def test_plain_prompt_choices_still_accepted_with_variety(h,photo_kind,framing):
    h.persona(101,appearance=APPEARANCE)
    data={**KIND,"kind":photo_kind,"framing":framing,
          "outfit":"a loose shirt and leggings","setting":"in a living room"}
    assert not kind.screen_kind(load_persona(h.store,101),data,ident=IDENT,member_names=())
    run(kind.stage_photo(h.ctx({"photo":[data]}),variety=True))
    assert load_persona(h.store,101).photo == ProfilePhoto(**data)


@pytest.mark.parametrize("setting,allowed",[
    ("on a quiet tree-lined city street at night",{1,3,4,5}),
    ("in a sunny garden beside the old wooden fence",{0,4}),
    ("inside a cosy restaurant after dark",{1,2}),
    ("inside a brightly sunlit office",set()),
    ("somewhere unfamiliar",set()),
])
def test_light_matches_place_and_time_categories(h,setting,allowed):
    p=replace(h.persona(101,appearance=APPEARANCE),photo=ProfilePhoto(**{**KIND,"setting":setting}))
    matches={i for i in range(len(photo_extra.LIGHTS)) if photo_extra.fits(i,p.photo)}
    assert matches==allowed
