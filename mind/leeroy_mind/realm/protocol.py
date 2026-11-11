"""Additive Realm messages for bridge v1. Registration is explicit and default off.

These snapshots are observations, never Python authority to release a body.
Cleanup must return through the original native token and complete manifest.
Game handlers validate command fields, except the native owner and action-shape
checks needed to preserve their wire contracts here.
"""

from dataclasses import dataclass, field
import re

from leeroy_mind.protocol import register_command, register_event


def _token(value: str) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{16}", value) is not None


def _id(value: str, *, empty: bool = False) -> bool:
    return type(value) is str and ((empty and value == "")
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None)


def _uint(value: int, maximum: int = 0xFFFFFFFF) -> bool:
    return type(value) is int and 0 < value <= maximum


def _manifest(members: tuple[int, ...]) -> bool:
    return (type(members) is tuple and 1 <= len(members) <= 150
        and all(_uint(member) for member in members) and len(set(members)) == len(members))


def _kill_id(value: str) -> bool:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}:[1-9][0-9]{0,19}", value) is None:
        return False
    return _uint(int(value[33:]), 0xFFFFFFFFFFFFFFFF)


def _raid_mode(difficulty: int, size: int, heroic: bool) -> bool:
    return (type(difficulty) is int and difficulty in {-1, 0, 1, 2, 3}
        and type(size) is int and size in {10, 25} and type(heroic) is bool
        and (difficulty == -1 or (size == (25 if difficulty % 2 else 10)
                                  and heroic == (difficulty >= 2))))


def _dungeon_mode(difficulty: int, size: int, heroic: bool) -> bool:
    """A 5-man: normal (0) or heroic (1), never an unmapped mode."""
    return (type(difficulty) is int and difficulty in {0, 1} and type(size) is int and size == 5
        and type(heroic) is bool and heroic == (difficulty == 1))


def _proof_identity(value, mode=_raid_mode) -> bool:
    return (_kill_id(value.kill_id) and _uint(value.native_epoch, 0xFFFFFFFFFFFFFFFF)
        and _uint(value.guild_id) and _uint(value.human_guid) and _uint(value.instance_id)
        and type(value.map) is int and 0 <= value.map <= 0xFFFFFFFF
        and type(value.boss_id) is int and 0 <= value.boss_id <= 0xFFFFFFFF
        and mode(value.difficulty, value.size, value.heroic))


# Native AttemptLedger: an exact count of confirmed pulls, 1..99; 0 is unknown.
MAX_ATTEMPTS = 99


def _kill_proof(value, mode) -> bool:
    # The native steady elapsed value is independent of Unix chronology.
    return not (not _proof_identity(value, mode)
            or any(not _uint(field, 253402300799999)
                   for field in (value.killed_at_ms, value.verified_at_ms, value.ts))
            or not value.killed_at_ms <= value.verified_at_ms <= value.ts
            or type(value.fight_started_at_ms) is not int or type(value.fight_ms) is not int
            or not ((value.fight_started_at_ms == 0 and value.fight_ms == -1)
                    or (_uint(value.fight_started_at_ms, 253402300799999)
                        and value.fight_started_at_ms <= value.killed_at_ms
                        and 0 <= value.fight_ms <= 3600000 and value.human_present is True))
            or type(value.attempts) is not int or not 0 <= value.attempts <= MAX_ATTEMPTS
            or type(value.human_present) is not bool
            or type(value.cast_members) is not tuple or len(value.cast_members) > 128
            or not all(_uint(member) and member != value.human_guid for member in value.cast_members)
            or tuple(sorted(set(value.cast_members))) != value.cast_members
            or (value.human_present and len(value.cast_members) == 128)
            or (not value.human_present and not value.cast_members))


def _bounded_text(value, lo, hi) -> bool:
    if type(value) is not str or "\0" in value:
        return False
    try:
        return lo <= len(value.encode("utf-8")) <= hi
    except UnicodeError:
        return False


@dataclass(frozen=True, slots=True)
class RealmActors:
    id: str
    guids: tuple[int, ...]
    gankers: tuple[int, ...] = ()
    guilds: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RealmSay:
    id: str
    guid: int
    channel: str
    text: str
    to_guid: int = 0


@dataclass(frozen=True, slots=True)
class RealmEmote:
    id: str
    guid: int
    emote: str
    target_guid: int = 0


@dataclass(frozen=True, slots=True)
class RealmMove:
    id: str
    guid: int
    place: str
    slot: int = 0


@dataclass(frozen=True, slots=True)
class RpgStatus:
    id: str
    guid: int
    status: str
    quest_id: int = 0


@dataclass(frozen=True, slots=True)
class LeavePvp:
    id: str
    guids: tuple[int, ...]
    pvp_owner: str = ""

    def __post_init__(self):
        if (not _id(self.id) or not _manifest(self.guids)
                or (self.pvp_owner != "" and not _token(self.pvp_owner))):
            raise ValueError("invalid Realm cleanup request")


@dataclass(frozen=True, slots=True)
class Rearm:
    id: str
    guids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WgJoin:
    id: str
    guids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ArenaTeam:
    id: str
    action: str
    # Native parsing requires unused action fields to be absent, including zero
    # and empty strings. Only these explicitly marked None values are omitted.
    captain_guid: int | None = field(default=None, metadata={"omit_none": True})
    arena_type: int | None = field(default=None, metadata={"omit_none": True})
    name: str | None = field(default=None, metadata={"omit_none": True})
    team_id: int | None = field(default=None, metadata={"omit_none": True})
    guid: int | None = field(default=None, metadata={"omit_none": True})

    def __post_init__(self):
        if self.action == "create":
            valid = (_uint(self.captain_guid) and type(self.arena_type) is int
                     and self.arena_type in {2, 3, 5} and type(self.name) is str
                     and 1 <= len(self.name.encode("utf-8")) <= 24 and "\0" not in self.name
                     and self.team_id is None and self.guid is None)
        elif self.action in {"add", "drop"}:
            valid = (_uint(self.team_id) and _uint(self.guid) and self.captain_guid is None
                     and self.arena_type is None and self.name is None)
        else:
            valid = False
        if not valid:
            raise ValueError("invalid Arena team action fields")


@dataclass(frozen=True, slots=True)
class ArenaQueue:
    id: str
    team_id: int
    captain_guid: int


@dataclass(frozen=True, slots=True)
class Duel:
    id: str
    guid: int
    target_guid: int


@dataclass(frozen=True, slots=True)
class Craft:
    id: str
    guid: int
    spell_id: int
    count: int = 1


@dataclass(frozen=True, slots=True)
class ItemCount:
    id: str
    guid: int
    item_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Equip:
    id: str
    guid: int
    item_id: int


@dataclass(frozen=True, slots=True)
class SetProfession:
    id: str
    guid: int
    skill_id: int


@dataclass(frozen=True, slots=True)
class TransmogSet:
    id: str
    guid: int
    slot: int
    entry: int


@dataclass(frozen=True, slots=True)
class CalendarPost:
    id: str
    creator_guid: int
    title: str
    description: str = field(default="", kw_only=True)
    event_type: str
    at: int
    dungeon_id: int = -1
    guild_event: bool = True
    invite_human: bool = False


@dataclass(frozen=True, slots=True)
class CalendarSignup:
    id: str
    event_id: int
    guid: int
    status: str


@dataclass(frozen=True, slots=True)
class CalendarUpdate:
    id: str
    event_id: int
    title: str = ""
    description: str = ""
    at: int = 0


@dataclass(frozen=True, slots=True)
class MailItem:
    item_id: int
    count: int
    from_: str


@dataclass(frozen=True, slots=True)
class MailSend:
    id: str
    from_guid: int
    subject: str
    body: str = ""
    items: tuple[MailItem, ...] = ()
    money: int = 0
    delay_s: int = 600


@dataclass(frozen=True, slots=True)
class BankTabBuy:
    id: str
    officer_guid: int
    tab: int
    name: str
    icon: str


@dataclass(frozen=True, slots=True)
class BankMoney:
    id: str
    amount: int


@dataclass(frozen=True, slots=True)
class BankDeposit:
    id: str
    guid: int
    item_id: int
    count: int
    tab: int


@dataclass(frozen=True, slots=True)
class BankWithdraw:
    id: str
    guid: int
    item_id: int
    count: int
    tab: int


@dataclass(frozen=True, slots=True)
class BankStock:
    id: str
    officer_guid: int
    tab: int
    item_id: int
    count: int


@dataclass(frozen=True, slots=True)
class GrantAchievement:
    id: str
    guid: int
    achievement_id: int


@dataclass(frozen=True, slots=True)
class GuildRename:
    id: str
    guild_id: int
    name: str


@dataclass(frozen=True, slots=True)
class GuildGrow:
    id: str
    guild_id: int
    guids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GuildAdmit:
    id: str
    guid: int
    rank: int = 4


@dataclass(frozen=True, slots=True)
class GuildNote:
    id: str
    guid: int
    officer_note: str


@dataclass(frozen=True, slots=True)
class GuildReadmit:
    """Re-admit one member he kicked, after his yes.

    kick_ts is the exact ts of the guard his_kick event; rank is that event's
    rank, or 2 for an officer (rank 1). Native refuses any other rejoin.
    """
    id: str
    guid: int
    kick_ts: int
    rank: int

    def __post_init__(self):
        if (not _id(self.id) or not _uint(self.guid) or not _uint(self.kick_ts, 253402300799999)
                or type(self.rank) is not int or not 2 <= self.rank <= 9):
            raise ValueError("invalid Realm re-admission")


@dataclass(frozen=True, slots=True)
class Announce:
    id: str
    kill_id: str
    native_epoch: int
    guild_id: int
    human_guid: int
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    heroic: bool
    boss_name: str
    attempts: int = field(default=0, kw_only=True)
    fight_s: int = field(default=0, kw_only=True)
    first: str = field(kw_only=True)

    def __post_init__(self):
        if (not _id(self.id) or not _proof_identity(self)
                or not _bounded_text(self.boss_name, 1, 48)
                or type(self.attempts) is not int or not 0 <= self.attempts <= 99
                or type(self.fight_s) is not int or not 0 <= self.fight_s <= 3600
                or type(self.first) is not str or self.first not in {"week", "ever"}):
            raise ValueError("invalid exact Realm announcement")


@dataclass(frozen=True, slots=True)
class RealmKill:
    """A verified raid boss kill. attempts: the native exact count, 0 when unknown."""
    ts: int
    kill_id: str
    native_epoch: int
    guild_id: int
    human_guid: int
    killed_at_ms: int
    verified_at_ms: int
    fight_started_at_ms: int
    fight_ms: int
    attempts: int
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    heroic: bool
    human_present: bool
    cast_members: tuple[int, ...]

    def __post_init__(self):
        if not _kill_proof(self, _raid_mode):
            raise ValueError("invalid native Realm kill proof")


@dataclass(frozen=True, slots=True)
class RealmDungeonKill:
    """A verified 5-man guild kill: him present, or a whole group of native cast.

    Evidence only: native never announces it, and its kill_id is in no proof ring.
    """
    ts: int
    kill_id: str
    native_epoch: int
    guild_id: int
    human_guid: int
    killed_at_ms: int
    verified_at_ms: int
    fight_started_at_ms: int
    fight_ms: int
    attempts: int
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    heroic: bool
    human_present: bool
    cast_members: tuple[int, ...]

    def __post_init__(self):
        if (not _kill_proof(self, _dungeon_mode)
                or (not self.human_present and len(self.cast_members) < 5)):
            raise ValueError("invalid native Realm dungeon kill proof")


@dataclass(frozen=True, slots=True)
class GetRealmPvp:
    id: str
    pvp_owner: str = ""
    origin_id: str = ""

    def __post_init__(self):
        if (not _id(self.id) or not _id(self.origin_id, empty=True)
                or (self.pvp_owner != "" and not _token(self.pvp_owner))
                or bool(self.pvp_owner) == bool(self.origin_id)):
            raise ValueError("exactly one native owner selector is required")


@dataclass(frozen=True, slots=True)
class RealmJob:
    ts: int
    id: str
    kind: str
    state: str
    reason: str
    guid: int
    value: int
    pvp_owner: str = ""
    changed: bool | None = None  # False is native proof for the whole failed job.
    # Members a leave or a rearm left as they were and stopped holding (one leading its battleground
    # group, one in combat); native sends the field only when there are some.
    skipped: tuple[int, ...] = ()

    def __post_init__(self):
        if (type(self.ts) is not int or not 0 <= self.ts <= 0x7FFFFFFFFFFFFFFF
                or not _id(self.id) or type(self.kind) is not str or not 1 <= len(self.kind) <= 64
                or self.state not in {"done", "failed"} or type(self.reason) is not str
                or type(self.guid) is not int or not 0 <= self.guid <= 0xFFFFFFFF
                or type(self.value) is not int or not 0 <= self.value <= 0xFFFFFFFFFFFFFFFF
                or (self.pvp_owner != "" and not _token(self.pvp_owner))
                or self.changed is not None and type(self.changed) is not bool
                or type(self.skipped) is not tuple or len(self.skipped) > 150
                or any(type(g) is not int or not 0 < g <= 0xFFFFFFFF for g in self.skipped)
                or len(set(self.skipped)) != len(self.skipped)):
            raise ValueError("invalid Realm job receipt")


@dataclass(frozen=True, slots=True)
class ChannelChat:
    ts: int
    channel: str
    from_guid: int
    from_name: str
    zone: int
    text: str
    via_bridge: bool


@dataclass(frozen=True, slots=True)
class PvpKill:
    ts: int
    killer: int
    victim: int
    map: int
    zone: int


@dataclass(frozen=True, slots=True)
class DuelEnd:
    """zone is the winner's; city: either duelist's zone has the core's capital flag."""
    ts: int
    winner: int
    loser: int
    result: str
    map: int
    zone: int
    city: bool


@dataclass(frozen=True, slots=True)
class PvpFlag:
    ts: int
    guid: int
    flagged: bool


@dataclass(frozen=True, slots=True)
class Wg:
    ts: int
    state: str
    defender: str
    seconds: int
    update_ms: float


@dataclass(frozen=True, slots=True)
class ArenaQueueEvent:
    ts: int
    team_id: int
    arena_type: int
    rated: bool


@dataclass(frozen=True, slots=True)
class ArenaEnd:
    ts: int
    arena_type: int
    winner_team: int
    loser_team: int
    winner_change: int
    loser_change: int
    members: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GameEvent:
    ts: int
    event_id: int
    state: str


@dataclass(frozen=True, slots=True)
class CalendarRsvp:
    ts: int
    event_id: int
    status: str


@dataclass(frozen=True, slots=True)
class MailTaken:
    ts: int
    item_guid: int
    item_id: int
    count: int


REALM_FIRST_FLAGS = 0x100 | 0x200  # AchievementFlags: REALM_FIRST_REACH, REALM_FIRST_KILL


@dataclass(frozen=True, slots=True)
class RealmAchievement:
    """An earned achievement of him or a cast member. Never a quiet grant.

    category and flags are the DBC's; realm_first is the core's realm-first rule
    (the flag, unless his account may not take one).
    """
    ts: int
    guid: int
    achievement_id: int
    name: str
    category: int
    flags: int
    realm_first: bool

    def __post_init__(self):
        if (not _uint(self.guid) or not _uint(self.achievement_id) or not _bounded_text(self.name, 0, 128)
                or type(self.category) is not int or not 0 <= self.category <= 0xFFFFFFFF
                or type(self.flags) is not int or not 0 <= self.flags <= 0xFFFFFFFF
                or type(self.realm_first) is not bool
                or (self.realm_first and not self.flags & REALM_FIRST_FLAGS)):
            raise ValueError("invalid native Realm achievement")


@dataclass(frozen=True, slots=True)
class Guard:
    ts: int
    guid: int
    action: str
    rank: int
    reason: str


@dataclass(frozen=True, slots=True)
class RealmPvp:
    ts: int
    id: str
    pvp_owner: str
    origin_id: str
    kind: str
    origin_epoch: int
    revision: int
    guild: int
    human: int
    members: tuple[int, ...]
    stage: str
    application_fenced: bool
    reason: str
    cleanup_id: str

    def __post_init__(self):
        if (type(self.ts) is not int or not 0 <= self.ts <= 0x7FFFFFFFFFFFFFFF
                or not _id(self.id) or not _token(self.pvp_owner) or not _id(self.origin_id)
                or self.kind not in {"wintergrasp", "rated_arena", "fishing", "duel"}
                or not _uint(self.origin_epoch, 0xFFFFFFFFFFFFFFFF)
                or not _uint(self.revision, 0xFFFFFFFFFFFFFFFF)
                or not _uint(self.guild) or not _uint(self.human) or not _manifest(self.members)
                or tuple(sorted(self.members)) != self.members
                or self.stage not in {"acquiring", "queued", "invited", "entering", "active", "ending",
                                      "cleaning", "clean", "unknown"}
                or type(self.application_fenced) is not bool
                or type(self.reason) is not str or len(self.reason.encode("utf-8")) > 128 or "\0" in self.reason
                or not _id(self.cleanup_id, empty=True)
                or (self.stage in {"cleaning", "clean"} and not self.cleanup_id)
                or (self.stage in {"cleaning", "clean", "unknown"} and not self.application_fenced)):
            raise ValueError("invalid native Realm owner receipt")


COMMANDS: dict[str, type] = {
    "realm_actors": RealmActors,
    "realm_say": RealmSay,
    "realm_emote": RealmEmote,
    "realm_move": RealmMove,
    "rpg_status": RpgStatus,
    "leave_pvp": LeavePvp,
    "rearm": Rearm,
    "wg_join": WgJoin,
    "arena_team": ArenaTeam,
    "arena_queue": ArenaQueue,
    "duel": Duel,
    "craft": Craft,
    "item_count": ItemCount,
    "equip": Equip,
    "set_profession": SetProfession,
    "transmog_set": TransmogSet,
    "calendar_post": CalendarPost,
    "calendar_signup": CalendarSignup,
    "calendar_update": CalendarUpdate,
    "mail_send": MailSend,
    "bank_tab_buy": BankTabBuy,
    "bank_money": BankMoney,
    "bank_deposit": BankDeposit,
    "bank_withdraw": BankWithdraw,
    "bank_stock": BankStock,
    "grant_achievement": GrantAchievement,
    "guild_rename": GuildRename,
    "guild_grow": GuildGrow,
    "guild_admit": GuildAdmit,
    "guild_note": GuildNote,
    "announce": Announce,
    "get_realm_pvp": GetRealmPvp,
    "guild_readmit": GuildReadmit,
}
EVENTS: dict[str, type] = {
    "realm_job": RealmJob,
    "channel_chat": ChannelChat,
    "pvp_kill": PvpKill,
    "duel_end": DuelEnd,
    "pvp_flag": PvpFlag,
    "wg": Wg,
    "arena_queue": ArenaQueueEvent,
    "arena_end": ArenaEnd,
    "game_event": GameEvent,
    "calendar_rsvp": CalendarRsvp,
    "mail_taken": MailTaken,
    "guard": Guard,
    "realm_pvp": RealmPvp,
    "realm_kill": RealmKill,
    "realm_dungeon_kill": RealmDungeonKill,
    "realm_achievement": RealmAchievement,
}
NOISY = frozenset({"realm_job", "channel_chat", "game_event"})


def register() -> None:
    """Register all Realm types without enabling game hooks; safe to repeat."""
    for name, cls in COMMANDS.items():
        register_command(name, cls)
    for name, cls in EVENTS.items():
        register_event(name, cls)
