"""Which channel a turn goes to. Never `yell`; `say` is chosen by the caller, only to answer
the human's own /say."""
from leeroy_mind.talk.raid import RaidPhase, RaidState
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView

OFFICER_OPEN_CHANCE = 0.10
_FIGHT_PHASES = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})
_NOT_SOCIAL = frozenset({TurnKind.REPLY_HUMAN, TurnKind.CALLOUT, TurnKind.URGENT,
                         TurnKind.WIPE_REACT})
_GROUP_TALK = frozenset({TurnKind.OPEN, TurnKind.BIT, TurnKind.ASIDE,
                         TurnKind.FLIRT})
_RAID_EVENT = frozenset({TurnKind.KILL, TurnKind.WIPE_REACT, TurnKind.CALLOUT, TurnKind.URGENT})


def choose_channel(kind: TurnKind, *, speaker: int, world: WorldView, raid: RaidState,
                   now: float, rng, reply_to: str | None = None,
                   opener_channel: str | None = None) -> str | None:
    """None means the turn must not be spoken. A REPLY_HUMAN goes where the caller says
    (reply_to) and a CONTINUE stays on its opener's channel; without those, None."""
    phase = raid.phase(now)
    if phase in _FIGHT_PHASES and kind not in _NOT_SOCIAL:
        return None
    if kind is TurnKind.REPLY_HUMAN:
        return reply_to
    if kind is TurnKind.CONTINUE:
        return opener_channel
    if kind is TurnKind.WHISPER:
        return "whisper"
    g = world.group
    grouped = g is not None and world.in_group(speaker)
    in_raid = grouped and g.raid
    if kind in _RAID_EVENT or phase in _FIGHT_PHASES:
        return "raid" if in_raid else "party" if grouped else "guild"
    if kind is TurnKind.OPEN and world.is_officer(speaker):
        officers = [x for x in world.online if world.is_officer(x)]
        if len(officers) >= 2 and rng.random() < OFFICER_OPEN_CHANCE:
            return "officer"
    if kind in _GROUP_TALK:
        if in_raid:
            return "raid"
        if grouped and world.human_online and world.in_group(world.human_guid) \
                and len(g.members) <= 5:
            return "party"
    return "guild"
