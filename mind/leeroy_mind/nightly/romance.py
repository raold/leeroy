"""Relationship stages and observed rivalry, advanced once per nightly unit.

Stages describe the story, never restrict live dialogue. Jealousy reaches dialogue
through memories whose privacy is derived from the observations they cite.
"""
import json
import re
from dataclasses import dataclass, replace
from datetime import date

from leeroy_mind.nightly.attraction import RUSH_S, aimed_at, reflection_row
from leeroy_mind.nightly.audience import audience_of
from leeroy_mind.nightly.confide import depth_with
from leeroy_mind.nightly.knowledge import could_have_seen, roots
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import ROMANCE_STAGES

LADDER = ROMANCE_STAGES[:7]
DAY_S = 86400.0


@dataclass(frozen=True)
class Gate:
    attraction: int
    affinity: int
    trust: int
    min_days: float
    depth: int


GATES = {1: Gate(20, 10, 0, 1, 0), 2: Gate(35, 20, 10, 4, 0),
         3: Gate(50, 35, 30, 7, 1), 4: Gate(60, 45, 45, 10, 2),
         5: Gate(70, 55, 55, 14, 2), 6: Gate(80, 65, 65, 21, 3)}
PACE_ATTITUDE = {"lust": .5, "drawn": 1.0, "fond": 2.0}
PACE_STYLE = {"bold": .8, "theatrical": .8, "earnest": 1., "competitive": 1.,
              "shy": 1.5, "guarded": 2.0}
MODE_MULT = {"good_sport": .5, "sulker": 1.2, "grumbler": 1., "teaser": .8,
             "play_by_play": 1.}
DECAY = .85
_RIVALRY = re.compile(r"\(rivalry, not hurt: chat:(\d+)\)")
_BAND = re.compile(r"(fond|drawn|lust) -> (fond|drawn|lust):")
_CHANGES = "romance_changes:"


def can_advance(stage: int, rel, *, attitude, style, days_since_change,
                new_evidence, rush=False, depth=0) -> bool:
    gate = GATES.get(stage + 1)
    if stage < 0 or gate is None or attitude not in PACE_ATTITUDE or not new_evidence:
        return False
    wait = gate.min_days * PACE_ATTITUDE[attitude] * PACE_STYLE.get(style, 1.)
    return (rel.attraction >= gate.attraction and rel.affinity >= gate.affinity
            and rel.trust >= gate.trust and depth >= gate.depth
            and days_since_change >= wait * (.5 if rush else 1.))


async def stage_romance(ctx) -> None:
    if ctx.done("romance", "all"):
        return
    now = ctx.clock.now()

    def write(store):
        changes = []
        for guid, stance in ctx.attitudes(fresh=True).items():
            rel = store.get_relationship(guid, ctx.human)
            if rel.romance_stage not in LADDER:
                continue  # includes the owner's ex state
            last = store.conn.execute(
                "SELECT coalesce(max(CASE WHEN dim='romance_stage' THEN ts END), min(ts), 0) "
                "FROM rel_delta WHERE a=? AND b=? AND ts<=?", (guid, ctx.human, now)).fetchone()[0]
            evidence = store.conn.execute(
                "SELECT m.id FROM memory m JOIN memory_actor a ON a.memory_id=m.id "
                "WHERE m.owner=? AND a.guid=? AND m.status='active' AND m.valence>0 "
                "AND m.t_event>? AND m.t_event<=? ORDER BY m.t_event DESC,m.id DESC LIMIT 1",
                (guid, ctx.human, last, now)).fetchone()
            rush = store.conn.execute("SELECT rush_until FROM attraction_state WHERE guid=?", (guid,)).fetchone()
            rushing = bool(rush and ctx.window[0] <= rush[0] - RUSH_S < ctx.window[1])
            stage = LADDER.index(rel.romance_stage)
            if not can_advance(stage, rel, attitude=stance, style=ctx.persona(guid).romance.style,
                               days_since_change=(now - last) / DAY_S,
                               new_evidence=evidence is not None, rush=rushing,
                               depth=depth_with(store, guid, ctx.human, now=now)):
                continue
            new = LADDER[stage + 1]
            store.put_relationship(replace(rel, romance_stage=new))
            delta = store.add_rel_delta(guid, ctx.human, "romance_stage", 1,
                                f"{rel.romance_stage}->{new}", [f"memory:{evidence[0]}"],
                                now, applied=True)
            changes.append(delta)
        # The run normally happens AFTER its closed observation window. Keep its
        # exact delta ids with the unit, including across a restart between stages.
        store.set_meta(f"{_CHANGES}{ctx.night}", json.dumps(changes))
    ctx.commit("romance", "all", write)


def jealousy_next(prev, *, nights, obs, spike, knob, mult=1.0) -> float:
    return 0.0 if knob == 0 else max(0., min(100.,
        prev * DECAY ** nights + knob / 10 * mult * (8 * min(5, obs) + 30 * spike)))


def _chat(store, ref):
    if not ref.startswith("chat:") or not ref[5:].isdigit():
        return None
    return store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()


def _could_know(store, holder, ref, row, end):
    if row["channel"] != "whisper" and holder in audience_of(store, row["id"]):
        return True
    return store.conn.execute("SELECT 1 FROM knows WHERE root=? AND guid=? AND learned<?",
                               (ref, holder, end)).fetchone() is not None


def known_acts(store, holder, target, window) -> list[str]:
    """Distinct observed acts, including a recorded extraction nudge about the rival."""
    with store.transaction():
        human = int(store.get_meta("human_guid") or 0)
        refs = []
        for act in store.conn.execute(
                "SELECT ref FROM attraction_act WHERE guid=? AND act IN ('flirt','public_compliment') "
                "AND ts>=? AND ts<? ORDER BY ts,id", (target, *window)):
            row = _chat(store, act["ref"])
            if (row is not None and row["from_guid"] == human and row["origin"] == "human"
                    and _could_know(store, holder, act["ref"], row, window[1])):
                refs.append(act["ref"])
        p = load_persona(store, target)
        if p is not None:
            for delta in store.conn.execute(
                    "SELECT reason FROM rel_delta WHERE a=? AND b=? AND dim='attraction' "
                    "AND applied=1 AND reason LIKE '%(rivalry, not hurt: chat:%' ORDER BY ts,id",
                    (holder, human)):
                for match in _RIVALRY.finditer(delta["reason"]):
                    ref = f"chat:{match[1]}"
                    row = _chat(store, ref)
                    if (row is not None and row["from_guid"] == human and row["origin"] == "human"
                            and window[0] <= row["ts"] < window[1]
                            and aimed_at(row, p, target)
                            and _could_know(store, holder, ref, row, window[1])):
                        refs.append(ref)
        return list(dict.fromkeys(refs))


def jealousy_level(store, holder, target) -> float:
    with store.transaction():
        row = store.conn.execute("SELECT level FROM jealousy WHERE holder=? AND target=?",
                                 (holder, target)).fetchone()
        return float(row[0]) if row else 0.


def jealousy_rows(store, min_level=0.0) -> list[tuple[int, int, str, float]]:
    with store.transaction():
        return [(r["holder"], r["target"], r["kind"], float(r["level"])) for r in store.conn.execute(
            "SELECT * FROM jealousy WHERE level>=? ORDER BY holder,target", (min_level,))]


def _observations(store, holder, window):
    ids = [r[0] for r in store.conn.execute(
        "SELECT id FROM memory WHERE owner=? AND status='active' AND t_event>=? AND t_event<? "
        "ORDER BY t_event,id", (holder, *window))]
    return store.get_memories(ids)


def _invitation_refs(store, guid, human, window, current) -> list[str]:
    return [ref for r in store.conn.execute(
        "SELECT reason,evidence FROM rel_delta d WHERE a=? AND b=? AND dim='romance_stage' "
        "AND applied=1 AND delta>0 AND (id IN (SELECT value FROM json_each(?)) OR "
        "(ts>=? AND ts<? AND NOT EXISTS (SELECT 1 FROM meta m, "
        "json_each(CASE WHEN m.key LIKE ? THEN m.value ELSE '[]' END) j WHERE j.value=d.id)))",
        (guid, human, json.dumps(current), *window, f"{_CHANGES}%"))
        if r["reason"].split("->")[-1] in LADDER[5:] for ref in json.loads(r["evidence"])]


def _known_progress(store, holder, refs, end):
    """The global stage is bookkeeping, not information everyone magically knows."""
    return [root for root in sorted(roots(store, refs)) if holder in could_have_seen(store, root)
            or store.conn.execute("SELECT 1 FROM knows WHERE root=? AND guid=? AND learned<?",
                                   (root, holder, end)).fetchone() is not None]


def _band_rose(store, guid, window) -> bool:
    rank = {"fond": 0, "drawn": 1, "lust": 2}
    return any((m := _BAND.match(r[0])) and rank[m[2]] > rank[m[1]] for r in store.conn.execute(
        "SELECT text FROM persona_ledger p WHERE guid=? AND kind='stance_band' AND ts>=? AND ts<? "
        "AND NOT EXISTS (SELECT 1 FROM persona_ledger u WHERE u.kind='undo' AND u.ref=p.id) "
        "AND NOT EXISTS (SELECT 1 FROM meta m, json_each(CASE WHEN "
        "m.key LIKE 'attraction_band_changes:%' THEN m.value ELSE '[]' END) j WHERE j.value=p.id)",
        (guid, *window)))


async def stage_jealousy(ctx) -> None:
    if ctx.done("jealousy", "all"):
        return
    now = ctx.clock.now()

    def write(store):
        attitudes = ctx.attitudes(fresh=True)
        men = ctx.men()
        memories = {g: _observations(store, g, ctx.window) for g in set(attitudes) | men}
        current = json.loads(store.get_meta(f"{_CHANGES}{ctx.night}") or "[]")
        advances = {g: _invitation_refs(store, g, ctx.human, ctx.window, current) for g in attitudes}
        rose = {g for g in attitudes if _band_rose(store, g, ctx.window)}
        rank = {"fond": 0, "drawn": 1, "lust": 2}
        band_ids = store.get_meta(f"attraction_band_changes:{ctx.night}") or "[]"
        remaining_rises = {r["guid"] for r in store.conn.execute(
            "SELECT guid,text FROM persona_ledger p WHERE id IN (SELECT value FROM json_each(?)) "
            "AND NOT EXISTS (SELECT 1 FROM persona_ledger u WHERE u.kind='undo' AND u.ref=p.id)",
            (band_ids,)) if (m := _BAND.match(r["text"])) and rank[m[2]] > rank[m[1]]}
        rose.update(r["guid"] for r in store.conn.execute(
            "SELECT guid,stance_before,stance_after FROM attraction_log WHERE night=?", (ctx.night,))
            if r["guid"] in remaining_rises
            and rank.get(r["stance_after"], -1) > rank.get(r["stance_before"], -1)
            and rank.get(attitudes.get(r["guid"]), -1) > rank.get(r["stance_before"], -1))
        progressed = {g for g in attitudes if store.get_relationship(g, ctx.human).romance_stage in LADDER[2:]}

        def update(holder, target, kind, evidence, obs, spike, mult=1.):
            row = store.conn.execute("SELECT * FROM jealousy WHERE holder=? AND target=?",
                                     (holder, target)).fetchone()
            before = float(row["level"]) if row else 0.
            nights = max(0., (now - row["updated"]) / DAY_S) if row else 1.
            level = jealousy_next(before, nights=nights, obs=obs, spike=spike,
                                   knob=ctx.settings.jealousy, mult=mult)
            shown = list(dict.fromkeys(evidence))
            evidence = shown[:3]
            store.conn.execute(
                "INSERT INTO jealousy(holder,target,kind,level,updated,evidence) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(holder,target) DO UPDATE SET kind=excluded.kind,level=excluded.level, "
                "updated=excluded.updated,evidence=excluded.evidence",
                (holder, target, kind, level, now, json.dumps(evidence)))
            if before < 40 <= level:
                year, week, _ = date.fromisoformat(ctx.night).isocalendar()
                text = (f"{ctx.name(target)} keeps going after {ctx.ident.called}'s attention."
                        if kind == "rivalry" else f"The women here only have eyes for {ctx.ident.called}.")
                ctx.writer.put(reflection_row(holder, text, now=now,
                    actors=[target, ctx.human] if kind == "rivalry" else [target],
                    evidence=evidence, key=f"jealousy:{target}:{year}-W{week:02}",
                    importance=5, valence=-.3), shown=shown)

        for holder in sorted(progressed):
            for target in sorted(progressed - {holder}):
                observed = [m for m in memories[holder] if {target, ctx.human} <= set(m.actors)]
                acts = known_acts(store, holder, target, ctx.window)
                progress = _known_progress(store, holder, advances[target], ctx.window[1])
                update(holder, target, "rivalry", progress + [f"memory:{m.id}" for m in observed] + acts,
                       len(observed) + len(acts), bool(progress) or target in rose and bool(acts))
        for holder in sorted(men):
            observed = [m for m in memories[holder] if ctx.human in m.actors and set(m.actors) & attitudes.keys()]
            acts = []
            for target in attitudes:
                acts.extend(ref for ref in known_acts(store, holder, target, ctx.window)
                            if (row := _chat(store, ref)) is not None and row["channel"] != "whisper"
                            and holder in audience_of(store, row["id"]))
            acts = list(dict.fromkeys(acts))
            progress = _known_progress(store, holder,
                [ref for refs in advances.values() for ref in refs], ctx.window[1])
            update(holder, ctx.human, "envy", progress + [f"memory:{m.id}" for m in observed] + acts,
                   len(observed) + len(acts), bool(progress),
                   MODE_MULT.get(ctx.persona(holder).romance.jealousy_mode, 0.))
    ctx.commit("jealousy", "all", write)
