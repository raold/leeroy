"""Part C's attachment, from the server's (server/attach.py), after Part B's: the server bots,
and the night's meme stages. Nothing at all unless culture is on with the switch in question;
custom emoji need no attachment (the desk and the console read `[culture] emoji` themselves)."""
from dataclasses import dataclass
import logging

from .config import app_bots, app_emoji, app_memes

log = logging.getLogger(__name__)


@dataclass
class CultureExtras:
    bots: object = None
    memes: bool = False
    meme_images: bool = False


def forget_bots_since(app) -> None:
    """At each start with the bots off (whatever turned them off): forget when they last started,
    so switching them on again never posts a backlog of what happened meanwhile."""
    if app_bots(app):
        return
    from .bots import SINCE_KEY, forget_since
    try:
        if app.store.get_meta(SINCE_KEY) is not None:
            forget_since(app.store)
    except Exception as error:  # a start never fails over this
        log.warning("culture_bots_since_failed code=%s", type(error).__name__)


def attach_part_c(app, server_config) -> CultureExtras | None:
    bots_on, memes_on = app_bots(app), app_memes(app)
    if not (bots_on or memes_on or app_emoji(app)):
        return None
    extras = CultureExtras()
    if bots_on:
        from .bots import ServerBots
        extras.bots = ServerBots(app, server_config)
        app.register_background(extras.bots.run)
        app.register_shutdown_drain(extras.bots.close)
    if memes_on:
        extras.memes = True
        app.engine.culture_memes = True  # a meme's caption reaches the thread's context as a note
        extras.meme_images = _attach_meme_stages(app)
    log.info("culture_part_c bots=%d emoji=%d memes=%d meme_images=%d", bots_on, app_emoji(app),
             memes_on, extras.meme_images)
    return extras


def _attach_meme_stages(app) -> bool:
    """The night's meme stages, only where the selfie worker renders: plan after the language
    stage (on the reflection model), render after the photo stages (an image stage). With
    memes_per_week 0 or selfies off, text memes still run and no image is made."""
    selfies = getattr(app, "selfies", None)
    cfg = getattr(selfies, "cfg", None)
    if cfg is None or not cfg.enabled:
        log.info("culture_memes_need key=selfies.enabled")
        return False
    if app.culture_config.memes_per_week <= 0:
        return False
    from . import memes
    runner = app.night_runner
    runner.add_stage(memes.PLAN_STAGE, memes.make_plan_stage(app, cfg), after="language", model="reflection")
    names = [stage.name for stage in runner.stages]
    runner.add_stage(memes.RENDER_STAGE, memes.make_render_stage(app, cfg),
                     after="snaps" if "snaps" in names else "selfies", model="image",
                     wants_gpu=memes.wants_gpu)
    return True
