"""The Tuning page's Heat panel, read from mind.db read-only and mind.toml (asked through
feed.read). The running mind writes the rows through its tune op; the console only reads.

Everything the panel says a value means comes from leeroy_mind.heat itself (explain_row,
what_changes, explain, resolve), the functions the mind's own code reads, so the page cannot
drift from the behaviour. The owner's overlay files are listed by name, variant count and size,
never their text; no persona text is read out either, only each member's resolved record."""
from pathlib import Path

from leeroy_mind import heat as H
from leeroy_mind.heat import overlays, photos, session
from leeroy_mind.heat.config import HeatConfig
from leeroy_mind.heat.live import resolve_for, world_of
from leeroy_mind.heat.table import HEAT_HI, HEAT_LO, OFFSET_HI, OFFSET_LO, ROWS, TODAY
from leeroy_mind.tuning import groups as G
from leeroy_mind.tuning.dials import HEAT_MOVES, HEAT_WHEN
from leeroy_mind.tuning.state import TABLES, Tuning

from .feed import human_guid
from .tuneview import _cast, _have, _personas

OFF_NOTE = ("Heat is off ([heat] enabled = false in mind.toml): the romance dial is in force, "
            "exactly as before. Turn it on and restart the mind; heat 9 is today's behaviour.")


def heat_view(conn, *, config: HeatConfig, data_dir: Path, romance_file: int | None = None,
              now: float = 0.0) -> dict:
    """`config`: [heat] as mind.toml has it now. `romance_file`: mind.toml's own [romance] dial
    (None when the file could not be read: 9, the documented default of a fresh heat, is not
    assumed; the panel then says the world value is unknown until one is set)."""
    have = _have(conn)
    out = {"enabled": config.enabled, "public_cap": config.public_cap, "available": True,
           "note": "" if config.enabled else OFF_NOTE, "lo": HEAT_LO, "hi": HEAT_HI,
           "offset_lo": OFFSET_LO, "offset_hi": OFFSET_HI, "today": TODAY, "when": HEAT_WHEN,
           "moves": HEAT_MOVES, "rows": [H.explain_row(h) for h in range(HEAT_LO, HEAT_HI + 1)],
           "world": None, "set": False, "follows": None, "changes": [], "groups": [],
           "members": [], "sessions": {"all": None, "members": {}}, "photos": None,
           "overlays": overlays.listing(data_dir)}
    if any(t not in have for t in TABLES):
        out["available"] = False
        return out
    t = Tuning.load(conn, now=now)
    dial = t.world.get("romance", romance_file)
    if t.heat_world is None and dial is None:
        return out
    world, legacy = world_of(t, dial if dial is not None else TODAY)
    out.update(world=world, set=t.heat_world is not None, follows=legacy,
               changes=[H.what_changes(world, h) for h in range(HEAT_LO, HEAT_HI + 1)],
               photos=photos.listing(ROWS[world].photo),
               sessions=session.read(conn, now))
    if "cast_member" not in have or "persona_core" not in have:
        return out
    human = human_guid(conn) or t.human
    cast, personas = _cast(conn), _personas(conn)
    for target, grp in t.groups.items():
        if target in t.heat_groups:
            out["groups"].append({"target": target, "name": grp.name,
                                  "describe": G.describe(grp.where),
                                  "offset": t.heat_groups[target]})
    for g, (p, _version) in personas.items():
        if g not in cast or g == human or p.identity.player_gender != "female":
            continue
        r = resolve_for(t.apply(p, cast.get(g)), t, dial if dial is not None else TODAY, config,
                        session=session.mode_in(out["sessions"], g), cast_member=cast.get(g))
        out["members"].append({
            "guid": g, "name": cast[g].name, "applies": r.applies, "heat": r.heat,
            "offset": r.offset, "pinned": t.heat_pins.get(g), "groups": r.groups,
            "own_dial": p.romance.dial if r.applies else None, "tier": r.tier,
            "voice": r.voice, "photo": r.photo, "session": r.session, **H.explain(r)})
    return out
