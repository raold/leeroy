"""Attach last, without starting tasks or touching the GPU."""
from dataclasses import dataclass

from .config import ServerConfig, load_server_config
from .lease import ServerLease


@dataclass
class ServerExt:
    config: ServerConfig
    lease: ServerLease


def attach_server(app) -> ServerExt | None:
    config = load_server_config(app.data_dir / 'mind.toml')
    if not config.enabled:
        return None
    lease = ServerLease(app, config)
    app.register_background(lease.run)
    app.register_shutdown_drain(lease.close)
    return ServerExt(config, lease)
