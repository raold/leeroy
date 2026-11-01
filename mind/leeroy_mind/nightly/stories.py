"""Public guild stories, with complete provenance and replay-safe retelling counts."""
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.evidence import Evidence, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, check, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.settings import iso_week, season_of
from leeroy_mind.nightly.store import insert_story
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.text import strip_wow_markup, truncate_words

STAGE = "stories"
PROVENANCE_MAX = 5000
_NUMBERED = re.compile(r"(memory|story|chronicle|chat|event|session):([0-9]+)")
_FACT = re.compile(r"rule:fact:([0-9]+)")
STORY_SCHEMA = schema("story", {
    "title": {"type": "string", "maxLength": 60},
    "summary": {"type": "string", "maxLength": 400},
    "first_person": {"type": "string", "maxLength": 200},
    "evidence": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
})


@dataclass
class Moment:
    key: str
    owners: set[int]
    memory_ids: list[int]
    refs: list[str]


def _json_list(text):
    try:
        values = json.loads(text)
    except (TypeError, ValueError):
        return None
    return values if isinstance(values, list) else None


def _public_node(store, ref):
    """A stable node snapshot and its children; volatile access counters are excluded."""
    if knowledge.is_secret(store, ref) or knowledge.is_private(store, ref):
        return None
    fact = _FACT.fullmatch(ref)
    match = _NUMBERED.fullmatch(ref)
    if fact:
        row = store.conn.execute("SELECT * FROM fact WHERE id=?", (int(fact.group(1)),)).fetchone()
        if row is None or row["secret"]:
            return None
        return tuple(row), (row["root"],), False
    if match is None:
        return None
    kind, ident = match.group(1), int(match.group(2))
    if kind == "memory":
        rows = store.get_memories([ident])
        if not rows:
            return None
        m = rows[0]
        if m.scope != "open" or m.status not in {"active", "superseded", "archived"}:
            return None
        fields = (m.id, m.owner, m.kind, m.text, m.t_event, m.t_created, m.actors,
                  m.place, m.boss_id, m.raid_id, m.importance, m.valence, m.source,
                  m.evidence, m.confidence, m.status, m.key, m.scope, m.scope_with)
        return fields, m.evidence, False
    if kind == "story":
        row = store.conn.execute("SELECT id,title,summary,participants,evidence,t_created "
                                  "FROM story WHERE id=?", (ident,)).fetchone()
    elif kind == "chronicle":
        row = store.conn.execute("SELECT * FROM chronicle WHERE id=?", (ident,)).fetchone()
    elif kind == "chat":
        row = store.conn.execute("SELECT id,ts,channel,from_guid,from_name,to_guid,text,thread_id "
                                  "FROM chat_line WHERE id=?", (ident,)).fetchone()
    elif kind == "event":
        row = store.conn.execute("SELECT id,ts,type,payload FROM game_event WHERE id=?", (ident,)).fetchone()
    else:
        row = store.conn.execute("SELECT id,started,ended FROM session WHERE id=?", (ident,)).fetchone()
    if row is None:
        return None
    children = _json_list(row["evidence"]) if kind in {"story", "chronicle"} else []
    if children is None or any(not isinstance(child, str) for child in children):
        return None
    return tuple(row), tuple(children), kind in {"chat", "event", "session"}


def public_snapshot(store, refs):
    """A comparable complete public history, or None for sensitive/incomplete/cyclic input.

    Persisted refs only: a caller's computed count item snapshots its backing event refs.
    Historical archived/superseded sources remain evidence; rejected rows do not. Unlike
    owner0_ok's short memory walk, this follows story and chronicle evidence too.
    """
    refs = tuple(dict.fromkeys(refs))
    if any(not isinstance(ref, str) for ref in refs):
        return None
    with store.transaction():
        snapshots, visiting, finished = {}, set(), set()
        stack = [(ref, False) for ref in reversed(refs)]
        durable = False
        while stack:
            ref, leaving = stack.pop()
            if leaving:
                visiting.remove(ref)
                finished.add(ref)
                continue
            if ref in visiting:
                return None
            if ref in finished:
                continue
            if len(snapshots) >= PROVENANCE_MAX:
                return None
            node = _public_node(store, ref)
            if node is None:
                return None
            value, children, original = node
            if any(not isinstance(child, str) for child in children):
                return None
            snapshots[ref] = value
            durable = durable or original
            visiting.add(ref)
            stack.append((ref, True))
            stack.extend((child, False) for child in reversed(children))
        if not durable or not knowledge.owner0_ok(store, refs):
            return None
        return tuple(sorted(snapshots.items()))


def _used_memories(store):
    used = set()
    for row in store.conn.execute("SELECT evidence FROM story"):
        used.update(ref for ref in (_json_list(row[0]) or [])
                    if isinstance(ref, str) and ref.startswith("memory:"))
    # Older story copies may carry the source refs even when the story row did not.
    for row in store.conn.execute("SELECT id,evidence FROM memory"):
        refs = _json_list(row["evidence"]) or []
        if any(isinstance(ref, str) and ref.startswith("story:") for ref in refs):
            used.add(f"memory:{row['id']}")
            used.update(ref for ref in refs if isinstance(ref, str) and ref.startswith("memory:"))
    return used


def find_moments(store, window, *, min_owners=3, min_importance=7) -> list[Moment]:
    """Sift witnessed moments, rejecting an entire group if any witness is private."""
    with store.transaction():
        ids = [row[0] for row in store.conn.execute("SELECT id FROM memory WHERE status='active' "
            "AND owner!=0 AND t_event>=? AND t_event<? AND importance>=? "
            "AND source NOT IN ('offscreen','hearsay','reflection') ORDER BY id",
            (*window, min_importance))]
        grouped = defaultdict(list)
        for m in store.get_memories(ids):
            root = next((ref for ref in m.evidence if ref.startswith(("chat:", "event:"))), None)
            key = m.key if m.key and m.key.startswith("boss:") else f"ev:{root}" if root else None
            if key is not None:
                grouped[key].append(m)
        used = _used_memories(store)
        found = []
        for key, memories in grouped.items():
            owners = {m.owner for m in memories}
            memory_refs = [f"memory:{m.id}" for m in memories]
            if len(owners) < min_owners or any(ref in used for ref in memory_refs):
                continue
            snapshot = public_snapshot(store, memory_refs)
            if snapshot is None:
                continue
            refs = [ref for ref, _ in snapshot if not ref.startswith("memory:")]
            found.append((sum(m.importance for m in memories),
                          Moment(key, owners, [m.id for m in memories], refs)))
        return [m for _, m in sorted(found, key=lambda entry: (-entry[0], entry[1].key))]


def _current(ctx, moment):
    return next((m for m in find_moments(ctx.store, (0, ctx.window[1])) if m.key == moment.key), None)


def _text(value):
    return " ".join(strip_wow_markup(value).split())


def _memory(owner, text, actors, refs, now, embedding):
    return MemoryRow(id=None, owner=owner, kind="story", text=text, t_event=int(now),
        t_created=int(now), t_accessed=int(now), access_n=0, actors=tuple(actors), place=None,
        boss_id=None, raid_id=None, importance=8, valence=0, source="reflection",
        evidence=tuple(refs), embedding=embedding)


async def write_story(ctx, m):
    unit = f"story:{m.key}"
    if ctx.done(STAGE, unit):
        return
    with ctx.store.transaction():
        if _current(ctx, m) != m:
            return
        participants = sorted(m.owners & set(ctx.members(include_away=True)))
        if not participants:
            return
        refs = [*(f"memory:{ident}" for ident in m.memory_ids),
                *(ref for ref in m.refs if ref.startswith("event:")),
                *[ref for ref in m.refs if ref.startswith("chat:")][:12]]
        refs = tuple(dict.fromkeys(refs))
        before = public_snapshot(ctx.store, refs)
        if before is None:
            return
        ev = Evidence()
        for item in load_items(ctx.store, refs, ident=ctx.ident):
            ev.add(item)
        if len(ev) != len(refs):
            return
        facts = load_facts(ctx)
    system = system_prompt("story", "Write a short story the guild remembers about this real "
        "shared moment. Keep its events, people, bosses and numbers true to the evidence.",
        ctx.settings.data_dir, human=ctx.ident)
    user = (f"Participants: {', '.join(ctx.name(g) for g in participants)}\n"
            f"Evidence:\n{ev.render(ctx.tz)}\nGive a title, a short factual summary and a "
            "first-person memory the participants can each recall. Cite the bracketed evidence aliases.")
    notes = await ctx.reasoner.think(system, user)
    reply = await ctx.reasoner.json(system, user, STORY_SCHEMA, notes=notes, temperature=0.8)
    title, summary, first = (_text(reply[key]) for key in ("title", "summary", "first_person"))
    cited = list(dict.fromkeys(ev.ref(alias) for alias in reply["evidence"]))
    title_claim = Claim("story", 0, title, cited)
    title_verdict = check(title_claim, ev, facts)
    claims = [Claim("story", 0, summary, cited), Claim("story", participants[0], first, cited)]
    pairs = [(title_claim, title_verdict), *await ground(ctx, claims, ev, facts)]
    accepted = all(verdict.ok for _, verdict in pairs)
    shared = truncate_words(f"{title}: {summary}", 200)
    vectors = await ctx.embed([shared, first]) if accepted else []
    if accepted and len(vectors) != 2:
        raise ValueError("Story received an incomplete embedding batch")

    def write(store):
        if public_snapshot(store, refs) != before or _current(ctx, m) != m:
            raise ValueError("Story evidence changed while it was generated")
        if sorted(m.owners & set(ctx.members(include_away=True))) != participants:
            raise ValueError("Story participants changed while it was generated")
        now = ctx.clock.now()
        log_verdicts(store, ctx.night, STAGE, pairs, now, ctx.ident)
        if not accepted:
            return
        # Keep every shown source in the durable story row even if the model cited
        # a smaller subset. It is both publication provenance and the dedup record.
        ident = insert_story(store, title=title, summary=summary, participants=participants,
                             evidence=refs, ts=now)
        store.set_meta(f"nightly:story-night:{ident}", ctx.night)
        root = f"story:{ident}"
        ctx.writer.put(_memory(0, shared, participants[:8], (root, *refs)[:20], now, vectors[0]), shown=refs)
        for owner in participants:
            ctx.writer.put(_memory(owner, first, [g for g in participants if g != owner][:8],
                                   (root,), now, vectors[1]), shown=refs)

    ctx.commit(STAGE, unit, write)


def count_retells(store, window) -> int:
    """Count each sent line/story once, even across overlapping windows or deleted copies.

    A tiny receipt per actual retelling stores only story IDs in existing metadata;
    the receipt and counters share a transaction. Deleting old source memories later
    cannot erase a previously counted retelling or make a retry count it twice.
    """
    with store.transaction():
        lines = store.conn.execute("SELECT id,memory_ids FROM utterance WHERE dropped IS NULL "
                                   "AND ts>=? AND ts<? ORDER BY id", window).fetchall()
        counts, receipts, changed = Counter(), {}, 0
        for line in lines:
            key = f"nightly:story-retell:{line['id']}"
            if store.get_meta(key) is not None:
                continue  # Later edits to a recalled memory cannot rewrite this spoken line.
            ids = _json_list(line["memory_ids"]) or []
            ids = [ident for ident in ids if isinstance(ident, int) and not isinstance(ident, bool)]
            story_ids = {int(ref.split(":", 1)[1]) for m in store.get_memories(ids)
                         for ref in m.evidence if ref.startswith("story:")
                         and ref.split(":", 1)[1].isdecimal()}
            story_ids = {ident for ident in story_ids if store.conn.execute(
                "SELECT 1 FROM story WHERE id=?", (ident,)).fetchone() is not None}
            if not story_ids:
                continue
            changed += 1
            counts.update(story_ids)
            receipts[key] = json.dumps(sorted(story_ids))
        for ident, count in counts.items():
            store.conn.execute("UPDATE story SET retold_n=retold_n+? WHERE id=?", (count, ident))
        for key, value in receipts.items():
            store.set_meta(key, value)
        return changed


async def stage_stories(ctx):
    # Search the unrecorded backlog up to this night's end. Restricting this to the
    # current window would permanently strand the moments left by yesterday's cap.
    with ctx.store.transaction():
        completed = {row[0] for row in ctx.store.conn.execute(
            "SELECT unit FROM night_step WHERE night=? AND stage=? AND unit LIKE 'story:%'",
            (ctx.night, STAGE))}
        room = max(0, ctx.settings.stories_per_night - len(completed))
        pending = [m for m in find_moments(ctx.store, (0, ctx.window[1]))
                   if f"story:{m.key}" not in completed][:room]
    moments = {f"story:{m.key}": m for m in pending}
    await ctx.map_units(STAGE, list(moments), lambda unit: write_story(ctx, moments[unit]))
    from leeroy_mind.nightly.chronicle import stage_awards, write_chronicle, write_recap
    extras = ((f"recap:{ctx.night}", write_recap),
              (f"chronicle:{iso_week(ctx.night)}", write_chronicle),
              (f"awards:{season_of(ctx.night, ctx.settings.season_weeks)}", stage_awards))
    for unit, fn in extras:
        # The chronicle sees a successful recap, but one failed model unit does not
        # prevent independent awards or retelling bookkeeping from completing.
        await ctx.map_units(STAGE, [unit], lambda _, fn=fn: fn(ctx))
    if not ctx.done(STAGE, "retell"):
        ctx.commit(STAGE, "retell", lambda store: count_retells(store, ctx.window))
