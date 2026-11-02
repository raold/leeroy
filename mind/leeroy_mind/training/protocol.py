"""Additive training messages for bridge v1. Game handlers validate commands.

Registration is explicit and does not enable telemetry, training, or app hooks.
Guild life continues to own the shared ``job`` event.
"""

from dataclasses import dataclass
from typing import cast

from leeroy_mind.protocol import register_command, register_event


_AUTO_MIN_MEMBERS = object()


@dataclass(frozen=True, slots=True)
class Telemetry:
    id: str
    record: bool
    watch: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class GetStats:
    id: str


@dataclass(frozen=True, slots=True)
class GetVocab:
    id: str
    classes: tuple[int, ...] = ()
    probes: tuple[int, ...] = ()
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CustomStrategy:
    id: str
    name: str
    class_: int
    lines: tuple[str, ...]
    probe: int = 0


@dataclass(frozen=True, slots=True)
class DcSetting:
    id: str
    leader_guid: int
    op: str
    key: str = ""
    centi: int = 0


@dataclass(frozen=True, slots=True)
class DummyTrial:
    id: str
    guids: tuple[int, ...]
    co: tuple[str, ...]
    place: int
    seconds: int


@dataclass(frozen=True, slots=True)
class GetRivals:
    id: str
    level: int = 80
    max_: int = 200


@dataclass(frozen=True, slots=True)
class StartBenchmark:
    id: str
    map_id: int
    size: int
    leader_guid: int
    members: tuple[int, ...]
    bosses: tuple[int, ...]
    heroic: bool = False
    max_wipes: int = 6
    max_wipes_per_boss: int = 3
    timeout_min: int = 180
    min_members: int = cast(int, _AUTO_MIN_MEMBERS)

    def __post_init__(self):
        # Same omitted-value semantics as guild.StartOfflineRaid. Explicit zero
        # stays zero so the server rejects it rather than silently repairing it.
        if self.min_members is _AUTO_MIN_MEMBERS:
            object.__setattr__(self, "min_members", min(self.size * 4 // 5,
                                                       len(self.members) + 1))


@dataclass(frozen=True, slots=True)
class SpellTotal:
    spell: int
    amount: int
    hits: int


@dataclass(frozen=True, slots=True)
class TakenTotal:
    spell: int
    source: int
    amount: int
    hits: int


@dataclass(frozen=True, slots=True)
class DebuffTotal:
    spell: int
    count: int
    max_stack: int


@dataclass(frozen=True, slots=True)
class CastTotal:
    spell: int
    count: int


@dataclass(frozen=True, slots=True)
class LastHit:
    at_ms: int
    spell: int
    source: int
    amount: int
    hp: int


@dataclass(frozen=True, slots=True)
class TrackCast:
    spell: int
    at_s: int


@dataclass(frozen=True, slots=True)
class DcKey:
    key: str
    min_: int
    max_: int
    value: int
    raid: int
    heroic: int


@dataclass(frozen=True, slots=True)
class Rival:
    guid: int
    name: str
    class_: int
    spec_tab: int
    guild_id: int
    ilvl: int
    zone: int
    team: int


@dataclass(frozen=True, slots=True)
class Pull:
    ts: int
    id: str
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    outcome: str
    start_ms: int
    end_ms: int
    members: int
    deaths: int
    tracks: int
    assisted: bool
    source: str
    boss_entry: int
    upd_mean_ms: int
    # These labels are evidence, not an assertion that requested damage reached
    # health. Older senders provide no basis and must never imply exact DPS.
    damage_basis: str = "unknown"
    healing_basis: str = "unknown"
    # Calculation-hook tags do not carry a damage/heal event identity. The
    # counter counts completed interrupt-spell casts, not proven interruptions.
    spell_basis: str = "unknown"
    interrupt_basis: str = "unknown"


@dataclass(frozen=True, slots=True)
class PullMember:
    ts: int
    pull: str
    guid: int
    class_: int
    spec_tab: int
    tank: bool
    ilvl: int
    damage: int
    damage_boss: int
    healing: int
    overheal: int
    taken: int
    deaths: int
    interrupts: int
    active_ms: int
    done: tuple[SpellTotal, ...]
    taken_by: tuple[TakenTotal, ...]
    debuffs: tuple[DebuffTotal, ...]
    casts: tuple[CastTotal, ...]


@dataclass(frozen=True, slots=True)
class PullDeath:
    ts: int
    pull: str
    guid: int
    at_ms: int
    spell: int
    source: int
    x: int
    y: int
    hits: tuple[LastHit, ...]


@dataclass(frozen=True, slots=True)
class PullTrack:
    ts: int
    pull: str
    who: int
    entry: int
    from_s: int
    xs: tuple[int, ...]
    ys: tuple[int, ...]
    o: tuple[int, ...]
    hp: tuple[int, ...]
    power: tuple[int, ...]
    target: tuple[int, ...]
    casts: tuple[TrackCast, ...]


@dataclass(frozen=True, slots=True)
class BossCast:
    ts: int
    pull: str
    map: int
    instance_id: int
    caster_entry: int
    caster: int
    spell: int
    target: int
    state: str
    cast_ms: int


@dataclass(frozen=True, slots=True)
class PullPulse:
    ts: int
    pull: str
    map: int
    instance_id: int
    boss_id: int
    at_ms: int
    boss_hp: int
    alive: int


@dataclass(frozen=True, slots=True)
class Vocab:
    ts: int
    id: str
    class_: int
    probe: int
    strategies: tuple[str, ...]
    actions: tuple[str, ...]
    spells: tuple[str, ...]
    spells_checked: bool
    triggers: tuple[str, ...]
    dc: tuple[DcKey, ...]
    whitelist: tuple[str, ...]
    # Exact selected probe's active talent tab, captured with its spells.
    # Old producers and non-probe rows establish no spec identity.
    spec_tab: int = -1


@dataclass(frozen=True, slots=True)
class ServerStats:
    ts: int
    id: str
    mean_ms: int
    last_ms: int
    telemetry: bool
    armed: int


@dataclass(frozen=True, slots=True)
class DummyResult:
    ts: int
    id: str
    guid: int
    damage: int
    ms: int
    hits: int
    dropped: str
    damage_basis: str = "unknown"
    spell_basis: str = "unknown"


@dataclass(frozen=True, slots=True)
class Rivals:
    ts: int
    id: str
    members: tuple[Rival, ...]


COMMANDS: dict[str, type] = {
    "telemetry": Telemetry, "get_stats": GetStats, "get_vocab": GetVocab,
    "custom_strategy": CustomStrategy, "dc_setting": DcSetting,
    "dummy_trial": DummyTrial, "get_rivals": GetRivals, "start_benchmark": StartBenchmark,
}
EVENTS: dict[str, type] = {
    "pull": Pull, "pull_member": PullMember, "pull_death": PullDeath,
    "pull_track": PullTrack, "boss_cast": BossCast, "pull_pulse": PullPulse,
    "vocab": Vocab, "server_stats": ServerStats, "dummy_result": DummyResult, "rivals": Rivals,
}
TG_EVENT_TYPES = frozenset(EVENTS)


def register() -> None:
    for name, cls in COMMANDS.items():
        register_command(name, cls)
    for name, cls in EVENTS.items():
        register_event(name, cls)
