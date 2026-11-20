"""The mind's start: [geography] and [owner] read once and pinned (geography/config.py), and
whether geography is on written to meta for the console. Logs only on/off and whether his home
is set, never where."""
import logging
import sqlite3

from . import config

log = logging.getLogger(__name__)


def _store(app, enabled: bool) -> None:
    try:
        if config.stored(app.store.conn) != enabled or app.store.get_meta(config.META_KEY) is None:
            app.store.set_meta(config.META_KEY, "1" if enabled else "0")
    except sqlite3.Error as e:
        log.warning("geography_meta_failed reason=%s", type(e).__name__)


def attach(app) -> config.GeographyCfg:
    path = app.data_dir / "mind.toml"
    cfg = config.load_geography_config(path)
    owner = config.load_owner_home(path) if cfg.enabled else None
    config.pin(cfg.enabled, owner)
    _store(app, cfg.enabled)
    log.info("geography %s owner_home=%s", "on" if cfg.enabled else "off",
             "set" if owner is not None else "unset")
    return cfg


def off(app) -> config.GeographyCfg:
    """An attach that broke: geography off, as the switch's false."""
    config.pin(False)
    _store(app, False)
    return config.GeographyCfg(enabled=False)
