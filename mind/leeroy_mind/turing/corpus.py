"""The lines the judge compares: his own typed lines and the members', per channel family.

- **game**: guild, officer, party, raid and say/yell chat in WoW (chat_line). A conversation is
  its channel within one of his sessions, and for say/yell also one place (map and zone), so two
  groups' chats are never stitched together. Party and raid lines count only where the console's
  feed shows them: in a session, his own or from a member counted in the line's audience (his
  group).
- **private**: his whispers with a member (chat_line) and his Discord DMs (discord_message).
  Whispers between two members are never his to see and are left out.
- **discord**: the Discord server's channels and group DMs (discord_message). A group DM
  (`gdm:<id>`) always has him among its readers, so it counts here as a channel; only numbers
  and fixed labels from it ever leave the stylometry, and the blind judge keeps no phrase from
  it (judge.public).

Left out everywhere: spoken lines (voice rooms; a whisper the voice service voiced, `voice_line`;
his push-to-talk transcribed into a whisper, `voice_heard`; Discord voice notes), addon pastes
(Recount and the like, his only), deleted and bot messages, and chat_line's mirror of Discord rows
(the Discord store is the source for those). His lines are origin "human" from his guid; a
member's are origin "mind". Read only: every query is a SELECT."""
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from leeroy_mind.talk.pastes import is_addon_row

FAMILIES = ("game", "private", "discord")
FAMILY_LABELS = {"game": "Game chat", "private": "Whispers and DMs", "discord": "Discord channels"}
GAME = frozenset({"guild", "officer", "party", "raid", "raid_warning", "say", "yell"})
GROUP = frozenset({"party", "raid", "raid_warning"})
LOCAL = frozenset({"say", "yell"})
DISCORD_PREFIXES = ("discord:", "dm:", "gdm:")
_CHAT = ("SELECT id, ts, channel, from_guid, to_guid, text, origin, session_id, map, zone, audience "
         "FROM chat_line WHERE origin IN ('human','mind') AND ")
_DISCORD = ("SELECT id, ts, channel, author, text, origin FROM discord_message "
            "WHERE deleted IS NULL AND origin IN ('human','mind') AND ")


@dataclass(frozen=True)
class Line:
    ref: str        # "c:<chat_line id>" or "d:<discord_message id>"
    ts: float
    family: str
    conv: str       # the conversation: a channel in a session, a whisper pair or a DM
    channel: str    # as stored: guild, whisper, dm:<guid>, gdm:<id>, or a Discord channel id
    speaker: int
    human: bool
    text: str


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _kept(text) -> bool:
    return isinstance(text, str) and bool(text.strip()) and not is_addon_row(text)


def _in_audience(audience, guid: int) -> bool:
    try:
        seen = json.loads(audience) if audience else None
    except (ValueError, TypeError):
        return False
    return isinstance(seen, list) and guid in seen


def _chat(row, human: int) -> Line | None:
    ident, ts, channel, sender, to, text, origin, session, map_, zone, audience = row
    if not _kept(text) or channel.startswith(DISCORD_PREFIXES) or sender <= 0:
        return None
    his = origin == "human"
    if his != (sender == human):
        return None
    if channel in GAME:
        if channel in GROUP and (session is None or not (his or _in_audience(audience, sender))):
            return None  # another group's chat, or one he was not in: never his to read
        place = f"#{map_}:{zone}" if channel in LOCAL else ""
        family, conv = "game", f"{channel}#{session if session is not None else '-'}{place}"
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


def _spoken_chat(names: set[str]) -> str:
    """A whisper the voice service voiced, or his push-to-talk transcribed: spoken, not typed."""
    return "".join(f" AND id NOT IN (SELECT line_id FROM {t})"
                   for t in ("voice_line", "voice_heard") if t in names)


def _spoken_discord(names: set[str]) -> str:
    return " AND id NOT IN (SELECT message FROM discord_voice)" if "discord_voice" in names else ""


def read_lines(conn: sqlite3.Connection, *, human: int, start: float, end: float) -> list[Line]:
    """His lines and the members' from `start` to `end` (unix seconds), by time."""
    names = tables(conn)
    out: list[Line] = []
    if "chat_line" in names:
        out += filter(None, (_chat(r, human) for r in conn.execute(
            _CHAT + "ts >= ? AND ts < ?" + _spoken_chat(names), (start, end))))
    if "discord_message" in names:
        out += filter(None, (_discord(r, human) for r in conn.execute(
            _DISCORD + "ts >= ? AND ts < ?" + _spoken_discord(names), (start, end))))
    out.sort(key=lambda line: (line.ts, line.ref))
    return out


def read_refs(conn: sqlite3.Connection, refs: Iterable[str], *, human: int) -> list[Line] | None:
    """The lines a saved window names, in its order; None when one is gone or no longer
    qualifies (a Discord message deleted since, a line since found to be spoken)."""
    refs = list(refs)
    names = tables(conn)
    found: dict[str, Line] = {}
    for prefix, table, query, make, spoken in (
            ("c", "chat_line", _CHAT, _chat, _spoken_chat),
            ("d", "discord_message", _DISCORD, _discord, _spoken_discord)):
        ids = [int(r[2:]) for r in refs if r.startswith(prefix + ":") and r[2:].isdigit()]
        if not ids or table not in names:
            continue
        for row in conn.execute(query + "id IN (SELECT value FROM json_each(?))" + spoken(names),
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


def persona_names(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(name, guid) for each persona's own real name, whole and first name alone: members use
    them with each other, on Discord most."""
    if "persona_core" not in tables(conn):
        return []
    out = []
    for guid, name in conn.execute("SELECT guid, json_extract(json, '$.identity.real_name') "
                                   "FROM persona_core WHERE json_valid(json)"):
        if isinstance(name, str) and name.strip():
            out.append((name.strip(), guid))
            first = name.split()[0]
            if first != name.strip():
                out.append((first, guid))
    return out


def name_pattern(all_names: Iterable[str], *, min_len: int = 3) -> re.Pattern | None:
    """One case-insensitive pattern for every name of `min_len`+ characters, whole words only,
    longest first, with an optional possessive or plural after it: "Arkon's", "Arkon’s" and
    "arkons" are all the name (group 1 is the name, the rest of the match its ending)."""
    words = sorted({n.strip().casefold() for n in all_names if n and len(n.strip()) >= min_len},
                   key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in words) + r")(?:['’]?s)?(?!\w)",
                      re.IGNORECASE)
