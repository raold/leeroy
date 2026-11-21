"""The night makes one schedule for each pair, using only named places and activities."""
from datetime import date, datetime, time, timedelta
import json
from zoneinfo import ZoneInfo

from leeroy_mind.culture import wowcal
from leeroy_mind.nightly.llm import check_schema, system_prompt
from leeroy_mind.realm.calendar_time import TIMEZONE
from leeroy_mind.server.state import tie_kind
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
    return {"type": "object", "additionalProperties": False, "required": ["blocks"],
            "properties": {"blocks": {"type": "array", "minItems": 2, "maxItems": 4,
                "items": {"type": "object", "additionalProperties": False,
                          "properties": block, "required": list(block)}}}}


def partner(conn, guid, candidates, preferred=()):
    ranked = []
    for other in candidates:
        ties = {tie_kind(conn, guid, other), tie_kind(conn, other, guid)}
        if not ties & {"rival", "feud"}:
            ranked.append((not bool(ties & {"friend", "crush"}), other not in preferred, other))
    return min(ranked)[2] if ranked else None


def preferences(persona, edits):
    return {"outside_raids": persona.game.outside_raids, "motivation": persona.game.motivation,
            "skill": edits.skill or persona.play.skill, "role": persona.play.raid_role,
            "activities": edits.activities, "rooms": dict(edits.rooms), "channels": dict(edits.channels)}


async def stage_day_plans(ctx, places):
    conn = ctx.store.conn
    available = set(ctx.members()) - {r[0] for r in conn.execute(
        "SELECT DISTINCT member FROM day_plan WHERE night=?", (ctx.night,))}
    slots = slots_for(conn, ctx.night)
    if len(slots) < 2:
        return
    active = holidays(ctx.store, ctx.clock.now())
    overlays = load(conn, ctx.clock.now())
    system = system_prompt("day_plans", "Choose two to four different slots for tomorrow's WoW day. "
        "Choose only listed activity/place pairs. Build one shared schedule for these members.",
        ctx.settings.data_dir, human=ctx.ident)
    while available:
        guid = min(available)
        available.remove(guid)
        edits = member_edits(overlays, guid)
        other = partner(conn, guid, available, edits.partners)
        members = [guid]
        if other is not None:
            available.remove(other)
            members.append(other)
        unit = str(guid)
        if ctx.done("day_plans", unit):
            continue
        options = choices(places, holiday=active, paired=other is not None)
        layout = schema(options, slots)
        question = json.dumps({"members": [preferences(ctx.persona(g), member_edits(overlays, g))
                               for g in members], "pairings": options, "start_hours": list(slots)})
        try:
            answer = await ctx.reasoner.json(system, question, layout)
            check_schema(layout, answer)
            blocks = answer["blocks"]
            if len({b["slot"] for b in blocks}) != len(blocks) or any(
                    b["place"] not in options[b["activity"]] for b in blocks):
                raise ValueError("invalid_day_pairing")
        except (ValueError, RuntimeError) as exc:
            ctx.fail("day_plans", unit, exc)
            continue

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
    if settings.day_plans:
        async def stage(ctx):
            await stage_day_plans(ctx, places())
        runner.add_stage("day_plans", stage, after="life", model="reflection")
