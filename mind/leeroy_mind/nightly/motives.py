"""Rule-derived MMO motives and seeded daily Big Five states (P1c Task 13).

Weights are preferences, not diagnoses or fixed identities. Deficits change once
per night; daily trait samples remain fixed even if a stage is retried later.
"""
import json
from typing import TYPE_CHECKING

from leeroy_mind.nightly.places import place_kind
from leeroy_mind.personas.schema import Persona
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import faction_known, is_raid_fight

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

YEE = ("advancement", "mechanics", "competition", "socialising", "relationship", "teamwork",
       "discovery", "role_play", "customisation", "escapism")
DEFICIT_RATE = 0.34
WANT = 1.0
TRAITS = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")
TRAIT_SPREAD = 0.8

_MOTIVATION = {
    "achiever": {"advancement": 0.3, "mechanics": 0.2},
    "competitor": {"competition": 0.4},
    "social": {"socialising": 0.3, "relationship": 0.2},
    "explorer": {"discovery": 0.3, "role_play": 0.1},
}
_THINKS = {
    "theorycrafter": {"mechanics": 0.4},
    "lore": {"discovery": 0.2, "role_play": 0.3},
    "collector": {"customisation": 0.4, "discovery": 0.1},
    "competitor": {"competition": 0.3},
    "social": {"socialising": 0.2, "teamwork": 0.2},
}
_OUTSIDE = {
    "battlegrounds": ("competition", 0.1), "arena": ("competition", 0.1),
    "collecting": ("customisation", 0.1), "fishing": ("escapism", 0.1),
    "idling": ("escapism", 0.1), "crafting": ("advancement", 0.1),
    "auction_house": ("advancement", 0.1), "dungeons": ("teamwork", 0.1),
    "gathering": ("escapism", 0.05),
}


def _clamp(value: float, low: float, high: float) -> float:
    return float(min(high, max(low, value)))


def motive_weights(p: Persona) -> dict[str, float]:
    weights = dict.fromkeys(YEE, 0.3)
    for additions in (_MOTIVATION[p.game.motivation], _THINKS[p.game.thinks_as]):
        for motive, amount in additions.items():
            weights[motive] += amount
    for interest in p.game.outside_raids:
        motive, amount = _OUTSIDE[interest]
        weights[motive] += amount
    if p.play.guild_role == "officer":
        weights["teamwork"] += 0.2
    weights["escapism"] += 0.1 * sum(h.substance in {"alcohol", "weed"} for h in p.habits)
    if p.big_five.neuroticism.score >= 7:
        weights["escapism"] += 0.1
    return {motive: _clamp(value, 0.1, 0.9) for motive, value in weights.items()}


def _events(ctx: "NightContext"):
    start, end = ctx.window
    for row in ctx.store.conn.execute(
            "SELECT type, payload FROM game_event WHERE ts >= ? AND ts < ? "
            "AND type IN ('loot_won', 'boss', 'bg_end') ORDER BY ts, id", (start, end)):
        yield row["type"], json.loads(row["payload"])


def _raid_fight(payload, guid: int) -> bool:
    return (guid in (payload.get("members") or ()) and payload.get("state") in {"killed", "wiped"}
            and is_raid_fight(payload.get("map"), payload.get("boss_id")))


def _hours(ctx: "NightContext", guid: int) -> float:
    row = ctx.store.conn.execute("SELECT hours FROM wow_hours WHERE night=? AND guid=?",
                                 (ctx.night, guid)).fetchone()
    return row["hours"] if row else 0.0


def met(ctx: "NightContext", guid: int) -> set[str]:
    """Only this member's own completed experiences satisfy a motive."""
    with ctx.store.transaction():
        return _met(ctx, guid)


def _met(ctx: "NightContext", guid: int) -> set[str]:
    out = set()
    roster_member = ctx.store.cast().get(guid)
    faction = faction_known(roster_member.race) if roster_member else None
    for kind, payload in _events(ctx):
        if kind == "loot_won" and payload.get("guid") == guid:
            out.add("advancement")
        elif kind == "boss" and _raid_fight(payload, guid):
            out.update(("mechanics", "teamwork"))
            if payload["state"] == "killed":
                out.add("competition")
        elif (kind == "bg_end" and guid in (payload.get("members") or ())
              and faction is not None and payload.get("winner") == faction):
            out.add("competition")

    start, end = ctx.window
    sent = ctx.store.conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE guid=? AND ts>=? AND ts<? AND dropped IS NULL",
        (guid, start, end)).fetchone()[0]
    encounters = ctx.store.conn.execute(
        "SELECT private, setting FROM encounter WHERE night=? AND status='done' AND (a=? OR b=?)",
        (ctx.night, guid, guid)).fetchall()
    if sent >= 5 or len(encounters) >= 2:
        out.add("socialising")
    whispered = ctx.store.conn.execute(
        "SELECT 1 FROM chat_line WHERE channel='whisper' AND ts>=? AND ts<? "
        "AND ((from_guid=? AND to_guid=?) OR (from_guid=? AND to_guid=?)) LIMIT 1",
        (start, end, guid, ctx.human, ctx.human, guid)).fetchone()
    if whispered or any(row["private"] for row in encounters):
        out.add("relationship")
    if any(place_kind(row["setting"]) in {"collecting", "fishing"} for row in encounters):
        out.update(("discovery", "customisation"))
    # Event time lets a memory extracted after the window close describe tonight;
    # a retold old story alone does not count as a new role-play experience.
    memories = ctx.store.conn.execute(
        "SELECT evidence FROM memory WHERE owner=? AND status='active' AND t_event>=? AND t_event<?",
        (guid, start, end))
    for row in memories:
        if any(isinstance(ref, str) and ref.partition(":")[0] in {"chronicle", "story"}
               and ref.partition(":")[2].isdigit() and int(ref.partition(":")[2]) > 0
               for ref in json.loads(row["evidence"])):
            out.add("role_play")
            break
    if _hours(ctx, guid) >= 4:
        out.add("escapism")
    return out


def step_motives(ctx: "NightContext") -> None:
    """Refresh weights and accrue/reset deficits atomically once per night."""
    if ctx.done("wowlife", "motives"):
        return

    def write(store: Store) -> None:
        for guid in ctx.members(include_away=True):
            weights, satisfied = motive_weights(ctx.persona(guid)), met(ctx, guid)
            old = {row["motive"]: row["deficit"] for row in store.conn.execute(
                "SELECT motive, deficit FROM motive WHERE guid=?", (guid,))}
            for motive, weight in weights.items():
                deficit = (0.0 if motive in satisfied else
                           _clamp(old.get(motive, 0.0) + weight * DEFICIT_RATE, 0.0, 2.0))
                store.conn.execute(
                    "INSERT INTO motive(guid,motive,weight,deficit,updated) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(guid,motive) DO UPDATE SET weight=excluded.weight, "
                    "deficit=excluded.deficit, updated=excluded.updated",
                    (guid, motive, weight, deficit, ctx.clock.now()))
    ctx.commit("wowlife", "motives", write)


def wants(store: Store, guid: int) -> list[str]:
    with store.transaction():
        return [row["motive"] for row in store.conn.execute(
            "SELECT motive FROM motive WHERE guid=? AND deficit>=? ORDER BY deficit DESC, motive",
            (guid, WANT)) if row["motive"] in YEE]


def _beat_mood(ctx: "NightContext", guid: int) -> float:
    # Beats have no mood column. Their owned offscreen memory retains the original
    # valence even when it was too small to create an emotion. Count each beat once.
    rows = ctx.store.conn.execute(
        "SELECT (SELECT m.valence FROM memory m WHERE m.owner=t.guid AND m.source='offscreen' "
        "AND m.status='active' AND EXISTS (SELECT 1 FROM json_each(m.evidence) e "
        "WHERE e.value='beat:' || b.id) ORDER BY m.id DESC LIMIT 1) AS mood "
        "FROM life_beat b JOIN life_thread t ON t.id=b.thread_id WHERE b.night=? AND t.guid=?",
        (ctx.night, guid))
    return sum(row["mood"] for row in rows if row["mood"] is not None)


def sample_traits(ctx: "NightContext", guid: int) -> dict[str, float]:
    """Store one member's sample; stage_wowlife owns the cast-wide traits unit."""
    with ctx.store.transaction():
        stored = {row["trait"]: row["value"] for row in ctx.store.conn.execute(
            "SELECT trait, value FROM trait_state WHERE night=? AND guid=?", (ctx.night, guid))
                  if row["trait"] in TRAITS}
        if len(stored) == len(TRAITS):
            return {trait: stored[trait] for trait in TRAITS}
        p = ctx.persona(guid)
        shifts = dict.fromkeys(TRAITS, 0.0)
        if any(kind == "boss" and _raid_fight(payload, guid) for kind, payload in _events(ctx)):
            shifts["extraversion"] += 0.5
        if _hours(ctx, guid) >= 10:
            shifts["conscientiousness"] -= 1.0
            shifts["agreeableness"] -= 0.5
        if ctx.store.get_state(guid).altered_state in {"drunk", "tipsy"}:
            shifts["extraversion"] += 1.0
            shifts["conscientiousness"] -= 1.0
        mood = _beat_mood(ctx, guid)
        shifts["neuroticism"] -= mood
        shifts["extraversion"] += 0.5 * mood
        rng = ctx.rng("traits", guid)
        sampled = {trait: _clamp(getattr(p.big_five, trait).score + rng.gauss(0, TRAIT_SPREAD)
                                 + shifts[trait], 1.0, 10.0) for trait in TRAITS}
        for trait in TRAITS:
            if trait not in stored:
                ctx.store.conn.execute("INSERT INTO trait_state(night,guid,trait,value) VALUES (?,?,?,?)",
                                       (ctx.night, guid, trait, sampled[trait]))
        return {trait: stored.get(trait, sampled[trait]) for trait in TRAITS}


def trait_today(store: Store, night: str, guid: int, p: Persona) -> dict[str, float]:
    with store.transaction():
        stored = {row["trait"]: row["value"] for row in store.conn.execute(
            "SELECT trait, value FROM trait_state WHERE night=? AND guid=?", (night, guid))}
    return {trait: stored.get(trait, float(getattr(p.big_five, trait).score)) for trait in TRAITS}
