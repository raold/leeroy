"""The nightly stage order and the adapters for extraction and weekly evaluation."""
import json

from leeroy_mind.nightly.attraction import stage_attraction
from leeroy_mind.nightly.confide import stage_confide
from leeroy_mind.nightly.critic import stage_critic
from leeroy_mind.nightly.encounters import stage_encounters
from leeroy_mind.nightly.enrich import stage_enrich
from leeroy_mind.nightly.forget import stage_forget
from leeroy_mind.nightly.human_world import stage_human_world
from leeroy_mind.nightly.knowledge import stage_knows
from leeroy_mind.nightly.language import stage_language
from leeroy_mind.nightly.life import stage_life
from leeroy_mind.nightly.mentors import stage_mentors
from leeroy_mind.nightly.mood import stage_mood
from leeroy_mind.nightly.reflect import stage_reflect
from leeroy_mind.nightly.relations import stage_notes, stage_nudges, stage_regress, stage_tier0
from leeroy_mind.nightly.romance import stage_jealousy, stage_romance
from leeroy_mind.nightly.runner import Stage, stage_snapshot
from leeroy_mind.nightly.settings import is_sunday, iso_week
from leeroy_mind.nightly.stories import stage_stories
from leeroy_mind.nightly.wowlife import stage_wowlife


class WeeklyEvaluationIncomplete(RuntimeError):
    """Preserve completed measurements and use the scheduler's bounded retry/backoff."""


async def stage_threads(ctx):
    """Extraction owns its transactions; only its terminal success earns a stage receipt."""
    async def one(unit):
        if ctx.extract_thread is None:
            raise RuntimeError("pending threads require an extractor")
        tid = int(unit)
        await ctx.extract_thread(tid)
        if ctx.store.thread_status(tid) not in ("extracted", "skipped"):
            raise RuntimeError("thread extraction did not complete")
        ctx.skip("threads", unit)

    await ctx.map_units("threads", [str(tid) for tid in ctx.store.threads_pending()], one)


async def stage_eval(ctx):
    """A factory owns its scratch asker; a supplied asker stays owned by the caller.

    The report may legitimately describe a partial run. Only its explicit completion
    receipt completes the week, so missing answers cannot be mistaken for success.
    """
    if not is_sunday(ctx.night):
        return
    week = iso_week(ctx.night)
    if ctx.done("eval", week):
        return
    from leeroy_mind.eval import run_weekly

    original = ctx.asker
    owned = None
    requested = original is not None
    try:
        if callable(original) and not hasattr(original, "ask"):
            owned = await original()
            if owned is None:
                raise RuntimeError("weekly scratch asker was unavailable")
            ctx.asker = owned
        await run_weekly(ctx, week, use_llm=requested)
        result = json.loads(ctx.store.get_meta(f"nightly:eval-result:{week}") or "{}")
        if result.get("complete") is True and result.get("requested_llm") is requested:
            ctx.skip("eval", week)
        else:
            error = WeeklyEvaluationIncomplete("weekly evaluation is incomplete")
            ctx.fail("eval", week, error)
            raise error
    finally:
        ctx.asker = original
        if owned is not None:
            await owned.aclose()


# Task14 (nightly breaks) was explicitly skipped by the owner.
STAGES = (
    Stage("snapshot", stage_snapshot, None),
    Stage("threads", stage_threads, "live"),
    Stage("confide", stage_confide, "reflection"),
    Stage("knows", stage_knows, None),
    Stage("nudges", stage_nudges, None),
    Stage("tier0", stage_tier0, None),
    Stage("enrich", stage_enrich, "reflection"),
    Stage("reflect", stage_reflect, "reflection"),
    Stage("life", stage_life, "reflection"),
    Stage("wowlife", stage_wowlife, "reflection"),
    Stage("mentors", stage_mentors, None),
    Stage("encounters", stage_encounters, "reflection"),
    Stage("attraction", stage_attraction, None),
    Stage("romance", stage_romance, None),
    Stage("jealousy", stage_jealousy, None),
    Stage("human_world", stage_human_world, "reflection"),
    Stage("language", stage_language, "reflection"),
    Stage("mood", stage_mood, None),
    Stage("notes", stage_notes, "reflection"),
    Stage("stories", stage_stories, "reflection"),
    Stage("critic", stage_critic, "reflection"),
    Stage("forget", stage_forget, "reflection"),
    Stage("regress", stage_regress, None),
    Stage("eval", stage_eval, None),
)
