"""The lead's one bridge command (mod-leeroy Guild/Lead.cpp), registered by its attach only; in an
instance it rides guild life's raid_control ("dc_on", "dc_off") instead."""

from dataclasses import dataclass, field

from leeroy_mind.protocol import register_command


@dataclass(frozen=True, slots=True)
class LeadDo:
    """"start": `guid` takes his group's lead for `activity` (quest, grind, travel; travel names
    `spot`, one of the meeting spots). "end": the lead in force ends (`guid` 0: whoever leads).
    activity and spot go only when set."""
    id: str
    action: str
    guid: int = 0
    activity: str = field(default="", metadata={"omit_default": True})
    spot: str = field(default="", metadata={"omit_default": True})


def register() -> None:
    register_command("lead_do", LeadDo)
