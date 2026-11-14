"""The deterministic outreach night stage; startup wiring owns its switch."""
from .fill import fill_member

STAGE = "outreach"


async def stage_outreach(ctx, *, library=None):
    async def one(unit):
        guid = int(unit)
        ctx.commit(STAGE, unit, lambda store: fill_member(store, guid, now=ctx.clock.now(),
            local=ctx.clock.local(), human=ctx.human, persona=ctx.persona(guid),
            romance_dial=ctx.settings.romance_dial, ident=ctx.ident, library=library))
    await ctx.map_units(STAGE, [str(g) for g in ctx.members(include_away=True)], one)
