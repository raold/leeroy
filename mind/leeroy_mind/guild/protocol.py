"""Additive guild-life messages for bridge protocol v1.

Importing this module does not attach guild life to the app. Call ``register``
before encoding commands or receiving job events. Game-side handlers validate
command fields; these immutable messages preserve their wire names and defaults.
"""

from dataclasses import dataclass, field
from typing import cast

from leeroy_mind.protocol import register_command, register_event


_AUTO_MIN_MEMBERS = object()


@dataclass(frozen=True, slots=True)
class FormRaid:
    id: str
    leader_guid: int
    members: tuple[int, ...]
    size: int
    heroic: bool = field(default=False, kw_only=True)
    map_id: int
    main_tank_guid: int
    assistants: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Summon:
    id: str
    map_id: int
    guids: tuple[int, ...]
    where: str = "entrance"
    summon_human: bool = False
    summoner_guid: int = 0


@dataclass(frozen=True, slots=True)
class ClearLockout:
    id: str
    map_id: int
    guids: tuple[int, ...]
    difficulties: tuple[int, ...] = (0, 1, 2, 3)


@dataclass(frozen=True, slots=True)
class SetLootMaster:
    id: str
    member_guid: int
    looter_guid: int
    threshold: int = 4


@dataclass(frozen=True, slots=True)
class GiveLoot:
    id: str
    map: int
    instance_id: int
    source_entry: int
    index: int
    item_id: int
    to_guid: int


@dataclass(frozen=True, slots=True)
class StartOfflineRaid:
    id: str
    map_id: int
    size: int
    heroic: bool = field(default=False, kw_only=True)
    leader_guid: int
    members: tuple[int, ...]
    assistants: tuple[int, ...] = field(default=(), kw_only=True)
    loot_master_guid: int
    max_wipes: int = 6
    max_wipes_per_boss: int = 3
    timeout_min: int = 180
    min_members: int = cast(int, _AUTO_MIN_MEMBERS)

    def __post_init__(self) -> None:
        # Omitted and explicitly invalid values stay distinct: the server must
        # still reject an explicit zero rather than silently changing its meaning.
        if self.min_members is _AUTO_MIN_MEMBERS:
            object.__setattr__(self, "min_members", min(self.size * 4 // 5,
                                                       len(self.members) + 1))


@dataclass(frozen=True, slots=True)
class StopOfflineRaid:
    id: str
    raid_id: str


@dataclass(frozen=True, slots=True)
class GearUp:
    id: str
    guids: tuple[int, ...]
    ilvl: int = 264
    level_to: int = 80


@dataclass(frozen=True, slots=True)
class RaidControl:
    id: str
    member_guid: int
    action: str
    leader_guid: int = 0
    param: str = ""
    training_run: str = ""


@dataclass(frozen=True, slots=True)
class RemoveFriend:
    id: str
    guid: int


@dataclass(frozen=True, slots=True)
class SetSpec:
    id: str
    guid: int
    spec_no: int
    glyphs: bool = True


@dataclass(frozen=True, slots=True)
class BotDo:
    id: str
    guids: tuple[int, ...]
    action: str
    param: str
    target_guid: int = 0
    training_run: str = ""


@dataclass(frozen=True, slots=True)
class StockItems:
    id: str
    guid: int
    items: tuple[int, ...]
    count: int = 5


@dataclass(frozen=True, slots=True)
class StringDo:
    """His string in game (godmode, D6 Part B): one cast member comes to him, joins his party,
    follows him (or stops), duels him, emotes at him or sits near him. Carried out at once or
    refused with one code in the ack; a done `job` (kind string_do) names what she did."""
    id: str
    guid: int
    kind: str
    target_guid: int = 0
    emote: str = ""


@dataclass(frozen=True, slots=True)
class GuildAddRoster:
    """The posted raid lineup (decisions.lineup) the owner's add-from-guild picker follows while
    Leeroy.GuildAdd.FollowRoster is on: members first within each role, the bench only when nobody
    else fits, until until_ms (Unix ms, at most 12 h ahead). Empty members and bench clear it.
    Held in memory: a worldserver start clears it and the mind sends it again on hello."""
    id: str
    members: tuple[int, ...]
    bench: tuple[int, ...]
    until_ms: int


@dataclass(frozen=True, slots=True)
class JobSkip:
    guid: int
    reason: str


@dataclass(frozen=True, slots=True)
class Job:
    ts: int
    id: str
    kind: str
    state: str
    reason: str
    stage: str
    guids: tuple[int, ...]
    skipped: tuple[JobSkip, ...]
    kills: int
    wipes: int
    instance_id: int
    guid: int
    level: int
    avg_ilvl: int
    training_run: str = ""
    members_in_instance: int | None = None
    last_progress_ts: int | None = None
    boss_engaged: bool | None = None
    # What dungeon-clear's leader run is doing (offline running heartbeats): unknown, off, on,
    # paused, door. Absent from older worldservers.
    dc_state: str | None = None


@dataclass(frozen=True, slots=True)
class GuildGroupAdd:
    guid: int
    role: str


@dataclass(frozen=True, slots=True)
class GuildGroup:
    """His own `.playerbots bot addN` filled his group from free guild members (mod-leeroy, not a
    raid call). reason "built": this run added `added`; "entered": he then reached an instance map
    leading that same group. map/instance_id/difficulty are where he is (instance_id 0 outside an
    instance); members are the guild's cast in his group, him excluded. roster_followed: the
    picker used the guild's posted lineup (guild_add_roster) for at least one member it added to
    this group (absent from older worldservers: False)."""
    ts: int
    reason: str
    size: int
    leader_guid: int
    raid: bool
    map: int
    instance_id: int
    difficulty: int
    added: tuple[GuildGroupAdd, ...]
    members: tuple[int, ...]
    roster_followed: bool = False


COMMANDS: dict[str, type] = {
    "form_raid": FormRaid,
    "summon": Summon,
    "clear_lockout": ClearLockout,
    "set_loot_master": SetLootMaster,
    "give_loot": GiveLoot,
    "start_offline_raid": StartOfflineRaid,
    "offline_raid_stop": StopOfflineRaid,
    "gear_up": GearUp,
    "raid_control": RaidControl,
    "remove_friend": RemoveFriend,
    "set_spec": SetSpec,
    "bot_do": BotDo,
    "stock_items": StockItems,
    "string_do": StringDo,
    "guild_add_roster": GuildAddRoster,
}


def register() -> None:
    """Register exactly the guild-life extension types; safe to call again."""
    for name, cls in COMMANDS.items():
        register_command(name, cls)
    register_event("job", Job)
    register_event("guild_group", GuildGroup)
