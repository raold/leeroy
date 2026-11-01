"""The epistemic ledger (P1c Task 6; the cognition memo's vector 1): who knows what, kept as
state outside the model, because a model's theory of mind is brittle.

- A root is what a memory finally rests on: a chat line, a game event, a scene, a fact (a ref
  with a ROOT_PREFIXES prefix). roots() follows memory refs to them.
- `knows`: how each member learned each root (HOWS), who told them, and whether they may pass
  it on. A secret (something told in confidence: a whisper that asked for it, a private scene,
  a confidential fact) is never passed on (amendment A4). A plain whisper is private, not
  secret: only its two people know it, and only her own gossip can pass it on (rulings I8, N4).
- `fact`: the dramatic facts. His real name is one of them, kept by id only: no text, and no
  second-order rows or reminders.
- `thinks_knows`: what a member believes another knows, two levels at most, never deeper (the
  memo's Hi-ToM caveat). The human is never a holder: he is not simulated.
- stage_knows: the night's stage that fills the ledger and writes the "doesn't know you know"
  reminders.

A memory's scope comes from P1b's MemoryWriter (Task 1; ruling B2), never from here; this
module reads what only P1c's refs can tell (a confession's scene, a confidential fact), and
scope_leaks checks that the two agree."""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Literal

from leeroy_mind.logs import log
from leeroy_mind.memory.privacy import SCOPES, Scope, derive_scope, secret_lines
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly.audience import audience_of
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store, clean

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

ROOT_PREFIXES = ("chat:", "event:", "session:", "story:", "beat:", "life:", "encounter:", "away:",
                 "rule:", "joke:", "feud:", "mentor:", "chronicle:", "award:")
HOWS = ("witnessed", "overheard", "told", "confided", "inferred")
BELIEFS = ("yes", "no", "unsure")
PROBE_MAX_AGE_DAYS = 35
PROBE_MIN_N = 36  # 12 probes x 3 samples (Task 23)
REMINDER_DAYS = 14  # how far back the reminders unit looks
REAL_NAME_ROOT = "rule:real_name"
STAGE = "knows"
DAY_S = 86400.0
ID_BATCH = 500  # memory ids read at once: SQLite binds at most 32,766 parameters (review M1)
_MEMORY = re.compile(r"memory:([0-9]+)")
_NUMBERED = re.compile(r"([a-z_]+):([0-9]+)")
_FACT_REF = re.compile(r"rule:fact:([0-9]+)")


def _rows(store: Store, sql: str, params=()) -> list[sqlite3.Row]:
    with store.transaction():
        return store.conn.execute(sql, params).fetchall()


def _number(root: str, kind: str) -> int | None:
    m = _NUMBERED.fullmatch(root) if isinstance(root, str) else None
    return int(m.group(2)) if m and m.group(1) == kind else None


# -- roots ---------------------------------------------------------------------------------------

def _memory_rows(conn: sqlite3.Connection, ids) -> dict[int, tuple[tuple, str]]:
    """(evidence, scope) of each memory among `ids`, of any status; unknown ids are left out."""
    return {r["id"]: (tuple(json.loads(r["evidence"])), r["scope"]) for r in conn.execute(
        "SELECT id, evidence, scope FROM memory WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(ids)),))}


def _walk(refs: Iterable[str], depth: int,
          lookup: Callable[[list[int]], dict[int, tuple[tuple, str]]]
          ) -> tuple[set[str], dict[int, str]]:
    """The roots `refs` rest on, and the scope of every memory the walk went through. A
    memory:<id> is replaced by its evidence, up to `depth` levels; an unknown id is dropped,
    and so is any ref that is neither a root nor a memory."""
    found: set[str] = set()
    visited: dict[int, str] = {}
    asked: set[int] = set()
    frontier, level = list(refs), 0
    while frontier:
        ids = []
        for ref in frontier:
            if not isinstance(ref, str):
                continue
            if ref.startswith(ROOT_PREFIXES):
                found.add(ref)
            elif level < depth and (m := _MEMORY.fullmatch(ref)) and int(m.group(1)) not in asked:
                asked.add(int(m.group(1)))
                ids.append(int(m.group(1)))
        if not ids:
            break
        rows = lookup(ids)
        visited.update({i: rows[i][1] for i in ids if i in rows})
        frontier = [r for i in ids if i in rows for r in rows[i][0]]
        level += 1
    return found, visited


def roots(store: Store, refs: Iterable[str], *, depth: int = 3) -> set[str]:
    """The roots `refs` rest on: a ref with a root prefix is a root, and memory:<id> is replaced
    by that memory's evidence, followed up to `depth` levels. An unknown memory id is dropped."""
    with store.transaction():
        return _walk(refs, depth, lambda ids: _memory_rows(store.conn, ids))[0]


# -- secret and private --------------------------------------------------------------------------

def _channel(conn: sqlite3.Connection, root: str) -> str | None:
    """The channel of a chat: root's line; None for any other root or a line that is not there."""
    i = _number(root, "chat")
    r = None if i is None else conn.execute("SELECT channel FROM chat_line WHERE id = ?",
                                            (i,)).fetchone()
    return None if r is None else r["channel"]


def _base(conn: sqlite3.Connection, root: str) -> str:
    """The root a rule:fact:<id> ref stands for: its fact's own root, whose secrecy, privacy and
    parties it carries (review M3). Any other root, or a fact that is not there, is itself."""
    m = _FACT_REF.fullmatch(root) if isinstance(root, str) else None
    r = None if m is None else conn.execute("SELECT root FROM fact WHERE id = ?",
                                            (int(m.group(1)),)).fetchone()
    return root if r is None else r["root"]


def _secret_facts(conn: sqlite3.Connection) -> frozenset[str]:
    """The roots that have a secret fact, read once for a call that checks many roots (the fact
    table has no index on its root; review M6)."""
    return frozenset(r[0] for r in conn.execute("SELECT DISTINCT root FROM fact WHERE secret = 1"))


def _secret(conn: sqlite3.Connection, root: str,
            secret_facts: frozenset[str] | None = None) -> bool:
    root = _base(conn, root)
    channel = _channel(conn, root)
    if channel is not None and channel != "whisper":
        return False  # what was said on a public channel cannot be made secret (ruling B2)
    line = _number(root, "chat")
    if channel == "whisper" and line in secret_lines(conn, [line]):
        return True
    i = _number(root, "encounter")
    if i is not None:
        r = conn.execute("SELECT private FROM encounter WHERE id = ?", (i,)).fetchone()
        if r is not None and r["private"]:
            return True
    if secret_facts is not None:
        return root in secret_facts
    return conn.execute("SELECT 1 FROM fact WHERE root = ? AND secret = 1 LIMIT 1",
                        (root,)).fetchone() is not None


def _private(conn: sqlite3.Connection, root: str,
             secret_facts: frozenset[str] | None = None) -> bool:
    return _channel(conn, _base(conn, root)) == "whisper" and not _secret(conn, root, secret_facts)


def _sensitive(conn: sqlite3.Connection, root: str,
               secret_facts: frozenset[str] | None = None) -> bool:
    """Secret or private: a secret root, or any whisper root (one that is not secret is private)."""
    return _secret(conn, root, secret_facts) or _channel(conn, _base(conn, root)) == "whisper"


def is_secret(store: Store, root: str) -> bool:
    """Something told in confidence (amendment A4): a whisper chat: root whose line or thread
    asked for secrecy, an earlier thread of the same two that night included
    (privacy.secret_lines, the writer's own reading, so the ledger and the scope agree); an
    encounter: root whose row is private (a 2am confession); or any root with a secret fact.
    Never a chat: root on a channel but whisper. A rule:fact:<id> ref is as secret as its fact's
    own root."""
    with store.transaction():
        return _secret(store.conn, root)


def is_private(store: Store, root: str) -> bool:
    """A whisper chat: root, from either end, that is not secret (or a rule:fact:<id> ref to a
    fact on one): only its two people know it, and only the gossip of one of them can pass it
    on (Task 16)."""
    with store.transaction():
        return _private(store.conn, root)


def _parties(conn: sqlite3.Connection, root: str) -> frozenset[int]:
    root = _base(conn, root)
    i = _number(root, "chat")
    if i is not None:
        r = conn.execute("SELECT from_guid, to_guid FROM chat_line WHERE id = ?", (i,)).fetchone()
        return frozenset(g for g in ((r["from_guid"], r["to_guid"]) if r else ()) if g)
    i = _number(root, "encounter")
    if i is not None:
        r = conn.execute("SELECT a, b FROM encounter WHERE id = ?", (i,)).fetchone()
        return frozenset(g for g in ((r["a"], r["b"]) if r else ()) if g)
    return frozenset()


def parties(store: Store, root: str) -> frozenset[int]:
    """Who a root is between, and so knows it without being told: a chat line's speaker and
    addressee (a whisper's two ends), an encounter's two members. Empty for any other root."""
    with store.transaction():
        return _parties(store.conn, root)


def could_have_seen(store: Store, root: str) -> frozenset[int]:
    """Who may know what `root` is though no ledger row says so: a chat line's audience
    (audience.audience_of: a whisper's two ends, the cast that could see a public line), a game
    event's members (or the one it was about), and always the human. The ledger records a
    public line's audience only when the line carries a fact, and it never holds him (he is not
    simulated), so it cannot vouch that he does not know something. A rule:fact:<id> ref stands
    for its fact's root. Grounding rule 12 (through evidence's knowers) and unknown_to read it
    alike, so a theory-of-mind claim and Task 11's who-knows-what agree on who really does not
    know (review-task11 I2)."""
    human = int(store.get_meta("human_guid") or 0)
    with store.transaction():
        base = _base(store.conn, root)
        line, event = _number(base, "chat"), _number(base, "event")
        seen = set(audience_of(store, line)) if line is not None else set()
        r = None if event is None else store.conn.execute(
            "SELECT payload FROM game_event WHERE id = ?", (event,)).fetchone()
    if r is not None:
        payload = json.loads(r["payload"])
        seen |= {g for g in payload.get("members") or () if g}
        if payload.get("guid"):
            seen.add(payload["guid"])
    return frozenset(seen | ({human} if human else set()))


# -- the ledger ----------------------------------------------------------------------------------

def learn(store: Store, root: str, guid: int, how: str, *, via: int | None = None, ts: float,
          confidence: float = 1.0, may_tell: bool | int | None = None) -> bool:
    """`guid` learned `root` (`how`, from `via` when told). The first learning wins (INSERT OR
    IGNORE). The row is secret when the root is. may_tell is 0, whatever the caller passes, for
    a secret (never passed on) and for a private root learned by someone outside it: the one
    hop was hers, so it goes no further (ruling I8; review M5). One of a private root's own two
    keeps 1, a confided row included: her discretion decides at gossip time. Anything else
    defaults to 1. True when a row was added."""
    with store.transaction():
        return _learn(store.conn, root, guid, how, via=via, ts=ts, confidence=confidence,
                      may_tell=may_tell)


def _learn(conn: sqlite3.Connection, root: str, guid: int, how: str, *, via: int | None,
           ts: float, confidence: float = 1.0, may_tell: bool | int | None = None,
           secret_facts: frozenset[str] | None = None) -> bool:
    """learn, inside the caller's transaction; a stage learning many roots passes the secret
    facts it read once (_secret_facts)."""
    if how not in HOWS:
        raise ValueError(f"how must be one of {HOWS}, not {how!r}")
    secret = _secret(conn, root, secret_facts)
    outside = (not secret and _channel(conn, _base(conn, root)) == "whisper"
               and guid not in _parties(conn, root))
    tell = 0 if secret or outside else 1 if may_tell is None else int(bool(may_tell))
    return conn.execute(
        "INSERT OR IGNORE INTO knows(root, guid, how, via, learned, confidence, secret, "
        "may_tell) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clean(root), guid, how, via, ts, confidence, int(secret), tell)).rowcount == 1


def add_fact(store: Store, *, kind: str, root: str, about: int | None, text: str, secret,
             now: float, holder: int = 0) -> int:
    """A fact about `root` (INSERT OR IGNORE on kind, root and holder), and its id. `holder` is 0
    for a shared fact, or the bystander whose own noticed fact it is (Task 15). Callers pass
    screened text; a real_name fact never holds any. A fact on a public chat line is stored
    with secret 0 whatever the caller says, and fact_secret_refused is logged (ruling B2)."""
    text = "" if kind == "real_name" else clean(text)
    with store.transaction():
        secret = int(bool(secret))
        if secret and _channel(store.conn, root) not in (None, "whisper"):
            log("fact_secret_refused", kind=kind, root=root)
            secret = 0
        store.conn.execute(
            "INSERT OR IGNORE INTO fact(kind, root, about, text, secret, holder, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, clean(root), about, text, secret, holder, now))
        return store.conn.execute("SELECT id FROM fact WHERE kind = ? AND root = ? AND holder = ?",
                                  (kind, clean(root), holder)).fetchone()["id"]


def believe(store: Store, holder: int, other: int, root: str, belief: str, *, now: float) -> None:
    """`holder` believes `other` knows `root` ("yes"), does not ("no"), or is unsure. A "yes"
    never turns back. The human (meta human_guid) is never a holder: he is not simulated, so a
    belief of his is logged as believe_refused and not written (review M2)."""
    if belief not in BELIEFS:
        raise ValueError(f"belief must be one of {BELIEFS}, not {belief!r}")
    if holder == int(store.get_meta("human_guid") or 0):
        log("believe_refused", level=logging.WARNING, holder=holder, root=root)
        return
    with store.transaction():
        store.conn.execute(
            "INSERT INTO thinks_knows(holder, other, root, belief, updated) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(holder, other, root) DO UPDATE SET belief = excluded.belief, "
            "updated = excluded.updated "
            "WHERE thinks_knows.belief != 'yes' OR excluded.belief = 'yes'",
            (holder, other, clean(root), belief, now))


def believes_knows(store: Store, holder: int, other: int,
                   root: str) -> Literal["yes", "no", "unsure"]:
    rows = _rows(store, "SELECT belief FROM thinks_knows WHERE holder = ? AND other = ? "
                        "AND root = ?", (holder, other, root))
    return rows[0]["belief"] if rows else "unsure"


def knowers(store: Store, root: str) -> set[int]:
    """Who the ledger says knows `root` (its knows rows)."""
    return {r["guid"] for r in _rows(store, "SELECT guid FROM knows WHERE root = ?", (root,))}


def known_since(store: Store, root: str, *, before: float) -> int:
    """How many members learned `root` before `before` (Task 16's last-to-know rule)."""
    return _rows(store, "SELECT count(*) AS n FROM knows WHERE root = ? AND learned < ?",
                 (root, before))[0]["n"]


def unknown_to(store: Store, owner: int, other: int, *, since: float, limit: int = 3) -> list[str]:
    """The roots `owner` learned at or after `since` that `other` does not know, newest first.
    One of a root's own parties knows it, ledger row or not, and so may anyone who could have
    seen it (could_have_seen): nothing, then, is unknown to the human."""
    if other == int(store.get_meta("human_guid") or 0):
        return []  # could_have_seen always holds him
    with store.transaction():
        conn = store.conn
        out = []
        for r in conn.execute(
                "SELECT k.root FROM knows k WHERE k.guid = ? AND k.learned >= ? AND NOT EXISTS "
                "(SELECT 1 FROM knows o WHERE o.root = k.root AND o.guid = ?) "
                "ORDER BY k.learned DESC, k.rowid DESC", (owner, since, other)).fetchall():
            if len(out) == limit:
                break
            root = r["root"]
            if other not in _parties(conn, root) and other not in could_have_seen(store, root):
                out.append(root)
        return out


def hops(store: Store, root: str, guid: int) -> int:
    """How many tellings brought `root` to `guid`, following knows.via through told rows (0
    when they did not learn it by being told)."""
    with store.transaction():
        n, cur, seen = 0, guid, set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            r = store.conn.execute("SELECT how, via FROM knows WHERE root = ? AND guid = ?",
                                   (root, cur)).fetchone()
            if r is None or r["how"] != "told":
                break
            n += 1
            cur = r["via"]
        return n


def may_tell(store: Store, guid: int, root: str) -> bool:
    """Whether `guid` may pass `root` on: their row allows it and the root is not secret now (a
    confidential fact can make a root secret after they learned it). False with no row."""
    with store.transaction():
        r = store.conn.execute("SELECT may_tell FROM knows WHERE root = ? AND guid = ?",
                               (root, guid)).fetchone()
        return r is not None and r["may_tell"] == 1 and not _secret(store.conn, root)


def owner0_ok(store: Store, refs: Iterable[str]) -> bool:
    """Whether a shared (owner-0) row may rest on `refs`: none of their roots is secret or
    private, and every memory they go through is open. A secret never becomes a shared row."""
    with store.transaction():
        conn = store.conn
        found, visited = _walk(refs, 3, lambda ids: _memory_rows(conn, ids))
        if any(s != "open" for s in visited.values()):
            return False
        secret_facts = _secret_facts(conn) if found else frozenset()
        return not any(_sensitive(conn, r, secret_facts) for r in found)


def secret_rooted(store: Store, refs: Iterable[str]) -> bool:
    """Any of `refs`' roots is secret: a memory whose own scope is only private can still rest
    on a root a confidential fact made secret (Task 8)."""
    with store.transaction():
        conn = store.conn
        found, _ = _walk(refs, 3, lambda ids: _memory_rows(conn, ids))
        secret_facts = _secret_facts(conn) if found else frozenset()
        return any(_secret(conn, r, secret_facts) for r in found)


def _weaker(stored: tuple[str, int], derived: Scope) -> bool:
    """A stored scope weaker than the derived one: a lower class, or, when the derived class is
    not open, a non-zero partner that differs from the derived one. A partner of 0 reaches
    nobody, so it is never weaker (re-review M1)."""
    cls, partner = stored
    if SCOPES.index(cls) < SCOPES.index(derived.scope):
        return True
    return derived.scope != "open" and partner != 0 and partner != derived.scope_with


def scope_leaks(store: Store) -> list[int]:
    """The ids of active memories that break the privacy rules, by id: one stored weaker than
    its evidence derives (a write that went past the writer); an open one resting on a private
    or secret root (the ledger's view, which also reads P1c's refs); and an owner-0 one that
    fails owner0_ok. Read in one transaction; it must always be empty."""
    with store.transaction():
        conn = store.conn
        everything = {r["id"]: (tuple(json.loads(r["evidence"])), r["scope"])
                      for r in conn.execute("SELECT id, evidence, scope FROM memory")}
        secret_facts = _secret_facts(conn)
        sensitive: dict[str, bool] = {}

        def touchy(root: str) -> bool:
            if root not in sensitive:
                sensitive[root] = _sensitive(conn, root, secret_facts)
            return sensitive[root]
        leaks = []
        for r in conn.execute("SELECT id, owner, evidence, scope, scope_with FROM memory "
                              "WHERE status = 'active' ORDER BY id").fetchall():
            evidence = json.loads(r["evidence"])
            if _weaker((r["scope"], r["scope_with"]), derive_scope(conn, r["owner"], evidence)):
                leaks.append(r["id"])
                continue
            found, visited = _walk(evidence, 3, lambda ids: {i: everything[i] for i in ids
                                                             if i in everything})
            if r["scope"] == "open" and any(touchy(x) for x in found):
                leaks.append(r["id"])
            elif r["owner"] == 0 and (any(s != "open" for s in visited.values())
                                      or any(touchy(x) for x in found)):
                leaks.append(r["id"])
        return leaks


def secret_probe_passed(store: Store, *, now: float, max_rate: float = 0.1) -> bool:
    """The live model has passed the secret-keeping probe (Task 23 writes the rows): the newest
    probe_run is no older than PROBE_MAX_AGE_DAYS, has at least PROBE_MIN_N samples and a leak
    rate of at most `max_rate` (settings.probe_max_rate)."""
    rows = _rows(store, "SELECT ts, n, rate FROM probe_run ORDER BY ts DESC, id DESC LIMIT 1")
    if not rows:
        return False
    r = rows[0]
    return (now - r["ts"] <= PROBE_MAX_AGE_DAYS * DAY_S and r["n"] >= PROBE_MIN_N
            and r["rate"] <= max_rate)


def _facts_on(conn: sqlite3.Connection, roots: Iterable[str]) -> dict[str, list[sqlite3.Row]]:
    """The facts with text on each of `roots` (never the real-name fact), by id, in one read."""
    out: dict[str, list[sqlite3.Row]] = {}
    for f in conn.execute(
            "SELECT id, root, holder, text, created FROM fact WHERE root IN "
            "(SELECT value FROM json_each(?)) AND kind != 'real_name' AND text != '' ORDER BY id",
            (json.dumps(sorted(set(roots))),)):
        out.setdefault(f["root"], []).append(f)
    return out


def secrets_for(store: Store, speaker: int, audience: Iterable[int]) -> list[str]:
    """The texts of facts on roots `speaker` may not pass on that someone in `audience` does not
    know, newest first. The roots are those the speaker knows with may_tell 0, or that are
    secret now, and the secret ones she was part of without a ledger row: her own whisper, or
    a scene she was in (review M4). A root's own parties know it. The speaker's own noticed
    facts count, another bystander's do not. For P1b's later post-check on a line that would
    name a secret (contract note 15); nothing in P1c calls it live."""
    audience = set(audience) - {speaker}
    with store.transaction():
        conn = store.conn
        secret_facts = _secret_facts(conn)
        mine = {r["root"]: r["may_tell"] for r in conn.execute(
            "SELECT root, may_tell FROM knows WHERE guid = ?", (speaker,))}
        held = {root for root, tell in mine.items()
                if not tell or _secret(conn, root, secret_facts)}
        held |= {r["root"] for r in conn.execute(
            "SELECT DISTINCT root FROM fact WHERE root IN (SELECT 'chat:' || id FROM chat_line "
            "WHERE channel = 'whisper' AND ? IN (from_guid, to_guid)) OR root IN "
            "(SELECT 'encounter:' || id FROM encounter WHERE ? IN (a, b))", (speaker, speaker))
            if _secret(conn, r["root"], secret_facts)}
        facts = _facts_on(conn, held)
        found = []
        for root in held:
            known = {r["guid"] for r in conn.execute("SELECT guid FROM knows WHERE root = ?",
                                                     (root,))} | _parties(conn, root)
            if audience - known:
                found += [f for f in facts.get(root, ()) if f["holder"] in (0, speaker)]
    found.sort(key=lambda f: (f["created"], f["id"]), reverse=True)
    return list(dict.fromkeys(f["text"] for f in found))


# -- the stage -----------------------------------------------------------------------------------

def _yes_both_ways(store: Store, a: int, b: int, root: str, *, now: float, human: int) -> None:
    for holder, other in ((a, b), (b, a)):
        if holder not in (0, human, other) and other:
            believe(store, holder, other, root, "yes", now=now)


def _pair_knows(store: Store, root: str, *, now: float, human: int) -> None:
    """The two ends of a whisper root, or the two members of a private encounter, believe "yes"
    of each other."""
    conn = store.conn
    i = _number(root, "encounter")
    if _channel(conn, root) == "whisper":
        private = True
    elif i is not None:
        r = conn.execute("SELECT private FROM encounter WHERE id = ?", (i,)).fetchone()
        private = r is not None and bool(r["private"])
    else:
        private = False
    two = sorted(_parties(conn, root))
    if private and len(two) == 2:
        _yes_both_ways(store, two[0], two[1], root, now=now, human=human)


def _learn_memories(ctx: "NightContext", store: Store) -> dict:
    """Unit "memories": every root of each active memory created in the window whose owner is a
    cast member. Hearsay is told (by actors[0], at the memory's confidence); anything else is
    witnessed. Of an offscreen memory only its encounter: roots count: the other refs it cites
    are what the scene drew on, learned only through gossip (Task 16; review N6). Then the
    second-order rules: a teller and the hearer, the two ends of a whisper and the two members
    of a private scene believe "yes" of each other."""
    start, end = ctx.window
    cast = set(ctx.members(include_away=True))
    conn = store.conn
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM memory WHERE status = 'active' AND owner != 0 AND t_created >= ? "
        "AND t_created < ? ORDER BY t_created, id", (start, end))]
    secret_facts = _secret_facts(conn)  # this unit adds no fact
    learned = 0
    for at in range(0, len(ids), ID_BATCH):  # the first night's window starts at 0 (review M1)
        for m in store.get_memories(ids[at:at + ID_BATCH]):
            if m.owner not in cast:
                continue
            found = roots(store, m.evidence)
            if m.source == "offscreen":
                found = {r for r in found if r.startswith("encounter:")}
            told = m.source == "hearsay"
            via = m.actors[0] if told and m.actors and m.actors[0] != m.owner else None
            for root in sorted(found):
                learned += _learn(conn, root, m.owner, "told" if told else "witnessed",
                                  via=via if told else None, ts=m.t_created,
                                  confidence=m.confidence if told else 1.0,
                                  secret_facts=secret_facts)
                if root == REAL_NAME_ROOT:
                    continue  # his real name is kept by id alone: no second-order rows (M9)
                if told and via is not None:
                    _yes_both_ways(store, m.owner, via, root, now=m.t_created, human=ctx.human)
                _pair_knows(store, root, now=m.t_created, human=ctx.human)
    return {"memories": len(ids), "learned": learned}


def _learn_audiences(ctx: "NightContext", store: Store) -> dict:
    """Unit "audience": for each fact created in the window on a non-whisper chat line, the
    line's own people (its speaker, and its addressee when it has one) know it as witnessed,
    and every other member of its audience who does not know it learns it as overheard; they
    did not notice that member. Then his real name, by id only."""
    start, end = ctx.window
    cast = set(ctx.members(include_away=True))
    now = ctx.clock.now()
    conn = store.conn
    secret_facts = _secret_facts(conn)  # this unit adds only public and real-name facts
    overheard = 0
    for r in conn.execute(
            "SELECT DISTINCT root FROM fact WHERE created >= ? AND created < ? "
            "AND kind != 'real_name' AND substr(root, 1, 5) = 'chat:' ORDER BY root",
            (start, end)).fetchall():
        root = r["root"]
        line = conn.execute("SELECT id, ts, channel FROM chat_line WHERE id = ?",
                            (_number(root, "chat"),)).fetchone()
        if line is None or line["channel"] == "whisper":
            continue
        own = _parties(conn, root)
        for p in sorted(own & cast):
            _learn(conn, root, p, "witnessed", via=None, ts=line["ts"], secret_facts=secret_facts)
        for g in sorted(audience_of(store, line["id"]) - own):
            if _learn(conn, root, g, "overheard", via=None, ts=line["ts"],
                      secret_facts=secret_facts):
                overheard += 1
                for p in own - {g, ctx.human}:
                    believe(store, p, g, root, "no", now=now)
    return {"overheard": overheard,
            "real_name": _learn_real_name(ctx, store, cast, secret_facts)}


def _learn_real_name(ctx: "NightContext", store: Store, cast: set[int],
                     secret_facts: frozenset[str]) -> int:
    """Each line in the window by a cast member that names him by his real name (P1b's matcher:
    the configured spelling and its Title and UPPER forms, never the lower-case word; ruling
    I4): the fact once, its speaker as witnessed and its audience as overheard. The name itself
    is never stored, and the fact gets no second-order rows. Returns how many lines named him."""
    if not ctx.ident.real_name:
        return 0
    start, end = ctx.window
    named = [r for r in store.conn.execute(
        "SELECT id, ts, from_guid, text FROM chat_line WHERE ts >= ? AND ts < ? ORDER BY ts, id",
        (start, end)).fetchall()
        if r["from_guid"] in cast and names_real_name(r["text"], ctx.ident)]
    if not named:
        return 0
    add_fact(store, kind="real_name", root=REAL_NAME_ROOT, about=ctx.human, text="", secret=0,
             now=ctx.clock.now())
    for r in named:  # the speakers first: one who also heard another say it still said it
        _learn(store.conn, REAL_NAME_ROOT, r["from_guid"], "witnessed", via=None, ts=r["ts"],
               secret_facts=secret_facts)
    for r in named:
        for g in sorted(audience_of(store, r["id"])):
            _learn(store.conn, REAL_NAME_ROOT, g, "overheard", via=None, ts=r["ts"],
                   secret_facts=secret_facts)
    return len(named)


@dataclass(frozen=True)
class _Reminder:
    holder: int
    other: int
    root: str
    fact: int
    text: str
    secret: bool
    learned: float

    @property
    def key(self) -> str:
        return f"tom:{self.other}:{self.fact}"


def _reminders_due(ctx: "NightContext") -> tuple[list[_Reminder], int]:
    """The reminders to write, and how many wait for the secret-keeping probe. For each member's
    "no" belief about a holder who learned the root in the last REMINDER_DAYS, as witnessed or
    overheard, of a root with a fact (never the real-name fact): the holder's own fact when
    there is one, else the shared one, and "<them> doesn't know you know". One join from the few
    "no" beliefs, then one read of their facts, never a scan per knows row (review I1); the
    human is never the believer (review M2)."""
    since = ctx.window[1] - REMINDER_DAYS * DAY_S
    cast = set(ctx.members(include_away=True))
    passed: bool | None = None
    due, waiting = [], 0
    with ctx.store.transaction():
        conn = ctx.store.conn
        pairs = conn.execute(
            "SELECT k.root, k.guid, k.learned, t.holder AS other FROM thinks_knows t "
            "JOIN knows k ON k.root = t.root AND k.guid = t.other "
            "WHERE t.belief = 'no' AND t.holder != t.other AND t.holder != ? "
            "AND k.how IN ('witnessed', 'overheard') AND k.learned >= ? AND k.root != ? "
            "ORDER BY k.learned, k.root, k.guid, t.holder",
            (ctx.human, since, REAL_NAME_ROOT)).fetchall()
        if not pairs:
            return [], 0
        facts = _facts_on(conn, (p["root"] for p in pairs))
        secret_facts = _secret_facts(conn)
        secret: dict[str, bool] = {}
        for p in pairs:
            holder, root, other = p["guid"], p["root"], p["other"]
            on_root = facts.get(root, ())
            fact = next((f for f in on_root if f["holder"] == holder),
                        next((f for f in on_root if f["holder"] == 0), None))
            if holder not in cast or fact is None:
                continue
            if root not in secret:
                secret[root] = _secret(conn, root, secret_facts)
            if secret[root]:
                if passed is None:
                    passed = secret_probe_passed(ctx.store, now=ctx.clock.now(),
                                                 max_rate=ctx.settings.probe_max_rate)
                if not passed:
                    waiting += 1
                    continue
            if ctx.store.keyed_memory(holder, f"tom:{other}:{fact['id']}") is not None:
                continue
            try:
                name = ctx.name(other)
            except KeyError:
                continue
            text = clean_memory_text(f"{fact['text']} {name} doesn't know you know.", ctx.ident)
            due.append(_Reminder(holder, other, root, fact["id"], text, secret[root],
                                 p["learned"]))
    return due, waiting


async def _remind(ctx: "NightContext") -> None:
    """Unit "reminders": the memo's SimpleToM reminder, so telling becomes a decision, not an
    accident. Each is the holder's reflection (importance 5, actors [other], key
    tom:{other}:{fact id}, evidence [root, rule:fact:{id}]), inserted only when absent and put
    through the writer, which derives its scope; a secret's carries the request "secret", so it
    reaches no live prompt. A secret's waits until the live model has passed the probe."""
    due, waiting = _reminders_due(ctx)
    if waiting:
        log("nightly_reminders_wait", night=ctx.night, n=waiting)
    if not due:
        ctx.skip(STAGE, "reminders")
        return
    vecs = await ctx.embed([d.text for d in due])  # before the transaction: nothing awaits in it
    now = int(ctx.clock.now())

    def write(store: Store) -> None:
        for d, vec in zip(due, vecs):
            if store.keyed_memory(d.holder, d.key) is not None:
                continue
            ctx.writer.put(MemoryRow(
                id=None, owner=d.holder, kind="reflection", text=d.text, t_event=int(d.learned),
                t_created=now, t_accessed=now, access_n=0, actors=(d.other,), place=None,
                boss_id=None, raid_id=None, importance=5, valence=0.0, source="reflection",
                evidence=(d.root, f"rule:fact:{d.fact}"), key=d.key, embedding=vec,
                scope="secret" if d.secret else "open"))
    if ctx.commit(STAGE, "reminders", write):
        log("nightly_knows", night=ctx.night, unit="reminders", written=len(due))


async def stage_knows(ctx: "NightContext") -> None:
    """Stage "knows", no model, three units in order: "memories" (how each member learned what
    their new memories rest on), "audience" (who overheard a fact's public line, and who knows
    his real name) and "reminders". A unit that fails is that unit's failure only."""
    for unit, write in (("memories", _learn_memories), ("audience", _learn_audiences)):
        if not ctx.done(STAGE, unit):
            counts: dict = {}
            if ctx.commit(STAGE, unit, lambda store, write=write, counts=counts:
                          counts.update(write(ctx, store))):
                log("nightly_knows", night=ctx.night, unit=unit, **counts)
    if not ctx.done(STAGE, "reminders"):
        try:
            await _remind(ctx)
        except Exception as e:  # a requested cancel is no Exception: it goes on up
            ctx.fail(STAGE, "reminders", e)
