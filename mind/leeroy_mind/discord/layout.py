"""The guild Discord's fixed channels, roles and who may see what."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..personas.schema import Persona
from ..protocol import Member
from ..voice.rooms import CLASS_ROOM


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    name: str
    category: str
    topic: str
    officers_only: bool = False


CATEGORIES = ("info", "general", "raiding", "wow", "officers")
CATEGORY_NAMES = {"info": "INFO", "general": "GENERAL", "raiding": "RAIDING", "wow": "WOW",
                  "officers": "OFFICERS"}

_CLASSES = ((1, "warrior", "Warriors"), (2, "paladin", "Paladins"), (3, "hunter", "Hunters"),
            (4, "rogue", "Rogues"), (5, "priest", "Priests"), (6, "death-knight", "Death Knights"),
            (7, "shaman", "Shamans"), (8, "mage", "Mages"), (9, "warlock", "Warlocks"),
            (11, "druid", "Druids"))

CLASS_CHANNEL: dict[int, str] = {cid: key for cid, key, _ in _CLASSES}
assert set(CLASS_CHANNEL) == set(CLASS_ROOM)


def _c(id_: str, category: str, topic: str, officers_only: bool = False) -> Channel:
    return Channel(id_, id_, category, topic, officers_only)


CHANNELS: tuple[Channel, ...] = (
    _c("announcements", "info", "Official news from the officers about the guild."),
    _c("rules", "info", "The guild's house rules and what we expect from each other."),
    _c("raid-schedule", "info", "Raid nights, times and which instance is next."),
    _c("general", "general", "Anything and everything, for the whole guild."),
    _c("irl", "general", "Real life: work, family, travel and how the week went."),
    _c("pics", "general", "Screenshots, photos and anything worth looking at."),
    _c("pets", "general", "Pets, real and in game, with pictures encouraged."),
    _c("food", "general", "What we are cooking, eating and craving."),
    _c("memes", "general", "Jokes, memes and silliness."),
    _c("raid-planning", "raiding", "Raid lineups, bench and signups for the next raid nights."),
    _c("strategy", "raiding", "Boss strategy, rotations and theorycrafting."),
    _c("loot-and-dkp", "raiding", "Loot drops, DKP standings and bidding questions."),
    _c("logs", "raiding", "Combat logs and how each pull went."),
    *(Channel(key, key, "wow", f"Talk for {label}: specs, gear, rotations and class news.")
      for _, key, label in _CLASSES),
    _c("pvp-arena", "wow", "Battlegrounds, arena teams and ratings."),
    _c("professions-and-gold", "wow", "Crafting, farming, the auction house and making gold."),
    _c("achievements-and-mounts", "wow", "Achievements, mounts, titles and collections."),
    _c("lfg-and-alts", "wow", "Looking for a group, and talk about alts."),
    _c("officer-chat", "officers", "Private talk for the officers.", True),
    _c("applications", "officers", "New applicants and trial reviews.", True),
    _c("bank-requests", "officers", "Guild bank requests waiting for an officer.", True),
)

_BY_ID = {c.id: c for c in CHANNELS}
assert len(_BY_ID) == len(CHANNELS)

_DM = "dm:"


def channel(id: str) -> Channel | None:
    return _BY_ID.get(id)


def is_dm(id: str) -> bool:
    return dm_peer(id) is not None


def dm_peer(id: str) -> int | None:
    if not id.startswith(_DM):
        return None
    tail = id[len(_DM):]
    return int(tail) if tail.isascii() and tail.isdigit() else None


def audience(channel_id: str, *, cast: Iterable[int], officers: set[int],
             human: int) -> frozenset[int]:
    """Who reads a channel: a DM its member and him, an officers' channel the officers and him,
    any other the whole cast and him, and a channel the layout lacks nobody (fail closed, as
    channels.is_restricted)."""
    peer = dm_peer(channel_id)
    if peer is not None:
        return frozenset({peer, human})
    ch = channel(channel_id)
    if ch is None:
        return frozenset()
    if ch.officers_only:
        return frozenset(officers) | {human}
    return frozenset(cast) | {human}


def role_of(member: Member, persona: Persona | None, *, trial_rank: int = 4) -> str:
    if member.rank == trial_rank:
        return "trial"
    if persona is None:
        return "social"
    return persona.play.guild_role


def class_key(class_id: int) -> str:
    return CLASS_CHANNEL[class_id]
