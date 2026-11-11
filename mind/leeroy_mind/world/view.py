"""Read-only operator counts and bounded console cards, including older databases."""
from collections import Counter
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.storage.store import Store
from leeroy_mind.clock import SystemClock
from leeroy_mind.wow import RACES, CLASSES
from .targets import roster
from .stage import priority, failures

PAGE = 50


def status(conn):
    store = Store(conn,clock=SystemClock())
    rows = roster(store,active=False)
    have = store.persona_versions(tier="world")
    groups = {}
    for g,r in rows.items():
        counts = groups.setdefault(r["guild_id"],Counter())
        counts["targets"] += 1
        counts["retired" if r["retired"] else "generated" if g in have else "pending"] += 1
        if not r["retired"] and g not in have and failures(store,g)[0] >= 3:
            counts["refused"] += 1
    return {"guilds":[{"guild":guild,**{k:c[k] for k in
        ("targets","generated","pending","retired","refused")}} for guild,c in sorted(groups.items())],
        "observed":store.get_meta("world:observed")}


def plan(conn,*,cap=12):
    store = Store(conn,clock=SystemClock())
    rows = roster(store)
    have = store.persona_versions(tier=None)
    return [g for g in priority(store,rows) if g not in have and failures(store,g)[0] < 3][:cap] if rows else []


def cards(conn,*,offset=0,guild=0):
    store = Store(conn,clock=SystemClock())
    rows = roster(store,active=False)
    world = store.persona_versions(tier="world")
    ordered = sorted((r for g,r in rows.items() if g in world and (not guild or r["guild_id"] == guild)),
                     key=lambda r:(r["retired"],r["guild_id"],r["rank"],r["guid"]))
    members = []
    for r in ordered[offset:offset+PAGE]:
        guid = r["guid"]
        try:
            p = load_persona(store,guid,include_world=True)
        except ValueError:
            p = None
        card = "Persona unavailable." if p is None else render_prompt_card(p,
            race=RACES.get(r["race"],""),class_name=CLASSES.get(r["class"],""))
        members.append({k:r[k] for k in ("guid","name","guild_id","guild_name","level","class","rank","retired")} |
                       {"card":f"Character level: {r['level']}.\n"+card,"version":world[guid]})
    return {"members":members,"offset":offset,"total":len(ordered),"page_size":PAGE,
            "next":offset+PAGE if offset+PAGE < len(ordered) else None,"read_only":True}


def face(conn,data_dir,guid):
    """First gated candidate as the world profile face; no implicit owner pick or file write."""
    from leeroy_mind.selfies import files
    from leeroy_mind.selfies.prompt import renderable
    from leeroy_mind.selfies.kind import chosen_style
    from leeroy_mind.console.faces import read_image
    store = Store(conn,clock=SystemClock())
    if guid not in store.persona_versions(tier="world"):
        return None
    try:
        p = load_persona(store,guid,include_world=True)
    except ValueError:
        return None
    if p is None or p.identity.age < 25 or renderable(p,chosen_style(store,p)) is not None:
        return None
    row = conn.execute("SELECT c.set_no,c.idx FROM selfie_candidate c JOIN selfie_set s "
        "ON s.guid=c.guid AND s.set_no=c.set_no WHERE c.guid=? AND s.status='ready' "
        "ORDER BY c.set_no DESC,c.idx LIMIT 1",(guid,)).fetchone()
    return read_image(files.candidate(data_dir,guid,row[0],row[1],thumb=True),data_dir/"selfies") if row else None
