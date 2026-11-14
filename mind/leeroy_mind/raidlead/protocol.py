"""The raid lead's one bridge command of its own (mod-leeroy Guild/RaidLead.cpp), registered by the
raid lead's attach only; its orders ride guild life's bot_do ("order", "cast")."""

from dataclasses import dataclass

from leeroy_mind.protocol import register_command


@dataclass(frozen=True, slots=True)
class RaidLead:
    """"assist"/"unassist": the assistant flag of cast member `guid` in the group he leads (never
    the leader flag); "mark": `icon` on his hostile target, set by `guid` (her, or him)."""
    id: str
    action: str
    guid: int
    icon: str = ""


def register() -> None:
    register_command("raid_lead", RaidLead)
