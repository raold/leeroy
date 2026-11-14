"""Attach last, without starting tasks or touching the GPU."""
from dataclasses import dataclass
import logging

from .config import ServerConfig, load_server_config
from .lease import ServerLease
from .state import refresh_play


@dataclass
class ServerExt:
    config: ServerConfig
    lease: ServerLease
    engine: object = None
    reactions: object = None
    dms: object = None
    culture: object = None


def attach_server(app) -> ServerExt | None:
    config = load_server_config(app.data_dir / 'mind.toml')
    if not config.enabled:
        return None
    lease = ServerLease(app, config)
    app.engine.server_app = app
    from .engine import ServerEngine
    from .reacting import Reactor
    from .dms import DmOpeners
    engine = ServerEngine(app, config, app.rng)
    reactions = Reactor(app, config, app.rng)
    dms = DmOpeners(app, config, app.rng)
    async def play_tick():
        while True:
            try:
                refresh_play(app, app.clock.now())
            except Exception as error:
                logging.getLogger('leeroy_mind').warning('server_play_failed code=%s', type(error).__name__)
            await app.clock.sleep(60)
    async def dm_tick():
        while True:
            try:
                await dms.tick(app.clock.now())
            except Exception as error:
                logging.getLogger('leeroy_mind').warning('server_dm_failed code=%s', type(error).__name__)
            await app.clock.sleep(30)
    async def seats_tick():
        from .seats import refresh_seats
        while True:
            try:
                refresh_seats(app, app.clock.now())
            except Exception as error:
                logging.getLogger('leeroy_mind').warning('server_seats_failed code=%s', type(error).__name__)
            await app.clock.sleep(300)
    app.register_background(seats_tick)
    app.register_background(play_tick)
    app.register_background(lease.run)
    app.register_background(engine.run)
    app.register_background(dm_tick)
    app.register_shutdown_drain(engine.close)
    app.register_shutdown_drain(dms.close)
    app.register_shutdown_drain(reactions.close)
    app.register_shutdown_drain(lease.close)
    from leeroy_mind.culture.live import attach_culture
    culture = attach_culture(app)  # Part B's calendar and loot; None with their switches off
    return ServerExt(config, lease, engine, reactions, dms, culture)
