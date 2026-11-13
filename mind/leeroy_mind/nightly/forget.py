"""Weekly, grounded folding of small memories without losing their private provenance.

The weekly plan freezes chunk identities before any model call. Successful siblings can
archive their sources without renumbering a failed chunk on the next night's resume.
"""
import json
import re
from collections import defaultdict

from leeroy_mind.discord.channels import is_officer_channel
from leeroy_mind.memory.privacy import OFFICERS
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.evidence import Evidence, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, check, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.store import step_finished_at
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.provenance import ProvenanceError, edges
from leeroy_mind.text import strip_wow_markup

STAGE = "forget"
DAY_S = 86400
CHUNK_MAX = 40
PROVENANCE_MAX = 200
PROTECTED_KEYS = ("summary:", "diary:", "jealousy:", "hours:", "hurt:", "tom:",
                  "nick:", "joke:", "lands:", "followup:", "absent:", "recap:",
                  "chronicle:", "award:", "emotion:", "disclose:", "correct:", "boss:",
                  "owner:")  # his own history (memories seed) is never folded away
SUMMARY_SCHEMA = schema("summary", {"text": {"type": "string", "maxLength": 200}})
_NUMBERED = re.compile(r"([a-z_]+):([0-9]+)")
_FACT = re.compile(r"rule:fact:([0-9]+)")
# The owner's authored history: "owner:<moment>", the moment as memories seed takes it. Only his
# own seeded memories may cite one (MemoryWriter), and recall treats it as public.
_OWNER = re.compile(r"owner:[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_TABLES = {"chat": "chat_line", "event": "game_event", "session": "session",
           "story": "story", "chronicle": "chronicle", "encounter": "encounter",
           "beat": "life_beat", "life": "life_thread", "away": "away", "feud": "feud",
           "mentor": "mentor", "award": "award", "joke": "lexicon"}


def candidates(store, owner, now):
    """Unrecalled small memories older than seven days, oldest event first (ties by id)."""
    with store.transaction():
        used = {ref for row in store.conn.execute("SELECT evidence FROM story")
                for ref in json.loads(row[0]) if isinstance(ref, str)}
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM memory WHERE owner=? AND status='active' AND t_created<? "
            "AND importance<=3 AND access_n=0 AND kind NOT IN ('story','plan') "
            "ORDER BY t_event,id", (owner, now - 7 * DAY_S))]
        return [m for m in store.get_memories(ids)
                if not (m.key or "").startswith(PROTECTED_KEYS)
                and not any(ref.startswith("story:") for ref in m.evidence)
                and f"memory:{m.id}" not in used]


def _node(store, ref):
    """Snapshot one persisted provenance node, including privacy outside its own row."""
    try:
        children = edges(store.conn, ref, recalls=False).children
    except ProvenanceError:
        return None
    fact = _FACT.fullmatch(ref)
    match = _NUMBERED.fullmatch(ref)
    if match and match.group(1) == "memory":
        rows = store.get_memories([int(match.group(2))])
        if not rows or rows[0].status == "rejected":
            return None
        m = rows[0]
        # Access timestamps/vectors are not provenance. Source eligibility separately checks
        # access_n, so a newly recalled source cannot be archived after an await.
        value = (m.id, m.owner, m.kind, m.text, m.t_event, m.t_created, m.actors,
                 m.place, m.boss_id, m.raid_id, m.importance, m.valence, m.source,
                 m.evidence, m.confidence, m.status, m.key, m.scope, m.scope_with)
        return value, children, m.owner, (m.scope, m.scope_with), frozenset()
    if fact:
        row = store.conn.execute("SELECT * FROM fact WHERE id=?", (int(fact.group(1)),)).fetchone()
    elif match and match.group(1) in _TABLES:
        kind, ident = match.group(1), int(match.group(2))
        row = store.conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id=?", (ident,)).fetchone()
    elif ref.startswith("rule:") or _OWNER.fullmatch(ref):
        # Deterministic rule roots and his authored history have no backing table; their
        # confidential fact flags do.
        row, children = (ref,), ()
    else:
        return None
    if row is None or any(not isinstance(child, str) for child in children):
        return None
    scope = "secret" if knowledge.is_secret(store, ref) else (
        "private" if knowledge.is_private(store, ref) else "open")
    parties = knowledge.parties(store, ref)
    partner = 0
    if scope == "private" and ref.startswith("chat:"):
        # an officers' line is the officers' (one audience in either medium), as the writer
        # derives it (privacy.OFFICERS); any other restricted or private root reaches nobody here
        line = store.conn.execute("SELECT channel FROM chat_line WHERE id=?",
                                  (int(ref.split(":", 1)[1]),)).fetchone()
        if line is not None and is_officer_channel(line[0]):
            partner = OFFICERS
    return (tuple(row), scope, tuple(sorted(parties))), children, None, (scope, partner), parties


def _snapshot(store, chunk):
    """Complete comparable ancestry, or None when incomplete/cyclic or under-scoped.

    Scope is propagated through each memory's owner, preserving a legitimate private
    retelling's sharing partner. An old open wrapper around a whisper cannot become an
    open summary merely because the writer only follows direct references.
    """
    # Mood's full walker reuses this module's persisted-node reader. Import it only
    # at call time to avoid a top-level cycle, and include its paired-chat recall
    # links and retained lexicon origins in the comparable snapshot.
    from leeroy_mind.nightly.mood import _evidence_scope_state

    nodes = {}
    for memory in chunk:
        state = _evidence_scope_state(store, memory.owner, [f"memory:{memory.id}"])
        if state is None or state[0] != (memory.scope, memory.scope_with):
            return None
        nodes.update(state[1])
        if len(nodes) > PROVENANCE_MAX:
            return None
    return tuple(sorted(nodes.items()))


def _unit(week, owner, n):
    return f"{week}:{owner}:{n}"


def _plan_key(week):
    return f"nightly:forget-plan:{week}"


def _planned(ctx, week):
    raw = ctx.store.get_meta(_plan_key(week))
    if raw is not None:
        return json.loads(raw)
    plan = []
    for owner in ctx.members():
        groups = defaultdict(list)
        for m in candidates(ctx.store, owner, ctx.clock.now()):
            groups[(m.scope, m.scope_with)].append(m.id)
        n = 0
        for scope, ids in sorted(groups.items()):
            for start in range(0, len(ids), CHUNK_MAX):
                plan.append({"owner": owner, "n": n, "scope": list(scope),
                             "ids": ids[start:start + CHUNK_MAX]})
                n += 1
    return plan


def count_pending(ctx):
    """Read-only budget count: one JSON call per unfinished frozen/prospective chunk."""
    week = iso_week(ctx.night)
    with ctx.store.transaction():
        if step_finished_at(ctx.store, STAGE, week) is not None:
            return 0
        return sum(step_finished_at(ctx.store, STAGE, _unit(week, p["owner"], p["n"])) is None
                   for p in _planned(ctx, week))


def _current(ctx, owner, chunk):
    if owner not in ctx.members() or not 1 <= len(chunk) <= CHUNK_MAX:
        return None
    ids = [m.id for m in chunk]
    if len(set(ids)) != len(ids) or any(m.owner != owner for m in chunk):
        return None
    eligible = {m.id: m for m in candidates(ctx.store, owner, ctx.clock.now())}
    if any(i not in eligible for i in ids):
        return None
    rows = [eligible[i] for i in ids]
    if len({(m.scope, m.scope_with) for m in rows}) != 1:
        return None
    return rows


async def summarize(ctx, owner, chunk, n):
    """One checked summary and its archived originals, accepted in one transaction."""
    # The shared subject/probe policy walks _node through mood; defer this import
    # until both modules are initialized, just as _snapshot does above.
    from leeroy_mind.nightly.mood import secret_memory_ok

    week = iso_week(ctx.night)
    unit = _unit(week, owner, n)
    with ctx.store.transaction():
        if step_finished_at(ctx.store, STAGE, week) is not None or (
                step_finished_at(ctx.store, STAGE, unit) is not None):
            return
        current = _current(ctx, owner, chunk)
        before = _snapshot(ctx.store, current) if current else None
        if before is None:
            ctx.skip(STAGE, unit)  # unsafe/stale input is intentionally left untouched
            return
        chunk = current
        scope = (chunk[0].scope, chunk[0].scope_with)
        refs = [f"memory:{m.id}" for m in chunk]
        if not secret_memory_ok(ctx, owner, refs):
            return  # wait with sources, frozen chunk and checkpoint untouched
        ev = Evidence()
        for item in load_items(ctx.store, refs, ident=ctx.ident):
            ev.add(item)
        facts = load_facts(ctx)
    system = system_prompt("summary", "Fold these small old memories into one brief factual "
        "memory. Keep the people, events and numbers true to the evidence. Do not add events.",
        ctx.settings.data_dir, human=ctx.ident)
    user = (f"These are {ctx.name(owner)}'s memories:\n{ev.render(ctx.tz)}\n"
            "Summarize their common thread in at most 200 characters.")
    reply = await ctx.reasoner.json(system, user, SUMMARY_SCHEMA)
    if not secret_memory_ok(ctx, owner, refs):
        return  # the probe or subject may have changed during generation
    text = " ".join(strip_wow_markup(reply["text"]).split())
    claim = Claim("summary", owner, text, refs)
    verdict = check(claim, ev, facts)
    vectors = await ctx.embed([text]) if verdict.ok else []
    if verdict.ok and len(vectors) != 1:
        raise ValueError("Summary received an incomplete embedding batch")
    if not secret_memory_ok(ctx, owner, refs):
        return  # embedding also awaits; a blocked unit remains retryable

    def write(store):
        fresh = _current(ctx, owner, chunk)
        if (fresh is None or _snapshot(store, fresh) != before
                or not secret_memory_ok(ctx, owner, refs)):
            raise ValueError("Summary evidence changed while it was generated")
        now = int(ctx.clock.now())
        log_verdicts(store, ctx.night, STAGE, [(claim, verdict)], now, ctx.ident)
        if not verdict.ok:
            return
        actors = tuple(dict.fromkeys(g for m in chunk for g in m.actors))[:10]
        row = MemoryRow(id=None, owner=owner, kind="observation", text=text,
            t_event=max(m.t_event for m in chunk), t_created=now, t_accessed=now,
            access_n=0, actors=actors, place=None, boss_id=None, raid_id=None,
            importance=3, valence=0, source="reflection", evidence=tuple(refs),
            key=f"summary:{week}:{n}", embedding=vectors[0],
            scope=scope[0], scope_with=scope[1])
        ctx.writer.put(row, shown=refs)
        store.conn.executemany("UPDATE memory SET status='archived' WHERE id=?",
                               [(m.id,) for m in chunk])

    ctx.commit(STAGE, unit, write)


async def stage_forget(ctx):
    week = iso_week(ctx.night)
    with ctx.store.transaction():
        if step_finished_at(ctx.store, STAGE, week) is not None:
            return
        plan = _planned(ctx, week)
        if ctx.store.get_meta(_plan_key(week)) is None:
            ctx.store.set_meta(_plan_key(week), json.dumps(plan))
        pending = {_unit(week, p["owner"], p["n"]): p for p in plan
                   if step_finished_at(ctx.store, STAGE, _unit(week, p["owner"], p["n"])) is None}

    async def one(unit):
        p = pending[unit]
        chunk = ctx.store.get_memories(p["ids"])
        if len(chunk) != len(p["ids"]) or any((m.scope, m.scope_with) != tuple(p["scope"])
                                              for m in chunk):
            ctx.skip(STAGE, unit)
            return
        await summarize(ctx, p["owner"], chunk, p["n"])

    await ctx.map_units(STAGE, list(pending), one)
    with ctx.store.transaction():
        if all(step_finished_at(ctx.store, STAGE, _unit(week, p["owner"], p["n"])) is not None
               for p in plan) and step_finished_at(ctx.store, STAGE, week) is None:
            ctx.skip(STAGE, week)
