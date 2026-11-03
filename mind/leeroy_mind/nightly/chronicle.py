"""Public raid recaps and a weekly history, grounded in complete source provenance."""
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime

from leeroy_mind.nightly.awards import AWARDS, stage_awards
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, check, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.reactions import laughs_at
from leeroy_mind.nightly.settings import is_sunday, iso_week, night_bounds, week_bounds
from leeroy_mind.nightly.store import insert_story
from leeroy_mind.nightly.stories import public_snapshot
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.text import strip_wow_markup, truncate_words
from leeroy_mind.wow import RAID_INSTANCES, is_raid_fight

STAGE = "stories"
ERA_RAID = {631: "Icecrown", 649: "Crusader", 603: "Ulduar", 533: "Naxxramas", 615: "Obsidian", 616: "Malygos", 724: "Ruby", 249: "Onyxia"}
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
RECAP_SCHEMA = schema("recap", {"title": {"type": "string", "maxLength": 60},
    "recap": {"type": "string", "maxLength": 400},
    "evidence": {"type": "array", "minItems": 1, "items": {"type": "integer"}}})
CHRONICLE_SCHEMA = schema("chronicle", {"entry": {"type": "string", "maxLength": 400},
    "evidence": {"type": "array", "minItems": 1, "items": {"type": "integer"}}})
RECAP_PROMPT = ("Write the guild's short recap of tonight's raid, the way someone posts it on "
    "the guild forum the next morning: what we killed, what wiped us, who was the MVP, the funniest moment.")
CHRONICLE_PROMPT = ("You are {name}, the guild's historian. Write this week's entry in the guild "
    "chronicle, in the era you call {era}: what happened and why it mattered, in your own voice.\n{card}")


def _text(ctx, text):
    return redact_real_name(" ".join(strip_wow_markup(text).split()), ctx.ident, ctx.ident.called)


def _bosses(store, window):
    rows = store.conn.execute("SELECT id,ts,type,payload FROM game_event "
                              "WHERE type='boss' AND ts>=? AND ts<? ORDER BY ts,id", window).fetchall()
    return [(dict(row), payload) for row in rows if
            is_raid_fight((payload := json.loads(row["payload"])).get("map", 0), payload.get("boss_id", -1))]


def chronicler(ctx):
    """An available historian by title, otherwise the lowest-guid lore-minded member."""
    people = [(guid, load_persona(ctx.store, guid)) for guid in sorted(ctx.members())]
    historians = [guid for guid, p in people if p is not None and "historian" in p.play.officer_title.casefold()]
    if historians:
        return historians[0]
    return next((guid for guid, p in people if p is not None and p.game.thinks_as == "lore"), None)


def era_name(store, end, tz):
    with store.transaction():
        raids = _bosses(store, (end - 28 * 86400, end))
        counts = Counter(p["map"] for row, p in raids
                         if public_snapshot(store, [f"event:{row['id']}"]) is not None)
    season = SEASONS[datetime.fromtimestamp(end, tz).month]
    if not counts:
        return f"the quiet {season}"
    map_id = min(counts, key=lambda key: (-counts[key], key))
    return f"the {ERA_RAID.get(map_id, RAID_INSTANCES.get(map_id, 'raid'))} {season}"


def _memory(owner, text, *, key, refs, actors, importance, now, embedding):
    return MemoryRow(id=None, owner=owner, kind="story", text=text, t_event=int(now),
        t_created=int(now), t_accessed=int(now), access_n=0, actors=tuple(actors)[:8],
        place=None, boss_id=None, raid_id=None, importance=importance, valence=0,
        source="reflection", evidence=tuple(refs), embedding=embedding, key=key)


def _recap_sources(ctx):
    """A complete prompt snapshot, rebuilt at commit including laughter eligibility."""
    store = ctx.store
    raids = [(row, p) for row, p in _bosses(store, ctx.window)
             if public_snapshot(store, [f"event:{row['id']}"]) is not None]
    if not raids:
        return None
    refs = [f"event:{row['id']}" for row, _ in raids]
    mvp = store.keyed_memory(0, f"mvp:{ctx.night}")
    if mvp is not None and mvp.status == "active" and public_snapshot(store, [f"memory:{mvp.id}"]) is not None:
        refs.append(f"memory:{mvp.id}")
    lines = store.conn.execute("SELECT id FROM chat_line WHERE channel IN ('raid','guild') "
                              "AND ts>=? AND ts<? ORDER BY ts,id", ctx.window).fetchall()
    laughed = []
    for line in lines:
        ref = f"chat:{line['id']}"
        if laughs_at(store, line["id"]) and public_snapshot(store, [ref]) is not None:
            laughed.append(ref)
            if len(laughed) == 12:
                break
    refs += laughed
    snapshot = public_snapshot(store, refs)
    if snapshot is None:
        return None
    members = set(ctx.members(include_away=True))
    participants = sorted({g for _, p in raids for g in p.get("members", ())} & members)
    counts = Counter((p["map"], p["boss_id"], p.get("state", "")) for _, p in raids)
    payloads = {f"event:{row['id']}": p for row, p in raids}
    items = []
    for item in load_items(store, refs, ident=ctx.ident):
        if item.ref in payloads:
            p = payloads[item.ref]
            pair = p["map"], p["boss_id"]
            # render_event names the individual fight; these derived totals let the usual
            # numerical grounding rule check a truthful recap of the night's attempts.
            item = replace(item, text=item.text + f". Tonight: {counts[(*pair, 'wiped')]} wipes, "
                           f"{counts[(*pair, 'killed')]} kills.")
        items.append(item)
    return {"refs": tuple(refs), "snapshot": snapshot, "participants": tuple(participants), "items": tuple(items)}


def _evidence(items):
    evidence = Evidence()
    for item in items:
        evidence.add(item)
    return evidence


async def write_recap(ctx):
    unit = f"recap:{ctx.night}"
    if ctx.done(STAGE, unit):
        return
    with ctx.store.transaction():
        sources = _recap_sources(ctx)
    if sources is None:
        ctx.skip(STAGE, unit)
        return
    ev = _evidence(sources["items"])
    system = system_prompt("recap", RECAP_PROMPT, ctx.settings.data_dir, human=ctx.ident)
    user = f"Raid night {ctx.night}. Cite the bracketed evidence aliases.\nEvidence:\n{ev.render(ctx.tz)}"
    notes = await ctx.reasoner.think(system, user)
    reply = await ctx.reasoner.json(system, user, RECAP_SCHEMA, notes=notes, temperature=0.8)
    refs = list(dict.fromkeys(ev.ref(alias) for alias in reply["evidence"]))
    title, recap = _text(ctx, reply["title"]), _text(ctx, reply["recap"])
    facts = load_facts(ctx)
    claim = Claim("recap", 0, recap, refs)
    pairs = await ground(ctx, [claim], ev, facts)
    title_claim = Claim("recap", 0, title, refs)
    pairs.append((title_claim, check(title_claim, ev, facts)))
    accepted = all(v.ok for _, v in pairs)
    text = truncate_words(f"{title}: {recap}", 200)
    vectors = await ctx.embed([text]) if accepted else []
    if accepted and len(vectors) != 1:
        raise ValueError("Recap received an incomplete embedding batch")

    def write(store):
        if _recap_sources(ctx) != sources:
            raise ValueError("Recap sources changed while its text was generated")
        now = ctx.clock.now()
        log_verdicts(store, ctx.night, STAGE, pairs, now, ctx.ident)
        if not accepted:
            return
        # Preserve every shown source in the durable story, even if the model cited a subset.
        shown = sources["refs"]
        ident = insert_story(store, title=title, summary=recap, participants=sources["participants"],
                             evidence=shown, ts=now)
        store.set_meta(f"nightly:story-night:{ident}", ctx.night)
        story_ref = f"story:{ident}"
        if public_snapshot(store, [story_ref]) is None:
            raise ValueError("Recap provenance is not public")
        ctx.writer.put(_memory(0, text, key=unit, refs=(story_ref, *shown)[:20],
            actors=sources["participants"], importance=7, now=now, embedding=vectors[0]), shown=shown)
        for owner in sources["participants"]:
            ctx.writer.put(_memory(owner, text, key=unit, refs=(story_ref,),
                actors=[g for g in sources["participants"] if g != owner], importance=6,
                now=now, embedding=vectors[0]), shown=shown)
    ctx.commit(STAGE, unit, write)


def _chronicle_sources(ctx):
    store, week = ctx.store, iso_week(ctx.night)
    author = chronicler(ctx)
    if author is None:
        return None
    start, end = week_bounds(week, ctx.tz)
    first_night = datetime.fromtimestamp(start, ctx.tz).date().isoformat()
    next_week = datetime.fromtimestamp(end, ctx.tz).date().isoformat()
    stories = store.conn.execute("SELECT s.*,m.value AS origin FROM story s LEFT JOIN meta m "
        "ON m.key='nightly:story-night:' || s.id WHERE (m.value>=? AND m.value<?) "
        "OR (m.value IS NULL AND s.t_created>=? AND s.t_created<?) ORDER BY s.t_created,s.id",
        (first_night, next_week, start, end)).fetchall()
    refs, items, participants, origins = [], [], set(), []
    for story in stories:
        ref = f"story:{story['id']}"
        provenance = public_snapshot(store, [ref])
        if provenance is None:
            continue
        people = set(json.loads(story["participants"]))
        actors = people | {guid for item in load_items(store, [r for r, _ in provenance], ident=ctx.ident)
                           for guid in item.actors}
        refs.append(ref)
        origins.append((story["id"], story["origin"]))
        participants.update(people)
        items.append(Item(ref, _text(ctx, story["title"] + ": " + story["summary"]),
                          frozenset(actors), night_bounds(story["origin"], ctx.tz)[0]
                          if story["origin"] is not None else story["t_created"]))
    ids = [r[0] for r in store.conn.execute("SELECT id FROM memory WHERE owner=0 AND status='active' "
        "AND key LIKE 'boss:%' AND t_event>=? AND t_event<? ORDER BY t_event,id", (start, end))]
    boss_refs = [f"memory:{ident}" for ident in ids
                 if public_snapshot(store, [f"memory:{ident}"]) is not None]
    refs += boss_refs
    items += load_items(store, boss_refs, ident=ctx.ident)
    if not refs:
        return None
    snapshot = public_snapshot(store, refs)
    if snapshot is None:
        return None
    members = set(ctx.members(include_away=True))
    return {"refs": tuple(refs), "items": tuple(items), "snapshot": snapshot, "author": author,
            "participants": tuple(sorted((participants & members) | {author})),
            "era": era_name(store, end, ctx.tz), "card": ctx.card(author),
            "persona": load_persona(store, author), "origins": tuple(origins)}


async def write_chronicle(ctx):
    if not is_sunday(ctx.night):
        return
    week, unit = iso_week(ctx.night), f"chronicle:{iso_week(ctx.night)}"
    if ctx.done(STAGE, unit):
        return
    with ctx.store.transaction():
        existing = ctx.store.conn.execute("SELECT 1 FROM chronicle WHERE week=?", (week,)).fetchone()
        sources = None if existing else _chronicle_sources(ctx)
    if sources is None:
        ctx.skip(STAGE, unit)
        return
    ev = _evidence(sources["items"])
    system = system_prompt("chronicle", CHRONICLE_PROMPT, ctx.settings.data_dir, human=ctx.ident,
        name=ctx.name(sources["author"]), era=sources["era"], card=sources["card"])
    user = f"Week {week}. Cite the bracketed evidence aliases.\nEvidence:\n{ev.render(ctx.tz)}"
    reply = await ctx.reasoner.json(system, user, CHRONICLE_SCHEMA, temperature=0.8)
    refs = list(dict.fromkeys(ev.ref(alias) for alias in reply["evidence"]))
    entry = _text(ctx, reply["entry"])
    pairs = await ground(ctx, [Claim("chronicle", sources["author"], entry, refs)], ev, load_facts(ctx))
    accepted = all(v.ok for _, v in pairs)
    text = truncate_words(f"{sources['era']}: {entry}", 200)
    vectors = await ctx.embed([text]) if accepted else []
    if accepted and len(vectors) != 1:
        raise ValueError("Chronicle received an incomplete embedding batch")

    def write(store):
        if _chronicle_sources(ctx) != sources:
            raise ValueError("Chronicle sources changed while its text was generated")
        if store.conn.execute("SELECT 1 FROM chronicle WHERE week=?", (week,)).fetchone():
            raise ValueError("Chronicle was already written while its entry was generated")
        now = ctx.clock.now()
        log_verdicts(store, ctx.night, STAGE, pairs, now, ctx.ident)
        if not accepted:
            return
        ident = store.conn.execute("INSERT INTO chronicle(week,era,author,text,evidence,created) "
            "VALUES (?,?,?,?,?,?)", (week, sources["era"], sources["author"], entry,
                                     json.dumps(sources["refs"]), now)).lastrowid
        ref = f"chronicle:{ident}"
        if public_snapshot(store, [ref]) is None:
            raise ValueError("Chronicle provenance is not public")
        for owner in (0, *sources["participants"]):
            ctx.writer.put(_memory(owner, text, key=unit, refs=(ref,),
                actors=[g for g in sources["participants"] if g != owner], importance=7 if owner == 0 else 6,
                now=now, embedding=vectors[0]), shown=sources["refs"])
    ctx.commit(STAGE, unit, write)
