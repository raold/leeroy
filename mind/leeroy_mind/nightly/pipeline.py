"""The nightly stage order and the adapters for extraction and weekly evaluation."""
import json
from datetime import date, timedelta

from leeroy_mind.arc.night import stage_arc
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
from leeroy_mind.bond.night import stage_bond
from leeroy_mind.nightly.runner import Stage, stage_snapshot
from leeroy_mind.nightly.settings import is_sunday, iso_week
from leeroy_mind.nightly.stories import stage_stories
from leeroy_mind.nightly.wowlife import stage_wowlife


class WeeklyEvaluationIncomplete(RuntimeError):
    """A partial weekly evaluation: recorded as a failed unit, never a failed night."""


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
    A partial or failed evaluation is recorded as a failed unit (ctx.fail) but never
    stops the night: the other stages still run. The week has no completion receipt, so
    the next night of the following week tries it again (see _eval_week).
    """
    week = _eval_week(ctx)
    if week is None or ctx.done("eval", week):
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
        result = _eval_result(ctx, week)
        if result.get("complete") is True and result.get("requested_llm") is requested:
            ctx.skip("eval", week)
        else:
            ctx.fail("eval", week, WeeklyEvaluationIncomplete("weekly evaluation is incomplete"))
    except Exception as error:
        ctx.fail("eval", week, error)
    finally:
        ctx.asker = original
        if owned is not None:
            await owned.aclose()


def _eval_result(ctx, week):
    try:
        result = json.loads(ctx.store.get_meta(f"nightly:eval-result:{week}") or "{}")
    except ValueError:
        return {}
    return result if isinstance(result, dict) else {}


def _eval_week(ctx):
    """The week to evaluate tonight: this one on its Sunday; on any other night, the
    week that just ended when its evaluation ran but did not complete (a retry)."""
    if is_sunday(ctx.night):
        week = iso_week(ctx.night)
        return None if _eval_result(ctx, week).get("complete") is True else week
    last_sunday = date.fromisoformat(ctx.night) - timedelta(days=date.fromisoformat(ctx.night).isoweekday())
    week = iso_week(last_sunday.isoformat())
    result = _eval_result(ctx, week)
    return week if result and result.get("complete") is not True else None


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
    Stage("arc", stage_arc, "reflection"),
    Stage("jealousy", stage_jealousy, None),
    Stage("bond", stage_bond, None),
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
