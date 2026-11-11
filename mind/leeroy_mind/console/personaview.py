"""The Personas tab's read-only questions to mind.db (asked through feed.read, so on a read-only
connection that is closed again). The mind writes these rows; the console only reads them:

- cast_member (the roster) and persona_core: each stored persona (personas.io.save_persona),
  its version (1, then one more per write that changed it), when it was saved and its source:
  seed (`personas seed`), owner (`personas import`, `rebalance-stances`, `fix-typing`) or
  variety (`selfies variety`), or generated, which three writers share: `personas generate`; the night's looks pass, which
  commits the persona together with its own night_step unit (LOOKS_STAGE, <guid>); and a
  stance move (nightly.attraction, live or at night), saved at the instant of its stance_band
  ledger row, or of the undo row when the owner undid one. `by` names the writer from those
  rows, and a generated write that matches neither is the generator's;
- pad_mood: the night's mood stage (nightly.mood), pleasure, arousal and dominance from -1 to
  1, named by their octant; guild_mood: the guild's mood per night and its reasons;
- relationship: how a member sees another (Store.put_relationship, note_interaction). A
  member's ties are its rows that hold a score or a label, the strongest feeling (|affinity|)
  first, then the best known (familiarity); its row toward the owner is shown apart;
- memory: active memories per owner and kind, all and those stored in the last RECENT_DAYS
  days (counts only, never their text);
- persona_ledger (nightly.store.ledger_add): the night's growth (note_added, note_dropped,
  note_rolled_back), its critic's drift_flag rows, stance moves (stance_band) and undo rows.
  An open drift flag is one from the last RECENT_DAYS days that is neither undone nor resolved
  in review_queue, as the critic counts them;
- trait_state (nightly.motives): the Big Five the night sampled for each member around their
  persona's own scores. The night's stages read it; the live talk does not.

Persona content is read through the persona loader (schema.persona_from_dict); of a persona
only its card summary is shown, never its secret, sample lines, bits, interview, habits, real
life or real name. A part whose table this mind.db lacks (an older schema) is None and the
table is listed in `missing`; a part with no rows yet is empty or None, never zeros."""
import json

from leeroy_mind.nightly.enrich import STAGE as LOOKS_STAGE  # the looks pass's stage
from leeroy_mind.nightly.mood import octant
from leeroy_mind.nightly.motives import TRAITS
from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict
from leeroy_mind.selfies.kind import STAGE as PHOTO_STAGE  # the photo-kind pass's stage

from .feed import guild_mood, human_guid
from .raidview import people

RECENT_DAYS = 7
TIES = 3            # ties in the roster
DETAIL_TIES = 40    # ties in a member's detail
LEDGER = 25         # ledger rows in a member's detail
LOOKS_S = 60.0      # a looks pass saves its persona this soon after its unit's time, at most
TABLES = ("cast_member", "persona_core", "pad_mood", "guild_mood", "relationship", "memory",
          "persona_ledger", "trait_state")
_SCORED = ("affinity != 0 OR trust != 0 OR respect != 0 OR attraction != 0 "
           "OR familiarity != 0 OR label != ''")
_TIE = ("a, b, affinity, trust, respect, attraction, familiarity, label, romance_stage, "
        "interactions, last_seen")


def _have(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _persona(js):
    """The stored persona through the loader, or None when it cannot be read (a newer schema)."""
    try:
        return persona_from_dict(json.loads(js))
    except (TypeError, ValueError):  # PersonaError and a JSON error are ValueErrors
        return None


def _writers(conn, have) -> tuple[dict, dict]:
    """((guid, ts) -> stance or undo, guid -> [(its looks or photo pass units' times, stage)]): what placed a
    generated persona write."""
    moves, looks = {}, {}
    if "persona_ledger" in have:
        for r in conn.execute("SELECT guid, ts, kind FROM persona_ledger "
                              "WHERE kind IN ('stance_band', 'undo')"):
            moves[(r["guid"], r["ts"])] = "stance" if r["kind"] == "stance_band" else "undo"
    if "night_step" in have:
        for r in conn.execute("SELECT unit, finished, stage FROM night_step WHERE stage IN (?, ?)",
                              (LOOKS_STAGE, PHOTO_STAGE)):
            if str(r["unit"]).isdigit():
                looks.setdefault(int(r["unit"]), []).append((r["finished"], r["stage"]))
    return moves, looks


def _by(guid, source, updated, moves, looks) -> str:
    if source != "generated":
        return source  # seed, owner or variety (labelled by the console)
    if (guid, updated) in moves:
        return moves[(guid, updated)]
    # the nearest unit before the write: the looks pass's last member and the photo pass's first
    # can finish within LOOKS_S of each other, and row order says nothing
    near = [(t, stage) for t, stage in looks.get(guid, ()) if t <= updated <= t + LOOKS_S]
    if near:
        return PHOTO_STAGE if max(near)[1] == PHOTO_STAGE else "night"
    return "generator"


def _tie(r) -> dict:
    return {"guid": r["b"], "affinity": r["affinity"], "trust": r["trust"],
            "respect": r["respect"], "attraction": r["attraction"],
            "familiarity": r["familiarity"], "label": r["label"],
            "romance_stage": r["romance_stage"], "interactions": r["interactions"],
            "last_seen": r["last_seen"] or None}


def _ties(conn, human: int, top: int, guid: int | None = None) -> tuple[dict, dict]:
    """(guid -> {"top": its strongest `top` ties, "total": how many}, guid -> its row toward
    the owner)."""
    one = "" if guid is None else "AND a = :guid "
    ties: dict[int, dict] = {}
    for r in conn.execute(
            f"SELECT {_TIE}, notes, total FROM (SELECT *, row_number() OVER (PARTITION BY a "
            "ORDER BY abs(affinity) DESC, familiarity DESC, b) AS n, count(*) OVER "
            f"(PARTITION BY a) AS total FROM relationship WHERE b != :human AND a != b {one}"
            f"AND ({_SCORED})) WHERE n <= :top ORDER BY a, n",
            {"human": human, "top": top, "guid": guid}):
        entry = ties.setdefault(r["a"], {"top": [], "total": r["total"]})
        entry["top"].append(dict(_tie(r), notes=r["notes"]) if guid is not None else _tie(r))
    you = {}
    if human:
        for r in conn.execute(f"SELECT {_TIE} FROM relationship WHERE b = :human {one}",
                              {"human": human, "guid": guid}):
            you[r["a"]] = {k: v for k, v in _tie(r).items() if k != "guid"}
    return ties, you


def _moods(conn) -> dict[int, dict]:
    return {r["guid"]: {"p": r["p"], "a": r["a"], "d": r["d"],
                        "octant": octant(r["p"], r["a"], r["d"]), "updated": r["updated"]}
            for r in conn.execute("SELECT guid, p, a, d, updated FROM pad_mood")}


def _memories(conn, since: float) -> dict[int, dict]:
    return {r["owner"]: {"total": r["total"], "recent": r["recent"] or 0}
            for r in conn.execute("SELECT owner, count(*) AS total, sum(t_created >= ?) AS recent "
                                  "FROM memory WHERE status = 'active' GROUP BY owner", (since,))}


def _drift_flags(conn, have, since: float) -> dict[int, int]:
    resolved = ("AND NOT EXISTS (SELECT 1 FROM review_queue q WHERE q.id = l.ref "
                "AND q.resolved IS NOT NULL) " if "review_queue" in have else "")
    return {r["guid"]: r["n"] for r in conn.execute(
        "SELECT l.guid, count(*) AS n FROM persona_ledger l WHERE l.kind = 'drift_flag' "
        "AND l.ts >= ? AND NOT EXISTS (SELECT 1 FROM persona_ledger u WHERE u.kind = 'undo' "
        f"AND u.ref = l.id) {resolved}GROUP BY l.guid", (since,))}


def _traits(conn) -> tuple[str | None, dict[int, dict]]:
    """The latest night the night sampled traits on, and each member's sample that night."""
    r = conn.execute("SELECT night FROM trait_state ORDER BY night DESC LIMIT 1").fetchone()
    if r is None:
        return None, {}
    out: dict[int, dict] = {}
    for t in conn.execute("SELECT guid, trait, value FROM trait_state WHERE night = ?",
                          (r["night"],)):
        if t["trait"] in TRAITS:
            out.setdefault(t["guid"], {})[t["trait"]] = t["value"]
    return r["night"], out


def _drift_traits(night, sample, persona) -> dict | None:
    if not sample:
        return None
    values = {}
    for trait in TRAITS:
        if trait not in sample:
            continue
        base = getattr(persona.big_five, trait).score if persona is not None else None
        values[trait] = {"base": base, "sampled": round(sample[trait], 2),
                         "shift": None if base is None else round(sample[trait] - base, 2)}
    return {"night": night, "values": values}


def _view(conn, *, now: float, guid: int | None = None) -> dict:
    have = _have(conn)
    missing = [t for t in TABLES if t not in have]
    human = human_guid(conn)
    everyone = people(conn) if "cast_member" in have else {}
    cast = everyone if guid is None else {g: p for g, p in everyone.items() if g == guid}
    rows = {}
    if "persona_core" in have:
        sql = "SELECT guid, json, version, source, updated FROM persona_core"
        rows = {r["guid"]: r for r in conn.execute(
            sql + ("" if guid is None else " WHERE guid = ?"), () if guid is None else (guid,))}
    moves, looks = _writers(conn, have) if rows else ({}, {})
    since = now - RECENT_DAYS * 86400
    ties, you = (_ties(conn, human, DETAIL_TIES if guid is not None else TIES, guid)
                 if "relationship" in have else ({}, {}))
    moods = _moods(conn) if "pad_mood" in have else {}
    memories = _memories(conn, since) if "memory" in have else {}
    flags = _drift_flags(conn, have, since) if "persona_ledger" in have else {}
    night, samples = _traits(conn) if "trait_state" in have else (None, {})
    members, personas = {}, {}
    for g in sorted((set(cast) | set(rows)) - {human}):
        row, facts = rows.get(g), cast.get(g)
        p = personas[g] = _persona(row["json"]) if row is not None else None
        f = facts or {"name": p.character_name if p else f"#{g}",  # a persona left the cast
                      "guild_role": p.play.guild_role if p else "",
                      "officer_title": p.play.officer_title if p else ""}
        members[g] = {
            "guid": g, "in_cast": facts is not None,
            **{k: f.get(k) for k in ("name", "class", "level", "rank", "online", "spec", "role",
                                     "guild_role", "officer_title")},
            "persona": None if row is None else {
                "version": row["version"], "source": row["source"], "updated": row["updated"],
                "by": _by(g, row["source"], row["updated"], moves, looks),
                "readable": p is not None},
            "stance": p.romance.stance if p is not None else None,
            "mood": moods.get(g), "you": you.get(g),
            "ties": ties.get(g, {"top": [], "total": 0}) if "relationship" in have else None,
            "memories": (memories.get(g, {"total": 0, "recent": 0}) if "memory" in have
                         else None),
            "drift": None if "persona_ledger" not in have and "trait_state" not in have else {
                "flags": flags.get(g, 0) if "persona_ledger" in have else None,
                "traits": _drift_traits(night, samples.get(g), p)}}
    names = {str(g): p["name"] for g, p in everyone.items()}
    for g, p in personas.items():
        if str(g) not in names and p is not None:
            names[str(g)] = p.character_name
    return {"human": human, "members": members, "personas": personas, "names": names,
            "missing": missing, "have": have, "since": since,
            "guild_mood": guild_mood(conn)}


def roster_view(conn, *, now: float) -> dict:
    """The Personas tab: every cast member and stored persona but the owner, by name, each with
    its mood, stance, row toward the owner, strongest ties, memory counts, persona version and
    drift; the guild's latest mood; and the tables this mind.db lacks."""
    v = _view(conn, now=now)
    return {"human": v["human"], "names": v["names"], "missing": v["missing"],
            "recent_days": RECENT_DAYS, "guild_mood": v["guild_mood"],
            "members": sorted(v["members"].values(), key=lambda m: (m["name"].lower(), m["guid"]))}


def roster_page(view: dict, offset: int, *, limit: int = 50) -> dict:
    """A bounded roster page; optional badge projections follow only these members.

    The detail endpoint retains each member's full data. Preserve the shared name index:
    relationships on this page can refer to members on a different one.
    """
    members = view.get("members", [])
    offset = min(offset, max(0, ((len(members) - 1) // limit) * limit))
    out = dict(view, members=members[offset:offset + limit])
    if "insight_badges" in out:
        guids = {str(m["guid"]) for m in out["members"]}
        out["insight_badges"] = {g: v for g, v in out["insight_badges"].items() if str(g) in guids}
    out["page"] = {"offset": offset, "limit": limit, "total": len(members),
                   "personas": sum(bool(m.get("persona")) for m in members),
                   "more": offset + limit < len(members)}
    return out


def _card(p) -> dict:
    """The persona card summary: who they are, how they type, how they play and their looks."""
    d = persona_to_dict(p)
    return {"identity": {k: d["identity"][k] for k in ("age", "player_gender", "region")},
            "big_five": d["big_five"], "attachment": d["attachment"], "humour": d["humour"],
            "typing": d["typing"], "game": d["game"], "play": d["play"],
            "romance": {k: d["romance"][k] for k in ("stance", "style", "jealousy_mode")},
            "appearance": d["appearance"], "photo": d.get("photo")}


def member_view(conn, guid: int, *, now: float) -> dict | None:
    """One member in detail: the roster's entry, the persona card summary, every tie (the
    strongest DETAIL_TIES), active memories by kind and the newest LEDGER ledger rows. None
    when mind.db knows no such member (or it is the owner)."""
    v = _view(conn, now=now, guid=guid)
    member = v["members"].get(guid)
    if member is None:
        return None
    p, have = v["personas"].get(guid), v["have"]
    kinds = None
    if "memory" in have:
        kinds = {r["kind"]: {"total": r["total"], "recent": r["recent"] or 0}
                 for r in conn.execute("SELECT kind, count(*) AS total, sum(t_created >= ?) "
                                       "AS recent FROM memory WHERE owner = ? AND status = "
                                       "'active' GROUP BY kind", (v["since"], guid))}
    ledger = None
    if "persona_ledger" in have:
        undone = {r["ref"] for r in conn.execute(
            "SELECT ref FROM persona_ledger WHERE guid = ? AND kind = 'undo'", (guid,))}
        ledger = [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "text": r["text"],
                   "undone": r["id"] in undone}
                  for r in conn.execute("SELECT id, ts, kind, text FROM persona_ledger "
                                        "WHERE guid = ? ORDER BY ts DESC, id DESC LIMIT ?",
                                        (guid, LEDGER))]
    return {"human": v["human"], "names": v["names"], "missing": v["missing"],
            "recent_days": RECENT_DAYS, "member": member,
            "card": _card(p) if p is not None else None, "memories": kinds, "ledger": ledger}
