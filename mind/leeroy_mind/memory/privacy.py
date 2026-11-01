"""The memory scope (P1c Task 1; controller rulings N4, B1 and B2): who a memory may be spoken
of with. Every memory is open, private or secret, and a non-open one has `scope_with`, the one
person it may come up with (0 for nobody: it then reaches no prompt).

The scope comes from the evidence. A whisper among it makes the memory private to the whisper's
two people, and secret when its thread asked for secrecy (DONT_TELL_RE), or an earlier thread of
the same two did that night (fix I1). A scoped memory it cites passes its scope on. The strictest
wins. A caller may raise a scope, never lower it, and what was said on a public channel is never
secret, whatever it says.

Standard library and leeroy_mind.clock (which imports nothing of the mind) only, so storage.store
and storage.db can use it without an import cycle."""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from leeroy_mind.clock import night_of

log = logging.getLogger("leeroy_mind")

DONT_TELL_RE = re.compile(
    r"\b(don'?t tell (anyone|anybody|people|the guild)|between (us|you and me)|"
    r"keep (it|this) (quiet|to yourself|between us)|promise (you )?(won'?t|not to) tell|"
    r"our (little )?secret)\b", re.I)
SCOPES = ("open", "private", "secret")  # in order of strictness
_REF = re.compile(r"(chat|memory):([0-9]+)")
_NIGHT_S = 25 * 3600  # the longest night: 24 hours, 25 when the clocks go back


@dataclass(frozen=True)
class Scope:
    """`scope_with` is the one person a non-open memory may be spoken of with (0: nobody).
    `public`: the evidence cites a chat line on a channel other than whisper."""
    scope: str = "open"
    scope_with: int = 0
    public: bool = False


@dataclass(frozen=True)
class _Line:
    channel: str
    frm: int
    to: int
    thread_id: int | None
    text: str


@dataclass(frozen=True)
class _Memory:
    owner: int
    scope: str
    scope_with: int


def _ids(refs, kind: str) -> set[int]:
    hits = (_REF.fullmatch(r) for r in refs if isinstance(r, str))
    return {int(h.group(2)) for h in hits if h and h.group(1) == kind}


def _lines(conn: sqlite3.Connection, ids: set[int]) -> dict[int, _Line]:
    if not ids:
        return {}
    return {r[0]: _Line(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(
        "SELECT id, channel, from_guid, to_guid, thread_id, text FROM chat_line "
        "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(ids)),))}


def _night(ts: float) -> str:
    """The night `ts` falls on (clock.night_of: 06:00 to 06:00) in the host's zone. Privacy has
    no clock (the migration's backfill runs before there is a store), and in production the
    host's zone is the mind clock's (SystemClock). A time keeps its own offset there in any
    season, so what derive_scope gives a stored memory does not shift with the clocks."""
    return night_of(datetime.fromtimestamp(ts))


def _spans(times: Iterable[float]) -> list[tuple[float, float]]:
    """[t - _NIGHT_S, t] for each of `times`, overlapping spans merged, so one call over many
    threads (the backfill) reads each line once."""
    spans: list[list[float]] = []
    for t in sorted(times):
        if spans and t - _NIGHT_S <= spans[-1][1]:
            spans[-1][1] = t
        else:
            spans.append([t - _NIGHT_S, t])
    return [(lo, hi) for lo, hi in spans]


def _secret_threads(conn: sqlite3.Connection, lines: dict[int, _Line]) -> set[int]:
    """The threads of the whisper lines among `lines` that asked for secrecy (fix I1): one of
    the thread's whisper lines matches DONT_TELL_RE, or a whisper line of another thread with
    its key (the same two people) did by the time it opened, on the same night (_night). P1b
    cuts a thread after 90 s of silence or 12 lines, so an ask covers the pair's later threads
    until 06:00; it never reaches back to an earlier thread, into the next night or to another
    pair. Plain SELECTs: the threads' own lines, their keys and openings, then the asks of the
    night before each opening (_spans, on the chat_line ts index)."""
    threads = {ln.thread_id for ln in lines.values()
               if ln.channel == "whisper" and ln.thread_id is not None}
    if not threads:
        return set()
    secret = {r[0] for r in conn.execute(
        "SELECT thread_id, text FROM chat_line WHERE channel = 'whisper' "
        "AND thread_id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(threads)),))
        if DONT_TELL_RE.search(r[1])}
    rest = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, key, opened FROM chat_thread WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(threads - secret)),))}
    if not rest:
        return secret
    keys = json.dumps(sorted({key for key, _ in rest.values()}))
    asks: dict[str, list[tuple[float, str]]] = {}
    for lo, hi in _spans(opened for _, opened in rest.values()):
        for r in conn.execute(
                "SELECT l.ts, l.text, t.key FROM chat_line l JOIN chat_thread t "
                "ON t.id = l.thread_id WHERE l.channel = 'whisper' AND l.ts BETWEEN ? AND ? "
                "AND t.key IN (SELECT value FROM json_each(?))", (lo, hi, keys)):
            if DONT_TELL_RE.search(r[1]):
                asks.setdefault(r[2], []).append((r[0], _night(r[0])))
    for tid, (key, opened) in rest.items():
        night = _night(opened)
        if any(ts <= opened and n == night for ts, n in asks.get(key, ())):
            secret.add(tid)
    return secret


def _asked_secrecy(ln: _Line, secret_threads: set[int]) -> bool:
    """A whisper line told in confidence: it matches DONT_TELL_RE, or its thread asked for
    secrecy (_secret_threads, an earlier thread of the same two that night included). A line on
    any other channel never is (controller ruling B2)."""
    return ln.channel == "whisper" and (DONT_TELL_RE.search(ln.text) is not None
                                        or ln.thread_id in secret_threads)


def secret_lines(conn: sqlite3.Connection, ids: Iterable[int]) -> set[int]:
    """The lines among `ids` told in confidence, in a few queries. _asked_secrecy is the one
    reading of secrecy: derive_scope and backfill_scopes apply it through _derive, and the
    night's ledger through this (knowledge.is_secret; pre-flight PR27). Plain SELECTs, no
    commit."""
    lines = _lines(conn, set(ids))
    threads = _secret_threads(conn, lines)
    return {i for i, ln in lines.items() if _asked_secrecy(ln, threads)}


def _memories(conn: sqlite3.Connection, ids: set[int]) -> dict[int, _Memory]:
    if not ids:
        return {}
    return {r[0]: _Memory(r[1], r[2], r[3]) for r in conn.execute(
        "SELECT id, owner, scope, scope_with FROM memory "
        "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(ids)),))}


def _derive(owner: int, evidence: list, shown: list, lines: dict[int, _Line],
            secret_threads: set[int], memories: dict[int, _Memory]) -> Scope:
    """derive_scope over rows already read."""
    cls, claims, public = "open", [], False
    for ref, cited in [*((r, True) for r in evidence), *((r, False) for r in shown)]:
        m = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if m is None:
            continue
        if m.group(1) == "chat":
            ln = lines.get(int(m.group(2)))
            if ln is None:
                continue
            if ln.channel != "whisper":
                public = public or cited
                continue
            here = "secret" if _asked_secrecy(ln, secret_threads) else "private"
            claim = (ln.to if ln.frm == owner else ln.frm) if owner in (ln.frm, ln.to) else 0
        else:
            mem = memories.get(int(m.group(2)))
            if mem is None or mem.scope == "open":
                continue
            here = mem.scope
            claim = mem.scope_with if mem.owner == owner else mem.owner
        cls = max(cls, here, key=SCOPES.index)
        claims.append(claim)
    if cls == "open":
        return Scope(public=public)
    partners = set(claims)
    return Scope(cls, partners.pop() if len(partners) == 1 else 0, public)


def derive_scope(conn: sqlite3.Connection, owner: int, evidence: Iterable[str], *,
                 shown: Iterable[str] = ()) -> Scope:
    """The least scope a memory of `owner` citing `evidence` may have; `shown` is what its
    generating prompt showed beyond what it cites. Each ref gives a class, and may claim a
    partner:
    - chat:<id> on whisper: secret when that line or its thread asked for secrecy
      (_secret_threads: a DONT_TELL_RE whisper line in it, or in an earlier thread of the same
      two that night), else private. It claims the line's other end when `owner` is one of
      its two ends (her replies count as much as his lines), and nobody (0) when she is not;
    - chat:<id> on any other channel: open, and public when cited (a line only shown is not);
    - memory:<id>, of any status: that memory's scope. A non-open one claims its partner when
      it is `owner`'s own memory, else its owner, the member who shared it;
    - any other ref, or an id with no row: open, and no claim.
    The scope is the strictest class; scope_with is the guid every claim names when that is
    one non-zero guid, else 0 (two partners, a claim of nobody). Plain SELECTs on `conn` and no
    commit, so it can run inside the caller's transaction."""
    evidence, shown = list(evidence), list(shown)
    refs = evidence + shown
    lines = _lines(conn, _ids(refs, "chat"))
    return _derive(owner, evidence, shown, lines, _secret_threads(conn, lines),
                   _memories(conn, _ids(refs, "memory")))


def with_request(derived: Scope, scope: str, scope_with: int) -> tuple[str, int]:
    """The scope a caller's request (`scope`, `scope_with`) gives on top of the derived one
    (controller ruling B2). It may raise the class, never lower it, and a request for secret on
    public evidence is refused: what was said on a public channel cannot be made secret. A
    derived partner always stands; the request's partner counts only when it raises an open
    scope (0: nobody)."""
    if scope == "secret" and derived.public:
        scope = derived.scope
    cls = max(derived.scope, scope, key=SCOPES.index)
    if cls == "open":
        return "open", 0
    return cls, derived.scope_with if derived.scope != "open" else scope_with


def stricter(a: tuple[str, int], b: tuple[str, int]) -> tuple[str, int]:
    """The stricter of two (scope, scope_with); two non-open scopes with different partners
    give partner 0."""
    cls = max(a[0], b[0], key=SCOPES.index)
    if cls == "open":
        return "open", 0
    partners = {w for s, w in (a, b) if s != "open"}
    return cls, partners.pop() if len(partners) == 1 else 0


def backfill_scopes(conn: sqlite3.Connection) -> int:
    """The scope migration's data step (storage.db.DATA_STEPS; controller ruling B1). Every
    memory, of any status, takes the scope derive_scope gives its evidence when that is
    stricter than the one it has. Memories go in id order, so one citing an earlier memory sees
    that memory's new scope. The memories, the lines they cite and those lines' threads are
    read in a few queries, and nothing is committed: it runs inside the migration's
    transaction. Returns how many rows changed."""
    rows = conn.execute(
        "SELECT id, owner, evidence, scope, scope_with FROM memory ORDER BY id").fetchall()
    evidence = {r[0]: json.loads(r[2]) for r in rows}
    memories = {r[0]: _Memory(r[1], r[3], r[4]) for r in rows}
    lines = _lines(conn, {i for refs in evidence.values() for i in _ids(refs, "chat")})
    secret_threads = _secret_threads(conn, lines)
    changed = []
    for r in rows:
        mid, owner = r[0], r[1]
        was = (memories[mid].scope, memories[mid].scope_with)
        d = _derive(owner, evidence[mid], [], lines, secret_threads, memories)
        now = stricter(was, (d.scope, d.scope_with))
        if now != was:
            memories[mid] = _Memory(owner, *now)
            changed.append((*now, mid))
    conn.executemany("UPDATE memory SET scope = ?, scope_with = ? WHERE id = ?", changed)
    log.info("scope_backfill: n=%d", len(changed))
    return len(changed)
