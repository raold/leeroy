"""The mind's view of the guild: who is online, who is grouped, who can see which line."""
import dataclasses
from collections import deque
from dataclasses import dataclass
from typing import Callable

from leeroy_mind.protocol import (AfkEvent, Cast, Chat, Event, Group, Hello, Login, Logout,
                                  Member, Zone)
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.wow import CAST_MIN_LEVEL

BOT_COMMANDS = frozenset({
    "follow", "stay", "wait", "flee", "attack", "grind", "summon", "release", "revive", "loot",
    "ss", "co", "nc", "dc", "do", "reset", "autogear", "maintenance", "tank", "heal", "dps",
    "los", "home", "rti", "formation", "ready", "leave", "invite", "give", "trade", "cast",
    "use", "equip"})
_PREFIXES = ("co ", "nc ", "dc ")
_MAX_COMMAND_TOKENS = 4
_PRONOUNS = frozenset({"you", "u", "we", "i", "ya", "they", "he", "she"})
# An "offline" ack (the bot was not in the world, as between maps on a far teleport) takes a
# member out of `online` for this long, or until any event of hers shows she is back
# (correctness M3, liveops M4); a logout is the only way out for good.
OFFLINE_ACK_S = 60.0
_QUESTION_WORDS = frozenset({"what", "why", "who", "how", "where", "when", "which"})


def looks_like_bot_command(text: str) -> bool:
    """A playerbots command typed by the human: no reply, no thread. A command is short (4
    tokens or fewer), is not a question, and does not read as speech: "do you need heals",
    "wait what" are chat while "wait" and "do pull" are commands."""
    t = text.strip().lower()
    if t.startswith(_PREFIXES):
        return True
    if t.endswith("?"):
        return False
    tokens = t.split()
    if not tokens or tokens[0].strip(",.!?:;") not in BOT_COMMANDS \
            or len(tokens) > _MAX_COMMAND_TOKENS:
        return False
    if len(tokens) > 1:
        second = tokens[1].strip(",.!?:;")
        if second in _QUESTION_WORDS or (tokens[0] == "do" and second in _PRONOUNS):
            return False
    return True


@dataclass(frozen=True, slots=True)
class SeenLine:
    row: ChatLineRow
    audience: frozenset[int]


class WorldView:
    def __init__(self, *, is_officer: Callable[[int], bool]):
        self.is_officer = is_officer
        self.human_guid = 0
        self.members: dict[int, Member] = {}
        self.online: set[int] = set()  # cast guids only; the human is human_online
        self._acked_off: dict[int, float] = {}  # taken out by an "offline" ack, and when
        self.human_online = False
        self.human_last_active = 0.0
        self.human_afk = False
        self.human_zone = 0
        self.group: Group | None = None
        self.afk: set[int] = set()
        self.history: deque[SeenLine] = deque(maxlen=300)
        self.last_spoke: dict[int, float] = {}
        # who sits in a `voice:` channel's room, once the voice extension is attached
        self.room_audience: Callable[[str], frozenset[int]] | None = None

    def cast_guids(self) -> set[int]:
        return {g for g, m in self.members.items()
                if g != self.human_guid and m.level >= CAST_MIN_LEVEL}

    def _touch_member(self, guid: int, **changes) -> None:
        m = self.members.get(guid)
        if m is not None:
            self.members[guid] = dataclasses.replace(m, **changes)

    def mark_offline(self, guid: int, now: float) -> None:
        """The bridge answered "offline" for her: out of `online` until she shows up again or
        OFFLINE_ACK_S passes (refresh)."""
        if guid in self.online:
            self.online.discard(guid)
            self._acked_off[guid] = now

    def _back(self, guid: int) -> None:
        if self._acked_off.pop(guid, None) is not None:
            m = self.members.get(guid)
            if m is not None and m.online and guid in self.cast_guids():
                self.online.add(guid)

    def refresh(self, now: float) -> None:
        """The tick: an "offline" mark older than OFFLINE_ACK_S runs out."""
        for guid, at in list(self._acked_off.items()):
            if now - at >= OFFLINE_ACK_S:
                self._back(guid)

    def set_group(self, ev: Group) -> None:
        """A group event with no leader and no members means the group is gone."""
        self.group = None if ev.leader_guid == 0 and not ev.members else ev

    def apply(self, ev: Event, now: float) -> None:
        """A hello (a new connection) forgets every AFK flag (final fix wave ruling): the
        bridge forgets what it published at each greeting and re-sends only the players who
        are AFK, and a hard worldserver kill sends no logout that would clear them. Any event
        of a member's but her logout ends an "offline" mark (mark_offline)."""
        if isinstance(ev, Hello):
            if ev.human_guid:
                self.human_guid = ev.human_guid
            self.human_online = ev.human_guid != 0
            self.human_afk = False
            self.afk.clear()
            if self.human_online:
                self.human_last_active = now
            return
        if isinstance(ev, Cast):
            self.members = {m.guid: m for m in ev.members}
            cast = self.cast_guids()
            self.online = {g for g in cast if self.members[g].online}
            self._acked_off.clear()
            self.afk &= cast
            h = self.members.get(self.human_guid)
            if h is not None:
                self.human_online = h.online
            return
        if isinstance(ev, Group):
            self.set_group(ev)
            return
        guid = ev.from_guid if isinstance(ev, Chat) else getattr(ev, "guid", None)
        if not guid:
            return
        if isinstance(ev, (Login, Logout)) and ev.human:
            self.human_guid = guid  # the mind often starts before he logs in: hello said 0
        is_human = guid == self.human_guid
        if isinstance(ev, Logout):
            self._acked_off.pop(guid, None)
        elif not is_human:
            self._back(guid)  # any other event of hers: she is in the world
        if isinstance(ev, (Login, Logout)):
            on = isinstance(ev, Login)
            if is_human or ev.human:
                self.human_online = on
                if not on:
                    self.human_afk = False
            elif on:
                self.online.add(guid)
            else:
                self.online.discard(guid)
                self.afk.discard(guid)
            self._touch_member(guid, online=on)
        elif isinstance(ev, AfkEvent):
            if is_human:
                self.human_afk = ev.on
            elif ev.on:
                self.afk.add(guid)
            else:
                self.afk.discard(guid)
        elif isinstance(ev, Zone):
            self._touch_member(guid, zone=ev.zone_id)
            if is_human:
                self.human_zone = ev.zone_id
        elif isinstance(ev, Chat) and is_human:
            self.human_zone = ev.zone
        if is_human:
            self.human_last_active = now

    def in_group(self, guid: int) -> bool:
        g = self.group
        return g is not None and (guid in g.members or guid == g.leader_guid)

    def audience(self, channel: str, *, from_guid: int, to_guid: int = 0,
                 zone: int = 0) -> frozenset[int]:
        """Cast guids that can see a line on this channel right now."""
        cast = self.cast_guids()
        online = self.online & cast
        if channel.startswith("voice:"):
            return self.room_audience(channel) & online if self.room_audience else frozenset()
        if channel == "guild":
            return frozenset(online)
        if channel == "officer":
            return frozenset(g for g in online if self.is_officer(g))
        if channel == "whisper":
            return frozenset({from_guid, to_guid} & cast)
        grouped = {g for g in online if self.in_group(g)}
        if channel in ("party", "raid", "raid_warning"):
            return frozenset(grouped)
        if channel in ("say", "yell"):
            return frozenset(g for g in grouped if self.members[g].zone == zone)
        return frozenset()

    def add_line(self, row: ChatLineRow, now: float) -> SeenLine:
        seen = SeenLine(row, self.audience(
            row.channel, from_guid=row.from_guid, to_guid=row.to_guid, zone=row.zone))
        self.history.append(seen)
        if row.from_guid:
            self.last_spoke[row.from_guid] = now
        if row.from_guid == self.human_guid:
            self.human_last_active = now
            self.human_zone = row.zone
        return seen

    def visible_to(self, seen: SeenLine, guid: int) -> bool:
        return guid in seen.audience

    def recent_visible(self, guid: int, n: int = 8, channel: str | None = None,
                       keep: Callable[[ChatLineRow], bool] | None = None) -> list[ChatLineRow]:
        """The last n lines `guid` could see, oldest first; with `keep`, the last n of those
        it accepts."""
        out: list[ChatLineRow] = []
        for seen in reversed(self.history):
            if len(out) >= n:
                break
            if guid in seen.audience and (channel is None or seen.row.channel == channel) and (
                    keep is None or keep(seen.row)):
                out.append(seen.row)
        out.reverse()
        return out

    def human_idle(self, now: float, idle_s: float = 600) -> bool:
        return self.human_afk or now - self.human_last_active > idle_s
