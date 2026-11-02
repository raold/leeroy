"""What the mind has told the service about the rooms: the tree (`channels`) and who sits where
(`moved`), with the members' names from their `voice` messages. Plain data, on the event loop."""
from .wire import Channels, Moved, Room


class Tree:
    """The rooms by id, in the mind's order; each `channels` replaces them all."""

    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def apply(self, msg: Channels) -> None:
        self.rooms = {room.id: room for room in msg.channels}


class Occupancy:
    """Who is in which room. `human` is his guid (from `session`), `where` guid -> room with
    nobody off comms in it, `names` guid -> name."""

    def __init__(self):
        self.human = 0
        self.where: dict[int, str] = {}
        self.names: dict[int, str] = {}

    def apply(self, msg: Moved) -> None:
        if msg.channel:
            self.where[msg.guid] = msg.channel
        else:
            self.where.pop(msg.guid, None)

    def room_of(self, guid: int) -> str:
        """The member's room; "" when off comms."""
        return self.where.get(guid, "")

    @property
    def human_room(self) -> str:
        return self.room_of(self.human)

    def in_room(self, room: str) -> set[int]:
        return {guid for guid, where in self.where.items() if where == room}

    def clear(self) -> None:
        """A new mind's snapshot follows: nobody is anywhere. The names stay until it sends its
        own."""
        self.where.clear()
