"""The night stage `turing` (reflection model, after `forget`), attached only with
`[turing] enabled = true` at the mind's start. Units, each its own transaction:

- `stylo`: part A over the `days` before the night's end, read on a read-only connection in a
  worker thread; writes each family's turing_night row.
- `plan`: the night's judge windows (judge.plan), saved in meta so a resumed run judges the
  same ones.
- `w1`..`wN`: one judge call each; writes its turing_judgement row and its family's totals.

N is `[turing] windows` (default 12), which the night's budget may lower (degrade step
`turing`, never below 4). A judge call that fails is that unit's failure and is tried again on
a resumed run; the owner's GPU pause stops the night as any stage's would. Logs carry the
night, unit, family and counts, never text."""
import asyncio
import json
import sqlite3
from pathlib import Path

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.llm import ReflectionError

from . import judge, store, stylo
from .config import load
from .corpus import name_pattern, names_by_guid, read_lines, read_refs

STAGE = "turing"
AFTER = "forget"


def attach(runner, data_dir) -> bool:
    cfg = load(Path(data_dir) / "mind.toml")
    if not cfg.enabled:
        return False
    runner.add_stage(STAGE, make_stage(cfg), after=AFTER, model="reflection")
    log("turing_attached", windows=cfg.windows, days=cfg.days)
    return True


def _cfg(ctx):
    key = "turing:cfg"
    if key not in ctx._memo:
        ctx._memo[key] = load(Path(ctx.settings.data_dir) / "mind.toml")
    return ctx._memo[key]


def count(ctx) -> int:
    """Judge calls left tonight: the saved plan's windows not done, else the setting."""
    if not _cfg(ctx).enabled:
        return 0
    saved = ctx.store.get_meta(store.PLAN_KEY + ctx.night)
    if saved is not None:
        try:
            units = [w["unit"] for w in json.loads(saved)]
        except (ValueError, TypeError, KeyError):
            units = []
        return sum(not ctx.done(STAGE, u) for u in units)
    return max(0, int(ctx.settings.turing_windows))


register_counter(STAGE, count)


def his_names(ident) -> tuple[str, ...]:
    """His names as members may write them (masked in what the judge reads; never shown)."""
    return tuple(dict.fromkeys(n for n in (ident.called, ident.character_name, ident.nickname,
                                           ident.real_name) if n and n.strip()))


async def _read(ctx, question):
    """question(conn) on a fresh read-only connection in a worker thread; the store's own
    connection when it has no file (an in-memory test database)."""
    with ctx.store.transaction():
        path = ctx.store.conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        return question(ctx.store.conn)

    def go():
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            return question(conn)
        finally:
            conn.close()
    return await asyncio.to_thread(go)


def make_stage(cfg):
    async def stage_turing(ctx) -> None:
        human, names = ctx.human, his_names(ctx.ident)
        end = min(ctx.window[1], ctx.clock.now())
        seed = f"turing|{ctx.night}"
        tz = ctx.tz
        if not ctx.done(STAGE, "stylo"):
            try:
                result = await _read(ctx, lambda conn: stylo.compute(
                    conn, human=human, end=end, days=cfg.days, tz=tz, his_names=names, seed=seed))
            except Exception:  # its unit's failure only; the message never carries text
                ctx.fail(STAGE, "stylo", ValueError("turing_stylo_failed"))
            else:
                def write_stylo(s):
                    store.put_stylo(s.conn, ctx.night, result, now=ctx.clock.now())
                if ctx.commit(STAGE, "stylo", write_stylo):
                    for family, r in result["families"].items():
                        log("turing_stylo", night=ctx.night, family=family, status=r["status"],
                            auc=r["auc"], human=r["human_lines"], members=r["member_lines"])
        if ctx.done(STAGE, "plan"):
            try:
                windows = json.loads(ctx.store.get_meta(store.PLAN_KEY + ctx.night) or "[]")
            except ValueError:
                windows = []
        else:
            n = max(0, int(ctx.settings.turing_windows))

            def choose(conn):
                lines = read_lines(conn, human=human, start=end - cfg.days * 86400, end=end)
                return judge.plan(lines, n, seed=seed + "|plan", judged=store.judged_refs(conn),
                                  recent_from=end - judge.RECENT_DAYS * 86400)
            try:
                windows = await _read(ctx, choose) if n else []
            except Exception:
                ctx.fail(STAGE, "plan", ValueError("turing_plan_failed"))
                return

            def write_plan(s):
                s.set_meta(store.PLAN_KEY + ctx.night, json.dumps(windows))
            if not ctx.commit(STAGE, "plan", write_plan):
                return
            log("turing_plan", night=ctx.night, windows=len(windows))
        if not windows or all(ctx.done(STAGE, w["unit"]) for w in windows):
            return
        try:
            by_guid = await _read(ctx, lambda conn: names_by_guid(conn, human=human, his=names))
        except Exception:
            ctx.fail(STAGE, "names", ValueError("turing_names_failed"))
            return
        pattern = name_pattern([*by_guid.values(), *names])
        by_unit = {w["unit"]: w for w in windows}

        async def one(unit: str) -> None:
            w = by_unit[unit]
            lines = await _read(ctx, lambda conn: read_refs(conn, w["refs"], human=human))
            if lines is None:
                scored = {"status": "missing", "speakers": 0, "correct": 0, "his": 0,
                          "his_correct": 0, "tells": []}
            else:
                shown = judge.render(lines, by_guid=by_guid, human=human, his_names=names)
                try:
                    reply = await ctx.reasoner.json(judge.SYSTEM, shown.prompt,
                                                    judge.response_schema(list(shown.letters)))
                except ReflectionError as err:  # the owner's GPU pause passes through untouched
                    raise ReflectionError("turing_judge_failed", fatal=err.fatal) from None
                scored = judge.score(reply, shown.letters, human=human,
                                     keep_phrases=judge.public(lines), pattern=pattern)

            def write(s):
                store.put_judgement(s.conn, ctx.night, unit, w["family"], w["refs"], scored,
                                    now=ctx.clock.now())
            if ctx.commit(STAGE, unit, write):
                log("turing_judged", night=ctx.night, unit=unit, family=w["family"],
                    status=scored["status"], speakers=scored["speakers"],
                    correct=scored["correct"], his=scored["his"])
        await ctx.map_units(STAGE, list(by_unit), one)
    return stage_turing
