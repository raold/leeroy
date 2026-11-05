"""Persona persistence: save to mind.db, export to / import from the owner's JSON files."""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..human import HumanIdentity
from ..storage.store import Store
from ..text import estimate_tokens
from ..tuning.effective import is_effective
from ..tuning.state import EMPTY, Tuning
from ..wow import CLASSES, RACES
from .card import card_race_class, render_core_card
from .schema import (Persona, PersonaError, persona_from_dict, persona_to_dict,
                     validate_persona)
from .screens import check_no_real_name, check_romance

log = logging.getLogger("leeroy_mind.personas")


def canonical_json(p: Persona) -> str:
    return json.dumps(persona_to_dict(p), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def save_persona(store: Store, p: Persona, *, source: str, ts: float, race: str,
                 class_name: str) -> int:
    """Validate and store a persona; returns its version. Saving an identical persona with the
    same card (race and class unchanged) changes nothing and returns the current version. An
    effective (tuned) persona is refused: the owner's dials are an overlay, never written into
    persona_core (tuning.effective)."""
    if is_effective(p):
        raise TypeError("an effective (tuned) persona is never saved: save the stored one")
    errs = validate_persona(p)
    if errs:
        raise PersonaError(errs)
    js = canonical_json(p)
    card = render_core_card(p, race=race, class_name=class_name)
    with store.transaction():
        current = store.get_persona_json(p.guid)
        if (current is not None and _canonical_text(current) == js
                and store.get_card(p.guid) == card):
            return store.persona_versions()[p.guid]
        return store.put_persona(p.guid, js, card, estimate_tokens(card), source, ts)


def _canonical_text(js: str) -> str | None:
    try:
        return canonical_json(persona_from_dict(json.loads(js)))
    except (ValueError, PersonaError):
        return None


def load_persona(store: Store, guid: int) -> Persona | None:
    """The stored persona, as written: the reader for every writer (attraction, enrichment,
    imports, seed, generate, rebalance, typing fixes) and for exports."""
    js = store.get_persona_json(guid)
    return None if js is None else persona_from_dict(json.loads(js))


def load_tuning(store: Store) -> Tuning:
    """The tuning in force, read in a unit of its own so it pins no WAL snapshot."""
    with store.transaction():
        return Tuning.load(store.conn)


def load_effective(store: Store, guid: int, *, tuning: Tuning | None = None) -> Persona | None:
    """The persona with the owner's dials folded in (tuning.effective), for a reader of
    behaviour that does not go through a PersonaCache: `tuning` when given (a night's frozen
    values), else what mind.db holds now. The stored object itself when nothing reaches it."""
    p = load_persona(store, guid)
    if p is None:
        return None
    t = load_tuning(store) if tuning is None else tuning
    if not (t.pins or t.offsets):
        return p
    return t.apply(p, store.cast().get(guid) if t.needs_cast else None)


def export_personas(store: Store, out_dir: Path, guids=None) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = sorted(store.persona_versions()) if guids is None else sorted(set(guids))
    paths = []
    for g in wanted:
        p = load_persona(store, g)
        if p is None:
            continue
        path = out_dir / f"{g}.json"
        path.write_text(json.dumps(persona_to_dict(p), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


@dataclass
class ImportReport:
    imported: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)


def _race_class(store: Store, cast, guid: int) -> tuple[str, str]:
    m = cast.get(guid)
    if m is not None:
        return RACES.get(m.race, ""), CLASSES.get(m.class_, "")
    prev = store.get_card(guid)  # C15: the cast is unknown, so keep the previous card's
    return card_race_class(prev) if prev else ("", "")


def _screen(p: Persona, human: HumanIdentity, characters: list[str]) -> list[str]:
    """What the persona screens refuse in a hand-edited record: the owner's real name, and
    romance aimed at a member. The messages never contain the name."""
    data, errs = persona_to_dict(p), []
    for check in (lambda: check_no_real_name(data, human),
                  lambda: check_romance(data, human, (), characters=characters)):
        try:
            check()
        except ValueError as e:
            errs.append(str(e))
    return errs


def import_personas(store: Store, in_dir: Path, *, ts: float, dry_run: bool = False,
                    human: HumanIdentity | None = None) -> ImportReport:
    """Validate every file first; if any is bad nothing is written. Each changed persona gets
    a version bump with source "owner"; an identical one is left alone. With `human` every file
    also goes through the persona screens (the real name, romance aimed at a member)."""
    report = ImportReport()
    parsed: list[tuple[int, Persona]] = []
    cast = store.cast()
    characters = [m.name for g, m in cast.items() if human is None or g != human.guid]
    files = sorted(Path(in_dir).glob("*.json"))
    if not files:  # most likely the wrong folder
        report.errors[str(in_dir)] = ["no persona files (<guid>.json) in this folder"]
        return report
    for path in files:
        errs: list[str] = []
        p = None
        try:  # utf-8-sig: an editor may have saved it with a byte order mark
            p = persona_from_dict(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError) as e:
            errs.append(f"file: cannot read ({e})")
        except json.JSONDecodeError as e:
            errs.append(f"file: not valid JSON ({e})")
        except PersonaError as e:
            errs = e.errors
        if p is not None:
            errs = validate_persona(p) + (_screen(p, human, characters) if human else [])
            if path.stem != str(p.guid):
                errs.append(f"guid: is {p.guid} but the file name says {path.stem}")
            else:
                known = cast[p.guid].name if p.guid in cast else None
                if known is None:
                    old = load_persona(store, p.guid)
                    known = old.character_name if old else None
                if known is not None and p.character_name != known:
                    errs.append(f"character_name: must stay {known}")
        if errs:
            report.errors[path.name] = errs
        elif p is not None:
            parsed.append((p.guid, p))
    versions = store.persona_versions()
    changed = []
    for guid, p in parsed:
        old = store.get_persona_json(guid)
        if guid in versions and old is not None and _canonical_text(old) == canonical_json(p):
            report.unchanged.append(guid)
        else:
            changed.append((guid, p))
    if report.errors:
        return report
    report.imported = [g for g, _ in changed]
    if dry_run:
        return report
    with store.transaction():
        for guid, p in changed:
            race, cls = _race_class(store, cast, guid)
            save_persona(store, p, source="owner", ts=ts, race=race, class_name=cls)
    return report


class PersonaCache:
    """The personas the mind reads. get() is the effective persona (the owner's dials folded
    in: tuning.effective), for every reader of behaviour; base() the stored one, for writers.
    With no dial reaching a member, both are the same stored object. The tuning is read from
    mind.db once and again at each full invalidate (the session start); the running mind swaps
    in a new one with set_tuning when the owner changes a dial. `tuning=False`: stored
    personas only, as before tuning existed."""

    def __init__(self, store: Store, *, tuning: bool = True):
        self._store = store
        self._tuned = tuning
        self._base: dict[int, Persona] = {}
        self._cache: dict[int, Persona] = {}
        self._unreadable: set[int] = set()
        self._tuning: Tuning | None = None
        self._cast: dict | None = None

    @property
    def tuning(self) -> Tuning:
        if self._tuning is None:
            self._tuning = load_tuning(self._store) if self._tuned else EMPTY
        return self._tuning

    def set_tuning(self, tuning: Tuning) -> None:
        """New values in force from the next read; the stored personas stay cached."""
        self._tuning = tuning
        self._cache.clear()
        self._cast = None

    def base(self, guid: int) -> Persona | None:
        """The stored persona, or None when there is none or it cannot be read (a row from a
        newer schema, say): one bad row must not silence the whole guild. An unreadable guid
        is logged once and not read again until invalidated (the app does at each session)."""
        p = self._base.get(guid)
        if p is None and guid not in self._unreadable:
            try:
                p = load_persona(self._store, guid)
            except ValueError as e:  # PersonaError, and a JSON error alike
                self._unreadable.add(guid)
                log.warning("persona_unreadable: guid %d: %s", guid, str(e)[:200])
                return None
            if p is not None:
                self._base[guid] = p
        return p

    def get(self, guid: int) -> Persona | None:
        """The effective persona (see the class docstring), or None as base() is."""
        p = self._cache.get(guid)
        if p is not None:
            return p
        p = self.base(guid)
        if p is None:
            return None
        t = self.tuning
        if t.pins or t.offsets:
            if t.needs_cast and self._cast is None:
                self._cast = self._store.cast()
            p = t.apply(p, (self._cast or {}).get(guid))
        self._cache[guid] = p
        return p

    def all(self) -> dict[int, Persona]:
        return {g: p for g in sorted(self._store.persona_versions())
                if (p := self.get(g)) is not None}

    def invalidate(self, guid: int | None = None) -> None:
        if guid is None:
            self._cache.clear()
            self._base.clear()
            self._unreadable.clear()
            self._tuning = None
            self._cast = None
        else:
            self._cache.pop(guid, None)
            self._base.pop(guid, None)
            self._unreadable.discard(guid)
