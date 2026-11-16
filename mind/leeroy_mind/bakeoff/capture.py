"""Capture: with `[bakeoff] capture = true` the talk pipeline keeps each sent model turn's exact
request in `bakeoff_turn` (migration *_bakeoff.sql). Every model call a turn makes is kept on
the turn (at most KEPT); when its line is recorded, the call whose reply the sent words came from
is written (chosen), inside its own savepoint of the line's transaction; a failure is logged by
its code and swallowed, so a turn is never slowed, changed or lost by it. Rows older than
RETENTION_S are deleted: at the
mind's start whatever the switch says, and each night by the model-free stage `bakeoff`, which
is attached while the switch is on or captured rows remain. Logs carry ids and counts only."""
import json
import logging
import re
import sqlite3

from leeroy_mind.logs import log

from .config import load

RETENTION_S = 14 * 86400
STAGE = "bakeoff"
AFTER = "threads"
_log = logging.getLogger("leeroy_mind")


def available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bakeoff_turn'"
                        ).fetchone() is not None


KEPT = 8  # the model calls a turn keeps for its next line (drafts, retries, a fallback)


def request(model: str, messages: list[dict], options: dict, think, reply: str, *,
            max_lines: int, words: int, two_lines: bool) -> dict:
    """What one model call asked and answered, as the pipeline keeps it until the line is sent."""
    return {"model": model, "messages": [dict(m) for m in messages], "options": dict(options),
            "think": think, "reply": reply,
            "shape": {"max_lines": int(max_lines), "words": int(words or 0),
                      "two_lines": bool(two_lines)}}


def keep(meta, req: dict) -> None:
    meta.bakeoff_requests.append(req)
    del meta.bakeoff_requests[:-KEPT]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def chosen(requests: list[dict], words: str) -> dict | None:
    """The call whose reply the sent words came from: the most words in common, the latest on a
    tie (a draft kept over a worse retry, an earlier candidate a guard preferred, the owed
    fallback). None when no reply shares a word with them: the line is not the model's."""
    sent = _tokens(words)
    best, score = None, 0
    for req in requests:
        common = len(sent & _tokens(req.get("reply") or ""))
        if common and common >= score:
            best, score = req, common
    return best


def record(conn: sqlite3.Connection, req: dict, *, ts: float, line_id: int,
           discord_id: int | None, guid: int, channel: str, kind: str) -> int:
    """One captured turn, its first line's ref in refs. The new row's id."""
    ref = f"d:{discord_id}" if discord_id is not None else f"c:{line_id}"
    think = req.get("think")
    cur = conn.execute(
        "INSERT INTO bakeoff_turn(ts, line_id, discord_id, refs, guid, channel, kind, model, think, "
        "options, messages, reply, shape) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, line_id, discord_id, json.dumps([ref]), guid, channel, kind, req["model"],
         None if think is None else int(bool(think)), json.dumps(req["options"]),
         json.dumps(req["messages"], ensure_ascii=False), req.get("reply") or "",
         json.dumps(req["shape"])))
    return cur.lastrowid


def add_ref(conn: sqlite3.Connection, row: int, *, line_id: int, discord_id: int | None) -> None:
    """A later line of the same generation (a second line, a burst's next message)."""
    ref = f"d:{discord_id}" if discord_id is not None else f"c:{line_id}"
    conn.execute("UPDATE bakeoff_turn SET refs = json_insert(refs, '$[#]', ?) WHERE id=?", (ref, row))


def prune(conn: sqlite3.Connection, *, now: float) -> int:
    if not available(conn):
        return 0
    return conn.execute("DELETE FROM bakeoff_turn WHERE ts < ?", (now - RETENTION_S,)).rowcount


def remaining(conn: sqlite3.Connection) -> int:
    if not available(conn):
        return 0
    return conn.execute("SELECT count(*) FROM bakeoff_turn").fetchone()[0]


def on_record(pipeline, turn, meta, *, line_id: int, discord_id: int | None, now: float,
              words: str) -> None:
    """Called by Pipeline._record inside the line's transaction, after the line is stored: after
    new model calls, the line (its `words`, before styling) picks the call it came from and
    writes its row; the generation's later lines add their refs. Never raises."""
    requests, row = meta.bakeoff_requests, meta.bakeoff_row
    if not requests and row is None:
        return
    if turn.channel.startswith("voice:"):  # spoken, never in the Turing corpus: of no use
        meta.bakeoff_requests.clear()
        return
    store = pipeline.store
    try:
        with store.transaction():
            if requests:
                req = chosen(requests, words)
                meta.bakeoff_row = None if req is None else record(
                    store.conn, req, ts=now, line_id=line_id, discord_id=discord_id,
                    guid=turn.speaker, channel=turn.channel, kind=turn.kind.value)
            else:
                add_ref(store.conn, row, line_id=line_id, discord_id=discord_id)
    except Exception as e:  # capture must never cost the turn its line
        _log.warning("bakeoff_capture_failed: guid=%d code=%s", turn.speaker, type(e).__name__)
        meta.bakeoff_row = None
    finally:
        meta.bakeoff_requests.clear()


async def stage_bakeoff(ctx) -> None:
    if ctx.done(STAGE, "prune"):
        return
    now = ctx.clock.now()
    gone = [0]

    def write(s):
        gone[0] = prune(s.conn, now=now)
    if ctx.commit(STAGE, "prune", write):
        log("bakeoff_pruned", night=ctx.night, rows=gone[0])


def attach(app):
    """At the mind's start: read the switch, prune, and add the nightly prune while there is
    anything to prune. The talk engine reads `bakeoff_capture`."""
    cfg = load(app.data_dir / "mind.toml")
    app.engine.bakeoff_capture = cfg.capture
    try:
        with app.store.transaction():
            gone = prune(app.store.conn, now=app.clock.now())
            left = remaining(app.store.conn)
    except sqlite3.Error as e:
        _log.warning("bakeoff_prune_failed: code=%s", type(e).__name__)
        gone, left = 0, 0
    if cfg.capture or left:
        app.night_runner.add_stage(STAGE, stage_bakeoff, after=AFTER, model=None)
    if cfg.capture or gone or left:
        log("bakeoff_capture", on=cfg.capture, pruned=gone, rows=left)
    return cfg
