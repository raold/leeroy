"""The server's own event calendar: the world database's scheduled rows, read once a day
through the read-only SELECT path, and the start/stop the bridge publishes as they happen.

The core gives a holiday stage (holidayStage > 0) its dates from the client's Holidays.dbc and
its own date rules, not from the world row, whose start_time is NULL for those. Only rows the
core itself schedules from the table (stage 0 with a real start) get dates ahead; a holiday is
known to be on once the bridge says it started. Nothing here ever guesses a date."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import re

from leeroy_mind.text import strip_wow_markup

CAL_KEY = "culture:wowcal"            # {"fetched": ts, "events": [row, ...]}
ACTIVE_KEY = "culture:wowcal:active"  # {entry: {"first", "last"}} from the bridge's game_event start
STOPPED_KEY = "culture:wowcal:stopped"  # {entry: stopped_ts} from the bridge's game_event stop
BOOT_KEY = "culture:wowcal:boot"     # {"started", "seen"}: the worldserver boot the last hello named
META_KEYS = (CAL_KEY, ACTIVE_KEY, STOPPED_KEY, BOOT_KEY)
FETCH_AT = (8, 5)                     # the daily read, local time
RETRY_S = 3600.0
STALE_SLACK_S = 6 * 3600.0            # a start whose stop was never seen ages out after its length
BOOT_GRACE_S = 300.0                  # a new boot's replayed starts are in by then
SEED_AHEAD_DAYS = 3
CARD_AHEAD_DAYS = 7
CARD_ROWS = 4
EARLIEST = 978307200.0                # 2001-01-01: the core's own floor for a real start_time
MAX_ENTRY = 255                       # game_event.eventEntry is a tinyint
FIXED = ("Darkmoon Faire (Elwynn Forest)", "Darkmoon Faire (Mulgore)",
         "Darkmoon Faire (Terokkar Forest)", "Stranglethorn Fishing Extravaganza Fishing Pools",
         "Kalu'ak Fishing Derby Fishing Pools")


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


SQL = ("SELECT eventEntry, UNIX_TIMESTAMP(start_time), UNIX_TIMESTAMP(end_time), occurence, length, "
       "holiday, holidayStage, description FROM game_event WHERE world_event = 0 AND "
       "(holiday <> 0 OR description IN (" + ", ".join(map(_quoted, FIXED)) + "))")


@dataclass(frozen=True, slots=True)
class CalendarRow:
    entry: int
    name: str
    start: float | None   # None: the core does not schedule it from this row
    end: float | None
    occurence: int        # minutes
    length: int           # minutes
    scheduled: bool


@dataclass(frozen=True, slots=True)
class WowEvent:
    entry: int
    name: str
    starts: float
    ends: float | None    # None: on, with no end the world row can tell


def display_name(description) -> str | None:
    """The event as people say it; preparation stages are not events of their own."""
    if not isinstance(description, str) or len(description) > 200:
        return None
    clean = " ".join("".join(c for c in strip_wow_markup(description) if c.isprintable()).split())
    if not clean or len(clean) > 80 or "Building" in clean or "|" in clean:
        return None
    clean = re.sub(r" Fishing Pools$", "", clean).rstrip("!").strip()
    return clean or None


def _ts(value):
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("calendar time")
    return number


def _row(entry, name, start, end, occurence, length, stage) -> CalendarRow | None:
    if type(entry) is not int or not 0 < entry <= MAX_ENTRY or name is None:
        return None
    if type(occurence) is not int or type(length) is not int or occurence < 0 or length < 0:
        return None
    scheduled = (stage == 0 and start is not None and start >= EARLIEST and 0 < length < occurence
                 and (end is None or end > start))
    return CalendarRow(entry, name, start if scheduled else None, end if scheduled else None,
                       occurence, length, scheduled)


def parse_rows(rows) -> list[CalendarRow]:
    """mysql --batch rows of SQL; malformed rows are left out, never repaired."""
    found = {}
    for raw in list(rows)[:4 * MAX_ENTRY]:
        try:
            if len(raw) != 8:
                continue
            row = _row(int(raw[0]), display_name(raw[7]), _ts(raw[1]), _ts(raw[2]),
                       int(raw[3]), int(raw[4]), int(raw[6]))
        except (TypeError, ValueError, OverflowError):
            continue
        if row is not None:
            found[row.entry] = row
    return [found[e] for e in sorted(found)]


def read_calendar(app, *, query=None) -> list[CalendarRow]:
    """One SELECT on the world database through library/mysql (a read-only session with a server
    time limit), with WorldDatabaseInfo from worldserver.conf. Raises on any failure; callers
    log only the exception's class, since a client error can carry server text."""
    from leeroy_mind.guild.config import load_guild_config
    from leeroy_mind.library import mysql
    from leeroy_mind.personas.roster import read_db_info
    cfg = getattr(getattr(app, "guild", None), "cfg", None) or load_guild_config(app.data_dir / "mind.toml")
    db = read_db_info(cfg.worldserver_conf, key="WorldDatabaseInfo")
    mysql.check_select(SQL)
    return parse_rows((query or mysql.query)(db, SQL, cfg.mysql_exe, timeout=10, max_ms=5000))


def due(fetched: float | None, local: datetime) -> bool:
    """A read is due once a day, after FETCH_AT local, and at once with nothing cached."""
    boundary = local.replace(hour=FETCH_AT[0], minute=FETCH_AT[1], second=0, microsecond=0)
    if local < boundary:
        boundary -= timedelta(days=1)
    return fetched is None or fetched < boundary.timestamp()


def _json(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def calendar_value(rows: list[CalendarRow], fetched: float) -> str:
    return json.dumps({"fetched": fetched, "events": [
        {"entry": r.entry, "name": r.name, "start": r.start, "end": r.end, "occurence": r.occurence,
         "length": r.length, "scheduled": r.scheduled} for r in rows]}, sort_keys=True)


def load_calendar(raw) -> tuple[float | None, list[CalendarRow]] | None:
    """(fetched, rows) from CAL_KEY's value; None when there is none or it cannot be read."""
    value = _json(raw)
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        return None
    fetched = value.get("fetched")
    rows = []
    for item in value["events"][:MAX_ENTRY]:
        if not isinstance(item, dict):
            continue
        try:
            start, end = _ts(item.get("start")), _ts(item.get("end"))
            row = _row(item.get("entry"), display_name(item.get("name")), start, end,
                       item.get("occurence"), item.get("length"), 0 if item.get("scheduled") is True else 1)
        except (TypeError, ValueError, OverflowError):
            continue
        if row is not None:
            rows.append(row)
    return (fetched if type(fetched) in (int, float) and math.isfinite(fetched) else None), rows


def load_times(raw) -> dict[int, float]:
    """STOPPED_KEY: {entry: ts}, bad items left out."""
    value = _json(raw)
    out = {}
    if isinstance(value, dict):
        for key, ts in value.items():
            if (isinstance(key, str) and key.isdecimal() and 0 < int(key) <= MAX_ENTRY
                    and type(ts) in (int, float) and math.isfinite(ts)):
                out[int(key)] = float(ts)
    return out


def _times_value(times: dict[int, float]) -> str:
    return json.dumps({str(k): times[k] for k in sorted(times)}, sort_keys=True)


def load_active(raw) -> dict[int, tuple[float, float]]:
    """ACTIVE_KEY: {entry: (first, last)}: the run's first start, which a start the core
    publishes again at a worldserver boot never moves, and the latest start seen."""
    value = _json(raw)
    out = {}
    if isinstance(value, dict):
        for key, run in value.items():
            if not (isinstance(key, str) and key.isdecimal() and 0 < int(key) <= MAX_ENTRY):
                continue
            if type(run) in (int, float):
                run = {"first": run, "last": run}
            if (isinstance(run, dict) and all(type(run.get(k)) in (int, float) and math.isfinite(run[k])
                                              for k in ("first", "last")) and run["first"] <= run["last"]):
                out[int(key)] = (float(run["first"]), float(run["last"]))
    return out


def _active_value(active: dict[int, tuple[float, float]]) -> str:
    return json.dumps({str(k): {"first": active[k][0], "last": active[k][1]} for k in sorted(active)},
                      sort_keys=True)


def load_boot(raw) -> tuple[float, float] | None:
    """BOOT_KEY: (the worldserver process's start, when the mind first saw that boot)."""
    value = _json(raw)
    if (isinstance(value, dict) and all(type(value.get(k)) in (int, float) and math.isfinite(value[k])
                                        for k in ("started", "seen"))):
        return float(value["started"]), float(value["seen"])
    return None


def note_boot(store, started_ms, now: float) -> bool:
    """A hello's started_ms; True when it names a worldserver process the mind had not seen.
    0 (an older native) changes nothing."""
    if type(started_ms) is not int or started_ms <= 0:
        return False
    started = started_ms / 1000
    with store.transaction():
        boot = load_boot(store.get_meta(BOOT_KEY))
        if boot is not None and boot[0] == started:
            return False
        store.set_meta(BOOT_KEY, json.dumps({"started": started, "seen": now}, sort_keys=True))
    return True


def record(store, entry, state, ts: float) -> bool | None:
    """The bridge's game_event start/stop into meta, in one transaction, for the cached
    calendar's entries only (the server's many trivial events write nothing). None: no
    calendar is cached yet, so the caller keeps the event until the first read. A start of a
    run that is still within its length (the core starts every running event again at each
    worldserver boot) keeps the run's first start and only moves its latest one."""
    if type(entry) is not int or not 0 < entry <= MAX_ENTRY or state not in ("start", "stop"):
        return False
    if type(ts) not in (int, float) or not math.isfinite(ts):
        return False
    with store.transaction():
        cached = load_calendar(store.get_meta(CAL_KEY))
        if cached is None:
            return None
        row = next((r for r in cached[1] if r.entry == entry), None)
        if row is None:
            return False
        active, stopped = load_active(store.get_meta(ACTIVE_KEY)), load_times(store.get_meta(STOPPED_KEY))
        if state == "start":
            run = active.get(entry)
            if run is not None and run[1] == ts:
                return False
            if run is not None and run[0] <= ts <= run[0] + row.length * 60 + STALE_SLACK_S:
                active[entry] = (run[0], max(run[1], ts))
            else:
                active[entry] = (ts, ts)
        else:
            active.pop(entry, None)
            stopped[entry] = ts
        store.set_meta(ACTIVE_KEY, _active_value(active))
        store.set_meta(STOPPED_KEY, _times_value(stopped))
    return True


def store_calendar(store, rows: list[CalendarRow], fetched: float, pending=()) -> None:
    """Cache a fresh read, keep only the bridge times of its entries, then apply the
    (entry, state, ts) events that arrived before any calendar was cached, in order."""
    known = {r.entry for r in rows}
    with store.transaction():
        store.set_meta(CAL_KEY, calendar_value(rows, fetched))
        active = load_active(store.get_meta(ACTIVE_KEY))
        if active and set(active) - known:
            store.set_meta(ACTIVE_KEY, _active_value({k: v for k, v in active.items() if k in known}))
        stopped = load_times(store.get_meta(STOPPED_KEY))
        if stopped and set(stopped) - known:
            store.set_meta(STOPPED_KEY, _times_value({k: v for k, v in stopped.items() if k in known}))
        for entry, state, ts in pending:
            record(store, entry, state, ts)


def occurrences(rows, *, now: float, ahead_days: int = 21) -> list[WowEvent]:
    """The core's own rule (GameEventMgr::CheckOneGameEvent): an event runs while
    (now - start) % occurence < length, after start and before end_time."""
    out = []
    horizon = now + ahead_days * 86400
    for r in rows:
        if not r.scheduled:
            continue
        period, span = r.occurence * 60.0, r.length * 60.0
        begin = r.start + max(0, math.floor((now - r.start) / period)) * period
        if begin + span <= now:
            begin += period
        for _ in range(64):
            if begin > horizon or r.end is not None and begin >= r.end:
                break
            ends = begin + span if r.end is None else min(begin + span, r.end)
            out.append(WowEvent(r.entry, r.name, begin, ends))
            begin += period
    return sorted(out, key=lambda e: (e.starts, e.entry))


def schedule(rows, active: dict[int, tuple[float, float]], stopped: dict[int, float], *, now: float,
             ahead_days: int, boot: tuple[float, float] | None = None) -> list[tuple[WowEvent, bool]]:
    """Each entry's running or next occurrence as (event, on). The bridge wins: a start it saw
    is on whatever the table says, and a stop it saw after an occurrence began ends it.

    A run needs the current worldserver boot to have started it, once BOOT_GRACE_S after that
    boot's hello has let its replay in: an event that ended while the server was down gets no
    stop, the boot just never starts it again. A run never outlives its length plus
    STALE_SLACK_S from its first start. A run the table also dates keeps the table's start, so
    "starts" and "on" of one occurrence are one subject."""
    by_entry = {r.entry: r for r in rows}
    found: dict[int, tuple[WowEvent, bool]] = {}
    for event in occurrences(rows, now=now, ahead_days=ahead_days):
        on = event.starts <= now < event.ends
        stop = stopped.get(event.entry)
        if on and stop is not None and event.starts <= stop <= now:
            continue
        if event.ends <= now:
            continue
        found.setdefault(event.entry, (event, on))
    for entry, (first, last) in active.items():
        row = by_entry.get(entry)
        stop = stopped.get(entry)
        if row is None or first > now or stop is not None and stop >= first:
            continue
        if first + row.length * 60 + STALE_SLACK_S < now:
            continue  # a run whose stop the mind never saw
        if boot is not None and last < boot[0] - 1 and now - boot[1] >= BOOT_GRACE_S:
            continue  # the current boot never started it: it ended while the server was down
        known = found.get(entry)
        dated = known[0] if known and known[1] and known[0].starts - STALE_SLACK_S <= first else None
        ends = dated.ends if dated is not None and dated.ends > now else None
        found[entry] = (WowEvent(entry, row.name, dated.starts if dated is not None else first, ends), True)
    return sorted(found.values(), key=lambda pair: (not pair[1], pair[0].starts, pair[0].entry))


def from_meta(values: dict, *, now: float, ahead_days: int) -> list[tuple[WowEvent, bool]]:
    """schedule() over the META_KEYS values as read ({key: value})."""
    cached = load_calendar(values.get(CAL_KEY))
    if cached is None:
        return []
    return schedule(cached[1], load_active(values.get(ACTIVE_KEY)), load_times(values.get(STOPPED_KEY)),
                    now=now, ahead_days=ahead_days, boot=load_boot(values.get(BOOT_KEY)))


def _day(moment: datetime, local: datetime, *, at: bool) -> str:
    days = (moment.date() - local.date()).days
    word = ("today" if days == 0 else "tomorrow" if days == 1 else
            f"{moment:%A}" if 1 < days < 7 else f"{moment:%B} {moment.day}")
    return word + (f" at {moment:%H:%M}" if at and days <= 1 else "")


def describe(event: WowEvent, on: bool, local: datetime) -> str:
    """'<name> is on until <day>' or '<name> starts <day>', in the mind's local time."""
    if on:
        if event.ends is None:
            return f"{event.name} is on now"
        end = datetime.fromtimestamp(event.ends, local.tzinfo)
        if end.date() == local.date():
            return f"{event.name} is on until {end:%H:%M} today"
        return f"{event.name} is on until {_day(end, local, at=False)}"
    return f"{event.name} starts {_day(datetime.fromtimestamp(event.starts, local.tzinfo), local, at=True)}"


def library_title(name: str) -> str:
    """The library's event page title: the location in brackets is the event's copy."""
    return re.sub(r"\s*\([^()]*\)$", "", name)
