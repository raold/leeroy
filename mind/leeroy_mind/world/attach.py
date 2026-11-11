"""World work is admitted explicitly, after the cast's writers and images."""
from .config import load
from .protocol import register
from .targets import Targets


def attach_world(app):
    register()
    app.skip_event_rows.add("world_roster")  # observation, never activity or a game clock
    service = Targets(app)
    cfg = load(app.data_dir / "mind.toml")
    if not cfg.enabled:
        return service
    app.register_connection_hook(service.connection)
    app.register_event_handler("hello",service.hello)
    app.register_event_handler("world_roster",service.event)
    app.register_background(service.run)
    from .stage import make_stage, count
    runner = app.night_runner
    async def refresh(ctx):
        await service.refresh()
    runner.add_stage("world_targets",refresh,after="regress",model=None)
    runner.add_stage("world_personas",make_stage(lambda:app.cfg.cast),
                     after="world_targets",model="reflection",wants_gpu=count)
    if cfg.selfies and app.selfies.cfg.enabled:
        from .photos import photo_stage, photo_count, image_stage, wants_gpu
        runner.add_stage("world_photos",photo_stage,after="world_personas",
                         model="reflection",wants_gpu=photo_count)
        # Already registered cast images retain priority over these image stages.
        runner.add_stage("world_selfies",image_stage(app.selfies.cfg),after="snaps" if app.selfies.cfg.snaps else "selfies",
                         model="image",wants_gpu=wants_gpu)
    return service
