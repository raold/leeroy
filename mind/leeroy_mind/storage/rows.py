import math
import struct
from dataclasses import dataclass

KINDS = ("observation", "conversation", "event", "reflection", "plan", "story")
SOURCES = ("game", "chat", "extract", "reflection", "offscreen", "hearsay", "owner")
STATUSES = ("active", "superseded", "archived", "rejected")
REL_DIMS = ("affinity", "trust", "respect", "attraction", "familiarity")
REL_RANGES = {
    "affinity": (-100.0, 100.0),
    "trust": (0.0, 100.0),
    "respect": (0.0, 100.0),
    "attraction": (0.0, 100.0),
    "familiarity": (0.0, 100.0),
}
ROMANCE_STAGES = (
    "none", "notices", "banter", "whispers", "vulnerable", "invitation", "acknowledged", "ex")


@dataclass(frozen=True, slots=True)
class MemoryRow:
    """All times are mind-clock unix seconds. Evidence refs: chat:<id>, event:<id>, memory:<id>."""
    id: int | None
    owner: int
    kind: str
    text: str
    t_event: int
    t_created: int
    t_accessed: int
    access_n: int
    actors: tuple[int, ...]
    place: str | None
    boss_id: int | None
    raid_id: int | None
    importance: int
    valence: float
    source: str
    evidence: tuple[str, ...]
    confidence: float = 1.0
    status: str = "active"
    superseded_by: int | None = None
    key: str | None = None
    merged_n: int = 1
    embedding: bytes | None = None


@dataclass(frozen=True, slots=True)
class Relationship:
    """Scores are REAL and never rounded. last_seen is mind-clock unix seconds."""
    a: int
    b: int
    affinity: float = 0.0
    trust: float = 0.0
    respect: float = 0.0
    attraction: float = 0.0
    familiarity: float = 0.0
    label: str = ""
    romance_stage: str = "none"
    notes: str = ""
    pinned: tuple[int, ...] = ()
    last_seen: int = 0
    interactions: int = 0


@dataclass(frozen=True, slots=True)
class PersonaStateRow:
    """Self-notes are {"text": str, "evidence": [str], "added": float (unix seconds)}.
    state_since and updated are mind-clock unix seconds."""
    guid: int
    mood: float = 0.0
    mood_baseline: float = 0.0
    goals: tuple[str, ...] = ()
    self_notes: tuple[dict, ...] = ()
    altered_state: str = "sober"
    state_since: int = 0
    state_session: int = 0
    life_thread: str = ""
    updated: int = 0


@dataclass(frozen=True, slots=True)
class ChatLineRow:
    """ts is mind-clock unix seconds at receipt (not the server's ms). session_id None means
    the line arrived outside a session. audience: the cast guids who could see it (None for
    rows stored before schema v2, or when it was not known)."""
    id: int
    ts: float
    channel: str
    from_guid: int
    from_name: str
    to_guid: int
    text: str
    map: int
    zone: int
    session_id: int | None
    thread_id: int | None
    origin: str
    audience: tuple[int, ...] | None = None


def pack_vec(v) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def unpack_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
