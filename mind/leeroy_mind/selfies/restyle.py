"""Explicit stopped-mind photo restyling. No rendering or gallery deletion."""
from dataclasses import replace

from leeroy_mind.nightly.enrich import _race_class
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.wow import CAST_MIN_LEVEL
from . import kind, photo_extra, style_variety
from .store import queue_restyled
from .style import LUST, style_of


def run(store, ident, clock, *, lust_only=False, go=False, include_owner=False):
    with store.transaction():
        personas = {g:p for g,m in store.cast().items() if g != ident.guid and m.level >= CAST_MIN_LEVEL
                    and (p := load_persona(store,g)) is not None}
        versions = store.persona_versions()
        owners = {g for g,p in personas.items() if p.photo is not None and kind.chosen_style(store,p) is None}
    selected = {g for g,p in personas.items() if p.appearance is not None and
                (not lust_only or style_of(p) == LUST) and (include_owner or g not in owners)}
    names = tuple(m.name for m in store.cast().values()) + tuple(p.identity.real_name for p in personas.values())
    seed = ":".join(f"{g}={versions[g]}" for g in sorted(personas))
    picked,rejected,cap,violations = style_variety.assign(personas,selected,seed=seed,ident=ident,names=names)
    report = dict(dry_run=not go,setting_cap=cap,owner_protected=sorted(owners if not include_owner else ()),
                  skipped=[{"guid":g,"code":"no_safe_capacity"} for g in sorted(rejected)],
                  existing_over_cap=violations,selected=[])
    if go:
        with store.transaction():
            if store.persona_versions() != versions:
                raise ValueError("personas_changed")
            for guid,choice in sorted(picked.items()):
                p = replace(personas[guid],photo=choice.photo)
                race,cls = _race_class(store,guid)
                save_persona(store,p,source="generated",ts=clock.now(),race=race,class_name=cls)
                kind._record(store,guid,style_of(p),p.photo)
                photo_extra.record(store,p,choice.light,clock.now())
                queue_restyled(store,guid,now=clock.now())
    for guid,choice in sorted(picked.items()):
        report["selected"].append(dict(guid=guid,code="restyled" if go else "would_restyle",
            choices=choice.codes,light=choice.light,pair_cap=style_variety.PAIR_CAP,setting_cap=cap))
    return report
