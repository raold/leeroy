"""Startup-only attachment; disabled adds no clocks, hooks, stages or queries."""
from functools import partial

from .config import load_outreach_config


def attach(app):
    if not load_outreach_config(app.data_dir / 'mind.toml').enabled:
        return None
    from .night import stage_outreach
    from .service import Outreach
    app.night_runner.add_stage('outreach', partial(stage_outreach, library=getattr(app, 'library', None)),
                               after='human_world', model=None)
    return Outreach(app)
