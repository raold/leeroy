"""Bounded Realm exclusion and exact public-memory reads in a Store unit.

An admitted manifest is not a privacy or publication proof. Ordinary input
eligibility and permanent write-side lineage remain the caller's responsibility.
"""
from collections.abc import Mapping
import json
import re
import unicodedata

from leeroy_mind.human import HumanIdentity
from leeroy_mind.realm.records import RealmFactReadStamp, RealmInputs, RealmReadStamp, Rejected
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
    if not isinstance(store, Store) or store.in_unit is not True:
        raise RuntimeError(_UNIT_REQUIRED)
    if type(human) is not HumanIdentity or type(mode) is not str:
        return Rejected("invalid_input")
    if mode not in ("exclude", "memory"):
        return Rejected("invalid_input")
    shown = _collect_refs(refs)
    if type(shown) is Rejected:
        return shown
    related = []
    for ref in shown:
        namespace, _, suffix = ref.partition(":")
        if namespace == "realm":
            if mode == "exclude":
                return Rejected("unsupported_policy")
            related.append(ref)
            continue
        if namespace == "memory":
            rejection = _memory_rejection(store, int(suffix))
            if mode == "memory" and rejection == Rejected("unsupported_policy"):
                related.append(ref)
                continue
        elif namespace in ("chat", "utterance"):
            rejection = _speech_rejection(store, namespace, int(suffix))
        else:
            continue
        if rejection is not None:
            return rejection
    if mode == "exclude":
        return RealmInputs(refs=shown, stamps=(), mode="exclude")
    # Keep exclusion imports and unrelated legacy ancestry inert.
    from .provenance import audit_realm_reads_in_unit
    audited = audit_realm_reads_in_unit(store, tuple(related), human=human)
    if type(audited) is Rejected:
        return audited
    stamps, fact_stamps = audited
    return RealmInputs(shown, stamps, mode, fact_stamps)


def _manifest_shape(inputs):
    if (type(inputs) is not RealmInputs
            or type(getattr(inputs, 'mode', None)) is not str
            or inputs.mode not in ('exclude', 'memory')
            or any(type(getattr(inputs, field, None)) is not tuple
                   for field in ('refs', 'stamps', 'fact_stamps'))):
        return False
    if inputs.mode == 'exclude' and (inputs.stamps or inputs.fact_stamps):
        return False
    try:
        # Constructors validate bounded shape only. Rebuild exact element types
        # here to reject forged/mutated frozen records before they reach SQL.
        for stamp in inputs.stamps:
            if type(stamp) is not RealmReadStamp or type(stamp.fact_ids) is not tuple:
                return False
            if stamp != RealmReadStamp(stamp.memory_id, stamp.memory_sha256, stamp.fact_ids, stamp.proof_sha256):
                return False
        for stamp in inputs.fact_stamps:
            if type(stamp) is not RealmFactReadStamp:
                return False
            if stamp != RealmFactReadStamp(stamp.fact_id, stamp.revision, stamp.fact_sha256, stamp.proof_sha256):
                return False
        return inputs == RealmInputs(inputs.refs, inputs.stamps, inputs.mode, inputs.fact_stamps)
    except (AttributeError, TypeError, ValueError):
        return False


def revalidate_realm_inputs_in_unit(store: Store, inputs: RealmInputs, *,
                                   human: HumanIdentity) -> bool:
    """Repeat the complete read; never refresh or filter an earlier manifest."""
    if not isinstance(store, Store) or store.in_unit is not True:
        raise RuntimeError(_UNIT_REQUIRED)
    if type(human) is not HumanIdentity or not _manifest_shape(inputs):
        return False
    current = realm_inputs_in_unit(store, inputs.refs, human=human, mode=inputs.mode)
    return type(current) is RealmInputs and current == inputs


def merge_realm_inputs_in_unit(store: Store, inputs: tuple[RealmInputs, ...], *,
                              human: HumanIdentity) -> RealmInputs | Rejected:
    """Preserve all shown refs and snapshots, then audit their entire union."""
    if not isinstance(store, Store) or store.in_unit is not True:
        raise RuntimeError(_UNIT_REQUIRED)
    if (type(human) is not HumanIdentity or type(inputs) is not tuple
            or not 1 <= len(inputs) <= 256 or any(not _manifest_shape(item) for item in inputs)):
        return Rejected('invalid_input')
    mode = inputs[0].mode
    if any(item.mode != mode for item in inputs):
        return Rejected('invalid_input')
    refs, memories, facts = set(), {}, {}
    for item in inputs:
        refs.update(item.refs)
        if len(refs) > 256:
            return Rejected('graph_limit')
        for stamps, collected, key in ((item.stamps, memories, 'memory_id'),
                                        (item.fact_stamps, facts, 'fact_id')):
            for stamp in stamps:
                identifier = getattr(stamp, key)
                if identifier in collected and collected[identifier] != stamp:
                    return Rejected('proof_changed')
                collected[identifier] = stamp
                if len(collected) > 200:
                    return Rejected('graph_limit')
    shown = _collect_refs(sorted(refs))
    if type(shown) is Rejected:
        return shown
    merged = RealmInputs(shown, tuple(memories[key] for key in sorted(memories)), mode,
                         tuple(facts[key] for key in sorted(facts)))
    current = realm_inputs_in_unit(store, shown, human=human, mode=mode)
    if type(current) is Rejected:
        return current
    return merged if current == merged else Rejected('proof_changed')
