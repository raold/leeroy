"""Seeded draws for the life model. Each draw is keyed by what it is for, so a switch that is off
draws nothing and moves no other draw (never the engine's shared rng)."""
import random
from hashlib import sha256


def unit(*parts) -> float:
    """A stable number in [0, 1) for these parts."""
    digest = sha256(("life:v1:" + ":".join(map(str, parts))).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def seeded(what: str, night: str, guid: int, minute: int, extra: int = 0) -> random.Random:
    """A generator of its own for one draw (what, night, member, minute, trigger)."""
    return random.Random(f"life:v1:{what}:{night}:{guid}:{minute}:{extra}")
