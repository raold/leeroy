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
