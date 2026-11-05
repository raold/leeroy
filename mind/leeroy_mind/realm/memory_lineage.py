"""Permanent Realm fact attribution for memory content writes."""
from collections.abc import Mapping
import re

from .records import Rejected

_ID = re.compile(r"[1-9][0-9]*\Z")
_MAX_ID = (1 << 63) - 1


class RealmLineageError(ValueError):
    def __init__(self, rejection: Rejected):
        self.rejection = rejection
        super().__init__(rejection.reason)


def _fail(reason):
    raise RealmLineageError(Rejected(reason))


def _id(s):
    if len(s) > 19 or _ID.fullmatch(s) is None:
        return None
    value = int(s)
    return value if value <= _MAX_ID else None


def _freeze(evidence, shown=()):
    """Freeze each iterable once and validate the complete distinct new ref union."""
    def materialize(values):
        if isinstance(values, (str, bytes, Mapping)):
            _fail("invalid_input")
        try:
            return tuple(values)
        except TypeError:
            _fail("invalid_input")
    ev, seen = materialize(evidence), materialize(shown)
    refs = set()
    total = 0
    for ref in (*ev, *seen):
        if type(ref) is not str or not ref:
            _fail("invalid_input")
        try:
            size = len(ref.encode("utf-8"))
        except UnicodeEncodeError:
            _fail("invalid_input")
        if size > 128:
            _fail("graph_limit")
        if ref not in refs:
            refs.add(ref)
            total += size
            if len(refs) > 256 or total > 32768:
                _fail("graph_limit")
        if ref.startswith("realm:") and (not ref.startswith("realm:fact:")
                                         or _id(ref[11:]) is None):
            _fail("invalid_input")
        if ref.startswith("memory:") and _id(ref[7:]) is None:
            _fail("invalid_input")
    return ev, seen


def _unit(store):
    from leeroy_mind.storage.store import Store
    if not isinstance(store, Store) or store.in_unit is not True:
        raise RuntimeError("Realm lineage requires Store.transaction()")


def _existing(store, memory_id, facts, *, required):
    row = store.conn.execute("SELECT evidence FROM memory WHERE id=?", (memory_id,)).fetchone()
    if row is None:
        # Missing untagged legacy inputs are opaque, but an orphaned dependency
        # is a corrupt relationship and must not be silently discarded.
        if required or store.conn.execute(
                "SELECT 1 FROM realm_memory_dependency WHERE memory=? LIMIT 1",
                (memory_id,)).fetchone() is not None:
            _fail("missing_row")
        return
    for link in store.conn.execute(
            "SELECT fact FROM realm_memory_dependency WHERE memory=? LIMIT 201", (memory_id,)):
        value = link[0]
        if type(value) is not int or not 1 <= value <= _MAX_ID:
            _fail("invalid_row")
        facts.add(value)
        if len(facts) > 200:
            _fail("graph_limit")
    # Only direct Realm strings in a valid array matter. Unrelated legacy ancestry is opaque.
    raw = row[0]
    valid = store.conn.execute(
        "SELECT CASE WHEN json_valid(?) THEN json_type(?) = 'array' ELSE 0 END",
        (raw, raw)).fetchone()[0]
    if valid:
        for entry in store.conn.execute(
                "SELECT DISTINCT value FROM json_each(?) WHERE type='text' "
                "AND substr(value,1,6)='realm:' LIMIT 201", (raw,)):
            ref = entry[0]
            fact = _id(ref[11:]) if ref.startswith("realm:fact:") else None
            if fact is None:
                _fail("invalid_row")
            facts.add(fact)
            if len(facts) > 200:
                _fail("graph_limit")


def _validate_facts(store, facts):
    if len(facts) > 200:
        _fail("graph_limit")
    for fact in facts:
        if store.conn.execute("SELECT 1 FROM realm_fact WHERE id=?", (fact,)).fetchone() is None:
            _fail("missing_row")


def realm_dependencies_in_unit(store, *, evidence, shown=(), existing_memory=None):
    _unit(store)
    evidence, shown = _freeze(evidence, shown)
    facts = set()
    if existing_memory is not None:
        if type(existing_memory) is not int or not 1 <= existing_memory <= _MAX_ID:
            _fail("invalid_input")
        _existing(store, existing_memory, facts, required=True)
    for ref in set((*evidence, *shown)):
        if ref.startswith("realm:"):
            facts.add(_id(ref[11:]))
        elif ref.startswith("memory:"):
            _existing(store, _id(ref[7:]), facts, required=False)
    _validate_facts(store, facts)
    return tuple(sorted(facts))


def attach_realm_dependencies_in_unit(store, memory_id: int, fact_ids: tuple[int, ...]):
    _unit(store)
    if type(memory_id) is not int or not 1 <= memory_id <= _MAX_ID or type(fact_ids) is not tuple:
        _fail("invalid_input")
    if any(type(i) is not int or not 1 <= i <= _MAX_ID for i in fact_ids):
        _fail("invalid_input")
    facts = set(fact_ids)
    _existing(store, memory_id, facts, required=True)
    _validate_facts(store, facts)
    store.conn.executemany(
        "INSERT OR IGNORE INTO realm_memory_dependency(memory, fact) VALUES (?, ?)",
        ((memory_id, fact) for fact in sorted(facts)))
