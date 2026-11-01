"""The slow WoW-vs-life loop: hours, pressure and talk, never a real absence."""
import json
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal

from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
from leeroy_mind.nightly.ground import Claim, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.motives import motive_weights, sample_traits, step_motives
from leeroy_mind.nightly.settings import iso_week, night_bounds
from leeroy_mind.nightly.store import active_away
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.talk.prompt import is_close
from leeroy_mind.text import strip_wow_markup

STAGE = "wowlife"
TIES_RE = re.compile(
    r"\b(girlfriend|boyfriend|wife|husband|partner|fianc[eé]e?|kids?|baby|son|daughter|"
    r"exams?|finals|thesis|class(es)?|school|uni|college|shifts?|job|boss|deadline)\b", re.I)
VOW_CHANCE = 0.15
ESCAPE_HOURS = 3.0
LINE_SCHEMA = schema("wow_line", {
    "line": {"type": "string", "maxLength": 100},
    "mood": {"type": "number", "minimum": -1, "maximum": 1},
})
LINE_PROMPT = ("Write what {name} would say about how much World of Warcraft they have "
               "been playing lately.\n{card}")
LINE_ASK = ("One short line in their own words, the way they'd mention it in guild chat: "
            "specific (hours, who is annoyed, what they skipped), not generic.")


@dataclass(frozen=True)
class WowLife:
    guid: int
    pressure: float = 0.0
    guilt: float = 0.0
    vow: bool = False
    line: str = ""
    line_night: str | None = None
    updated: float = 0.0


def get_wow_life(store, guid: int) -> WowLife:
    with store.transaction():
        row = store.conn.execute("SELECT * FROM wow_life WHERE guid=?", (guid,)).fetchone()
    return WowLife(**{**dict(row), "vow": bool(row["vow"])}) if row else WowLife(guid)


def put_wow_life(store, row: WowLife) -> None:
    with store.transaction():
        store.conn.execute(
            "INSERT INTO wow_life(guid,pressure,guilt,vow,line,line_night,updated) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(guid) DO UPDATE SET pressure=excluded.pressure,guilt=excluded.guilt,"
            "vow=excluded.vow,line=excluded.line,line_night=excluded.line_night,updated=excluded.updated",
            (row.guid, row.pressure, row.guilt, int(row.vow), row.line, row.line_night, row.updated))


def human_hours(store, bounds) -> tuple[float, list[int]]:
    """Each overlapping session clipped to [start, end); open sessions stop at end."""
    start, end = bounds
    if end <= start:
        return 0.0, []
    seconds, ids = 0.0, []
    with store.transaction():
        rows = list(store.conn.execute(
            "SELECT id,started,ended FROM session WHERE started < ? AND "
            "(ended IS NULL OR ended > ?) ORDER BY started,id", (end, start)))
    for row in rows:
        duration = min(end, row["ended"] if row["ended"] is not None else end) - max(start, row["started"])
        if duration > 0:
            seconds += duration
            ids.append(row["id"])
    return seconds / 3600, ids


def member_hours(p, *, human_hours, away, rng, escapism=0.0, pressure=0.0) -> float:
    own = 0.0 if away else (rng.uniform(1, 4) + 1.5 * p.real_life.night_owl
                           + ESCAPE_HOURS * escapism * pressure)
    return min(16.0, human_hours + own)


def _clamp(value):
    return max(0.0, min(1.0, value))


def next_state(pressure, guilt, *, hours_week, ties, conscientious, dial,
               escapism=0.0) -> tuple[float, float]:
    strain = _clamp((hours_week - 20) / 40) * (1 + 0.5 * escapism)
    return (_clamp(0.8 * pressure + 0.2 * strain * ties * dial / 5),
            _clamp(0.85 * guilt + 0.15 * strain * (1.0 if conscientious else 0.6) * dial / 5))


def band(pressure, guilt) -> Literal["none", "some", "high"]:
    level = max(pressure, guilt)
    return "high" if level >= 0.6 else "some" if level >= 0.3 else "none"


def on_mind(title, line, band) -> str:
    parts = (line, title) if band == "high" else (title, line) if band == "some" else (title,)
    return "; ".join(p.strip() for p in parts if p and p.strip())[:200]


def _title(store, guid):
    row = store.conn.execute(
        "SELECT title FROM life_thread WHERE guid=? AND status='active' ORDER BY id DESC LIMIT 1",
        (guid,)).fetchone()
    if row is not None:
        return row["title"]
    p = load_persona(store, guid)
    return p.real_life.life_thread if p is not None else ""


def refresh_on_mind(store, guid, *, dial):
    """Join the caller's transaction; preserve every other current persona-state field."""
    wow = get_wow_life(store, guid)
    text = on_mind(_title(store, guid), wow.line if dial else "", band(wow.pressure, wow.guilt))
    store.put_state(replace(store.get_state(guid), life_thread=text, updated=store.clock.now()))


def _week_start(night):
    return (date.fromisoformat(night) - timedelta(days=6)).isoformat()


def _hours(store, night, guid):
    row = store.conn.execute("SELECT hours FROM wow_hours WHERE night=? AND guid=?", (night, guid)).fetchone()
    return float(row["hours"]) if row is not None else 0.0


def _week_hours(store, night, guid):
    return float(store.conn.execute(
        "SELECT COALESCE(SUM(hours),0) FROM wow_hours WHERE guid=? AND night>=? AND night<=?",
        (guid, _week_start(night), night)).fetchone()[0])


def _step_state(ctx):
    if ctx.done(STAGE, "state"):
        return True

    def write(store):
        members = ctx.members(include_away=True)
        dial, now = ctx.settings.wow_vs_life, ctx.clock.now()
        hours, _ = human_hours(store, night_bounds(ctx.night, ctx.tz))
        away = active_away(store)
        weights = {g: motive_weights(ctx.persona(g))["escapism"] for g in members}
        rows = [(ctx.night, ctx.human, hours)]
        rows.extend((ctx.night, g, member_hours(ctx.persona(g), human_hours=hours,
            away=g in away, rng=ctx.rng("hours", g), escapism=weights[g],
            pressure=get_wow_life(store, g).pressure)) for g in members)
        store.conn.executemany(
            "INSERT INTO wow_hours(night,guid,hours) VALUES (?,?,?) "
            "ON CONFLICT(night,guid) DO UPDATE SET hours=excluded.hours", rows)
        for guid in members:
            p, before = ctx.persona(guid), get_wow_life(store, guid)
            ties = 1.0 if TIES_RE.search(" ".join((p.real_life.household, p.real_life.job,
                                                  _title(store, guid)))) else 0.5
            pressure, guilt = next_state(before.pressure, before.guilt,
                hours_week=_week_hours(store, ctx.night, guid), ties=ties,
                conscientious=p.big_five.conscientiousness.score >= 6, dial=dial,
                escapism=weights[guid])
            level, vow = band(pressure, guilt), before.vow
            if level == "none" or dial == 0:
                vow = False
            elif level == "high" and ctx.rng("vow", guid).random() < VOW_CHANCE * dial / 5:
                vow = True
            put_wow_life(store, replace(before, pressure=pressure, guilt=guilt, vow=vow, updated=now))
            refresh_on_mind(store, guid, dial=dial)
    return ctx.commit(STAGE, "state", write)


async def _line(ctx, guid, facts):
    unit = f"line:{guid}"
    if ctx.done(STAGE, unit):
        return
    wow = get_wow_life(ctx.store, guid)
    hn, hw = _hours(ctx.store, ctx.night, guid), _week_hours(ctx.store, ctx.night, guid)
    ref = f"rule:wowlife:{ctx.night}:{guid}"
    ev = Evidence()
    ev.add(Item(ref, f"{hw:.0f} hours this week", frozenset({guid}), ctx.clock.now(),
                knowers=frozenset({guid})))
    system = system_prompt(STAGE, LINE_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=ctx.name(guid), card=ctx.card(guid))
    user = (f"Hours last night: {hn:.1f}. Hours this week: {hw:.1f}.\n"
            f"Band: {band(wow.pressure, wow.guilt)}.\nLife thread: {_title(ctx.store, guid)}\n")
    if wow.vow:
        user += "They keep saying they'll quit after the Lich King. They won't.\n"
    reply = await ctx.reasoner.json(system, user + LINE_ASK, LINE_SCHEMA, temperature=0.8)
    claim = Claim("wowlife", guid, " ".join(strip_wow_markup(reply["line"]).split()), [ref],
                  valence=float(reply["mood"]))
    pairs = await ground(ctx, [claim], ev, facts)

    def write(store):
        now = ctx.clock.now()
        log_verdicts(store, ctx.night, STAGE, pairs, now, ctx.ident)
        if not pairs[0][1].ok:
            return
        current = get_wow_life(store, guid)
        put_wow_life(store, replace(current, line=claim.text, line_night=ctx.night, updated=now))
        if claim.valence <= -0.3:
            store.conn.execute(
                "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                "VALUES (?,?,'distress',0,1,?,'wowlife',?,?)",
                (guid, ctx.night, claim.text[:80], json.dumps([ref]), now))
        refresh_on_mind(store, guid, dial=ctx.settings.wow_vs_life)
    ctx.commit(STAGE, unit, write)


async def _notice_hours(ctx):
    if ctx.settings.wow_vs_life == 0 or ctx.done(STAGE, "hours"):
        return
    hn, hw = _hours(ctx.store, ctx.night, ctx.human), _week_hours(ctx.store, ctx.night, ctx.human)
    writers = [g for g in ctx.members(include_away=True)
               if is_close(ctx.store.get_relationship(g, ctx.human))]
    writers.sort(key=lambda g: (-ctx.store.get_relationship(g, ctx.human).affinity, g))
    writers = writers[:ctx.settings.notice_members]
    bounds = night_bounds(ctx.night, ctx.tz)
    _, nightly = human_hours(ctx.store, bounds)
    _, weekly = human_hours(ctx.store, (night_bounds(_week_start(ctx.night), ctx.tz)[0], bounds[1]))
    notices = []
    if hn >= ctx.settings.notice_night_hours and nightly:
        notices.append((f"{ctx.ident.called} was on for {round(hn)} hours last night.",
                        f"hours:{ctx.night}", nightly[:10]))
    if hw >= ctx.settings.notice_week_hours and weekly:
        notices.append((f"{ctx.ident.called} has played {round(hw)} hours this week.",
                        f"hours:{iso_week(ctx.night)}", weekly[:10]))
    vectors = await ctx.embed([text for text, _, _ in notices]) if notices and writers else []
    if notices and writers and len(vectors) != len(notices):
        raise ValueError("Hours observations received an incomplete embedding batch")

    def write(store):
        now = int(ctx.clock.now())
        for guid in writers:
            for (text, key, ids), vector in zip(notices, vectors):
                refs = tuple(f"session:{sid}" for sid in ids)
                ctx.writer.put(MemoryRow(id=None, owner=guid, kind="observation", text=text,
                    t_event=now, t_created=now, t_accessed=now, access_n=0,
                    actors=(ctx.human,), place=None, boss_id=None, raid_id=None,
                    importance=3, valence=0.0, source="game", evidence=refs, key=key,
                    embedding=vector), shown=refs)
    ctx.commit(STAGE, "hours", write)


async def stage_wowlife(ctx):
    if not _step_state(ctx):
        return
    if not ctx.done(STAGE, "traits"):
        def traits(store):
            for guid in ctx.members(include_away=True):
                sample_traits(ctx, guid)
        if not ctx.commit(STAGE, "traits", traits):
            return
    step_motives(ctx)
    if not ctx.done(STAGE, "motives"):
        return
    dial = ctx.settings.wow_vs_life
    if dial > 0:
        # A resumed night may see edited pressure or a different ranking. Completed
        # line units still spend this night's cap instead of admitting another batch.
        finished = {row["unit"] for row in ctx.store.conn.execute(
            "SELECT unit FROM night_step WHERE night=? AND stage=? AND unit LIKE 'line:%'",
            (ctx.night, STAGE))}
        remaining = max(0, round(ctx.settings.wow_lines_per_night * dial / 10) - len(finished))
        members = [(g, get_wow_life(ctx.store, g)) for g in ctx.members(include_away=True)]
        eligible = sorted(((g, row) for g, row in members
                           if f"line:{g}" not in finished and band(row.pressure, row.guilt) != "none"),
                          key=lambda pair: (-max(pair[1].pressure, pair[1].guilt), pair[0]))
        selected = [g for g, _ in eligible[:remaining]]
        if selected:
            facts = load_facts(ctx)
            await ctx.map_units(STAGE, [f"line:{g}" for g in selected],
                                lambda unit: _line(ctx, int(unit.split(":")[1]), facts))
    await _notice_hours(ctx)
