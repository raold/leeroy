"""Wall-clock windows, including windows that cross midnight or a DST change."""
from datetime import datetime, time, timedelta


def in_window(local: datetime, window: tuple[time, time]) -> bool:
    start, end = window
    wall = local.time()
    return start <= wall < end if start < end else wall >= start or wall < end


def window_bounds(local: datetime, window: tuple[time, time]) -> tuple[float, float]:
    """The window containing local, or the next window, in unix seconds."""
    start, end = window
    day = local.date()
    if in_window(local, window):
        if start > end and local.time() < end:
            day -= timedelta(days=1)
    elif local.time() >= start:
        day += timedelta(days=1)
    end_day = day + timedelta(days=start > end)
    return (datetime.combine(day, start, tzinfo=local.tzinfo).timestamp(),
            datetime.combine(end_day, end, tzinfo=local.tzinfo).timestamp())


def night_key(local: datetime, window: tuple[time, time]) -> str:
    start = window[0]
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date().isoformat()
