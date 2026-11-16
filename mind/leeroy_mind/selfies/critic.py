"""The photo critic ([selfies] critic): what the selfie worker's detectors measured of each
rendered candidate (its `critic` op: faces, hands and fingers, eyes, identity cosine to her
reference) and what the vision judge (judge.py) said of it, one selfie_critic row each, and the
verdict both make: `clean`, or `flagged` with defect codes. A flagged situational or request
photo is never sent; best-of-N (snapstage, request_render) sends the clean candidate with the
best score. A flagged profile-set candidate keeps its place in the set and shows a red dot in the
console's gallery.

Rejected (codes): from the detectors a second face (extra_face), no face where one is expected
(no_face), more than two hands (extra_hands) or eyes off level with the mouth (eyes_tilt); from
the judge a limb, hand, finger or eye defect (limbs, hands, fingers, eyes) or another person
(extra_people). The score (0-1) ranks the clean ones: the judge's overall grade, the identity
cosine and the hands' shapes; a judge that says she is not the woman of the reference halves it.

Rows hold numbers and codes; `file` and `ref` are paths under mind-data built from integers.
Logs carry ids, counts and codes, never a path, a prompt or a word of the judge's."""
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log

EYE_TILT_MAX = 8.0  # degrees between the eye line and the mouth line
EYE_LEVEL_MAX = 0.12  # one eye above the other, in eye distances
IDENTITY_LOW, IDENTITY_HIGH = 0.5, 0.9  # the cosine's span the score reads
MAX_FACES = 64
MAX_HANDS = 64
DETECTOR_STATES = ("ok", "unavailable", "failed")
IDENTITY_CODES = ("", "ok", "no_ref", "no_face", "encoder_unavailable", "encoder_failed")
REJECT = ("extra_face", "no_face", "extra_hands", "eyes_tilt", "limbs", "hands", "fingers",
          "eyes", "extra_people")
PENDING_LIMIT = 200  # rows one judge pass takes


class Malformed(ValueError):
    pass


@dataclass(frozen=True)
class Hand:
    conf: float
    fingers: int
    plausible: bool
    spread: float


@dataclass(frozen=True)
class Critique:
    """What the worker's detectors measured of one image (None: that detector unavailable)."""
    faces: int | None = None
    hands: int | None = None
    hand: tuple[Hand, ...] = ()
    eye_tilt: float | None = None
    eye_level: float | None = None
    gaze_gap: float | None = None
    identity: float | None = None
    identity_code: str = ""
    detectors: tuple[tuple[str, str], ...] = (("faces", "unavailable"), ("hands", "unavailable"))
    seconds: float = 0.0
    odd: int = -1  # a stored row's hands_odd; -1: counted from `hand`

    @property
    def hands_odd(self) -> int:
        """Hands whose shape the landmarks find odd: fewer than four distinct fingers, or
        implausible."""
        if self.odd >= 0:
            return self.odd
        return sum(1 for h in self.hand if h.fingers < 4 or not h.plausible)


def _count(v, hi: int):
    if v is None:
        return None
    if type(v) is not int or not 0 <= v <= hi:
        raise Malformed("count")
    return v


def _real(v, lo: float, hi: float):
    if v is None:
        return None
    if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
        raise Malformed("number")
    return float(v)


def from_answer(answer: dict) -> Critique:
    """The worker's critic answer as a Critique; Malformed for anything off the protocol."""
    if type(answer) is not dict:
        raise Malformed("answer")
    hands = answer.get("hand", [])
    if type(hands) is not list or len(hands) > 16:
        raise Malformed("hand")
    parsed = []
    for h in hands:
        if type(h) is not dict or type(h.get("plausible")) is not bool:
            raise Malformed("hand")
        parsed.append(Hand(_real(h.get("conf"), 0, 1), _count(h.get("fingers"), 5),
                           h["plausible"], _real(h.get("spread"), 0, 1000)))
    eyes = answer.get("eyes")
    if eyes is not None and type(eyes) is not dict:
        raise Malformed("eyes")
    eyes = eyes or {}
    states = answer.get("detectors")
    if (type(states) is not dict or set(states) != {"faces", "hands"}
            or any(v not in DETECTOR_STATES for v in states.values())):
        raise Malformed("detectors")
    code = answer.get("identity_code", "")
    if code not in IDENTITY_CODES:
        raise Malformed("identity_code")
    return Critique(
        faces=_count(answer.get("faces"), MAX_FACES), hands=_count(answer.get("hands"), MAX_HANDS),
        hand=tuple(parsed), eye_tilt=_real(eyes.get("tilt"), 0, 360),
        eye_level=_real(eyes.get("level"), 0, 100), gaze_gap=_real(eyes.get("gaze_gap"), 0, 100),
        identity=_real(answer.get("identity"), -1, 1), identity_code=code,
        detectors=(("faces", states["faces"]), ("hands", states["hands"])),
        seconds=_real(answer.get("seconds"), 0, 3600) or 0.0)


@dataclass(frozen=True)
class Verdict:
    codes: tuple[str, ...]
    score: float

    @property
    def clean(self) -> bool:
        return not self.codes


def detector_codes(c: "Critique | None", *, face_expected: bool = True) -> tuple[str, ...]:
    if c is None:
        return ()
    out = []
    if c.faces is not None and c.faces > 1:
        out.append("extra_face")
    if c.faces == 0 and face_expected:
        out.append("no_face")
    if c.hands is not None and c.hands > 2:
        out.append("extra_hands")
    if ((c.eye_tilt is not None and c.eye_tilt > EYE_TILT_MAX)
            or (c.eye_level is not None and c.eye_level > EYE_LEVEL_MAX)):
        out.append("eyes_tilt")
    return tuple(out)


def score(c: "Critique | None", judged=None) -> float:
    """0-1: the judge's grade (weight 0.6), the identity cosine across IDENTITY_LOW..HIGH (0.3)
    and the share of well-shaped hands (0.1), over the parts there are; 0.5 with none. A judge
    that says it is not the woman of the reference halves it."""
    parts = []
    if judged is not None:
        parts.append((0.6, judged.overall / 3))
    if c is not None and c.identity is not None:
        parts.append((0.3, min(1.0, max(0.0, (c.identity - IDENTITY_LOW)
                                         / (IDENTITY_HIGH - IDENTITY_LOW)))))
    if c is not None and c.hands is not None:
        parts.append((0.1, 1.0 if not c.hand else 1 - c.hands_odd / len(c.hand)))
    value = 0.5 if not parts else sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    if judged is not None and judged.same_person is False:
        value /= 2
    return round(value, 4)


def verdict(c: "Critique | None", judged=None, *, face_expected: bool = True) -> Verdict:
    codes = list(detector_codes(c, face_expected=face_expected))
    if judged is not None:
        codes += [k for k in judged.codes if k not in codes]
    return Verdict(tuple(k for k in REJECT if k in codes), score(c, judged))


# -- rows -----------------------------------------------------------------------------------

def rel(data_dir, path) -> str:
    """`path` under the data dir, with forward slashes (every part is an integer or a fixed
    name, so the string names nothing else)."""
    return Path(path).absolute().relative_to(Path(data_dir).absolute()).as_posix()


def insert(store, *, run: str, purpose: str, guid: int, seed: int, file: str,
           critique: "Critique | None", set_no: int | None = None, idx: int | None = None,
           snap_id: int | None = None, ref: str | None = None, crop=None,
           framing: str | None = None, outfit: str | None = None, prop: str | None = None,
           scale: float | None = None, steps: int | None = None, arm: bool = False,
           face_expected: bool = True, now: float) -> int:
    """One pending row (the caller's transaction, or its own); its id."""
    c = critique
    row = (now, run, purpose, guid, set_no, idx, snap_id, seed, file, ref,
           None if crop is None else json.dumps([round(float(v), 4) for v in crop]),
           framing, outfit, prop, scale, steps, int(bool(arm)), int(bool(face_expected)),
           None if c is None else c.faces, None if c is None else c.hands,
           None if c is None else json.dumps([h.fingers for h in c.hand]),
           None if c is None else c.hands_odd, None if c is None else c.eye_tilt,
           None if c is None else c.eye_level, None if c is None else c.gaze_gap,
           None if c is None else c.identity,
           json.dumps(dict(c.detectors)) if c is not None else None)
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO selfie_critic(ts, run, purpose, guid, set_no, idx, snap_id, seed, file, "
            "ref, crop, framing, outfit, prop, scale, steps, arm, face_expected, faces, hands, "
            "fingers, hands_odd, eye_tilt, eye_level, gaze_gap, identity, detectors) "
            "VALUES (" + ",".join("?" * 27) + ")", row)
        return cur.lastrowid


def _critique_of(r) -> Critique | None:
    """The detector fields of a stored row back as a Critique (hands' shapes as counts)."""
    if r["detectors"] is None:
        return None
    hand = tuple(Hand(1.0, f, True, 1.0) for f in json.loads(r["fingers"] or "[]"))
    return Critique(faces=r["faces"], hands=r["hands"], hand=hand, eye_tilt=r["eye_tilt"],
                    eye_level=r["eye_level"], gaze_gap=r["gaze_gap"], identity=r["identity"],
                    detectors=tuple(json.loads(r["detectors"]).items()),
                    odd=r["hands_odd"] or 0)


_COLS = ("id, guid, file, ref, faces, hands, fingers, hands_odd, eye_tilt, eye_level, gaze_gap, "
         "identity, detectors, face_expected, verdict, purpose, scale, steps, framing")


def _rows(store, sql: str, params=()) -> list[dict]:
    names = [c.strip() for c in _COLS.split(",")]
    with store.transaction():
        return [dict(zip(names, tuple(r))) for r in store.conn.execute(
            f"SELECT {_COLS} FROM selfie_critic WHERE {sql}", params).fetchall()]


def _finish(store, r: dict, judged, model: str | None, *, now: float) -> Verdict:
    v = verdict(_critique_of(r), judged, face_expected=bool(r["face_expected"]))
    j = judged
    with store.transaction():
        store.conn.execute(
            "UPDATE selfie_critic SET judge_model=?, limbs=?, hands_judged=?, fingers_ok=?, "
            "eyes_ok=?, extra_people=?, same_person=?, overall=?, verdict=?, codes=?, score=? "
            "WHERE id=? AND verdict='pending'",
            (model if j is not None else None, j and j.limbs, j and j.hands,
             None if j is None else int(j.fingers_ok), None if j is None else int(j.eyes_ok),
             None if j is None else int(j.extra_people),
             None if j is None or j.same_person is None else int(j.same_person),
             None if j is None else j.overall, "clean" if v.clean else "flagged",
             ",".join(v.codes), v.score, r["id"]))
    return v


async def settle(store, data_dir, judge, *, ids=None, run: str | None = None, now: float,
                 on_settled=None) -> dict[int, Verdict]:
    """The pending rows (those of `ids`, else those of `run`, else every pending row, at most
    PENDING_LIMIT) judged and given their verdict: with `judge` (judge.VisionJudge, or None)
    while its model is there and the GPU is not paused, else on the detectors alone. A row whose
    image is gone is settled on its detectors. `on_settled(row, verdict)` runs after each (the
    parameter bandit's credit). Returns the verdicts by row id."""
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        rows = _rows(store, f"verdict='pending' AND id IN ({','.join('?' * len(ids))})",
                     tuple(ids))
    elif run is not None:
        rows = _rows(store, "verdict='pending' AND run=? ORDER BY id LIMIT ?", (run, PENDING_LIMIT))
    else:
        rows = _rows(store, "verdict='pending' ORDER BY id LIMIT ?", (PENDING_LIMIT,))
    out, judged_n, began = {}, 0, time.monotonic()
    for r in rows:
        answer = None
        image = Path(data_dir) / r["file"]
        if judge is not None and image.is_file() and not judge.paused:
            ref = Path(data_dir) / r["ref"] if r["ref"] else None
            answer = await judge.judge(image, ref if ref is not None and ref.is_file() else None)
            judged_n += answer is not None
        v = _finish(store, r, answer, None if judge is None else judge.model, now=now)
        out[r["id"]] = v
        if on_settled is not None:
            try:
                on_settled(r, v, answer)
            except Exception as e:  # a credit that fails never unsettles a verdict
                log("critic_credit_failed", level=logging.WARNING, error=type(e).__name__)
    if rows:
        log("critic_settled", rows=len(rows), judged=judged_n,
            flagged=sum(1 for v in out.values() if not v.clean),
            seconds=round(time.monotonic() - began, 1))
    return out


def best(verdicts: dict[int, Verdict]) -> int | None:
    """The clean row with the highest score (the lowest id on a tie), or None."""
    clean = [(v.score, -i, i) for i, v in verdicts.items() if v.clean]
    return max(clean)[2] if clean else None


def mark_chosen(conn, row_id: int) -> None:
    conn.execute("UPDATE selfie_critic SET chosen=1 WHERE id=?", (row_id,))


async def unload(admin, model: str) -> None:
    """The judge's model out of VRAM once its pass is done (never in a cancel's cleanup)."""
    if admin is None:
        return
    try:
        await admin.unload(model)
    except Exception as e:
        log("critic_judge_unload_failed", level=logging.WARNING, error=type(e).__name__)


# -- what a row was rendered from (codes only) -------------------------------------------------

def scene_codes(scene) -> tuple[str, str, str]:
    """(framing, outfit, prop) codes of a composed Scene: the enum keys of the plain lists, 'lust'
    for a lust outfit, 'none' for no prop."""
    from .scene_choices import OUTFITS, PROPS
    from .style import LUST
    outfit = next((k for k, v in OUTFITS.items() if v == scene.outfit), None)
    if outfit is None:
        outfit = "lust" if getattr(scene, "style", None) == LUST else "other"
    prop = next((k for k, v in PROPS.items() if v and scene.activity.endswith(", " + v)), "none")
    return scene.framing, outfit, prop


def flags(conn, guid: int) -> dict[tuple[int, int], str]:
    """(set_no, idx) -> the comma-separated codes of every flagged profile-set candidate of
    `guid`; an older database without the table has none."""
    import sqlite3
    try:
        rows = conn.execute("SELECT set_no, idx, codes FROM selfie_critic WHERE guid=? AND "
                            "purpose='set' AND verdict='flagged' ORDER BY id", (guid,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(r[0], r[1]): r[2] for r in rows}


def report(conn) -> dict:
    """Defect rates of the settled rows by purpose, framing, outfit, prop and scale: counts and
    the share flagged per code. Read-only."""
    rows = conn.execute("SELECT purpose, framing, outfit, prop, scale, steps, verdict, codes, "
                        "overall FROM selfie_critic WHERE verdict<>'pending'").fetchall()
    out: dict = {"rows": len(rows), "pending": conn.execute(
        "SELECT count(*) FROM selfie_critic WHERE verdict='pending'").fetchone()[0]}
    for i, key in enumerate(("purpose", "framing", "outfit", "prop", "scale", "steps")):
        groups: dict = {}
        for r in rows:
            g = groups.setdefault(str(r[i]) if r[i] is not None else "-",
                                  {"n": 0, "flagged": 0, "codes": {}, "overall": []})
            g["n"] += 1
            if r[6] == "flagged":
                g["flagged"] += 1
            for code in filter(None, (r[7] or "").split(",")):
                g["codes"][code] = g["codes"].get(code, 0) + 1
            if r[8] is not None:
                g["overall"].append(r[8])
        out[key] = {k: {"n": g["n"], "flagged": round(g["flagged"] / g["n"], 3),
                        "codes": {c: round(n / g["n"], 3) for c, n in sorted(g["codes"].items())},
                        "overall": (round(sum(g["overall"]) / len(g["overall"]), 2)
                                    if g["overall"] else None)}
                    for k, g in sorted(groups.items())}
    return out
