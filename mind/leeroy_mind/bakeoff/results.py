"""`<out>/results.sqlite`: one bake-off's plan, every candidate's lines and the judge's verdicts.
Never mind.db. It holds generated lines, some explicit, and the masked context of his private
conversations: local only, like the scratch folder it defaults to. A run that stops part way
goes on from here when given the same --out."""
import json
import sqlite3
from pathlib import Path

NAME = "results.sqlite"
SCHEMA = """
CREATE TABLE IF NOT EXISTS run (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn (
  id INTEGER PRIMARY KEY,           -- the captured turn (bakeoff_turn.id)
  family TEXT NOT NULL,             -- game, private or discord
  kind TEXT NOT NULL,               -- whisper, dm, guild, discord ... (cli.KINDS)
  guid INTEGER NOT NULL,
  refs TEXT NOT NULL,               -- JSON: the corpus lines it became
  answers INTEGER NOT NULL,         -- 1: it answers his line just before it
  context TEXT NOT NULL             -- JSON: the conversation before it, masked as the judge masks
);
CREATE TABLE IF NOT EXISTS window (
  unit TEXT PRIMARY KEY, family TEXT NOT NULL, refs TEXT NOT NULL, turns TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS result (
  candidate TEXT NOT NULL,          -- a candidate's name, or sent / live (the baselines)
  turn INTEGER NOT NULL,
  status TEXT NOT NULL,             -- ok, silent or error
  code TEXT,                        -- an error's class
  text TEXT,                        -- the reply as the model gave it (its thinking never)
  lines TEXT NOT NULL,              -- JSON: the lines as typed (repaired and styled)
  masked TEXT NOT NULL,             -- JSON: the same, names masked as the judge masks
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  eval_count INTEGER NOT NULL DEFAULT 0,
  seconds REAL,
  PRIMARY KEY (candidate, turn));
CREATE TABLE IF NOT EXISTS judged (
  candidate TEXT NOT NULL, unit TEXT NOT NULL, family TEXT NOT NULL,
  status TEXT NOT NULL,             -- ok, invalid, skipped or error
  speakers INTEGER NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
  his INTEGER NOT NULL DEFAULT 0, his_correct INTEGER NOT NULL DEFAULT 0,
  tells TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (candidate, unit));
"""


def connect(out: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(out) / NAME, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def open_existing(out: Path) -> sqlite3.Connection:
    path = Path(out) / NAME
    if not path.is_file():
        raise FileNotFoundError("no results.sqlite in that folder: run `bakeoff run` first")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM run WHERE key=?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def put(conn, key: str, value) -> None:
    conn.execute("INSERT INTO run(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))


def put_result(conn, candidate: str, turn: int, *, status: str, lines: list[str],
               masked: list[str], text: str | None = None, code: str | None = None,
               thinking_tokens: int = 0, eval_count: int = 0, seconds: float | None = None) -> None:
    conn.execute(
        "INSERT INTO result(candidate, turn, status, code, text, lines, masked, thinking_tokens, "
        "eval_count, seconds) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate, turn) DO UPDATE "
        "SET status=excluded.status, code=excluded.code, text=excluded.text, lines=excluded.lines, "
        "masked=excluded.masked, thinking_tokens=excluded.thinking_tokens, "
        "eval_count=excluded.eval_count, seconds=excluded.seconds",
        (candidate, turn, status, code, text, json.dumps(lines, ensure_ascii=False),
         json.dumps(masked, ensure_ascii=False), thinking_tokens, eval_count, seconds))


def results(conn, candidate: str) -> dict[int, sqlite3.Row]:
    return {r["turn"]: r for r in conn.execute("SELECT * FROM result WHERE candidate=?",
                                                (candidate,))}


def candidates(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT candidate FROM result ORDER BY candidate")]
