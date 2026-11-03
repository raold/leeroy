"""Bounded, direct-input Realm exclusion inside an existing Store unit.

An admitted manifest is not a privacy or publication proof. Ordinary input
eligibility and permanent write-side lineage remain the caller's responsibility.
"""
from collections.abc import Mapping
import json
import re
import unicodedata

from leeroy_mind.human import HumanIdentity
from leeroy_mind.realm.records import RealmInputs, Rejected
from leeroy_mind.storage.store import Store


_UNIT_REQUIRED = "Realm input eligibility requires Store.transaction()"
_MAX_REFS = 256
_MAX_REF_BYTES = 128
_MAX_TOTAL_BYTES = 32768
_MAX_EVIDENCE_BYTES = 65536
_SQLITE_MAX = 9223372036854775807
_NUMBERED = frozenset((
    "memory", "chat", "utterance", "event", "session", "story", "chronicle",
    "encounter", "beat", "life", "away", "feud", "mentor", "award", "joke",
))
_OPAQUE = frozenset(("rule", "owner"))
_DECIMAL = re.compile(r"[1-9][0-9]*")


def _ref_size(value, *, invalid: str, oversized: str) -> int | Rejected:
    if type(value) is not str or not value:
        return Rejected(invalid)
    # UTF-8 needs at least one byte per code point; avoid encoding huge inputs.
    if len(value) > _MAX_REF_BYTES:
        return Rejected(oversized)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return Rejected(invalid)
    if size > _MAX_REF_BYTES:
        return Rejected(oversized)
    if any(char.isspace() or unicodedata.category(char) in ("Cc", "Cf", "Cs")
           for char in value):
        return Rejected(invalid)
    return size


def _row_id(value: str) -> int | None:
    if len(value) > 19 or _DECIMAL.fullmatch(value) is None:
        return None
    identifier = int(value)
    return identifier if identifier <= _SQLITE_MAX else None


def _realm_id(ref: str) -> int | None:
    return _row_id(ref[11:]) if ref.startswith("realm:fact:") else None


def _collect_refs(refs) -> tuple[str, ...] | Rejected:
    if isinstance(refs, (str, bytes, Mapping)):
        return Rejected("invalid_input")
    try:
        iterator = iter(refs)
    except TypeError:
        return Rejected("invalid_input")
    shown = set()
    total = 0
    for count, ref in enumerate(iterator, 1):
        if count > _MAX_REFS:
            return Rejected("graph_limit")
        size = _ref_size(ref, invalid="invalid_input", oversized="graph_limit")
        if type(size) is Rejected:
            return size
        total += size
        if total > _MAX_TOTAL_BYTES:
            return Rejected("graph_limit")
        namespace, separator, suffix = ref.partition(":")
        if (not separator
                or (namespace in _NUMBERED and _row_id(suffix) is None)
                or (namespace in _OPAQUE and not suffix)
                or (namespace == "realm" and _realm_id(ref) is None)
                or namespace not in _NUMBERED | _OPAQUE | {"realm"}):
            return Rejected("invalid_input")
        shown.add(ref)
    return tuple(sorted(shown))


def _nonfinite(raw):
    raise ValueError("invalid evidence constant")


def _memory_rejection(store: Store, identifier: int) -> Rejected | None:
    row = store.conn.execute("SELECT evidence FROM memory WHERE id=?", (identifier,)).fetchone()
    if row is None:
        return Rejected("missing_row")
    if store.conn.execute(
            "SELECT 1 FROM realm_memory_dependency WHERE memory=? LIMIT 1",
            (identifier,)).fetchone() is not None:
        return Rejected("unsupported_policy")
    raw = row["evidence"]
    if type(raw) is not str:
        return Rejected("invalid_row")
    if len(raw) > _MAX_EVIDENCE_BYTES:
        return Rejected("graph_limit")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        return Rejected("invalid_row")
    if size > _MAX_EVIDENCE_BYTES:
        return Rejected("graph_limit")
    try:
        evidence = json.loads(raw, parse_constant=_nonfinite)
    except (ValueError, RecursionError):
        return Rejected("invalid_row")
    if type(evidence) is not list:
        return Rejected("invalid_row")
    for child in evidence:
        size = _ref_size(child, invalid="invalid_row", oversized="invalid_row")
        if type(size) is Rejected:
            return size
        if child.startswith("realm:"):
            return Rejected("unsupported_policy" if _realm_id(child) is not None else "invalid_row")
    return None


def _speech_rejection(store: Store, namespace: str, identifier: int) -> Rejected | None:
    # These are fixed table/column pairs, never input-derived SQL identifiers.
    if namespace == "chat":
        marker = "SELECT 1 FROM realm_speech_line WHERE chat_line=? LIMIT 1"
        source = "SELECT 1 FROM chat_line WHERE id=?"
    else:
        marker = "SELECT 1 FROM realm_speech_line WHERE utterance=? LIMIT 1"
        source = "SELECT 1 FROM utterance WHERE id=?"
    if store.conn.execute(marker, (identifier,)).fetchone() is not None:
        return Rejected("unsupported_policy")
    if store.conn.execute(source, (identifier,)).fetchone() is None:
        return Rejected("missing_row")
    return None


def realm_inputs_in_unit(store: Store, refs, *, human: HumanIdentity,
                         mode: str = "exclude") -> RealmInputs | Rejected:
    """Classify the complete shown reference set without writing or committing."""
    if store.in_unit is not True:
        raise RuntimeError(_UNIT_REQUIRED)
    if type(human) is not HumanIdentity or type(mode) is not str:
        return Rejected("invalid_input")
    if mode == "memory":
        return Rejected("unsupported_policy")
    if mode != "exclude":
        return Rejected("invalid_input")
    shown = _collect_refs(refs)
    if type(shown) is Rejected:
        return shown
    for ref in shown:
        namespace, _, suffix = ref.partition(":")
        if namespace == "realm":
            return Rejected("unsupported_policy")
        if namespace == "memory":
            rejection = _memory_rejection(store, int(suffix))
        elif namespace in ("chat", "utterance"):
            rejection = _speech_rejection(store, namespace, int(suffix))
        else:
            continue
        if rejection is not None:
            return rejection
    return RealmInputs(refs=shown, stamps=(), mode="exclude")


def revalidate_realm_inputs_in_unit(store: Store, inputs: RealmInputs, *,
                                   human: HumanIdentity) -> bool:
    """Repeat complete direct classification; never refresh or filter a manifest."""
    if store.in_unit is not True:
        raise RuntimeError(_UNIT_REQUIRED)
    if (type(human) is not HumanIdentity or type(inputs) is not RealmInputs
            or type(getattr(inputs, "mode", None)) is not str or inputs.mode != "exclude"
            or type(getattr(inputs, "stamps", None)) is not tuple or inputs.stamps != ()
            or type(getattr(inputs, "refs", None)) is not tuple):
        return False
    current = realm_inputs_in_unit(store, inputs.refs, human=human)
    return type(current) is RealmInputs and current == inputs
