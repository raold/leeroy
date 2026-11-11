"""Attach the enabled claims night stage to either runner."""
from leeroy_mind.config import ConfigError
from leeroy_mind.logs import log

from .config import ClaimsCfg, load_claims_config


def attach_claims(app) -> ClaimsCfg:
    return attach(app.night_runner, app.data_dir)


def attach(runner, root) -> ClaimsCfg:
    try:
        cfg = load_claims_config(root / 'mind.toml')
    except ConfigError:
        log('claims_config_invalid')
        cfg = ClaimsCfg()
    if cfg.enabled:
        from .stage import STAGE, stage_claims
        runner.add_stage(STAGE, stage_claims, after='notes', model='reflection')
    log('claims_on' if cfg.enabled else 'claims_off')
    return cfg
