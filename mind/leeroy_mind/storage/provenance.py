"""Persisted evidence edges shared by the critic, memory writers and repair tool.

Only edges live here. Eligibility, privacy and comparable row snapshots remain with
the audit readers. Reachability includes rejected memories: their stored evidence
can become relevant again when a keyed memory is rewritten.
"""
import json
import re
from dataclasses import dataclass

MAX_NODES = 5000  # Preserve the critic/merge walk budget; refuse an unproved addition.
_REF = re.compile(r"(memory|chat|story|chronicle|feud|joke|rule:fact):([0-9]+)")


class ProvenanceError(ValueError):
    """Stored provenance cannot be traced completely."""


class WalkLimit(ProvenanceError):
    """Reachability exceeded its bounded work allowance."""


@dataclass(frozen=True)
class Edges:
    children: tuple = ()
    extras: tuple = ()


def canonical(ref):
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return f"{match.group(1)}:{int(match.group(2))}" if match else ref


def memory_ids(raw):
    values = json.loads(raw)
    if not isinstance(values, list) or any(type(i) is not int or i <= 0 for i in values):
        raise ProvenanceError("Invalid critic source memory IDs")
    return values


def _refs(raw, code="Invalid critic source references"):
    values = json.loads(raw)
    if not isinstance(values, list) or any(not isinstance(r, str) for r in values):
        raise ProvenanceError(code)
    return tuple(values)


def paired(conn, line):
    return conn.execute("SELECT * FROM utterance WHERE guid=? AND channel=? AND text=? "
        "AND to_guid=? AND ts BETWEEN ? AND ? ORDER BY abs(ts-?),id LIMIT 1",
        (line["from_guid"], line["channel"], line["text"], line["to_guid"],
         line["ts"] - 2, line["ts"] + 2, line["ts"])).fetchone()


def edges(conn, ref, *, recalls=True, audit=False, memory_evidence=None):
    """Read all direct edges. Unknown/missing roots have none; audits check existence.

    ``recalls=False`` is the old persisted-node view used by privacy walkers,
    which add their own paired-chat checks. The full graph is the default.
    Audits also require a non-dropped pair and retain the exact comparable extras.
    A repair can supply its proposed memory evidence without writing the database.
    """
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return Edges()
    kind, ident = match.group(1), int(match.group(2))
    if kind == "memory" and memory_evidence is not None:
        return Edges(tuple(memory_evidence.get(ident, ())))
    table = {"chat": "chat_line", "joke": "lexicon", "rule:fact": "fact"}.get(kind, kind)
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
    if row is None:
        return Edges()
    if kind in {"memory", "story", "chronicle", "feud"}:
        return Edges(_refs(row["evidence"]))
    if kind == "rule:fact":
        return Edges((row["root"],))
    if kind == "joke":
        children = (row["origin_ref"],)
        if not recalls:
            return Edges(children)
        source = conn.execute("SELECT value FROM meta WHERE key=?",
                              (f"nightly:lexicon-sources:{ident}",)).fetchone()
        retained = (source[0] if source else None) or "[]"
        return Edges((*children, *_refs(retained, "Invalid critic term provenance")),
                     (retained,))
    if not recalls:
        return Edges()
    if row["origin"] != "mind":
        return Edges(extras=(tuple(row),))
    pair = paired(conn, row)
    if audit and (pair is None or pair["dropped"] is not None):
        raise ProvenanceError("Unpaired critic source output")
    if pair is None:
        return Edges()
    return Edges(tuple(f"memory:{i}" for i in memory_ids(pair["memory_ids"])),
                 (tuple(row), tuple(pair)))


def reaches(children, start, target, *, limit=MAX_NODES):
    """Bounded iterative graph walk; an exhausted budget is never proof of safety."""
    seen, todo = set(), [start]
    target = canonical(target)
    while todo:
        ref = canonical(todo.pop())
        if ref == target:
            return True
        if ref in seen:
            continue
        if len(seen) >= limit:
            raise WalkLimit("Provenance walk limit")
        seen.add(ref)
        todo.extend(children(ref))
    return False
