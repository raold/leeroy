"""Durable Realm ownership reducers. Every write owns ``with conn``.

Call outside Store.transaction(): canonical connections use autocommit=False.
There is deliberately no generic release API. Typed correlated evidence alone
can end a dispatched reservation (RealmModule.cpp:595-665,906-1011,827-856),
apart from abandon(): the operator's release of one unknown body move, with
the mind stopped (`realm release`), which keeps a meta receipt.
"""
from dataclasses import asdict, dataclass, replace
import json
import logging
import math
import re
import sqlite3

from leeroy_mind.bodies import BodyEvidenceUnavailable, BodyHold, RANKS
from leeroy_mind.guild.protocol import Job, JobSkip
from leeroy_mind.protocol import Ack
from . import protocol as p
from .action_policy import Classified, classify, decode, document, identity, integer, members, text, token, U64

log = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "not_sent"})
STATES = frozenset({"prepared", "dispatched", "unknown", "conflict", *TERMINAL})
# A queued command's body fences (RealmModule::AcceptDomain): its executor's default admission,
# then MutationReason (RealmPorts.h NativeMutationReason) for each body, all before native
# reserves anyone. One fenced body refuses the whole command at its ack: nobody was moved.
BODY_FENCES = frozenset({"not_actor", "not_bot", "busy:realm_owner", "busy:raid_owner",
                         "busy:body_unavailable", "busy:training_owner", "busy:training_cleanup",
                         "busy:training_state"})
REFUSALS = frozenset({"realm_off", "realm_not_started", "unsupported", "busy:queue_full",
                      "busy:body_identity", "pin_rejected", "bad field: queued_operation", *BODY_FENCES})
# guild_grow's admission refusals (RealmGuild.cpp GrowAdmission, LeeroyRealmGuild.h): native
# refused the whole grow at its ack, before reserving or moving anyone. A guid's reason names it.
GROW_REFUSALS = frozenset({"refused_guild", "guild not found"})
GROW_GUID_REFUSALS = frozenset({"cast", "actor", "not_bot", "not_guildless", "level", "faction"})
LOCAL_CANCELED = frozenset({"stale_context", "stale_identity", "owner_dropped", "queue_closed",
                           "restart_not_sent", "held", "realm_off", "barrier_missing", "backoff",
                           "query_origin_unavailable", "cleanup_proof_unavailable"})
# A queued step's refusals that end a body move after its positive ack (RealmModule::Tick): the
# pre-step fence MutationReason (the body fences, ValidateOwnedBody's), the default admission,
# and Eligible's own (AdmissionReason's busy:<BusyReason>, busy:teleport, busy:duel, not_cast),
# which each such executor returns as Refuse before it changes anything. Tick's finish then
# releases every holder of the command. That holds only for a kind whose executor never goes
# pending, declares no scope, starts no continuation and joins no guild, and never reports one
# of these reasons as Unknown: the same reason from a pending walk or leave is a retained unknown.
STEP_REFUSALS = frozenset({*BODY_FENCES, "busy:operation_identity", "busy:body_identity_changed",
                           "busy:body_scope_changed", "not_cast", "busy:teleport", "busy:duel",
                           *(f"busy:{r}" for r in ("offline", "dead", "combat", "flight", "raid_group",
                                                   "human_group", "instance", "battleground", "arena", "lfg"))})
STEP_REFUSAL_KINDS = frozenset({"rearm", "rpg_status", "equip", "set_profession"})
# A refusal only one such executor makes, and only as a Refuse before it changes the body. Rearm
# (RealmBodies.cpp) has no weapon record for it, a record kept only in memory: a worldserver start
# empties them until the 60 s poll finds the body online and free. After a swap it says
# weapon_record_unproved instead, which stays unknown.
KIND_STEP_REFUSALS = {"rearm": frozenset({"weapon_record_unavailable"})}
# A leave_pvp with members in a Wintergrasp state native cannot prove restored is refused whole at
# its ack (LeavePvpAckError), before any body is reserved or moved, and the error names them:
# "wg_private_state_unavailable:301,302". It is accepted only when every named guid is in the root's
# manifest. The same reason from its job, after earlier members may have changed, stays unknown.
WG_REFUSAL = "wg_private_state_unavailable"


def wg_refusal_guids(error, manifest=None):
    """The guids a Wintergrasp ack refusal names, or None when it is not one (or names a guid
    outside the manifest, when one is given)."""
    reason, _, rest = error.partition(":")
    if reason != WG_REFUSAL or re.fullmatch(r"[0-9]{1,10}(,[0-9]{1,10}){0,127}", rest) is None: return None
    named = tuple(int(g) for g in rest.split(","))
    if len(set(named)) != len(named) or manifest is not None and not set(named) <= set(manifest): return None
    return named


STEP_REFUSED = "step_refused"
ABANDONED = "abandoned"
ABANDON_RECEIPT = "realm:action:abandoned:"


def _step_refused(r, job) -> bool:
    """Native refused one body of this command before it changed it, and released them all."""
    return (r.role == "root" and r.policy == "ordinary" and job.get("changed") is None
            and r.kind in STEP_REFUSAL_KINDS
            and job["state"] == "failed"
            and (job["reason"] in STEP_REFUSALS or job["reason"] in KIND_STEP_REFUSALS.get(r.kind, ()))
            and job["guid"] in r.reserved_manifest and job["value"] == 0 and job["pvp_owner"] == "")


def _durable(r) -> bool:
    """A settled root the prune keeps for good: a grow's row is rivals._grow's only memory of that
    attempt (a missing attempt id is sent again), and a release is the operator's own record."""
    return r.kind == "guild_grow" or r.result_reason == ABANDONED


def release_reason(reason) -> bool:
    """The operator's reason for a release: 1 to 200 printable characters."""
    return (type(reason) is str and bool(reason.strip()) and len(reason) <= 200
            and all(ch.isprintable() for ch in reason))


def abandon_refusal(r, children) -> str:
    """Why the operator may not release this root, or "" when he may."""
    if r.role != "root" or r.parent_id is not None:
        return "only a root command can be released"
    if r.state != "unknown":
        return f"only an unknown root can be released; this one is {r.state}"
    if r.transport_state != "settled":
        return "its transport is not settled; start and stop the mind once so its recovery settles it"
    if r.policy not in {"ordinary", "defaults"}:
        return "only a body move (ordinary or defaults policy) can be released"
    if children:
        return "it has child commands"
    if json.loads(r.evidence).get("conflict"):
        return "its native evidence conflicts"
    if not r.reserved_manifest:
        return "it holds nobody"
    return ""


def _grow_refusal(error, manifest) -> bool:
    if error in GROW_REFUSALS:
        return True
    reason, _, guid = error.partition(":")
    return reason in GROW_GUID_REFUSALS and guid.isdigit() and int(guid) in manifest


class ActionUnavailable(BodyEvidenceUnavailable):
    """The durable action evidence cannot safely describe ownership."""


class ActionCapacity(ValueError):
    pass


def _now(value):
    if type(value) not in (int, float): raise ValueError("invalid action time")
    try: result = float(value)
    except (ValueError, OverflowError): raise ValueError("invalid action time") from None
    if not math.isfinite(result): raise ValueError("invalid action time")
    return result


def _rank(owner, rank):
    text(owner, 1, 256); integer(rank, 0, 4)
    if owner in RANKS and rank != RANKS[owner]: raise ValueError("inconsistent action rank")


def _evidence(ev):
    return {k: v for k, v in asdict(ev).items() if k not in {"id", "ts"}
            and not (type(ev) is p.RealmJob and k == "changed" and v is None)}


def _realm_job(value, command_id):
    if type(value) is not dict: raise ValueError("invalid action evidence")
    ev = p.RealmJob(ts=0, id=command_id, **value)
    text(ev.reason, 0, 256)
    if ev.state == "done" and ev.reason: raise ValueError("invalid success reason")
    return ev


def _status(value, command_id):
    if type(value) is not dict: raise ValueError("invalid action evidence")
    return p.RealmPvp(ts=0, id=command_id, **{**value, "members": tuple(value["members"])})


def _bot(value, command_id):
    if type(value) is not dict: raise ValueError("invalid action evidence")
    ev = Job(ts=0, id=command_id, **{**value, "guids": tuple(value["guids"]),
             "skipped": tuple(JobSkip(**v) for v in value["skipped"])})
    if ev.kind != "bot_do" or ev.state not in {"done", "failed"} or ev.training_run != "":
        raise ValueError("invalid defaults evidence")
    text(ev.reason, 0, 256); text(ev.stage, 0, 128)
    members(ev.guids, 0, 40); members(tuple(s.guid for s in ev.skipped), 0, 40)
    for skip in ev.skipped: text(skip.reason, 0, 256)
    for key in ("kills", "wipes", "instance_id", "guid", "level", "avg_ilvl"):
        integer(getattr(ev, key), 0)
    return ev


def _stored_announce_v1(payload):
    """Read the exact historical no-body schema, never a dispatchable command.

    This reader is used only after strict current-schema decoding fails while
    validating an existing journal record. No missing proof identity or native
    mode is inferred. Canonical serialization also rejects duplicate JSON keys.
    """
    raw = json.loads(payload)
    keys = {"type", "id", "map", "instance_id", "boss_id", "boss_name", "attempts", "fight_s", "first"}
    if type(raw) is not dict or set(raw) != keys or raw["type"] != "announce":
        raise ValueError("invalid historical announcement")
    command_id = identity(raw["id"])
    integer(raw["map"], 0); integer(raw["instance_id"]); integer(raw["boss_id"], 0)
    text(raw["boss_name"], 1, 48); integer(raw["attempts"], 0, 99); integer(raw["fight_s"], 0, 3600)
    if text(raw["first"], 1, 64) not in {"week", "ever"} or document(raw) != payload:
        raise ValueError("invalid historical announcement")
    return command_id, Classified("announce", "ack", payload, ())


@dataclass(frozen=True, slots=True)
class ActionSpec:
    command_id: str
    parent_id: str | None
    role: str
    policy: str
    owner: str
    rank: int
    kind: str
    payload: str
    reserved_manifest: tuple[int, ...]
    guild: int
    human: int


@dataclass(frozen=True, slots=True)
class ActionRecord(ActionSpec):
    seq: int
    native_manifest: tuple[int, ...] | None
    state: str
    transport_state: str
    process_id: str
    connection_epoch: int
    send_error: str
    evidence: str
    result_ok: bool | None
    result_reason: str
    created: float
    updated: float
    dispatched: float | None


@dataclass(frozen=True, slots=True)
class Transition:
    matched: bool
    changed: bool = False
    command_id: str = ""
    result_ready: bool = False
    result_ok: bool | None = None
    reason: str = ""
    value: int = 0


class ActionStore:
    def __init__(self, conn):
        self.conn = conn

    def _validate(self, r, *, stored=False):
        identity(r.command_id)
        if r.parent_id is not None: identity(r.parent_id)
        _rank(r.owner, r.rank); integer(r.guild); integer(r.human)
        legacy = False
        try:
            cmd, c = decode(r.payload)
            command_id = cmd.id
        except (ValueError, TypeError, KeyError):
            if not stored:
                raise
            command_id, c = _stored_announce_v1(r.payload)
            cmd = None
            legacy = True
            if (r.kind != "announce" or r.policy != "ack" or r.role != "root"
                    or r.parent_id is not None or r.reserved_manifest != () or r.native_manifest is not None):
                raise ValueError("invalid historical announcement binding")
        if (command_id != r.command_id or c.kind != r.kind or c.policy != r.policy
                or c.reserved_manifest != r.reserved_manifest
                or r.reserved_manifest != tuple(sorted(members(r.reserved_manifest, 0)))):
            raise ValueError("invalid action binding")
        if r.role not in {"root", "query", "cleanup"} or (r.role == "root") != (r.parent_id is None):
            raise ValueError("invalid action role")
        if r.role == "query" and r.policy != "query" or r.role == "cleanup" and r.policy != "cleanup": raise ValueError("invalid child policy")
        if r.role == "root" and r.policy in {"query", "cleanup"}: raise ValueError("invalid root policy")
        if r.native_manifest is not None:
            native = members(r.native_manifest)
            if native != tuple(sorted(native)) or not set(native) <= set(r.reserved_manifest): raise ValueError("invalid native membership")
        if r.state not in STATES or r.transport_state not in {"not_entered", "inflight", "settled"}: raise ValueError("invalid action state")
        if r.transport_state != "not_entered":
            text(r.process_id, 1, 256); integer(r.connection_epoch, 1, U64)
        elif r.process_id or r.connection_epoch: raise ValueError("invalid action transport")
        if r.state == "prepared" and r.transport_state != "not_entered": raise ValueError("invalid prepared transport")
        if r.state in {"dispatched", "unknown", "conflict", "completed"} and r.transport_state == "not_entered":
            raise ValueError("missing dispatch proof")
        if r.state == "completed" and r.transport_state != "settled": raise ValueError("unsettled completion")
        if r.result_ok is not None and type(r.result_ok) is not bool: raise ValueError("invalid action result")
        text(r.send_error, 0, 64); text(r.result_reason, 0, 256)
        _now(r.created); _now(r.updated)
        if r.dispatched is not None: _now(r.dispatched)
        ev = json.loads(r.evidence)
        if type(ev) is not dict or document(ev, limit=32768) != r.evidence: raise ValueError("invalid action evidence")
        allowed = {"ack", "job", "bot", "enrollment_job", "origin_failure", "status", "binding", "query_id",
                   "last_query", "cleanup_id", "cleanup_ids", "cleanup_admitted", "canceled", "dispatch_revision", "conflict", "failed_at",
                   "abandoned"}
        if not set(ev) <= allowed: raise ValueError("invalid action evidence keys")
        if legacy and not set(ev) <= {"ack", "conflict", "failed_at"}:
            raise ValueError("invalid historical announcement evidence")
        if "ack" in ev:
            ack = ev["ack"]
            if type(ack) is not dict or set(ack) != {"ok", "error"} or type(ack["ok"]) is not bool: raise ValueError("invalid action ack")
            text(ack["error"], 0, 256)
            if ack["ok"] and ack["error"]: raise ValueError("invalid action ack")
        for key in ("job", "enrollment_job", "origin_failure"):
            if key in ev and _realm_job(ev[key], r.command_id).kind != r.kind: raise ValueError("invalid action job kind")
        if "bot" in ev: _bot(ev["bot"], r.command_id)
        if "status" in ev: _status(ev["status"], r.command_id)
        for key in ("query_id", "cleanup_id"):
            if key in ev and ev[key]: identity(ev[key])
        for key in ("cleanup_ids", "cleanup_admitted", "canceled"):
            if key in ev:
                if type(ev[key]) is not list or len(ev[key]) > 17 or len(set(ev[key])) != len(ev[key]): raise ValueError("invalid cleanup history")
                for command_id in ev[key]: identity(command_id)
        if "last_query" in ev: _now(ev["last_query"])
        if "failed_at" in ev: _now(ev["failed_at"])
        if "abandoned" in ev:
            _now(ev["abandoned"])
            if r.role != "root" or r.policy not in {"ordinary", "defaults"}: raise ValueError("invalid abandonment")
        if "dispatch_revision" in ev: integer(ev["dispatch_revision"], 0, U64)
        if "conflict" in ev and ev["conflict"] is not True: raise ValueError("invalid conflict")
        if "binding" in ev:
            b = ev["binding"]
            if (type(b) is not dict or set(b) != {"origin_id", "pvp_owner", "kind", "guild", "human", "origin_epoch", "members"}
                    or b["origin_id"] != r.command_id or b["kind"] != "wintergrasp"
                    or b["guild"] != r.guild or b["human"] != r.human
                    or tuple(b["members"]) != r.native_manifest): raise ValueError("invalid native binding")
            token(b["pvp_owner"]); integer(b["origin_epoch"], 1, U64)
        elif r.native_manifest is not None: raise ValueError("missing native binding")
        if r.policy == "wg" and "status" in ev:
            status_binding = {k: ev["status"][k] for k in ("origin_id", "pvp_owner", "kind", "guild", "human", "origin_epoch", "members")}
            if ev.get("binding") != status_binding: raise ValueError("invalid status binding")
        if "bot" in ev and r.policy != "defaults": raise ValueError("wrong defaults policy")
        if any(k in ev for k in ("enrollment_job", "origin_failure", "binding")) and r.policy != "wg":
            raise ValueError("wrong continuation policy")
        if "status" in ev and r.policy not in {"wg", "query"}: raise ValueError("wrong status policy")
        for key in ("job", "enrollment_job", "origin_failure"):
            if key not in ev: continue
            job = ev[key]
            if r.policy == "wg":
                token(job["pvp_owner"])
                if job["guid"] != 0 or key == "enrollment_job" and (job["state"] != "done" or not 1 <= job["value"] <= len(r.reserved_manifest)):
                    raise ValueError("invalid enrollment proof")
                if key == "origin_failure" and job["state"] != "failed": raise ValueError("invalid origin failure")
            else:
                if job["pvp_owner"] != json.loads(r.payload).get("pvp_owner", ""): raise ValueError("invalid job token")
                if job["state"] == "done":
                    expected = r.reserved_manifest[0] if len(r.reserved_manifest) == 1 else 0
                    if r.policy == "result": expected = getattr(cmd, "guid", getattr(cmd, "creator_guid", 0))
                    if r.policy == "cleanup": expected = 0
                    if job["guid"] != expected or r.kind == "calendar_post" and job["value"] == 0:
                        raise ValueError("invalid job proof")
                    if r.policy == "cleanup" and job["value"] != len(r.reserved_manifest): raise ValueError("invalid cleanup count")
        if r.state == "completed" and (r.policy != "wg" or r.result_ok is False):
            check = self._reduce(replace(r, state="dispatched", result_ok=None), ev, r.updated)
            if check.state != "completed" or check.result_ok != r.result_ok: raise ValueError("missing completion proof")
        if r.state == "completed" and r.policy == "wg" and r.result_ok is not False:
            cleanup = self.get(ev.get("cleanup_id", ""))
            status = ev.get("status")
            if cleanup is None or cleanup.role != "cleanup" or cleanup.parent_id != r.command_id or r.transport_state != "settled":
                raise ValueError("missing cleanup proof")
            cdata = json.loads(cleanup.evidence)
            job_clean = cleanup.state == "completed" and cleanup.result_ok is True
            query_clean = (status and status["stage"] == "clean" and status["application_fenced"]
                           and status["cleanup_id"] == cleanup.command_id and cleanup.transport_state == "settled")
            if (not (job_clean or query_clean) or r.result_ok is not True or ev.get("conflict")
                    or ev.get("ack", {}).get("ok") is False or cdata.get("ack", {}).get("ok") is False
                    or cdata.get("conflict")): raise ValueError("invalid cleanup proof")
        if r.state == "not_sent" and r.transport_state != "not_entered":
            if r.send_error not in {"NotConnected", "RealmDisabled"} or any(k in ev for k in ("ack", "job", "bot", "binding")):
                raise ValueError("invalid no-send proof")
        return r

    def _decode_row(self, names, row):
        d = dict(zip(names, tuple(row)))
        d["reserved_manifest"] = tuple(json.loads(d["reserved_manifest"]))
        d["native_manifest"] = None if d["native_manifest"] is None else tuple(json.loads(d["native_manifest"]))
        if type(d["connection_epoch"]) is not str or re.fullmatch(r"0|[1-9][0-9]{0,19}", d["connection_epoch"]) is None: raise ValueError("invalid stored epoch")
        d["connection_epoch"] = int(d["connection_epoch"])
        if d["result_ok"] is not None:
            if type(d["result_ok"]) is not int or d["result_ok"] not in (0, 1): raise ValueError("invalid action result")
            d["result_ok"] = bool(d["result_ok"])
        return self._validate(ActionRecord(**d), stored=True)

    def _read(self, command_id):
        cursor = self.conn.execute("SELECT * FROM realm_action WHERE command_id=?", (command_id,))
        row = cursor.fetchone()
        if row is None: return None
        return self._decode_row([c[0] for c in cursor.description], row)

    def get(self, command_id):
        try: return self._read(command_id)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
            raise ActionUnavailable("realm action evidence unavailable") from None

    def _records(self, where="1", params=()):
        try:
            cursor = self.conn.execute("SELECT * FROM realm_action WHERE " + where + " ORDER BY seq", params)
            names = [c[0] for c in cursor.description]
            return tuple(self._decode_row(names, row) for row in cursor.fetchall())
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, sqlite3.Error):
            raise ActionUnavailable("realm action evidence unavailable") from None

    def records(self):
        """Validated history, also used for durable 600s backoff; bounded but for grows and releases."""
        return self._records()

    def unresolved(self, *, limit=128):
        integer(limit, 1, 128)
        rows = self._records("role='root' AND state NOT IN ('completed','not_sent')")
        if len(rows) > 128: raise ActionUnavailable("realm action evidence unavailable")
        return rows[:limit]

    def holds(self):
        rows = self.records()
        by_id = {r.command_id: r for r in rows}
        for r in rows:
            data = json.loads(r.evidence)
            if r.parent_id is not None:
                parent = by_id.get(r.parent_id)
                if (parent is None or parent.role != "root" or parent.policy != "wg"
                        or (r.owner, r.rank, r.guild, r.human) != (parent.owner, parent.rank, parent.guild, parent.human)):
                    raise ActionUnavailable("realm action evidence unavailable")
                parent_data = json.loads(parent.evidence)
                if r.role == "query":
                    payload = json.loads(r.payload)
                    if parent_data.get("query_id") != r.command_id or payload["origin_id"] != parent.command_id or payload["pvp_owner"]:
                        raise ActionUnavailable("realm action evidence unavailable")
                elif (r.command_id not in parent_data.get("cleanup_ids", []) or r.reserved_manifest != parent.native_manifest
                      or json.loads(r.payload)["pvp_owner"] != parent_data.get("binding", {}).get("pvp_owner")):
                    raise ActionUnavailable("realm action evidence unavailable")
            if r.policy == "wg":
                history = data.get("cleanup_ids", [])
                if (not set(data.get("canceled", [])) <= set(history)
                        or not set(data.get("cleanup_admitted", [])) <= set(history)):
                    raise ActionUnavailable("realm action evidence unavailable")
                for child_id, role in [(data.get("query_id"), "query"), *[(i, "cleanup") for i in history]]:
                    child = by_id.get(child_id) if child_id else None
                    if child_id and (child is None or child.parent_id != r.command_id or child.role != role):
                        raise ActionUnavailable("realm action evidence unavailable")
                if data.get("cleanup_id") and data["cleanup_id"] not in history:
                    raise ActionUnavailable("realm action evidence unavailable")
        return tuple(BodyHold(r.command_id, r.owner, r.rank, frozenset(r.reserved_manifest), True)
                     for r in rows if r.role == "root" and r.state not in TERMINAL
                     and r.transport_state != "not_entered" and r.reserved_manifest)

    def _save(self, r):
        if r.result_ok is False and r.result_reason not in LOCAL_CANCELED:
            evidence = json.loads(r.evidence)
            evidence.setdefault("failed_at", r.updated)
            r = replace(r, evidence=document(evidence, limit=32768))
        self._validate(r, stored=True)
        if r.role == "root" and r.state in {"unknown", "conflict"}:
            before = self.conn.execute("SELECT state FROM realm_action WHERE seq=?", (r.seq,)).fetchone()
            if before is not None and before[0] != r.state:
                # It now holds its manifest until typed evidence or `realm release` ends it.
                ev = json.loads(r.evidence)
                native = ev.get("job", {}).get("reason") or ev.get("ack", {}).get("error") or "none"
                log.warning("realm_action_%s id=%s kind=%s reason=%s native=%s members=%d", r.state, r.command_id,
                            r.kind, r.result_reason or r.send_error or "none", native, len(r.reserved_manifest))
        if r.role == "root" and r.kind == "leave_pvp" and r.result_reason.startswith(WG_REFUSAL + ":"):
            before = self.conn.execute("SELECT state FROM realm_action WHERE seq=?", (r.seq,)).fetchone()
            if before is not None and before[0] != r.state:
                log.warning("realm_leave_wg_refused id=%s kind=%s reason=%s named=%d members=%d", r.command_id, r.kind,
                            WG_REFUSAL, len(wg_refusal_guids(r.result_reason) or ()), len(r.reserved_manifest))
        if r.role == "root" and r.state == "completed" and r.result_reason == STEP_REFUSED:
            before = self.conn.execute("SELECT state FROM realm_action WHERE seq=?", (r.seq,)).fetchone()
            if before is not None and before[0] != r.state:
                native = json.loads(r.evidence).get("job", {}).get("reason", "")
                # A wire reason is untrusted text. Log only its code, never arbitrary words.
                code = native if re.fullmatch(r"[a-z_]+(?::[a-z_]+)?", native) else "native_refusal"
                log.info("realm_action_step_refused id=%s kind=%s reason=%s members=%d",
                         r.command_id, r.kind, code or "none", len(r.reserved_manifest))
        values = asdict(r)
        values["reserved_manifest"] = document(list(r.reserved_manifest))
        values["native_manifest"] = None if r.native_manifest is None else document(list(r.native_manifest))
        values["connection_epoch"] = str(r.connection_epoch)
        values["result_ok"] = None if r.result_ok is None else int(r.result_ok)
        self.conn.execute("UPDATE realm_action SET " + ",".join(k + "=?" for k in values if k != "seq") + " WHERE seq=?",
                          tuple(v for k, v in values.items() if k != "seq") + (r.seq,))
        return r

    def _insert(self, spec, now):
        values = asdict(spec); values["reserved_manifest"] = document(list(spec.reserved_manifest))
        values.update(state="prepared", transport_state="not_entered", created=now, updated=now)
        self.conn.execute("INSERT INTO realm_action(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
        return self.get(spec.command_id)

    def prepare(self, spec, *, now):
        now = _now(now)
        if type(spec) is not ActionSpec or spec.role != "root" or spec.parent_id is not None: raise ValueError("invalid root action")
        self._validate(ActionRecord(**asdict(spec), seq=0, native_manifest=None, state="prepared", transport_state="not_entered",
             process_id="", connection_epoch=0, send_error="", evidence="{}", result_ok=None, result_reason="", created=now, updated=now, dispatched=None))
        with self.conn:
            self._prune(now=now)
            if len(self.unresolved()) >= 128: raise ActionCapacity("realm action capacity")
            roots = tuple(r for r in self.records() if r.role == "root")
            reserved_history = sum(r.state not in TERMINAL or r.result_ok is False and "failed_at" in json.loads(r.evidence)
                and now < json.loads(r.evidence)["failed_at"] + 600 for r in roots)
            if reserved_history >= 256: raise ActionCapacity("realm backoff history capacity")
            if self.get(spec.command_id) is not None: raise ValueError("action identity already enrolled")
            return self._insert(spec, now)

    def begin_dispatch(self, command_id, *, payload, process_id, connection_epoch, now):
        now = _now(now); text(process_id, 1, 256); integer(connection_epoch, 1, U64)
        with self.conn:
            r = self.get(command_id)
            if r is None or r.state != "prepared" or r.payload != payload: raise ValueError("invalid action dispatch")
            # Historical v1 Announce can be read/recovered but never replayed.
            # Admission and transport both require the complete current schema.
            decode(r.payload)
            if r.role == "query":
                parent = self.get(r.parent_id)
                data = json.loads(parent.evidence)
                if parent.state in TERMINAL or parent.state == "conflict" or data.get("query_id") != r.command_id:
                    raise ValueError("invalid query dispatch")
                data["last_query"] = now
                self._save(replace(parent, evidence=document(data, limit=32768), updated=now))
            elif r.role == "cleanup":
                parent = self.get(r.parent_id)
                data = json.loads(parent.evidence)
                if parent.state in TERMINAL or parent.state == "conflict" or data.get("cleanup_id") != r.command_id:
                    raise ValueError("invalid cleanup dispatch")
                child_data = json.loads(r.evidence)
                child_data["dispatch_revision"] = data["status"]["revision"]
                r = replace(r, evidence=document(child_data, limit=32768))
            return self._save(replace(r, state="dispatched", transport_state="inflight", process_id=process_id,
                                      connection_epoch=connection_epoch, updated=now, dispatched=now))

    def _transition(self, old, new):
        ev = json.loads(new.evidence)
        value = next((ev[key]["value"] for key in ("job", "enrollment_job") if key in ev), 0)
        return Transition(True, old != new, new.command_id, old.result_ok is None and new.result_ok is not None,
                          new.result_ok, new.result_reason, value)

    def predispatch_failed(self, command_id, *, reason, now):
        now = _now(now); text(reason, 1, 64)
        with self.conn:
            old = self.get(command_id)
            if old is None or old.state != "prepared": return Transition(False)
            new = self._save(replace(old, state="not_sent", result_ok=False, result_reason=reason, updated=now))
            self._prune(now=now)
            return self._transition(old, new)

    def _conflict(self, r, ev, now):
        ev["conflict"] = True
        return replace(r, state="conflict", evidence=document(ev, limit=32768), result_ok=False, result_reason="conflict", updated=now)

    def _reduce(self, r, ev, now):
        reduced = self._settle(r, ev, now)
        # The operator's release stands against later native evidence, unless it contradicts itself.
        if "abandoned" in ev and reduced.state != "conflict":
            return replace(reduced, state="completed", result_ok=False, result_reason=ABANDONED)
        return reduced

    def _settle(self, r, ev, now):
        r = replace(r, evidence=document(ev, limit=32768), updated=now)
        if r.state == "not_sent": return self._conflict(r, ev, now)
        if ev.get("conflict") or r.state == "conflict": return self._conflict(r, ev, now)
        ack = ev.get("ack"); job = ev.get("job")
        def result(ok, reason="", state="completed"):
            return replace(r, state=state, result_ok=ok, result_reason=reason)
        if r.policy == "query":
            return result(bool(ack["ok"]), "" if ack["ok"] else "query_refused") if ack else r
        if ack and not ack["ok"]:
            positive = (job and job["state"] == "done") or "enrollment_job" in ev or "binding" in ev
            if positive: return self._conflict(r, ev, now)
            error = ack["error"]
            refused = (r.policy in {"ordinary", "wg"} and error in REFUSALS
                       or r.policy == "ordinary" and r.kind == "leave_pvp" and wg_refusal_guids(error, r.reserved_manifest)
                       or r.kind == "guild_grow" and _grow_refusal(error, r.reserved_manifest)
                       or r.policy == "wg" and error in {"no_wintergrasp", "no_online_members"}
                       or r.policy == "defaults" and (error in {"disabled", "busy", "busy:realm_owner"}
                           or error in {f"not_cast:{g}" for g in r.reserved_manifest})
                       or r.policy in {"ack", "result"} and error == "realm_off")
            if "bot" in ev or job or "origin_failure" in ev: refused = False
            if refused and r.kind == "leave_pvp" and error.startswith(WG_REFUSAL):
                return result(False, error, "completed")  # the named members, for the recall to leave out
            return result(False, "refused" if refused else "unknown", "completed" if refused else "unknown")
        if r.policy == "ack" and ack: return result(True)
        if r.policy == "wg":
            if "origin_failure" in ev: return result(False, "origin_failed", "unknown")
            return r
        if r.policy == "defaults" and "bot" in ev:
            bot = ev["bot"]; succeeded = tuple(bot["guids"]); skipped = tuple(s["guid"] for s in bot["skipped"])
            full = not set(succeeded) & set(skipped) and set(succeeded) | set(skipped) == set(r.reserved_manifest)
            ok = bot["state"] == "done" and bot["reason"] == "" and not skipped
            if not full: return result(False, "incomplete_defaults", "unknown")
            return result(ok, "" if ok else "defaults_failed") if ack else r
        if job:
            unchanged = (r.role == "root" and r.policy == "ordinary" and job.get("changed") is False
                         and job["guid"] in r.reserved_manifest and job["value"] == 0 and job["pvp_owner"] == ""
                         and not self.conn.execute("SELECT 1 FROM realm_action WHERE parent_id=? LIMIT 1",
                                                   (r.command_id,)).fetchone())
            if job["state"] == "failed" and (unchanged or _step_refused(r, job)):
                return result(False, STEP_REFUSED) if ack else r  # admitted: its ack is on its way
            if job["state"] == "failed": return result(False, "job_failed", "unknown")
            if ack: return result(True)
        return r

    def _finish(self, old, new, now):
        # Snapshot a valid parent before saving child evidence: a newly arriving
        # contradiction can invalidate a formerly clean proof. Reconcile it to
        # retained conflict atomically instead of refusing to read the parent.
        root = new if new.role == "root" else self.get(new.parent_id)
        self._save(new)
        if root.policy == "wg":
            root_old = root
            root = self._reconcile(root, now); self._save(root)
            if new.role != "root":
                self._prune(now=now)
                return self._transition(root_old, root)
            new = root
        self._prune(now=now)
        return self._transition(old, new)

    def observe_ack(self, ack, *, now):
        if type(ack) is not Ack: return Transition(False)
        identity(ack.id); integer(ack.ts, 0, 0x7FFFFFFFFFFFFFFF)
        if type(ack.ok) is not bool: raise ValueError("invalid action ack")
        text(ack.error, 0, 256)
        if ack.ok and ack.error: raise ValueError("invalid positive action ack")
        now = _now(now)
        with self.conn:
            old = self.get(ack.id)
            if old is None or old.transport_state == "not_entered": return Transition(False)
            ev = json.loads(old.evidence); incoming = {"ok": ack.ok, "error": ack.error}
            if "ack" in ev and ev["ack"] != incoming: ev["conflict"] = True
            else: ev["ack"] = incoming
            return self._finish(old, self._reduce(replace(old, transport_state="settled"), ev, now), now)

    def observe_send_error(self, command_id, safe_type, *, now):
        if type(safe_type) is not str or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", safe_type) is None: raise ValueError("invalid safe transport error")
        now = _now(now)
        with self.conn:
            old = self.get(command_id)
            if old is None or old.transport_state == "not_entered": return Transition(False)
            ev = json.loads(old.evidence)
            before_write = safe_type in {"NotConnected", "RealmDisabled"} and not any(k in ev for k in ("ack", "job", "bot", "enrollment_job", "origin_failure", "binding"))
            new = replace(old, transport_state="settled", send_error=safe_type, updated=now)
            if before_write and old.role == "root": new = replace(new, state="not_sent", result_ok=False, result_reason="not_sent")
            elif old.state not in TERMINAL and old.state != "conflict": new = replace(new, state="unknown", result_ok=False, result_reason="transport_unknown")
            return self._finish(old, new, now)

    def observe_realm_job(self, ev, *, now):
        if type(ev) is not p.RealmJob: return Transition(False)
        integer(ev.ts, 0, 0x7FFFFFFFFFFFFFFF)
        now = _now(now); incoming = _evidence(ev); _realm_job(incoming, ev.id)
        with self.conn:
            old = self.get(ev.id)
            if old is None or old.transport_state == "not_entered" or old.kind != ev.kind or old.policy in {"ack", "query", "defaults"}: return Transition(False)
            data = json.loads(old.evidence)
            if old.policy == "wg":
                if not ev.pvp_owner or ev.guid != 0 or ev.state == "done" and not 1 <= ev.value <= len(old.reserved_manifest): return Transition(False)
                key = "enrollment_job" if ev.state == "done" else "origin_failure"
                others = [data[k]["pvp_owner"] for k in ("enrollment_job", "origin_failure") if k in data]
                if "binding" in data: others.append(data["binding"]["pvp_owner"])
                if any(t != ev.pvp_owner for t in others): data["conflict"] = True
            else:
                expected_token = json.loads(old.payload).get("pvp_owner", "")
                if ev.pvp_owner != expected_token: return Transition(False)
                if ev.state == "done":
                    expected_guid = old.reserved_manifest[0] if len(old.reserved_manifest) == 1 else 0
                    if old.policy == "result": expected_guid = json.loads(old.payload).get("guid", json.loads(old.payload).get("creator_guid"))
                    if old.policy == "cleanup": expected_guid = 0
                    if ev.guid != expected_guid or old.kind == "calendar_post" and ev.value == 0: return Transition(False)
                    if old.policy == "cleanup" and ev.value != len(old.reserved_manifest): return Transition(False)
                key = "job"
            if key in data and data[key] != incoming: data["conflict"] = True
            else: data[key] = incoming
            return self._finish(old, self._reduce(old, data, now), now)

    def observe_bot_job(self, ev, *, now):
        if type(ev) is not Job: return Transition(False)
        # Job is shared with Guild progress and scoped Training mutations.
        # Establish our dispatched command and exact receipt namespace before
        # applying the narrower terminal/default-accounting validator.
        if type(ev.id) is not str: return Transition(False)
        with self.conn:
            old = self.get(ev.id)
            if (old is None or old.role != "root" or old.policy != "defaults"
                    or old.transport_state == "not_entered" or ev.kind != old.kind
                    or ev.training_run != json.loads(old.payload)["training_run"]):
                return Transition(False)
            identity(ev.id); integer(ev.ts, 0, 0x7FFFFFFFFFFFFFFF)
            now = _now(now)
            incoming = json.loads(document(_evidence(ev), limit=32768)); _bot(incoming, ev.id)
            data = json.loads(old.evidence)
            if "bot" in data and data["bot"] != incoming: data["conflict"] = True
            else: data["bot"] = incoming
            return self._finish(old, self._reduce(old, data, now), now)

    def prepare_query(self, origin_id, query, *, now):
        now = _now(now); c = classify(query, child=True)
        if type(query) is not p.GetRealmPvp or query.origin_id != origin_id or query.pvp_owner: raise ValueError("invalid recovery query")
        with self.conn:
            root = self.get(origin_id)
            if root is None or root.policy != "wg" or root.state in TERMINAL or root.transport_state == "not_entered": raise ValueError("invalid query origin")
            if self.get(query.id) is not None: raise ValueError("query identity already enrolled")
            data = json.loads(root.evidence)
            if now < data.get("last_query", -1e30) + 10: raise ValueError("recovery query too soon")
            previous = self.get(data["query_id"]) if data.get("query_id") else None
            if previous is not None:
                if previous.transport_state != "settled" and previous.state != "not_sent": raise ValueError("query still outstanding")
                self.conn.execute("DELETE FROM realm_action WHERE command_id=?", (previous.command_id,))
            child = self._insert(ActionSpec(query.id, origin_id, "query", c.policy, root.owner, root.rank, c.kind, c.payload, (), root.guild, root.human), now)
            data.update(query_id=query.id, last_query=now)
            self._save(replace(root, evidence=document(data, limit=32768), updated=now))
            return child

    def observe_pvp(self, ev, *, now):
        if type(ev) is not p.RealmPvp: return Transition(False)
        integer(ev.ts, 0, 0x7FFFFFFFFFFFFFFF)
        now = _now(now); incoming = json.loads(document(_evidence(ev), limit=32768)); _status(incoming, ev.id)
        with self.conn:
            query = self.get(ev.id)
            if query is None or query.policy != "query" or query.transport_state == "not_entered": return Transition(False)
            root = self.get(query.parent_id); data = json.loads(root.evidence)
            if data.get("query_id") != query.command_id or ev.origin_id != root.command_id: return Transition(False)
            qdata = json.loads(query.evidence)
            if "status" in qdata and qdata["status"] != incoming: qdata["conflict"] = True
            else: qdata["status"] = incoming
            return self._finish(query, self._reduce(query, qdata, now), now)

    def _reconcile(self, root, now):
        data = json.loads(root.evidence)
        if root.state == "not_sent": return root
        query = self.get(data["query_id"]) if data.get("query_id") else None
        if query:
            qdata = json.loads(query.evidence)
            if qdata.get("conflict"): data["conflict"] = True
            ack = qdata.get("ack")
            candidate = qdata.get("status") if ack and ack["ok"] else None
            if candidate:
                binding = {k: candidate[k] for k in ("origin_id", "pvp_owner", "kind", "guild", "human", "origin_epoch", "members")}
                if (candidate["kind"] != "wintergrasp" or candidate["guild"] != root.guild or candidate["human"] != root.human
                        or not set(candidate["members"]) <= set(root.reserved_manifest)):
                    data["conflict"] = True
                elif "binding" in data and data["binding"] != binding: data["conflict"] = True
                else:
                    data["binding"] = binding; root = replace(root, native_manifest=tuple(candidate["members"]))
                    for key in ("enrollment_job", "origin_failure"):
                        if key in data and data[key]["pvp_owner"] != candidate["pvp_owner"]: data["conflict"] = True
                    previous = data.get("status")
                    if previous is None or candidate["revision"] > previous["revision"]:
                        if previous:
                            stages = {name: i for i, name in enumerate(("acquiring", "queued", "invited", "entering", "active", "ending", "cleaning", "clean", "unknown"))}
                            legal_cancel = (previous["stage"] == "cleaning" and candidate["stage"] == "ending"
                                            and candidate["application_fenced"] and not candidate["cleanup_id"])
                            if (previous["application_fenced"] and not candidate["application_fenced"]
                                    or previous["stage"] == "unknown" and candidate["stage"] != "unknown"
                                    or previous["stage"] == "clean" and candidate["stage"] != "clean"
                                    or stages[candidate["stage"]] < stages[previous["stage"]] and not legal_cancel): data["conflict"] = True
                        data["status"] = candidate
                    elif candidate["revision"] == previous["revision"] and candidate != previous: data["conflict"] = True
        ack = data.get("ack")
        if ack and not ack["ok"] and "binding" in data: data["conflict"] = True
        cleanup = self.get(data["cleanup_id"]) if data.get("cleanup_id") else None
        status = data.get("status")
        if status and status["stage"] in {"cleaning", "clean"} and status["cleanup_id"] in data.get("cleanup_ids", []):
            admitted = data.setdefault("cleanup_admitted", [])
            if status["cleanup_id"] not in admitted: admitted.append(status["cleanup_id"])
        for controller_id in data.get("cleanup_ids", []):
            controller = self.get(controller_id)
            if controller is None: raise ActionUnavailable("realm action evidence unavailable")
            controller_data = json.loads(controller.evidence)
            if controller_data.get("conflict") or controller.state == "conflict": data["conflict"] = True
            if controller_id in data.get("cleanup_admitted", []) and controller_data.get("ack", {}).get("ok") is False:
                data["conflict"] = True
        if cleanup:
            cdata = json.loads(cleanup.evidence)
            if cdata.get("conflict") or cleanup.state == "conflict": data["conflict"] = True
            cack = cdata.get("ack")
            if status and status["cleanup_id"] == cleanup.command_id and status["stage"] in {"cleaning", "clean"} and cack and not cack["ok"]: data["conflict"] = True
            if cleanup.state == "not_sent" and cleanup.transport_state == "not_entered":
                data["cleanup_id"] = ""; cleanup = None
            elif (status and status["stage"] == "ending" and status["application_fenced"] and not status["cleanup_id"]
                    and status["revision"] > cdata.get("dispatch_revision", U64)):
                canceled = data.setdefault("canceled", [])
                if cleanup.command_id not in canceled: canceled.append(cleanup.command_id)
                data["cleanup_id"] = ""; cleanup = None
        if data.get("conflict") or root.state == "conflict": return self._conflict(root, data, now)
        if status and status["stage"] == "unknown":
            return replace(root, state="unknown", evidence=document(data, limit=32768), result_ok=False, result_reason="native_unknown", updated=now)
        complete = False
        if cleanup:
            cdata = json.loads(cleanup.evidence)
            complete = cleanup.state == "completed" and cleanup.result_ok is True
            if (status and status["stage"] == "clean" and status["application_fenced"]
                    and status["cleanup_id"] == cleanup.command_id
                    and cleanup.transport_state == "settled" and root.transport_state == "settled"
                    and not (data.get("ack") and not data["ack"]["ok"])
                    and not (cdata.get("ack") and not cdata["ack"]["ok"])): complete = True
        if complete and root.transport_state == "settled":
            return replace(root, state="completed", evidence=document(data, limit=32768), result_ok=True, result_reason="", updated=now)
        return replace(root, evidence=document(data, limit=32768), updated=now)

    def prepare_cleanup(self, origin_id, command, *, now):
        now = _now(now); c = classify(command, child=True)
        if type(command) is not p.LeavePvp or c.policy != "cleanup": raise ValueError("invalid cleanup command")
        with self.conn:
            root = self.get(origin_id)
            if root is None or root.policy != "wg" or root.state in TERMINAL or root.state == "conflict": raise ValueError("invalid cleanup origin")
            data = json.loads(root.evidence); binding = data.get("binding"); status = data.get("status")
            if (binding is None or status is None or status["stage"] in {"unknown", "clean"} or data.get("cleanup_id")
                    or command.pvp_owner != binding["pvp_owner"] or tuple(command.guids) != root.native_manifest): raise ValueError("cleanup requires complete proved manifest")
            history = data.setdefault("cleanup_ids", [])
            if len(history) >= 17: raise ActionCapacity("cleanup replacement capacity")
            child = self._insert(ActionSpec(command.id, origin_id, "cleanup", c.policy, root.owner, root.rank,
                               c.kind, c.payload, c.reserved_manifest, root.guild, root.human), now)
            child = self._save(replace(child, evidence=document({"dispatch_revision": status["revision"]})))
            history.append(command.id); data["cleanup_id"] = command.id
            self._save(replace(root, evidence=document(data, limit=32768), updated=now))
            return child

    def recover(self, *, process_id, now):
        text(process_id, 1, 256); now = _now(now)
        with self.conn:
            self.holds()
            for old in self.records():
                if old.state == "prepared": self._save(replace(old, state="not_sent", result_ok=False, result_reason="restart_not_sent", updated=now))
                elif old.transport_state == "inflight":
                    self._save(replace(old, transport_state="settled", send_error="ProcessRestart",
                         state=old.state if old.state in TERMINAL or old.state == "conflict" else "unknown", updated=now))
            for root in self.unresolved():
                # An unknown whose only evidence is native's refusal is read again under the current
                # rules: at its ack (a fenced body), or a failed job after a positive ack (one body
                # refused at its step, native released them all). An older build kept them unknown.
                ev = json.loads(root.evidence)
                ack = ev.get("ack", {}).get("ok")
                if (root.state == "unknown" and root.transport_state == "settled" and not root.send_error
                        and (root.result_reason == "unknown" and set(ev) <= {"ack", "failed_at"} and ack is False
                             or root.result_reason == "job_failed" and set(ev) <= {"ack", "job", "failed_at"}
                             and ack is True)):
                    settled = self._reduce(replace(root, state="dispatched", result_ok=None, result_reason=""), ev, now)
                    if settled.state == "completed": self._save(settled)
            for root in self.unresolved():
                if root.policy == "wg": self._save(self._reconcile(root, now))
            return self.unresolved()

    def abandon(self, command_id, *, reason, now):
        """The operator's release of one unknown body move: completed, result abandoned, holding
        nobody. Only with the mind stopped, under its lock (realm release). The meta receipt keeps
        the time, his reason and the row as it was; the prune never deletes the row."""
        identity(command_id); now = _now(now)
        if not release_reason(reason): raise ValueError("a release needs a reason of 1 to 200 printable characters")
        with self.conn:
            self.holds()  # the whole journal reads before one row of it changes
            old = self.get(command_id)
            if old is None: raise ValueError("no such root")
            refusal = abandon_refusal(old, self._records("parent_id=?", (command_id,)))
            if refusal: raise ValueError(refusal)
            key = ABANDON_RECEIPT + command_id
            if self.conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() is not None:
                raise ValueError("this root was already released once")
            ev = json.loads(old.evidence)
            receipt = {"at": now, "reason": reason, "seq": old.seq, "kind": old.kind, "policy": old.policy,
                       "owner": old.owner, "state": old.state, "result_reason": old.result_reason,
                       "created": old.created, "dispatched": old.dispatched,
                       "members": list(old.reserved_manifest), "evidence": ev}
            new = self._save(replace(old, state="completed", evidence=document({**ev, "abandoned": now}, limit=32768),
                                     result_ok=False, result_reason=ABANDONED, updated=now))
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, document(receipt)))
            return new

    def _prune(self, keep=256, now=None):
        roots = tuple(r for r in self._records("role='root' AND state IN ('completed','not_sent')") if not _durable(r))
        protected = {r.command_id for r in roots if r.result_ok is False and "failed_at" in json.loads(r.evidence)
                     and (now is None or now < json.loads(r.evidence)["failed_at"] + 600)}
        newest = sorted((r for r in roots if r.command_id not in protected), key=lambda r: (r.updated, r.seq), reverse=True)
        retain = protected | {r.command_id for r in newest[:max(0, keep-len(protected))]}
        remove = tuple(r for r in roots if r.command_id not in retain)
        count = 0
        for root in remove:
            children = self._records("parent_id=?", (root.command_id,))
            if any(c.transport_state == "inflight" for c in children) or root.transport_state == "inflight": continue
            count += self.conn.execute("DELETE FROM realm_action WHERE parent_id=?", (root.command_id,)).rowcount
            count += self.conn.execute("DELETE FROM realm_action WHERE command_id=?", (root.command_id,)).rowcount
        return count

    def prune(self, *, keep=256, now=None):
        integer(keep, 0, 256)
        if now is not None: now = _now(now)
        with self.conn: return self._prune(keep, now)
