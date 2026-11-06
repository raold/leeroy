"""Profile photos in the running mind (P2 Task 12): `self.selfies = attach_selfies(self)` in
MindApp.__init__.

[selfies] in mind.toml is read here once, at the start (an invalid table is logged and means
off). The pick service is always made, so his picks and undos of photos an earlier night
rendered work with rendering off; only with `enabled = true` does the night gain its two stages:
"photo" (the photo-kind pass, kind.py) after "enrich" on the reflection model and "selfies" (the
render, stage.py) after "regress" as an image stage. Then the photo files are repaired once
(SelfieService.sync_all), here and nowhere else: before run() starts any loop or night, since
the repair removes what a render in progress writes. It starts nothing and sends nothing."""
import logging

from leeroy_mind.config import ConfigError
from leeroy_mind.logs import log

from . import kind, stage
from .config import SelfieCfg, load_selfie_config
from .service import SelfieService


def attach_selfies(app) -> SelfieService:
    try:
        cfg = load_selfie_config(app.data_dir / "mind.toml")
    except ConfigError as e:
        log("selfies_config_invalid", level=logging.WARNING, reason=str(e))
        cfg = SelfieCfg()
    service = SelfieService(app, cfg)
    if cfg.enabled:
        runner = app.night_runner
        runner.add_stage(kind.STAGE, kind.stage_photo, after="enrich", model="reflection")
        runner.add_stage(stage.STAGE, stage.make_stage(cfg), after="regress", model="image")
    try:
        service.sync_all()
    except Exception as e:  # a repair that fails never keeps the mind from starting
        log("selfies_repair_failed", level=logging.WARNING, error=type(e).__name__)
    log("selfies_on" if cfg.enabled else "selfies_off")
    return service
