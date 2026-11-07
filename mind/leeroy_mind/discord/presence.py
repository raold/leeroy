"""When a cast member shows as online in the guild Discord."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ..personas.schema import Persona

UK_AHEAD_HOURS = 5


@dataclass(frozen=True, slots=True)
class Presence:
    state: Literal["online", "idle", "dnd", "offline"]
    playing: bool
    zone: int


def local_time(owner_now: datetime, region: str) -> datetime:
    """The owner's clock (US Eastern) shifted to a member's region."""
    return owner_now + timedelta(hours=UK_AHEAD_HOURS) if region == "UK" else owner_now


def _dnd_day(guid: int, day, rng_seed: int) -> bool:
    h = hashlib.sha256(f"{rng_seed}:{guid}:{day.isoformat()}".encode()).digest()
    return int.from_bytes(h[:4], "big") % 7 == 0


def _state(p: Persona, local: datetime, rng_seed: int) -> str:
    hour, mins = local.hour, local.hour * 60 + local.minute
    if p.real_life.night_owl:
        if hour >= 18 or hour < 3:
            return "online"
        if 5 <= hour < 12:
            return "offline"
        return "idle"
    weekend = local.weekday() >= 5
    if mins < 7 * 60:
        return "offline"
    if mins < 8 * 60 + 30 or 17 * 60 <= mins < 23 * 60 + 30:
        return "online"
    if 9 * 60 <= mins < 17 * 60:
        if weekend:
            return "online"
        return "dnd" if _dnd_day(p.guid, local.date(), rng_seed) else "idle"
    if weekend and mins < 17 * 60:
        return "online"
    return "idle"


def presence_of(p: Persona, *, local: datetime, body_online: bool, body_zone: int,
                rng_seed: int, in_his_group: bool = False) -> Presence:
    if in_his_group:
        return Presence("online", True, body_zone)
    state = _state(p, local, rng_seed)
    return Presence(state, state == "online" and body_online, body_zone)
