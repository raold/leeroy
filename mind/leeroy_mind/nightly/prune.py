"""The night's bounded share of retention (storage/prune.py), model-free.

`[prune] enabled = true` in mind.toml (read once at the mind's start; missing, invalid or an
unknown key means off) adds the stage `prune` after `eval`. Each night it removes at most
`rows_per_night` rows per rule (a resumed night goes on within the same allowance), in transactions of 500 rows with the loop free between them, and
the files `prune_files` names. What it finds is read on a worker's read-only connection. A
backlog (the first nights, or a rule that just came of age) is worked off over several nights;
`db prune --go` with the mind stopped takes all of it at once.

The file shrinks only when it is rebuilt (`db prune --go --vacuum`): until then the freed pages
are reused by new rows, so the file stops growing."""
import asyncio
import json
import logging
import sqlite3
import tomllib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log
from leeroy_mind.storage import prune
from leeroy_mind.storage.db import error_code

STAGE = "prune"
SPENT = "prune:night:"   # meta: {rule: rows removed} of the night in the key; the last night's only
AFTER = "eval"
ROWS_MAX = 200_000


@dataclass(frozen=True)
class PruneCfg:
    enabled: bool = False
    rows_per_night: int = 20_000   # per rule


def load(path: Path) -> PruneCfg:
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("prune", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return PruneCfg()
    if not isinstance(table, dict) or set(table) - {"enabled", "rows_per_night"}:
        return PruneCfg()
    enabled = table.get("enabled", False)
    rows = table.get("rows_per_night", PruneCfg.rows_per_night)
    if type(enabled) is not bool or type(rows) is not int or not 1 <= rows <= ROWS_MAX:
        return PruneCfg()
    return PruneCfg(enabled, rows)


def _spent(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and type(v) is int and v >= 0}


def _read(path: Path, now: float) -> prune.Work | None:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)) as conn:
        if not prune.indexed(conn):
            return None
        return prune.gather(conn, now)


def make_stage(cfg: PruneCfg):
    async def stage_prune(ctx) -> None:
        data_dir = Path(ctx.settings.data_dir)
        now = ctx.clock.now()
        if not ctx.done(STAGE, "rows"):
            try:
                work = await asyncio.to_thread(_read, data_dir / "mind.db", now)
            except sqlite3.Error as e:
                ctx.fail(STAGE, "rows", RuntimeError(f"read_{error_code(e)}"))
                return
            if work is None:
                log("prune_skipped", level=logging.WARNING, night=ctx.night, reason="not_migrated")
            else:
                done = []
                # What this night already removed, kept with each batch: a night stopped
                # between batches (his login) and resumed goes on within the same allowance.
                key = SPENT + ctx.night
                with ctx.store.transaction():
                    ctx.store.conn.execute("DELETE FROM meta WHERE key >= ? AND key < ? AND key <> ?",
                                           (SPENT, SPENT + "\uffff", key))
                    spent = _spent(ctx.store.get_meta(key))
                left = lambda rule: cfg.rows_per_night - spent.get(rule, 0)
                for rule, write in prune.steps(work, now, limit=left):
                    try:
                        with ctx.store.transaction():
                            rows, size = write(ctx.store.conn)
                            spent[rule] = spent.get(rule, 0) + rows
                            ctx.store.set_meta(key, json.dumps(spent, sort_keys=True))
                        done.append((rule, rows, size))
                    except sqlite3.IntegrityError:
                        pass  # something began to stand on a row of the batch: it stays
                    await asyncio.sleep(0)
                for f in prune.fold(done):
                    log("prune_rows", night=ctx.night, rule=f.rule, rows=f.rows, bytes=f.bytes)
            if not ctx.skip(STAGE, "rows"):
                return
        if not ctx.done(STAGE, "files"):
            for f in await asyncio.to_thread(prune.prune_files, data_dir, now):
                log("prune_files", night=ctx.night, rule=f.rule, files=f.rows, bytes=f.bytes)
            ctx.skip(STAGE, "files")
    return stage_prune


def attach(runner, data_dir) -> PruneCfg:
    cfg = load(Path(data_dir) / "mind.toml")
    if cfg.enabled:
        runner.add_stage(STAGE, make_stage(cfg), after=AFTER, model=None)
        log("prune_on", rows_per_night=cfg.rows_per_night)
    return cfg
