"""Startup-only attachment: with [godmode] off nothing is attached, so seeds, cards, openers and
reactions run as on main; the tune op's edit verbs say godmode is off."""
from .config import load_godmode_config


def attach(app):
    app.godmode_config = load_godmode_config(app.data_dir / "mind.toml")
    if not app.godmode_config.enabled:
        return None
    from leeroy_mind.guild.protocol import register
    from .service import Godmode
    register()  # string_do and the job event (safe again)
    service = Godmode(app, app.godmode_config)
    app.engine.godmode = service  # her card's intent lines (talk.awareness)

    async def job(ev):
        service.on_job(ev)
    app.register_event_handler("job", job)
    app.register_shutdown_receipt("job", service.on_job)
    app.register_background(service.run)
    app.register_shutdown_drain(service.close)
    return service
