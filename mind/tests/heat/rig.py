"""Heat for the talk rig (tests/engine_rig.py builds a TalkEngine with no MindApp): a HeatLive
over what the engine already holds, and the tuning rows a test sets."""
from types import SimpleNamespace

from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat.config import HeatConfig
from leeroy_mind.personas.io import load_persona, load_tuning
from leeroy_mind.tuning import changes as C

ON = HeatConfig(enabled=True)
CAPPED = HeatConfig(enabled=True, public_cap=True)


def attach(engine, store, clock, data_dir, config: HeatConfig = ON):
    """[heat] on for this engine, as MindApp attaches it; conftest's fixture detaches it."""
    app = SimpleNamespace(personas=engine.personas, store=store, clock=clock, cfg=engine.cfg,
                          file_cfg=engine.cfg, data_dir=data_dir, engine=engine,
                          human_guid=engine.human.guid)
    live = heat_live.HeatLive(app, config)
    heat_live._LIVE = live
    engine.heat = live
    return live


def on(config: HeatConfig = ON, sets=()):
    """For tuning.scenario.record(heat=...): attaches heat to the rig, with `sets` stored."""
    def hook(r):
        from pathlib import Path
        set_values(r.store, r.engine, sets)
        attach(r.engine, r.store, r.clock, Path(r.engine.data_dir), config)
    return hook


def set_values(store, engine, sets) -> None:
    """(scope, target, value) heat rows written as the tune op writes them, and in force."""
    if not sets:
        return
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    C.apply_sets(store, [C.Set(scope, target, "heat", value) for scope, target, value in sets],
                 pic, source="console", now=store.clock.now())
    engine.personas.set_tuning(load_tuning(store, store.clock.now()))
