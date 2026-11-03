"""The voice rooms: the owner's tree, and who sits where. Pure state; the extension decides
when anyone moves and tells the service."""
from dataclasses import dataclass
from typing import Callable

from leeroy_mind.voice import wire


@dataclass(frozen=True, slots=True)
class RoomDef:
    id: str
    name: str
    parent: str = ""
    capacity: int = 0  # 0: unlimited


_CLASS_ROOMS = (("warriors", "Warriors"), ("paladins", "Paladins"), ("hunters", "Hunters"),
                ("rogues", "Rogues"), ("priests", "Priests"),
                ("death_knights", "Death Knights"), ("shamans", "Shamans"), ("mages", "Mages"),
                ("warlocks", "Warlocks"), ("druids", "Druids"))

ROOMS: tuple[RoomDef, ...] = (
    RoomDef("guild_hall", "Guild Hall"),
    RoomDef("raid", "Raid"),
    RoomDef("raid2", "Raid 2", "raid"),
    RoomDef("officers", "Officers"),
    RoomDef("tanks", "Tanks"),
    RoomDef("healers", "Healers"),
    RoomDef("casters", "Casters"),
    RoomDef("melee", "Melee"),
    RoomDef("class_halls", "Class Halls"),
    *(RoomDef(i, n, "class_halls") for i, n in _CLASS_ROOMS),
    RoomDef("loot_dkp", "Loot & DKP"),
    RoomDef("lore", "Lore & Strategy"),
    RoomDef("pvp", "PvP & Arena"),
    RoomDef("achievements", "Achievements & Mounts"),
    RoomDef("kitchen", "The Kitchen"),
    RoomDef("irl", "IRL"),
    RoomDef("smoke_room", "The Smoke Room"),
    RoomDef("afk", "AFK"),
    RoomDef("private1", "Private 1", capacity=3),
    RoomDef("private2", "Private 2", capacity=3),
    RoomDef("private3", "Private 3", capacity=3),
    RoomDef("interview", "Interview Room"),
    RoomDef("penalty_box", "Penalty Box"),
)
BY_ID = {r.id: r for r in ROOMS}
PRIVATE = ("private1", "private2", "private3")
RAID_ROOMS = ("raid", "raid2")
CLASS_ROOM = {1: "warriors", 2: "paladins", 3: "hunters", 4: "rogues", 5: "priests",
              6: "death_knights", 7: "shamans", 8: "mages", 9: "warlocks", 11: "druids"}
HOME = "guild_hall"


class RoomState:
    def __init__(self, *, officers: Callable[[], set[int]], human: Callable[[], int]):
        self._officers = officers
        self._human = human
        self.where: dict[int, str] = {}  # the human included
        self._by: dict[int, int] = {}
        self._locks: dict[str, frozenset[int]] = {}

    def human_room(self) -> str | None:
        return self.where.get(self._human())

    def occupants(self, room: str) -> frozenset[int]:
        return frozenset(g for g, r in self.where.items() if r == room)

    def allowed(self, room: str) -> frozenset[int] | None:
        """Who may enter a locked room (the Officers room always takes him); None: it is open."""
        if room == "officers":
            return frozenset(self._officers()) | {self._human()}
        return self._locks.get(room)

    def can_enter(self, guid: int, room: str) -> str | None:
        """None when `guid` may sit in `room`, else the `refused` reason."""
        d = BY_ID.get(room)
        if d is None:
            return "unknown"
        if self.where.get(guid) == room:
            return None
        allowed = self.allowed(room)
        if allowed is not None and guid not in allowed:
            return "locked"
        if d.capacity and len(self.occupants(room)) >= d.capacity:
            return "full"
        return None

    def move(self, guid: int, room: str, by: int = 0) -> str | None:
        """Seat `guid` in `room` (callers ask can_enter first); the room left, or None."""
        if room not in BY_ID:
            raise ValueError(f"unknown room {room!r}")
        left = self.where.get(guid)
        self.where[guid] = room
        self._by[guid] = by
        return left

    def leave(self, guid: int) -> None:
        self.where.pop(guid, None)
        self._by.pop(guid, None)

    def lock(self, room: str, guids) -> None:
        self._locks[room] = frozenset(guids)

    def unlock(self, room: str) -> None:
        self._locks.pop(room, None)

    def free_private(self) -> str | None:
        for room in PRIVATE:
            if not self.occupants(room) and room not in self._locks:
                return room
        return None

    def clear(self) -> None:
        self.where.clear()
        self._by.clear()
        self._locks.clear()

    def channels_message(self) -> wire.Channels:
        rooms = []
        for d in ROOMS:
            allowed = self.allowed(d.id)
            rooms.append(wire.Room(d.id, d.name, d.parent, allowed is not None,
                                   tuple(sorted(allowed or ())), d.capacity))
        return wire.Channels(tuple(rooms))

    def moves(self) -> list[wire.Moved]:
        """One per member on comms, by guid, then one for him."""
        human = self._human()
        out = [wire.Moved(g, r, self._by.get(g, 0))
               for g, r in sorted(self.where.items()) if g != human]
        if human in self.where:
            out.append(wire.Moved(human, self.where[human], self._by.get(human, 0)))
        return out
