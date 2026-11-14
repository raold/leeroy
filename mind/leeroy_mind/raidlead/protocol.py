"""The raid lead's one bridge command of its own (mod-leeroy Guild/RaidLead.cpp), registered by the
raid lead's attach only; its orders ride guild life's bot_do ("order", "cast")."""

from dataclasses import dataclass

from leeroy_mind.protocol import register_command


@dataclass(frozen=True, slots=True)
class RaidLead:
    """"mark": `icon` on his hostile target, set server-side by `guid` (her, or him; no assistant
    flag is ever set); "probe": whether native's switch is on (the ack); "sync": that, and native
    publishes his group as a group event. guid 0 and icon "" are none."""
    id: str
    action: str
    guid: int
    icon: str = ""


def register() -> None:
    register_command("raid_lead", RaidLead)
