"""Startup-only attachment; off (or without [server]) adds no task, hook, seed or card part. A Part C
kind switched off ([decisions] boss_strategy, rank_reviews) wires nothing into training or the cast."""
from .config import load_decisions_config


def attach_decisions(app):
    config = load_decisions_config(app.data_dir / 'mind.toml')
    if not config.enabled or getattr(app, 'server', None) is None:
        return None
    from .service import DecisionsService
    service = DecisionsService(app, config)  # with its own instances of the shipped kinds
    app.engine.decisions_enabled = True
    app.engine.decisions_kinds = service.kinds_on  # the card's Decisions part leaves the others out
    training = getattr(app, "training", None)
    if training is not None and service.kind_on("strategy"):
        from . import strategy
        rules = service.rules_for("strategy")
        training.listener.guild_layer = lambda context: strategy.layer_for(app, context) if rules.native else None
        training.listener.guild_hold = lambda: strategy.held(app)
        training.listener.guild_played = lambda ident: strategy.played(app, ident)
        def guild_allowed(context, ident):
            selected = training.listener.guild_layer(context)
            return selected is not None and selected[0] == ident
        training.applier.guild_allowed = guild_allowed
        training.applier.guild_hold = training.listener.guild_hold
    app.register_event_handler('hello', service.on_hello)
    if service.kind_on("rank"):
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
