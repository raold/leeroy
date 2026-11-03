"""One grounded, fictional offscreen life beat per member and night (P1c Task 13)."""
import json

from leeroy_mind.nightly import wowlife
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
from leeroy_mind.nightly.ground import Claim, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.text import strip_wow_markup

STAGE = "life"
DAY = 86400.0
BEAT_SCHEMA = schema("life_beat", {
    "beat": {"type": "string", "maxLength": 200},
    "mood": {"type": "number", "minimum": -1, "maximum": 1},
    "resolved": {"type": "boolean"},
    "next_title": {"type": ["string", "null"], "maxLength": 80},
})
PROMPT = "Write the next small event in {name}'s life away from the game.\n{card}"


def ensure_thread(store, guid, title, now, rng, *, dial=5) -> int:
    """Keep an active thread, or start one and compose its title onto today's mind."""
    with store.transaction():
        row = store.conn.execute(
            "SELECT id FROM life_thread WHERE guid=? AND status='active' ORDER BY id LIMIT 1",
            (guid,)).fetchone()
        if row is not None:
            return row["id"]
        due = now + rng.uniform(1, 3) * DAY
        maximum = rng.randint(3, 8)
        tid = store.conn.execute(
            "INSERT INTO life_thread(guid,title,beat_n,beats_max,next_beat_at,status,started) "
            "VALUES (?,?,0,?,?,'active',?)", (guid, title, maximum, due, now)).lastrowid
        wowlife.refresh_on_mind(store, guid, dial=dial)
        return tid


def due_threads(store, now):
    with store.transaction():
        return store.conn.execute(
            "SELECT * FROM life_thread WHERE status='active' AND next_beat_at<=? "
            "ORDER BY next_beat_at,id", (now,)).fetchall()


def _has_beat(store, night, guid):
    return store.conn.execute(
        "SELECT 1 FROM life_beat b JOIN life_thread t ON t.id=b.thread_id "
        "WHERE b.night=? AND t.guid=? LIMIT 1", (night, guid)).fetchone() is not None


async def advance(ctx, thread):
    guid, tid = thread["guid"], thread["id"]
    unit = str(guid)
    if ctx.done(STAGE, unit):
        return
    with ctx.store.transaction():
        current = ctx.store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
        already = _has_beat(ctx.store, ctx.night, guid)
        recent = ctx.store.conn.execute(
            "SELECT text FROM life_beat WHERE thread_id=? ORDER BY ts DESC,id DESC LIMIT 3",
            (tid,)).fetchall()
    if already:
        ctx.skip(STAGE, unit)
        return
    if (current is None or current["guid"] != guid or current["status"] != "active"
            or current["next_beat_at"] > ctx.clock.now()):
        return
    thread = current
    system = system_prompt("life", PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=ctx.name(guid), card=ctx.card(guid))
    user = (f"Life thread: {thread['title']}\nLast three beats:\n"
            + "\n".join(r["text"] for r in reversed(recent))
            + "\nOne sentence: what happened next in their life away from the game. "
            "Give its mood, whether the thread has resolved, and an optional next thread title.")
    reply = await ctx.reasoner.json(system, user, BEAT_SCHEMA, temperature=0.8)
    reply["beat"] = " ".join(strip_wow_markup(reply["beat"]).split())
    if reply["next_title"] is not None:
        reply["next_title"] = " ".join(strip_wow_markup(reply["next_title"]).split())
    count = thread["beat_n"] + 1
    resolved = (reply["resolved"] and count >= 3) or count >= thread["beats_max"]
    ref = f"life:{tid}"
    ev = Evidence()
    ev.add(Item(ref, thread["title"], frozenset({guid}), thread["started"]))
    claims = [Claim("life", guid, reply["beat"], [ref], valence=reply["mood"])]
    # A replacement title is persisted generated text too. It must pass the same
    # name and fiction invariants as the beat, even though it is not a memory.
    if resolved and reply["next_title"] is not None:
        claims.append(Claim("life", guid, reply["next_title"], [ref]))
    verdicts = await ground(ctx, claims, ev, load_facts(ctx))
    if not all(v.ok for _, v in verdicts):
        ctx.commit(STAGE, unit, lambda store: log_verdicts(
            store, ctx.night, STAGE, verdicts, ctx.clock.now(), ctx.ident))
        return
    vector = (await ctx.embed([reply["beat"]]))[0]
    now = ctx.clock.now()
    rng = ctx.rng("life", guid, tid)
    next_due = now + rng.uniform(2, 5) * DAY

    def write(store):
        latest = store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
        if (latest is None or latest["status"] != "active"
                or any(latest[k] != thread[k] for k in
                       ("guid", "title", "beat_n", "beats_max", "next_beat_at"))
                or _has_beat(store, ctx.night, guid)):
            raise ValueError("Life thread changed while its next beat was generated")
        bid = store.conn.execute(
            "INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
            (tid, ctx.night, reply["beat"], now)).lastrowid
        evidence = (f"beat:{bid}",)
        ctx.writer.put(MemoryRow(
            id=None, owner=guid, kind="event", text=reply["beat"], t_event=int(now),
            t_created=int(now), t_accessed=int(now), access_n=0, actors=(), place=None,
            boss_id=None, raid_id=None, importance=6 if resolved else 5,
            valence=reply["mood"], source="offscreen", evidence=evidence, embedding=vector))
        mood = reply["mood"]
        if abs(mood) >= .2:
            store.conn.execute(
                "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                "VALUES (?,?,?,0,?,?,'life',?,?)",
                (guid, ctx.night, "joy" if mood > 0 else "distress",
                 2 if abs(mood) >= .6 else 1, reply["beat"][:80], json.dumps(evidence), now))
        store.conn.execute(
            "UPDATE life_thread SET beat_n=?,next_beat_at=?,status=?,resolved=? WHERE id=?",
            (count, next_due, "resolved" if resolved else "active", now if resolved else None, tid))
        if resolved:
            persona = load_persona(store, guid)
            title = reply["next_title"] if reply["next_title"] is not None else persona.real_life.life_thread
            ensure_thread(store, guid, title, now, rng, dial=ctx.settings.wow_vs_life)
        wowlife.refresh_on_mind(store, guid, dial=ctx.settings.wow_vs_life)
        log_verdicts(store, ctx.night, STAGE, verdicts, now, ctx.ident)

    ctx.commit(STAGE, unit, write)


async def stage_life(ctx):
    members = set(ctx.members(include_away=True))
    now = ctx.clock.now()
    for guid in sorted(members):
        ensure_thread(ctx.store, guid, ctx.persona(guid).real_life.life_thread, now,
                      ctx.rng("life_thread", guid), dial=ctx.settings.wow_vs_life)
    by_guid = {}
    for thread in due_threads(ctx.store, now):
        if thread["guid"] in members:
            by_guid.setdefault(thread["guid"], thread)
    await ctx.map_units(STAGE, [str(guid) for guid in by_guid],
                        lambda unit: advance(ctx, by_guid[int(unit)]))


def _count(ctx):
    # Include overdue threads: a catch-up run advances one beat, never the entire
    # backlog. Counting only threads born within the window would underbudget it.
    members = set(ctx.members(include_away=True))
    return len({row["guid"] for row in due_threads(ctx.store, ctx.window[1])
                if row["guid"] in members and not ctx.done(STAGE, str(row["guid"]))})


register_counter(STAGE, _count)
