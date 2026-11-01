"""The night's own rows in mind.db: its runs and their checkpoints (night_run, night_stage,
night_step), unit commits, the grounding log, the review queue, who is away, stories and the
persona ledger. Every write runs inside store.transaction(), and so does every read, which then
ends its read transaction as Store's own reads do, or joins the caller's. Text is stored as the
caller gives it: callers screen it first."""
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from leeroy_mind.storage.store import Store, clean


@dataclass(frozen=True)
class NightRun:
    """One night's run. Times are unix seconds; `plan` is the night's budget plan and `resumes`
    the crash-loop guard's count (Task 5)."""
    night: str
    window_start: float
    window_end: float
    status: str
    started: float
    finished: float | None
    attempts: int
    foreign_loads: int
    error: str | None
    plan: dict
    resumes: int = 0


def _j(obj) -> str:
    return clean(json.dumps(obj, ensure_ascii=False))


def _read(store: Store, sql: str, params=()) -> list[sqlite3.Row]:
    with store.transaction():
        return store.conn.execute(sql, params).fetchall()


# -- runs ------------------------------------------------------------------------------------------

def run_get(store: Store, night: str) -> NightRun | None:
    rows = _read(store, "SELECT * FROM night_run WHERE night = ?", (night,))
    if not rows:
        return None
    r = rows[0]
    return NightRun(night=r["night"], window_start=r["window_start"], window_end=r["window_end"],
                    status=r["status"], started=r["started"], finished=r["finished"],
                    attempts=r["attempts"], foreign_loads=r["foreign_loads"], error=r["error"],
                    plan=json.loads(r["plan"]), resumes=r["resumes"])


def run_begin(store: Store, night: str, window_start: float, window_end: float, *,
              now: float, count_attempt: bool = True) -> NightRun:
    """A new run of the night, or on a night that has one, the same run again: running, the
    original window, and one more attempt unless count_attempt is False (Task 4 resumes an
    interrupted run that way). An attempt starts `resumes` from 0, and a resume adds one."""
    with store.transaction():
        store.conn.execute(
            "INSERT INTO night_run(night, window_start, window_end, status, started) "
            "VALUES (?, ?, ?, 'running', ?) "
            "ON CONFLICT(night) DO UPDATE SET status = 'running', attempts = attempts + ?, "
            "resumes = CASE WHEN ? THEN 0 ELSE resumes + 1 END",
            (night, window_start, window_end, now, int(count_attempt), int(count_attempt)))
        return run_get(store, night)


def run_finish(store: Store, night: str, *, now: float, status: str,
               error: str | None = None) -> None:
    with store.transaction():
        store.conn.execute("UPDATE night_run SET status = ?, finished = ?, error = ? "
                           "WHERE night = ?", (status, now, error, night))


def last_window_end(store: Store) -> float | None:
    """The greatest window_end among done and abandoned runs: an abandoned run's window counts as
    covered, so the units it finished are not done again."""
    return _read(store, "SELECT max(window_end) AS t FROM night_run "
                        "WHERE status IN ('done', 'abandoned')")[0]["t"]


UNFINISHED = ("running", "interrupted")  # a run that resumes, with no new attempt


def unfinished_nights(store: Store) -> list[str]:
    """The nights whose run is unfinished (running or interrupted), earliest first."""
    return [r["night"] for r in _read(store, "SELECT night FROM night_run WHERE status IN "
                                             "('running', 'interrupted') ORDER BY night")]


def last_finished(store: Store) -> float | None:
    """The greatest finished among done runs."""
    return _read(store, "SELECT max(finished) AS t FROM night_run WHERE status = 'done'")[0]["t"]


def set_run_plan(store: Store, night: str, plan: dict) -> None:
    """The night's budget plan; a night with no run row is left alone."""
    with store.transaction():
        store.conn.execute("UPDATE night_run SET plan = ? WHERE night = ?", (_j(plan), night))


def set_run_error(store: Store, night: str, error: str | None) -> None:
    """night_run.error: the runner's unit-failure summary (JSON, or None for none)."""
    with store.transaction():
        store.conn.execute("UPDATE night_run SET error = ? WHERE night = ?", (error, night))


def refund_resume(store: Store, night: str) -> None:
    """Take back the resume that brought a run which then stopped because it was asked to (his
    login, P1d's raid, the mind stopping): only stalls count. Never below 0."""
    with store.transaction():
        store.conn.execute("UPDATE night_run SET resumes = max(resumes - 1, 0) WHERE night = ?",
                           (night,))


def add_foreign_loads(store: Store, night: str, n: int) -> None:
    with store.transaction():
        store.conn.execute("UPDATE night_run SET foreign_loads = foreign_loads + ? "
                           "WHERE night = ?", (n, night))


def stage_record(store: Store, night: str, stage: str, *, started: float, finished: float,
                 calls: int, call_s: float) -> None:
    """What the stage took on this night, over all its passes. The first pass makes the row; a
    later one (a resumed run) that made model calls adds its calls and seconds and moves
    `finished` on, and one that made none leaves the row as it was, so a resume that only skips
    done units keeps the night's real figures."""
    with store.transaction():
        store.conn.execute(
            "INSERT INTO night_stage(night, stage, started, finished, calls, call_s) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(night, stage) DO UPDATE SET "
            "started = min(started, excluded.started), finished = max(finished, "
            "excluded.finished), calls = calls + excluded.calls, call_s = call_s + excluded.call_s "
            "WHERE excluded.calls > 0", (night, stage, started, finished, calls, call_s))


# -- units ---------------------------------------------------------------------------------------

def step_done(store: Store, night: str, stage: str, unit: str) -> bool:
    return bool(_read(store, "SELECT 1 FROM night_step WHERE night = ? AND stage = ? "
                             "AND unit = ?", (night, stage, unit)))


def step_finished_at(store: Store, stage: str, unit: str) -> float | None:
    """When the unit last finished, on any night."""
    return _read(store, "SELECT max(finished) AS t FROM night_step WHERE stage = ? AND unit = ?",
                 (stage, unit))[0]["t"]


def commit_unit(store: Store, night: str, stage: str, unit: str,
                write: Callable[[Store], None], *, now: float) -> None:
    """One unit of work: `write`'s rows and the unit's night_step row in one transaction, so a
    failure leaves neither and a done unit cannot commit twice. A committed unit is progress,
    so the run's resume count starts again."""
    with store.transaction():
        write(store)
        store.conn.execute("INSERT INTO night_step(night, stage, unit, finished) "
                           "VALUES (?, ?, ?, ?)", (night, stage, unit, now))
        store.conn.execute("UPDATE night_run SET resumes = 0 WHERE night = ? AND resumes > 0",
                           (night,))


# -- logs and the review queue -------------------------------------------------------------------

def log_grounding(store: Store, *, night: str, stage: str, owner: int, kind: str, text: str,
                  verdict: str, check: str, reason: str, ts: float) -> None:
    with store.transaction():
        store.conn.execute(
            "INSERT INTO grounding_log(night, stage, owner, kind, text, verdict, check_stage, "
            "reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (night, stage, owner, kind, clean(text), verdict, check,
             None if reason is None else clean(reason), ts))


def enqueue_review(store: Store, *, guid: int, kind: str, text: str, evidence, reason: str,
                   now: float) -> int:
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO review_queue(guid, kind, text, evidence, reason, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guid, kind, clean(text), _j(list(evidence)), clean(reason), now)).lastrowid


# -- away and stories ----------------------------------------------------------------------------

def active_away(store: Store) -> set[int]:
    """The members on a break now (at most one: the away_one index)."""
    return {r["guid"] for r in _read(store, "SELECT guid FROM away WHERE returned_at IS NULL")}


def insert_story(store: Store, *, title: str, summary: str, participants, evidence,
                 ts: float) -> int:
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO story(title, summary, participants, evidence, t_created) "
            "VALUES (?, ?, ?, ?, ?)",
            (clean(title), clean(summary), _j(list(participants)), _j(list(evidence)), ts)
        ).lastrowid


# -- the persona ledger --------------------------------------------------------------------------

def ledger_add(store: Store, *, guid: int, kind: str, text: str, evidence, ref: int | None = None,
               now: float, score_before: float | None = None) -> int:
    """A row of the persona ledger: slow personality change and stance moves. `ref` is the row
    an undo takes back; `score_before` belongs to a stance_band row."""
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO persona_ledger(guid, ts, kind, text, evidence, ref, score_before) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guid, now, kind, clean(text), _j(list(evidence)), ref, score_before)).lastrowid


def ledger_rows(store: Store, guid: int | None = None, *,
                since: float | None = None) -> list[sqlite3.Row]:
    """Ledger rows, of one member or everyone, from `since` on: newest first, and of two at one
    time the one written later first."""
    where, params = [], []
    if guid is not None:
        where.append("guid = ?")
        params.append(guid)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    sql = "SELECT * FROM persona_ledger" + (" WHERE " + " AND ".join(where) if where else "")
    return _read(store, sql + " ORDER BY ts DESC, id DESC", params)
