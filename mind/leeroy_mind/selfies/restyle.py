"""Explicit stopped-mind photo restyling. No rendering or gallery deletion."""
from dataclasses import asdict, replace
import json

from leeroy_mind.nightly.enrich import _race_class
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.wow import CAST_MIN_LEVEL
from . import kind, photo_extra, style_variety
from .store import queue_restyled
from .style import LUST, style_of


def run(store, ident, clock, *, lust_only=False, go=False, include_owner=False):
    with store.transaction():
        personas,unreadable = {},[]
        for g,m in store.cast().items():
            if g == ident.guid or m.level < CAST_MIN_LEVEL:
                continue
            try:
                p = load_persona(store,g)
            except ValueError:
                unreadable.append(g)
                continue
            if p is not None:
                personas[g] = p
        versions = store.persona_versions()
        owners = {g for g,p in personas.items() if p.photo is not None and kind.chosen_style(store,p) is None}
    eligible = {g for g,p in personas.items() if p.appearance is not None and
                (not lust_only or style_of(p) == LUST) and (include_owner or g not in owners)}
    underage = {g for g in eligible if personas[g].identity.age < 25}
    selected = eligible - underage
    plain = {g for g in selected if style_of(personas[g]) != LUST}
    names = tuple(m.name for m in store.cast().values()) + tuple(p.identity.real_name for p in personas.values())
    seed = ":".join(f"{g}={versions[g]}" for g in sorted(personas))
    picked,rejected,cap,violations = style_variety.assign(personas,selected-plain,seed=seed,ident=ident,names=names)
    report = dict(dry_run=not go,setting_cap=cap,owner_protected=sorted(owners if not include_owner else ()),
                  skipped=([{"guid":g,"code":"no_safe_capacity"} for g in sorted(rejected)]
                           + [{"guid":g,"code":"persona_unreadable"} for g in sorted(unreadable)]
                           + [{"guid":g,"code":"adult_floor"} for g in sorted(underage)]),
                  existing_over_cap=violations,selected=[])
    if go:
        with store.transaction():
            if store.persona_versions() != versions:
                raise ValueError("personas_changed")
            photo_extra.seed(store,personas,clock.now())
            store.set_meta("selfies:restyle:last",json.dumps({"at":clock.now(), "old_kinds":{
                str(g):asdict(personas[g].photo) if personas[g].photo else None
                for g in sorted(picked.keys() | plain)}}))
            for guid in sorted(plain):
                race,cls = _race_class(store,guid)
                save_persona(store,replace(personas[guid],photo=None),source="generated",
                             ts=clock.now(),race=race,class_name=cls)
                store.conn.execute("DELETE FROM meta WHERE key=?",(kind.STYLE_KEY.format(guid=guid),))
                store.conn.execute("DELETE FROM photo_extra WHERE guid=?",(guid,))
                store.set_meta(f"selfies:restyle:pending:{guid}","1")
            for guid,choice in sorted(picked.items()):
                p = replace(personas[guid],photo=choice.photo)
                race,cls = _race_class(store,guid)
                save_persona(store,p,source="generated",ts=clock.now(),race=race,class_name=cls)
                kind._record(store,guid,style_of(p),p.photo)
                photo_extra.record(store,p,choice.light,clock.now())
                queue_restyled(store,guid,now=clock.now())
    for guid in sorted(plain):
        report["selected"].append(dict(guid=guid,code="kind_cleared" if go else "would_clear_kind"))
    for guid,choice in sorted(picked.items()):
        report["selected"].append(dict(guid=guid,code="restyled" if go else "would_restyle",
            choices=choice.codes,light=choice.light,pair_cap=style_variety.PAIR_CAP,setting_cap=cap))
    return report
