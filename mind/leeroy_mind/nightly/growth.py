"""Persona growth (P1c Task 11; the owner's beyond-Smallville item 5): the slow change of who a
member is, kept as self-notes in persona_state, with every change in the persona ledger and
every ledger row undoable (owner, 2026-10-30: "visible and undoable on the dashboard").

A fixed core persona keeps growth from becoming drift (controller ruling B2). A self-insight
from the night's reflection (Task 11) becomes a note only when:
- it rests on enough (eligible): three cited items or more, over two nights or more; none of
  them bot talk (BOT_Q); none with him among its people when the member is a woman with an
  attitude toward him (romance with him never rewrites who a member is); and nothing told in
  confidence or kept private: no secret root, no cited item that derives private or secret, no
  disclosure's memory (Task 8's disclose: key). A confession or a disclosure never becomes a
  self-note (the cognition memo's vector 3 caveat on drift);
- the reflection model, shown the core persona (core_text), finds it consistent with who the
  member is or an extension of it (judge_core). One that contradicts the core goes to the
  owner's review queue instead, and changes nothing.

At most NOTES_CAP notes: a new one pushes the oldest out (add_note). Every note added, pushed
out or taken back by a drift rollback (Tasks 22 and 24) is a ledger row, and undo reverses one
row inside a transaction; Task 12 adds the branch for its stance_band rows.

Ledger and review texts are the notes' own texts. A note's text passed grounding rule 4 (no
real name, by P1b's matcher) before it got here, so the callers' screening is already done; the
reasons callers pass are theirs to screen."""
import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from leeroy_mind.clock import night_of
from leeroy_mind.memory.privacy import derive_scope
from leeroy_mind.nightly.evidence import Evidence
from leeroy_mind.nightly.ground import Claim
from leeroy_mind.nightly.knowledge import is_secret, roots
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.store import enqueue_review, ledger_add
from leeroy_mind.personas.schema import Persona, persona_to_dict
from leeroy_mind.storage.rows import PersonaStateRow
from leeroy_mind.storage.store import Store

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

PROMPT = "core_judge"  # the owner's overlay is prompts/nightly_core_judge.txt
VERDICTS = ("consistent", "extension", "contradicts")
NOTES_CAP = 5        # self-notes a member holds; a new one pushes the oldest out
MIN_REFS = 3         # cited items a self-note rests on, at least...
MIN_NIGHTS = 2       # ...over this many nights (06:00 to 06:00: one raid past midnight is one)
DISCLOSE_KEY = "disclose:"  # Task 8's memory of a disclosure he listened to
ROLLBACK_REASON = "drift rollback"
# What core_text leaves out: not who the member is (lines, bits, the secret, the interview and
# typing), and what the night's other passes keep (looks and disclosure layers, Task 7).
NOT_CORE = ("sample_lines", "bits", "secret", "mock_interview", "typing", "appearance",
            "disclosures")
NOT_CORE_ROMANCE = ("drawn_to", "turned_off_by")  # her tastes (Task 7's carry, row 56)

CORE_SCHEMA = schema("core_judge", {
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "why": {"type": "string", "maxLength": 120}})
BOT_Q = re.compile(r"\b(are|r)\s+(you|u)\s+(a\s+)?(bot|ai|real|npc|script)", re.I)
CORE_PROMPT = ("You know who {name} is at heart. You judge whether something {name} has come "
               "to see in themselves fits who they are.")
CORE_ASK = (
    "Who {name} is:\n{core}\n\n"
    "What {name} has come to see in themselves: {note}\n\n"
    "verdict: \"consistent\" when it fits who {name} is, \"extension\" when it adds something "
    "new that goes against none of it, \"contradicts\" when it goes against who {name} is. "
    "why: a few words.")
_MEMORY = re.compile(r"memory:([0-9]+)")


# -- the core and who may grow -------------------------------------------------------------------

def core_text(p: Persona) -> str:
    """The persona as JSON, less NOT_CORE and her tastes: who the member is, fixed."""
    d = persona_to_dict(p)
    for key in NOT_CORE:
        d.pop(key, None)
    if isinstance(d.get("romance"), dict):
        for key in NOT_CORE_ROMANCE:
            d["romance"].pop(key, None)
    return json.dumps(d)


def eligible(claim: Claim, ev: Evidence, *, owner_has_attitude: bool, human: int,
             store: Store) -> bool:
    """Whether a self-insight rests on enough to become a self-note (the module docstring).
    Dates are nights in the mind clock's zone; a cited item derives its scope as P1b's writer
    reads it (a private or secret memory, or a whisper line), and a secret root is the ledger's
    (is_secret over its roots: a confidential fact, a private encounter)."""
    refs = list(dict.fromkeys(claim.evidence))
    if len(refs) < MIN_REFS or any(ref not in ev for ref in refs):
        return False
    items = [ev[ref] for ref in refs]
    tz = store.clock.local().tzinfo
    if len({night_of(datetime.fromtimestamp(i.t, tz)) for i in items}) < MIN_NIGHTS:
        return False
    if any(BOT_Q.search(i.text) for i in items):
        return False
    if owner_has_attitude and any(human in i.actors for i in items):
        return False
    with store.transaction():
        if any(derive_scope(store.conn, claim.owner, [ref]).scope != "open" for ref in refs):
            return False
    ids = [int(m.group(1)) for ref in refs if (m := _MEMORY.fullmatch(ref))]
    if any((m.key or "").startswith(DISCLOSE_KEY) for m in store.get_memories(ids)):
        return False
    return not any(is_secret(store, root) for root in sorted(roots(store, refs)))


async def judge_core(ctx: "NightContext", guid: int, note: str) -> tuple[str, str]:
    """The reflection model's verdict on a self-insight against the member's fixed core (one
    JSON call, CORE_SCHEMA): (verdict, why). The why is apply_note's review reason, so both come
    back (the plan's `-> str` held only the verdict). A failed request is the unit's failure."""
    name = ctx.name(guid)
    system = system_prompt(PROMPT, CORE_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=name)
    user = CORE_ASK.format(name=name, core=core_text(ctx.persona(guid)), note=note)
    reply = await ctx.reasoner.json(system, user, CORE_SCHEMA)
    return reply["verdict"], reply["why"]


# -- the notes -----------------------------------------------------------------------------------

def add_note(state: PersonaStateRow, note: dict, *,
             cap: int = NOTES_CAP) -> tuple[PersonaStateRow, dict | None]:
    """`state` with `note` last, and the note it pushed out: when the list is full, the oldest
    (the first: notes are kept in the order they joined) is dropped and returned."""
    notes = [*state.self_notes, dict(note)]
    dropped = notes.pop(0) if len(notes) > cap else None
    return replace(state, self_notes=tuple(notes)), dropped


def _store_note(store: Store, guid: int, note: dict, now: float) -> None:
    """`note` added to the member's notes, and a note_dropped row for the one it pushed out."""
    state, dropped = add_note(store.get_state(guid), note)
    store.put_state(replace(state, updated=int(now)))
    if dropped is not None:
        ledger_add(store, guid=guid, kind="note_dropped", text=dropped["text"],
                   evidence=dropped.get("evidence", []), now=now)


def apply_note(store: Store, guid: int, claim: Claim, verdict: str, why: str,
               now: float) -> None:
    """The core judge's verdict on a self-insight, inside the caller's transaction (it joins
    it). consistent or extension: the note {"text", "evidence", "added": now} is added, with a
    note_added ledger row, and note_dropped for a note it pushed out. contradicts: the owner's
    review queue gets it (kind self_note, reason `why`), and nothing else changes."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, not {verdict!r}")
    with store.transaction():
        if verdict == "contradicts":
            enqueue_review(store, guid=guid, kind="self_note", text=claim.text,
                           evidence=claim.evidence, reason=why, now=now)
            return
        ledger_add(store, guid=guid, kind="note_added", text=claim.text,
                   evidence=claim.evidence, now=now)
        _store_note(store, guid, {"text": claim.text, "evidence": list(claim.evidence),
                                  "added": now}, now)


def rollback_week(store: Store, guid: int, since: float, now: float) -> int:
    """Take back the member's notes added at `since` or later (a week of drift: Tasks 22 and 24):
    each is queued for the owner as kind drift_rollback and ledgered note_rolled_back, whose ref
    is that queue row (an undo resolves it). Returns how many; joins the caller's transaction."""
    with store.transaction():
        state = store.get_state(guid)
        gone = [n for n in state.self_notes if n.get("added", 0) >= since]
        if not gone:
            return 0
        store.put_state(replace(state, updated=int(now), self_notes=tuple(
            n for n in state.self_notes if n.get("added", 0) < since)))
        for n in gone:
            review = enqueue_review(store, guid=guid, kind="drift_rollback", text=n["text"],
                                    evidence=n.get("evidence", []), reason=ROLLBACK_REASON,
                                    now=now)
            ledger_add(store, guid=guid, kind="note_rolled_back", text=n["text"],
                       evidence=n.get("evidence", []), ref=review, now=now)
        return len(gone)


# -- undo ----------------------------------------------------------------------------------------

def _same(note: dict, row: sqlite3.Row) -> bool:
    return note.get("text") == row["text"] and list(note.get("evidence", [])) == json.loads(
        row["evidence"])


def _take_back(store: Store, row: sqlite3.Row, now: float) -> bool:
    """note_added: the note it added, if it is still there (the one added at the row's time
    first, when two read the same)."""
    state = store.get_state(row["guid"])
    matches = [i for i, n in enumerate(state.self_notes) if _same(n, row)]
    if not matches:
        return False
    at = next((i for i in matches if state.self_notes[i].get("added") == row["ts"]), matches[0])
    store.put_state(replace(state, updated=int(now), self_notes=tuple(
        n for i, n in enumerate(state.self_notes) if i != at)))
    return True


def _added_at(store: Store, row: sqlite3.Row) -> float:
    """When the note a note_dropped or note_rolled_back row took out had been added: its
    newest note_added row by then, else the row's own time."""
    r = store.conn.execute(
        "SELECT ts FROM persona_ledger WHERE guid = ? AND kind = 'note_added' AND text = ? "
        "AND evidence = ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
        (row["guid"], row["text"], row["evidence"], row["ts"])).fetchone()
    return r["ts"] if r is not None else row["ts"]


def _put_back(store: Store, row: sqlite3.Row, now: float) -> bool:
    """note_dropped or note_rolled_back: the note back through add_note, with the time it was
    first added, unless it is there already; a note this pushes out is ledgered note_dropped. A
    rollback's queue row is resolved."""
    if any(_same(n, row) for n in store.get_state(row["guid"]).self_notes):
        return False
    _store_note(store, row["guid"], {"text": row["text"], "evidence": json.loads(row["evidence"]),
                                     "added": _added_at(store, row)}, now)
    if row["kind"] == "note_rolled_back" and row["ref"] is not None:
        _resolve(store, row["ref"], now)
    return True


def _resolve(store: Store, review: int | None, now: float) -> bool:
    """drift_flag (Task 22): its review_queue row resolved, if it is not yet."""
    return review is not None and store.conn.execute(
        "UPDATE review_queue SET resolved = ? WHERE id = ? AND resolved IS NULL",
        (now, review)).rowcount == 1


def undo(store: Store, ledger_id: int, *, now: float, personas=None) -> bool:
    """Reverse one persona ledger row, inside a transaction (it joins the caller's):
    - note_added: the note is removed, if it is still there;
    - note_dropped, note_rolled_back: the note is put back through add_note (a note this pushes
      out is ledgered note_dropped), unless it is there already;
    - drift_flag (Task 22): its review_queue row is resolved;
    - stance_band: restore the prior stance and its exact score; first sights and hand moves
      are not reversed. An undo row is never undone.
    What was reversed writes an undo row with ref = ledger_id. False when the row is not there,
    was undone already, or nothing is left to reverse; then nothing is written."""
    with store.transaction():
        row = store.conn.execute("SELECT * FROM persona_ledger WHERE id = ?",
                                 (ledger_id,)).fetchone()
        if row is None or store.conn.execute(
                "SELECT 1 FROM persona_ledger WHERE kind = 'undo' AND ref = ? LIMIT 1",
                (ledger_id,)).fetchone() is not None:
            return False
        kind = row["kind"]
        if kind == "note_added":
            done = _take_back(store, row, now)
        elif kind in ("note_dropped", "note_rolled_back"):
            done = _put_back(store, row, now)
        elif kind == "drift_flag":
            done = _resolve(store, row["ref"], now)
        elif kind == "stance_band":
            from leeroy_mind.nightly.attraction import restore_stance
            done = restore_stance(store, row, now=now)
        else:
            done = False
        if done:
            ledger_add(store, guid=row["guid"], kind="undo", text=f"undid {kind}: {row['text']}",
                       evidence=json.loads(row["evidence"]), ref=ledger_id, now=now)
    if done and kind == "stance_band" and personas is not None:
        personas.invalidate(row["guid"])
    return done
