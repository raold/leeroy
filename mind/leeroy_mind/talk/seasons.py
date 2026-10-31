"""The calendar the guild lives by: the weekday and tonight's holidays, real life's and the
game's (fix wave G1, from rehearsal 2: on Halloween, with Hallow's End running, nobody said a
word about it). Nights follow clock.night_of, so 01:00 is still the evening before, and a
holiday is tonight's for the whole night.

The game's seasonal events are 3.3.5's, at the dates AzerothCore's game_event table starts
them by default; real life's are the fixed dates plus Thanksgiving (the fourth Thursday of
November, for members in the US)."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from leeroy_mind.clock import night_of
from leeroy_mind.talk.budget import DayPhase, day_phase


@dataclass(frozen=True, slots=True)
class Holiday:
    """name: as a sentence says it ("Halloween", "Hallow's End"). start and end: MM-DD,
    inclusive, an end before the start wrapping the year. game: one of the game's seasonal
    events. topic: what an opener brings up about it, a concrete situation. regions: the
    members it fits (Identity.region), None for everyone."""
    name: str
    start: str
    end: str
    game: bool
    topic: str
    regions: frozenset[str] | None = None


THANKSGIVING = Holiday("Thanksgiving", "", "", False,
                       "Thanksgiving: the food, the family, the long weekend",
                       frozenset({"US"}))
HOLIDAYS: tuple[Holiday, ...] = (
    Holiday("Halloween", "10-31", "10-31", False,
            "it's Halloween: costumes, candy, trick-or-treaters at the door"),
    Holiday("Christmas Eve", "12-24", "12-24", False,
            "Christmas Eve: the last presents, the family coming over"),
    Holiday("Christmas Day", "12-25", "12-25", False,
            "Christmas: the presents, the food, the family"),
    Holiday("New Year's Eve", "12-31", "12-31", False,
            "New Year's Eve: plans for midnight, or none"),
    Holiday("New Year's Day", "01-01", "01-01", False,
            "New Year's Day: last night, and the resolutions"),
    Holiday("Valentine's Day", "02-14", "02-14", False,
            "Valentine's Day: plans tonight, or none"),
    Holiday("Hallow's End", "10-18", "11-01", True,
            "Hallow's End in game: candy buckets, the Headless Horseman, the wickerman"),
    Holiday("Brewfest", "09-20", "10-06", True,
            "Brewfest in game: the rams, the kegs, Coren Direbrew"),
    Holiday("the Feast of Winter Veil", "12-15", "01-02", True,
            "Winter Veil in game: the presents under the tree, Greatfather Winter"),
    Holiday("Love is in the Air", "02-07", "02-20", True,
            "Love is in the Air in game: the perfume, the crown chemical guys"),
    Holiday("the Midsummer Fire Festival", "06-21", "07-05", True,
            "Midsummer in game: the bonfires, the flames, Ahune"),
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _in(md: str, start: str, end: str) -> bool:
    return start <= md <= end if start <= end else md >= start or md <= end


def thanksgiving(year: int) -> date:
    """The fourth Thursday of November."""
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def tonight(local: datetime) -> date:
    """The night `local` belongs to (clock.night_of)."""
    return date.fromisoformat(night_of(local))


def holidays_on(day: date) -> list[Holiday]:
    """The holidays on this night, real life's first."""
    md = day.strftime("%m-%d")
    out = [h for h in HOLIDAYS if _in(md, h.start, h.end)]
    if day == thanksgiving(day.year):
        out.append(THANKSGIVING)
    return sorted(out, key=lambda h: h.game)


def fits(h: Holiday, region: str) -> bool:
    return h.regions is None or region in h.regions


def day_line(local: datetime, region: str = "") -> str:
    """The calendar half of "Right now:": the day and the time of day, then tonight's
    holidays the member keeps (`region`; "" keeps them all) and the game's event: "It's
    Saturday evening. It's Halloween, and Hallow's End is on in the game."."""
    phase = day_phase(local)
    day = tonight(local)
    name = _WEEKDAYS[day.weekday()]
    when = {DayPhase.MORNING: f"It's {name} morning.",
            DayPhase.DAY: f"It's the middle of the day on {name}.",
            DayPhase.EVENING: f"It's {name} evening.",
            DayPhase.LATE: f"It's late on {name} night.",
            DayPhase.DEAD: f"It's the small hours of {name} night."}[phase]
    hs = [h for h in holidays_on(day) if not region or fits(h, region)]
    real = [h.name for h in hs if not h.game]
    game = [h.name for h in hs if h.game]
    parts = []
    if real:
        parts.append(f"It's {' and '.join(real)}")
    if game:
        event = f"{' and '.join(game)} {'is' if len(game) == 1 else 'are'} on in the game"
        parts.append(event[0].upper() + event[1:] if not parts else event)
    return when + (" " + ", and ".join(parts) + "." if parts else "")


def holiday_topics(local: datetime, region: str) -> list[str]:
    """What an opener could bring up about tonight's holidays, the ones that fit."""
    return [h.topic for h in holidays_on(tonight(local)) if fits(h, region)]
