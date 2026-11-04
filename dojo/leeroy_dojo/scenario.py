"""Scenario manifests (dojo/scenarios/*.json): structure, canonical form and the SHA-256 the worldserver pins.

The canonical form is json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True); mod-dojo's
DojoManifest.cpp produces the same bytes with nlohmann's dump, and both refuse the same malformed documents (the
shared cases in modules/mod-dojo/tests/fixtures/scenario_cases.json). Whether each id exists in the installed data is
the worldserver's check at start (src/Runtime/DojoScenario.cpp), which never substitutes a failed lookup.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

KIND = "leeroy-dojo-scenario"
MAX_BYTES = 256 * 1024
EQUIPMENT_SLOTS = 19
GLYPH_SLOTS = 6
MAX_LEVEL = 80
SLUG = re.compile(r"^[a-z0-9_]{1,64}$")
CHARACTER = re.compile(r"^[A-Z][a-z]{1,11}$")
ACCOUNT = re.compile(r"^[A-Z0-9]{3,16}$")
TOP_KEYS = ("kind", "format", "id", "curriculum", "version", "description", "rules", "arena", "fixture", "actors",
            "loadout")


class ScenarioError(Exception):
    pass


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


class _Reader:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, path: str, why: str) -> None:
        self.errors.append(f"{path}: {why}")

    def obj(self, v, path: str, keys: tuple[str, ...]) -> bool:
        if not isinstance(v, dict):
            self.fail(path, "must be an object")
            return False
        ok = True
        for key in v:
            if key not in keys:
                self.fail(f"{path}.{key}", "is not a known key")
                ok = False
        for key in keys:
            if key not in v:
                self.fail(f"{path}.{key}", "is missing")
                ok = False
        return ok

    def uint(self, v, path: str, lo: int, hi: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            self.fail(path, "must be a non-negative integer")
            return 0
        if not lo <= v <= hi:
            self.fail(path, f"must be {lo}-{hi}")
            return 0
        return v

    def number(self, v, path: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.fail(path, "must be a number")
            return 0.0
        d = float(v)
        if not math.isfinite(d) or abs(d) > 1e6:
            self.fail(path, "must be finite and within 1e6")
            return 0.0
        if isinstance(v, float) and round(d * 1e6) / 1e6 != d:
            self.fail(path, "may have at most six decimals")
            return 0.0
        return d

    def text(self, v, path: str, max_len: int = 128) -> str:
        if not isinstance(v, str) or not 1 <= len(v) <= max_len or any(not 0x20 <= ord(c) < 0x7F for c in v):
            self.fail(path, f"must be printable ASCII text of 1-{max_len} characters")
            return ""
        return v

    def pos(self, v, path: str, with_map: bool = False) -> tuple[float, float, float, float]:
        keys = ("map_id", "x", "y", "z", "o") if with_map else ("x", "y", "z", "o")
        if not self.obj(v, path, keys):
            return (0.0, 0.0, 0.0, 0.0)
        p = tuple(self.number(v[k], f"{path}.{k}") for k in ("x", "y", "z", "o"))
        if not 0 <= p[3] < 2 * 3.14159266:
            self.fail(f"{path}.o", "must be an orientation in [0, 2 pi)")
        return p  # type: ignore[return-value]

    def named(self, v, path: str, max_id: int) -> None:
        if self.obj(v, path, ("id", "name")):
            self.uint(v["id"], f"{path}.id", 1, max_id)
            self.text(v["name"], f"{path}.name")


def _loadout(r: _Reader, v) -> None:
    p = "loadout"
    if not r.obj(v, p, ("level", "race", "class", "gender", "appearance", "talents", "glyphs", "equipment", "kit",
                        "spellbook", "initial")):
        return
    level = r.uint(v["level"], f"{p}.level", 1, MAX_LEVEL)
    r.named(v["race"], f"{p}.race", 11)
    r.named(v["class"], f"{p}.class", 11)
    r.uint(v["gender"], f"{p}.gender", 0, 1)
    a = v["appearance"]
    if r.obj(a, f"{p}.appearance", ("skin", "face", "hair_style", "hair_color", "facial_hair")):
        for k in ("skin", "face", "hair_style", "hair_color", "facial_hair"):
            r.uint(a[k], f"{p}.appearance.{k}", 0, 255)

    if not isinstance(v["talents"], list):
        r.fail(f"{p}.talents", "must be an array")
    else:
        ids, points = set(), 0
        for i, t in enumerate(v["talents"]):
            tp = f"{p}.talents[{i}]"
            if not r.obj(t, tp, ("talent_id", "rank", "spell_id", "name")):
                continue
            tid = r.uint(t["talent_id"], f"{tp}.talent_id", 1, 0xFFFFFF)
            rank = r.uint(t["rank"], f"{tp}.rank", 1, 5)
            r.uint(t["spell_id"], f"{tp}.spell_id", 1, 0xFFFFFF)
            r.text(t["name"], f"{tp}.name")
            if tid in ids:
                r.fail(f"{tp}.talent_id", "repeats a talent")
            ids.add(tid)
            points += rank
        if level >= 10 and points > level - 9:
            r.fail(f"{p}.talents", f"spend {points} points; level {level} has {level - 9}")

    for key, slots, item_keys, id_key, id_max in (
            ("glyphs", GLYPH_SLOTS, ("slot", "glyph_id", "spell_id", "name"), "glyph_id", 0xFFFF),
            ("equipment", EQUIPMENT_SLOTS, ("slot", "item_id", "name"), "item_id", 0xFFFFFF)):
        if not isinstance(v[key], list):
            r.fail(f"{p}.{key}", "must be an array")
            continue
        seen = set()
        for i, e in enumerate(v[key]):
            ep = f"{p}.{key}[{i}]"
            if not r.obj(e, ep, item_keys):
                continue
            slot = r.uint(e["slot"], f"{ep}.slot", 0, slots - 1)
            r.uint(e[id_key], f"{ep}.{id_key}", 1, id_max)
            if "spell_id" in e:
                r.uint(e["spell_id"], f"{ep}.spell_id", 1, 0xFFFFFF)
            r.text(e["name"], f"{ep}.name")
            if slot in seen:
                r.fail(f"{ep}.slot", "repeats a glyph slot" if key == "glyphs" else "repeats an equipment slot")
            seen.add(slot)

    kit_ids: list[int] = []
    if not isinstance(v["kit"], list) or not v["kit"]:
        r.fail(f"{p}.kit", "must be a non-empty array")
    else:
        for i, k in enumerate(v["kit"]):
            kp = f"{p}.kit[{i}]"
            if not r.obj(k, kp, ("spell_id", "name", "rank")):
                continue
            sid = r.uint(k["spell_id"], f"{kp}.spell_id", 1, 0xFFFFFF)
            r.text(k["name"], f"{kp}.name")
            r.uint(k["rank"], f"{kp}.rank", 1, 32)
            if sid in kit_ids:
                r.fail(f"{kp}.spell_id", "repeats a kit spell")
            kit_ids.append(sid)

    book = v["spellbook"]
    if not isinstance(book, list) or not book:
        r.fail(f"{p}.spellbook", "must be a non-empty array")
    else:
        ids = [r.uint(s, f"{p}.spellbook[{i}]", 1, 0xFFFFFF) for i, s in enumerate(book)]
        if ids != sorted(set(ids)):
            r.fail(f"{p}.spellbook", "must be sorted without repeats")
        for sid in kit_ids:
            if sid not in ids:
                r.fail(f"{p}.spellbook", f"lacks the kit spell {sid}")

    init = v["initial"]
    if r.obj(init, f"{p}.initial", ("max_health", "max_mana")):
        r.uint(init["max_health"], f"{p}.initial.max_health", 1, 10_000_000)
        r.uint(init["max_mana"], f"{p}.initial.max_mana", 1, 10_000_000)


def validate(doc) -> list[str]:
    """Every structural problem, in the C++ parser's wording; empty when the manifest is well formed."""
    r = _Reader()
    if not r.obj(doc, "manifest", TOP_KEYS):
        return r.errors
    if doc["kind"] != KIND:
        r.fail("kind", f'must be "{KIND}"')
    r.uint(doc["format"], "format", 1, 1)
    for key in ("id", "curriculum"):
        if not isinstance(doc[key], str) or not SLUG.match(doc[key]):
            r.fail(key, "must be 1-64 of a-z 0-9 _")
    r.uint(doc["version"], "version", 1, 1_000_000)
    r.text(doc["description"], "description", 512)

    rules = doc["rules"]
    if r.obj(rules, "rules", ("rated", "prep_seconds", "match_seconds")):
        if not isinstance(rules["rated"], bool):
            r.fail("rules.rated", "must be true or false")
        elif rules["rated"]:
            r.fail("rules.rated", "must be false: a training arena never touches rating")
        r.uint(rules["prep_seconds"], "rules.prep_seconds", 0, 120)
        r.uint(rules["match_seconds"], "rules.match_seconds", 1, 3600)

    arena = doc["arena"]
    bounds = None
    if r.obj(arena, "arena", ("map_id", "map", "battleground_type_id", "battleground", "arena_type", "bounds")):
        r.uint(arena["map_id"], "arena.map_id", 1, 0xFFFF)
        r.text(arena["map"], "arena.map")
        r.uint(arena["battleground_type_id"], "arena.battleground_type_id", 1, 0xFFFF)
        r.text(arena["battleground"], "arena.battleground")
        if r.uint(arena["arena_type"], "arena.arena_type", 2, 5) not in (0, 2, 3, 5):
            r.fail("arena.arena_type", "must be a stock arena type (2, 3 or 5)")
        b = arena["bounds"]
        if r.obj(b, "arena.bounds", ("x", "y", "radius", "z_min", "z_max")):
            bounds = tuple(r.number(b[k], f"arena.bounds.{k}") for k in ("x", "y", "radius", "z_min", "z_max"))
            if bounds[2] <= 0 or bounds[3] >= bounds[4]:
                r.fail("arena.bounds", "needs a positive radius and z_min below z_max")

    fixture = doc["fixture"]
    if r.obj(fixture, "fixture", ("account", "staging")):
        if not isinstance(fixture["account"], str) or not ACCOUNT.match(fixture["account"]):
            r.fail("fixture.account", "must be 3-16 of A-Z 0-9")
        r.pos(fixture["staging"], "fixture.staging", with_map=True)
        if isinstance(fixture["staging"], dict) and "map_id" in fixture["staging"]:
            r.uint(fixture["staging"]["map_id"], "fixture.staging.map_id", 0, 0xFFFF)

    actors = doc["actors"]
    if not isinstance(actors, list) or len(actors) != 2:
        r.fail("actors", "must hold exactly two actors")
    else:
        names, teams = [], []
        for i, a in enumerate(actors):
            ap = f"actors[{i}]"
            if not r.obj(a, ap, ("slot", "name", "team", "start")):
                continue
            if r.uint(a["slot"], f"{ap}.slot", 0, 1) != i:
                r.fail(f"{ap}.slot", f"must be {i}")
            if not isinstance(a["name"], str) or not CHARACTER.match(a["name"]):
                r.fail(f"{ap}.name", "must be 2-12 letters, capitalised")
            if a["team"] not in ("alliance", "horde"):
                r.fail(f"{ap}.team", 'must be "alliance" or "horde" (the arena side)')
            x, y, z, _ = r.pos(a["start"], f"{ap}.start")
            if bounds and bounds[2] > 0 and (math.hypot(x - bounds[0], y - bounds[1]) > bounds[2]
                                             or not bounds[3] <= z <= bounds[4]):
                r.fail(f"{ap}.start", "is outside the arena bounds")
            names.append(a["name"])
            teams.append(a["team"])
        if len(names) == 2 and names[0] == names[1]:
            r.fail("actors", "need two different names")
        if len(teams) == 2 and teams[0] == teams[1]:
            r.fail("actors", "need one actor on each side")

    _loadout(r, doc["loadout"])
    return r.errors


@dataclass(frozen=True)
class Manifest:
    path: Path
    data: dict
    sha256: str

    @property
    def id(self) -> str:
        return self.data["id"]


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ScenarioError(f"{path} is larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ScenarioError(f"{path} is not valid JSON: {e}") from None
    errors = validate(doc)
    if errors:
        raise ScenarioError(f"{path}: " + "; ".join(errors[:5]) + (f" (and {len(errors) - 5} more)" if len(errors) > 5
                                                                    else ""))
    return Manifest(path, doc, sha256(doc))


def pinned_hash(path: str | Path) -> str:
    """The hash recorded next to a manifest (<name>.sha256), which the configs pin."""
    return Path(path).with_suffix(".sha256").read_text(encoding="ascii").split()[0]
