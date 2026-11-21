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
    # Its purpose ([server] channel_topics; discord/topics.py): what it is about and what does
    # not belong there, in the words of the hold line "Stay on what this channel is about
    # (<about>): <not_here>." #general takes anything and has no `not_here`.
    about: str = ''
    not_here: str = ''
    # Its talk rests on something posted there (a picture, a meme, an applicant, a request, a
    # log): no conversation opens on its purpose alone (server/topics.py)
    event_only: bool = False


CATEGORIES = ("info", "general", "raiding", "wow", "officers")
CATEGORY_NAMES = {"info": "INFO", "general": "GENERAL", "raiding": "RAIDING", "wow": "WOW",
                  "officers": "OFFICERS"}

_CLASSES = ((1, "warrior", "Warriors"), (2, "paladin", "Paladins"), (3, "hunter", "Hunters"),
            (4, "rogue", "Rogues"), (5, "priest", "Priests"), (6, "death-knight", "Death Knights"),
            (7, "shaman", "Shamans"), (8, "mage", "Mages"), (9, "warlock", "Warlocks"),
            (11, "druid", "Druids"))

CLASS_CHANNEL: dict[int, str] = {cid: key for cid, key, _ in _CLASSES}
assert set(CLASS_CHANNEL) == set(CLASS_ROOM)


def _c(id_: str, category: str, topic: str, officers_only: bool = False, *, about: str,
       not_here: str = '', event_only: bool = False) -> Channel:
    return Channel(id_, id_, category, topic, officers_only, about, not_here, event_only)


NO_CHAT = "members don't chat here"
NO_LIFE = "leave real life for #irl"


CHANNELS: tuple[Channel, ...] = (
    _c("announcements", "info", "Official news from the officers about the guild.",
       about="the officers' news for the guild", not_here=NO_CHAT, event_only=True),
    _c("rules", "info", "The guild's house rules and what we expect from each other.",
       about="the guild's house rules", not_here=NO_CHAT, event_only=True),
    _c("raid-schedule", "info", "Raid nights, times and which instance is next.",
       about="raid nights and times", not_here=NO_CHAT, event_only=True),
    _c("general", "general", "Anything and everything, for the whole guild.",
       about="anything and everything"),
    _c("irl", "general", "Real life: work, family, travel and how the week went.",
       about="real life: work, family, home and how the week is going",
       not_here="raids, loot and DKP have their own channels"),
    _c("pics", "general", "Screenshots, photos and anything worth looking at.",
       about="the pictures posted here",
       not_here="talk about what's in them, not your day or the raid", event_only=True),
    _c("pets", "general", "Pets, real and in game, with pictures encouraged.",
       about="pets, real and in game", not_here="keep it about the pets"),
    _c("food", "general", "What we are cooking, eating and craving.",
       about="food: what we cook, eat and crave",
       not_here="bring up raids, loot or your day only as they touch food"),
    _c("memes", "general", "Memes and image or text jokes, and laughing at them.",
       about="the memes posted here",
       not_here=("post a meme or react to the one posted in a few words; never plain chat about "
                 "your day, food, the raid or the calendar"), event_only=True),
    _c("raid-planning", "raiding", "Raid lineups, bench and signups for the next raid nights.",
       about="the next raids' lineups, bench and signups", not_here=NO_LIFE),
    _c("strategy", "raiding", "Boss strategy, rotations and theorycrafting.",
       about="boss strategy, rotations and theorycrafting", not_here=NO_LIFE),
    _c("loot-and-dkp", "raiding", "Loot drops, DKP standings and bidding questions.",
       about="loot drops, DKP standings and bids", not_here=NO_LIFE),
    _c("logs", "raiding", "Combat logs and how each pull went.",
       about="combat logs and how the pulls went", not_here=NO_LIFE, event_only=True),
    *(Channel(key, key, "wow", f"Talk for {label}: specs, gear, rotations and class news.",
              about=f"{label} and how they play: specs, gear, rotations and class news",
              not_here=NO_LIFE)
      for _, key, label in _CLASSES),
    _c("pvp-arena", "wow", "Battlegrounds, arena teams and ratings.",
       about="battlegrounds, arena teams and ratings", not_here=NO_LIFE),
    _c("professions-and-gold", "wow", "Crafting, farming, the auction house and making gold.",
       about="crafting, farming, the auction house and gold", not_here=NO_LIFE),
    _c("achievements-and-mounts", "wow", "Achievements, mounts, titles and collections.",
       about="achievements, mounts, titles and collections", not_here=NO_LIFE),
    _c("lfg-and-alts", "wow", "Looking for a group, and talk about alts.",
       about="finding groups, and alts", not_here=NO_LIFE),
    _c("officer-chat", "officers", "Private talk for the officers.", True,
       about="guild business for the officers", not_here="leave real-life chat for #irl"),
    _c("applications", "officers", "New applicants and trial reviews.", True,
       about="applicants and trial members", not_here="nothing else belongs here",
       event_only=True),
    _c("bank-requests", "officers", "Guild bank requests waiting for an officer.", True,
       about="guild bank requests", not_here="nothing else belongs here", event_only=True),
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
