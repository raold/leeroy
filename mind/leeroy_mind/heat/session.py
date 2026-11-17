"""The session override: one sentence from him pins a member's heat low or high for the evening.

Read by fixed patterns only, in a whisper or a one-to-one DM of his to a member; no model call
decides it. "clean" caps her heat (table.CLEAN_CAP); "open" sets it to 10 (table.OPEN_HEAT), and
only the dial: never her stance, the photo lists, the gate, the age rules or the public cap.
The console's button sets either for everyone.

It ends at his logout when it was said in game; said in a Discord DM (or set from the console
while he is not in game) it ends IDLE_S after his last line in that conversation or at 06:00
local, whichever is first. A mind start while he is offline clears every one. It lives in meta
(`heat:session:<guid>`, `heat:session:all`), not in the tuning's change log: it is a mood of the
evening, not a setting. Logs carry the guid and the code, never his words."""
import json
import re
from datetime import timedelta

KEY = "heat:session:{guid}"
ALL = "heat:session:all"
PREFIX = "heat:session:"
IDLE_S = 6 * 3600.0
MORNING_HOUR = 6
MODES = ("clean", "open")
MEDIA = ("game", "discord")
_APOS = "['’ʼ]"
CLEAN_RE = re.compile(r"\b(?:keep it clean|not tonight|behave tonight)\b", re.IGNORECASE)
OPEN_RE = re.compile(rf"\b(?:no limits tonight|don{_APOS}?t hold back tonight)\b", re.IGNORECASE)
CLEAR_RE = re.compile(r"\bback to normal\b", re.IGNORECASE)


def parse(text: str) -> str | None:
    """"clean", "open" or "clear" when his line says one of the fixed phrases, else None. A line
    with both a clean and an open phrase is read as clean."""
    text = " ".join((text or "").split())
    if CLEAR_RE.search(text):
        return "clear"
    if CLEAN_RE.search(text):
        return "clean"
    if OPEN_RE.search(text):
        return "open"
    return None


def _morning(local, now: float) -> float:
    """The next 06:00 local after `local` (the clock's local time at `now`), as a timestamp."""
    target = local.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return now + (target - local).total_seconds()


def _expired(rec: dict, now: float) -> bool:
    """A Discord-medium override past its idle limit or its morning. One said in game ends only
    at his logout (SessionBook.end_game)."""
    return rec["medium"] == "discord" and (
        now - rec["last"] >= IDLE_S or (rec.get("until") is not None and now >= rec["until"]))


def _valid(value: str) -> dict | None:
    try:
        rec = json.loads(value)
        if rec["mode"] in MODES and rec["medium"] in MEDIA:
            float(rec["at"]), float(rec["last"])
            return rec
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _shown(rows: dict[str, dict], now: float) -> dict:
    out = {"all": None, "members": {}}
    for key, rec in rows.items():
        if _expired(rec, now):
            continue
        row = {"mode": rec["mode"], "ends": "logout" if rec["medium"] == "game" else "morning"}
        if key == ALL:
            out["all"] = row
        else:
            out["members"][key[len(PREFIX):]] = row
    return out


def read(conn, now: float) -> dict:
    """The overrides in force as stored, read-only (the console's page): everyone's and each
    member's mode with how it ends. Nothing is written or dropped here, and none of his words
    are stored at all."""
    try:
        found = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?",
                             (PREFIX + "%",)).fetchall()
    except Exception:  # a database without the meta table
        return {"all": None, "members": {}}
    rows = {k: rec for k, v in found if (rec := _valid(v)) is not None}
    return _shown(rows, now)


def mode_in(view: dict, guid: int) -> str | None:
    """A member's mode in a view (SessionBook.view, read): hers, else everyone's; clean wins."""
    modes = {r["mode"] for r in (view["members"].get(str(guid)), view["all"]) if r}
    return "clean" if "clean" in modes else "open" if "open" in modes else None


class SessionBook:
    """The overrides in force, kept in memory and in meta. All of it runs on the mind's loop."""

    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self._rows: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        for key, value in self.store.conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?", (PREFIX + "%",)).fetchall():
            rec = _valid(value)
            if rec is not None:
                self._rows[key] = rec

    def _key(self, guid: int | None) -> str:
        return ALL if guid is None else KEY.format(guid=int(guid))

    def _drop(self, key: str) -> None:
        self._rows.pop(key, None)
        with self.store.transaction():
            self.store.conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def set(self, guid: int | None, mode: str, *, medium: str, ack: bool = False) -> None:
        """Start (or replace) an override for one member, or for everyone (guid None). `ack`:
        she says so once, in her own words, on her next private turn to him."""
        if mode not in MODES or medium not in MEDIA:
            raise ValueError("mode is clean or open; medium is game or discord")
        now = self.clock.now()
        rec = {"mode": mode, "medium": medium, "at": now, "last": now,
               "until": _morning(self.clock.local(), now) if medium == "discord" else None,
               "ack": bool(ack and guid is not None)}
        key = self._key(guid)
        self._rows[key] = rec
        self.store.set_meta(key, json.dumps(rec, sort_keys=True))

    def clear(self, guid: int | None = None, *, everything: bool = False) -> int:
        """End one member's override, everyone's (guid None), or every row (`everything`). How
        many ended."""
        keys = [k for k in (list(self._rows) if everything else [self._key(guid)])
                if k in self._rows]
        for k in keys:
            self._drop(k)
        return len(keys)

    def _live(self, key: str, now: float) -> dict | None:
        rec = self._rows.get(key)
        if rec is None:
            return None
        if _expired(rec, now):
            self._drop(key)
            return None
        return rec

    def mode(self, guid: int, now: float | None = None) -> str | None:
        """The override in force for `guid`: hers, else everyone's; clean wins when both are."""
        now = self.clock.now() if now is None else now
        own, shared = self._live(self._key(guid), now), self._live(ALL, now)
        modes = {r["mode"] for r in (own, shared) if r is not None}
        return "clean" if "clean" in modes else "open" if "open" in modes else None

    def touch(self, guid: int, now: float | None = None) -> None:
        """A line of his in his conversation with `guid`: her idle limit starts again (in
        memory; a restart while he is away clears the session anyway)."""
        now = self.clock.now() if now is None else now
        for key in (self._key(guid), ALL):
            rec = self._live(key, now)
            if rec is not None:
                rec["last"] = now

    def take_ack(self, guid: int) -> str | None:
        """The mode she has yet to acknowledge, once: "clean" or "open", then None."""
        key = self._key(guid)
        rec = self._live(key, self.clock.now())
        if rec is None or not rec.get("ack"):
            return None
        rec["ack"] = False
        self.store.set_meta(key, json.dumps(rec, sort_keys=True))
        return rec["mode"]

    def end_game(self) -> int:
        """His logout: every override said in game ends. How many."""
        keys = [k for k, r in self._rows.items() if r["medium"] == "game"]
        for k in keys:
            self._drop(k)
        return len(keys)

    def view(self, now: float | None = None) -> dict:
        """For the page: everyone's mode and each member's, with how each ends. No words of his."""
        now = self.clock.now() if now is None else now
        for key in list(self._rows):
            self._live(key, now)  # drops the ones that ended
        return _shown(self._rows, now)
