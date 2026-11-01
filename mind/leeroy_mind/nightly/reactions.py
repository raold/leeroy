"""Deterministic responses to cast chat, shared by attraction and the night's feedback."""
import json
import re
from dataclasses import dataclass
from typing import Literal

from leeroy_mind.personas.screens import has_word

LAUGH_RE = re.compile(r"(?<!\w)(?:lo+l|lmf?ao|rofl|(?:ha){2,}|(?:he){2,}|xd)(?!\w)|:d\b", re.I)
NO_RE = re.compile(r"\b(not now|not rn|not right now|i'?m busy|busy rn|shh+|stop it|not the time|focus up|cut it out)\b", re.I)
LIFE_RE = re.compile(r"\b(work|job|boss|shift|school|class|exams?|finals|sister|brother|mum|mom|dad|parents|wife|girlfriend|kids?|dog|cat|birthday|holiday|vacation|trip|moving|house|car|sick|doctor|interview)\b", re.I)
REPLY_S = 90.0
WHISPER_BACK_S = 300.0
ANSWERED = frozenset({"laugh", "whisper", "reply", "emote"})


@dataclass(frozen=True)
class Reaction:
    line_id: int
    speaker: int
    kind: str
    channel: str
    ts: float
    addressed: bool
    reaction: Literal["laugh", "rebuff", "whisper", "reply", "emote", "ignored", "none"]


def addressed_to(row, guid: int, names: dict[int, str]) -> bool:
    return ((row["channel"] == "whisper" and row["to_guid"] == guid)
            or has_word(row["text"], names.get(guid, "")))


def first_after(store, row, answer) -> bool:
    """No intervening line on this channel (including another cast member's line)."""
    private = row["channel"] == "whisper"
    pair = " AND ((from_guid=? AND to_guid=?) OR (from_guid=? AND to_guid=?))" if private else ""
    params = (row["from_guid"], row["to_guid"], row["to_guid"], row["from_guid"]) if private else ()
    first = store.conn.execute(
        "SELECT id FROM chat_line WHERE channel = ? AND (ts > ? OR (ts = ? AND id > ?)) "
        + pair + " ORDER BY ts, id LIMIT 1",
        (row["channel"], row["ts"], row["ts"], row["id"], *params)).fetchone()
    return first is not None and first["id"] == answer["id"]


def reactions(store, window, human: int, names: dict[int, str]) -> list[Reaction]:
    roster = store.cast()
    human_names = {names.get(human, ""), roster[human].name if human in roster else ""}
    rows = store.conn.execute(
        "SELECT c.* FROM chat_line c WHERE c.origin = 'mind' AND c.ts >= ? AND c.ts < ? "
        "AND EXISTS (SELECT 1 FROM session s WHERE s.started <= c.ts "
        "AND (s.ended IS NULL OR s.ended >= c.ts)) ORDER BY c.ts, c.id", window).fetchall()
    out = []
    for row in rows:
        g = row["from_guid"]
        if g not in names or g == human:
            continue
        addressed = (row["to_guid"] == human if row["channel"] == "whisper" else
                     any(has_word(row["text"], n) for n in human_names))
        answers = store.conn.execute(
            "SELECT * FROM chat_line WHERE origin = 'human' AND from_guid = ? "
            "AND (ts > ? OR (ts = ? AND id > ?)) AND ts <= ? ORDER BY ts, id",
            (human, row["ts"], row["ts"], row["id"], row["ts"] + WHISPER_BACK_S)).fetchall()
        direct = []
        for a in answers:
            whisper = a["channel"] == "whisper" and a["to_guid"] == g
            same = (a["channel"] == row["channel"] and a["channel"] != "whisper"
                    and a["ts"] <= row["ts"] + REPLY_S)
            if whisper or (same and (has_word(a["text"], names[g]) or (
                    not any(has_word(a["text"], name) for other, name in names.items()
                            if other not in (g, human)) and first_after(store, row, a)))):
                direct.append(a)
        if any(LAUGH_RE.search(a["text"]) for a in direct):
            reaction = "laugh"
        elif any(NO_RE.search(a["text"]) for a in direct):
            reaction = "rebuff"
        elif any(a["channel"] == "whisper" for a in direct):
            reaction = "whisper"
        elif direct:
            reaction = "reply"
        else:
            emotes = store.conn.execute(
                "SELECT payload FROM game_event WHERE type = 'text_emote' AND ts > ? AND ts <= ?",
                (row["ts"], row["ts"] + REPLY_S)).fetchall()
            if any((p := json.loads(e["payload"])).get("guid") == human
                   and p.get("target_guid") == g for e in emotes):
                reaction = "emote"
            elif addressed and any(a["ts"] <= row["ts"] + REPLY_S for a in answers):
                reaction = "ignored"
            else:
                reaction = "none"
        utter = store.conn.execute(
            "SELECT kind FROM utterance WHERE guid = ? AND channel = ? AND text = ? "
            "AND dropped IS NULL AND ts BETWEEN ? AND ? ORDER BY abs(ts - ?), id LIMIT 1",
            (g, row["channel"], row["text"], row["ts"] - 2, row["ts"] + 2, row["ts"])).fetchone()
        out.append(Reaction(row["id"], g, utter["kind"] if utter else "?", row["channel"],
                            row["ts"], addressed, reaction))
    return out


def laughs_at(store, line_id: int, *, window_s: float = 60) -> set[int]:
    row = store.conn.execute("SELECT * FROM chat_line WHERE id = ?", (line_id,)).fetchone()
    if row is None:
        return set()
    return {a["from_guid"] for a in store.conn.execute(
        "SELECT from_guid, to_guid, text FROM chat_line WHERE channel = ? AND ts > ? AND ts <= ? "
        "AND origin IN ('mind','human') AND from_guid != ?",
        (row["channel"], row["ts"], row["ts"] + window_s, row["from_guid"]))
        if LAUGH_RE.search(a["text"]) and (row["channel"] != "whisper" or
           (a["from_guid"] == row["to_guid"] and a["to_guid"] == row["from_guid"]))}
