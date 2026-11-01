"""Casting (Task 19): a stable voice spec for every member of the brief.

The brief is mind-data/voices/brief.json, written by the mind's `voices brief`: for each member
of the cast, their gender, accent, age, voice descriptors, speed, guild and raid role, and whether
they are core. Nothing else of a persona reaches here.

The recipe, per member, is drawn from a seeded stream (seed(guid, try) = crc32 of
"voice:<guid>:<try>"), so the same brief gives the same specs:
- 2-3 base voices of the member's gender and accent group (GROUPS) at Dirichlet(1) weights;
- core members and officers get a base graded B- or better (GRADES) at weight 0.6 or more, else,
  where the group has none (Kokoro has no male voice above C+), the best graded base it has;
- a British woman may, with Decision.uk_timbre_mix, take her timbre from US women's bases with a
  British prosody half; nobody else mixes groups;
- DESCRIPTOR_LEVERS turn the persona's words into speed, pitch, gain and the mic profile; without
  a word for it the speed is the persona's own, clamped to the allowed range, and the mic a seeded
  draw from MIC_PROFILES.
Each candidate is rendered dry on CAST_TEXT and embedded (ECAPA on the CPU); it is rejected when
its cosine to an accepted voice reaches the threshold. After `tries` the least similar candidate
is kept and flagged. Core members and officers are cast first, then the rest by guid.

A member who already has a spec keeps it, unless recast; a locked one is never changed. A recast
starts after the try the old spec came from, so recasting again moves on rather than back.

An expressive engine speaks a member from a reference clip (Task 22, I11):
render_designed_references gives each unlocked member <guid>.ref.wav beside their spec, from
`source`: a design engine in its lab venv (DesignWorker), on a description made from the brief
alone, or their own Kokoro blend (KokoroSource). Each reads REF_TEXT. The spec keeps its Kokoro
blend, that member's fallback voice, and gains `ref` and the engine. One engine per member."""
import json
import logging
import os
import random
import re
import tempfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Collection

import numpy as np
import soundfile as sf

from .decision import Decision
from .engines import EngineError
from .engines.expressive import (REFERENCE_MIN_S as REF_MIN_S, WorkerProcess,
                                 reference_fingerprint, validate_reference)
from .fx import MIC_PROFILES
from .logs import log
from .voices import (GROUPS, SPEC_VERSION, SPEED_RANGE, SpecError, VoiceSpec, spec_from_dict,
                     spec_to_dict)

CAST_TEXT = "The quick brown fox jumps over the lazy dog, and we pull at nine."
EMBED_RATE = 16000  # what ECAPA takes
THRESHOLD = 0.80
TRIES = 20
PAIRS = 10  # the closest pairs the report keeps for the audition
CLEAN_GRADE = "B-"  # core members and officers: this or better
CLEAN_WEIGHT = 0.6
MIN_WEIGHT = 0.02

# Kokoro's VOICES.md overall grades, from GROUPS
GRADES: dict[str, str] = {base: grade for grades in GROUPS.values()
                          for base, grade in grades.items()}
_ORDER = ("A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F+", "F")
# a word in a member's descriptors -> the ranges it sets: speed, pitch_st and gain_db are
# (low, high); mics are the profiles it chooses from. The first word to set a lever wins.
DESCRIPTOR_LEVERS: dict[str, dict] = {
    "quick": {"speed": (1.08, 1.15)},
    "measured": {"speed": (0.90, 0.95)},
    "slow": {"speed": (0.90, 0.95)},
    "low": {"pitch_st": (-2.0, -1.0)},
    "high": {"pitch_st": (1.0, 1.0)},
    "raspy": {"mics": ("hot_mic", "headset_cheap")},
    "hoarse": {"mics": ("hot_mic", "headset_cheap")},
    "quiet": {"gain_db": (-3.0, -1.5), "mics": ("soft_mic",)},
    "soft": {"gain_db": (-3.0, -1.5), "mics": ("soft_mic",)},
}
_WORDS = re.compile(r"[a-z]+")
BRIEF_KEYS = ("guid", "name", "gender", "accent", "age", "descriptors", "speed", "guild_role",
              "raid_role", "core")
# what every reference reads: the listening kit's (private/voice-test/render.py)
REF_TEXT = ("Hi, it's me. I just got home from work, made myself a cup of tea, and logged on for "
            "the night. Honestly, I could play until two in the morning. Is anyone around for "
            "heroics? My repair bill is paid, and I'm in a really good mood.")
DESIGN_TAKES = 3  # a designed voice gets this many to fit the member's gender (the kit's rule)
DESIGN_TIMEOUT_S = 900.0  # three takes on the CPU, at about 100 s each
PEAK = 0.99


class CastError(ValueError):
    pass


@dataclass(frozen=True)
class BriefEntry:
    guid: int
    name: str
    gender: str  # "female" or "male", as the persona says
    accent: str  # "en-gb" or "en-us"
    age: int
    descriptors: tuple[str, ...]
    speed: float
    guild_role: str
    raid_role: str
    core: bool

    @property
    def clean(self) -> bool:
        """Core members and officers get the best-graded voices."""
        return self.core or self.guild_role == "officer"

    @property
    def group(self) -> tuple[str, str]:
        """GROUPS' key: ("f"|"m", "a"|"b")."""
        return ("f" if self.gender == "female" else "m", "b" if self.accent == "en-gb" else "a")


def load_brief(path: Path) -> list[BriefEntry]:
    """The brief file's members; CastError for a file that is not the ten-key list."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CastError(f"{path}: cannot read the brief: {type(e).__name__}") from None
    if not isinstance(data, list):
        raise CastError(f"{path}: the brief is a JSON list of members")
    out = []
    for i, d in enumerate(data):
        if not isinstance(d, dict) or set(d) != set(BRIEF_KEYS):
            raise CastError(f"{path}: member {i}: expected exactly the keys "
                            f"{', '.join(BRIEF_KEYS)}")
        try:
            e = BriefEntry(guid=d["guid"], name=d["name"], gender=d["gender"],
                           accent=d["accent"], age=d["age"], descriptors=tuple(d["descriptors"]),
                           speed=float(d["speed"]), guild_role=d["guild_role"],
                           raid_role=d["raid_role"], core=bool(d["core"]))
        except (TypeError, ValueError):
            raise CastError(f"{path}: member {i}: a value is the wrong type") from None
        if (type(e.guid) is not int or e.gender not in ("female", "male")
                or e.accent not in ("en-gb", "en-us") or not isinstance(e.name, str)):
            raise CastError(f"{path}: member {i}: guid, name, gender or accent is not valid")
        out.append(e)
    return out


def seed(guid: int, attempt: int) -> int:
    return zlib.crc32(f"voice:{guid}:{attempt}".encode())


def _rank(base: str) -> int:
    return _ORDER.index(GRADES[base])


def _pool(group: tuple[str, str], names: Collection[str]) -> list[str]:
    return [b for b in GROUPS[group] if b in names]  # GROUPS' order: the draw is stable


def _weights(rng: random.Random, n: int, top: bool) -> list[float]:
    """Dirichlet(1) weights for n bases; with `top`, the first gets the largest and at least
    CLEAN_WEIGHT. Rounded to 4 places, the first taking the rounding so they sum to 1."""
    if n == 1:
        return [1.0]
    raw = [rng.gammavariate(1.0, 1.0) for _ in range(n)]
    w = [max(x / sum(raw), MIN_WEIGHT) for x in raw]
    w = [x / sum(w) for x in w]
    if top:
        w.sort(reverse=True)
        if w[0] < CLEAN_WEIGHT:
            rest = sum(w[1:])
            w = [CLEAN_WEIGHT] + [x * (1 - CLEAN_WEIGHT) / rest for x in w[1:]]
    w = [round(x, 4) for x in w]
    w[0] = round(1 - sum(w[1:]), 4)
    return w


def _blend(rng: random.Random, pool: list[str], *, clean: bool) -> dict[str, float]:
    n = min(rng.choice((2, 3)), len(pool))
    first = None
    if clean:
        good = [b for b in pool if _rank(b) <= _ORDER.index(CLEAN_GRADE)]
        if not good:  # no base that good in this group: the best it has
            best = min(_rank(b) for b in pool)
            good = [b for b in pool if _rank(b) == best]
        first = rng.choice(good)
    rest = [b for b in pool if b != first]
    bases = ([first] if first else []) + rng.sample(rest, n - (1 if first else 0))
    w = _weights(rng, n, top=clean)
    return dict(zip(bases, w))


def _prosody(rng: random.Random, pool: list[str]) -> dict[str, float]:
    n = min(rng.choice((1, 2)), len(pool))
    bases = rng.sample(pool, n)
    return dict(zip(bases, _weights(rng, n, top=False)))


def _levers(e: BriefEntry, rng: random.Random) -> dict:
    """speed, pitch_st, mic and gain_db from the descriptors (the first word to set a lever wins)."""
    chosen: dict = {}
    for text in e.descriptors:
        for word in _WORDS.findall(str(text).lower()):
            for key, value in DESCRIPTOR_LEVERS.get(word, {}).items():
                chosen.setdefault(key, value)
    out = {}
    for key in ("speed", "pitch_st", "gain_db"):
        if key in chosen:
            lo, hi = chosen[key]
            out[key] = round(lo + (hi - lo) * rng.random(), 3)
    if "speed" not in out:
        out["speed"] = round(min(max(e.speed, SPEED_RANGE[0]), SPEED_RANGE[1]), 3)
    out.setdefault("pitch_st", 0.0)
    out.setdefault("gain_db", 0.0)
    drawn = rng.choice(sorted(MIC_PROFILES))  # always drawn, so the stream does not depend on it
    mics = chosen.get("mics")
    out["mic"] = rng.choice(mics) if mics else drawn
    return out


def candidate(e: BriefEntry, attempt: int, bank, decision: Decision) -> VoiceSpec:
    """The member's spec for this try: the same every time for the same brief entry."""
    rng = random.Random(seed(e.guid, attempt))
    names = set(bank.names())
    lang = e.group[1]
    own = _pool(e.group, names)
    timbre, prosody = None, {}
    if e.group == ("f", "b") and decision.uk_timbre_mix and rng.random() < 0.5:
        us = _pool(("f", "a"), names)
        if us and own:
            timbre, prosody = _blend(rng, us, clean=e.clean), _prosody(rng, own)
    if timbre is None:
        if not own:
            raise CastError(f"{e.name}: the voices file has no {e.accent} "
                            f"{'women' if e.gender == 'female' else 'men'}'s voice")
        timbre = _blend(rng, own, clean=e.clean)
    levers = _levers(e, rng)
    return VoiceSpec(version=SPEC_VERSION, guid=e.guid, name=e.name, engine="kokoro", lang=lang,
                     timbre=timbre, prosody=prosody, locked=False, voices_sha256=bank.sha256,
                     ref="", **levers)


def _unit(v) -> np.ndarray:
    x = np.asarray(v, dtype=np.float64).reshape(-1)
    n = float(np.linalg.norm(x))
    return x / n if n > 0 else x


def _first_try(e: BriefEntry, old: VoiceSpec | None, bank, decision, tries: int) -> int:
    """Where a recast starts: after the try the old spec came from (0 when it came from none)."""
    if old is None:
        return 0
    for t in range(tries):
        try:
            if candidate(e, t, bank, decision) == old:
                return t + 1
        except (CastError, SpecError):
            break
    return 0


def cast_voices(brief: list[BriefEntry], bank, *, decision: Decision,
                embed: Callable[[np.ndarray], np.ndarray],
                render: Callable[[VoiceSpec], np.ndarray],
                existing: dict[int, VoiceSpec], threshold: float = THRESHOLD, tries: int = TRIES,
                today: str, recast: Collection[int] = (), report: dict | None = None
                ) -> list[VoiceSpec]:
    """A spec for every member of the brief, by guid. `render(spec)` is the spec's dry reading
    of CAST_TEXT as the audio `embed` takes (mono, EMBED_RATE); an existing spec is kept (and
    counted for distinctness) unless its guid is in `recast` and it is not locked. `report`,
    when given, is filled for the cast report (see below)."""
    recast = set(recast)
    ordered = sorted(brief, key=lambda e: (not e.clean, e.guid))
    out: dict[int, VoiceSpec] = {}
    vecs: dict[int, np.ndarray] = {}
    rows: dict[int, dict] = {}
    todo = []
    for e in ordered:
        old = existing.get(e.guid)
        if old is not None and (e.guid not in recast or old.locked):
            out[e.guid] = old
            vecs[e.guid] = _unit(embed(render(old)))
            rows[e.guid] = {"guid": e.guid, "name": e.name, "status": (
                "skipped_locked" if e.guid in recast else "kept"), "flagged": False}
        else:
            todo.append(e)
    for e in todo:
        old = existing.get(e.guid)
        start = _first_try(e, old, bank, decision, tries) if e.guid in recast else 0
        best = None
        cosines = []
        for t in range(start, start + tries):
            spec = candidate(e, t, bank, decision)
            vec = _unit(embed(render(spec)))
            top = max((float(vec @ v) for v in vecs.values()), default=-1.0)
            cosines.append(round(top, 6))
            if best is None or top < best[0]:
                best = (top, t, spec, vec)
            if top < threshold:
                break
        top, t, spec, vec = best
        flagged = top >= threshold
        out[e.guid], vecs[e.guid] = spec, vec
        rows[e.guid] = {"guid": e.guid, "name": e.name,
                        "status": "recast" if old is not None else "new", "try": t,
                        "tries": len(cosines), "cosines": cosines,
                        "max_cosine": round(top, 6), "flagged": flagged}
        log("cast_member", guid=e.guid, tries=len(cosines), flagged=flagged)
    if report is not None:
        guids = sorted(out)
        pairs = sorted(({"a": a, "b": b, "cosine": round(float(vecs[a] @ vecs[b]), 6)}
                        for i, a in enumerate(guids) for b in guids[i + 1:]),
                       key=lambda p: (-p["cosine"], p["a"], p["b"]))[:PAIRS]
        report.clear()
        report.update(
            date=today, threshold=threshold, tries=tries,
            members=[rows[g] for g in guids],
            flagged=[g for g in guids if rows[g]["flagged"]],
            skipped_locked=[g for g in guids if rows[g]["status"] == "skipped_locked"],
            closest_pairs=pairs)
    return [out[g] for g in sorted(out)]


# -- files and the real render/embed -------------------------------------------------------------

def load_existing(specs_dir: Path) -> dict[int, VoiceSpec]:
    """Every <guid>.json in the folder as a spec. CastError naming the file (never its text)
    for one that is not a valid spec, so a recast never overwrites what it could not read."""
    out = {}
    folder = Path(specs_dir)
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if not path.stem.isdigit():
            continue
        try:
            spec = spec_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:  # SpecError is a ValueError, as JSON's is
            raise CastError(f"{path.name}: not a valid voice spec ({type(e).__name__}); "
                            "fix or remove it") from None
        if spec.guid != int(path.stem):
            raise CastError(f"{path.name}: holds guid {spec.guid}")
        out[spec.guid] = spec
    return out


def write_spec(specs_dir: Path, spec: VoiceSpec) -> Path:
    """<specs_dir>/<guid>.json, whole or not at all."""
    folder = Path(specs_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{spec.guid}.json"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{spec.guid}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(spec_to_dict(spec), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def lock_specs(specs_dir: Path, which: str) -> list[int]:
    """Set `locked` on "all" the specs or the comma-separated guids; the guids changed. CastError
    for a guid with no spec (nothing is written then)."""
    existing = load_existing(specs_dir)
    if which.strip() == "all":
        wanted = sorted(existing)
    else:
        try:
            wanted = sorted({int(x) for x in which.split(",") if x.strip()})
        except ValueError:
            raise CastError(f"lock: expected 'all' or guids like 101,102; got {which!r}") from None
        if not wanted:
            raise CastError("lock: no guids given")
        missing = [g for g in wanted if g not in existing]
        if missing:
            raise CastError("lock: no voice spec for " + ", ".join(map(str, missing)))
    changed = []
    for g in wanted:
        if not existing[g].locked:
            write_spec(specs_dir, VoiceSpec(**{**asdict(existing[g]), "locked": True}))
            changed.append(g)
    return changed


def describe(e: BriefEntry) -> str:
    """The design engine's description: the brief's fields and nothing else of a persona (I11)."""
    who = (f"A {e.age}-year-old {'British' if e.accent == 'en-gb' else 'American'} "
           f"{'woman' if e.gender == 'female' else 'man'}")
    return f"{who}, {', '.join(e.descriptors)}." if e.descriptors else f"{who}."


class DesignWorker:
    """A design engine in its lab venv: one process for a whole run, replaced if it dies.
    `params` are added to every request."""

    def __init__(self, engine: str, python: Path, *, script: Path | None = None, args=(),
                 env: dict | None = None, timeout_s: float = DESIGN_TIMEOUT_S,
                 params: dict | None = None):
        self.params = dict(params or {})
        name = f"worker_{engine.replace('-', '_')}.py"
        self._make = lambda: WorkerProcess(
            python, script or Path(__file__).with_name("engines") / name, args=args, env=env,
            below_normal=True)
        self._timeout_s = timeout_s
        self._worker: WorkerProcess | None = None

    def clip(self, e: BriefEntry, spec: VoiceSpec) -> tuple[np.ndarray, int]:
        """The member's reference, (audio, rate). Raises EngineError."""
        if self._worker is None or not self._worker.up:
            self.close()
            self._worker = self._make()
            self._worker.start()
        params = {"instruct": describe(e), "gender": e.gender, "seed": seed(e.guid, 0),
                  "takes": DESIGN_TAKES, **self.params}
        return self._worker.ask(REF_TEXT, "", params, timeout=self._timeout_s)

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()


class KokoroSource:
    """The member's own Kokoro blend reading REF_TEXT: voice_source "kokoro"."""

    def __init__(self, engine, bank, decision: Decision):
        self._engine, self._bank, self._decision = engine, bank, decision

    def clip(self, e: BriefEntry, spec: VoiceSpec) -> tuple[np.ndarray, int]:
        style = self._bank.style(spec, "none", self._decision)
        return self._engine.synth(REF_TEXT, spec, style, spec.speed), self._engine.sample_rate

    def close(self) -> None:
        pass


def write_reference(path: Path, audio: np.ndarray, rate: int) -> None:
    """Validated mono 16-bit PCM, whole or not at all; bad audio never replaces a voice."""
    a = validate_reference(audio, rate)
    peak = float(np.abs(a).max())
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    os.close(fd)
    try:
        sf.write(tmp, a * min(1.0, PEAK / max(peak, PEAK)), rate, subtype="PCM_16", format="WAV")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _reference_ready(path: Path) -> bool:
    try:
        reference_fingerprint(path)
    except EngineError:
        return False
    return True


def render_designed_references(brief: list[BriefEntry], source, out_dir: Path, *,
                               decision: Decision, stock: Callable | None = None) -> dict:
    """A reference clip for each unlocked member of the brief whose engine the decision says is
    a cloning one, core members and officers first. `source` has clip(entry, spec) and close();
    `out_dir` holds the specs and the clips. A locked spec is never changed (N9), and listed. A
    run that stopped goes on where it was: a member with a valid clip keeps it; a missing or
    corrupt clip is repaired. One a source fails on stays as they were, and is listed.

    `stock()`, called once after the design source has closed, gives the StockCache of the
    expressive engine. Each member changed, or left half done by a run that stopped, loses their
    old folder and gets their callouts in the new voice (I6), so no recast follows. Returns the
    guids by what became of them."""
    out_dir = Path(out_dir)
    existing = load_existing(out_dir)
    report: dict = {"written": [], "kept": [], "locked": [], "uncast": [], "failed": []}
    done = []
    try:
        for e in sorted(brief, key=lambda e: (not e.clean, e.guid)):
            old = existing.get(e.guid)
            engine = decision.featured.get(e.guid, decision.engine)
            ref = f"{e.guid}.ref.wav"
            if engine == "kokoro":
                continue
            if old is None:
                report["uncast"].append(e.guid)
            elif old.locked:
                report["locked"].append(e.guid)
            elif old.engine == engine and old.ref == ref and _reference_ready(out_dir / ref):
                report["kept"].append(e.guid)
                done.append(old)
            else:
                try:
                    write_reference(out_dir / ref, *source.clip(e, old))
                except EngineError as err:
                    report["failed"].append(e.guid)
                    log("reference_failed", level=logging.WARNING, guid=e.guid, error=str(err))
                    continue
                spec = VoiceSpec(**{**asdict(old), "ref": ref, "engine": engine})
                write_spec(out_dir, spec)
                report["written"].append(e.guid)
                done.append(spec)
                log("reference_written", guid=e.guid)
    finally:
        source.close()
    if stock is not None and done:
        cache = stock()
        for spec in done:
            cache.drop_stale(spec)
            cache.render_missing(spec)
    return report


def make_render(engine, bank, decision: Decision) -> Callable[[VoiceSpec], np.ndarray]:
    """The render casting uses: CAST_TEXT dry in the spec's voice at its speed, at EMBED_RATE."""
    import soxr

    def render(spec: VoiceSpec) -> np.ndarray:
        style = bank.style(spec, "none", decision)
        audio = np.asarray(engine.synth(CAST_TEXT, spec, style, spec.speed), dtype=np.float32)
        return soxr.resample(audio, engine.sample_rate, EMBED_RATE).astype(np.float32)
    return render


def ecapa_embedder(source: Path, savedir: Path) -> Callable[[np.ndarray], np.ndarray]:
    """speechbrain's ECAPA on the CPU from the pinned local folder. local_strategy COPY copies the
    pinned files into savedir, so no symlink (which Windows allows only with a privilege, M10) is
    made; NO_LINK would leave speechbrain's pretrainer looking for them in savedir. Lazy: torch
    and speechbrain load here. The model's yaml points its weights at the Hub repo; the override
    points them at the same folder, so nothing is fetched."""
    import torch
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    classifier = EncoderClassifier.from_hparams(
        source=str(source), savedir=str(savedir), run_opts={"device": "cpu"},
        overrides={"pretrained_path": str(source)},  # the yaml names the Hub repo
        local_strategy=LocalStrategy.COPY)

    def embed(audio: np.ndarray) -> np.ndarray:
        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
        with torch.no_grad():
            return classifier.encode_batch(x).squeeze().cpu().numpy()
    return embed
