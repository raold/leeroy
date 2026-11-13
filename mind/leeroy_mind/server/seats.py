"""Deterministic, silent Discord seats; never starts a voice session."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Mapping

from leeroy_mind.personas.schema import Persona
from leeroy_mind.voice.rooms import BY_ID, CLASS_ROOM
from .state import DiscordState, state_of

SEATS_KEY = "server:voice_seats"
VOICE_SHARE = 0.4
SEATS_STALE_S = 900


def away_seats(personas: Mapping[int, Persona], states: Mapping[int, DiscordState],
               local: datetime, *, class_rooms: Mapping[int, str] | None = None) -> dict[str, tuple[int, ...]]:
    if not (local.hour >= 17 or local.hour < 3):
        return {}
    evening = (local - timedelta(days=1) if local.hour < 3 else local).date()
    rooms = {}
    for guid, persona in sorted(personas.items()):
        state = states.get(guid)
        if state is None or state.presence.state != "online":
            continue
        share = int.from_bytes(sha256(f"{guid}:{evening}".encode()).digest()[:8], "big") / 2**64
        if share >= VOICE_SHARE:
            continue
        kind = persona.game.thinks_as
        social = "kitchen" if 17 <= local.hour < 20 else "irl" if local.hour >= 23 or local.hour < 3 else "guild_hall"
        room = {"lore":"lore", "collector":"achievements", "competitor":"pvp",
                "social":social, "theorycrafter":(class_rooms or {}).get(guid, "guild_hall")}[kind]
        rooms.setdefault(room, []).append(guid)
    return {room: tuple(guids) for room, guids in rooms.items()}


def refresh_seats(app, now: float) -> None:
    rooms = {}
    voice = getattr(app, "voice", None)
    if voice is None or voice.rooms.human_room() is None:
        cast = app.world.discord_members()
        personas = {g:p for g in cast if (p := app.personas.get(g)) is not None}
        states = {g:state_of(app, g, now) for g in personas}
        rooms = away_seats(personas, states, app.clock.local(),
                           class_rooms={g:CLASS_ROOM.get(m.class_, "guild_hall") for g,m in cast.items()})
    with app.store.transaction():
        app.store.set_meta(SEATS_KEY, json.dumps({"at":now, "rooms":rooms}))


def read_seats(conn, now: float, *, human: int = 0) -> dict:
    # Hide a still-fresh away snapshot immediately when his actual session begins.
    if human and conn.execute("SELECT 1 FROM voice_presence WHERE guid=? AND until IS NULL LIMIT 1", (human,)).fetchone():
        return {}
    row = conn.execute("SELECT value FROM meta WHERE key=?", (SEATS_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        if (not isinstance(value, dict) or type(value.get("at")) not in (float,int)
                or not 0 <= now-value["at"] <= SEATS_STALE_S or not isinstance(value.get("rooms"),dict)):
            return {}
        rooms = value["rooms"]
        if any(r not in BY_ID or not isinstance(gs,list) or any(type(g) is not int or g <= 0 for g in gs)
               for r,gs in rooms.items()):
            return {}
        return value
    except (TypeError,ValueError):
        return {}
