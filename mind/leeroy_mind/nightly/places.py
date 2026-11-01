"""Shared encounter settings, keyed by the cast's outside-raids interests."""

PLACES: dict[str, tuple[str, ...]] = {
    "battlegrounds": ("a Wintergrasp battle", "an Arathi Basin queue"),
    "arena": ("an arena queue",),
    "gathering": ("herb farming in Sholazar Basin",),
    "crafting": ("the Dalaran crafting district",),
    "dungeons": ("a heroic Utgarde Pinnacle run",),
    "idling": ("the Orgrimmar bank steps", "voice chat late at night"),
    "auction_house": ("the Orgrimmar auction house",),
    "fishing": ("the docks in Dalaran",),
    "collecting": ("a rare-mount camp in Northrend",),
}


def place_kind(setting: str) -> str | None:
    """An exact known setting's interest; unknown settings have no inferred motive."""
    return next((kind for kind, settings in PLACES.items() if setting in settings), None)
