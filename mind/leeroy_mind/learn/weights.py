"""Append-only weight versions. Callers own transactions; reads never initialize data."""
from dataclasses import dataclass
import json
import math
from types import MappingProxyType

DEFAULTS = MappingProxyType(dict(explicit=.40, engagement=.25, emote=.15, quality=.20))


@dataclass(frozen=True)
class Weights:
    version: int
    values: dict


def available(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"learn_weights", "talk_recipe", "talk_reward", "talk_signal", "chat_line"} <= names


def validate(values):
    if (type(values) is not dict or set(values) != set(DEFAULTS)
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                   for v in values.values()) or not math.isclose(sum(values.values()), 1, abs_tol=1e-9)):
        raise ValueError("invalid_learn_weights")
    return {key: float(values[key]) for key in DEFAULTS}


def read(conn, *, at):
    if available(conn):
        row = conn.execute("SELECT version,weights FROM learn_weights WHERE created<=? "
                           "ORDER BY created DESC,version DESC LIMIT 1", (at,)).fetchone()
        if row is not None:
            try:
                return Weights(row[0], validate(json.loads(row[1])))
            except (ValueError, TypeError, RecursionError):
                pass
    return Weights(0, dict(DEFAULTS))


def change(conn, values, *, now):
    values = validate(values)
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("invalid_weights_time")
    version = conn.execute("INSERT INTO learn_weights(created,weights) VALUES (?,?)",
                           (now, json.dumps(values, sort_keys=True, allow_nan=False))).lastrowid
    return Weights(version, values)


def ensure(conn, *, now):
    weights = read(conn, at=now)
    return weights if weights.version else change(conn, dict(DEFAULTS), now=now)
