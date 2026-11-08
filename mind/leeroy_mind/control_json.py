"""Bounded JSON objects for the console and control socket, without ambiguous fields."""
import json
import math

MAX_DEPTH = 32


def _object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = child
    return value


def _constant(_):
    raise ValueError("nonfinite JSON number")


def decode_body(raw: bytes):
    """A UTF-8 value with unique keys, finite numbers and at most 32 container levels.

    Callers cap raw bytes before parsing; no error includes request contents.
    """
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    pending = [(value, 0)]
    while pending:
        child, depth = pending.pop()
        if type(child) in (dict, list):
            if depth >= MAX_DEPTH:
                raise ValueError("JSON nesting limit")
            items = child.values() if type(child) is dict else child
            pending.extend((item, depth + 1) for item in items)
        elif type(child) is float and not math.isfinite(child):
            raise ValueError("nonfinite JSON number")
    return value
