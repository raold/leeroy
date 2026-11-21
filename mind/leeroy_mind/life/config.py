"""The `[life]` values in force, pinned for the whole process.

The talk engine's LifeService pins mind.toml's `[life]` (with the owner's availability world dial
over it) at mind start, at each login and when a world dial moves; Discord presence
(discord/presence.py) and the replies on her own time (discord/owntime.py) read the pin, so their
callers keep their signatures. Until pinned it is 0 and false: always present, today's
behaviour. The console has no mind.toml of the mind's to read, so the mind writes the
availability in force to meta META_KEY and the console pins what it finds there (`stored`).

`dm_wait_cap()` is `[life] dm_wait_cap_s` in force: the longest his DM waits for her answer
(discord/owntime.py), 0 (no cap) until pinned; meta CAP_KEY holds it for health (`stored_cap`)."""
import sqlite3

META_KEY = "life:availability"
AVAILABILITY_MAX = 10
CAP_KEY = "life:dm_wait_cap_s"
CAP_MAX = 3600
STEPPED_KEY = 'life:stepped-away'  # fresh active presence, never the historical health bounds

_availability = 0
_state = False
_dm_wait_cap = 0


def _valid(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= AVAILABILITY_MAX


def _valid_cap(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= CAP_MAX


def pin(availability: int, state: bool, dm_wait_cap_s: int = 0) -> None:
    """The values in force from now on: an availability that is not an integer 0-10 is 0, a
    state that is not True is False, a cap that is not an integer 0-3600 is 0 (no cap)."""
    global _availability, _state, _dm_wait_cap
    _availability = availability if _valid(availability) else 0
    _state = state is True
    _dm_wait_cap = dm_wait_cap_s if _valid_cap(dm_wait_cap_s) else 0


def availability() -> int:
    return _availability


def state_on() -> bool:
    return _state


def dm_wait_cap() -> int:
    """[life] dm_wait_cap_s in force, seconds; 0: no cap."""
    return _dm_wait_cap


def stored_cap(conn) -> int:
    """The cap the mind last put in force (meta CAP_KEY); 0 when there is none, it is not
    digits or the meta table cannot be read."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CAP_KEY,)).fetchone()
    except sqlite3.Error:
        return 0
    raw = row[0] if row else None
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or len(raw) > 4:
        return 0
    return int(raw) if _valid_cap(int(raw)) else 0


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
