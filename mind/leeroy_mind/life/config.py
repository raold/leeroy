"""The `[life]` values in force, pinned for the whole process.

The talk engine's LifeService pins mind.toml's `[life]` (with the owner's availability world dial
over it) at mind start, at each login and when a world dial moves; Discord presence
(discord/presence.py) and the replies on her own time (discord/owntime.py) read the pin, so their
callers keep their signatures. Until pinned it is 0 and false: always present, today's
behaviour. The console has no mind.toml of the mind's to read, so the mind writes the
availability in force to meta META_KEY and the console pins what it finds there (`stored`)."""
import sqlite3

META_KEY = "life:availability"
AVAILABILITY_MAX = 10

_availability = 0
_state = False


def _valid(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= AVAILABILITY_MAX


def pin(availability: int, state: bool) -> None:
    """The values in force from now on: an availability that is not an integer 0-10 is 0, a
    state that is not True is False."""
    global _availability, _state
    _availability = availability if _valid(availability) else 0
    _state = state is True


def availability() -> int:
    return _availability


def state_on() -> bool:
    return _state


def parse(raw) -> int:
    """meta META_KEY's text as an availability: an integer 0-10 written as digits, else 0."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or len(raw) > 2:
        return 0
    value = int(raw)
    return value if _valid(value) else 0


def stored(conn) -> int:
    """The availability the mind last put in force (meta META_KEY); 0 when there is none or the
    meta table cannot be read."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    except sqlite3.Error:
        return 0
    return parse(row[0]) if row else 0
