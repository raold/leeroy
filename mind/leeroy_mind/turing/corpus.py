"""The lines the judge compares: his own typed lines and the members', per channel family.

- **game**: guild, officer, party, raid and say/yell chat in WoW (chat_line).
- **private**: his whispers with a member (chat_line) and his Discord DMs (discord_message).
  Whispers between two members are never his to see and are left out.
- **discord**: the Discord server's channels and group DMs (discord_message). A group DM
  (`gdm:<id>`) always has him among its readers, so it counts here as a channel; only numbers
  and fixed labels from it ever leave the stylometry, and the blind judge keeps no phrase from
  it (judge.public).

Left out everywhere: spoken lines (voice rooms and Discord voice notes), addon pastes (Recount
and the like, his only), deleted and bot messages, and chat_line's mirror of Discord rows (the
Discord store is the source for those). His lines are origin "human" from his guid; a member's
are origin "mind". Read only: every query is a SELECT."""
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from leeroy_mind.talk.pastes import is_addon_row

FAMILIES = ("game", "private", "discord")
FAMILY_LABELS = {"game": "Game chat", "private": "Whispers and DMs", "discord": "Discord channels"}
GAME = frozenset({"guild", "officer", "party", "raid", "raid_warning", "say", "yell"})
DISCORD_PREFIXES = ("discord:", "dm:", "gdm:")
_CHAT = ("SELECT id, ts, channel, from_guid, to_guid, text, origin FROM chat_line "
         "WHERE origin IN ('human','mind') AND ")
_DISCORD = ("SELECT id, ts, channel, author, text, origin FROM discord_message "
            "WHERE deleted IS NULL AND origin IN ('human','mind') AND ")


@dataclass(frozen=True)
class Line:
    ref: str        # "c:<chat_line id>" or "d:<discord_message id>"
    ts: float
    family: str
    conv: str       # the conversation: a channel, a whisper pair or a DM
    channel: str    # as stored: guild, whisper, dm:<guid>, gdm:<id>, or a Discord channel id
    speaker: int
    human: bool
    text: str


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _kept(text) -> bool:
    return isinstance(text, str) and bool(text.strip()) and not is_addon_row(text)


def _chat(row, human: int) -> Line | None:
    ident, ts, channel, sender, to, text, origin = row
    if not _kept(text) or channel.startswith(DISCORD_PREFIXES) or sender <= 0:
        return None
    his = origin == "human"
    if his != (sender == human):
        return None
    if channel in GAME:
        family, conv = "game", channel
    elif channel == "whisper" and human in (sender, to):
        family, conv = "private", f"w:{to if his else sender}"
    else:
        return None
    return Line(f"c:{ident}", ts, family, conv, channel, sender, his, text)


def _discord(row, human: int) -> Line | None:
    ident, ts, channel, author, text, origin = row
    his = origin == "human"
    if not _kept(text) or his != (author == human):
        return None
    if channel.startswith("dm:"):
        family, conv = "private", channel
    else:
        family = "discord"
        conv = channel if channel.startswith("gdm:") else f"discord:{channel}"
    return Line(f"d:{ident}", ts, family, conv, channel, author, his, text)


def _spoken(names: set[str]) -> str:
    return " AND id NOT IN (SELECT message FROM discord_voice)" if "discord_voice" in names else ""


def read_lines(conn: sqlite3.Connection, *, human: int, start: float, end: float) -> list[Line]:
    """His lines and the members' from `start` to `end` (unix seconds), by time."""
    names = tables(conn)
    out: list[Line] = []
    if "chat_line" in names:
        out += filter(None, (_chat(r, human) for r in conn.execute(
            _CHAT + "ts >= ? AND ts < ?", (start, end))))
    if "discord_message" in names:
        out += filter(None, (_discord(r, human) for r in conn.execute(
            _DISCORD + "ts >= ? AND ts < ?" + _spoken(names), (start, end))))
    out.sort(key=lambda line: (line.ts, line.ref))
    return out


def read_refs(conn: sqlite3.Connection, refs: Iterable[str], *, human: int) -> list[Line] | None:
    """The lines a saved window names, in its order; None when one is gone or no longer
    qualifies (a Discord message deleted since)."""
    refs = list(refs)
    names = tables(conn)
    found: dict[str, Line] = {}
    for prefix, table, query, make in (("c", "chat_line", _CHAT, _chat),
                                       ("d", "discord_message", _DISCORD, _discord)):
        ids = [int(r[2:]) for r in refs if r.startswith(prefix + ":") and r[2:].isdigit()]
        if not ids or table not in names:
            continue
        extra = _spoken(names) if prefix == "d" else ""
        for row in conn.execute(query + "id IN (SELECT value FROM json_each(?))" + extra,
                                (json.dumps(ids),)):
            line = make(row, human)
            if line is not None:
                found[line.ref] = line
    if any(r not in found for r in refs):
        return None
    return [found[r] for r in refs]


def names_by_guid(conn: sqlite3.Connection, *, human: int, his: Iterable[str] = ()) -> dict[int, str]:
    """guid -> the name the corpus may mention: the stored roster and the names chat stored,
    with his first configured name for his guid."""
    names = tables(conn)
    out: dict[int, str] = {}
    if "cast_member" in names:
        out.update((g, n) for g, n in conn.execute("SELECT guid, name FROM cast_member"))
    if "chat_line" in names:
        for g, n in conn.execute("SELECT from_guid, max(from_name) FROM chat_line "
                                 "WHERE origin IN ('human','mind') GROUP BY from_guid"):
            out.setdefault(g, n)
    his = [h for h in his if h]
    if his:
        out[human] = his[0]
    return {g: n for g, n in out.items() if isinstance(n, str) and n.strip()}


def name_pattern(all_names: Iterable[str]) -> re.Pattern | None:
    """One case-insensitive pattern for every name of 3+ characters, whole words only (a
    possessive "Arkon's" is still the name), longest first."""
    words = sorted({n.strip().casefold() for n in all_names if n and len(n.strip()) >= 3},
                   key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in words) + r")(?!\w)",
                      re.IGNORECASE)
