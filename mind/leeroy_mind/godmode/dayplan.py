"""The night makes one schedule for each pair, using only named places and activities."""
from datetime import date, datetime, time, timedelta
import json
from itertools import combinations
from zoneinfo import ZoneInfo

from leeroy_mind.culture import wowcal
from leeroy_mind.nightly.llm import ReflectionError, check_schema, system_prompt
from leeroy_mind.nightly.store import set_run_plan
from leeroy_mind.realm.calendar_time import TIMEZONE
from leeroy_mind.server.state import roster_ties
from leeroy_mind.tuning.edits import load, member_edits

PAIRINGS = {"city": ("city",), "inn": ("inn",), "fishing": ("fish",),
            "gathering": ("herb", "ore"), "camp": ("camp",), "duel_practice": ("duel",),
            "holiday": ("holiday",), "offline": (), "questing": ("camp",)}
HOLIDAYS = {"brewfest_durotar": "brewfest", "org_bonfire": "midsummer"}
SLOTS = tuple(range(8, 26, 2))


def boundary(night, hour):
    day = date.fromisoformat(night) + timedelta(days=1 + hour // 24)
    return datetime.combine(day, time(hour % 24), ZoneInfo(TIMEZONE)).timestamp()


def window(night):
    return boundary(night, 8), boundary(night, 26)


def slots_for(conn, night):
    start, end = window(night)
    # An earlier raid on this calendar day can still be going when the first slot opens.
    cutoff = conn.execute("SELECT min(at) FROM realm_calendar WHERE kind='raid' AND state='posted' "
                          "AND at>=? AND at<?", (boundary(night, 0), end)).fetchone()[0]
    limit = end if cutoff is None else cutoff
    return {f"{h % 24:02}": (a, b) for h in SLOTS
            if (a := boundary(night, h)) < (b := boundary(night, h+2)) <= limit}


def holidays(store, now):
    values = {k: store.get_meta(k) for k in wowcal.META_KEYS}
    active = wowcal.load_active(values.get(wowcal.ACTIVE_KEY))
    names = [e.name.casefold() for e, on in wowcal.from_meta(values, now=now, ahead_days=0)
             if on and e.entry in active]
    return {p for p, key in HOLIDAYS.items() if any(key in name for name in names)}


def choices(places, *, holiday, paired):
    result = {}
    for activity, kinds in PAIRINGS.items():
        if activity == "duel_practice" and not paired:
            continue
        available = [p.name for kind in kinds for p in places.by_kind(kind)]
        if activity == "offline":
            available = ["none"]
        if activity == "holiday":
            available = [p for p in available if holiday and p in holiday]
        if available:
            result[activity] = available
    return result


def schema(options, slots):
    def enum(values):
        return {"type": "string", "enum": list(values)}
    block = {"slot": enum(slots), "activity": enum(options),
             "place": enum(sorted({p for places in options.values() for p in places}))}
    return {"title": "day_plans", "type": "object", "additionalProperties": False, "required": ["blocks"],
            "properties": {"blocks": {"type": "array", "minItems": 2, "maxItems": 4,
                "items": {"type": "object", "additionalProperties": False,
                          "properties": block, "required": list(block)}}}}


def completed_units(ctx):
    return ctx.store.conn.execute("SELECT count(*) FROM night_step WHERE night=? AND stage='day_plans'",
                                  (ctx.night,)).fetchone()[0]


def remaining_schedule(ctx):
    """Choose unfinished, reciprocal cohorts before either reserving or spending model time."""
    conn = ctx.store.conn
    slots = slots_for(conn, ctx.night)
    if len(slots) < 2:
        return slots, [], {}
    planned = {r[0] for r in conn.execute("SELECT DISTINCT member FROM day_plan WHERE night=?", (ctx.night,))}
    finished = {r[0] for r in conn.execute("SELECT unit FROM night_step WHERE night=? AND stage='day_plans'",
                                          (ctx.night,))}
    available = {g for g in ctx.members() if g not in planned and str(g) not in finished}
    if not available:
        return slots, [], {}
    overlays = load(conn, ctx.clock.now())
    edits = {g: member_edits(overlays, g) for g in available}
    graph = roster_ties(conn, available)
    ranked = []
    for a, b in combinations(sorted(available), 2):
        ties = {graph.get((a, b)), graph.get((b, a))}
        if ties & {"rival", "feud"}:
            continue
        positive = bool(ties & {"friend", "crush"})
        preferred = b in edits[a].partners or a in edits[b].partners
        ranked.append((not positive, not preferred, a, b))
    cohorts = []
    for _, _, a, b in sorted(ranked):
        if a in available and b in available:
            cohorts.append((a, b))
            available.difference_update((a, b))
    cohorts.extend((g,) for g in sorted(available))
    return slots, cohorts, edits


def preferences(persona, edits):
    return {"outside_raids": persona.game.outside_raids, "motivation": persona.game.motivation,
            "skill": edits.skill or persona.play.skill, "role": persona.play.raid_role,
            "activities": edits.activities, "rooms": dict(edits.rooms), "channels": dict(edits.channels)}


async def stage_day_plans(ctx, places, *, enabled=lambda: True):
    if not ctx.settings.day_plans or not enabled():
        return
    slots, cohorts, edits = remaining_schedule(ctx)
    limit = ctx.plan["units"].get("day_plans", 0) if ctx.plan else None
    # Older saved plans only recorded completed units. New reservations debit the actual
    # requests (including reasoner's correction) before the model, even across a resume.
    used = ctx.plan.get("day_calls")
    if used is None:
        used = completed_units(ctx) if limit is not None else 0

    def reserve_call():
        nonlocal used
        if not enabled() or limit is not None and used >= limit:
            raise ReflectionError("day_plan_admission_closed", fatal=False)
        if limit is not None:
            used += 1
            ctx.plan["day_calls"] = used
            set_run_plan(ctx.store, ctx.night, ctx.plan)

    if not cohorts:
        return
    active = holidays(ctx.store, ctx.clock.now())
    system = system_prompt("day_plans", "Choose two to four different slots for tomorrow's WoW day. "
        "Choose only listed activity/place pairs. Build one shared schedule for these members.",
        ctx.settings.data_dir, human=ctx.ident)
    for members in cohorts:
        if not enabled() or limit is not None and used >= limit:
            return
        unit = str(members[0])
        options = choices(places, holiday=active, paired=len(members) == 2)
        layout = schema(options, slots)
        question = json.dumps({"members": [preferences(ctx.persona(g), edits[g]) for g in members],
                               "pairings": options, "start_hours": list(slots)})
        try:
            answer = await ctx.reasoner.json(system, question, layout, before_call=reserve_call)
            check_schema(layout, answer)
            blocks = answer["blocks"]
            if len({b["slot"] for b in blocks}) != len(blocks) or any(
                    b["place"] not in options[b["activity"]] for b in blocks):
                raise ValueError("invalid_day_pairing")
        except (ValueError, ReflectionError) as exc:
            if isinstance(exc, ReflectionError) and exc.fatal:
                raise
            ctx.fail("day_plans", unit, ValueError("invalid_day_plan"))
            continue

        if not enabled():
            return

        def write(store):
            current = slots_for(store.conn, ctx.night)
            if any(b["slot"] not in current for b in blocks):
                return
            for b in blocks:
                for member in members:
                    companions = [g for g in members if g != member] if b["activity"] != "offline" else []
                    store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,"
                        "partners,status) VALUES(?,?,?,?,?,?,?,'planned')", (ctx.night, member,
                        *current[b["slot"]], b["activity"], None if b["place"] == "none" else b["place"],
                        json.dumps(companions)))
        ctx.commit("day_plans", unit, write)


def attach(runner, settings, places):
    """The nightly reload owns registration. Previously captured callbacks also see the switch."""
    stage = None

    def configure(on):
        nonlocal stage
        if on == (stage is not None):
            return
        if not on:
            runner.stages = tuple(s for s in runner.stages if s.fn is not stage)
            stage = None
            return

        def current(ctx):
            return stage is run and ctx.settings.day_plans

        async def run(ctx):
            if current(ctx):
                await stage_day_plans(ctx, places(), enabled=lambda: current(ctx))

        stage = run
        runner.add_stage("day_plans", run, after="life", model="reflection", wants_gpu=current)

    configure(settings.day_plans)
    return configure
