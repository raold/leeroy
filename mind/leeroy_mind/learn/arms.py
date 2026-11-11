"""Versioned numeric/enum variants. Rendering is deliberately a separate concern."""
from dataclasses import dataclass
import json
import math
import time
import uuid

FAMILIES = ("length", "questions", "humour", "callback", "tease", "typing", "followup")
STATES = ("control", "active", "proposed", "retired")
PRESETS = {
    "length": ({"min_words": 3, "max_words": 12}, {"min_words": 8, "max_words": 30},
               {"min_words": 20, "max_words": 60}),
    "questions": ({"rate": 0.0}, {"rate": 0.2}, {"rate": 0.5}),
    "humour": ({"level": "none"}, {"level": "light"}, {"level": "playful"}),
    "callback": ({"enabled": 0}, {"enabled": 1}),
    "tease": ({"level": "none"}, {"level": "light"}),
    "typing": ({"strength": 0.0}, {"strength": 0.5}, {"strength": 1.0}),
    "followup": ({"rate": 0.0, "window_s": 30}, {"rate": 0.1, "window_s": 30},
                 {"rate": 0.2, "window_s": 30}),
}


def controls():
    return {family: f"{family}.control.v1" for family in FAMILIES}


def valid(family, params, *, control=False):
    if family not in FAMILIES or type(params) is not dict:
        return False
    if control:
        return params == {"mode": "control"}
    def number(value, low, high):
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    if family == "length":
        return (set(params) == {"min_words", "max_words"}
                and type(params["min_words"]) is int and type(params["max_words"]) is int
                and 1 <= params["min_words"] <= params["max_words"] <= 80)
    if family in ("humour", "tease"):
        return set(params) == {"level"} and params["level"] in (
            ("none", "light", "playful") if family == "humour" else ("none", "light"))
    if family == "callback":
        return set(params) == {"enabled"} and type(params["enabled"]) is int and params["enabled"] in (0, 1)
    key = "strength" if family == "typing" else "rate"
    return (set(params) == ({key, "window_s"} if family == "followup" else {key})
            and number(params[key], 0, 1)
            and (family != "followup" or type(params["window_s"]) is int and params["window_s"] == 30))


@dataclass(frozen=True)
class Arm:
    id: str
    family: str
    params: dict
    version: int
    status: str
    origin: str
    created: float


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_arm'").fetchone() is not None


def ensure(conn, *, now=None):
    """Explicit write path only; disabled choices and inspection never seed the registry."""
    now = time.time() if now is None else now
    for family in FAMILIES:
        rows = [(controls()[family], {"mode": "control"}, "control")]
        rows += [(f"{family}.{i}.v1", p, "active") for i, p in enumerate(PRESETS[family])]
        for arm_id, params, status in rows:
            conn.execute("INSERT OR IGNORE INTO learn_arm VALUES (?,?,?,?,?,?,?)",
                         (arm_id, family, json.dumps(params, sort_keys=True), 1, status, "builtin", now))


def propose(conn, family, params, *, now=None):
    if not valid(family, params):
        raise ValueError("arm_parameters_invalid")
    arm_id = "coach." + uuid.uuid4().hex
    conn.execute("INSERT INTO learn_arm VALUES (?,?,?,?,?,?,?)", (arm_id, family,
                 json.dumps(params, sort_keys=True, allow_nan=False), 1, "proposed", "coach",
                 time.time() if now is None else now))
    return arm_id


def read(conn):
    if not available(conn):
        return []
    return [Arm(r[0], r[1], json.loads(r[2]), *r[3:])
            for r in conn.execute("SELECT * FROM learn_arm ORDER BY family,id")]


def bounded(arm, *, humour=0, forward=0):
    """Future fixed-wording renderer must use these caps; control means existing behaviour."""
    p = dict(arm.params)
    if arm.status == "control":
        return p
    if arm.family == "humour":
        if humour <= 0:
            p["level"] = "none"
        elif humour < 5 and p["level"] == "playful":
            p["level"] = "light"
    if arm.family == "tease" and forward <= 0:
        p["level"] = "none"
    return p
