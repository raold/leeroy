"""`raidplay dbm-extract`: numeric facts from an installed Deadly Boss Mods, for local use.

DBM's licence does not fit this repository, so nothing from it is ever committed: this reads
the owner's installed Lua files (never runs them) and writes only facts (creature ids, spell
ids, the kind of each warning or timer, timer seconds) to a JSON file in the mind's data
directory. No DBM text (warnings, options, localisation, variable names) is kept. The
playbook check can compare those facts with the playbooks; nothing requires them.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FORMAT = 1
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 2000

_NEWMOD = re.compile(r'DBM:NewMod\(\s*"([^"\n]{1,80})"\s*,\s*"(DBM-[A-Za-z0-9-]{1,40})"')
_CREATURES = re.compile(r'mod:SetCreatureID\(([0-9,\s]*)\)')
_OBJECT = re.compile(r'mod:New([A-Za-z]{1,40})\(([^)\n]*)\)')
_IS_SPELL = re.compile(r'IsSpellID\(([0-9,\s]*)\)')
_SPELL_EQ = re.compile(r'spellId\s*==\s*([0-9]+)')
_NUMBER = re.compile(r'-?[0-9]+(?:\.[0-9]+)?')
_SKIP_KINDS = {"Sound", "AchievementTimer", "Mod", "Icon", "RangeFrame"}


def _kind(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def _numbers(args: str) -> list[float]:
    return [float(t) for t in (a.strip() for a in args.split(",")) if _NUMBER.fullmatch(t)]


def _spell(value: float) -> int | None:
    return int(value) if value == int(value) and 100 <= value < 1_000_000 else None


def parse_mod(text: str, addon: str, relative: str) -> dict | None:
    """One boss mod's facts, or None for a file that defines no mod."""
    head = _NEWMOD.search(text)
    if head is None:
        return None
    creatures = sorted({int(n) for m in _CREATURES.finditer(text) for n in re.findall(r"[0-9]+", m.group(1))})
    warnings, timers = [], []
    for m in _OBJECT.finditer(text):
        name, numbers = m.group(1), _numbers(m.group(2))
        if name in _SKIP_KINDS or name.startswith("Add") or not numbers:
            continue
        if name.endswith("Timer"):
            spell = _spell(numbers[1]) if len(numbers) > 1 else None
            timers.append({"kind": _kind(name), "seconds": numbers[0], "spell": spell})
        else:
            spell = next((s for s in map(_spell, numbers) if s), None)
            if spell:
                warnings.append({"kind": _kind(name), "spell": spell})
    spells = {int(n) for m in _IS_SPELL.finditer(text) for n in re.findall(r"[0-9]+", m.group(1))}
    spells |= {int(m.group(1)) for m in _SPELL_EQ.finditer(text)}
    spells |= {w["spell"] for w in warnings} | {t["spell"] for t in timers if t["spell"]}
    return {"mod": head.group(1), "addon": addon, "file": relative, "creatures": creatures,
            "warnings": warnings, "timers": timers, "spells": sorted(spells)}


def extract(addons: Path) -> dict:
    """Facts from every DBM-* addon folder under ``addons`` (an Interface/AddOns path)."""
    mods, files = [], 0
    for folder in sorted(p for p in Path(addons).iterdir() if p.is_dir() and p.name.startswith("DBM-")):
        for path in sorted(folder.rglob("*.lua")):
            if path.name.lower().startswith("localization") or path.is_symlink():
                continue
            files += 1
            if files > MAX_FILES:
                raise ValueError("too many Lua files under the addons folder")
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            mod = parse_mod(path.read_text(encoding="utf-8", errors="replace"), folder.name,
                            path.relative_to(folder).as_posix())
            if mod is not None:
                mods.append(mod)
    return {"format": FORMAT, "source": "dbm", "mods": mods}


def write(facts: dict, out: Path) -> None:
    """Write atomically: a reader sees the old file or the new one."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes((json.dumps(facts, sort_keys=True, indent=1) + "\n").encode("utf-8"))
    os.replace(tmp, out)


def read(path: Path) -> dict | None:
    """The local facts file, or None when it is missing or not this format."""
    try:
        facts = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    return facts if isinstance(facts, dict) and facts.get("format") == FORMAT else None
