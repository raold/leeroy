"""Private inputs and content-free detector results."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from leeroy_mind.realm.calendar_time import TIMEZONE
from leeroy_mind.retrieval import cosine
from leeroy_mind.storage.rows import unpack_vec
from leeroy_mind.text import strip_wow_markup

DAY_ZONE = ZoneInfo(TIMEZONE)


@dataclass(frozen=True)
class Line:
    id: int
    ts: float
    guid: int
    to_guid: int
    thread_id: int | None
    channel: str
    origin: str
    map: int
    zone: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class Flag:
    line_id: int
    guid: int
    rule: str
    related_id: int | None = None
    score: float | None = None


def words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", strip_wow_markup(text).casefold()))


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, DAY_ZONE).date().isoformat()


def evening(ts: float) -> str | None:
    local = datetime.fromtimestamp(ts, DAY_ZONE)
    if 6 <= local.hour < 18:
        return None
    return (local.date() - timedelta(days=int(local.hour < 6))).isoformat()


def similarity(a: bytes, b: bytes) -> float:
    return cosine(unpack_vec(a), unpack_vec(b))


def read_lines(conn, since: float, until: float, owner: int) -> tuple[list[Line], list[Line]]:
    # Five minutes before the window is context only, never counted as an evaluated line.
    rows = conn.execute(
        "SELECT id,ts,from_guid,to_guid,thread_id,channel,origin,map,zone,text FROM chat_line "
        "WHERE ts>=? AND ts<=? AND (origin='mind' OR (origin='human' AND from_guid=?)) ORDER BY ts,id",
        (since - 300, until, owner))
    context = [Line(*tuple(row)) for row in rows]
    lines = [line for line in context if line.origin == 'mind' and line.guid != owner and line.ts >= since]
    return lines, context
