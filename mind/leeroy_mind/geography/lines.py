"""The words a prompt says about places: her home in her card, the "where you live, right now"
line of her private and Discord turns, and where he lives in her private turns with him.

All code-built from the place sheet (geography/places.toml) and her local clock: no model.
`now_parts` is at most NOW_TOKENS estimated tokens, its parts in the order the prompt's trim
lets them go (the last first)."""
import math
from datetime import datetime, timedelta, timezone

from leeroy_mind.text import estimate_tokens

from .places import Place, km_between

NOW_TOKENS = 60
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
# Driving: the road is about this much longer than the straight line; average speeds with stops
ROAD_FACTOR = 1.2
DRIVE_KMH = {"US": 100.0, "UK": 85.0}
FLIGHT_KMH = 750.0
FAR_DRIVE_H = 10  # longer than this a flight is the usual way
# Hawaii is an ocean away from the other states; Alaska a long way from all of them
_AWAY = frozenset({"Hawaii", "Alaska"})


def home_phrase(place: Place | None) -> str:
    """Her card's line: "You live in Colorado." ("" with no place, or the UK as a whole, which
    her card's "from the UK" says already)."""
    if place is None or (place.country == "UK" and place.key == "uk.uk"):
        return ""
    return f"You live in {place.label}."


def _f(place: Place, value_f: float) -> str:
    """A temperature as people there say it, in plain ASCII ("52F", "7C")."""
    if place.celsius:
        return f"{round((value_f - 32) * 5 / 9)}C"
    return f"{round(value_f)}F"


def typical(place: Place, local: datetime) -> tuple[float, float]:
    """The typical high and low (°F) for her local date: the month's normals, eased toward the
    next or the last month's away from mid-month."""
    m = local.month - 1
    day = local.day
    if day >= 15:
        other, weight = (m + 1) % 12, (day - 15) / 30
    else:
        other, weight = (m - 1) % 12, (15 - day) / 30
    high = place.highs[m] * (1 - weight) + place.highs[other] * weight
    low = place.lows[m] * (1 - weight) + place.lows[other] * weight
    return high, low


def at_hour(high: float, low: float, local: datetime) -> float:
    """About the temperature at her local hour: rising from the low near 6:00 to the high near
    15:00, then falling, fast in the evening and slowly overnight."""
    h = local.hour + local.minute / 60
    if 6 <= h <= 15:
        share = (1 - math.cos(math.pi * (h - 6) / 9)) / 2
    else:
        share = 1 - math.sqrt(((h - 15) % 24) / 15)
    return low + (high - low) * share


def feel(temp_f: float, *, humid: bool) -> str:
    word = ("bitterly cold" if temp_f < 20 else "freezing" if temp_f < 33 else
            "cold" if temp_f < 46 else "cool" if temp_f < 59 else "mild" if temp_f < 71 else
            "warm" if temp_f < 81 else "hot" if temp_f < 91 else "very hot")
    return f"{word} and humid" if humid and temp_f >= 78 else word


def sun_elevation(point: tuple[float, float], instant: datetime) -> float:
    """The sun's elevation in degrees at `point` (latitude, longitude) at an aware instant: NOAA's
    general solar position approximation (within about a degree)."""
    t = instant.astimezone(timezone.utc)
    hours = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (t.timetuple().tm_yday - 1 + (hours - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    solar_min = hours * 60 + eqtime + 4 * point[1]
    hour_angle = math.radians(solar_min / 4 - 180)
    lat = math.radians(point[0])
    cos_zenith = (math.sin(lat) * math.sin(decl)
                  + math.cos(lat) * math.cos(decl) * math.cos(hour_angle))
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def light_words(place: Place, local: datetime) -> str:
    """"It's dark out now." and the like, from the sun at her place."""
    instant = local if local.tzinfo else local.replace(tzinfo=place.tz)
    e = sun_elevation(place.point, instant)
    if e >= 0:
        return "It's light out now."
    if e >= -6:
        return "It's getting light out." if local.hour < 12 else "It's getting dark out."
    return "It's dark out now."


def _when(local: datetime) -> str:
    part = "Early" if local.day <= 10 else "Mid" if local.day <= 20 else "Late"
    return f"{part} {_MONTHS[local.month - 1]}"


def now_parts(place: Place, local: datetime) -> list[str]:
    """Her "where you live, right now" line in parts, at most NOW_TOKENS estimated tokens
    together: where (the place and its land), the season (typical highs and lows for the date,
    how it feels at her hour, snow when it can come, the season's own weather or event), and
    whether it is light. `local` is her local time (an aware one in her zone, or her
    wall-clock time). What is over the budget goes in this order: the season's note, its event,
    the light, then the land's words after its first clause."""
    high, low = typical(place, local)
    now = at_hour(high, low, local)
    month = local.month
    snow = month in place.snow and low <= 34
    event = next((e.what for e in place.events if month in e.months), "")
    note = place.note(month)
    where = f"Where you live: {place.label}, {place.terrain}."
    light = light_words(place, local)

    def season(with_event: bool, with_note: bool) -> str:
        extra = [x for x in ("snow possible" if snow else "", event if with_event else "",
                             note if with_note else "") if x]
        return (f"{_when(local)} there: highs near {_f(place, high)}, lows near {_f(place, low)};"
                f" {feel(now, humid=place.humid)} at this hour"
                + "".join(f", {x}" for x in extra) + ".")

    def fits(parts: list[str]) -> bool:
        return estimate_tokens(" ".join(parts)) <= NOW_TOKENS

    for parts in ([where, season(True, True), light], [where, season(True, False), light],
                  [where, season(False, False), light], [where, season(False, False)]):
        if fits(parts):
            return parts
    short = f"Where you live: {place.label}, {place.terrain.split(',')[0]}."
    for parts in ([short, season(False, False)], [short]):
        if fits(parts):
            return parts
    return [f"Where you live: {place.label}."]


def where_he_lives(home: Place, detail: str) -> str:
    """"Billings, Montana", "Bolton in the North West of England", or the place alone."""
    if not detail:
        return home.label
    return f"{detail} in {home.label}" if home.label.startswith("the ") else f"{detail}, {home.label}"


def travel(her: Place, his: Place) -> str:
    """How far he is from her, as people say it: the same state, a drive of about N hours, a
    flight, or an ocean."""
    if her.key == his.key:
        return ("the same state as you" if her.country == "US" else
                "the same part of the UK as you")
    if her.country != his.country:
        return "an ocean away: a long flight, eight hours or more in the air"
    km = km_between(her, his)
    flight = max(1, round(km / FLIGHT_KMH + 0.5))
    flight_text = "about an hour's flight" if flight == 1 else f"about {flight} hours' flight"
    if her.key in _AWAY or his.key in _AWAY:
        return f"a long way: {flight_text}"
    hours = km * ROAD_FACTOR / DRIVE_KMH[her.country]
    if hours < 1:
        return "under an hour's drive"
    h = round(hours)
    drive = "about an hour's drive" if h <= 1 else f"about {h} hours' drive"
    return drive if h <= FAR_DRIVE_H else f"{flight_text}, or {drive}"


def clock_gap(her: Place, his: Place, instant: datetime, their: str) -> str:
    """How his clock stands to hers now ("Their clock is 2 hours ahead of yours."; "" when they
    keep the same time). `their` is his possessive ("his")."""
    t = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    gap = (t.astimezone(his.tz).utcoffset() or timedelta()) - (
        t.astimezone(her.tz).utcoffset() or timedelta())
    hours = gap.total_seconds() / 3600
    if abs(hours) < 0.25:
        return "You keep the same time."
    n = abs(hours)
    count = f"{n:g} hour" + ("" if n == 1 else "s")
    side = "ahead of" if hours > 0 else "behind"
    return f"{their[:1].upper() + their[1:]} clock is {count} {side} yours."


MEET_WHAT_IF = "Meeting up is only ever a what-if between you."


def owner_line(*, called: str, their: str, home: Place, detail: str, her: Place | None,
               instant: datetime, apart: bool) -> str:
    """Where he lives, for her private turns with him: his place, how far it is from hers and how
    his clock stands to hers (when hers is known), and, while they have never met, that meeting
    up is only a what-if."""
    parts = [f"{called} lives in {where_he_lives(home, detail)}."]
    if her is not None:
        parts.append(f"From you that's {travel(her, home)}.")
        parts.append(clock_gap(her, home, instant, their))
    if apart:
        parts.append(MEET_WHAT_IF)
    return " ".join(p for p in parts if p)
