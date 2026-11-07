"""Attach only the night extension. No check is wired into live generation."""
from leeroy_mind.config import ConfigError
from leeroy_mind.logs import log

from .config import ClaimsCfg, load_claims_config


def attach_claims(app) -> ClaimsCfg:
    try:
        cfg = load_claims_config(app.data_dir / 'mind.toml')
    except ConfigError:
        log('claims_config_invalid')
        cfg = ClaimsCfg()
    if cfg.enabled:
        from .stage import STAGE, stage_claims
        app.night_runner.add_stage(STAGE, stage_claims, after='notes', model='reflection')
    log('claims_on' if cfg.enabled else 'claims_off')
    return cfg
