"""`raidplay check`: the playbooks against the spell book and wow.BOSS_NAMES.

Errors: a covered raid's boss without a playbook, a spell id the spell book does not know, a
check name that resolves to no spell, a stock playerbots action or strategy name that mod-playerbots
does not register (when its source is given). Notes (never errors): a check whose names resolve to
none of the ids that hit players in the mechanics mapped to it, and the optional DBM cross-check
(spell ids DBM watches that no mechanic lists, berserk timers that differ). Findings carry ids and
numbers only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from leeroy_mind.wow import BOSS_NAMES, is_raid_fight

from .model import Playbooks


class Book(Protocol):
    def info(self, id: int) -> object | None: ...
    def ids(self, name: str) -> frozenset[int]: ...


@dataclass(frozen=True)
class Finding:
    level: str   # "error" or "note"
    code: str
    ref: str     # a mechanic, check or boss id
    detail: str = ""


def coverage(playbooks: Playbooks) -> list[Finding]:
    out = []
    for map_id in playbooks.covered_maps():
        for (m, index) in sorted(BOSS_NAMES):
            if m == map_id and is_raid_fight(m, index) and playbooks.boss(m, index) is None:
                out.append(Finding("error", "boss_missing", f"{m}:{index}"))
    return out


def spells(playbooks: Playbooks, book: Book) -> list[Finding]:
    out = []
    for boss in playbooks.bosses():
        for m in boss.mechanics:
            for spell in sorted(m.all_ids()):
                if book.info(spell) is None:
                    out.append(Finding("error", "spell_unknown", m.id, str(spell)))
        for c in boss.checks:
            resolved = set()
            for name in (*c.names, *c.exempt):
                ids = book.ids(name)
                if not ids:
                    out.append(Finding("error", "check_name_unresolved", c.id))
                resolved |= ids
            mapped = set().union(*(m.all_hit_ids() for m in boss.mechanics if m.check == c.id))
            # The original version 1 avoid rows keep their transcription for parity; the checker
            # never resolves an avoid row's names (it records unverified damage instead).
            legacy = c.since == 1 and c.kind == "avoid"
            if c.names and resolved and not resolved & mapped and not legacy:
                out.append(Finding("note", "check_names_disjoint", c.id))
    return out


_CREATOR = re.compile(r'creators\["([^"\n]{1,80})"\]')


def registered_names(src: Path) -> frozenset[str]:
    """Action and strategy names mod-playerbots registers (`creators["name"]`) under src/Ai."""
    names = set()
    for path in sorted(p for p in Path(src).rglob("*") if p.suffix in (".h", ".cpp")):
        names.update(_CREATOR.findall(path.read_text(encoding="utf-8", errors="replace")))
    return frozenset(names)


def stock_actions(playbooks: Playbooks, names: frozenset[str]) -> list[Finding]:
    return [Finding("error", "stock_action_unknown", m.id, a) for m in playbooks.mechanics()
            for a in m.playerbots if a not in names]


def _dbm_mods(facts) -> Iterable[dict]:
    mods = facts.get("mods") if isinstance(facts, dict) else None
    return [m for m in mods if isinstance(m, dict)] if isinstance(mods, list) else []


def _ints(value) -> list[int]:
    return [v for v in value if type(v) is int] if isinstance(value, list) else []


def dbm_cross_check(playbooks: Playbooks, facts) -> list[Finding]:
    """Compare local DBM facts (dbm.py's JSON) with the playbooks; notes only."""
    by_creature = {c: boss for boss in playbooks.bosses() for c in boss.creatures}
    out, matched = [], set()
    for mod in _dbm_mods(facts):
        bosses = {by_creature[c].key: by_creature[c] for c in _ints(mod.get("creatures")) if c in by_creature}
        if not bosses:
            continue
        for boss in bosses.values():
            matched.add(boss.key)
            ref = f"{boss.map_id}:{boss.index}"
            listed = set().union(*(m.all_ids() for m in boss.mechanics))
            for spell in sorted(set(_ints(mod.get("spells"))) - listed):
                out.append(Finding("note", "dbm_spell_unlisted", ref, str(spell)))
            berserk = [t.get("seconds") for t in mod.get("timers", []) if isinstance(t, dict)
                       and t.get("kind") == "berserk_timer" and type(t.get("seconds")) in (int, float)]
            enrage = sorted(set(boss.enrage.values()))
            if berserk and enrage and berserk[0] not in enrage:
                out.append(Finding("note", "dbm_enrage_differs", ref,
                                   f"dbm={berserk[0]:g} script={','.join(f'{e:g}' for e in enrage)}"))
    for boss in playbooks.bosses():
        if _dbm_mods(facts) and boss.key not in matched:
            out.append(Finding("note", "dbm_boss_unmatched", f"{boss.map_id}:{boss.index}"))
    return out


def validate(playbooks: Playbooks, book: Book | None = None, *, dbm=None,
             stock: frozenset[str] | None = None) -> list[Finding]:
    found = coverage(playbooks)
    if book is not None:
        found += spells(playbooks, book)
    if stock is not None:
        found += stock_actions(playbooks, stock)
    if dbm is not None:
        found += dbm_cross_check(playbooks, dbm)
    return found
