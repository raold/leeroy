"""The Social tab's read-only question to mind.db: the guild's drama as the night's stages
wrote it (feuds, jealousy, romances, how members feel about the owner, live rumours, the guild
mood). A table an older mind.db lacks is named in `missing` and reads as empty."""
from leeroy_mind.nightly.mood import octant
from leeroy_mind.storage.rows import ROMANCE_STAGES

from . import views
from .feed import guild_mood, human_guid

SOCIAL_TABLES = ("relationship", "attraction_state", "feud", "jealousy", "rumor", "rumor_state",
                 "knows", "guild_mood", "pad_mood")
ROMANCE_ORDER = ROMANCE_STAGES  # the stored ladder, furthest last
JEALOUSY_MAX = 30
RUMOURS_MAX = 20


def _stage(s) -> int:
    return ROMANCE_ORDER.index(s) if s in ROMANCE_ORDER else 0


def social_view(conn, *, now: float) -> dict:
    have = views.tables(conn)
    missing = [t for t in SOCIAL_TABLES if t not in have]
    human = human_guid(conn)

    def q(sql, p=()):
        return views.rows(conn, sql, p)

    toward = ({r["a"]: r for r in q("SELECT * FROM relationship WHERE b = ?", (human,))}
              if human else {})
    states = {r["guid"]: r for r in q("SELECT * FROM attraction_state")}
    felt = {g for g, r in toward.items() if r["romance_stage"] != "none"}
    you = []
    for g in sorted((set(states) | felt) - {human}):
        s, r = states.get(g), toward.get(g)
        you.append({"guid": g, "stance": s["stance"] if s else None,
                    "affinity": r["affinity"] if r else 0,
                    "attraction": r["attraction"] if r else 0,
                    "romance_stage": r["romance_stage"] if r else "none",
                    "together_since": s["together_since"] if s else None,
                    "hurt_until": s["hurt_until"] if s else 0,
                    "hurt_deep": bool(s and s["hurt_deep"]),
                    "rush_until": s["rush_until"] if s else 0,
                    "bids_7d": s["bids_7d"] if s else 0})
    you.sort(key=lambda y: (-y["attraction"], y["guid"]))

    feuds = [{"id": r["id"], "a": r["a"], "b": r["b"], "started": r["started"], "cause": r["cause"]}
             for r in q("SELECT id, a, b, started, cause FROM feud WHERE ended IS NULL "
                        "ORDER BY started DESC, id DESC")]
    jealousy = [{"holder": r["holder"], "target": r["target"], "kind": r["kind"],
                 "level": r["level"], "updated": r["updated"]}
                for r in q("SELECT holder, target, kind, level, updated FROM jealousy "
                           "WHERE level > 0 ORDER BY level DESC, holder, target LIMIT ?",
                           (JEALOUSY_MAX,))]

    stage = {(r["a"], r["b"]): r["romance_stage"]
             for r in q("SELECT a, b, romance_stage FROM relationship")}
    pairs = {tuple(sorted(k)) for k in stage if human not in k and k[0] != k[1]}
    romances = []
    for a, b in pairs:
        ab, ba = stage.get((a, b), "none"), stage.get((b, a), "none")
        if ab != "none" or ba != "none":
            romances.append({"a": a, "b": b, "ab": ab, "ba": ba})
    romances.sort(key=lambda r: (-max(_stage(r["ab"]), _stage(r["ba"])), r["a"], r["b"]))

    rumours = []
    for r in q("SELECT id, root, about, canonical, created, last_told FROM rumor "
               "WHERE status = 'live' ORDER BY last_told DESC, id DESC LIMIT ?", (RUMOURS_MAX,)):
        side = {"spreader": [], "stifler": []}
        for s in q("SELECT guid, state FROM rumor_state WHERE rumor = ? ORDER BY guid", (r["id"],)):
            side[s["state"]].append(s["guid"])
        knowers = q("SELECT count(*) FROM knows WHERE root = ?", (r["root"],))
        rumours.append({"id": r["id"], "about": r["about"], "text": r["canonical"],
                        "created": r["created"], "last_told": r["last_told"],
                        "spreaders": side["spreader"], "stiflers": side["stifler"],
                        "knowers": knowers[0][0] if knowers else 0})

    octants: dict[str, int] = {}
    for r in q("SELECT p, a, d FROM pad_mood"):
        o = octant(r["p"], r["a"], r["d"])
        octants[o] = octants.get(o, 0) + 1

    return {"human": human, "names": {str(g): n for g, n in views.names(conn).items()},
            "missing": missing, "mood": guild_mood(conn), "octants": octants, "you": you,
            "feuds": feuds, "jealousy": jealousy, "romances": romances, "rumours": rumours}
