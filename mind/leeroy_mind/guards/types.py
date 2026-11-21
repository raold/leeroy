"""Private draft/history inputs and content-free decisions."""
from dataclasses import dataclass, field
import sqlite3

from .config import GuardsCfg


@dataclass(frozen=True)
class Draft:
    guid: int
    channel: str
    text: str = field(repr=False)
    parent_id: int | None = None
    thread_ids: tuple[int, ...] = ()
    embedding: bytes | None = field(default=None, repr=False)
    to_guid: int = 0
    # For game, the original say/party/raid/etc. channel; for Discord/voice, its room id.
    room: str = field(default='', repr=False)
    now: float | None = None
    map: int = 0
    zone: int = 0
    real_answer_due: bool = False
    # Only replay supplies this: an already-stored candidate must not match itself.
    line_id: int | None = None
    anchor: str = field(default='', repr=False)
    server: bool = False
    anchor_embedding: bytes | None = field(default=None, repr=False)
    readers: tuple[int, ...] = ()
    # Her own earlier lines the prompt showed as examples (talk/grounding.py): a draft that
    # copies one is `phrase_exemplar` (guards/phrases.py)
    exemplars: tuple[str, ...] = field(default=(), repr=False)
    # A WoW turn with [talk] wow_topic on (talk/wowtopic.py): the chat register is checked
    # (guards/register.py) and her own words are rationed tighter (guards/voice.py)
    wow_topic: bool = False


@dataclass(frozen=True)
class HistoryLine:
    id: int
    ts: float
    guid: int
    text: str = field(repr=False)
    channel: str = 'guild'
    to_guid: int = 0
    room: str = field(default='', repr=False)
    thread_id: int | None = None
    embedding: bytes | None = field(default=None, repr=False)
    readers: tuple[int, ...] | None = None
    origin: str = 'mind'
    own_repeat_only: bool = False


@dataclass(frozen=True)
class GuardStores:
    conn: sqlite3.Connection
    config: GuardsCfg = field(default_factory=GuardsCfg)
    claims_enabled: bool = False
    # Caller-observed Discord/DM history; captured readers are mandatory there.
    history: tuple[HistoryLine, ...] = field(default=(), repr=False)
    # Cached vectors for stored chat ids, all in the draft's embedding space.
    vectors: dict[int, bytes] = field(default_factory=dict, repr=False)
    # An already-loaded eval.talk.unawareness.Facts snapshot, never loaded per draft.
    facts: object | None = field(default=None, repr=False)
    clock: object | None = None
    cross_medium: bool = False
    # His names as the guild calls him (nickname, character name): a "he" beside one may be him.
    his_names: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Score:
    code: str
    value: float


@dataclass(frozen=True)
class Reference:
    kind: str
    id: int


@dataclass(frozen=True)
class Verdict:
    action: str
    codes: tuple[str, ...] = ()
    scores: tuple[Score, ...] = ()
    related: tuple[Reference, ...] = ()
    reaction: str | None = None
    # The phrase cooldown's words, quoted from the draft itself for the rewrite hint; never logged.
    phrases: tuple[str, ...] = field(default=(), repr=False)
    # Common stock entries an answer owed to him leaned on (phrases.Found.pending): no rewrite,
    # a hint on her next turn; the list's own words, never logged.
    pending: tuple[str, ...] = field(default=(), repr=False)
    # The message she answers when her opening says it back (`echo_opener`), for opener.cut on
    # an owed answer's second draft; never logged.
    echo_of: str = field(default='', repr=False)
    # `member_misgender`: (member's name, the pronoun as written, "she/her" or "he/him") for the
    # rewrite hint (pronouns.hint); never logged.
    pronouns: tuple[tuple[str, str, str], ...] = field(default=(), repr=False)
    # `voice_never`: the words of her voice sheet's never list the draft used, as the sheet
    # has them, for the rewrite hint (voice.hint); never logged.
    never: tuple[str, ...] = field(default=(), repr=False)
