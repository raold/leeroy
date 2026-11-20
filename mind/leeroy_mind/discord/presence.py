"""When a cast member shows as online in the guild Discord.

With `[life] availability` above 0 (life/config.py's pin) her state comes from her day plan
(life.presence: asleep offline, at work, commuting, at the gym or out idle, or do not disturb at
work on her dnd day, otherwise online), she is playing only in a gaming block, and her status line
names her block (none while she is free or gaming: just online); at 0 every answer is today's
clock, except that a member playing WoW takes none of the clock's evening phrases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from ..life import config as life_config
from ..life.day import Block
from ..life.presence import discord_state, presence_at
from ..personas.schema import Persona

OWNER_ZONE = ZoneInfo("America/New_York")
UK_ZONE = ZoneInfo("Europe/London")
RNG_SEED = 0  # the seed the mind and the console both pass, so they agree on dnd days
STATUS_SHARE = 0.6
STATUS_PHRASES = {
    'work': ('at work', 'in meetings', 'working', 'lunch break'),
    'commute': ('commuting', 'on my way home'),
    'evening': ('making dinner', 'at the gym', 'out for a walk', 'chilling'),
    'late': ("can't sleep", 'up late'),
    'home': ("kids' bedtime", 'family time', 'making dinner'),
    'gym': ('at the gym',),
    'out': ('out tonight',),
}
# her day's blocks with a status of their own ([life] availability above 0). Free and gaming have
# none: she is just online, never the clock's "at the gym" or "out for a walk" while her plan
# has her home or at her PC (review-340 P2-1)
LIFE_STATUS = {Block.WORK: 'work', Block.COMMUTE: 'commute', Block.HOME_DUTY: 'home',
               Block.GYM: 'gym', Block.OUT: 'out'}


@dataclass(frozen=True, slots=True)
class Presence:
    state: Literal["online", "idle", "dnd", "offline"]
    playing: bool
    zone: int


def local_time(owner_now: datetime, region: str) -> datetime:
    """Convert an instant to the member's region; a naive owner clock is US Eastern.

    Britain and the US change daylight-saving time on different weekends. Keep naive
    inputs naive for the schedule-only callers, while aware inputs preserve their instant.
    """
    if region != "UK":
        return owner_now
    local = (owner_now if owner_now.tzinfo else owner_now.replace(tzinfo=OWNER_ZONE)).astimezone(UK_ZONE)
    return local if owner_now.tzinfo else local.replace(tzinfo=None)


def her_local(owner_now: datetime, p: Persona, *, enabled: bool | None = None) -> datetime:
    """Her own local time: her home's zone (geography.places.place_of: her state's, her UK
    region's, Britain's) while [geography] is on and the sheet knows where she lives, else
    local_time's region rule (a US member without a home keeps the owner's clock). A naive
    `owner_now` is US Eastern and the answer stays naive, as local_time's. `enabled`: the
    console's reading of the mind's switch (geography.config.stored); None reads the pin."""
    from ..geography import config as geo_config
    from ..geography.places import place_of
    on = geo_config.on() if enabled is None else enabled
    place = place_of(p) if on else None
    if place is None:
        return local_time(owner_now, p.identity.region)
    local = (owner_now if owner_now.tzinfo else owner_now.replace(tzinfo=OWNER_ZONE)).astimezone(place.tz)
    return local if owner_now.tzinfo else local.replace(tzinfo=None)


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
    if life_config.availability() > 0:
        lp = presence_at(p, local, medium="discord")
        state = discord_state(lp, dnd_day=_dnd_day(p.guid, local.date(), rng_seed))
        return Presence(state, state == "online" and body_online and lp.block is Block.GAMING,
                        body_zone)
    state = _state(p, local, rng_seed)
    return Presence(state, state == "online" and body_online, body_zone)


def _clock_block(local: datetime, mins: int) -> str | None:
    return ('work' if local.weekday() < 5 and 540 <= mins < 1020 else
            'commute' if 420 <= mins < 510 or 1020 <= mins < 1080 else
            'evening' if 1080 <= mins < 1320 else 'late' if mins >= 1320 or mins < 180 else None)


def status_text(p: Persona, local: datetime, presence: Presence, *, rng_seed: int) -> str | None:
    if presence.state == 'offline':
        return None
    mins = local.hour * 60 + local.minute
    block = _clock_block(local, mins)
    if life_config.availability() > 0:
        # her day's block; playing or free she is at home or at her keyboard: no status
        day = Block.GAMING if presence.playing else presence_at(p, local, medium="discord").block
        block = LIFE_STATUS.get(day)
    elif presence.playing and block == 'evening':
        block = None  # playing WoW: never "out for a walk" or "at the gym" (review-340 P2-1)
    if block is None:
        return None
    # The share is stable for the day; the phrase changes with her current block.
    key = f'{rng_seed}:{p.guid}:{local.date().isoformat()}'
    if int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'big') / 2**32 >= STATUS_SHARE:
        return None
    phrases = STATUS_PHRASES[block]
    if block == 'work' and not 720 <= mins < 780:
        phrases = phrases[:-1]
    draw = int.from_bytes(hashlib.sha256(f'{key}:{block}'.encode()).digest()[:4], 'big')
    return phrases[draw % len(phrases)]
