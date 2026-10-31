"""The owner's own memories of the guild's history (`memories seed`), seeded as data.

The file is a JSON list with one memory of one member per entry:
- guid: the member who remembers it (on the roster, with a persona);
- text: at most 200 characters, never the owner's real name;
- moment: the id of what is remembered, such as "M17", "M17b" or "J3";
- kind: "moment" or "joke" become a story, "about_human" (or "about_" and the nickname the
  guild calls him) an observation;
- importance (1-10, default 6), actors (guids on the roster), and the in-fiction `date`
  (YYYY-MM-DD) or `t_event` (unix seconds), all optional; character (the member's name) is
  checked against the roster when given.

Each becomes source "owner" with evidence ["owner:<moment>"] and key "owner:<moment>:<guid>",
written through MemoryWriter: its real-name screen applies, and only source "owner" may cite
an owner: ref. A dated memory is as old as the fiction says: with the fiction's today
(`fiction_now`) set to the day the history was first seeded (meta owner_seed_now, so a re-run
computes the same times), t_event = that day - (fiction_now - date), and it was made and last
touched then too; an undated one is from that day. When the text itself says the year it
happened in ("In 2005 ..."), that year (and its month, when named) wins over a date that
disagrees, and the plan lists the move (said_when). A re-run of the same file changes nothing;
an edited entry is updated in place, and a memory retired since (archived or superseded) is
left as it is. Nothing here returns or prints a memory's text: the file is private."""
import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date as Date, datetime

from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import (MAX_TEXT, OWNER_REF, MemoryCandidate, MemoryWriter,
                                       clean_memory_text)
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.store import Store

FICTION_NOW = "2009-12-31"
ANCHOR_META = "owner_seed_now"  # the day the history was first seeded, unix seconds
DEFAULT_IMPORTANCE = 6
_FIELDS = frozenset({"guid", "character", "text", "moment", "kind", "importance", "actors",
                     "date", "t_event"})
_MOMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KINDS = {"moment": "story", "joke": "story", "about_human": "observation"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
_MONTH = "(?P<month>" + "|".join(MONTHS) + ")"
_YEAR = r"(?P<year>(?:19|20)\d\d)"
# The year a text says something happened in: "in 2007", "back in May 2006", "May 3rd 2007",
# or a year that is a sentence of its own ("2006."); never one it counts from ("since 2007").
# Dress rehearsal 2 (judge B): seeded memories that gave one year were dated in another.
_SAID_IN = (re.compile(rf"(?<!\w)(?:back\s+)?in\s+(?:{_MONTH}\s+)?{_YEAR}(?!\w)", re.IGNORECASE),
            re.compile(rf"(?<!\w){_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{_YEAR}(?!\w)",
                       re.IGNORECASE),
            re.compile(rf"(?:^|[.!?]\s+){_YEAR}[.!?]"))
UNDATED_MONTH = 7  # an undated entry whose text says only the year: the middle of it
_SINCE = re.compile(r"(?<!\w)since(?!\w)[^.!?;,]*$", re.IGNORECASE)  # a "since" clause before it
# A story she was not there for: its year is the story's, its date when she heard it.
_NOT_THERE = re.compile(r"(?<!\w)(?:wasn't|was not|weren't|were not)\s+there(?!\w)"
                        r"|(?<!\w)before\s+(?:I|we)\s+joined(?!\w)|(?<!\w)before\s+my\s+time(?!\w)",
                        re.IGNORECASE)


def said_when(text: str) -> tuple[int, int | None] | None:
    """(year, month or None) a memory's text says it happened in; None when it names no such
    year, or more than one. A year inside a "since" clause counts from, not happened in."""
    found = []
    for pat in _SAID_IN:
        for m in pat.finditer(text):
            if _SINCE.search(text, 0, m.start()):
                continue
            month = m.groupdict().get("month")
            found.append((int(m["year"]), MONTHS.index(month.title()) + 1 if month else None))
    if len({y for y, _ in found}) != 1:
        return None
    months = {mo for _, mo in found if mo}
    return found[0][0], (months.pop() if len(months) == 1 else None)


def _redate(e: dict, when: Date | None, fiction_now: Date) -> Date | None:
    """The date an entry moves to because its text says another year (or month), or None to
    keep it: a named month takes its day nearest the old date (its last day for a later date,
    its first for an earlier one), so a move never lands before the event it names; a year
    alone keeps the old month and day. An explicit t_event is kept, and so are a story she
    says she was not there for and a move past fiction_now."""
    said = said_when(e["text"])
    if said is None or "t_event" in e or _NOT_THERE.search(e["text"]):
        return None
    year, month = said
    if when is None:
        new = Date(year, month or UNDATED_MONTH, 1)
    elif year == when.year and month in (None, when.month):
        return None
    elif month is None:
        new = Date(year, when.month, min(when.day, calendar.monthrange(year, when.month)[1]))
    else:
        first, last = Date(year, month, 1), Date(year, month, calendar.monthrange(year, month)[1])
        new = last if when > last else first
    return new if new <= fiction_now else None


class SeedFileError(ValueError):
    """The file is not a JSON list of entries."""


@dataclass(frozen=True)
class Reject:
    index: int  # 1-based, in the file
    guid: object
    moment: object
    reason: str


@dataclass(frozen=True)
class Redate:
    index: int  # 1-based, in the file
    guid: int
    moment: str
    said: str  # what the text says: "2005", "April 2005"
    new: str  # YYYY-MM-DD
    old: str  # YYYY-MM-DD, or "undated"
    key: str  # the memory's key, owner:<moment>:<guid>
    t_event: int  # where the move sets it


@dataclass
class SeedPlan:
    candidates: list[MemoryCandidate] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    anchor: int = 0
    redated: list[Redate] = field(default_factory=list)


def read_seed_file(raw: str) -> list:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SeedFileError(f"not JSON: line {e.lineno}, column {e.colno}") from None
    if not isinstance(data, list):
        raise SeedFileError("expected a JSON list of entries")
    return data


def parse_fiction_now(text: str) -> Date:
    if not _DATE.fullmatch(text or ""):
        raise ValueError("--fiction-now must be YYYY-MM-DD")
    return Date.fromisoformat(text)


def kinds(human: HumanIdentity | None) -> dict[str, str]:
    """The entry kinds and the memory kinds they become; "about_<nickname>" too."""
    out = dict(_KINDS)
    if human is not None and human.nickname:
        out[f"about_{human.nickname.casefold()}"] = "observation"
    return out


def _guid(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _problem(e: dict, *, store: Store, roster: dict, personas: set[int],
             human: HumanIdentity | None, fiction_now: Date, kind_map: dict) -> str | None:
    """Why this entry cannot be seeded, or None. Never quotes the text."""
    unknown = sorted(set(e) - _FIELDS)
    if unknown:
        return "unknown field(s): " + ", ".join(unknown)
    guid = _guid(e.get("guid"))
    if guid is None:
        return "guid must be a positive integer"
    if guid not in roster:
        return f"guid {guid} is not on the roster"
    if guid not in personas:
        return f"guid {guid} has no persona"
    who = e.get("character")
    if who is not None and (not isinstance(who, str)
                            or who.casefold() != roster[guid].name.casefold()):
        return f"character does not match guid {guid}'s name on the roster"
    text = e.get("text")
    if not isinstance(text, str) or not text.strip():
        return "no text"
    if len(" ".join(text.split())) > MAX_TEXT:
        return f"text too long ({len(' '.join(text.split()))} > {MAX_TEXT} characters)"
    if human is not None and redact_real_name(text, human, "") != text:
        return "text uses the owner's real name; use the nickname"
    moment = e.get("moment")
    if not isinstance(moment, str) or not _MOMENT.fullmatch(moment):
        return "moment must be an id such as M17 or M17b"
    if e.get("kind") not in kind_map:
        return "kind must be one of " + ", ".join(sorted(kind_map))
    imp = e.get("importance", DEFAULT_IMPORTANCE)
    if not (isinstance(imp, int) and not isinstance(imp, bool) and 1 <= imp <= 10):
        return "importance must be an integer 1-10"
    actors = e.get("actors", [])
    if not isinstance(actors, list) or any(_guid(a) is None for a in actors):
        return "actors must be a list of guids"
    if missing := [a for a in actors if a not in roster]:
        return f"actor {missing[0]} is not on the roster"
    if "date" in e and "t_event" in e:
        return "give date or t_event, not both"
    if "date" in e:
        d = e["date"]
        if not isinstance(d, str) or not _DATE.fullmatch(d):
            return "date must be YYYY-MM-DD"
        try:
            when = Date.fromisoformat(d)
        except ValueError:
            return "date must be YYYY-MM-DD"
        if when > fiction_now:
            return f"date is after --fiction-now {fiction_now.isoformat()}"
    if "t_event" in e and _guid(e["t_event"]) is None:
        return "t_event must be unix seconds"
    return None


def plan_owner_memories(entries: list, *, store: Store, human: HumanIdentity | None,
                        fiction_now: Date, now: float) -> SeedPlan:
    """The memories the entries make, and the entries refused with why (by position, guid and
    moment). The anchor is the day the history was first seeded, else `now`."""
    stored = store.get_meta(ANCHOR_META)
    anchor = int(stored) if stored and stored.isdigit() else int(now)
    plan = SeedPlan(anchor=anchor)
    roster = store.cast()
    personas = set(store.persona_versions())
    kind_map = kinds(human)
    seen: dict[tuple, int] = {}
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            plan.rejects.append(Reject(i, None, None, "not a JSON object"))
            continue
        why = _problem(e, store=store, roster=roster, personas=personas, human=human,
                       fiction_now=fiction_now, kind_map=kind_map)
        pair = (e.get("moment"), e.get("guid"))
        if why is None and pair in seen:
            why = f"same moment and guid as entry {seen[pair]}"
        if why is not None:
            plan.rejects.append(Reject(i, e.get("guid"), e.get("moment"), why))
            continue
        seen[pair] = i
        when = Date.fromisoformat(e["date"]) if "date" in e else None
        moved = _redate(e, when, fiction_now)
        day = moved or when
        t = anchor - (fiction_now - day).days * 86400 if day else int(e.get("t_event", anchor))
        key = f"{OWNER_REF}{e['moment']}:{e['guid']}"
        if moved is not None:
            year, month = said_when(e["text"])
            plan.redated.append(Redate(
                i, e["guid"], e["moment"], f"{MONTHS[month - 1]} {year}" if month else str(year),
                moved.isoformat(), when.isoformat() if when else "undated", key, t))
        plan.candidates.append(MemoryCandidate(
            owner=e["guid"], kind=kind_map[e["kind"]], text=e["text"], t_event=t,
            actors=tuple(e.get("actors", ())), importance=e.get("importance", DEFAULT_IMPORTANCE),
            valence=0.0, source="owner", evidence=(f"{OWNER_REF}{e['moment']}",), key=key,
            t_created=t))
    return plan


def is_retired(store: Store, c: MemoryCandidate) -> bool:
    """Its stored memory was retired (archived, superseded): a re-seed leaves it so."""
    m = store.keyed_memory(c.owner, c.key)
    return m is not None and m.status != "active"


def is_unchanged(store: Store, c: MemoryCandidate, human: HumanIdentity | None) -> bool:
    m = store.keyed_memory(c.owner, c.key)
    return m is not None and (
        m.text, m.kind, m.importance, m.actors, m.evidence, m.t_event, m.source, m.status) == (
        clean_memory_text(c.text, human), c.kind, c.importance, tuple(sorted(set(c.actors))),
        c.evidence, c.t_event, "owner", "active")


@dataclass
class SeedResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    retired: int = 0


async def write_owner_memories(plan: SeedPlan, writer: MemoryWriter, store: Store,
                               human: HumanIdentity | None) -> SeedResult:
    """Write the plan through the writer (keyed, so a re-run updates in place), leaving a
    retired memory as it is; record the anchor the first time."""
    out = SeedResult()
    if store.get_meta(ANCHOR_META) is None:
        store.set_meta(ANCHOR_META, str(plan.anchor))
    for c in plan.candidates:
        if is_retired(store, c):
            out.retired += 1
            continue
        if is_unchanged(store, c, human):
            out.unchanged += 1
            continue
        existed = store.keyed_memory(c.owner, c.key) is not None
        r = await writer.write(c, enforce_limits=False)
        if r.action != "updated":
            out.failed += 1
        elif existed:
            out.updated += 1
        else:
            out.new += 1
    return out


def summary(plan: SeedPlan, roster: dict) -> list[str]:
    """One line per member: how many memories, and the earliest and latest date they are set
    at (local time). No text."""
    by: dict[int, list[int]] = {}
    for c in plan.candidates:
        by.setdefault(c.owner, []).append(c.t_event)
    lines = []
    for guid in sorted(by):
        ts = by[guid]
        name = roster[guid].name if guid in roster else "?"
        first, last = (datetime.fromtimestamp(x).date().isoformat() for x in (min(ts), max(ts)))
        lines.append(f"{guid:>7} {name}: {len(ts)} memor{'y' if len(ts) == 1 else 'ies'}, "
                     f"{first} .. {last}")
    return lines
