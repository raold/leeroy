"""Capture: with `[bakeoff] capture = true` the talk pipeline keeps each sent model turn's exact
request in `bakeoff_turn` (migration *_bakeoff.sql).

Every model call a generation makes is kept on its turn (`keep`: the first always, then the last
KEPT - 1, each numbered). When its line is recorded, the call whose reply the sent words came
from (`chosen`) is written with its number (`attempt`) and the number of calls made (`calls`),
and, when that is not the first call, the first call too: the draft before any retry hint, so a
bake-off can ask the candidates what the live model was first asked. The write runs inside its
own savepoint of the line's transaction; a failure is logged by its code and swallowed, so a turn
is never slowed, changed or lost by it (only SQLite's own rollback of the whole transaction is
raised again, as it would be without capture).

Rows older than RETENTION_S are deleted: at the mind's start whatever the switch says, and each
night by the model-free stage `bakeoff`, which is attached while the switch is on or captured rows
remain. Logs carry ids and counts only."""
import json
import logging
import re
import sqlite3

from leeroy_mind.logs import log

from .config import load

RETENTION_S = 14 * 86400
STAGE = "bakeoff"
AFTER = "threads"
KEPT = 8  # the model calls a generation keeps for its line: the first, then the latest
_log = logging.getLogger("leeroy_mind")


def available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bakeoff_turn'"
                        ).fetchone() is not None


def request(model: str, messages: list[dict], options: dict, think, reply: str, *,
            max_lines: int, words: int, two_lines: bool, done_reason: str | None = None,
            beat: str | None = None) -> dict:
    """What one model call asked and answered, as the pipeline keeps it until the line is sent.
    A private answer's `beat` (its lines, talk/beat.py) is taken back out of the messages, so its
    words never reach a store; only that there was one and its want code are kept."""
    beat = beat if isinstance(beat, str) and beat else None
    return {"model": model, "messages": without_beat(messages, beat), "options": dict(options),
            "think": think, "reply": reply,
            "done_reason": done_reason if isinstance(done_reason, str) else None,
            "beat": beat is not None, "want": want_of(beat),
            "shape": {"max_lines": int(max_lines), "words": int(words or 0),
                      "two_lines": bool(two_lines)}}


def without_beat(messages: list[dict], beat: str | None) -> list[dict]:
    """The messages as they were before the beat's lines: the pipeline appends a newline and the
    beat to the instruction (the last user message of the composed prompt) before anything else,
    and later hints come after it, so its first appearance is taken out once, from the last user
    message that holds it (a guard's rewrite adds messages after that one)."""
    out = [dict(m) for m in messages]
    if not beat:
        return out
    suffix = "\n" + beat
    for m in reversed(out):
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, str) and suffix in content:
            i = content.index(suffix)
            m["content"] = content[:i] + content[i + len(suffix):]
            break
    return out


_WANT = re.compile(r"^You want to: (.+)$", re.MULTILINE)


def want_of(beat: str | None) -> str | None:
    """The beat's want code, read back from its "You want to:" line (talk/beat.py WANTS)."""
    if not beat or not (m := _WANT.search(beat)):
        return None
    try:
        from leeroy_mind.talk.beat import WANTS
    except ImportError:
        return None
    said = m[1].strip()
    for code, words in WANTS.items():
        head, named, tail = words.partition("{human}")
        if said == words or (named and said.startswith(head) and said.endswith(tail)
                             and len(said) > len(head) + len(tail)):
            return code
    return None


def keep(meta, req: dict) -> None:
    """Number the call and keep it: the generation's first stays, the oldest later one goes."""
    kept = meta.bakeoff_requests
    req["index"] = kept[-1]["index"] + 1 if kept else 0
    kept.append(req)
    if len(kept) > KEPT:
        del kept[1]


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


def _think(req: dict):
    think = req.get("think")
    return None if think is None else int(bool(think))


def record(conn: sqlite3.Connection, sent: dict, first: dict | None, *, calls: int, ts: float,
           line_id: int, discord_id: int | None, guid: int, channel: str, kind: str) -> int:
    """One captured turn, its first line's ref in refs: the sent call, and the generation's first
    call when that is another one. The new row's id."""
    ref = f"d:{discord_id}" if discord_id is not None else f"c:{line_id}"
    f = first or {}
    cur = conn.execute(
        "INSERT INTO bakeoff_turn(ts, line_id, discord_id, refs, guid, channel, kind, calls, attempt, "
        "beat, want, model, think, options, messages, reply, done_reason, first_model, first_think, "
        "first_options, first_messages, first_reply, first_done_reason, shape) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, line_id, discord_id, json.dumps([ref]), guid, channel, kind, calls,
         int(sent.get("index", 0)), int(bool(sent.get("beat"))), sent.get("want"),
         sent["model"], _think(sent), json.dumps(sent["options"]),
         json.dumps(sent["messages"], ensure_ascii=False), sent.get("reply") or "",
         sent.get("done_reason"),
         f.get("model"), _think(f) if first else None,
         json.dumps(f["options"]) if first else None,
         json.dumps(f["messages"], ensure_ascii=False) if first else None,
         (f.get("reply") or "") if first else None, f.get("done_reason"),
         json.dumps(sent["shape"])))
    return cur.lastrowid


def add_ref(conn: sqlite3.Connection, row: int, *, line_id: int, discord_id: int | None) -> None:
    """A later line of the same generation (a second line, a burst's next message)."""
    ref = f"d:{discord_id}" if discord_id is not None else f"c:{line_id}"
    conn.execute("UPDATE bakeoff_turn SET refs = json_insert(refs, '$[#]', ?) WHERE id=?", (ref, row))


def prune(conn: sqlite3.Connection, *, now: float) -> int:
    if not available(conn):
        return 0
    return conn.execute("DELETE FROM bakeoff_turn WHERE ts < ?", (now - RETENTION_S,)).rowcount


def remaining(conn: sqlite3.Connection) -> bool:
    if not available(conn):
        return False
    return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM bakeoff_turn)").fetchone()[0])


def on_record(pipeline, turn, meta, *, line_id: int, discord_id: int | None, now: float,
              words: str) -> None:
    """Called by Pipeline._record inside the line's transaction, after the line is stored: after
    new model calls, the line (its `words`, before styling) picks the call it came from and
    writes its row; the generation's later lines add their refs. Never raises, but for SQLite's
    own rollback of the line's whole transaction."""
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
                sent = chosen(requests, words)
                first = requests[0] if sent is not None and sent is not requests[0] else None
                meta.bakeoff_row = None if sent is None else record(
                    store.conn, sent, first, calls=requests[-1]["index"] + 1, ts=now,
                    line_id=line_id, discord_id=discord_id, guid=turn.speaker,
                    channel=turn.channel, kind=turn.kind.value)
            else:
                add_ref(store.conn, row, line_id=line_id, discord_id=discord_id)
    except Exception as e:  # capture must never cost the turn its line
        if isinstance(e, sqlite3.Error) and not store.conn.in_transaction:
            raise  # SQLite rolled the line's own transaction back: not capture's to hide
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
        gone, left = 0, False
    if cfg.capture or left:
        app.night_runner.add_stage(STAGE, stage_bakeoff, after=AFTER, model=None)
    if cfg.capture or gone or left:
        log("bakeoff_capture", on=cfg.capture, pruned=gone, left=int(left))
    return cfg
