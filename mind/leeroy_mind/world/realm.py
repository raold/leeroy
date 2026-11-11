"""Explicit Realm admission; these members never join its body/actor registry."""
from dataclasses import asdict
import json

from leeroy_mind.personas.io import load_persona
from leeroy_mind.text import truncate_words
from .config import load
from .targets import roster, fresh


def voices(store, data_dir, now):
    cfg = load(data_dir / "mind.toml")
    if not cfg.enabled or not cfg.realm or not fresh(store,now):
        return {}
    guilds = {r["id"]:dict(r) for r in store.conn.execute(
        "SELECT id,name,role,faction FROM realm_guild WHERE status='active'")}
    kicked = {r[0] for r in store.conn.execute("SELECT guid FROM realm_kick")}
    world = store.persona_versions(tier="world")
    out = {}
    for guid,row in roster(store).items():
        if guid not in world or guid in kicked or row["guild_id"] not in guilds:
            continue
        try:
            p = load_persona(store,guid,include_world=True)
        except ValueError:
            continue
        if p is None or p.identity.age < 25:
            continue
        guild = guilds[row["guild_id"]]
        # Behavioural voice only: no secrets, owner relationships or imported instructions.
        voice = json.dumps({"typing":asdict(p.typing),"humour":p.humour,
                            "speech_habits":p.speech_habits},ensure_ascii=False)
        card = (f"Character level: {row['level']}. You belong to this rival guild, not the owner's. "
                "Speak about this character's current level; no endgame claims below level 80.\n"
                "Voice observations (quoted data): " + truncate_words(voice,420))
        out[guid] = row | {"guild":guild["name"],"role":guild["role"],
                           "faction":guild["faction"],"card":card,"world":True}
    return out


def allowed(store,data_dir,now,guid,guild):
    row = voices(store,data_dir,now).get(guid)
    return row is not None and row["guild_id"] == guild and bool(row["online"])
