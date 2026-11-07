"""The Realm tab's read-only question to mind.db: the rival guilds with their people and
progress, the race for first kills, this week's poaching offer, the rivals' Trade lines and the
canon achievements. A table an older mind.db lacks is named in `missing` and reads as empty."""
import sqlite3

from leeroy_mind.realm import race
from leeroy_mind.realm.canon import REALM_FIRSTS
from leeroy_mind.wow import boss_name

from . import views

REALM_TABLES = ("realm_guild", "realm_actor", "realm_raider", "realm_progress", "realm_line",
                "realm_canon")
LINES_MAX = 50
POACH_KEYS = ("period", "guild", "officer", "member", "ts")
ROLES = ("tank", "healer", "melee", "ranged")


def _own(conn) -> dict | None:
    v = views.meta_json(conn, "realm:guild_name")
    if isinstance(v, dict) and isinstance(v.get("guild"), int) and isinstance(v.get("name"), str):
        return {"guild": v["guild"], "name": v["name"]}
    return None


def _guild_kills(conn, now: float) -> dict:
    try:
        return race.Race._live_kills(conn, int(now * 1000))
    except sqlite3.OperationalError as e:
        if str(e).startswith(("no such table", "no such column")):
            return {}
        raise


def realm_view(conn, *, now: float) -> dict:
    have = views.tables(conn)
    missing = [t for t in REALM_TABLES if t not in have]
    own = _own(conn)

    def q(sql, p=()):
        return views.rows(conn, sql, p)

    own_id = own["guild"] if own else None
    rivals = []
    for g in q("SELECT id, name, role, faction FROM realm_guild WHERE status = 'active' "
               "ORDER BY id"):
        if g["id"] == own_id:
            continue
        actors = q("SELECT guid, name, role, place FROM realm_actor WHERE guild = ? "
                   "AND status = 'active' ORDER BY guid", (g["id"],))
        raiders = q("SELECT role, avg_ilvl FROM realm_raider WHERE guild = ?", (g["id"],))
        counts = {r: sum(1 for x in raiders if x["role"] == r) for r in ROLES}
        counts["ilvl"] = round(sum(x["avg_ilvl"] for x in raiders) / len(raiders)) if raiders else 0
        kills = q("SELECT count(*) FROM realm_progress WHERE guild = ? AND state = 'killed'",
                  (g["id"],))
        rivals.append({
            "id": g["id"], "name": g["name"], "role": g["role"], "faction": g["faction"],
            "officers": [{"guid": a["guid"], "name": a["name"], "place": a["place"]}
                         for a in actors if a["role"] == "officer"],
            "gankers": sum(1 for a in actors if a["role"] == "ganker"),
            "applicants": sum(1 for a in actors if a["role"] == "applicant"),
            "raiders": counts, "kills": kills[0][0] if kills else 0})

    progress: dict[tuple, dict] = {}
    for p in q("SELECT guild, map, boss_id, difficulty, state, best_pct, killed_at, source "
               "FROM realm_progress"):
        progress.setdefault((p["map"], p["boss_id"], p["difficulty"]), {})[p["guild"]] = p
    live = _guild_kills(conn, now)
    keys = [k for k in race.race_keys() if k in live or k in progress]
    keys.sort(key=lambda k: (k not in live, live.get(k, 0), k[0], k[1], k[2]))
    shown = {r["id"] for r in rivals}
    races = []
    for k in keys:
        row = progress.get(k, {})
        races.append({
            "map": k[0], "boss": k[1], "difficulty": k[2], "boss_name": boss_name(k[0], k[1]),
            "mode": race.mode_label(k[0], k[2]), "guild_at": live.get(k),
            "rivals": {str(g): {"state": p["state"], "best_pct": p["best_pct"],
                                "killed_at": p["killed_at"], "source": p["source"]}
                       for g, p in row.items() if g in shown}})

    guilds = {r["id"]: r["name"] for r in q("SELECT id, name FROM realm_guild")}
    actors = {r["guid"]: r["name"] for r in q("SELECT guid, name FROM realm_actor")}
    marker = views.meta_json(conn, "realm:poach:week")
    poach = None
    if isinstance(marker, dict):
        poach = {k: marker.get(k) for k in POACH_KEYS}
        poach["officer_name"] = actors.get(marker.get("officer"))
        poach["guild_name"] = guilds.get(marker.get("guild"))
    lines = [{"ts": r["ts"], "guid": r["guid"], "name": actors.get(r["guid"]) or "#%d" % r["guid"],
              "guild": guilds.get(r["guild"], ""), "channel": r["channel"], "text": r["text"]}
             for r in q("SELECT ts, guid, guild, channel, text FROM realm_line "
                        "WHERE kind = 'rival' AND state = 'sent' ORDER BY ts DESC, id DESC LIMIT ?",
                        (LINES_MAX,))]
    canon = [{"achievement": r["achievement"],
              "name": REALM_FIRSTS.get(r["achievement"]) or "achievement %d" % r["achievement"],
              "guid": r["guid"], "ts": r["ts"]}
             for r in q("SELECT achievement, guid, ts FROM realm_canon WHERE state = 'granted' "
                        "ORDER BY ts DESC, achievement, guid")]
    return {"missing": missing, "own": own, "rivals": rivals, "race": races, "poach": poach,
            "lines": lines, "canon": canon,
            "names": {str(g): n for g, n in views.names(conn).items()}}
