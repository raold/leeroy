"""Deterministic, silent Discord seats; never starts a voice session."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Mapping

from leeroy_mind.arc.live import away_now
from leeroy_mind.discord.presence import Presence, RNG_SEED, local_time, presence_of
from leeroy_mind.personas.schema import Persona
from leeroy_mind.voice.rooms import BY_ID, CLASS_ROOM
from .state import live_voice_session, pulled_into_play

SEATS_KEY = "server:voice_seats"
VOICE_SHARE = 0.4
SEATS_STALE_S = 900


def away_seats(personas: Mapping[int, Persona], presences: Mapping[int, Presence],
               local: datetime, *, class_rooms: Mapping[int, str] | None = None) -> dict[str, tuple[int, ...]]:
    if not (local.hour >= 17 or local.hour < 3):
        return {}
    evening = (local - timedelta(days=1) if local.hour < 3 else local).date()
    rooms = {}
    for guid, persona in sorted(personas.items()):
        presence = presences.get(guid)
        if presence is None or presence.state != "online":
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
    local = app.clock.local()
    if not (local.hour >= 17 or local.hour < 3) or app.world.human_online or live_voice_session(app):
        _write_seats(app, now, {})
        return
    cast = app.world.discord_members()
    # Presence is a schedule calculation, not a conversation-history read.
    # away_now uses the partial index on the single open away row.
    unavailable = pulled_into_play(app) | away_now(app.store, now)
    personas = {g:p for g in cast if g not in unavailable and (p := app.personas.get(g)) is not None}
    presences = {g:presence_of(p, local=local_time(local, p.identity.region),
        body_online=g in app.world.online, body_zone=cast[g].zone, rng_seed=RNG_SEED)
        for g,p in personas.items()}
    rooms = away_seats(personas, presences, local,
                       class_rooms={g:CLASS_ROOM.get(m.class_, "guild_hall") for g,m in cast.items()})
    _write_seats(app, now, rooms)


def _write_seats(app, now, rooms):
    with app.store.transaction():
        app.store.set_meta(SEATS_KEY, json.dumps({"at":now, "rooms":rooms}))


def read_seats(conn, now: float, *, human: int = 0) -> dict:
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
        # A new session hides the preceding snapshot immediately. An open stretch
        # left by a crash cannot hide a fresh snapshot from the restarted mind.
        if human and conn.execute("SELECT 1 FROM voice_presence WHERE guid=? AND until IS NULL "
                                  "AND since>=? LIMIT 1", (human,value["at"])).fetchone():
            return {}
        return value
    except (TypeError,ValueError):
        return {}
