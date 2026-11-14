"""Startup-only attachment; off (or without [server]) adds no task, hook, seed or card part."""
from .config import load_decisions_config


def attach_decisions(app):
    config = load_decisions_config(app.data_dir / 'mind.toml')
    if not config.enabled or getattr(app, 'server', None) is None:
        return None
    from . import apply
    from .service import DecisionsService
    apply.load_builtin()
    service = DecisionsService(app, config)
    app.engine.decisions_enabled = True
    app.register_background(service.run)
    return service
