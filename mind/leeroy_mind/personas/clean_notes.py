"""Relationship notes without the sentences the retired everyday-word slang left in them (owner's
live DM test, 2026-11-16).

Until the slang filter (talk/everyday.py, #320) the slang stage promoted everyday words and gave
their holders "Picked up 'X' from Y." memories; the night's notes writer (nightly/relations.py)
then wrote sentences from them into `relationship.notes`, which every prompt with the other end
in the talk shows: a note that she "picked up 'get', 'still' and 'this' from him", or that she
wants "to hear him say 'want' to me". `lexicon clean-junk` archived the memories; the notes kept
the sentences.

`strip` takes out of a note each sentence that cites an everyday word (talk.everyday's
everyday_reason, "common" or "typo") as picked up ("picked up 'X'", a list of quoted words
after it, any of them everyday) or as said to her ("say 'X' to me"); the rest of the note stays
as written. The night's notes writer passes its own notes through it, so no new note carries
one. `personas clean-notes` sweeps every relationship row of every member (members toward him,
toward each other, his rows too) with it: a read-only plan by default, `--go` with the mind
stopped writes the changes and a receipt (meta `personas:clean-notes:<stamp>`, each changed row's
note before and after), `--undo <stamp> --go` puts back each note still as the run left it.
Nothing here logs; the CLI prints counts and ids only."""
import json
import re
from dataclasses import dataclass

from leeroy_mind.talk.everyday import everyday_reason

RECEIPT = "personas:clean-notes:"
_Q = "'‘’`"
_QUOTED = re.compile(rf"[{_Q}]([^{_Q}\n]{{1,40}})[{_Q}]")
_PICKED = re.compile(
    rf"\bpick(?:ed|ing|s)?\s+up\s+((?:[{_Q}][^{_Q}\n]{{1,40}}[{_Q}]"
    rf"(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)?)+)", re.IGNORECASE)
_SAID_TO_ME = re.compile(rf"\bsays?\s+[{_Q}]([^{_Q}\n]{{1,40}})[{_Q}]\s+to\s+me\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _everyday(quoted: str) -> bool:
    words = quoted.split()
    return bool(words) and all(everyday_reason(w.strip(".,!?;:")) is not None for w in words)


def junk(sentence: str) -> bool:
    """Whether one sentence cites an everyday word as picked up or as said to her."""
    for m in _PICKED.finditer(sentence):
        if any(_everyday(q) for q in _QUOTED.findall(m.group(1))):
            return True
    return any(_everyday(q) for q in _SAID_TO_ME.findall(sentence))


def strip(note: str) -> tuple[str, int]:
    """The note without its junk sentences (`junk`), and how many went. A note with none comes
    back exactly as it was."""
    text = note or ""
    parts = _SENTENCE.split(text.strip())
    kept = [p for p in parts if not junk(p)]
    removed = len(parts) - len(kept)
    if not removed:
        return note, 0
    return " ".join(p for p in kept if p.strip()), removed


@dataclass(frozen=True)
class Change:
    a: int
    b: int
    before: str
    after: str
    removed: int  # sentences


def plan(conn) -> list[Change]:
    """Every relationship row whose note `strip` changes, by (a, b)."""
    out = []
    for a, b, notes in conn.execute("SELECT a, b, notes FROM relationship WHERE notes != '' "
                                    "ORDER BY a, b"):
        after, removed = strip(notes)
        if removed:
            out.append(Change(int(a), int(b), notes, after, removed))
    return out


def apply(conn, changes: list[Change], *, now: float, stamp: str) -> dict:
    """Write the plan in the caller's transaction, each row only while its note is still the one
    planned, and store the receipt RECEIPT + stamp; returns it."""
    rows = []
    for c in changes:
        if conn.execute("UPDATE relationship SET notes = ? WHERE a = ? AND b = ? AND notes = ?",
                        (c.after, c.a, c.b, c.before)).rowcount:
            rows.append([c.a, c.b, c.before, c.after])
    receipt = {"at": now, "rows": rows}
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (RECEIPT + stamp, json.dumps(receipt)))
    return receipt


def undo(conn, stamp: str, *, now: float) -> int:
    """Put back each note of the receipt RECEIPT + stamp that is still as the run left it; marks
    the receipt undone (never deletes it). How many rows went back. Raises KeyError for an unknown
    receipt and ValueError for one already undone."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (RECEIPT + stamp,)).fetchone()
    if row is None:
        raise KeyError(stamp)
    receipt = json.loads(row[0])
    if receipt.get("undone"):
        raise ValueError("already undone")
    done = 0
    for a, b, before, after in receipt["rows"]:
        done += conn.execute("UPDATE relationship SET notes = ? WHERE a = ? AND b = ? AND notes = ?",
                             (before, a, b, after)).rowcount
    receipt["undone"] = now
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (json.dumps(receipt), RECEIPT + stamp))
    return done


__all__ = ["Change", "RECEIPT", "apply", "junk", "plan", "strip", "undo"]
