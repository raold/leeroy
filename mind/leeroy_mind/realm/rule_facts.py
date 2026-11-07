"""Facts the realm derives by rule: a rival's rule kill, a lost benchmark, a poach offer and the
night's beats and forum posts.

No game_event or chat_line row stands behind them, so their origin is realm_rule_origin, keyed
`rule:<policy>:<key>` by its producer, with the producer's inputs and the fact's knowers kept
canonical. A key is written once: the same key again returns the stored fact when everything
it says is the same, and is refused when anything differs. Every text is screened as a
memory's is stored (clean_memory_text): at most 200 characters, never his real name, no
romance and nobody leaving the guild. It is screened again, against his current identity, on
every read. A private fact is read here for the rules and never reaches a prompt:
FactStore.known_in_unit takes public ones only. Callers own the Store unit; nothing here
commits.
"""
from __future__ import annotations

import hashlib
import json
import re

from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.personas.screens import ROMANCE_RE, names_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup

from .records import FactRow, Rejected

# Each policy and the realm_fact.source its facts carry.
POLICIES = {
    "race_rule_kill_v1": "rule",
    "benchmark_loss_v1": "benchmark",
    "poach_offer_v1": "rule",
    "night_beat_v1": "night",
    "forum_post_v1": "night",
}
HOWS = ("present", "server", "forum", "trade", "own")
MAX_TEXT = 200
MAX_KNOWERS = 129
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,96}")
_KIND = re.compile(r"[a-z0-9_]{1,64}")
_U32 = (1 << 32) - 1
_SQLITE_MAX = (1 << 63) - 1
_MAX_SECONDS = 253402300799.999
_ORIGIN_COLUMNS = "fact,producer_key,policy,inputs_json,known_json,text_sha256,revision,state"


class _Refused(Exception):
    def __init__(self, reason):
        self.reason = reason


def _fail(reason):
    raise _Refused(reason)


def _require_unit(store):
    if not isinstance(store, Store) or not store.in_unit:
        raise RuntimeError("realm rule facts require an active Store unit")


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_input")


def _seconds(value):
    return type(value) in (int, float) and 0 <= value <= _MAX_SECONDS  # NaN compares false.


def _human(human):
    if type(human) is not HumanIdentity or type(human.real_name) is not str:
        _fail("invalid_input")


def _screen(text, human):
    """The stored form of a text, or why it cannot be one."""
    # Here, not at import: the night's grounding module loads the config, which importing the
    # fact store must not.
    from leeroy_mind.nightly.ground import LEAVES_RE
    if type(text) is not str:
        _fail("invalid_input")
    flat = " ".join(strip_wow_markup(text).split())
    if names_real_name(text, human) or names_real_name(flat, human):
        _fail("real_name")
    if not flat or len(flat) > MAX_TEXT:
        _fail("unsupported_text")
    cleaned = clean_memory_text(text, human)
    if cleaned != flat or len(cleaned.encode("utf-8")) > 800:
        _fail("unsupported_text")
    if ROMANCE_RE.search(cleaned):
        _fail("romance")
    if LEAVES_RE.search(cleaned):
        _fail("unsupported_text")
    return cleaned


def _knowers(known_by):
    """[[guild, how], ...] by guild, each guild once."""
    try:
        pairs = sorted(tuple(pair) for pair in known_by)
    except TypeError:
        _fail("invalid_input")
    if not 1 <= len(pairs) <= MAX_KNOWERS:
        _fail("invalid_input")
    for pair in pairs:
        if (len(pair) != 2 or type(pair[0]) is not int or not 1 <= pair[0] <= _U32
                or type(pair[1]) is not str or pair[1] not in HOWS):
            _fail("invalid_input")
    if len({guild for guild, _ in pairs}) != len(pairs):
        _fail("invalid_input")
    return [list(pair) for pair in pairs]


def _inputs(inputs):
    if type(inputs) is not dict:
        _fail("invalid_input")
    raw = _json(inputs)
    if len(raw.encode("utf-8")) > 8192:
        _fail("invalid_input")
    return raw


def _stored(origin):
    """The origin's own canonical inputs and knowers, or invalid_row."""
    try:
        inputs, known = json.loads(origin["inputs_json"]), json.loads(origin["known_json"])
        return _inputs(inputs) == origin["inputs_json"] and _knowers(known) == known, known
    except (TypeError, ValueError, _Refused):
        return False, None


def _read(store, fact_id, human):
    _human(human)
    if type(fact_id) is not int or not 1 <= fact_id <= _SQLITE_MAX:
        _fail("invalid_input")
    conn = store.conn
    fact = conn.execute("SELECT id,kind,text,source,ref,public,ts FROM realm_fact WHERE id=?",
                        (fact_id,)).fetchone()
    origin = conn.execute(f"SELECT {_ORIGIN_COLUMNS} FROM realm_rule_origin WHERE fact=?",
                          (fact_id,)).fetchone()
    if fact is None or origin is None:
        _fail("missing_row")
    if conn.execute("SELECT 1 FROM realm_fact_origin WHERE fact=?", (fact_id,)).fetchone():
        _fail("proof_changed")  # Two origins: neither speaks for it.
    if origin["state"] != "active":
        _fail("private_evidence")
    policy, key = origin["policy"], origin["producer_key"]
    if (type(policy) is not str or policy not in POLICIES or fact["source"] != POLICIES[policy]
            or type(key) is not str or fact["ref"] != key or not key.startswith(f"rule:{policy}:")
            or not _KEY.fullmatch(key[len(policy) + 6:])
            or type(fact["kind"]) is not str or not _KIND.fullmatch(fact["kind"])
            or type(fact["public"]) is not int or fact["public"] not in (0, 1) or not _seconds(fact["ts"])
            or type(origin["revision"]) is not int or not 1 <= origin["revision"] <= _SQLITE_MAX):
        _fail("invalid_row")
    valid, known = _stored(origin)
    if not valid:
        _fail("invalid_row")
    text, ts = fact["text"], float(fact["ts"])
    if type(text) is not str or origin["text_sha256"] != _sha256(text):
        _fail("proof_changed")
    if _screen(text, human) != text:
        _fail("unsupported_text")
    rows = conn.execute("SELECT guild,since,how FROM realm_knows WHERE fact=? ORDER BY guild LIMIT ?",
                        (fact_id, MAX_KNOWERS + 1)).fetchall()
    if [[row["guild"], row["how"]] for row in rows] != known or any(row["since"] != ts for row in rows):
        _fail("proof_changed")
    try:
        return FactRow(fact["id"], fact["kind"], text, fact["source"], key, bool(fact["public"]),
                       ts, origin["revision"])
    except ValueError:
        _fail("invalid_row")


def rule_fact_in_unit(store, fact_id, *, human) -> FactRow | Rejected:
    """Read one active rule fact, public or private, rechecked against its origin and his
    current identity. Only the rules may use a private one."""
    _require_unit(store)
    try:
        return _read(store, fact_id, human)
    except _Refused as error:
        return Rejected(error.reason)


def screen_rule_text(text, *, human) -> str | Rejected:
    """A text as put_rule_fact_in_unit would store it, or why it would refuse it. Reads and
    writes nothing, so it needs no Store unit."""
    try:
        _human(human)
        return _screen(text, human)
    except _Refused as error:
        return Rejected(error.reason)


def put_rule_fact_in_unit(store, *, policy, key, kind, text, public, known_by, inputs, ts,
                          human) -> FactRow | Rejected:
    """Write a rule's fact once under `rule:<policy>:<key>`, with who knows it and how.

    The same key again returns the stored fact when its kind, text, publicity, knowers and
    inputs are the same, whatever the time; anything else is refused (proof_changed). A
    refusal writes nothing.
    """
    _require_unit(store)
    try:
        if type(policy) is not str or policy not in POLICIES:
            _fail("unsupported_policy")
        if (type(key) is not str or not _KEY.fullmatch(key) or type(kind) is not str
                or not _KIND.fullmatch(kind) or type(public) is not bool or not _seconds(ts)):
            _fail("invalid_input")
        _human(human)
        ts = float(ts)
        known = _knowers(known_by)
        known_json, inputs_json = _json(known), _inputs(inputs)
        cleaned = _screen(text, human)
        producer_key = f"rule:{policy}:{key}"
        source = POLICIES[policy]
        conn = store.conn
        existing = conn.execute("SELECT fact,inputs_json,known_json FROM realm_rule_origin "
                                "WHERE producer_key=?", (producer_key,)).fetchone()
        if existing is not None:
            fact = _read(store, existing["fact"], human)
            if (fact.kind != kind or fact.text != cleaned or fact.public != public
                    or existing["known_json"] != known_json or existing["inputs_json"] != inputs_json):
                _fail("proof_changed")
            return fact
        with store.transaction():  # A savepoint: a failed readback undoes only this fact.
            fact_id = conn.execute(
                "INSERT INTO realm_fact(kind,text,source,ref,public,ts) VALUES (?,?,?,?,?,?)",
                (kind, cleaned, source, producer_key, int(public), ts)).lastrowid
            conn.execute(
                f"INSERT INTO realm_rule_origin({_ORIGIN_COLUMNS}) VALUES (?,?,?,?,?,?,1,'active')",
                (fact_id, producer_key, policy, inputs_json, known_json, _sha256(cleaned)))
            conn.executemany("INSERT INTO realm_knows(fact,guild,since,how) VALUES (?,?,?,?)",
                             [(fact_id, guild, ts, how) for guild, how in known])
            fact = _read(store, fact_id, human)
            if fact != FactRow(fact_id, kind, cleaned, source, producer_key, public, ts, 1):
                _fail("proof_changed")
        return fact
    except _Refused as error:
        return Rejected(error.reason)
