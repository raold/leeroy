"""The arc's state. His play days are the arc's clock: a stage waits on the nights he played,
never on the calendar, so a week away pauses every arc."""
from datetime import date, datetime, timedelta, tzinfo

from leeroy_mind.clock import night_of
from leeroy_mind.storage.store import Store

_TICK = timedelta(microseconds=1)


def play_days(store: Store, since: float, until: float, tz: tzinfo) -> int:
    """How many nights (clock.night_of in `tz`, 06:00 to 06:00) he played between `since` and
    `until`: the distinct nights his sessions overlap inside that span. An open session runs to
    `until`; a session ends where it ended, so one that stops at 06:00 never counts the next
    night. Two sessions in one night count it once."""
    if until <= since:
        return 0
    with store.transaction():
        rows = store.conn.execute(
            "SELECT started, ended FROM session WHERE started < ? AND (ended IS NULL OR ended > ?)",
            (until, since)).fetchall()
    nights: set[date] = set()
    for started, ended in rows:
        lo, hi = max(since, started), min(until, until if ended is None else ended)
        if hi <= lo:
            continue
        day = date.fromisoformat(night_of(datetime.fromtimestamp(lo, tz)))
        last = date.fromisoformat(night_of(datetime.fromtimestamp(hi, tz) - _TICK))
        while day <= last:
            nights.add(day)
            day += timedelta(days=1)
    return len(nights)
