"""Importing voice sheets (`personas voices`): one `<guid>.json` per member, read from a folder
outside the repo, checked, and saved as her persona's `speech` field (schema.Speech;
personas/speech.py says what a prompt makes of it).

- read_sheet: a file's sheet. Either the sheet lab's shape ({"guid", "voice": {"register",
  "sentence_shape", "says", "never_says", "unprompted", "with_him", "relationship_stance"},
  "samples": {kind: line}}; its other keys, such as the humour and typing notes, the texture
  seed and the rendered block, are not read) or the schema's own ({"guid", "speech": {...}}).
- plan: each file as a Plan with its outcome: "import", "unchanged", "refused" with codes
  (REFUSALS) or "skipped" (SKIPS: a sheet for a persona outside the cast, such as a world
  persona, is passed over and is no refusal), and the sizes a dry run prints: the block's
  estimated tokens and how many samples. A plan never holds a sheet's text in anything it
  prints.
- Every saved sheet is stamped with the household her persona has then (stamped): her stance is
  shown only while her household still reads the same (speech.stance_current), so the night's
  arc or an owner edit that changes her household takes the stance out of her prompts until a
  sheet is imported again (a plan says when an import only re-sets that stamp: `restamps`).
- changed_at: when her sheet last changed, the night's new baseline for drift.
- set_voice: one transaction. Her stored persona read again, saved with the sheet as a new
  persona version, source "owner", with a persona ledger row of kind "voice" and a private
  receipt (meta RECEIPT: the sheet before and after, and the source before), as personas.homes
  does. Taking a sheet off (`--clear`) saves the source she had before her first sheet.
- restore: nightly.growth.undo's "voice" branch (`personas undo <id>`, the console's Undo):
  the sheet as it was before, with the source before, while it is still what the step left.

Older code refuses a persona with a `speech` field: run `personas voices --clear --go` before
rolling the code back, as with `home` and `bust`."""
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .io import load_persona, save_persona
from .schema import (PersonaError, Speech, speech_from_dict, speech_to_dict, validate_persona)
from .speech import block_tokens, screen

KIND = "voice"
RECEIPT = "personas:voice-undo:{ledger}"
SOURCES = ("seed", "generated", "owner", "variety")  # persona_core's sources
MAX_FILE_BYTES = 256 * 1024
# What a sheet is refused for, by code (a dry run and --go print these, never a sheet's text)
REFUSALS = {
    "unreadable": "the file cannot be read, or is too large",
    "not_json": "the file is not valid JSON",
    "bad_shape": "no sheet in it, or a field of the wrong type or an unknown field",
    "guid_mismatch": "its guid is not the file name's",
    "name_mismatch": "its character name is not the stored member's (a stale sheet)",
    "no_persona": "no stored persona for this guid in the cast",
    "invalid": "the persona's own checks refuse it (lengths, counts, the block's size)",
    "real_name": "it holds the guild master's real name",
    "member_romance": "it has romance aimed at a guild member",
    "assistant_text": "a line reads like an assistant, not a person",
    "later_era": "slang or a year from after 2009",
    "member_name": "a sample line names a guild member",
    "never_in_sample": "a sample line uses a word she never uses",
    "says_slop": ("one of her words holds or sits inside a stock slop entry, which the guards "
                  "rewrite on every turn"),
    "never_in_typing": ("her own abbreviations or typing habits produce a word she never uses, "
                        "or a word of a never-entry"),
    "shape_over_typing": "her shape names more words than her typing's longest line",
}
# A file passed over without a refusal, by code (never an error exit)
SKIPS = {"not_cast": "a persona outside the cast (a world persona): sheets are for the cast"}
# the lab's field names onto the schema's
_LAB = {"register": "register", "sentence_shape": "shape", "says": "says", "never_says": "never",
        "unprompted": "brings_up", "with_him": "with_him", "relationship_stance": "stance"}


class SheetError(ValueError):
    def __init__(self, code: str, details=()):
        self.code, self.details = code, list(details)
        super().__init__(code)


@dataclass(frozen=True)
class Plan:
    guid: int
    name: str = ""
    outcome: str = "refused"          # import, unchanged, clear, skipped or refused
    codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()     # the schema's messages: paths and limits, never values
    tokens: int = 0                   # the rendered block's estimated tokens
    samples: int = 0
    speech: Speech | None = field(default=None, repr=False, compare=False)
    # her stored sheet is this one but stamped for another household: importing re-sets her
    # stance for the household she has now
    restamps: bool = False


def read_sheet(path: Path) -> tuple[int | None, str | None, Speech]:
    """(the file's guid, its character name, its sheet); SheetError with a code otherwise."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SheetError("unreadable")
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise SheetError("unreadable") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SheetError("not_json") from None
    if not isinstance(data, dict):
        raise SheetError("bad_shape")
    if isinstance(data.get("speech"), dict):
        sheet = data["speech"]
    elif isinstance(data.get("voice"), dict):
        voice = data["voice"]
        sheet = {ours: voice[theirs] for theirs, ours in _LAB.items() if theirs in voice}
        sheet["samples"] = data.get("samples", voice.get("samples"))
    else:
        raise SheetError("bad_shape")
    try:
        speech = speech_from_dict(sheet)
    except PersonaError as e:
        raise SheetError("bad_shape", e.errors) from None
    guid, name = data.get("guid"), data.get("character_name")
    return (guid if type(guid) is int else None, name if isinstance(name, str) else None,
            speech)


def _checked(p, speech: Speech, *, human, characters) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(codes, details) the persona with this sheet is refused for; both empty when it may be
    saved."""
    from .screens import check_no_real_name, check_romance
    new = replace(p, speech=speech)
    errs = [e for e in validate_persona(new) if e.startswith("speech")]
    codes = ["invalid"] if errs else []
    data = {"speech": speech_to_dict(speech)}
    if human is not None:
        try:
            check_no_real_name(data, human)
        except ValueError:
            codes.append("real_name")
        try:
            check_romance(data, human, (), characters=[c for c in characters
                                                       if c != p.character_name])
        except ValueError:
            codes.append("member_romance")
    codes += screen(new, characters=characters)
    return tuple(dict.fromkeys(codes)), tuple(errs)


def plan(store, folder: Path, *, guids=None, human=None) -> list[Plan]:
    """A Plan for each `<guid>.json` in `folder` (only `guids` when given), by guid. Files
    whose name is no number (a cast plan, notes) are not sheets and are passed over."""
    cast = store.cast()
    characters = [m.name for g, m in cast.items() if human is None or g != human.guid]
    stored = store.persona_versions()  # the cast's, never a world persona
    try:
        everyone = set(store.persona_versions(tier=None))
    except Exception:  # a schema from before the tiers: everyone stored is the cast
        everyone = set(stored)
    out = []
    for path in sorted((f for f in Path(folder).glob("*.json") if f.stem.isdigit()),
                       key=lambda f: int(f.stem)):
        guid = int(path.stem)
        if guids is not None and guid not in guids:
            continue
        try:
            named, name, speech = read_sheet(path)
        except SheetError as e:
            out.append(Plan(guid, codes=(e.code,), details=tuple(e.details)))
            continue
        if named is not None and named != guid:
            out.append(Plan(guid, codes=("guid_mismatch",)))
            continue
        try:
            p = load_persona(store, guid) if guid in stored else None
        except ValueError:
            p = None
        if p is None:
            if guid in everyone:  # a world persona: passed over, never a refusal
                out.append(Plan(guid, outcome="skipped", codes=("not_cast",)))
            else:
                out.append(Plan(guid, codes=("no_persona",)))
            continue
        if name is not None and name != p.character_name:
            out.append(Plan(guid, p.character_name, codes=("name_mismatch",)))
            continue
        speech = stamped(speech, p)
        codes, details = _checked(p, speech, human=human, characters=characters)
        sizes = dict(tokens=block_tokens(speech), samples=len(speech.samples))
        if codes:
            out.append(Plan(guid, p.character_name, codes=codes, details=details, **sizes))
        else:
            restamps = (p.speech is not None and p.speech != speech
                        and replace(p.speech, household=speech.household) == speech)
            out.append(Plan(guid, p.character_name, "unchanged" if p.speech == speech
                            else "import", speech=speech, restamps=restamps, **sizes))
    return out


def stamped(speech: Speech, p) -> Speech:
    """The sheet stamped with the household her persona has now: her stance is written for it
    (speech.stance_current)."""
    return replace(speech, household=" ".join(p.real_life.household.split()))


def plan_clear(store, *, guids=None) -> list[Plan]:
    """A Plan taking the sheet off each member that has one (only `guids` when given)."""
    out = []
    for guid in sorted(store.persona_versions()):
        if guids is not None and guid not in guids:
            continue
        try:
            p = load_persona(store, guid)
        except ValueError:
            continue
        if p is not None and p.speech is not None:
            out.append(Plan(guid, p.character_name, "clear", tokens=block_tokens(p.speech),
                            samples=len(p.speech.samples)))
    return out


def changed_at(store, guid: int) -> float | None:
    """When her voice sheet last changed: the newest `voice` ledger row of hers, or undo of
    one. None when it never did (or on a schema without the ledger). The night's drift checks
    take it as her new baseline (nightly.critic, eval.persona.drift_check)."""
    import sqlite3
    try:
        row = store.conn.execute(
            "SELECT max(ts) FROM persona_ledger WHERE guid=? AND (kind=? OR (kind='undo' AND "
            "ref IN (SELECT id FROM persona_ledger WHERE guid=? AND kind=?)))",
            (guid, KIND, guid, KIND)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row is not None and row[0] is not None else None


def describe(before: Speech | None, after: Speech | None) -> str:
    """The ledger row's words: never a sheet's text."""
    if after is None:
        return "voice sheet removed"
    return "voice sheet replaced" if before is not None else "voice sheet set"


def _dict(speech: Speech | None):
    return None if speech is None else speech_to_dict(speech)


def _race_class(store, guid: int) -> tuple[str, str]:
    from leeroy_mind.nightly.enrich import _race_class as race_class
    return race_class(store, guid)


def _source_before(store, guid: int, current) -> str | None:
    """The persona's source before the voice steps that gave her the sheet she has: her voice
    rows not undone, newest first, followed by their receipts back to the one that gave her a
    first sheet. None when no receipt leads there."""
    source, want = None, current
    for r in store.conn.execute(
            "SELECT id FROM persona_ledger l WHERE guid=? AND kind=? AND NOT EXISTS (SELECT 1 "
            "FROM persona_ledger u WHERE u.kind='undo' AND u.ref=l.id) ORDER BY id DESC",
            (guid, KIND)).fetchall():
        raw = store.get_meta(RECEIPT.format(ledger=r["id"]))
        receipt = json.loads(raw) if raw else {}
        if want is None or receipt.get("after") != want:
            continue
        source, want = receipt.get("source"), receipt.get("before")
    return source if want is None and source in SOURCES else None


def set_voice(store, guid: int, speech: Speech | None, *, now: float) -> int | None:
    """Her sheet as given (None takes it off), saved as an owner persona version with its
    ledger row and receipt; the ledger row's id, or None when it already is (nothing written).
    The schema refuses a sheet that fails its checks (PersonaError)."""
    from leeroy_mind.nightly.store import ledger_add
    with store.transaction():
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        before = stored.speech
        if speech is not None:
            speech = stamped(speech, stored)  # her stance is for the household she has now
        if before == speech:
            return None
        source = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",
                                    (guid,)).fetchone()[0]
        new_source = ((_source_before(store, guid, _dict(before)) or "owner")
                      if speech is None else "owner")
        race, class_name = _race_class(store, guid)
        version = save_persona(store, replace(stored, speech=speech), source=new_source, ts=now,
                               race=race, class_name=class_name)
        ledger = ledger_add(store, guid=guid, kind=KIND, text=describe(before, speech),
                            evidence=[], now=now)
        store.set_meta(RECEIPT.format(ledger=ledger), json.dumps(
            {"before": _dict(before), "after": _dict(speech), "source": source,
             "version": version}, sort_keys=True, ensure_ascii=False))
    return ledger


def restore(store, row, *, now: float) -> bool:
    """nightly.growth.undo of a "voice" ledger row, inside its transaction: the sheet as it was
    before, with the persona's source before, while it is still what the step left. False
    (nothing written) when the receipt is gone or a later change moved it."""
    raw = store.get_meta(RECEIPT.format(ledger=row["id"]))
    if raw is None:
        return False
    receipt = json.loads(raw)
    p = load_persona(store, row["guid"])
    if p is None or _dict(p.speech) != receipt.get("after"):
        return False  # a later change wins
    before = receipt.get("before")
    restored = replace(p, speech=None if before is None else speech_from_dict(before))
    source = receipt.get("source") if receipt.get("source") in SOURCES else "owner"
    race, class_name = _race_class(store, p.guid)
    save_persona(store, restored, source=source, ts=now, race=race, class_name=class_name)
    return True
