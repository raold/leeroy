"""The memory scope (P1c Task 1; controller rulings N4, B1 and B2): who a memory may be spoken
of with. Every memory is open, private or secret, and a non-open one has `scope_with`, the one
person it may come up with (0 for nobody: it then reaches no prompt).

The scope comes from the evidence. A whisper among it makes the memory private to the whisper's
two people, and secret when its thread asked for secrecy (DONT_TELL_RE), or an earlier thread of
the same two did that night (fix I1). A Discord DM is a whisper here (ruling R7: the private
channels are discord.channels.is_private). A memory resting on a line of a restricted channel,
directly or through memory: refs (restricted_channels), is private: to the officers (scope_with
OFFICERS) when every such line is the officers' (officer chat or their voice room in the game, an
officers' Discord channel: one audience, 2026-11-12) and nothing else narrows it, else to nobody
(ruling R10), so no open-only reader ever takes it. A scoped memory it cites passes its scope on.
The strictest wins. A caller may raise a scope, never lower it, and what was said on a
public channel is never secret, whatever it says.

Standard library, leeroy_mind.clock and leeroy_mind.discord.channels (which reads the layout only
when asked about a Discord channel) only, so storage.store and storage.db can use it without an
import cycle."""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from leeroy_mind.clock import night_of
from leeroy_mind.discord.channels import (OFFICER_GAME, is_officer_channel, is_private, is_group,
                                          is_restricted, private_sql, restricted_sql)
from leeroy_mind.groups.audience import GroupSource, parse_readers

log = logging.getLogger("leeroy_mind")

# An apostrophe as he may type it: ASCII, the right single quote (U+2019) a phone's smart
# punctuation puts in, or the modifier letter apostrophe (U+02BC).
_APOSTROPHE = "['\u2019\u02bc]"
DONT_TELL_RE = re.compile(
    rf"\b(don{_APOSTROPHE}?t tell (anyone|anybody|people|the guild)|between (us|you and me)|"
    rf"keep (it|this) (quiet|to yourself|between us)|"
    rf"promise (you )?(won{_APOSTROPHE}?t|not to) tell|our (little )?secret)\b", re.I)
SCOPES = ("open", "private", "secret")  # in order of strictness
_REF = re.compile(r"(chat|memory):([0-9]+)")
_NIGHT_S = 25 * 3600  # the longest night: 24 hours, 25 when the clocks go back
WALK_DEPTH = 512  # bounded graph walk; unresolved remainder fails closed
# scope_with of a private memory the officers may know, in either medium (recall.scope_ok); no guid
OFFICERS = -1
GROUP = -2  # historical group evidence is checked by hidden_ok, never a pair guid


@dataclass(frozen=True)
class Scope:
    """`scope_with` is the one person a non-open memory may be spoken of with (0: nobody).
    `public`: the evidence cites a chat line on a channel that is not private (a whisper or a
    DM)."""
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
    nobody: bool = False  # a scope that claims nobody whoever asks (a read past its bound)


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
               if is_private(ln.channel) and ln.thread_id is not None}
    if not threads:
        return set()
    secret = {r[0] for r in conn.execute(
        f"SELECT thread_id, text FROM chat_line WHERE {private_sql()} "
        "AND thread_id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(threads)),))
        if DONT_TELL_RE.search(r[1])}
    rest = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, key, opened FROM chat_thread WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(threads - secret)),))}
    if not rest:
        return secret
    # One mind per member: their whispers and her DM with him are one conversation, so an ask
    # in either covers the pair's later threads in both that night
    human = _human_guid(conn)
    keys = json.dumps(sorted({k for key, _ in rest.values() for k in _pair_keys(key, human)}))
    asks: dict[str, list[tuple[float, str]]] = {}
    for lo, hi in _spans(opened for _, opened in rest.values()):
        for r in conn.execute(
                "SELECT l.ts, l.text, t.key FROM chat_line l JOIN chat_thread t "
                f"ON t.id = l.thread_id WHERE {private_sql('l.channel')} AND l.ts BETWEEN ? AND ? "
                "AND t.key IN (SELECT value FROM json_each(?))", (lo, hi, keys)):
            if DONT_TELL_RE.search(r[1]):
                asks.setdefault(_pair_of(r[2], human), []).append((r[0], _night(r[0])))
    for tid, (key, opened) in rest.items():
        night = _night(opened)
        if any(ts <= opened and n == night for ts, n in asks.get(_pair_of(key, human), ())):
            secret.add(tid)
    return secret


def _human_guid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'human_guid'").fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else 0


def _pair_of(key: str, human: int) -> str:
    """A private thread key's pair: whisper:lo:hi as it is, and a DM's (dm:<guid>, her and him)
    the whisper key of the same two; any other key as it is."""
    if key.startswith("dm:") and key[3:].isdigit() and human:
        lo, hi = sorted((int(key[3:]), human))
        return f"whisper:{lo}:{hi}"
    return key


def _pair_keys(key: str, human: int) -> set[str]:
    """Every thread key of the pair `key` belongs to: their whisper key and, with him in it,
    her DM's."""
    pair = _pair_of(key, human)
    keys = {key, pair}
    parts = pair.split(":")
    if human and len(parts) == 3 and parts[0] == "whisper" and str(human) in parts[1:]:
        other = parts[2] if parts[1] == str(human) else parts[1]
        keys.add(f"dm:{other}")
    return keys


def _asked_secrecy(ln: _Line, secret_threads: set[int]) -> bool:
    """A whisper or DM line told in confidence: it matches DONT_TELL_RE, or its thread asked for
    secrecy (_secret_threads, an earlier thread of the same two that night included). A line on
    any other channel never is (controller ruling B2)."""
    return is_private(ln.channel) and (DONT_TELL_RE.search(ln.text) is not None
                                       or ln.thread_id in secret_threads)


def secret_lines(conn: sqlite3.Connection, ids: Iterable[int]) -> set[int]:
    """The lines among `ids` told in confidence, in a few queries. _asked_secrecy is the one
    reading of secrecy: derive_scope and backfill_scopes apply it through _derive, and the
    night's ledger through this (knowledge.is_secret; pre-flight PR27). Plain SELECTs, no
    commit."""
    lines = _lines(conn, set(ids))
    threads = _secret_threads(conn, lines)
    return {i for i, ln in lines.items() if _asked_secrecy(ln, threads)}


def _memories(conn: sqlite3.Connection, ids: set[int], *,
              known: bool = False) -> dict[int, _Memory]:
    """The cited memories, each with its effective scope (resolve): the stored one made as
    strict as its own evidence implies, to a fixed point. So what derive_scope gives a memory
    citing them never rests on a stored scope its evidence has outgrown. One past the read
    bound counts as secret with nobody, whoever its owner; with `known` (the writer storing a
    new memory) as the strictest scope known for it, so no budget is ever stored as a scope."""
    if not ids:
        return {}
    out = {}
    for i, r in resolve(conn, ids).items():
        if r.bounded and not known:
            out[i] = _Memory(r.owner, *STRICTEST, nobody=True)
        else:
            out[i] = _Memory(r.owner, *r.effective)
    return out


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
            if not is_private(ln.channel):
                public = public or cited and not is_group(ln.channel)
                continue
            here = "secret" if _asked_secrecy(ln, secret_threads) else "private"
            claim = (ln.to if ln.frm == owner else ln.frm) if owner in (ln.frm, ln.to) else 0
        else:
            mem = memories.get(int(m.group(2)))
            if mem is None or mem.scope == "open":
                continue
            here = mem.scope
            claim = 0 if mem.nobody else (
                mem.scope_with if mem.owner == owner or mem.scope_with == GROUP else mem.owner)
        cls = max(cls, here, key=SCOPES.index)
        claims.append(claim)
    if cls == "open":
        return Scope(public=public)
    partners = set(claims)
    return Scope(cls, partners.pop() if len(partners) == 1 else 0, public)


def restricted_channels(conn: sqlite3.Connection, refs: dict) -> dict:
    """For each key of `refs` (key -> refs) whose refs rest on a line of a restricted channel
    (discord.channels.is_restricted), directly or through memory:<id> refs followed up
    to WALK_DEPTH levels (each memory's evidence, of any status), the set of those channels
    (ruling R10). Keys that rest on none are left out. A few batched SELECTs per level, no
    commit."""
    out: dict = {}
    frontier = {k: list(v) for k, v in refs.items()}
    walked: dict = {k: set() for k in refs}
    for _ in range(WALK_DEPTH + 1):
        chats = {k: _ids(v, "chat") for k, v in frontier.items()}
        wanted = set().union(*chats.values()) if chats else set()
        hidden = {r[0]: GroupSource(r[1],parse_readers(r[2])) if is_group(r[1]) else r[1] for r in conn.execute(
            "SELECT id, channel, audience FROM chat_line WHERE id IN (SELECT value FROM json_each(?)) "
            f"AND {restricted_sql()}",
            (json.dumps(sorted(wanted)),)) if is_restricted(r[1])} if wanted else {}
        for k, ids in chats.items():
            found = {hidden[i] for i in ids if i in hidden}
            if found:
                out[k] = out.get(k, frozenset()) | found
        nxt = {k: _ids(v, "memory") - walked[k] for k, v in frontier.items()}
        nxt = {k: ids for k, ids in nxt.items() if ids}
        if not nxt:
            break
        mids = set().union(*nxt.values())
        # a cited memory's evidence that does not parse cites nothing here (the store checks
        # every memory it writes through this walk; one bad old row must not stop a write)
        evidence = {r[0]: _refs_of(r[1]) for r in conn.execute(
            "SELECT id, evidence FROM memory WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(mids)),))}
        frontier = {}
        for k, ids in nxt.items():
            walked[k] |= ids
            frontier[k] = [e for i in ids for e in evidence.get(i, ())]
    else:
        for k, pending in frontier.items():
            if pending:
                out[k] = out.get(k, frozenset()) | {'unresolved'}
    return out


def derive_scope(conn: sqlite3.Connection, owner: int, evidence: Iterable[str], *,
                 shown: Iterable[str] = (), known: bool = False) -> Scope:
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
    one non-zero guid, else 0 (two partners, a claim of nobody). On top of that, refs that rest
    on a restricted line, at any depth (restricted_channels), make it at least private: with the
    officers (OFFICERS) when every such line is the officers' and the rest derives open or the
    officers already, else with nobody (ruling R10). A cited memory that cannot be resolved
    within the read bound counts as secret with nobody; `known` (the writer, which stores what
    this gives and leaves the row unchecked for the night) takes the strictest scope known for
    it instead. Plain SELECTs on `conn` and no commit, so it can run inside the caller's
    transaction."""
    evidence, shown = list(evidence), list(shown)
    refs = evidence + shown
    lines = _lines(conn, _ids(refs, "chat"))
    derived = _derive(owner, evidence, shown, lines, _secret_threads(conn, lines),
                      _memories(conn, _ids(refs, "memory"), known=known))
    return _with_hidden(derived, restricted_channels(conn, {0: refs}).get(0), owner)


def _with_hidden(derived: Scope, hidden, owner: int) -> Scope:
    """derive_scope's last step: `derived` raised by the restricted channels (`hidden`,
    restricted_channels' set for the memory, or nothing) its evidence rests on."""
    if not hidden:
        return derived
    cls = max(derived.scope, "private", key=SCOPES.index)
    if all(isinstance(c, GroupSource) for c in hidden):
        known = set.intersection(*(set(c.readers) for c in hidden))
        if owner in known:
            if derived.scope == 'open' or derived.scope_with == GROUP:
                return Scope(cls, GROUP, derived.public)
            if derived.scope_with > 0 and derived.scope_with in known:
                return Scope(cls, derived.scope_with, derived.public)
        return Scope(cls, 0, derived.public)
    officers = all(is_officer_channel(c) for c in hidden) and (
        derived.scope == "open" or derived.scope_with == OFFICERS)
    return Scope(cls, OFFICERS if officers else 0, derived.public)


# -- a scope is never wider than its evidence (2026-11-17) ---------------------------------
#
# derive_scope runs when a memory is written. When a memory it cites is made stricter later
# (a cleanup, a secrecy ask, an owner's edit), the scope stored on the citing memory is too
# wide. So the scope a reader uses is the *effective* one: the stored scope made as strict as
# the memory's evidence implies, where a cited memory counts with its own effective scope (a
# fixed point over memory: refs; the graph has cycles). memory.derived_scope/derived_with
# keep what the evidence implied when it was last checked and derived_at when; triggers
# (the scope_evidence migration) clear derived_at on every memory resting on one whose
# scope or evidence changed, and a row without derived_at is computed, never trusted.

STRICTEST = ("secret", 0)  # reaches no prompt: what a memory beyond the read bound counts as
RESOLVE_NODES = 2000  # unchecked memories one read works through; the rest fail closed


@dataclass
class Resolved:
    """One memory as resolve read it. `effective` is the strictest scope known for it: the
    stored one and what its evidence implies; `derived` what its evidence alone implies;
    `checked` False for one whose stored check was missing (computed here). `bounded`: the
    read bound was hit in what it rests on, so `effective` may still be too wide: readers go by
    `read` (secret with nobody), and nothing is stored for it. `complete` False when its
    evidence, at any depth, is malformed or cites a memory that has no row: its scope stands,
    but a gate that needs whole provenance (the lesson walk) must not take it as proved."""
    owner: int
    stored: tuple[str, int]
    effective: tuple[str, int]
    derived: tuple[str, int]
    checked: bool
    bounded: bool = False
    complete: bool = True

    @property
    def read(self) -> tuple[str, int]:
        """The scope a reader goes by."""
        return STRICTEST if self.bounded else self.effective


_SCOPE_COLS = ("id, owner, evidence, scope, scope_with, derived_scope, derived_with, derived_at, "
               "derived_complete")


def _scope_rows(conn: sqlite3.Connection, ids: set[int]) -> list:
    """The scope columns of memories `ids`. A database from before the scope_evidence
    migration (an older migration's data step, a read-only look at an old file) has no
    derived columns: its stored scopes stand as they did then."""
    wanted = json.dumps(sorted(ids))
    try:
        return conn.execute(f"SELECT {_SCOPE_COLS} FROM memory "
                            "WHERE id IN (SELECT value FROM json_each(?))", (wanted,)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such column" not in str(e):
            raise
        return [(*r, r[3], r[4], 0.0, 1) for r in conn.execute(
            "SELECT id, owner, evidence, scope, scope_with FROM memory "
            "WHERE id IN (SELECT value FROM json_each(?))", (wanted,))]


def _refs_of(raw) -> list:
    return _parsed(raw)[0]


def _parsed(raw) -> tuple[list, bool]:
    """A memory's evidence as a list, and whether it is well formed (a JSON list of strings)."""
    try:
        refs = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return [], False
    if not isinstance(refs, list):
        return [], False
    return refs, all(isinstance(r, str) for r in refs)


def _valid(scope, scope_with) -> tuple[str, int]:
    return (scope, scope_with if type(scope_with) is int else 0) if scope in SCOPES else STRICTEST


def resolve(conn: sqlite3.Connection, ids: Iterable[int], *,
            cap: int | None = RESOLVE_NODES, recheck: bool = False) -> dict[int, Resolved]:
    """Each memory of `ids` as its evidence makes it. One whose check is stored (derived_at)
    costs nothing beyond the one SELECT. An unchecked one is derived here from its evidence,
    and so is every unchecked memory it cites, at any depth, all to a fixed point (cycles
    included: scopes only rise, so it ends). At most `cap` unchecked memories are worked
    through in one call; one beyond that is `bounded`, and so is everything resting on it
    (Resolved.read: secret with nobody, never another member's). `recheck` takes every memory
    of `ids` as unchecked (what they cite keeps its stored check). Ids with no row are left
    out. Plain SELECTs, no write."""
    rows: dict[int, Resolved] = {}
    refs: dict[int, list] = {}
    sound: dict[int, bool] = {}
    todo = {i for i in ids if type(i) is int}
    again = set(todo) if recheck else set()
    absent: set[int] = set()
    while todo:
        found = _scope_rows(conn, todo)
        asked, todo = todo, set()
        for mid, owner, raw, scope, scope_with, d_scope, d_with, d_at, d_complete in found:
            stored = _valid(scope, scope_with)
            if d_at is not None and mid not in again:
                derived = _valid(d_scope, d_with)
                rows[mid] = Resolved(owner, stored, stricter(stored, derived), derived, True,
                                     complete=d_complete is None or bool(d_complete))
                continue
            if cap is not None and len(refs) >= cap:
                known = stricter(stored, _valid(d_scope, d_with)) if d_scope is not None else stored
                rows[mid] = Resolved(owner, stored, known, ("open", 0), False, bounded=True)
                continue
            # derived afresh: what the evidence implies now. The stored scope is what never
            # widens (settle raises it); a ref taken out of the evidence no longer counts
            rows[mid] = Resolved(owner, stored, stored, ("open", 0), False)
            refs[mid], sound[mid] = _parsed(raw)
            todo |= _ids(refs[mid], "memory")
        absent |= asked - rows.keys()
        todo -= rows.keys() | absent
    if not refs:
        return rows
    lines = _lines(conn, {i for r in refs.values() for i in _ids(r, "chat")})
    secret_threads = _secret_threads(conn, lines)
    hidden = restricted_channels(conn, refs)
    wanted = {mid: _ids(r, "memory") for mid, r in refs.items()}
    cited = {mid: w & rows.keys() for mid, w in wanted.items()}
    whole = {mid: sound[mid] and wanted[mid] <= rows.keys() for mid in refs}
    citers: dict[int, list[int]] = {}
    for mid, deps in cited.items():
        for d in deps:
            if d in refs:
                citers.setdefault(d, []).append(mid)
    queue, queued = sorted(refs), set(refs)
    while queue:
        nxt: list[int] = []
        for mid in queue:
            queued.discard(mid)
            row = rows[mid]
            d = _with_hidden(_derive(row.owner, refs[mid], [], lines, secret_threads, {
                c: _Memory(rows[c].owner, *rows[c].effective) for c in cited[mid]}),
                hidden.get(mid), row.owner)
            row.derived = stricter(row.derived, (d.scope, d.scope_with))
            state = (stricter(row.effective, row.derived),
                     any(rows[c].bounded for c in cited[mid]),
                     whole[mid] and all(rows[c].complete for c in cited[mid]))
            if state != (row.effective, row.bounded, row.complete):
                row.effective, row.bounded, row.complete = state
                for c in citers.get(mid, ()):
                    if c not in queued:
                        queued.add(c)
                        nxt.append(c)
        queue = sorted(nxt)
    return rows


def effective_scopes(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, tuple[str, int]]:
    """The (scope, scope_with) a reader must go by for each memory of `ids` (resolve)."""
    ids = set(ids)
    return {i: r.read for i, r in resolve(conn, ids).items() if i in ids}


def gated(conn: sqlite3.Connection, row, cache: dict | None = None):
    """A memory row (a mapping with id, scope and scope_with, as SELECT * gives it) as a dict
    carrying its effective scope, for readers that take rows straight from SQL. `cache` (a
    walk's own dict) keeps what one resolve read, so a walk down a chain of unchecked memories
    derives them once."""
    out = dict(row)
    found = None if cache is None else cache.get(out["id"])
    if found is None:
        read = resolve(conn, (out["id"],))
        if cache is not None:
            cache.update(read)
        found = read.get(out["id"])
    if found is not None:
        out["scope"], out["scope_with"] = found.read
    return out


def _officer_guids(conn: sqlite3.Connection) -> frozenset[int]:
    """The members whose persona is an officer (play.guild_role), as the guards read it."""
    try:
        return frozenset(r[0] for r in conn.execute(
            "SELECT guid FROM persona_core WHERE json_valid(json) "
            "AND json_extract(json, '$.play.guild_role') = 'officer'"))
    except sqlite3.OperationalError:
        return frozenset()


def backfill_officer_scopes(conn: sqlite3.Connection) -> int:
    """The officer-scope migration's data step (one mind per member, 2026-11-12). A memory resting
    on officer lines only (officer chat or their voice room in the game, an officers' Discord
    channel), at any depth, that is stored open (game officer chat, which was not restricted
    before) or private with nobody (an officers' Discord channel: R10's fail-closed class), takes
    the officers' scope derive_scope now gives it. Nothing else changes, and nothing is
    committed: it runs inside the migration's transaction. Returns how many rows changed.

    First, his officer-chat lines were taken as public until officer chat became restricted,
    and night stages cited them where they did not belong: slang and the lexicon learned from
    them (they learn from public lines only), and raiders' memories cited them though raiders
    never read officer chat. So a slang memory, any memory of a member who is not an officer,
    and the lexicon's retained sources (meta nightly:lexicon-sources:<id>) drop those refs and
    keep the rest; a memory or term with nothing else left keeps them. On the live guild
    (2026-11-12) this left 15 memories that really rest on officer chat."""
    officer = {r[0] for r in conn.execute(
        "SELECT id FROM chat_line WHERE channel IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(OFFICER_GAME)),))}

    def strip(refs):
        return [e for e in refs if not (isinstance(e, str) and e.startswith("chat:")
                                        and e[5:].isdigit() and int(e[5:]) in officer)]
    cleaned = terms = 0
    if officer:
        officers = _officer_guids(conn)
        for mid, owner, key, raw in conn.execute(
                "SELECT id, owner, key, evidence FROM memory").fetchall():
            if not ((key or "").startswith("slang:") or owner not in officers):
                continue
            refs = json.loads(raw)
            kept = strip(refs)
            if kept and len(kept) != len(refs):
                conn.execute("UPDATE memory SET evidence = ? WHERE id = ?", (json.dumps(kept), mid))
                cleaned += 1
        for key, raw in conn.execute("SELECT key, value FROM meta "
                                     "WHERE key GLOB 'nightly:lexicon-sources:*'").fetchall():
            try:
                refs = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(refs, list):
                continue
            kept = strip(refs)
            if kept and len(kept) != len(refs):
                conn.execute("UPDATE meta SET value = ? WHERE key = ?", (json.dumps(kept), key))
                terms += 1
    if cleaned or terms:
        log.info("officer_scope_cleaned: memories=%d terms=%d", cleaned, terms)
    rows = conn.execute("SELECT id, owner, evidence, scope, scope_with FROM memory "
                        "ORDER BY id").fetchall()
    stored = {r[0]: (r[1], json.loads(r[2]), r[3], r[4]) for r in rows}
    hidden = restricted_channels(conn, {i: v[1] for i, v in stored.items()})
    changed = 0
    for mid in sorted(hidden):
        if not all(is_officer_channel(c) for c in hidden[mid]):
            continue
        owner, evidence, scope, scope_with = stored[mid]
        if (scope, scope_with) != ("open", 0):
            continue  # only raised: a memory already reaching nobody is never widened
        d = derive_scope(conn, owner, evidence)
        new = stricter((scope, scope_with), (d.scope, d.scope_with))
        if new == (scope, scope_with):
            continue
        conn.execute("UPDATE memory SET scope = ?, scope_with = ? WHERE id = ?", (*new, mid))
        changed += 1
    if changed:
        log.info("officer_scope_backfill: changed=%d", changed)
    return changed


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
    give partner 0, but the group's (GROUP) and one reader give that reader."""
    cls = max(a[0], b[0], key=SCOPES.index)
    if cls == "open":
        return "open", 0
    partners = {w for s, w in (a, b) if s != "open"}
    if len(partners) == 2 and GROUP in partners and max(partners) > 0:
        # a group's memory that is also one reader's: that reader's, whichever came first
        # (derive_scope gives the same for the two together; the group's readers are still
        # checked where it is read: recall.hidden_ok)
        partners.discard(GROUP)
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


_DEPENDANTS = (
    "UPDATE memory SET derived_at = NULL WHERE derived_at IS NOT NULL AND id IN ("
    "WITH RECURSIVE dep(id) AS (SELECT src FROM memory_cite WHERE kind = ? AND dst IN "
    # dep CROSS JOIN: the queue outside, the cites by index (kind, dst). Left to itself the
    # planner (no sqlite_stat1) reads every memory cite once per row of the queue
    "(SELECT value FROM json_each(?)) UNION SELECT c.src FROM dep CROSS JOIN memory_cite c "
    "WHERE c.kind = 'memory' AND c.dst = dep.id) SELECT id FROM dep)")


def uncheck_citing(conn: sqlite3.Connection, kind: str, ids: Iterable[int]) -> int:
    """Every memory resting on the chat lines (`kind` "chat") or memories ("memory") `ids`, at
    any depth, is unchecked from now on (its derived_at cleared), so the next reader derives it
    again; how many. For a change the triggers cannot see: a line's secrecy. No commit."""
    ids = sorted(set(ids))
    if not ids:
        return 0
    try:
        return conn.execute(_DEPENDANTS, (kind, json.dumps(ids))).rowcount
    except sqlite3.OperationalError as e:
        if "no such" not in str(e):
            raise
        return 0  # a database from before the scope_evidence migration


def secrecy_reach(conn: sqlite3.Connection, line: int) -> set[int]:
    """The private lines an ask for secrecy in chat line `line` may cover (_secret_threads):
    those of its own thread, and of the pair's threads opened within a night after it (either
    medium). A superset: whoever cites one is derived again, which settles what it covers."""
    row = conn.execute("SELECT channel, thread_id, ts FROM chat_line WHERE id = ?",
                       (line,)).fetchone()
    if row is None or not is_private(row[0]):
        return set()
    out = {line}
    if row[1] is None:
        return out
    key = conn.execute("SELECT key FROM chat_thread WHERE id = ?", (row[1],)).fetchone()
    threads = {row[1]}
    if key is not None:
        keys = json.dumps(sorted(_pair_keys(key[0], _human_guid(conn))))
        threads |= {r[0] for r in conn.execute(
            "SELECT id FROM chat_thread WHERE key IN (SELECT value FROM json_each(?)) "
            "AND opened BETWEEN ? AND ?", (keys, row[2], row[2] + _NIGHT_S))}
    out |= {r[0] for r in conn.execute(
        f"SELECT id FROM chat_line WHERE {private_sql()} "
        "AND thread_id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(threads)),))}
    return out
