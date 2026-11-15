"""The Raid and Guild tabs' read-only questions to mind.db (asked through feed.read, so on a
read-only connection that is closed again). Guild life writes these rows; the console only
reads them:

- guild_raid (a raid: started, ended, raid key, size, heroic, offline, lead, tank, loot officer,
  kills, wipes, outcome) and guild_attendance (raided or benched);
- meta guild:raid:<id>:plan (its members and bench), guild:raid:<id>:instance (bound to an
  instance), guild:raid:<id>:not-raided (an offline run that never reached a fight: a record,
  without attendance or DKP), guild:raid:<id>:mvp (the MVP vote and its result), guild:loot:<id>:<slot> (each
  epic drop: its name, price, state and award) and guild:raid-night:quiet-night;
- dkp_ledger (every award and charge: kill, attendance, standby, item, decay, adjust);
- cast_member (the roster) and persona_core's play section (guild role, officer title, raid
  role): nothing else of a persona is read;
- award (season awards) and chronicle (the weekly guild chronicle).

A table an older mind.db lacks reads as empty."""
import json
import sqlite3
from datetime import datetime

from leeroy_mind.clock import night_of
from leeroy_mind.guild.raid_night import QUIET_NIGHT_KEY
from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.guild.roles import role_of, spec_name

from .feed import human_guid

RECENT = 12
LOOT_HISTORY = 100
CHRONICLE = 20
DKP_EVENTS = 60
DUE_RAIDS = 4
DUE_MAX = 10
DKP_RAIDS = 20
_PLAY_KEYS = ("guild_role", "officer_title", "raid_role")


def _meta(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _rows(conn, sql, params=()):
    """The rows, or none when the table is missing (a mind.db from before guild life)."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _num(v):
    return v if type(v) in (int, float) else None


def _guids(value) -> list[int]:
    return [g for g in value if type(g) is int] if isinstance(value, list) else []


def people(conn) -> dict[int, dict]:
    """The cast as the console names them: game facts from cast_member, and from each persona
    only its guild role, officer title and raid role."""
    from leeroy_mind.personas.tiers import is_cast
    plays = {}
    for row in _rows(conn, "SELECT guid, json FROM persona_core"):
        if not is_cast(conn,row["guid"]):
            continue
        try:
            play = json.loads(row["json"]).get("play") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(play, dict):
            plays[row["guid"]] = {k: play[k] if isinstance(play.get(k), str) else ""
                                  for k in _PLAY_KEYS}
    out = {}
    for r in _rows(conn, "SELECT guid, name, class, level, rank, online, map, zone, spec_tab "
                         "FROM cast_member"):
        if not is_cast(conn,r["guid"]):
            continue
        play = plays.get(r["guid"], dict.fromkeys(_PLAY_KEYS, ""))
        out[r["guid"]] = {
            "guid": r["guid"], "name": r["name"], "class": r["class"], "level": r["level"],
            "rank": r["rank"], "online": bool(r["online"]), "map": r["map"], "zone": r["zone"],
            "spec_tab": r["spec_tab"], "spec": spec_name(r["class"], r["spec_tab"]),
            "role": role_of(r["class"], r["spec_tab"], play["raid_role"] or None),
            "guild_role": play["guild_role"], "officer_title": play["officer_title"],
            "persona": r["guid"] in plays}
    return out


def _loot(conn, raid_id: int) -> list[dict]:
    prefix = f"guild:loot:{raid_id}:"
    out = []
    for row in _rows(conn, "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
                     (len(prefix), prefix)):
        try:
            rec = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        award = rec.get("award") if isinstance(rec.get("award"), dict) else {}
        out.append({"ordinal": _num(rec.get("ordinal")) or 0,
                    "name": rec.get("name") if isinstance(rec.get("name"), str) else "",
                    "item_id": _num(rec.get("item_id")), "price": _num(rec.get("price")),
                    "state": rec.get("state") if isinstance(rec.get("state"), str) else "",
                    "winner": _num(award.get("guid")), "paid": _num(award.get("paid")),
                    "close": award.get("close") is True, "runner_up": _num(award.get("runner_up")),
                    "claimed": rec.get("human_claimed") is True,
                    "received": _num(rec.get("received"))})
    return sorted(out, key=lambda r: r["ordinal"])


def _dkp(conn, raid_id: int) -> dict:
    totals = {r["reason"]: {"n": r["n"], "total": round(r["total"] or 0, 1)}
              for r in _rows(conn, "SELECT reason, count(*) AS n, sum(delta) AS total "
                                   "FROM dkp_ledger WHERE raid_id=? GROUP BY reason", (raid_id,))}
    events = []
    # Awards to many at once (a kill, attendance, standby) are one line each; charges one each
    for r in _rows(conn, "SELECT ts, reason, count(*) AS n, sum(delta) AS total, "
                         "min(delta) AS each, NULL AS guid, NULL AS item_id, '' AS note "
                         "FROM dkp_ledger WHERE raid_id=? AND reason IN "
                         "('kill', 'attendance', 'standby') GROUP BY ts, reason "
                         "UNION ALL SELECT ts, reason, 1, delta, delta, guid, item_id, note "
                         "FROM dkp_ledger WHERE raid_id=? AND reason NOT IN "
                         "('kill', 'attendance', 'standby') ORDER BY ts DESC LIMIT ?",
                   (raid_id, raid_id, DKP_EVENTS)):
        note = (r["note"] or "").removeprefix("council:")  # a loot council's row names its receipt too
        item = _meta(conn, note) if note.startswith("guild:loot:") else None
        events.append({"ts": r["ts"], "reason": r["reason"], "n": r["n"],
                       "total": round(r["total"] or 0, 1), "each": round(r["each"] or 0, 1),
                       "guid": r["guid"], "item_id": r["item_id"],
                       "item": item.get("name") if isinstance(item, dict)
                       and isinstance(item.get("name"), str) else None})
    return {"totals": totals, "events": events}


def _mvp(conn, raid_id: int) -> dict | None:
    state = _meta(conn, f"guild:raid:{raid_id}:mvp")
    result = state.get("result") if isinstance(state, dict) else None
    if not isinstance(result, dict) or type(result.get("winner")) is not int:
        return None
    return {"guid": result["winner"], "votes": _num(result.get("votes"))}


def _raid(conn, row, *, detail: bool) -> dict:
    raid_id = row["raid_id"]
    raid = CATALOG.get(row["raid_key"])
    out = {"raid_id": raid_id, "raid": row["raid_key"], "name": raid.name if raid else row["raid_key"],
           "size": row["size"], "heroic": bool(row["heroic"]), "offline": bool(row["offline"]),
           "started": row["started"], "ended": row["ended"], "outcome": row["outcome"],
           "kills": row["kills"], "wipes": row["wipes"], "bosses": raid.bosses if raid else None,
           "lead": row["lead_guid"], "tank": row["tank_guid"], "loot": row["loot_guid"],
           "not_raided": _meta(conn, f"guild:raid:{raid_id}:not-raided") is True}
    attendance = {"raided": [], "benched": []}
    for r in _rows(conn, "SELECT guid, status FROM guild_attendance WHERE raid_id=? ORDER BY guid",
                   (raid_id,)):
        attendance.setdefault(r["status"], []).append(r["guid"])
    if not detail:
        loot = _loot(conn, raid_id)
        out.update(mvp=_mvp(conn, raid_id), raided=len(attendance["raided"]),
                   items=sum(r["state"] == "charged" for r in loot))
        return out
    plan = _meta(conn, f"guild:raid:{raid_id}:plan")
    plan = plan if isinstance(plan, dict) else {}
    members = _guids(plan.get("members")) or attendance["raided"]
    bench = _guids(plan.get("bench")) or attendance["benched"]
    out.update(members=members, bench=bench, human_in_raid=plan.get("human_in_raid") is True,
               instance=_meta(conn, f"guild:raid:{raid_id}:instance") is not None,
               loot_items=_loot(conn, raid_id), dkp=_dkp(conn, raid_id), mvp=_mvp(conn, raid_id))
    return out


def balances(conn) -> dict[int, float]:
    return {r["guid"]: round(r["total"] or 0, 1)
            for r in _rows(conn, "SELECT guid, sum(delta) AS total FROM dkp_ledger GROUP BY guid")}


def raid_view(conn, *, now: float, tz=None) -> dict:
    """The Raid tab: the raid that is open (if any) in detail, the recent ones, tonight's quiet
    night, the raids that can be picked and the officers who can be asked to lead."""
    cast = people(conn)
    open_rows = _rows(conn, "SELECT * FROM guild_raid WHERE ended IS NULL "
                            "ORDER BY started DESC, raid_id DESC LIMIT 1")
    recent = _rows(conn, "SELECT * FROM guild_raid WHERE ended IS NOT NULL "
                         "ORDER BY started DESC, raid_id DESC LIMIT ?", (RECENT,))
    tonight = night_of(datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now))
    return {"human": human_guid(conn),
            "open": _raid(conn, open_rows[0], detail=True) if open_rows else None,
            "recent": [_raid(conn, r, detail=False) for r in recent],
            "quiet_night": _meta(conn, QUIET_NIGHT_KEY) == tonight,
            "balances": {str(g): v for g, v in balances(conn).items()},
            "people": {str(g): p for g, p in cast.items()},
            "catalog": [{"key": r.key, "name": r.name, "sizes": list(r.sizes), "heroic": r.heroic}
                        for r in CATALOG.values()],
            "officers": sorted(({"guid": g, "name": p["name"]} for g, p in cast.items()
                                if p["guild_role"] == "officer"), key=lambda o: o["name"].lower())}


def dkp_stats(conn) -> dict:
    """Attendance and DKP per member over the finished raids, who is due an item (DUE_RAIDS
    finished raids attended since their last item, or ever, and DKP left) and what each of the
    last DKP_RAIDS raids handed out."""
    raids = _rows(conn, "SELECT raid_id, started, raid_key, size, heroic, offline, kills "
                        "FROM guild_raid WHERE ended IS NOT NULL ORDER BY started, raid_id")
    started = {r["raid_id"]: r["started"] for r in raids}
    attended: dict[int, dict[int, str]] = {}
    for r in _rows(conn, "SELECT a.raid_id, a.guid, a.status FROM guild_attendance a "
                         "JOIN guild_raid g ON g.raid_id = a.raid_id WHERE g.ended IS NOT NULL"):
        attended.setdefault(r["guid"], {})[r["raid_id"]] = r["status"]
    ledger = {r["guid"]: r for r in _rows(
        conn, "SELECT guid, sum(delta) AS balance, "
              "sum(CASE WHEN reason IN ('kill', 'attendance', 'standby') THEN delta END) AS earned, "
              "-sum(CASE WHEN reason = 'item' THEN delta END) AS spent, "
              "sum(reason = 'item') AS items FROM dkp_ledger GROUP BY guid")}
    last_item = {}
    for r in _rows(conn, "SELECT l.guid, l.ts, g.started FROM dkp_ledger l "
                         "LEFT JOIN guild_raid g ON g.raid_id = l.raid_id "
                         "WHERE l.reason = 'item' ORDER BY l.ts, l.id"):
        last_item[r["guid"]] = r["started"] if r["started"] is not None else r["ts"]
    members = []
    for guid, seen in sorted(attended.items()):
        first = min(started[i] for i in seen)
        possible = sum(1 for t in started.values() if t >= first)
        raided = sum(1 for v in seen.values() if v == "raided")
        benched = len(seen) - raided
        since = sum(1 for i in seen if i in started and started[i] > last_item.get(guid, float("-inf")))
        row = ledger.get(guid)
        balance = round((row["balance"] if row else 0) or 0, 1)
        members.append({"guid": guid, "raids": raided, "benched": benched, "possible": possible,
                        "attendance": round(100 * (raided + benched) / possible) if possible else None,
                        "earned": round((row["earned"] if row else 0) or 0, 1),
                        "spent": round((row["spent"] if row else 0) or 0, 1),
                        "items": (row["items"] if row else 0) or 0, "since_item": since,
                        "due": since >= DUE_RAIDS and balance > 0, "balance": balance})
    due = sorted((m for m in members if m["due"]), key=lambda m: (-m["balance"], m["guid"]))[:DUE_MAX]
    for m in members:
        del m["balance"]
    handed = {r["raid_id"]: r for r in _rows(
        conn, "SELECT raid_id, "
              "sum(CASE WHEN reason IN ('kill', 'attendance', 'standby') AND delta > 0 THEN delta END) AS awarded, "
              "-sum(CASE WHEN reason = 'item' THEN delta END) AS spent, sum(reason = 'item') AS items, "
              "count(DISTINCT CASE WHEN delta > 0 THEN guid END) AS paid "
              "FROM dkp_ledger WHERE raid_id IS NOT NULL GROUP BY raid_id")}
    out = []
    for r in reversed(raids[-DKP_RAIDS:]):
        h = handed.get(r["raid_id"])
        out.append({"raid_id": r["raid_id"], "started": r["started"], "raid": r["raid_key"],
                    "size": r["size"], "heroic": bool(r["heroic"]), "offline": bool(r["offline"]),
                    "kills": r["kills"], "awarded": round((h["awarded"] if h else 0) or 0, 1),
                    "spent": round((h["spent"] if h else 0) or 0, 1),
                    "items": (h["items"] if h else 0) or 0, "paid": (h["paid"] if h else 0) or 0})
    return {"members": members, "raids": out, "due": [m["guid"] for m in due]}


def guild_view(conn) -> dict:
    """The Guild tab: the roster, the DKP ladder and its totals, loot history, season awards
    and the chronicle."""
    cast = people(conn)
    ladder = {}
    for r in _rows(conn, "SELECT guid, sum(delta) AS balance, "
                         "sum(CASE WHEN reason IN ('kill', 'attendance', 'standby') THEN delta END) AS earned, "
                         "-sum(CASE WHEN reason = 'item' THEN delta END) AS spent, "
                         "sum(CASE WHEN reason = 'decay' THEN delta END) AS decay, "
                         "sum(CASE WHEN reason = 'adjust' THEN delta END) AS adjust, "
                         "sum(reason = 'item') AS items, max(ts) AS last "
                         "FROM dkp_ledger GROUP BY guid"):
        ladder[r["guid"]] = {"guid": r["guid"], "balance": round(r["balance"] or 0, 1),
                             "earned": round(r["earned"] or 0, 1), "spent": round(r["spent"] or 0, 1),
                             "decay": round(r["decay"] or 0, 1), "adjust": round(r["adjust"] or 0, 1),
                             "items": r["items"] or 0, "last": r["last"], "raids": 0, "benched": 0}
    for r in _rows(conn, "SELECT guid, sum(status = 'raided') AS raids, "
                         "sum(status = 'benched') AS benched FROM guild_attendance GROUP BY guid"):
        entry = ladder.setdefault(r["guid"], {"guid": r["guid"], "balance": 0.0, "earned": 0.0,
                                              "spent": 0.0, "decay": 0.0, "adjust": 0.0,
                                              "items": 0, "last": None})
        entry["raids"], entry["benched"] = r["raids"] or 0, r["benched"] or 0
    ranked = sorted(ladder.values(), key=lambda e: (-e["balance"], e["guid"]))
    totals = _rows(conn, "SELECT count(*) AS raids, sum(offline) AS offline, sum(kills) AS kills, "
                         "sum(wipes) AS wipes, min(started) AS first FROM guild_raid g "
                         "WHERE ended IS NOT NULL AND NOT EXISTS (SELECT 1 FROM meta m "
                         "WHERE m.key = 'guild:raid:' || g.raid_id || ':not-raided' AND m.value = 'true')")
    dkp = _rows(conn, "SELECT sum(CASE WHEN delta > 0 THEN delta END) AS awarded, "
                      "-sum(CASE WHEN reason = 'item' THEN delta END) AS spent, "
                      "sum(reason = 'item') AS items, "
                      "-sum(CASE WHEN reason = 'decay' THEN delta END) AS decayed FROM dkp_ledger")
    t, d = (totals[0] if totals else None), (dkp[0] if dkp else None)
    stats = {"raids": (t["raids"] if t else 0) or 0, "offline": (t["offline"] if t else 0) or 0,
             "kills": (t["kills"] if t else 0) or 0, "wipes": (t["wipes"] if t else 0) or 0,
             "first": t["first"] if t else None,
             "awarded": round((d["awarded"] if d else 0) or 0, 1),
             "spent": round((d["spent"] if d else 0) or 0, 1),
             "decayed": round((d["decayed"] if d else 0) or 0, 1),
             "items": (d["items"] if d else 0) or 0}
    loot = []
    for r in _rows(conn, "SELECT l.ts, l.guid, l.delta, l.item_id, l.raid_id, l.note, "
                         "g.raid_key, g.size, g.heroic FROM dkp_ledger l "
                         "LEFT JOIN guild_raid g ON g.raid_id = l.raid_id "
                         "WHERE l.reason = 'item' ORDER BY l.ts DESC, l.id DESC LIMIT ?",
                   (LOOT_HISTORY,)):
        note = (r["note"] or "").removeprefix("council:")  # a loot council's row names its receipt too
        item = _meta(conn, note) if note.startswith("guild:loot:") else None
        loot.append({"ts": r["ts"], "guid": r["guid"], "paid": round(-(r["delta"] or 0), 1),
                     "item_id": r["item_id"], "raid_id": r["raid_id"], "raid": r["raid_key"],
                     "size": r["size"], "heroic": bool(r["heroic"]),
                     "item": item.get("name") if isinstance(item, dict)
                     and isinstance(item.get("name"), str) else None})
    awards = [{"season": r["season"], "title": r["title"], "guid": r["guid"], "value": r["value"]}
              for r in _rows(conn, "SELECT season, title, guid, value FROM award "
                                   "ORDER BY season DESC, id")]
    chronicle = [{"week": r["week"], "era": r["era"], "author": r["author"], "text": r["text"],
                  "created": r["created"]}
                 for r in _rows(conn, "SELECT week, era, author, text, created FROM chronicle "
                                      "ORDER BY week DESC LIMIT ?", (CHRONICLE,))]
    return {"human": human_guid(conn), "people": {str(g): p for g, p in cast.items()},
            "roster": sorted(cast.values(), key=lambda p: (p["rank"], p["name"].lower())),
            "ladder": ranked, "stats": stats, "loot": loot, "awards": awards,
            "chronicle": chronicle, "dkp_stats": dkp_stats(conn)}
