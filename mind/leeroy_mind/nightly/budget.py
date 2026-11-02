"""The night's GPU budget (P1c Task 5; controller ruling I11): what a unit of each stage costs on
the reflection model, how many units each stage will run tonight, the slot-seconds the night
has before 06:00, and the fixed order in which optional work is cut while the estimate is over.

A unit's planning cost includes its generating and judging calls. Encounters, life and
WoW-life each make two JSON calls when accepted, even though their fiction judge only
checks invariants.
The per-call planning costs are the memo's upper ends. Once a stage has made calls on 3
nights, its measured cost per call replaces them
(measured_call_s), and the decide calls of a reflect unit with candidates and the core judge's
calls of a reflecting member are the counts the nights measured (measured_supersede_calls,
measured_core_judge_calls).

A counter says how many units its stage will run tonight. It registers at the import of its
stage's module (register_counter), never when the stage runs, because plan_night runs before
the first stage; a stage without one counts 0. This module registers the counts Task 1's and
P1b's tables give; later stage modules register their own (enrich, confide, life, ...)."""
import json
import logging
import statistics
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Callable, Literal, NamedTuple

from leeroy_mind.logs import log
from leeroy_mind.nightly.settings import NightlySettings, is_sunday, night_bounds
from leeroy_mind.nightly.store import set_run_plan
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import is_raid_fight

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

# Planning seconds per call on one slot: the memo's upper ends (10-20 s for a JSON call, 45-90 s
# for a thinking call) and about 6 s for a live ask. A judge batch is a JSON call.
PLAN_S = {"think": 90.0, "json": 20.0, "live": 6.0}

# A reflect unit with candidates (Task 10) asks supersession's decide once for each belief or
# self-insight near an older one: about two of a reflection's five outputs, before measurement.
SUPERSEDE_CALLS = 2

# A reflecting member asks the core judge (Task 11's growth.judge_core) once for each self-insight
# that may become a self-note (eligible and stored open): about one reflection in two, before
# measurement. Task 11's stage_reflect records the calls it made as the night's CORE_JUDGE row
# of night_stage, which is how they are measured apart from supersession's (review-task11 I1).
CORE_JUDGE = "core_judge"
CORE_JUDGE_CALLS = 0.5

# What one unit of each stage costs, in calls of each kind, at planning costs:
UNIT_CALLS: dict[str, dict[str, float]] = {
    "reflect": {"think": 1, "json": 3},        # the questions, the reflection, a judge batch: 150 s
    "reflect_small": {"think": 1, "json": 2},  # without the questions call (DEGRADE step 6): 130 s
    # a reflect unit with candidates, on top of its own: its decide calls (K2): 40 s
    "supersede": {"json": SUPERSEDE_CALLS},
    CORE_JUDGE: {"json": CORE_JUDGE_CALLS},    # a reflecting member's core judgements: 10 s
    "notes": {"json": 2.0},                    # the call and its own judge batch of one: 40 s
    "encounters": {"json": 2},                # scene generation and one judge batch
    "confide": {"json": 1.1},
    "life": {"json": 2},                      # generation and its invariant judge
    "wowlife": {"json": 2},                   # generation and its invariant judge
    "follow": {"json": 2},                    # follow-up generation and its grounding judge
    "lexicon": {"json": 1},                   # one proposal per eligible laughed-at moment
    "profile": {"json": 2},                   # preference profile and its grounding judge
    "stories": {"think": 1, "json": 2},        # 130 s
    "recap": {"think": 1, "json": 2},          # 130 s
    "chronicle": {"json": 2},                  # 40 s
    "forget": {"json": 1},                    # one grounded summary per eligible chunk
    "breaks": {"json": 1},                     # 20 s
    "critic": {"json": 1.1},                   # one call per sampled member (PR23): 22 s
    "enrich": {"think": 1, "json": 1},         # 110 s
    # Sundays: 36 probes, about 25 quiz questions and 80 interview asks, then the quiz judge,
    # consistency and distinctiveness: about 2,400 s
    "eval": {"live": 141, "json": 77},
}

# Where a budget stage's calls are measured when it runs inside another of Task 25's stages.
MEASURED_AS = {"reflect_small": "reflect", "supersede": "reflect", CORE_JUDGE: "reflect",
               "follow": "human_world", "profile": "human_world", "lexicon": "language",
               "recap": "stories", "chronicle": "stories"}
MEASURE_NIGHTS = 7  # measured_call_s reads the last 7 nights that made calls...
MEASURE_MIN = 3     # ...once there are 3 of them

COUNTERS: dict[str, Callable[["NightContext"], int]] = {}


def register_counter(stage: str, fn: Callable[["NightContext"], int]) -> None:
    """`fn(ctx)` is how many units `stage` will run tonight. Call it at the import of the
    stage's module. A stage needs its planning cost in UNIT_CALLS first."""
    if stage not in UNIT_CALLS:
        raise ValueError(f"stage {stage!r} has no planning cost in UNIT_CALLS")
    COUNTERS[stage] = fn


def measured_call_s(store: Store, stage: str) -> float | None:
    """The median of call_s / calls over the stage's night_stage rows of the last
    MEASURE_NIGHTS nights that made calls, once there are MEASURE_MIN of them; else None."""
    with store.transaction():
        rows = store.conn.execute(
            "SELECT calls, call_s FROM night_stage WHERE stage = ? AND calls > 0 "
            "ORDER BY night DESC LIMIT ?", (stage, MEASURE_NIGHTS)).fetchall()
    if len(rows) < MEASURE_MIN:
        return None
    return statistics.median(r["call_s"] / r["calls"] for r in rows)


def _reflect_nights(store: Store) -> list[tuple[int, int, dict]]:
    """(the reflect stage's calls, the core judge's calls, the plan's units) of each night whose
    reflect stage made calls, newest first. A night with no CORE_JUDGE row made no such call."""
    with store.transaction():
        rows = store.conn.execute(
            "SELECT s.calls, coalesce(c.calls, 0) AS core, r.plan FROM night_stage s "
            "JOIN night_run r ON r.night = s.night LEFT JOIN night_stage c "
            "ON c.night = s.night AND c.stage = ? WHERE s.stage = 'reflect' AND s.calls > 0 "
            "ORDER BY s.night DESC", (CORE_JUDGE,)).fetchall()
    return [(r["calls"], r["core"], json.loads(r["plan"]).get("units", {})) for r in rows]


def _measured(per: list[float]) -> float | None:
    per = per[:MEASURE_NIGHTS]
    return statistics.median(per) if len(per) >= MEASURE_MIN else None


def measured_supersede_calls(store: Store) -> float | None:
    """The decide calls a reflect unit with candidates made (K2), as measured: over the last
    MEASURE_NIGHTS nights whose reflect stage made calls and whose plan counted units with
    candidates (plan["units"]["supersede"]), the median of the stage's calls less its units' own
    (unit_calls of each budget stage measured as reflect) and the core judge's (its CORE_JUDGE
    row) over those units, once MEASURE_MIN nights have it; else None. A night whose calls fall
    short of what they leave out (failed or cancelled units) counts 0."""
    own = [s for s in UNIT_CALLS
           if MEASURED_AS.get(s, s) == "reflect" and s not in ("supersede", CORE_JUDGE)]
    return _measured([max(0.0, (calls - core - sum(units.get(s, 0) * unit_calls(s) for s in own))
                          / units["supersede"])
                      for calls, core, units in _reflect_nights(store)
                      if units.get("supersede", 0) > 0])


def measured_core_judge_calls(store: Store) -> float | None:
    """The core judge's calls a reflecting member made (Task 11; review-task11 I1), as measured:
    over the last MEASURE_NIGHTS nights whose reflect stage made calls and whose plan counted
    reflecting members (plan["units"][CORE_JUDGE]), the median of the night's CORE_JUDGE calls
    over those members, once MEASURE_MIN nights have it; else None. A night where nobody held
    supersession's candidates counts as much as any."""
    return _measured([core / units[CORE_JUDGE] for _, core, units in _reflect_nights(store)
                      if units.get(CORE_JUDGE, 0) > 0])


def unit_calls(stage: str) -> float:
    """The calls one unit of `stage` makes at planning: a reflect unit's own 4, and for one with
    candidates also the "supersede" stage's SUPERSEDE_CALLS."""
    return sum(UNIT_CALLS[stage].values())


def _unit_s(stage: str, per_call: float | None, count: float | None = None) -> float:
    """One unit's seconds: `count` calls (the measured count, else unit_calls) at the measured
    cost per call, or at PLAN_S scaled to the count when the price is not measured."""
    calls = UNIT_CALLS[stage]
    planned = sum(n * PLAN_S[kind] for kind, n in calls.items())
    if per_call is not None:
        return (unit_calls(stage) if count is None else count) * per_call
    return planned if count is None else planned * count / unit_calls(stage)


def estimate(counts: dict[str, int], *, measured: dict[str, float | None],
             calls: dict[str, float | None] | None = None) -> dict[str, float]:
    """Slot-seconds per stage: its units times the cost of one, every call of a stage that has a
    measured cost per call in `measured` at that cost, the others at PLAN_S. `calls` gives a
    stage's measured count of calls per unit, in place of its planned one (K2: "supersede")."""
    calls = calls or {}
    return {stage: n * _unit_s(stage, measured.get(stage), calls.get(stage))
            for stage, n in counts.items()}


def night_budget_s(start: float, cutoff: float, parallel: int, floor_hours: float) -> float:
    """`parallel` slots from `start` to `cutoff`, and never less than `floor_hours` of them."""
    return parallel * max(floor_hours * 3600, cutoff - start)


class Cut(NamedTuple):
    """One setting a degrade step changes. "fit" lowers it to the largest value that fits the
    budget and "halve" halves it until it fits, neither below `to`; "lower" and "raise" move it
    to `to` unless it is already past it."""
    setting: str
    how: Literal["fit", "halve", "lower", "raise"]
    to: int


class Degrade(NamedTuple):
    name: str
    cuts: tuple[Cut, ...]


# The degrade order. Each step runs only while the estimate is over the budget, and stops at its
# floor. Never cut: the snapshot, threads, confide (the secrets), knows, nudges, tier-0, the looks
# of newcomers, the reflection itself, attraction, romance, jealousy, follow-ups and absence,
# mood, forgetting, regression and the weekly evaluation.
DEGRADE = (
    Degrade("encounters", (Cut("encounters_per_night", "fit", 20),)),
    Degrade("notes", (Cut("notes_per_night", "fit", 100),)),
    Degrade("wow_lines", (Cut("wow_lines_per_night", "halve", 10),)),
    Degrade("extras", (Cut("stories_per_night", "lower", 1), Cut("lexicon_candidates", "lower", 0),
                       Cut("profile_per_night", "lower", 0))),
    Degrade("critic", (Cut("critic_lines", "lower", 0),)),
    # a member with fewer than 6 new memories reflects on the digest alone (Task 11), which
    # the estimate then counts as reflect_small
    Degrade("questions", (Cut("questions_min_new", "raise", 6),)),
)


def _cut(c: Cut, s: NightlySettings, fits: Callable[[NightlySettings], bool]) -> NightlySettings:
    now = getattr(s, c.setting)

    def at(v: int) -> NightlySettings:
        return replace(s, **{c.setting: v})
    if c.how == "lower":
        return at(min(now, c.to))
    if c.how == "raise":
        return at(max(now, c.to))
    if now <= c.to:
        return s
    if c.how == "halve":
        v = now
        while v > c.to and not fits(at(v)):
            v = max(c.to, v // 2)
        return at(v)
    if not fits(at(c.to)):  # "fit": even the floor is over
        return at(c.to)
    lo, hi = c.to, now      # lo fits; hi does not, as the step runs only while over the budget
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if fits(at(mid)) else (lo, mid)
    return at(lo)


def _counts(ctx: "NightContext", failed: set[str]) -> dict[str, int]:
    """Each registered stage's units; a counter that raises counts 0, logged once a plan."""
    out = {}
    for stage, fn in COUNTERS.items():
        try:
            out[stage] = max(0, int(fn(ctx)))
        except Exception as e:
            if stage not in failed:
                failed.add(stage)
                log("nightly_counter_failed", level=logging.WARNING, night=ctx.night,
                    stage=stage, error=redact_real_name(repr(e), ctx.ident, "[name]"))
            out[stage] = 0
    return out


async def plan_night(ctx: "NightContext") -> dict:
    """The night's plan, with no model. The units of each stage (COUNTERS), with the scene cap
    scaled to the window (encounters_per_night x min(1, window hours / 24)), priced by
    `estimate` against night_budget_s from now to the night's 06:00; then DEGRADE's steps in
    order while the estimate is over the budget. Past the last floor the night simply runs
    longer: the GPU is idle while he is offline, and Task 4's guard keeps the next night from
    starting at once.

    The plan is {"settings": the settings it changed, "estimate_s": the night's estimate after
    the steps, "budget_s", "steps": the names of the steps that changed something, "units":
    each counted stage's units at the plan's settings, which measured_supersede_calls and
    measured_core_judge_calls read on later nights}. It is stored on the night's run
    (set_run_plan: a resumed run reuses it, like its window), applied to ctx.settings and
    ctx.plan, and logged as nightly_budget."""
    base = ctx.settings
    hours = (ctx.window[1] - ctx.window[0]) / 3600
    work = replace(base, encounters_per_night=round(
        base.encounters_per_night * max(0.0, min(1.0, hours / 24))))
    measured = {stage: measured_call_s(ctx.store, MEASURED_AS.get(stage, stage))
                for stage in UNIT_CALLS}
    counted = {"supersede": measured_supersede_calls(ctx.store),
               CORE_JUDGE: measured_core_judge_calls(ctx.store)}
    budget = night_budget_s(ctx.clock.now(), night_bounds(ctx.night, ctx.tz)[1], base.parallel,
                            base.budget_floor_hours)
    failed: set[str] = set()

    def total(settings: NightlySettings) -> float:
        ctx.settings = settings  # the counters read the settings from the context
        return sum(estimate(_counts(ctx, failed), measured=measured, calls=counted).values())

    def fits(settings: NightlySettings) -> bool:
        return total(settings) <= budget
    steps, est = [], total(work)
    for step in DEGRADE:
        if est <= budget:
            break
        cut = work
        for c in step.cuts:
            cut = _cut(c, cut, fits)
        if cut != work:
            steps.append(step.name)
            work, est = cut, total(cut)
    ctx.settings = work
    changed = {f.name: getattr(work, f.name) for f in fields(work)
               if getattr(work, f.name) != getattr(base, f.name)}
    units = {stage: n for stage, n in _counts(ctx, failed).items() if n}
    plan = {"settings": changed, "estimate_s": round(est, 1), "budget_s": round(budget, 1),
            "steps": steps, "units": units}
    ctx.plan = plan
    set_run_plan(ctx.store, ctx.night, plan)
    log("nightly_budget", night=ctx.night, **plan)
    return plan


# -- the counts Tasks 1-5 can give ---------------------------------------------------------------

def reflecting(ctx: "NightContext") -> dict[int, int]:
    """The members who reflect tonight (away ones too: they still reflect), those with at least
    min_new_memories active memories created in the window, and how many each has. Task 10's
    count of the reflect units with candidates reads it too."""
    start, end = ctx.window
    with ctx.store.transaction():
        rows = ctx.store.conn.execute(
            "SELECT owner, count(*) AS n FROM memory WHERE status = 'active' AND t_created >= ? "
            "AND t_created < ? GROUP BY owner", (start, end)).fetchall()
    members = set(ctx.members(include_away=True))
    return {r["owner"]: r["n"] for r in rows
            if r["owner"] in members and r["n"] >= ctx.settings.min_new_memories}


def _reflect(ctx: "NightContext") -> int:
    return sum(n >= ctx.settings.questions_min_new for n in reflecting(ctx).values())


def _reflect_small(ctx: "NightContext") -> int:
    return sum(n < ctx.settings.questions_min_new for n in reflecting(ctx).values())


def _notes(ctx: "NightContext") -> int:
    return min(ctx.settings.notes_per_night, 3 * len(reflecting(ctx)))


def _scenes(ctx: "NightContext") -> int:
    return ctx.settings.encounters_per_night  # plan_night has scaled it to the window


def _wow_lines(ctx: "NightContext") -> int:
    return round(ctx.settings.wow_lines_per_night * ctx.settings.wow_vs_life / 10)


def _stories(ctx: "NightContext") -> int:
    return ctx.settings.stories_per_night


def _recap(ctx: "NightContext") -> int:
    """1 when the window holds a raid fight's boss event (P1b's game_event), else 0."""
    start, end = ctx.window
    with ctx.store.transaction():
        rows = ctx.store.conn.execute(
            "SELECT payload FROM game_event WHERE type = 'boss' AND ts >= ? AND ts < ?",
            (start, end)).fetchall()
    bosses = (json.loads(r["payload"]) for r in rows)
    return int(any(is_raid_fight(b.get("map"), b.get("boss_id")) for b in bosses))


def _sunday(ctx: "NightContext") -> int:
    return int(is_sunday(ctx.night))


def _forget(ctx: "NightContext") -> int:
    # Import lazily: planning must count unfinished weekly chunks before the stage runs.
    from leeroy_mind.nightly.forget import count_pending
    return count_pending(ctx)


def _lexicon(ctx: "NightContext") -> int:
    from leeroy_mind.nightly.language_births import count_candidates
    return count_candidates(ctx)


def _follow(ctx: "NightContext") -> int:
    from leeroy_mind.nightly.human_world import count_follow
    return count_follow(ctx)


def _profile(ctx: "NightContext") -> int:
    from leeroy_mind.nightly.human_world import count_profile
    return count_profile(ctx)


def _critic(ctx: "NightContext") -> int:
    from leeroy_mind.nightly.critic import count_pending
    return count_pending(ctx)


register_counter("reflect", _reflect)
register_counter("reflect_small", _reflect_small)
register_counter("encounters", _scenes)
register_counter("notes", _notes)
register_counter("wowlife", _wow_lines)
register_counter("stories", _stories)
register_counter("recap", _recap)
register_counter("chronicle", _sunday)
register_counter("forget", _forget)
register_counter("lexicon", _lexicon)
register_counter("follow", _follow)
register_counter("profile", _profile)
register_counter("critic", _critic)
register_counter("eval", _sunday)
