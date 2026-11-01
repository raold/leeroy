"""State chooses the night's intentions; the scene writer only supplies their words.

Planning stores the intentions, their selection and the scenes in one unit transaction.
These synchronous helpers join that transaction and never run a model or create a feud.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import json
import math
from typing import TYPE_CHECKING

from leeroy_mind.storage.store import Store
from leeroy_mind.wow import is_raid_fight

from .knowledge import is_private, is_secret, knowers
from .motives import trait_today, wants
from .settings import night_bounds

if TYPE_CHECKING:
    from .runner import NightContext

VOLITIONS = ("confront", "make_up", "confide", "gossip", "correct", "recruit", "needle",
             "bond", "mentor", "tease")
LAYER_TRUST = {"surface": 20.0, "middle": 45.0, "deep": 65.0}
NIGHT_OWL_DROP = 10.0
RESOLVE_DAYS = 10
BLOWUP_DAYS = 7
BLOWUP_DELTA = -4

INTENT = {
    "confront": "{a} wants to have it out with {b} about what's been bugging them.",
    "make_up": "{a} wants to patch things up with {b}.",
    "confide": "{a} wants to tell {b} something personal.",
    "gossip": "{a} has something to pass on to {b}.",
    "correct": "{a} wants to set the record straight with {b}.",
    "recruit": "{a} wants {b} along for a mount run.",
    "needle": "{a} wants to needle {b} about the meters.",
    "bond": "{a} just wants to hang out with {b}.",
    "mentor": "{a} is showing {b} the ropes.",
    "tease": "{a} wants to bring up the running joke with {b}.",
}


@dataclass(frozen=True)
class Volition:
    guid: int
    kind: str
    target: int
    weight: float
    ref: str = ""


def _order(v: Volition) -> tuple:
    return -v.weight, VOLITIONS.index(v.kind), v.target, v.guid, v.ref


def _stored(ctx: "NightContext", *, chosen: bool = False) -> dict[int, list[Volition]]:
    result = {}
    for row in ctx.store.conn.execute(
            "SELECT guid,kind,target,weight,ref FROM volition WHERE night=?"
            + (" AND chosen=1" if chosen else ""), (ctx.night,)):
        v = Volition(**dict(row))
        result.setdefault(v.guid, []).append(v)
    return {guid: sorted(result[guid], key=_order) for guid in sorted(result)}


def _raid_members(ctx: "NightContext") -> set[int]:
    result = set()
    for row in ctx.store.conn.execute(
            "SELECT payload FROM game_event WHERE type='boss' AND ts>=? AND ts<?", ctx.window):
        payload = json.loads(row["payload"])
        if (payload.get("state") in {"killed", "wiped"}
                and is_raid_fight(payload.get("map"), payload.get("boss_id"))):
            result.update(payload.get("members") or ())
    return result


def volitions(ctx: "NightContext") -> dict[int, list[Volition]]:
    """Rank each available member's intentions, replacing an uncommitted night's draft.

    A completed encounter plan is immutable: another call must not erase its chosen flags
    after planning reset the motive deficits that generated those intentions.
    """
    from .encounters import layers, pair_setting

    with ctx.store.transaction():
        if ctx.done("encounters", "plan"):
            return _stored(ctx)
        members = ctx.members()
        eligible = set(members)
        people = {guid: ctx.persona(guid) for guid in members}
        traits = {guid: trait_today(ctx.store, ctx.night, guid, p)
                  for guid, p in people.items()}
        candidates: dict[int, dict[tuple[str, int], Volition]] = {g: {} for g in members}

        def add(guid: int, kind: str, target: int, weight: float, ref: str = "") -> None:
            if guid not in eligible or target not in eligible or guid == target:
                return
            v = Volition(guid, kind, target, weight, ref)
            old = candidates[guid].get((kind, target))
            if old is None or _order(v) < _order(old):
                candidates[guid][kind, target] = v

        now = ctx.clock.now()
        conn = ctx.store.conn
        for row in conn.execute("SELECT a,b,started FROM feud WHERE ended IS NULL AND started<=?",
                                (now,)):
            days = (now - row["started"]) / 86400
            for a, b in ((row["a"], row["b"]), (row["b"], row["a"])):
                add(a, "confront", b, 1.0)
                if days >= 5:
                    weight = min(1.5, 0.6 + 0.1 * (days - 5))
                    add(a, "make_up", b, weight * (3 if days >= RESOLVE_DAYS else 1))

        recent_start = night_bounds((date.fromisoformat(ctx.night) - timedelta(days=2)).isoformat(),
                                    ctx.tz)[0]
        negative = {(r["a"], r["b"]) for r in conn.execute(
            "SELECT DISTINCT a,b FROM rel_delta WHERE delta<0 AND applied=1 AND ts>=? AND ts<? "
            "AND dim IN ('affinity','trust','respect','familiarity')", (recent_start, ctx.window[1]))}
        opened = {(r["guid"], r["other"], r["layer"]) for r in conn.execute(
            "SELECT guid,other,layer FROM layer_open")}
        raiders = _raid_members(ctx) & eligible
        for a, p in people.items():
            desired = set(wants(ctx.store, a))
            inner = {g for g, weight in layers(ctx, a).items() if weight == 3.0}
            for b in members:
                if a == b:
                    continue
                rel = ctx.store.get_relationship(a, b)
                if (a, b) in negative and rel.affinity <= -30:
                    add(a, "confront", b, 0.8)
                if traits[a]["extraversion"] <= 4:
                    layer = next((d for i, d in enumerate(p.disclosures)
                                  if (a, b, i) not in opened), None)
                    if layer is not None and rel.trust >= (
                            LAYER_TRUST[layer.depth] - (NIGHT_OWL_DROP if p.real_life.night_owl else 0)):
                        add(a, "confide", b, 0.9)
                if desired & {"discovery", "customisation"} and (
                        b in inner or set(p.game.outside_raids) & set(people[b].game.outside_raids)
                        & {"collecting", "fishing"}):
                    add(a, "recruit", b, 0.8)
                if desired & {"socialising", "teamwork"} and (
                        b in inner or pair_setting(ctx, a, b) is not None):
                    add(a, "bond", b, 0.6)
            if "competition" in desired:
                rivals = sorted(g for g in raiders if g != a
                                and people[g].play.raid_role == p.play.raid_role)
                if rivals:
                    add(a, "needle", rivals[0], 0.7)

        # Only rumor rows can generate these intentions. Private tellings have no such rows
        # (Task 16); reject sensitive roots defensively if an old/seeded row violates that rule.
        for rumor in conn.execute("SELECT id,root,about FROM rumor WHERE status='live' ORDER BY id"):
            root = rumor["root"]
            if is_secret(ctx.store, root) or is_private(ctx.store, root):
                continue
            known = knowers(ctx.store, root)
            states = conn.execute(
                "SELECT s.guid,s.state,v.op FROM rumor_state s LEFT JOIN rumor_version v "
                "ON v.id=s.version AND v.rumor=s.rumor WHERE s.rumor=?", (rumor["id"],)).fetchall()
            ref = f"rule:rumor:{rumor['id']}"
            for state in states:
                if state["state"] == "spreader":
                    for b in members:
                        if b not in known:
                            add(state["guid"], "gossip", b, 0.7, ref)
                if state["op"] in {"sharpen", "assimilate"}:
                    for a, p in people.items():
                        if a == rumor["about"] or (p.play.guild_role == "officer" and a in known):
                            add(a, "correct", state["guid"], 0.8, ref)

        for row in conn.execute(
                "SELECT mentor,mentee FROM mentor WHERE ended IS NULL AND started<=? AND ends>?",
                (now, now)):
            add(row["mentor"], "mentor", row["mentee"], 0.8)
        for row in conn.execute(
                "SELECT a.guid,l.originator FROM lexicon_adopter a JOIN lexicon l ON l.id=a.term "
                "WHERE a.active=1 AND l.kind='joke' AND l.status='active'"):
            add(row["guid"], "tease", row["originator"], 0.5)

        result = {}
        conn.execute("DELETE FROM volition WHERE night=?", (ctx.night,))
        for guid in members:
            count = ctx.settings.volitions_per_member if traits[guid]["extraversion"] >= 7 else 1
            selected = sorted(candidates[guid].values(), key=_order)[:max(0, count)]
            if selected:
                result[guid] = selected
            for v in selected:
                conn.execute("INSERT INTO volition(night,guid,kind,target,weight,ref) VALUES (?,?,?,?,?,?)",
                             (ctx.night, v.guid, v.kind, v.target, v.weight, v.ref))
        return result


def arc_key(kind: str, *ids: int) -> str:
    return ":".join((kind, *(str(guid) for guid in sorted(ids))))


def arc_tension(store: Store, key: str, now: float) -> float:
    with store.transaction():
        arc = store.conn.execute("SELECT kind,opened,status FROM story_arc WHERE key=?", (key,)).fetchone()
        if arc is not None and arc["status"] == "resolved":
            return 0.0
        if arc is not None and arc["kind"] == "feud":
            return min(1.5, 0.5 + 0.1 * max(0.0, (now - arc["opened"]) / 86400))
        kind, _, number = key.partition(":")
        if kind == "rumor" and number.isdecimal():
            live = store.conn.execute("SELECT 1 FROM rumor WHERE id=? AND status='live'",
                                      (int(number),)).fetchone()
            return 0.5 if live else 0.0
        return 0.0


def _arc(v: Volition) -> str:
    if v.kind in {"confront", "make_up"}:
        return arc_key("feud", v.guid, v.target)
    if v.kind in {"gossip", "correct"} and v.ref.startswith("rule:rumor:"):
        number = v.ref.removeprefix("rule:rumor:")
        if number.isdecimal():
            return arc_key("rumor", int(number))
    return ""


def choose(ctx: "NightContext", vols: dict[int, list[Volition]]) -> list[Volition]:
    """Choose disjoint scenes under the night's tension and scene budgets."""
    with ctx.store.transaction():
        if ctx.done("encounters", "plan"):
            return [v for values in _stored(ctx, chosen=True).values() for v in values]
        conn = ctx.store.conn
        day = date.fromisoformat(ctx.night)
        first = (day - timedelta(days=BLOWUP_DAYS)).isoformat()
        blowup = conn.execute(
            "SELECT 1 FROM encounter WHERE blowup=1 AND night>=? AND night<? LIMIT 1",
            (first, ctx.night)).fetchone()
        previous = night_bounds((day - timedelta(days=1)).isoformat(), ctx.tz)
        new_feud = conn.execute("SELECT 1 FROM feud WHERE started>=? AND started<? LIMIT 1",
                                previous).fetchone()
        eligible = set(ctx.members())
        candidates = [v for values in vols.values() for v in values
                      if v.guid in eligible and v.target in eligible and v.guid != v.target
                      and v.kind in VOLITIONS and math.isfinite(v.weight) and v.weight > 0
                      and not (v.kind == "confront" and (blowup or new_feud))]
        now = ctx.clock.now()
        tensions = {key: arc_tension(ctx.store, key, now) for key in {_arc(v) for v in candidates}}
        candidates.sort(key=lambda v: (-v.weight * (1 + tensions[_arc(v)]), *_order(v)[1:]))
        selected, used = [], set()
        confrontation_chosen = False
        conn.execute("UPDATE volition SET chosen=0 WHERE night=?", (ctx.night,))
        for v in candidates:
            if len(selected) >= ctx.settings.volition_scenes:
                break
            if (v.guid in used or v.target in used
                    or (v.kind == "confront" and confrontation_chosen)):
                continue
            selected.append(v)
            used.update((v.guid, v.target))
            # Scenes render concurrently: reserve the one possible blow-up before any
            # model call, so two disjoint confrontations cannot spend the same allowance.
            confrontation_chosen = confrontation_chosen or v.kind == "confront"
            conn.execute(
                "INSERT INTO volition(night,guid,kind,target,weight,ref,chosen) VALUES (?,?,?,?,?,?,1) "
                "ON CONFLICT(night,guid,kind,target) DO UPDATE SET weight=excluded.weight, "
                "ref=excluded.ref, chosen=1", (ctx.night, v.guid, v.kind, v.target, v.weight, v.ref))
        return selected


def reset_want(store: Store, guid: int, kind: str, *, now: float) -> None:
    """Called only as part of persisting a chosen scene, never while merely ranking it."""
    motives = {"recruit": ("discovery", "customisation"), "needle": ("competition",),
               "bond": ("socialising", "teamwork"), "confide": ("relationship",)}.get(kind, ())
    with store.transaction():
        for motive in motives:
            store.conn.execute("UPDATE motive SET deficit=0,updated=? WHERE guid=? AND motive=?",
                               (now, guid, motive))
