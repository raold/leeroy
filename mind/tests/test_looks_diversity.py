"""Fictional looks only; population priors, screens and sticky owner provenance."""
from collections import Counter
from dataclasses import asdict, replace
import json
import random
import sqlite3

import pytest

from conftest import make_persona, seed_test_cast
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE
from leeroy_mind.personas import looks_diversity as looks
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies.kind import screen_kind, screen_lust
from leeroy_mind.selfies.style import AGE_RE
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from test_migrations import db_before


def save(store,p,source="generated"):
    return save_persona(store,p,source=source,ts=1000,race="Orc",class_name="Warrior")


@pytest.mark.parametrize("region", ["UK","US"])
def test_sampler_determinism_and_documented_region_body_spread(region):
    p = make_persona(101,"Arkon",region=region)
    counts,builds = Counter(),Counter()
    for seed in range(500):
        a = looks.sample(p,seed=seed,ident=IDENT)
        assert a == looks.sample(p,seed=seed,ident=IDENT)
        counts[a.face[0]] += 1
        builds[a.build] += 1
        lo,hi = looks.BMI[a.build]
        assert lo-.5 <= a.weight_kg/(a.height_cm/100)**2 <= hi+.5
        assert 140 <= a.height_cm <= 210 and 40 <= a.weight_kg <= 180
        assert not AGE_RE.search(str(asdict(a)))
    for name,weight in zip(looks.ANCESTRIES,looks.REGIONS[region]):
        assert abs(counts[name]/500-weight/100) < .065
    assert set(builds) == set(looks.BUILDS)
    assert all(n > 10 for n in builds.values())


@pytest.mark.parametrize("gender,stance,lust", [("female","lust",True),("female","drawn",False),
                                             ("male","none",False)])
def test_lust_body_rule_and_every_appearance_value_passes_existing_screens(gender,stance,lust):
    p = make_persona(102,"Belra",player_gender=gender,stance=stance)
    seen = set()
    for seed in range(150):
        a = looks.sample(p,seed=seed,ident=IDENT)
        assert looks.safe(p,a,ident=IDENT,names=("Arkon","Belra","Coros","Dunna","Ebbo"))
        seen.add(a.build)
    assert seen == ({"curvy","voluptuous"} if lust else set(looks.BUILDS))


def test_each_fixed_string_meets_age_likeness_and_content_screens():
    from leeroy_mind.nightly.enrich import LIKENESS_RE
    from leeroy_mind.selfies.kind import ERA_RE, EXPLICIT_RE, WEAR_RE
    values = (*looks.ANCESTRIES,*looks.SKIN,*looks.COLOURS,*looks.LENGTHS,*looks.TEXTURES,
              *looks.STYLES,*looks.EYES,*looks.FACES,*looks.DETAILS,*looks.PC_WEAR,*looks.BUILDS)
    assert all(not rx.search(value) for value in values for rx in (AGE_RE,LIKENESS_RE,ERA_RE,EXPLICIT_RE,WEAR_RE))


def test_age_floor_and_collision_screen_fail_closed():
    p = make_persona(101,"Arkon")
    with pytest.raises(looks.LooksRefused,match="identity_invalid"):
        looks.sample(replace(p,identity=replace(p.identity,age=24)),ident=IDENT)
    with pytest.raises(looks.LooksRefused,match="no_distinct_candidate"):
        looks.sample(p,ident=IDENT,names=looks.ANCESTRIES)


def test_duplicate_candidate_is_redrawn_against_self_and_existing_cast():
    p = make_persona(101,"Arkon")
    first = looks.sample(p,ident=IDENT)
    p = replace(p,appearance=first)
    other = looks.sample(p,[p],ident=IDENT)
    assert first != other
    assert looks.distance(looks.looks_vector(first),looks.looks_vector(first)) == pytest.approx(0)
    assert looks.distance(looks.looks_vector(first),looks.looks_vector(other)) >= looks.MIN_LOOK_DISTANCE


@pytest.mark.parametrize("text,expected", [("",False),("[looks]\ndiversity=true",True),
    ("[looks]\ndiversity=false",False),("[looks]\ndiversity=1",False),
    ('[looks]\ndiversity="true"',False),("looks=1",False),
    ("[looks]\ndiversity=true\nunknown=true",False),("[broken",False)])
def test_invalid_configuration_is_off(tmp_path,text,expected):
    (tmp_path/"mind.toml").write_text(text,encoding="utf-8")
    assert looks.enabled(tmp_path) is expected


def test_disabled_hook_preserves_exact_persona_without_database_read(tmp_path):
    p = make_persona(101,"Arkon")
    assert looks.apply_if_enabled(None,p,tmp_path,(),ident=IDENT) is p


def test_owner_appearance_protection_survives_later_generated_changes(store,tmp_path):
    (tmp_path/"mind.toml").write_text("[looks]\ndiversity=true",encoding="utf-8")
    p = make_persona(101,"Arkon")
    save(store,p)
    owned = replace(p,appearance=APPEARANCE)
    save(store,owned,"owner")
    assert looks.protection(store.conn,101) == "owner"
    later = replace(owned,secret="An invented new secret")
    save(store,later)
    assert looks.protection(store.conn,101) == "owner"
    assert looks.apply_if_enabled(store,later,tmp_path,(),ident=IDENT) is later


def test_owner_edit_of_unrelated_fields_does_not_claim_generated_looks(store):
    p = replace(make_persona(101,"Arkon"),appearance=APPEARANCE)
    save(store,p)
    save(store,replace(p,secret="An invented different secret"),"owner")
    assert looks.protection(store.conn,101) is None


def test_owner_clearing_appearance_still_protects_the_choice(store):
    p = replace(make_persona(101,"Arkon"),appearance=APPEARANCE)
    save(store,p)
    save(store,replace(p,appearance=None),"owner")
    assert looks.protection(store.conn,101) == "owner"


def test_old_schema_upgrade_preserves_rows_and_protects_unknown_history(tmp_path,monkeypatch,clock):
    path = tmp_path/"old.db"
    conn = db_before("looks_diversity",path,tmp_path,monkeypatch)
    store = Store(conn,clock=clock)
    p = replace(make_persona(101,"Arkon"),appearance=APPEARANCE)
    save(store,p,"owner")
    save(store,replace(p,secret="An invented changed secret"),"generated")
    p2 = replace(make_persona(102,"Belra"),appearance=APPEARANCE)
    save(store,p2,"owner")
    before = store.get_persona_json(101)
    store.close()
    upgraded = Store(open_db(path),clock=clock)
    try:
        assert upgraded.get_persona_json(101) == before
        assert looks.protection(upgraded.conn,101) == "legacy_unknown"
        assert looks.protection(upgraded.conn,102) == "owner"
        assert load_persona(upgraded,102) == p2
    finally:
        upgraded.close()


def test_enabled_generator_hook_assigns_distinct_appearance(store,tmp_path):
    (tmp_path/"mind.toml").write_text("[looks]\ndiversity=true",encoding="utf-8")
    cast = seed_test_cast(store)
    p = cast[101]
    result = looks.apply_if_enabled(store,p,tmp_path,cast.values(),ident=IDENT)
    assert result.appearance is not None
    assert replace(result,appearance=p.appearance) == p
