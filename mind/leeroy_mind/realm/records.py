"""Immutable Realm domain shapes, without storage or public-proof authority.

Construction checks bounded shape only. A PublicProof still needs the real
source/privacy resolver and every effect needs its current eligibility checks.
External ingress, event and action types are annotations only in this leaf.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
import re
import struct
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from leeroy_mind.ingress import RowReceipt
    from leeroy_mind.protocol import Event
    from .actions import ActionContext


FactPolicy = Literal["verified_raid_kill_v1", "bg_result_v1", "arena_result_v1",
                     "wg_result_v1", "pvp_result_v1", "channel_raid_topic_v1",
                     "realm_first_achievement_v1", "duel_result_v1", "verified_dungeon_kill_v1"]
PublicPolicy = Literal["verified_raid_kill_v1", "bg_result_v1", "arena_result_v1",
                       "wg_result_v1", "pvp_result_v1", "channel_raid_topic_v1",
                       "realm_first_achievement_v1", "duel_result_v1", "verified_dungeon_kill_v1",
                       "stored_forum_topic_v1"]
Assurance = Literal["verified_event", "observed_event", "observed_topic", "stored_topic"]
AnnouncementReason = Literal["none", "expired", "context_unavailable", "capacity", "proof_unavailable",
    "payload_mismatch", "native_refused", "transport_unknown", "recovered_prepared", "recovered_dispatched",
    "evidence_conflict", "clock_conflict", "pretransport_refused"]
QuestionReason = Literal["none", "expired", "context_unavailable", "capacity", "proof_unavailable",
    "payload_mismatch", "native_refused", "transport_unknown", "recovered_prepared", "recovered_dispatched",
    "evidence_conflict", "clock_conflict", "pretransport_refused", "model_unavailable", "lead_unavailable",
    "speech_refused", "record_failed", "text_mismatch", "chat_output_unproved"]
OutputFields = tuple[tuple[str, int | str | bool], ...]

_FACT_POLICIES = ("verified_raid_kill_v1", "bg_result_v1", "arena_result_v1", "wg_result_v1",
                  "pvp_result_v1", "channel_raid_topic_v1", "realm_first_achievement_v1",
                  "duel_result_v1", "verified_dungeon_kill_v1")
_PUBLIC_POLICIES = (*_FACT_POLICIES, "stored_forum_topic_v1")
_ASSURANCES = ("verified_event", "observed_event", "observed_topic", "stored_topic")
_QUESTION_REASONS = ("none", "expired", "context_unavailable", "capacity", "proof_unavailable",
    "payload_mismatch", "native_refused", "transport_unknown", "recovered_prepared", "recovered_dispatched",
    "evidence_conflict", "clock_conflict", "pretransport_refused", "model_unavailable", "lead_unavailable",
    "speech_refused", "record_failed", "text_mismatch", "chat_output_unproved")
_SQLITE_MAX = (1 << 63) - 1
_U32 = (1 << 32) - 1
_MAX_MS = 253402300799999
_MAX_SECONDS = 253402300799.999


def _integer(value, name, minimum=1, maximum=_SQLITE_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")


def _seconds(value, name, *, nonnegative=True):
    if (type(value) not in (int, float) or not -62135596800 <= value <= _MAX_SECONDS
            or (nonnegative and value < 0) or not math.isfinite(value)):
        raise ValueError(f"invalid {name}")


def _text(value, name, maximum, *, minimum=1):
    try:
        valid = type(value) is str and minimum <= len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {name}")


def _hash(value, name):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"invalid {name}")


def _identifier(value, name):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError(f"invalid {name}")


def _enum(value, name, values):
    if type(value) is not str or value not in values:
        raise ValueError(f"invalid {name}")


def _tuple(record, name, maximum):
    try:
        value = tuple(getattr(record, name))
    except TypeError as error:
        raise ValueError(f"invalid {name}") from error
    if len(value) > maximum:
        raise ValueError(f"invalid {name}")
    object.__setattr__(record, name, value)
    return value


def _ids(record, name, maximum=128, upper=_U32):
    value = _tuple(record, name, maximum)
    for item in value:
        _integer(item, name, maximum=upper)
    if value != tuple(sorted(set(value))):
        raise ValueError(f"invalid {name}")
    return value


def _refs(record, name, maximum):
    value = _tuple(record, name, maximum)
    for item in value:
        _text(item, name, 128)
    if value != tuple(sorted(set(value))):
        raise ValueError(f"invalid {name}")
    return value


def _json_object(value, name, maximum):
    _text(value, name, maximum)

    def number(raw):
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise ValueError(f"invalid {name}")
        return parsed

    def constant(raw):
        raise ValueError(f"invalid {name}")

    try:
        parsed = json.loads(value, parse_float=number, parse_constant=constant)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"invalid {name}") from error
    if type(parsed) is not dict:
        raise ValueError(f"invalid {name}")
    # Semantic keys and canonical projections belong to the real proof resolver.


def _public_text(value, name):
    _text(value, name, 800)
    if len(value) > 200:
        raise ValueError(f"invalid {name}")


RejectReason = Literal[
    "unsupported_policy", "invalid_input", "missing_row", "invalid_row",
    "graph_limit", "cycle", "private_evidence", "real_name", "romance",
    "unsupported_text", "topic_mismatch", "proof_changed",
]
_REJECT_REASONS = (
    "unsupported_policy", "invalid_input", "missing_row", "invalid_row",
    "graph_limit", "cycle", "private_evidence", "real_name", "romance",
    "unsupported_text", "topic_mismatch", "proof_changed",
)


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: RejectReason

    def __post_init__(self):
        _enum(self.reason, "reason", _REJECT_REASONS)

@dataclass(frozen=True, slots=True)
class ActorQualification:
    guid: int
    guild: int
    role: str
    status: str
    row_sha256: str

    def __post_init__(self):
        _integer(self.guid, "guid", maximum=_U32); _integer(self.guild, "guild", maximum=_U32)
        _enum(self.role, "role", ("officer", "ganker", "applicant"))
        _enum(self.status, "status", ("active", "retired")); _hash(self.row_sha256, "row_sha256")


@dataclass(frozen=True, slots=True)
class QualificationSnapshot:
    guild: int
    human: int
    captured_at: float
    human_confirmed: bool
    cast: tuple[int, ...]
    actors: tuple[ActorQualification, ...]
    rival_guilds: tuple[int, ...]
    speaker_kind: Literal["none", "human", "actor"]
    speaker_guid: int
    speaker_guild: int
    transport_epoch: int
    speaker_name: str
    speaker_name_source: Literal["none", "human_meta", "cast_member", "realm_actor"]
    speaker_name_sha256: str | None

    def __post_init__(self):
        _integer(self.guild, "guild", maximum=_U32); _integer(self.human, "human", maximum=_U32)
        _seconds(self.captured_at, "captured_at")
        if type(self.human_confirmed) is not bool:
            raise ValueError("invalid human_confirmed")
        _ids(self, "cast"); _ids(self, "rival_guilds")
        actors = _tuple(self, "actors", 128)
        if any(type(actor) is not ActorQualification for actor in actors):
            raise ValueError("invalid actors")
        if tuple(actor.guid for actor in actors) != tuple(sorted({actor.guid for actor in actors})):
            raise ValueError("invalid actors")
        _enum(self.speaker_kind, "speaker_kind", ("none", "human", "actor"))
        _integer(self.speaker_guid, "speaker_guid", 0, _U32)
        _integer(self.speaker_guild, "speaker_guild", 0, _U32)
        if ((self.speaker_kind == "none") != (self.speaker_guid == 0 and self.speaker_guild == 0)
                or (self.speaker_kind != "none" and (not self.speaker_guid or not self.speaker_guild))):
            raise ValueError("invalid speaker identity")
        _integer(self.transport_epoch, "transport_epoch", maximum=(1 << 64) - 1)
        _text(self.speaker_name, "speaker_name", 48, minimum=0)
        _enum(self.speaker_name_source, "speaker_name_source", ("none", "human_meta", "cast_member", "realm_actor"))
        if self.speaker_kind == "none":
            if self.speaker_name != "" or self.speaker_name_source != "none" or self.speaker_name_sha256 is not None:
                raise ValueError("invalid speaker name")
        else:
            _text(self.speaker_name, "speaker_name", 48)
            _hash(self.speaker_name_sha256, "speaker_name_sha256")
            allowed = ("human_meta", "cast_member") if self.speaker_kind == "human" else ("realm_actor",)
            if self.speaker_name_source not in allowed:
                raise ValueError("invalid speaker name source")


@dataclass(frozen=True, slots=True)
class RealmObservation:
    event: Event
    receipt: RowReceipt
    context: ActionContext
    qualification: QualificationSnapshot

    def __post_init__(self):
        if type(self.qualification) is not QualificationSnapshot:
            raise ValueError("invalid qualification")


@dataclass(frozen=True, slots=True)
class StoredSource:
    id: int
    source_key: str
    protocol_version: int
    event_type: str
    row_kind: Literal["game_event", "chat_line"]
    row_id: int
    row_sha256: str
    server_ms: int
    received_at: float
    guild: int
    human: int
    top_keys: tuple[str, ...]

    def __post_init__(self):
        _integer(self.id, "id"); _hash(self.source_key, "source_key")
        _integer(self.protocol_version, "protocol_version", 1, 1)
        _text(self.event_type, "event_type", 128)
        _enum(self.row_kind, "row_kind", ("game_event", "chat_line")); _integer(self.row_id, "row_id")
        _hash(self.row_sha256, "row_sha256"); _integer(self.server_ms, "server_ms", 1, _MAX_MS)
        _seconds(self.received_at, "received_at")
        _integer(self.guild, "guild", maximum=_U32); _integer(self.human, "human", maximum=_U32)
        keys = _tuple(self, "top_keys", 64)
        for key in keys:
            _text(key, "top_keys", 8192, minimum=0)
        if keys != tuple(sorted(set(keys))) or len(json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 8192:
            raise ValueError("invalid top_keys")


@dataclass(frozen=True, slots=True)
class ProofNode:
    kind: str
    key: str
    projection_version: int
    sha256: str

    def __post_init__(self):
        _enum(self.kind, "kind", ("game_event", "chat_line", "memory", "rumor", "realm_source",
                                  "realm_fact", "privacy_set", "qualification"))
        _text(self.key, "key", 128); _integer(self.projection_version, "projection_version", 1, 1)
        _hash(self.sha256, "sha256")


@dataclass(frozen=True, slots=True)
class PublicProof:
    policy: PublicPolicy
    assurance: Assurance
    roots: tuple[str, ...]
    sources: tuple[int, ...]
    nodes: tuple[ProofNode, ...]
    qualification_json: str
    output_fields_json: str
    sha256: str

    def __post_init__(self):
        _enum(self.policy, "policy", _PUBLIC_POLICIES); _enum(self.assurance, "assurance", _ASSURANCES)
        _refs(self, "roots", 200); _ids(self, "sources", 200, _SQLITE_MAX)
        nodes = _tuple(self, "nodes", 200)
        if any(type(node) is not ProofNode for node in nodes):
            raise ValueError("invalid nodes")
        keys = tuple((node.kind, node.key, node.projection_version) for node in nodes)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("invalid nodes")
        _json_object(self.qualification_json, "qualification_json", 8192)
        _json_object(self.output_fields_json, "output_fields_json", 32768); _hash(self.sha256, "sha256")


@dataclass(frozen=True, slots=True)
class FactDraft:
    producer_key: str
    policy: FactPolicy
    source_at: float
    template_id: str
    output_fields: OutputFields
    public: bool = True

    def __post_init__(self):
        _enum(self.policy, "policy", _FACT_POLICIES)
        if type(self.producer_key) is not str or re.fullmatch(r"event:[0-9a-f]{64}:" + re.escape(self.policy), self.producer_key) is None:
            raise ValueError("invalid producer_key")
        _seconds(self.source_at, "source_at"); _text(self.template_id, "template_id", 48)
        fields = _tuple(self, "output_fields", 32)
        copied = []
        for entry in fields:
            if type(entry) not in (tuple, list) or len(entry) != 2:
                raise ValueError("invalid output_fields")
            key, value = entry
            if type(key) is not str or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,47}", key) is None:
                raise ValueError("invalid output_fields")
            if type(value) not in (bool, int, str):
                raise ValueError("invalid output_fields")
            if type(value) is str:
                _text(value, "output_fields", 32768, minimum=0)
            copied.append((key, value))
        keys = tuple(key for key, _ in copied)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("invalid output_fields")
        object.__setattr__(self, "output_fields", tuple(copied))
        if type(self.public) is not bool:
            raise ValueError("invalid public")


@dataclass(frozen=True, slots=True)
class FactRow:
    id: int
    kind: str
    text: str
    source: str
    ref: str
    public: bool
    ts: float
    revision: int

    def __post_init__(self):
        _integer(self.id, "id"); _text(self.kind, "kind", 128); _public_text(self.text, "text")
        _enum(self.source, "source", ("event", "channel", "rumor", "rule", "benchmark", "night"))
        _text(self.ref, "ref", 128, minimum=0); _seconds(self.ts, "ts"); _integer(self.revision, "revision")
        if type(self.public) is not bool:
            raise ValueError("invalid public")


@dataclass(frozen=True, slots=True)
class ForumCandidate:
    rumor_id: int
    source_memory: int
    root: str
    about: int
    safe_text: str
    proof: PublicProof

    def __post_init__(self):
        _integer(self.rumor_id, "rumor_id"); _integer(self.source_memory, "source_memory")
        _text(self.root, "root", 128)
        if re.fullmatch(r"event:[1-9][0-9]*", self.root) is None:
            raise ValueError("invalid root")
        _integer(int(self.root[6:]), "root"); _integer(self.about, "about", maximum=_U32)
        _public_text(self.safe_text, "safe_text")
        if type(self.proof) is not PublicProof:
            raise ValueError("invalid proof")


@dataclass(frozen=True, slots=True)
class BackIn:
    fact_id: int
    fact_revision: int
    fact_sha256: str
    owner: int
    reason: Literal["channel", "forum"]
    source_at: float
    text: str
    actors: tuple[int, ...]
    proof: PublicProof

    def __post_init__(self):
        _integer(self.fact_id, "fact_id"); _integer(self.fact_revision, "fact_revision")
        _hash(self.fact_sha256, "fact_sha256"); _integer(self.owner, "owner", maximum=_U32)
        _enum(self.reason, "reason", ("channel", "forum")); _seconds(self.source_at, "source_at")
        _public_text(self.text, "text"); _ids(self, "actors")
        if type(self.proof) is not PublicProof:
            raise ValueError("invalid proof")


@dataclass(frozen=True, slots=True)
class PreparedBackIn:
    plan: BackIn
    embedding: bytes | None
    embedded_text_sha256: str

    def __post_init__(self):
        if type(self.plan) is not BackIn:
            raise ValueError("invalid plan")
        if self.embedding is not None:
            if type(self.embedding) is not bytes or len(self.embedding) != 2048:
                raise ValueError("invalid embedding")
            if any(not math.isfinite(value) for (value,) in struct.iter_unpack("<f", self.embedding)):
                raise ValueError("invalid embedding")
        _hash(self.embedded_text_sha256, "embedded_text_sha256")


@dataclass(frozen=True, slots=True)
class RealmReadStamp:
    memory_id: int
    memory_sha256: str
    fact_ids: tuple[int, ...]
    proof_sha256: str

    def __post_init__(self):
        _integer(self.memory_id, "memory_id"); _hash(self.memory_sha256, "memory_sha256")
        _ids(self, "fact_ids", 200, _SQLITE_MAX); _hash(self.proof_sha256, "proof_sha256")


@dataclass(frozen=True, slots=True)
class RealmFactReadStamp:
    fact_id: int
    revision: int
    fact_sha256: str
    proof_sha256: str

    def __post_init__(self):
        _integer(self.fact_id, "fact_id"); _integer(self.revision, "revision")
        _hash(self.fact_sha256, "fact_sha256"); _hash(self.proof_sha256, "proof_sha256")


@dataclass(frozen=True, slots=True)
class RealmInputs:
    refs: tuple[str, ...]
    stamps: tuple[RealmReadStamp, ...]
    mode: Literal["exclude", "memory"]
    fact_stamps: tuple[RealmFactReadStamp, ...] = ()

    def __post_init__(self):
        refs = _refs(self, "refs", 256)
        if sum(len(ref.encode("utf-8")) for ref in refs) > 32768:
            raise ValueError("invalid refs")
        stamps = _tuple(self, "stamps", 200)
        if any(type(stamp) is not RealmReadStamp for stamp in stamps):
            raise ValueError("invalid stamps")
        ids = tuple(stamp.memory_id for stamp in stamps)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("invalid stamps")
        fact_stamps = _tuple(self, "fact_stamps", 200)
        if any(type(stamp) is not RealmFactReadStamp for stamp in fact_stamps):
            raise ValueError("invalid fact_stamps")
        fact_ids = tuple(stamp.fact_id for stamp in fact_stamps)
        if fact_ids != tuple(sorted(set(fact_ids))):
            raise ValueError("invalid fact_stamps")
        _enum(self.mode, "mode", ("exclude", "memory"))


@dataclass(frozen=True, slots=True)
class QuestionIntent:
    episode_id: str
    ask_id: str
    target_guid: int
    lead_guid: int
    human_guid: int
    guild_id: int
    text: str
    text_sha256: str
    ask_expires_ms: int
    context: ActionContext

    def __post_init__(self):
        if type(self.episode_id) is not str or re.fullmatch(r"kick:[0-9a-f]{64}", self.episode_id) is None:
            raise ValueError("invalid episode_id")
        _identifier(self.ask_id, "ask_id")
        for name in ("target_guid", "lead_guid", "human_guid", "guild_id"):
            _integer(getattr(self, name), name, maximum=_U32)
        if self.lead_guid in (self.target_guid, self.human_guid):
            raise ValueError("invalid lead_guid")
        _public_text(self.text, "text"); _hash(self.text_sha256, "text_sha256")
        _integer(self.ask_expires_ms, "ask_expires_ms", 1, _MAX_MS)


@dataclass(frozen=True, slots=True)
class QuestionReceipt:
    ask_id: str
    command_id: str
    speaker: int
    human: int
    line_id: int
    text_sha256: str
    ack_ms: int
    ack_received_ms: int

    def __post_init__(self):
        _identifier(self.ask_id, "ask_id"); _identifier(self.command_id, "command_id")
        _integer(self.speaker, "speaker", maximum=_U32); _integer(self.human, "human", maximum=_U32)
        _integer(self.line_id, "line_id"); _hash(self.text_sha256, "text_sha256")
        _integer(self.ack_ms, "ack_ms", 1, _MAX_MS)
        _integer(self.ack_received_ms, "ack_received_ms", self.ack_ms, _MAX_MS)


@dataclass(frozen=True, slots=True)
class QuestionSendResult:
    state: Literal["delivered", "refused", "unknown"]
    receipt: QuestionReceipt | None
    safe_reason: QuestionReason

    def __post_init__(self):
        _enum(self.state, "state", ("delivered", "refused", "unknown"))
        if self.receipt is not None and type(self.receipt) is not QuestionReceipt:
            raise ValueError("invalid receipt")
        if (self.state == "delivered") != (self.receipt is not None):
            raise ValueError("receipt must match delivered state")
        _enum(self.safe_reason, "safe_reason", _QUESTION_REASONS)


@dataclass(frozen=True, slots=True)
class ProductPeriod:
    cadence: str
    key: str
    starts_at: float
    ends_at: float

    def __post_init__(self):
        _enum(self.cadence, "cadence", ("product-tue04-america-new_york-v1",))
        try:
            valid_key = type(self.key) is str and date.fromisoformat(self.key).isoformat() == self.key
        except ValueError:
            valid_key = False
        if not valid_key:
            raise ValueError("invalid key")
        _seconds(self.starts_at, "starts_at", nonnegative=False)
        _seconds(self.ends_at, "ends_at", nonnegative=False)
        if self.ends_at <= self.starts_at:
            raise ValueError("invalid period boundaries")
