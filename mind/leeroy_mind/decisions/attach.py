"""Startup-only attachment; off (or without [server]) adds no task, hook, seed or card part."""
from .config import load_decisions_config


def attach_decisions(app):
    config = load_decisions_config(app.data_dir / 'mind.toml')
    if not config.enabled or getattr(app, 'server', None) is None:
        return None
    from .service import DecisionsService
    service = DecisionsService(app, config)  # with its own instances of the shipped kinds
    app.engine.decisions_enabled = True
    training = getattr(app, "training", None)
    if training is not None:
        from . import strategy
        rules = service.rules_for("strategy")
        training.listener.guild_layer = lambda context: strategy.layer_for(app, context) if rules.native else None
        training.listener.guild_played = lambda ident: strategy.played(app, ident)
        training.applier.guild_allowed = lambda context: bool(rules.native and strategy.layer_for(app, context))
    app.register_event_handler('hello', service.on_hello)
    from .ranks import observe_cast
    async def on_cast(ev):
        roster = {m.guid: m for m in ev.members if m.guid != app.human_guid and m.rank and m.level >= 80}
        observe_cast(app.store, roster, now=app.clock.now())
        service.rules_for("rank").seen = True
    app.register_event_handler("cast", on_cast)
    app.register_connection_hook(service.on_connection)
    app.register_background(service.run)
    if config.bank_restock_per_week and getattr(app, "night_runner", None) is not None:
        from .restock import run
        async def restock_stage(ctx):
            await run(app, start=True)
        app.night_runner.add_stage("decisions_restock", restock_stage, after="snapshot", model=None)
    return service
