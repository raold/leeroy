"""The night's runner (P1c Task 5): its stages, the context every stage works with, and the
snapshot.

A night is a sequence of stages (Task 25's STAGES), each a coroutine over a NightContext. A
stage's work is units: each unit is one store transaction together with its night_step row
(NightContext.commit), with nothing awaited inside it. So a run cancelled mid-night writes
nothing for the units in flight, and the next run resumes at the first unfinished one (map_units
skips the done ones). A unit that fails is that unit's failure only; a fatal ReflectionError
(the model is not there) fails the night, and the scheduler backs off (Task 4).

NightRunner.run, for one night:
1. The crash-loop guard. A night whose run has stalled through RESUMES_MAX resumes since its last
   committed unit (a crash, or a cancel nobody asked for, each time) is marked failed
   (NightStalled, logged as nightly_stalled) instead of being run again.
2. The reasoner's preflight, when any stage uses the reflection model.
3. The night's plan (budget.plan_night), or on a resumed run the plan the run stored.
4. Each stage in order. The arbiter makes room for the model a stage needs when it differs from
   the last one (ensure_model). Afterwards the stage's night_stage record is written, and the
   models loaded that the night did not ask for are counted (foreign_models), never unloaded.
5. night_run.error: the failed units per stage as JSON, or NULL when none failed, on every run
   that returns.

The cancel path (his login, P1d's offline raid, the mind stopping) makes no arbiter call: the
arbiter awaits the run's end while it holds its lock. It writes one row at most: a requested
stop takes back the resume that brought the run, so his logins never count toward the guard.
It returns at once, well within P1b's 5 s stop budget."""
import asyncio
import json
import logging
import random
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from typing import Awaitable, Callable, Literal, NamedTuple, Sequence

from leeroy_mind.clock import Clock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.nightly.budget import plan_night
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.llm import Reasoner, ReflectionError
from leeroy_mind.nightly.settings import NightlySettings
from leeroy_mind.nightly.store import (NightRun, active_away, add_foreign_loads, commit_unit,
                                       refund_resume, run_get, set_run_error, stage_record,
                                       step_done)
from leeroy_mind.personas.card import card_race_class, render_prompt_card
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import snapshot
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import CAST_MIN_LEVEL, CLASSES, RACES

# The romance stances that are an attitude toward the owner (owner, 2026-10-30), and generic,
# non-explicit words for each. The owner's nightly_reflect overlay may say more.
ATTITUDES = ("fond", "drawn", "lust")
ATTITUDE_WORDS = {"fond": "{name} is warm toward {human}.",
                  "drawn": "{name} is clearly into {human}.",
                  "lust": "{name} openly wants {human}."}

RESUMES_MAX = 3  # stalled resumes since the last committed unit before the night fails
SNAPSHOT_STAGE, SNAPSHOT_UNIT, SNAPSHOT_LABEL = "snapshot", "db", "night"
MESSAGE_CHARS = 500  # of a failed unit's message in the log


class NightStalled(Exception):
    """The night's run stalled through RESUMES_MAX resumes without committing a unit: a crash
    loop, or a cancel nobody asked for at every tick. The scheduler marks the run failed."""


def _cancelling() -> bool:
    """The current task has been told to stop (P1b's app and Task 4 ask the same way)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _nothing(store: Store) -> None:
    return None


@dataclass
class NightContext:
    """What every stage works with on one night. `writer` (P1b's MemoryWriter) is the night's
    only way to store a memory: every stage writes with `writer.put(row, shown=...)` inside its
    unit's commit, so the scope always comes from the evidence (controller ruling B2). `ident`
    is built with P1b's human_identity; `plan` is the night's budget plan; `asker` (Task 23) and
    `extract_thread` (Task 25) are set by the caller.

    The lookups (the cast, names, cards, attitudes, women and men) are read once and kept for
    the night, except who is away, which members() reads each time; attitudes(fresh=True)
    re-reads the stored personas."""
    store: Store
    night: str
    window: tuple[float, float]
    settings: NightlySettings
    reasoner: Reasoner
    embedder: Embedder
    writer: MemoryWriter
    arbiter: GpuArbiter
    clock: Clock
    ident: HumanIdentity
    personas: PersonaCache
    failures: Counter[str] = field(default_factory=Counter)
    asker: object | None = None
    extract_thread: Callable[[int], Awaitable[None]] | None = None
    plan: dict = field(default_factory=dict)
    _memo: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    @property
    def human(self) -> int:
        return self.ident.guid

    @property
    def tz(self):
        """The zone for every date shown to a model: the mind clock's, never the host's."""
        return self.clock.local().tzinfo

    # -- units ---------------------------------------------------------------------------------

    def done(self, stage: str, unit: str) -> bool:
        return step_done(self.store, self.night, stage, unit)

    def commit(self, stage: str, unit: str, write: Callable[[Store], None]) -> bool:
        """`write`'s rows and the unit's night_step row in one transaction (Task 1's
        commit_unit). True when the unit committed; on any exception the transaction has rolled
        back, the unit is failed (fail) and it is False, so one bad row never sinks a night."""
        try:
            commit_unit(self.store, self.night, stage, unit, write, now=self.clock.now())
        except Exception as e:
            self.fail(stage, unit, e)
            return False
        return True

    def skip(self, stage: str, unit: str) -> bool:
        """The unit is done with nothing to write."""
        return self.commit(stage, unit, _nothing)

    def fail(self, stage: str, unit: str, err: BaseException) -> None:
        """Count the unit's failure and log nightly_unit_failed with its stage, its unit and
        the exception's type and message, the owner's real name redacted."""
        self.failures[stage] += 1
        log("nightly_unit_failed", level=logging.WARNING, night=self.night, stage=stage,
            unit=self._redact(str(unit)), error=type(err).__name__,
            message=self._redact(str(err))[:MESSAGE_CHARS])

    def _redact(self, text: str) -> str:
        return redact_real_name(text, self.ident, "[name]")

    async def map_units(self, stage: str, units: list[str],
                        fn: Callable[[str], Awaitable[None]]) -> None:
        """`fn(unit)` for each unit not done yet, at most settings.parallel at once. An exception
        from a unit, a non-fatal ReflectionError included, is that unit's failure and the others
        go on; so is a cancel nobody asked for (the run itself was not told to stop). A fatal
        ReflectionError ends the stage and is raised as itself; a requested cancel propagates."""
        todo = [u for u in dict.fromkeys(units) if not self.done(stage, u)]
        if not todo:
            return
        slots = asyncio.Semaphore(max(1, self.settings.parallel))

        async def one(unit: str) -> None:
            async with slots:
                try:
                    await fn(unit)
                except asyncio.CancelledError as e:
                    if _cancelling():
                        raise
                    self.fail(stage, unit, e)
                except ReflectionError as e:
                    if e.fatal:
                        raise
                    self.fail(stage, unit, e)
                except Exception as e:
                    self.fail(stage, unit, e)
        try:
            async with asyncio.TaskGroup() as tg:
                for unit in todo:
                    tg.create_task(one(unit), name=f"nightly-{stage}-{unit}")
        except ExceptionGroup as group:
            fatal = [e for e in group.exceptions if isinstance(e, ReflectionError)]
            if fatal:
                raise fatal[0]
            raise

    def rng(self, *parts) -> random.Random:
        """A generator seeded by the night and `parts`, the same on every run of the night."""
        return random.Random(f"{self.night}|" + "|".join(map(str, parts)))

    async def embed(self, texts: list[str]) -> list[bytes]:
        return await self.embedder.embed(list(texts), Priority.BACKGROUND)

    # -- lookups, kept for the night -----------------------------------------------------------

    def _cached(self, key, build):
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    def _roster(self) -> dict[int, Member]:
        return self._cached("roster", self.store.cast)

    def members(self, include_away: bool = False) -> list[int]:
        """The cast, sorted: guids with a stored persona, in the stored roster at level
        CAST_MIN_LEVEL or more, and not the human (pre-flight rulings C26 and C27); without the
        member on a break unless `include_away`. The cast is read once a night; who is away is
        read each time, so a break tonight's breaks stage starts keeps her out of tonight's
        scenes."""
        def build() -> list[int]:
            roster = self._roster()
            return sorted(g for g in self.store.persona_versions()
                          if g != self.human and g in roster
                          and roster[g].level >= CAST_MIN_LEVEL)
        cast = self._cached("cast", build)
        if include_away:
            return list(cast)
        away = active_away(self.store)
        return [g for g in cast if g not in away]

    def persona(self, guid: int) -> Persona:
        p = self.personas.get(guid)
        if p is None:
            raise KeyError(f"no stored persona for guid {guid}")
        return p

    def name(self, guid: int) -> str:
        """The character name from the roster; the human's is what the guild calls him."""
        if guid == self.human:
            return self.ident.called
        m = self._roster().get(guid)
        if m is None:
            raise KeyError(f"guid {guid} is not in the roster")
        return m.name

    def card(self, guid: int) -> str:
        """The card live talk shows (Pipeline._card, PR28): rendered from the persona, with race
        and class from the roster, or from the stored card for a member the roster lacks."""
        def build() -> str:
            m = self._roster().get(guid)
            if m is not None:
                race, class_name = RACES.get(m.race, ""), CLASSES.get(m.class_, "")
            else:
                race, class_name = card_race_class(self.store.get_card(guid) or "")
            return render_prompt_card(self.persona(guid), race=race, class_name=class_name)
        return self._cached(("card", guid), build)

    def attitudes(self, *, fresh: bool = False) -> dict[int, str]:
        """The women of the cast (away ones included) whose romance stance is an attitude toward
        the owner, mapped to it. Men stay friends (owner, 2026-10-30), so a man is never here,
        whatever his stance. fresh=True re-reads the stored personas, for the stages after
        attraction may have moved a band; the night's cards are rendered again too."""
        if fresh:
            for g in self.members(include_away=True):
                self.personas.invalidate(g)
            self._memo = {k: v for k, v in self._memo.items()
                          if k != "attitudes" and not (isinstance(k, tuple) and k[0] == "card")}

        def build() -> dict[int, str]:
            out = {}
            for g in self.members(include_away=True):
                p = self.personas.get(g)
                if p is not None and is_woman(p) and p.romance.stance in ATTITUDES:
                    out[g] = p.romance.stance
            return out
        return dict(self._cached("attitudes", build))

    def _genders(self) -> tuple[frozenset[int], frozenset[int]]:
        def build() -> tuple[frozenset[int], frozenset[int]]:
            people = [p for g in self.members(include_away=True)
                      if (p := self.personas.get(g)) is not None]
            return (frozenset(p.guid for p in people if is_woman(p)),
                    frozenset(p.guid for p in people if not is_woman(p)))
        return self._cached("genders", build)

    def women(self) -> frozenset[int]:
        return self._genders()[0]

    def men(self) -> frozenset[int]:
        return self._genders()[1]


Stage = NamedTuple("Stage", [("name", str), ("fn", Callable[[NightContext], Awaitable[None]]),
                             ("model", Literal["live", "reflection"] | None)])


async def stage_snapshot(ctx: NightContext) -> None:
    """Stage "snapshot", unit "db": a copy of mind.db before the night writes anything, as
    snapshots/mind-night-<stamp>.db. P1b's prune keeps each label's newest ten (PR4)."""
    if ctx.done(SNAPSHOT_STAGE, SNAPSHOT_UNIT):
        return
    path = snapshot(ctx.store.conn, ctx.settings.data_dir / "snapshots", SNAPSHOT_LABEL,
                    now=ctx.clock.now())
    if ctx.skip(SNAPSHOT_STAGE, SNAPSHOT_UNIT):
        log("nightly_snapshot", night=ctx.night, path=str(path))


class NightRunner:
    """Runs a night's stages in order (see the module docstring). `make_ctx(night, window)`
    builds each run's context; its `run(night, window)` is the scheduler's run_night."""

    def __init__(self, stages: Sequence[Stage],
                 make_ctx: Callable[[str, tuple[float, float]], NightContext]):
        self.stages = tuple(stages)
        self.make_ctx = make_ctx
        names = [s.name for s in self.stages]
        for s in self.stages:
            if s.model not in (None, "live", "reflection"):
                raise ValueError(f"stage {s.name!r}: unknown model {s.model!r}")
            if names.count(s.name) > 1:
                raise ValueError(f"stage {s.name!r} is listed twice")

    async def run(self, night: str, window: tuple[float, float]) -> None:
        ctx = self.make_ctx(night, window)
        prior = run_get(ctx.store, night)
        if prior is not None and prior.resumes > RESUMES_MAX:
            stalls = prior.resumes - 1  # this begin's resume has not run yet
            log("nightly_stalled", level=logging.WARNING, night=night, resumes=stalls)
            raise NightStalled(f"night {night} stalled through {stalls} resumes without "
                               "committing a unit")
        try:
            await self._run(ctx, prior)
        except asyncio.CancelledError:
            if _cancelling():  # a requested stop: no stall
                self._refund(ctx)
            raise

    @staticmethod
    def _refund(ctx: NightContext) -> None:
        try:
            refund_resume(ctx.store, ctx.night)
        except Exception as e:  # a closed or busy store must not turn the stop into a failure
            log("nightly_refund_failed", level=logging.WARNING, night=ctx.night, error=repr(e))

    @staticmethod
    def _model(ctx: NightContext, which: str) -> str:
        return ctx.settings.live_model if which == "live" else ctx.settings.reflection_model

    async def _run(self, ctx: NightContext, prior: NightRun | None) -> None:
        if any(s.model == "reflection" for s in self.stages):
            await ctx.reasoner.preflight()
        if prior is not None and prior.plan:
            known = {f.name for f in fields(ctx.settings)}
            ctx.plan = prior.plan
            ctx.settings = replace(ctx.settings, **{k: v for k, v in
                                                    prior.plan.get("settings", {}).items()
                                                    if k in known})
        else:
            await plan_night(ctx)
        current = None  # the model the last model stage needed
        for stage in self.stages:
            started = ctx.clock.now()
            if stage.model is not None and stage.model != current:
                await ctx.arbiter.ensure_model(self._model(ctx, stage.model))
                current = stage.model
            calls, seconds = ctx.reasoner.calls, ctx.reasoner.seconds
            await stage.fn(ctx)
            calls, seconds = ctx.reasoner.calls - calls, ctx.reasoner.seconds - seconds
            stage_record(ctx.store, ctx.night, stage.name, started=started,
                         finished=ctx.clock.now(), calls=calls, call_s=seconds)
            log("nightly_stage", night=ctx.night, stage=stage.name, calls=calls,
                call_s=round(seconds, 1), failures=ctx.failures[stage.name])
            if current is not None:
                expected = self._model(ctx, current)
                foreign = await ctx.arbiter.foreign_models(expected)
                if foreign:
                    add_foreign_loads(ctx.store, ctx.night, len(foreign))
                    log("nightly_foreign_model", night=ctx.night, stage=stage.name,
                        expected=expected, models=foreign)
        failures = dict(sorted(ctx.failures.items()))
        set_run_error(ctx.store, ctx.night, json.dumps(failures) if failures else None)
