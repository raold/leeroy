"""Wall-clock windows, including windows that cross midnight or a DST change."""
from datetime import date, datetime, time, timedelta

from leeroy_mind.clock import SystemClock, night_of


def in_window(local: datetime, window: tuple[time, time]) -> bool:
    start, end = window
    wall = local.time()
    return start <= wall < end if start < end else wall >= start or wall < end


def _bounds(day, local, window, clock):
    start, end = window
    # SystemClock.local() has a fixed offset for that instant. Resolve each wall
    # boundary separately through the host, including its DST rule for that day.
    tz = None if isinstance(clock, SystemClock) else local.tzinfo
    return (datetime.combine(day, start, tzinfo=tz).timestamp(),
            datetime.combine(day + timedelta(days=start > end), end, tzinfo=tz).timestamp())


def window_bounds(local: datetime, window: tuple[time, time], *, clock=None) -> tuple[float, float]:
    """The window containing local, or the next window, in unix seconds."""
    start, end = window
    day = local.date()
    if in_window(local, window):
        if start > end and local.time() < end:
            day -= timedelta(days=1)
    elif local.time() >= start:
        day += timedelta(days=1)
    return _bounds(day, local, window, clock)


def night_key(local: datetime, window: tuple[time, time]) -> str:
    start = window[0]
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date().isoformat()


def latest_window_bounds(local: datetime, window: tuple[time, time], *, clock=None) -> tuple[float, float]:
    """The most recently started window, including its overrun (never tomorrow's)."""
    return _bounds(date.fromisoformat(night_key(local, window)), local, window, clock)


def reporting_night(local: datetime, window: tuple[time, time]) -> str:
    """Keep stages' 06:00 reporting date, anchored to this whole scheduling window."""
    start = datetime.combine(date.fromisoformat(night_key(local, window)), window[0])
    return night_of(start)
