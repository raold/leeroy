"""The arc's ladder: the stages she climbs toward him, the gate into each one, how fast an
attitude and a style take them, and the mapping of the old stored stages onto this ladder.

GATED runs from none to planning: each step up it is gated by her scores and his play. Weekend
and after follow a meetup, and ex an ending, never a score."""
import json
import logging
import sqlite3
from dataclasses import dataclass

from leeroy_mind.storage.rows import ROMANCE_STAGES, Relationship

log = logging.getLogger("leeroy_mind.arc")


@dataclass(frozen=True)
class Gate:
    attraction: int
    affinity: int
    trust: int
    min_play_days: float
    depth: int


# Keyed by the stage entered. Each gate holds the ones below it, so meeting a stage's scores
# means meeting every lower stage's too.
GATES: dict[str, Gate] = {
    "notices": Gate(20, 10, 0, 1, 0),
    "banter": Gate(30, 15, 5, 2, 0),
    "whispers": Gate(40, 20, 10, 2, 0),
    "late_nights": Gate(50, 25, 20, 3, 1),
    "voice": Gate(55, 30, 30, 3, 1),
    "picture": Gate(60, 35, 35, 2, 1),
    "confession": Gate(65, 40, 40, 3, 2),
    "planning": Gate(70, 45, 45, 3, 2),
}
GATED = ROMANCE_STAGES[:ROMANCE_STAGES.index("planning") + 1]
PACE_ATTITUDE = {"lust": 0.7, "drawn": 1.0, "fond": 2.0}
PACE_STYLE = {"bold": .8, "theatrical": .8, "earnest": 1., "competitive": 1.,
              "shy": 1.5, "guarded": 2.0}
FULL_ARC = frozenset({"drawn", "lust"})  # a fond woman stops at FOND_CAP
FOND_CAP = "whispers"
NOTICED = frozenset({"voice", "planning", "weekend", "after"})  # the guild can tell
NOTICED_HANDLED = "arc_noticed:"  # meta key prefix: <prefix><night> holds the rel_delta ids it noticed
# The old ladder's stages that are gone, and where a stored one lands (migration romance_arc).
LEGACY = {"vulnerable": "late_nights", "invitation": "planning", "acknowledged": "confession"}
_FLOOR = "whispers"
_STASH = "romance_arc:legacy"
_MAPPED = "romance_arc:mapped"


def best_stage(rel, *, ceiling: str) -> str:
    """The highest gated stage up to `ceiling` whose attraction, affinity and trust `rel` meets,
    never below "whispers". Only the scores count: play days, depth and new evidence are the
    night's to judge. `ceiling` is a gated stage from "whispers" up."""
    floor = GATED.index(_FLOOR)
    if ceiling not in GATED[floor:]:
        raise ValueError(f"not a gated stage from {_FLOOR} up: {ceiling!r}")
    for stage in reversed(GATED[floor + 1:GATED.index(ceiling) + 1]):
        gate = GATES[stage]
        if (rel.attraction >= gate.attraction and rel.affinity >= gate.affinity
                and rel.trust >= gate.trust):
            return stage
    return _FLOOR


def map_stored_stages(conn: sqlite3.Connection) -> None:
    """The romance_arc migration's data step (storage.db.DATA_STEPS). The migration's script has
    rebuilt relationship, mapping each old stage by LEGACY, and stashed every such row's old
    stage in meta romance_arc:legacy ([[a, b, old], ...]). A former invitation is lowered to
    the stage her scores support (best_stage, up to planning). Only a drawn or lust woman goes
    past FOND_CAP (controller ruling): her stance and gender are read from her stored persona,
    and a man, a fond or none woman, or a row with no readable persona lands on FOND_CAP if
    above it (never raised). Every remapped row is recorded in meta romance_arc:mapped as
    {"<a>:<b>": [old, new]}, and the stash is removed. No rel_delta is written, so her pace
    clock is unchanged. A stash it cannot place (none, a row that is gone, an old stage with
    no place) raises, so the migration rolls back with the old stages. It runs inside the
    migration's transaction and never commits."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_STASH,)).fetchone()
    if row is None:
        raise RuntimeError(f"romance_arc: no {_STASH} stash to map")
    mapped, lowered = {}, 0
    for a, b, old in json.loads(row[0]):
        found = conn.execute(
            "SELECT r.attraction, r.affinity, r.trust, "
            "CASE WHEN json_valid(p.json) THEN json_extract(p.json, '$.romance.stance') END, "
            "CASE WHEN json_valid(p.json) "
            "THEN json_extract(p.json, '$.identity.player_gender') END "
            "FROM relationship r LEFT JOIN persona_core p ON p.guid = r.a "
            "WHERE r.a = ? AND r.b = ?", (a, b)).fetchone()
        if found is None or old not in LEGACY:
            raise RuntimeError(f"romance_arc: cannot map {a}:{b} from {old!r}")
        attraction, affinity, trust, stance, gender = found
        new = LEGACY[old]
        if old == "invitation":
            new = best_stage(Relationship(a=a, b=b, attraction=attraction, affinity=affinity,
                                          trust=trust), ceiling=new)
        full_arc = gender == "female" and stance in FULL_ARC
        if not full_arc and ROMANCE_STAGES.index(new) > ROMANCE_STAGES.index(FOND_CAP):
            new = FOND_CAP
        lowered += new != LEGACY[old]
        conn.execute("UPDATE relationship SET romance_stage = ? WHERE a = ? AND b = ?",
                     (new, a, b))
        mapped[f"{a}:{b}"] = [old, new]
    if mapped:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (_MAPPED, json.dumps(mapped)))
    conn.execute("DELETE FROM meta WHERE key = ?", (_STASH,))
    log.info("romance_arc_mapped: n=%d lowered=%d", len(mapped), lowered)
