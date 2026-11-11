"""Invented TEST_CAST members only; no models, real photos or live writes."""
from collections import Counter
from dataclasses import asdict, replace
import json
import logging

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.config import ConfigError
from leeroy_mind.nightly.enrich import LIKENESS_RE
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import cli, files, kind, photo_extra, restyle, style_variety as sv
from leeroy_mind.selfies.config import load_selfie_config
from leeroy_mind.selfies.prompt import build_prompt
from leeroy_mind.selfies.store import current, pick, queued_sets
from leeroy_mind.selfies.style import LUST, PLAIN, style_of
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_kind import KIND
from nightly.test_night_photo_lust import TODAY_KIND_PROMPT, LUST_REPLY
from selfies.fakes import ready


def population(h, lust=True):
    make = (lambda g:h.woman(g,"lust",appearance=APPEARANCE)) if lust else (
        lambda g:h.persona(g,appearance=APPEARANCE))
    return {g:make(g) for g in TEST_CAST}


@pytest.mark.parametrize("lust",[True])
@pytest.mark.parametrize("seed",[0,1,2,17,88,100])
def test_caps_spread_determinism_and_screens(h,lust,seed):
    people = population(h,lust)
    result = sv.assign(people,people,seed=seed,ident=IDENT)
    assert result == sv.assign(dict(reversed(list(people.items()))),reversed(people),seed=seed,ident=IDENT)
    chosen,rejected,cap,over = result
    assert len(chosen) == 5 and not rejected and cap == 1 and not any(over.values())
    assert max(Counter(x.photo.outfit for x in chosen.values()).values()) <= 2
    assert max(Counter(x.photo.setting for x in chosen.values()).values()) <= cap
    assert len({x.codes["colour"] for x in chosen.values()}) == 5
    assert len({x.codes["outfit"] for x in chosen.values()}) == (5 if lust else 4)
    assert set(x.photo.kind for x in chosen.values()) == set(sv.KINDS)
    frames = Counter(x.photo.framing for x in chosen.values())
    assert len(frames) == (2 if lust else 4) and max(frames.values())-min(frames.values()) <= 1
    for g,choice in chosen.items():
        assert sv._safe(people[g],choice.photo,None if not lust else dict(
            kind=choice.photo.kind,framing=choice.photo.framing,setting=choice.photo.setting,
            outfit=sv.DATE_OUTFITS[choice.codes["outfit"]],colour=kind.LUST_COLOURS[choice.codes["colour"]],
            expression=choice.photo.expression),IDENT,())


def test_every_date_outfit_colour_and_setting_keeps_lust_pairings(h):
    p = h.woman(101,"lust",appearance=APPEARANCE)
    assert len(sv.DATE_SETTINGS) >= 8
    assert not set(sv.DATE_OUTFITS) & (kind.SWIM_OUTFITS | kind.GYM_OUTFITS)
    for outfit in sv.DATE_OUTFITS:
        for colour in kind.LUST_COLOURS:
            for setting in sv.DATE_SETTINGS:
                assert not kind.screen_lust(p,{**LUST_REPLY,"outfit":outfit,"colour":colour,"setting":setting})


def test_existing_fixed_choices_count_toward_caps(h):
    people = population(h)
    first = sv.assign(people,[101],ident=IDENT)[0][101].photo
    people[101] = replace(people[101],photo=first)
    people[102] = replace(people[102],photo=first)
    chosen,_,_,over = sv.assign(people,[103,104,105],ident=IDENT)
    assert all(x.photo.outfit != first.outfit and x.photo.setting != first.setting for x in chosen.values())
    assert over == {"outfit_pairs":0,"settings":1}  # pre-existing protected conflict is reported
    assert sv.key("A fitted little black dress") == sv.key("fitted little black dress")


def test_exhausted_capacity_and_age_are_refused_without_text(h,monkeypatch):
    people = population(h)
    monkeypatch.setattr(sv,"DATE_SETTINGS",())
    assert set(sv.assign(people,people,ident=IDENT)[1]) == set(people)
    monkeypatch.undo()
    people[101] = replace(people[101],identity=replace(people[101].identity,age=24))
    assert sv.assign(people,[101],ident=IDENT)[1] == [101]


@pytest.mark.parametrize("light",photo_extra.LIGHTS)
def test_light_safety_priority_and_unchanged_baseline(h,light):
    p = replace(h.persona(101,appearance=APPEARANCE),photo=ProfilePhoto(**KIND))
    for rx in (kind.AGE_RE,kind.ERA_RE,kind.EXPLICIT_RE,kind.WEAR_RE,LIKENESS_RE):
        assert not rx.search(light)
    base = build_prompt(p)
    assert build_prompt(p,light=light) == base  # unknown free-text setting: no light
    setting = sorted(photo_extra.LIGHT_SETTINGS[photo_extra.LIGHTS.index(light)])[0]
    p = replace(p,photo=replace(p.photo,setting=setting))
    changed = build_prompt(p,light=light)
    assert changed.phrases[-1] == (light,9)
    assert changed.negative == base.negative
    assert build_prompt(p,light="arbitrary untrusted text") == build_prompt(p)
    assert kind.KIND_PROMPT == TODAY_KIND_PROMPT


def test_light_invalidates_on_hand_edit_and_is_name_screened(h):
    p = replace(h.persona(101,appearance=APPEARANCE),photo=ProfilePhoto(**{**KIND,"setting":"a garden party"}))
    with h.store.transaction():
        photo_extra.record(h.store,p,0,h.T)
    assert photo_extra.light_for(h.store,p) == photo_extra.LIGHTS[0]
    assert not photo_extra.light_for(h.store,replace(p,photo=replace(p.photo,outfit="a blue jumper")))
    p = replace(p,character_name="Golden",identity=replace(p.identity,real_name="Neon"))
    for seed in range(50):
        assert photo_extra.choose_light(p,seed,ident=IDENT) not in (0,1)


@pytest.mark.parametrize("text,expected",[("",False),("[selfies]\nvariety=true",True),
    ("[selfies]\nvariety=false",False),("[selfies]\nvariety=true\noops=true",False)])
def test_switch_default_and_unknown_off(tmp_path,text,expected):
    path = tmp_path / "mind.toml"
    path.write_text(text)
    assert load_selfie_config(path).variety is expected


@pytest.mark.parametrize("text",['[selfies]\nvariety="yes"','selfies=true','[selfies'])
def test_invalid_switch_is_refused(tmp_path,text):
    path = tmp_path / "mind.toml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_selfie_config(path)


def prepared(h):
    people = population(h)
    for g,p in people.items():
        p = replace(p,photo=ProfilePhoto(**KIND))
        save_persona(h.store,p,source="seed",ts=h.T,race="Human",class_name="Warrior")
        kind._record(h.store,g,LUST,p.photo)
    # An owner edit stays protected even if a later update changes source to generated.
    p = replace(load_persona(h.store,101),photo=replace(ProfilePhoto(**KIND),outfit="a blue coat"))
    save_persona(h.store,p,source="owner",ts=h.T,race="Human",class_name="Warrior")
    save_persona(h.store,p,source="generated",ts=h.T,race="Human",class_name="Warrior")
    root = h.settings.data_dir
    (root / "mind.toml").write_text("")
    ready(h.store,root,102,1,now=h.T)
    pick(h.store,102,1,0,source="cli",now=h.T)
    files.sync_chosen(root,102,current(h.store,102))
    return root


def test_restyle_cli_dry_then_go_preserves_owner_gallery_and_cover(h,capsys,caplog):
    root = prepared(h)
    versions = h.store.persona_versions()
    before = {p:p.read_bytes() for p in (root/"selfies").rglob("*.jpg")}
    cover = current(h.store,102)
    with caplog.at_level(logging.DEBUG):
        assert cli.selfies_main(["restyle","--data-dir",str(root),"--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["owner_protected"] == [101] and len(plan["selected"]) == 4
    assert h.store.persona_versions() == versions
    assert h.count("photo_extra") == h.count("photo_style_history") == 0
    assert not queued_sets(h.store)
    assert cli.selfies_main(["restyle","--data-dir",str(root),"--go"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert [x["choices"] for x in done["selected"]] == [x["choices"] for x in plan["selected"]]
    assert h.store.persona_versions()[101] == versions[101]
    assert h.count("photo_extra") <= 4 and h.count("photo_style_history") == 9
    assert set(s.guid for s in queued_sets(h.store)) == {102,103,104,105}
    assert current(h.store,102) == cover
    assert before and all(p.read_bytes() == data for p,data in before.items())
    for g in (102,103,104,105):
        p = load_persona(h.store,g)
        assert p.appearance == APPEARANCE and kind.chosen_style(h.store,p) == LUST
        assert photo_extra.light_for(h.store,p) or not any(photo_extra.fits(i,p.photo) for i in range(6))
    text = caplog.text + json.dumps(plan) + json.dumps(done)
    assert all(p.photo.outfit not in text and p.character_name not in text
               for p in [load_persona(h.store,g) for g in TEST_CAST])


def test_include_owner_is_explicit_and_lust_only_preserves_plain(h):
    prepared(h)
    plain = h.persona(105,appearance=APPEARANCE)
    before = h.store.persona_versions()
    result = restyle.run(h.store,IDENT,h.clock,lust_only=True,include_owner=True,go=True)
    assert {r["guid"] for r in result["selected"]} == {101,102,103,104}
    assert h.store.persona_versions()[105] == before[105]
    assert load_persona(h.store,105) == plain


def test_running_mind_refuses_both_modes(h,capsys):
    root = prepared(h)
    versions = h.store.persona_versions()
    with acquire_instance_lock(root):
        for mode in ("--dry-run","--go"):
            assert cli.selfies_main(["restyle","--data-dir",str(root),mode]) == 1
            assert "mind must be stopped" in capsys.readouterr().err
    assert h.store.persona_versions() == versions


@pytest.mark.parametrize("enabled",[False,True])
def test_night_plain_history_is_recent_quoted_user_content_only(h,enabled):
    p = h.persona(101,appearance=APPEARANCE)
    prior = replace(p,photo=ProfilePhoto(**KIND))
    with h.store.transaction():
        photo_extra.record(h.store,prior,None,h.T)
        h.store.conn.execute("INSERT INTO photo_style_history(guid,created,outfit,setting) VALUES(?,?,?,?)",
            (102,h.T-photo_extra.MONTH-1,"expired invented outfit","expired setting"))
        h.store.conn.execute("INSERT INTO photo_style_history(guid,created,outfit,setting) VALUES(?,?,?,?)",
            (103,h.T+1,"future invented outfit","future setting"))
    (h.settings.data_dir/"mind.toml").write_text(f"[selfies]\nvariety={str(enabled).lower()}")
    ctx = h.ctx({"photo":[KIND]})
    run(kind.stage_photo(ctx,variety=enabled))
    messages = ctx.reasoner.llm.calls[0]["messages"]
    system = messages[0]["content"]
    user = messages[-1]["content"]
    assert ("quoted observations" in user) is enabled
    assert "quoted observations" not in system and "expired invented" not in user and "future invented" not in user
    assert kind.KIND_PROMPT == TODAY_KIND_PROMPT
    assert h.count("photo_style_history") == 3
    assert bool(h.store.conn.execute("SELECT 1 FROM photo_style_history WHERE created<?",
        (h.T-photo_extra.MONTH,)).fetchone()) is (not enabled)
    assert not photo_extra.light_for(h.store,load_persona(h.store,101))  # no known setting/light pair


def test_night_lust_balances_without_calling_model_and_off_stays_original(h):
    population(h)
    (h.settings.data_dir/"mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx()
    run(kind.stage_photo(ctx,variety=True))
    assert ctx.reasoner.calls == 0
    photos = [load_persona(h.store,g).photo for g in TEST_CAST]
    assert all(photos) and len({p.setting for p in photos}) == 5
    assert h.count("photo_style_history") == 5


def test_enabled_night_plain_pass_preserves_original_age_selection(h):
    p = h.persona(101,appearance=APPEARANCE)
    save_persona(h.store,replace(p,identity=replace(p.identity,age=24)),source="seed",
                 ts=h.T,race="Human",class_name="Warrior")
    (h.settings.data_dir/"mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx({"photo":[KIND]})
    run(kind.stage_photo(ctx,variety=True))
    assert ctx.reasoner.calls == 1 and load_persona(h.store,101).photo == ProfilePhoto(**KIND)


def test_unreadable_member_does_not_block_other_nightly_choices(h):
    population(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE persona_core SET json='{' WHERE guid=105")
    (h.settings.data_dir/"mind.toml").write_text("[selfies]\nvariety=true")
    ctx = h.ctx()
    run(kind.stage_photo(ctx,variety=True))
    assert ctx.reasoner.calls == 0
    assert all(load_persona(h.store,g).photo for g in (101,102,103,104))
    assert h.count("photo_style_history") == 4


def test_unreadable_member_does_not_block_manual_restyle(h):
    prepared(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE persona_core SET json='{' WHERE guid=105")
    result = restyle.run(h.store,IDENT,h.clock,go=True)
    assert {r["guid"] for r in result["selected"]} == {102,103,104}
    assert {"guid":105,"code":"persona_unreadable"} in result["skipped"]
    assert h.store.get_persona_json(105) == "{"


@pytest.mark.parametrize("seed",range(10))
def test_name_matching_a_kind_or_frame_uses_safe_alternative(h,seed):
    p = replace(h.woman(101,"lust",appearance=APPEARANCE),character_name="Snapshot")
    chosen,rejected,_,_ = sv.assign({101:p},[101],seed=seed,ident=IDENT,names=("Face",))
    assert not rejected
    assert chosen[101].photo.kind != "snapshot" and chosen[101].photo.framing != "face"


def test_restyled_queue_renders_with_saved_light_through_fake_worker(h,tmp_path):
    from selfies.fakes import fake_worker, SELFIE_CFG
    from leeroy_mind.selfies.stage import make_stage
    root = prepared(h)
    restyle.run(h.store,IDENT,h.clock,go=True)
    ctx = h.ctx()
    run(make_stage(SELFIE_CFG,worker=fake_worker(tmp_path))(ctx))
    rows = h.store.conn.execute("SELECT guid,prompt FROM selfie_candidate WHERE set_no>1 AND guid=102").fetchall()
    assert len(rows) == 4
    light = photo_extra.light_for(h.store,load_persona(h.store,102))
    assert all(light in r[1] for r in rows)
    assert current(h.store,102).set_no == 1
