"""Startup-only attachment; off (or without [server]) adds no task, hook, seed or card part."""
from .config import load_decisions_config


def attach_decisions(app):
    config = load_decisions_config(app.data_dir / 'mind.toml')
    if not config.enabled or getattr(app, 'server', None) is None:
        return None
    from .service import DecisionsService
    service = DecisionsService(app, config)  # with its own instances of the shipped kinds
    app.engine.decisions_enabled = True
    app.register_event_handler('hello', service.on_hello)
    app.register_connection_hook(service.on_connection)
    app.register_background(service.run)
    return service
