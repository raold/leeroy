"""Atomic, resumable nightly fingerprint units with no model or prompt changes."""
from leeroy_mind.logs import log
from leeroy_mind.tuning import dials_of

from . import distinctiveness, fingerprint, update
from .config import enabled


def attach(runner, data_dir):
    if enabled(data_dir / 'mind.toml'):
        runner.add_stage('fingerprint', stage_fingerprint, after='threads', model=None)


async def stage_fingerprint(ctx):
    from leeroy_mind.learn.control import effective
    from leeroy_mind.learn.config import load_learn_config
    cfg=load_learn_config(ctx.settings.data_dir/'mind.toml')
    if effective(ctx.store.conn,cfg).freeze:
        return
    async def one(unit):
        guid = int(unit)
        def write(store):
            if effective(store.conn,cfg).freeze:
                return
            try:
                persona = ctx.persona(guid)
                dials = dials_of(persona)
                profile = update(store.conn, guid, now=ctx.clock.now(),
                                 talkative=dials.level('talkative'), humour_dial=dials.level('humour'),
                                 blocked_names=(ctx.ident.real_name,), settings=cfg)
            except Exception:
                # NightContext.fail logs exception messages; do not forward SQLite/JSON text.
                raise ValueError('fingerprint_unit_failed') from None
            log('fingerprint_updated', guid=guid, samples=profile.samples, seeded=profile.seeded,
                version=profile.version)
        ctx.commit('fingerprint', unit, write)
    await ctx.map_units('fingerprint', [str(g) for g in ctx.members(include_away=True)], one)


def report_section(ctx, week):
    """Current profile snapshot, labelled as such; not a retrospective reconstruction."""
    profiles = [fingerprint(ctx.store.conn,g) for g in ctx.members(include_away=True)]
    return {'status': 'complete', 'snapshot_at': ctx.clock.now(),
            'members': len(profiles), 'available': sum(p.available for p in profiles),
            'missing': sum(not p.available for p in profiles),
            'seeded': sum(p.seeded for p in profiles if p.available),
            'mean_pairwise_distance': distinctiveness(profiles)}
