"""Public API and ordered nightly units for the guild's shared language."""
from leeroy_mind.nightly.lexicon import (
    ADOPT_AFFINITY, ADOPT_AGREE, ADOPT_BASE, ADOPT_PRESTIGE, ADOPT_RANGE, ADOPT_TENURE,
    DORMANT_DAYS, DORMANT_HEAT, HEAT_DECAY, MODERN_SLANG, MUTATE_JACCARD,
    adopt_chance, era_ok, fade, prestige, spread, veto,
)
from leeroy_mind.nightly.language_births import (
    CANDIDATE_SCHEMA, JOKE_LAUGHS, candidates, find_jokes, jokes,
)
from leeroy_mind.nightly.vocab import SLANG_NEW_DAYS, SLANG_USERS, slang, vocab

STAGE = "language"


async def stage_language(ctx):
    """Births precede spread and fade; every SQL unit commits its checkpoint atomically."""
    async def sql_unit(name, function):
        async def one(unit):
            ctx.commit(STAGE, unit, lambda store: function(ctx))
        await ctx.map_units(STAGE, [name], one)

    for name, function in (("vocab", vocab), ("slang", slang), ("jokes", jokes)):
        await sql_unit(name, function)
    if not ctx.done(STAGE, "candidates"):
        await candidates(ctx)
    for name, function in (("spread", spread), ("fade", fade)):
        await sql_unit(name, function)
