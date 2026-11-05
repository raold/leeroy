"""The Personas tab's tuning, read from mind.db read-only (asked through feed.read). The running
mind writes these rows through its control endpoint's tune op; the console only reads them:

- tuning_value, tuning_group, tuning_change and tuning_row (leeroy_mind.tuning): the values in
  force at world, group and member level, the saved groups and the change log;
- persona_core and cast_member: each member's own value of every dial (the persona as written),
  the value in force (the owner's own value for the member, else the persona's plus every group
  offset that reaches them, clamped), and whether the persona changed since a value of the
  owner's own was set. Only dial numbers are read out of a persona, never its text.

World dials show mind.toml's value (read from the file, read-only), the override and the value
in force. The dials with nothing behind them yet are not in the registry, so never here. A
mind.db without the tuning tables (an older schema) says so in `missing`."""
import json

from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.protocol import Member
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import groups as G
from leeroy_mind.tuning.dials import (MEMBER_DIALS, WORLD_DIALS, applies, base_value, bounds,
                                      not_applicable)
from leeroy_mind.tuning.state import TABLES, Tuning

from .feed import human_guid

HISTORY = 30


def _have(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _personas(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT guid, json, version FROM persona_core ORDER BY guid"):
        try:
            out[r[0]] = (persona_from_dict(json.loads(r[1])), r[2])
        except (ValueError, TypeError):
            continue  # a persona this console cannot read: no dials for it here
    return out


def _cast(conn) -> dict:
    cols = ("guid", "name", "race", "class", "gender", "level", "rank", "online", "map", "zone",
            "spec_tab")
    out = {}
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM cast_member"):
        d = dict(zip(cols, r))
        d["class_"] = d.pop("class")
        d["online"] = bool(d["online"])
        out[d["guid"]] = Member(**d)
    return out


def tuning_view(conn, *, file_values: dict | None = None, now: float = 0.0) -> dict:
    """The tuning as the page shows it. `file_values`: mind.toml's own world values (None when
    it could not be read)."""
    have = _have(conn)
    missing = [t for t in TABLES if t not in have]
    dials = [{"name": d.name, "label": d.label, "kind": d.kind, "who": d.who, "when": d.when,
              "moves": d.moves, "frequency": d.frequency, "wording": d.wording,
              "lo": bounds(d.name)[0], "hi": bounds(d.name)[1]} for d in MEMBER_DIALS.values()]
    fields = {k: list(v[0]) for k, v in G.FIELDS.items()}
    out = {"available": not missing, "missing": missing, "dials": dials, "fields": fields,
           "version": 0, "pinned_members": 0, "world_dials": [], "groups": [], "members": [],
           "history": []}
    t = Tuning.load(conn) if not missing else Tuning(human=human_guid(conn) or 0)
    files = file_values or {}
    out["world_dials"] = [{"name": d.name, "label": d.label, "lo": d.lo, "hi": d.hi,
                           "when": d.when, "moves": d.moves, "file": files.get(d.name),
                           "override": t.world.get(d.name),
                           "value": t.world.get(d.name, files.get(d.name))}
                          for d in WORLD_DIALS.values()]
    if "cast_member" not in have or "persona_core" not in have:
        return out
    human = human_guid(conn) or t.human
    cast, personas = _cast(conn), _personas(conn)
    cast_members = {g: personas[g][0] for g in personas if g in cast and g != human}
    for target, grp in t.groups.items():
        members = [g for g, p in cast_members.items()
                   if grp.builtin or G.matches(grp.where, p, cast.get(g))]
        out["groups"].append({"target": target, "name": grp.name, "builtin": grp.builtin,
                              "where": grp.where, "describe": G.describe(grp.where),
                              "members": len(members),
                              "offsets": dict(t.offsets.get(target, {}))})
    for g, p in cast_members.items():
        mt = t.member(p, cast.get(g))
        pins = t.pins.get(g, {})
        version = personas[g][1]
        row = {}
        for name in MEMBER_DIALS:
            own = pins.get(name)
            set_at = t.pin_versions.get((g, name))
            row[name] = {"base": base_value(name, p), "value": mt.value(name, p), "own": own,
                         "applies": applies(name, p), "note": not_applicable(name, p),
                         "stale": own is not None and set_at is not None and set_at != version}
        out["members"].append({"guid": g, "name": cast[g].name, "core": p.core,
                               "gender": p.identity.player_gender, "dials": row})
    out["version"] = t.version
    out["pinned_members"] = len([g for g in t.pins if g in cast_members])
    if not missing:
        out["history"] = C.history(conn, HISTORY)
    out["names"] = {str(g): cast[g].name for g in cast_members}
    return out
