"""Bounded native Realm owner receipts. Registration is explicit and default off.

These snapshots are observations, never Python authority to release a body.
Cleanup must return through the original native token and complete manifest.
"""

from dataclasses import dataclass
import re

from leeroy_mind.protocol import register_command, register_event


def _token(value: str) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{16}", value) is not None


def _id(value: str, *, empty: bool = False) -> bool:
    return type(value) is str and ((empty and value == "")
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None)


def _uint(value: int, maximum: int = 0xFFFFFFFF) -> bool:
    return type(value) is int and 0 < value <= maximum


def _manifest(members: tuple[int, ...]) -> bool:
    return (type(members) is tuple and 1 <= len(members) <= 150
        and all(_uint(member) for member in members) and len(set(members)) == len(members))


@dataclass(frozen=True, slots=True)
class LeavePvp:
    id: str
    guids: tuple[int, ...]
    pvp_owner: str = ""

    def __post_init__(self):
        if (not _id(self.id) or not _manifest(self.guids)
                or (self.pvp_owner != "" and not _token(self.pvp_owner))):
            raise ValueError("invalid Realm cleanup request")


@dataclass(frozen=True, slots=True)
class GetRealmPvp:
    id: str
    pvp_owner: str = ""
    origin_id: str = ""

    def __post_init__(self):
        if (not _id(self.id) or not _id(self.origin_id, empty=True)
                or (self.pvp_owner != "" and not _token(self.pvp_owner))
                or bool(self.pvp_owner) == bool(self.origin_id)):
            raise ValueError("exactly one native owner selector is required")


@dataclass(frozen=True, slots=True)
class RealmPvp:
    ts: int
    id: str
    pvp_owner: str
    origin_id: str
    kind: str
    origin_epoch: int
    revision: int
    guild: int
    human: int
    members: tuple[int, ...]
    stage: str
    application_fenced: bool
    reason: str
    cleanup_id: str

    def __post_init__(self):
        if (type(self.ts) is not int or not 0 <= self.ts <= 0x7FFFFFFFFFFFFFFF
                or not _id(self.id) or not _token(self.pvp_owner) or not _id(self.origin_id)
                or self.kind not in {"wintergrasp", "rated_arena", "fishing", "duel"}
                or not _uint(self.origin_epoch, 0xFFFFFFFFFFFFFFFF)
                or not _uint(self.revision, 0xFFFFFFFFFFFFFFFF)
                or not _uint(self.guild) or not _uint(self.human) or not _manifest(self.members)
                or tuple(sorted(self.members)) != self.members
                or self.stage not in {"acquiring", "queued", "invited", "entering", "active", "ending",
                                      "cleaning", "clean", "unknown"}
                or type(self.application_fenced) is not bool
                or type(self.reason) is not str or len(self.reason.encode("utf-8")) > 128 or "\0" in self.reason
                or not _id(self.cleanup_id, empty=True)
                or (self.stage in {"cleaning", "clean"} and not self.cleanup_id)
                or (self.stage in {"cleaning", "clean", "unknown"} and not self.application_fenced)):
            raise ValueError("invalid native Realm owner receipt")


@dataclass(frozen=True, slots=True)
class RealmJob:
    ts: int
    id: str
    kind: str
    state: str
    reason: str
    guid: int
    value: int
    pvp_owner: str = ""

    def __post_init__(self):
        if (type(self.ts) is not int or not 0 <= self.ts <= 0x7FFFFFFFFFFFFFFF
                or not _id(self.id) or type(self.kind) is not str or not 1 <= len(self.kind) <= 64
                or self.state not in {"done", "failed"} or type(self.reason) is not str
                or type(self.guid) is not int or not 0 <= self.guid <= 0xFFFFFFFF
                or type(self.value) is not int or not 0 <= self.value <= 0xFFFFFFFFFFFFFFFF
                or (self.pvp_owner != "" and not _token(self.pvp_owner))):
            raise ValueError("invalid Realm job receipt")


def register() -> None:
    register_command("leave_pvp", LeavePvp)
    register_command("get_realm_pvp", GetRealmPvp)
    register_event("realm_pvp", RealmPvp)
    register_event("realm_job", RealmJob)
