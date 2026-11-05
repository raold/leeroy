"""Read-only ownership evidence; this module never claims or restores bodies."""

from dataclasses import asdict, dataclass
import json
import math

from leeroy_mind.guild.raids import CATALOG


@dataclass(frozen=True, slots=True)
class JournalBodyScope:
    run: object
    owner: str
    scope_id: str
    members: frozenset[int]


def identity(value):
    if (type(value) is not str or not value or "\0" in value
            or len(value.encode("utf-8")) > 256):
        raise ValueError("body ownership evidence unavailable")
    return value


def members(value):
    if type(value) not in (list, tuple, frozenset):
        raise ValueError("body ownership evidence unavailable")
    if (not value or any(type(g) is not int or not 0 < g <= 0xFFFFFFFF for g in value)
            or len(value) != len(set(value))):
        raise ValueError("body ownership evidence unavailable")
    return frozenset(value)


def _same(left, right):
    # JSON equality preserves bool/int distinctions in persisted plans.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False)


def session_evidence(conn, session, human, *, run=None, proof=False):
    """Validate persisted identity, including sessions retained by their owner.

    Reservation reads permit closing/unbound sessions: neither state proves
    release. Application/restoration proof additionally requires an open row
    and its exact positive instance binding. Controller slots are Realm's job.
    """
    raid_id, started, plan = session.raid_id, session.started, session.plan
    if (type(raid_id) is not int or raid_id <= 0 or type(human) is not int or human <= 0
            or type(started) not in (int, float) or not math.isfinite(started)):
        raise ValueError("body ownership evidence unavailable")
    saved = asdict(plan)
    bots = members(plan.members)
    if (human in bots or type(plan.offline) is not bool or type(plan.human_in_raid) is not bool
            or plan.offline and plan.human_in_raid or plan.raid_key not in CATALOG
            or type(plan.size) is not int or plan.size not in (10, 25)
            or type(plan.heroic) is not bool or len(bots) + int(plan.human_in_raid) > plan.size
            or any(type(g) is not int or not 0 < g <= 0xFFFFFFFF for g in
                   (plan.lead_guid, plan.tank_guid, plan.loot_guid))
            or plan.tank_guid not in bots or plan.loot_guid not in bots
            or plan.lead_guid not in bots | ({human} if plan.human_in_raid else set())):
        raise ValueError("body ownership evidence unavailable")
    row = conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()
    stored = conn.execute("SELECT value FROM meta WHERE key=?", (f"guild:raid:{raid_id}:plan",)).fetchone()
    if (row is None or stored is None or not _same(json.loads(stored[0]), saved)
            or row["started"] != started
            or any(row[key] != getattr(plan, key) for key in
                   ("raid_key", "size", "heroic", "offline", "lead_guid", "tank_guid", "loot_guid"))
            or session.ctx.raid_id != raid_id or session.ctx.plan is not plan
            or session.ctx.map_id != CATALOG[plan.raid_key].map_id
            or session._members != bots):
        raise ValueError("body ownership evidence unavailable")
    if run is not None and (run.session is not session or not _same(asdict(run.plan), saved)
            or run.human_in_raid is not plan.human_in_raid or type(run.lead_is_human) is not bool
            or run.lead_is_human != (plan.lead_guid == human)):
        raise ValueError("body ownership evidence unavailable")
    instance = session.ctx.instance_id
    if proof:
        bound = conn.execute("SELECT value FROM meta WHERE key=?", (f"guild:raid:{raid_id}:instance",)).fetchone()
        if (row["ended"] is not None or session._closing is not False
                or type(instance) is not int or not 0 < instance <= 0xFFFFFFFF
                or bound is None or type(json.loads(bound[0])) is not int or json.loads(bound[0]) != instance):
            raise ValueError("body ownership evidence unavailable")
    return identity(f"guild:raid:{raid_id}:{started}"), bots, instance


def validate_journals(ground):
    """Every unresolved applier journal must belong to an exact retained run.

    Jobs do not use tg_applied. Orphan/legacy idle rows therefore cannot borrow
    a lab or benchmark reservation. This only accounts for the journal; existing
    native binding/fence checks still decide whether any restore may execute.
    """
    from .applier import _command_guids, _decode
    from .native_cleanup import NativeRun

    rows = ground.ports.conn.execute("SELECT * FROM tg_applied WHERE undone IS NULL ORDER BY id").fetchall()
    if not rows:
        return ()
    listener = ground.listener
    runs = list(listener._ending.values())
    if listener.run is not None and all(r is not listener.run for r in runs):
        runs.append(listener.run)
    validated = {}
    for run in runs:
        key = listener.run_key(run)
        if key in validated:
            raise ValueError("body ownership evidence unavailable")
        session_key, bots, instance = session_evidence(
            ground.ports.conn, run.session, ground.ports.human_guid, run=run, proof=True)
        validated[key] = (run, session_key, bots, instance)
    evidence = []
    for row in rows:
        envelope = json.loads(row["command"])
        scope = envelope["scope"]
        run, session_key, bots, instance = validated[row["run"]]
        owner = "offline_raid" if run.plan.offline else "raid"
        epoch = scope["connection_epoch"]
        expected_owner = owner if getattr(ground.app, "bodies", None) is not None else None
        required = {"run_key", "session_key", "instance_id", "connection_epoch", "body_owner",
                    "tank_guid", "lead_is_human"}
        if (set(scope) - {"native"} != required
                or type(row["ts"]) not in (int, float) or not math.isfinite(row["ts"]) or row["ts"] < 0
                or scope["run_key"] != row["run"] or scope["session_key"] != session_key
                or type(scope["instance_id"]) is not int or scope["instance_id"] != instance
                or scope["body_owner"] != expected_owner or type(scope["tank_guid"]) is not int
                or scope["tank_guid"] != run.plan.tank_guid
                or scope["lead_is_human"] is not run.lead_is_human
                or type(epoch) is not int or not 0 < epoch <= 0xFFFFFFFFFFFFFFFF
                or type(envelope["settled"]) is not bool):
            raise ValueError("body ownership evidence unavailable")
        for wire in (envelope["wire"], json.loads(row["inverse"])):
            command = _decode(wire)
            if command is not None and not members(_command_guids(command)) <= bots:
                raise ValueError("body ownership evidence unavailable")
        targets = members(_command_guids(_decode(envelope["wire"])))
        manifest = envelope["members"]
        if type(manifest) is not dict or set(manifest) != {str(g) for g in targets}:
            raise ValueError("body ownership evidence unavailable")
        for guid in targets:
            saved = manifest[str(guid)]
            if (type(saved) is not dict or set(saved) != {"guid", "role", "cls", "spec_tab"}
                    or type(saved["guid"]) is not int or saved["guid"] != guid
                    or saved["role"] not in {"tank", "healer", "melee", "ranged"}
                    or type(saved["cls"]) is not int or saved["cls"] not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 11}
                    or type(saved["spec_tab"]) is not int or not -1 <= saved["spec_tab"] <= 2):
                raise ValueError("body ownership evidence unavailable")
        keys = envelope["keys"]
        if (type(keys) is not list or any(type(key) is not list or len(key) != 2
                or type(key[0]) is not int or key[0] not in bots or type(key[1]) is not str
                or not key[1] or len(key[1]) > 256 for key in keys)):
            raise ValueError("body ownership evidence unavailable")
        source_scope = session_key
        if "native" in scope:
            native = NativeRun.load(scope["native"])
            if (not run.plan.offline or native.run_key != row["run"] or native.session_key != session_key
                    or native.instance_id != instance or set(native.members) != bots
                    or native.human_guid != ground.ports.human_guid or native.leader_guid != run.plan.tank_guid
                    or native.map_id != run.session.ctx.map_id):
                raise ValueError("body ownership evidence unavailable")
            source_scope = identity(native.job_id)
        elif run.plan.offline:
            # Without the original command identity a retained journal cannot
            # borrow a replacement offline operation's reservation.
            raise ValueError("body ownership evidence unavailable")
        evidence.append(JournalBodyScope(run, owner, source_scope, bots))
    return tuple(evidence)


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _member_list(value, full):
    if (type(value) is not list or any(type(g) is not int or g not in full for g in value)
            or len(value) != len(set(value))):
        raise ValueError("lab body evidence unavailable")
    return set(value)


def validate_lab_complete(conn, owned):
    """Read the actual finish transaction; a phase flag alone is not handback.

    Both installed and disabled Training use this source-owned terminal rule.
    It never repairs state or advances attempts. The producer's summary/cursor
    and restored/rejected attempt receipts corroborate its complete history.
    """
    from .lab import _DAMAGE_SPECS
    from .lab_runner import ATTEMPT_PREFIX, CURSOR_KEY, SUMMARY_PREFIX

    scope, full = identity(owned["id"]), members(owned["guids"])
    if (owned["phase"] != "complete" or owned["current"] is not None
            or type(owned["spec"]) is not list or len(owned["spec"]) != 2
            or any(type(v) is not int for v in owned["spec"]) or tuple(owned["spec"]) not in _DAMAGE_SPECS
            or not _finite_number(owned["started"]) or owned["started"] < 0
            or type(owned["attempts"]) is not int or not 0 <= owned["attempts"] <= 128):
        raise ValueError("lab body evidence unavailable")
    _member_list(owned["alive"], full)
    _member_list(owned["preempted"], full)
    attempts = owned["attempt_ids"]
    if (type(attempts) is not list or len(attempts) != owned["attempts"]
            or len(attempts) != len(set(attempts))):
        raise ValueError("lab body evidence unavailable")
    for ident in attempts:
        identity(ident)
    night = identity(owned["night"])
    summary = _meta(conn, SUMMARY_PREFIX + night)
    if (type(summary) is not dict or summary["lab"] != scope
            or not _same(summary["spec"], owned["spec"]) or not _same(_meta(conn, CURSOR_KEY), owned["spec"])
            or summary["attempts"] != owned["attempts"] or type(summary["attempts"]) is not int
            or type(owned["halt"]) is not str or summary["reason"] != owned["halt"]
            or summary["status"] != "dummy/provisional" or summary["metric"] != "dummy_requested_damage_proxy"
            or summary["field_performance"] != "unavailable"
            or not _finite_number(summary["ts"]) or summary["ts"] < owned["started"]
            or type(owned["gaps"]) is not list or not _same(summary["gaps"], owned["gaps"])
            or any(type(gap) is not str or gap not in attempts for gap in owned["gaps"])):
        raise ValueError("lab body evidence unavailable")
    for ident in attempts:
        attempt = _meta(conn, ATTEMPT_PREFIX + ident)
        if type(attempt) is not dict or attempt["id"] != ident:
            raise ValueError("lab body evidence unavailable")
        guids = _member_list(attempt["guids"], full)
        if (not guids or type(attempt["sent_ms"]) is not int
                or attempt["sent_ms"] < int(owned["started"] * 1000)):
            raise ValueError("lab body evidence unavailable")
        if attempt.get("rejected") is True:
            if attempt["ack"] != "rejected" or attempt["terminal"] is not None:
                raise ValueError("lab body evidence unavailable")
            continue
        terminal = attempt["terminal"]
        if (attempt["lab"] != scope or not _same(attempt["spec"], owned["spec"])
                or attempt["restoration"] != "complete_no_desired_setup"
                or not _finite_number(attempt["restoration_at"])
                or not attempt["sent_ms"] / 1000 <= attempt["restoration_at"] <= summary["ts"]
                or type(terminal) is not dict or terminal["id"] != ident or terminal["kind"] != "dummy_trial"
                or terminal["state"] not in {"done", "failed"} or terminal["stage"] != ""
                or type(terminal["ts"]) is not int or terminal["ts"] < attempt["sent_ms"]
                or type(terminal["reason"]) is not str or terminal["reason"].startswith("cleanup_failed")
                or type(terminal["skipped"]) is not list):
            raise ValueError("lab body evidence unavailable")
        returned = _member_list(terminal["guids"], guids)
        skipped = []
        for skip in terminal["skipped"]:
            if (type(skip) is not dict or type(skip["guid"]) is not int or skip["guid"] not in guids
                    or type(skip["reason"]) is not str or skip["reason"].startswith("cleanup_failed")):
                raise ValueError("lab body evidence unavailable")
            skipped.append(skip["guid"])
        if len(skipped) != len(set(skipped)) or returned.intersection(skipped) or returned | set(skipped) != guids:
            raise ValueError("lab body evidence unavailable")
