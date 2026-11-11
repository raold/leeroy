"""Deterministic fictional appearance priors and bounded anti-duplication.

Ancestry is an authored fictional descriptor in face[0], not inferred from a real
person. Hair texture is carried in hair_style. Existing appearance schema suffices.
See looks_diversity.md for the coarse regional priors and their limits.
"""
from dataclasses import asdict, replace
import math
import random
import sqlite3
import tomllib
from pathlib import Path

from leeroy_mind.personas.schema import Appearance
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.selfies.style import AGE_RE

# Categorical near-duplicate guard, not a perceptual/diversity score. Free-text
# legacy looks rarely share exact tokens; ordinary random draws usually pass.
MIN_LOOK_DISTANCE = .22
MIN_LOOK_AGE = 25
MAX_DRAWS = 128
ANCESTRIES = ("European ancestry", "South Asian ancestry", "East Asian ancestry",
              "African ancestry", "Latin American ancestry", "mixed ancestry",
              "Indigenous American ancestry")
REGIONS = {"UK": (86, 6, 2, 3, 0, 3, 0), "US": (63, 2, 3, 12, 16, 3, 1)}
SKIN = ("fair", "light", "light olive", "olive", "light brown", "medium brown", "deep brown")
SKIN_WEIGHTS = ((4, 5, 3, 2, 1, 0, 0), (0, 1, 2, 3, 5, 4, 1),
                (1, 4, 3, 2, 2, 1, 0), (0, 0, 0, 1, 2, 4, 5),
                (1, 2, 3, 4, 4, 2, 1), (1, 2, 3, 3, 3, 3, 2), (0, 1, 2, 3, 4, 3, 1))
COLOURS = ("black", "dark brown", "chestnut", "auburn", "copper", "dark blonde", "blonde", "grey")
LENGTHS = ("shaved", "short", "chin", "shoulder", "long", "very_long")
TEXTURES = ("straight", "wavy", "curly", "coily")
STYLES = ("side part", "centre part", "swept back", "loose layers", "neatly cropped", "tied back")
EYES = ("brown", "dark brown", "hazel", "green", "blue", "grey")
FACES = ("an oval face", "a strong jaw", "a round face", "high cheekbones", "a broad nose",
         "a straight nose", "an aquiline nose", "arched brows", "thick brows")
DETAILS = ("freckles", "glasses", "dimples", "a beauty mark", "a cleft chin", "a nose stud",
           "smile lines", "a faint eyebrow scar", "a square chin")
PC_WEAR = ("a cotton shirt", "a plain sweater", "a zip hoodie", "a long-sleeved top")
BUILDS = ("slim", "athletic", "average", "curvy", "stocky", "heavy")
BODY_WEIGHTS = (20, 22, 28, 12, 10, 8)
BMI = {"slim": (19,22), "athletic": (22,27), "average": (22,28), "curvy": (26,33),
       "voluptuous": (29,36), "stocky": (27,34), "heavy": (32,40)}


class LooksRefused(ValueError):
    """Fixed reason codes only: safe to report without persona text."""


def enabled(root):
    try:
        raw = tomllib.loads((Path(root) / "mind.toml").read_text(encoding="utf-8-sig"))
        table = raw.get("looks", {})
        return isinstance(table, dict) and set(table) <= {"diversity"} and table.get("diversity") is True
    except (OSError, ValueError, UnicodeError):
        return False


def protection(conn, guid):
    """Field-change evidence wins; old rows have only their current source as evidence.

    The ledger records notes/stance/household, not historic appearance versions.
    The migration therefore seeds owner rows only. Subsequent owner changes to
    appearance are captured by the two triggers, even after generated saves.
    """
    try:
        row = conn.execute("SELECT reason FROM persona_looks_protection WHERE guid=?", (guid,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        row = conn.execute("SELECT source FROM persona_core WHERE guid=?",(guid,)).fetchone()
        return "owner" if row and row[0] == "owner" else None


def looks_vector(a):
    """Sparse categorical vector, with coarse height/weight bins; no learned model."""
    result = {}
    for field, weight in (("build", 1), ("hair_colour", 2), ("hair_length", 1),
                          ("hair_style", 2), ("skin_tone", 2), ("eye_colour", 1)):
        result[field + ":" + " ".join(getattr(a, field).casefold().split())] = math.sqrt(weight)
    for face in a.face:
        result["face:" + " ".join(face.casefold().split())] = 1.0
    result[f"height:{a.height_cm // 10}"] = .5
    result[f"weight:{a.weight_kg // 15}"] = .5
    return result


def distance(left, right):
    dot = sum(v * right.get(k, 0) for k, v in left.items())
    norm = math.sqrt(sum(v*v for v in left.values()) * sum(v*v for v in right.values()))
    return max(0, min(1, 1 - dot/norm)) if norm else 0


def _candidate(p, rng):
    ancestry = rng.choices(range(len(ANCESTRIES)), weights=REGIONS[p.identity.region])[0]
    lust = is_woman(p) and p.romance.stance == "lust"
    build = rng.choices(("curvy", "voluptuous"), weights=(3,2))[0] if lust else rng.choices(BUILDS, weights=BODY_WEIGHTS)[0]
    height = max(145, min(205, round(rng.gauss(164 if is_woman(p) else 178, 8))))
    weight = max(40, min(180, round((height/100)**2 * rng.uniform(*BMI[build]))))
    length = rng.choice(LENGTHS)
    style = "closely shaved" if length == "shaved" else rng.choice(TEXTURES) + " " + rng.choice(STYLES)
    return Appearance(height, build, weight, rng.choice(COLOURS), length, style,
        rng.choices(SKIN, weights=SKIN_WEIGHTS[ancestry])[0], rng.choice(EYES),
        (ANCESTRIES[ancestry], rng.choice(FACES), rng.choice(DETAILS)), rng.choice(PC_WEAR))


def safe(p, appearance, *, ident, names=()):
    from leeroy_mind.nightly.enrich import LIKENESS_RE, screen_enrichment
    from leeroy_mind.selfies.kind import ERA_RE, EXPLICIT_RE, WEAR_RE
    if p.identity.age < MIN_LOOK_AGE:
        return False
    text = " ".join(str(v) if not isinstance(v, tuple) else " ".join(v)
                    for v in asdict(appearance).values())
    if any(rx.search(text) for rx in (AGE_RE, LIKENESS_RE, ERA_RE, EXPLICIT_RE, WEAR_RE)):
        return False
    if names_real_name(text, ident) or any(has_word(text, n) for n in names if n):
        return False
    data = dict(appearance=asdict(appearance), disclosures=[], drawn_to=[], turned_off_by=[])
    data["appearance"]["face"] = list(appearance.face)
    return not screen_enrichment(p, data, ident=ident, member_names=names)


def sample(p, existing=(), *, seed="v1", ident, names=()):
    if p.identity.age < MIN_LOOK_AGE or p.identity.region not in REGIONS:
        raise LooksRefused("looks_identity_invalid")
    rng = random.Random(f"looks-v1:{p.guid}:{seed}")
    # Include her own old appearance: a reroll must actually look different.
    vectors = [looks_vector(q.appearance) for q in existing if q.appearance is not None]
    protected = tuple(names) + (p.character_name, p.identity.real_name)
    for _ in range(MAX_DRAWS):
        candidate = _candidate(p, rng)
        if not safe(p, candidate, ident=ident, names=protected):
            continue
        vector = looks_vector(candidate)
        if all(distance(vector, v) >= MIN_LOOK_DISTANCE for v in vectors):
            return candidate
    raise LooksRefused("looks_no_distinct_candidate")


def apply_if_enabled(store, p, diversity, existing, *, ident, names=()):
    """Generator hook: off preserves the exact object and does no new DB reads."""
    if not diversity or protection(store.conn, p.guid):
        return p
    return replace(p, appearance=sample(p, existing, ident=ident, names=names))


def mark_sampled(store, p):
    if p.appearance is not None:
        store.set_meta(f"looks:sampled:{p.guid}",str(store.persona_versions()[p.guid]))


def needs_enrichment(store, guid):
    """Sampled looks still waiting for their first enrichment. The marker stays until the enrich
    save deletes it, so a refused or deferred enrichment (or a later save such as the photo pass,
    which moves the version) leaves it due; the caller also requires empty disclosures."""
    return store.get_meta(f"looks:sampled:{guid}") is not None
