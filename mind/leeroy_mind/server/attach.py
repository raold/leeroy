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


def attach_server(app) -> ServerExt | None:
    config = load_server_config(app.data_dir / 'mind.toml')
    if not config.enabled:
        return None
    lease = ServerLease(app, config)
    app.engine.server_app = app
    async def play_tick():
        while True:
            try:
                refresh_play(app, app.clock.now())
            except Exception as error:
                logging.getLogger('leeroy_mind').warning('server_play_failed code=%s', type(error).__name__)
            await app.clock.sleep(60)
    app.register_background(play_tick)
    app.register_background(lease.run)
    app.register_shutdown_drain(lease.close)
    return ServerExt(config, lease)
