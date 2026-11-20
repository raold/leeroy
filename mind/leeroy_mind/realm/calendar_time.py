"""The product's weekly cadence and actual local write date.

These boundaries describe a product period, not a native instance lockout.
"""
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .records import ProductPeriod


TIMEZONE = "America/New_York"
CADENCE = "product-tue04-america-new_york-v1"
_MAX_SECONDS = 253402300799.999
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ProductCalendarUnavailable(RuntimeError):
    """The named product timezone is unavailable on this installation."""


def _local(seconds: float) -> datetime:
    if (type(seconds) not in (int, float) or not 0 <= seconds <= _MAX_SECONDS
            or not math.isfinite(seconds)):
        raise ValueError("seconds must be a finite Unix time in the supported range")
    try:
        zone = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError as error:
        raise ProductCalendarUnavailable(f"timezone unavailable: {TIMEZONE}") from error
    try:
        # Floor fractional microseconds so an instant before a boundary stays before it.
        # Integer components also avoid CRT limits and large-epoch float multiplication.
        if type(seconds) is int:
            whole_seconds, microseconds = seconds, 0
        else:
            numerator, denominator = seconds.as_integer_ratio()
            whole_seconds, remainder = divmod(numerator, denominator)
            microseconds = remainder * 1_000_000 // denominator
        return (_UTC_EPOCH + timedelta(seconds=whole_seconds, microseconds=microseconds)).astimezone(zone)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("seconds cannot be represented in the product timezone") from error


def product_period(seconds: float) -> ProductPeriod:
    """Return the half-open Tuesday 04:00 period containing ``seconds``."""
    local = _local(seconds)
    try:
        start_day = local.date() - timedelta(days=(local.weekday() - 1) % 7)
        starts = datetime.combine(start_day, time(4), tzinfo=local.tzinfo)
        if local < starts:
            start_day -= timedelta(days=7)
            starts = datetime.combine(start_day, time(4), tzinfo=local.tzinfo)
        # Build the next boundary from its local date, independently of UTC duration.
        ends = datetime.combine(start_day + timedelta(days=7), time(4), tzinfo=local.tzinfo)
        return ProductPeriod(CADENCE, start_day.isoformat(), starts.timestamp(), ends.timestamp())
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("seconds has no representable complete product period") from error


def local_write_day(seconds: float) -> str:
    """Return the local calendar date at the supplied actual write time."""
    return _local(seconds).date().isoformat()


SERVER_TIME = "server time"  # how WoW players say a raid's time; members live in other zones
EVENING_HOUR = 17  # from here a day's time reads as its night ("tonight", "tomorrow night")
LATE_HOUR = 6  # before here a time is the night before's, late ("late tonight, at 1am")


def clock_words(when: datetime) -> str:
    """A time as players type it: "9pm", "9:30pm", "midnight", "noon"."""
    if when.minute == 0 and when.hour in (0, 12):
        return "midnight" if when.hour == 0 else "noon"
    hour = when.hour % 12 or 12
    minutes = f":{when.minute:02d}" if when.minute else ""
    return f"{hour}{minutes}{'am' if when.hour < 12 else 'pm'}"


def when_words(at: float, local: datetime) -> str:
    """When a calendar event is, said from `local` (now, aware) the way the guild would: "tonight
    at 9pm server time", "tomorrow night at 8pm server time", "on Thursday at 9pm server time",
    and a time after midnight as the night before's, late: "late tonight, at 1am server time"
    (2026-11-20: "Raid night at 03:00" was read as a 3am raid). A 12-hour clock and the server's
    zone, never a bare "03:00"."""
    when = datetime.fromtimestamp(at, local.tzinfo)
    clock = f"{clock_words(when)} {SERVER_TIME}"
    if when.hour < LATE_HOUR:  # the night before's, late
        night = (when - timedelta(hours=LATE_HOUR)).date()
        tonight = (local - timedelta(hours=LATE_HOUR)).date()
        days = (night - tonight).days
        said = ("tonight" if days == 0 else "tomorrow night" if days == 1
                else f"on {night:%A} night" if 1 < days < 7
                else f"on the night of {night:%A} {night.day} {night:%B}")
        return f"late {said}, at {clock}"
    days = (when.date() - local.date()).days
    evening = when.hour >= EVENING_HOUR
    said = (("tonight" if evening else "today") if days == 0
            else ("tomorrow night" if evening else "tomorrow") if days == 1
            else f"on {when:%A}" if 1 < days < 7
            else f"on {when:%A} {when.day} {when:%B}")
    return f"{said} at {clock}"
