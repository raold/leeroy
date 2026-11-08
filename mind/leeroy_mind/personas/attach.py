"""Admit the optional newcomer writer before the night's persona consumers."""
from leeroy_mind.config import ConfigError
from leeroy_mind.logs import log

from .config import PersonasCfg, load_personas_config


def attach_personas(app) -> PersonasCfg:
    try:
        cfg = load_personas_config(app.data_dir / "mind.toml")
    except ConfigError:
        log("personas_config_invalid")
        cfg = PersonasCfg()
    if cfg.auto_newcomers:
        from .newcomers import STAGE, count, make_stage
        app.night_runner.add_stage(STAGE, make_stage(lambda: app.cfg.cast),
                                   after="snapshot", model="reflection", wants_gpu=count)
    log("newcomers_on" if cfg.auto_newcomers else "newcomers_off")
    return cfg
