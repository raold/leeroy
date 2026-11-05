"""Exact native offline ownership bindings and queried cleanup evidence.

The event path only records a bounded hint for our own start command. Queries
and receipt settlement belong to independently owned service/listener lanes.
An unknown handle, reconnect, or ordinary job terminal proves no restoration.
"""

import asyncio
from dataclasses import asdict, dataclass
import json
import re
from uuid import uuid4

from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.offline import OWNED_KEY
from leeroy_mind.guild.protocol import Job

from .protocol import GetTrainingCleanup, TrainingCleanup


PREFIX = "tg_native_binding:"
PROOF_PREFIX = "tg_native_proof:"
DONE_PREFIX = "tg_native_done:"
HINT_KEY = "tg_native_hint"


@dataclass(frozen=True, slots=True)
class NativeRun:
    run_key: str
    session_key: str
    training_run: str
    job_id: str
    training_token: str
    leader_guid: int
    guild_id: int
    human_guid: int
    map_id: int
    instance_id: int
    group_id: str
    members: tuple[int, ...]

    def __post_init__(self):
        if any(type(s) is not str or not s or len(s) > 256 for s in (self.run_key, self.session_key)):
            raise ValueError("invalid local native binding")
        # Reuse the bounded, strict wire scope validation for persisted data.
        TrainingCleanup(0, "binding", 1, **self.native_fields(), state="open")

    def native_fields(self):
        return {k: v for k, v in asdict(self).items() if k not in {"run_key", "session_key"}}

    def saved(self):
        result = asdict(self)
        result["members"] = list(self.members)
        return result

    @classmethod
    def load(cls, value):
        if type(value) is not dict or type(value.get("members")) not in {list, tuple}:
            raise ValueError("invalid saved native binding")
        return cls(**(value | {"members": tuple(value["members"])}))

    @classmethod
    def bind(cls, run_key, session_key, receipt):
        fields = {k: v for k, v in asdict(receipt).items() if k not in {"ts", "id", "state", "receipt_version"}}
        fields["members"] = tuple(sorted(fields["members"]))
        return cls(run_key, session_key, **fields)

    def matches(self, receipt):
        if not isinstance(receipt, TrainingCleanup) or receipt.state == "unknown":
            return False
        try:
            other = NativeRun.bind(self.run_key, self.session_key, receipt)
            return other == self
        except (TypeError, ValueError):
            return False


def session_key(run):
    return f"guild:raid:{run.session.raid_id}:{run.session.started}"


class NativeCleanup:
    def __init__(self, ports, ground):
        self.ports, self.ground = ports, ground
        self._open = {}
        self._query_lock = asyncio.Lock()
        self._cursor = ""

    def disconnect(self):
        self._open.clear()

    def _saved(self, key):
        try:
            return NativeRun.load(_get(self.ports.conn, PREFIX + key))
        except (TypeError, ValueError, KeyError):
            return None

    def for_run(self, run):
        key = self.ground.listener.run_key(run)
        binding = self._open.get(key)
        owned = self._owned_run(run)
        hint = _get(self.ports.conn, HINT_KEY)
        if (binding is None or owned is None or owned.get("id") != binding.job_id or type(hint) is not dict
                or hint.get("job_id") != binding.job_id or hint.get("training_run") != binding.training_run
                or hint.get("poisoned") or hint.get("sealed")
                or binding.session_key != session_key(run)
                or binding.instance_id != run.session.ctx.instance_id
                or binding.map_id != run.session.ctx.map_id
                or binding.leader_guid != run.plan.tank_guid
                or set(binding.members) != set(run.plan.members)
                or self.ground.applier.native_fenced(binding)):
            return None
        return binding

    def _pending_engage(self, run):
        # Guild callbacks are on a ticker, so a boss event can already be
        # validated/queued while proof lookup or a hold ACK is awaiting. Do not
        # install startup tactics ahead of that queued combat transition.
        offline = getattr(getattr(self.ground.app, "guild", None), "offline", None)
        return any(kind == "boss" and ev.state == "engaged"
                   and ev.map == run.session.ctx.map_id and ev.instance_id == run.session.ctx.instance_id
                   and set(ev.members).intersection(run.plan.members)
                   for kind, ev in getattr(offline, "_inputs", ()))

    def feed(self, ev):
        """No transport await; only our exact owned command can supply a hint."""
        if not isinstance(ev, Job) or ev.kind != "offline_raid":
            return
        owned = _get(self.ports.conn, OWNED_KEY)
        offline = getattr(getattr(self.ground.app, "guild", None), "offline", None)
        if (not isinstance(owned, dict) or ev.id != owned.get("id")
                or offline is None or not offline._valid_job(ev)
                or type(ev.training_run) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ev.training_run)):
            return
        previous = _get(self.ports.conn, HINT_KEY)
        same = type(previous) is dict and previous.get("job_id") == ev.id
        hint = dict(job_id=ev.id, training_run=previous.get("training_run", "") if same else ev.training_run,
                    poisoned=bool(same and (previous.get("poisoned") or previous.get("training_run") != ev.training_run)),
                    sealed=bool(same and previous.get("sealed") or ev.state in {"done", "failed"}
                                or ev.stage == "tearing_down"))
        with self.ports.conn:
            _put(self.ports.conn, HINT_KEY, hint)
        if hint["poisoned"] or hint["sealed"]:
            for key, binding in tuple(self._open.items()):
                if binding.job_id == ev.id:
                    self.ground.applier.fence_native(binding)
                    self._open.pop(key, None)

    def _owned_run(self, run):
        owned = _get(self.ports.conn, OWNED_KEY)
        offline = getattr(getattr(self.ground.app, "guild", None), "offline", None)
        if (not isinstance(owned, dict) or offline is None or offline.run is not run
                or not offline.active or owned.get("cancelled") or owned.get("terminal") is not None
                or owned.get("raid_id") != run.session.raid_id or owned.get("instance") != run.session.ctx.instance_id
                or owned.get("actual_plan") != asdict(run.plan)
                or not run.plan.offline or run.human_in_raid or run.lead_is_human
                or run.session._closing or self._pending_engage(run)):
            return None
        return owned

    async def _query(self, handle):
        ident = "tgproof_" + uuid4().hex
        rows = await self.ports.request(GetTrainingCleanup(ident, handle), "training_cleanup",
            until=lambda ev: True, timeout_s=5.0)
        if len(rows) != 1:
            return None
        result = rows[0]
        return result if (isinstance(result, TrainingCleanup) and result.id == ident
                          and result.training_run == handle) else None

    async def prepare(self, run):
        """Listener ticker prerequisite; a delayed hint/query is retryable."""
        if not run.plan.offline:
            return True
        owned = self._owned_run(run)
        if owned is None or self._query_lock.locked():
            return False
        key = self.ground.listener.run_key(run)
        if self.for_run(run) is not None:
            return True
        hint = _get(self.ports.conn, HINT_KEY)
        if (not isinstance(hint, dict) or hint.get("job_id") != owned["id"]
                or hint.get("poisoned") or hint.get("sealed")):
            return False
        old = self._saved(key)
        if (old is None and self.ports.conn.execute("SELECT 1 FROM meta WHERE key=?", (PREFIX + key,)).fetchone()):
            return False  # Corrupt prior identity is not an absent first binding.
        if old is not None and (old.training_run != hint.get("training_run") or old.session_key != session_key(run)):
            return False
        epoch = self.ports.connection_epoch
        async with self._query_lock:
            receipt = await self._query(hint["training_run"])
        if (receipt is None or receipt.state != "open" or epoch != self.ports.connection_epoch
                or self._owned_run(run) != owned or _get(self.ports.conn, HINT_KEY) != hint
                or not self.ground._connected or self.ground._stopping):
            return False
        try:
            guild = self.ports.conn.execute("SELECT value FROM meta WHERE key='guild_id'").fetchone()
            if (guild is None or receipt.guild_id != int(guild[0])
                    or receipt.human_guid != self.ports.human_guid or receipt.job_id != owned["id"]
                    or receipt.leader_guid != run.plan.tank_guid or receipt.map_id != run.session.ctx.map_id
                    or receipt.instance_id != run.session.ctx.instance_id
                    or set(receipt.members) != set(run.plan.members)):
                return False
            binding = NativeRun.bind(key, session_key(run), receipt)
        except (TypeError, ValueError):
            return False
        if old is not None and old != binding:
            return False
        if self.ground.applier.native_fenced(binding):
            return False
        # Commit the exact immutable receipt before any hold or tactic send.
        with self.ports.conn:
            _put(self.ports.conn, PREFIX + key, binding.saved())
        self._open[key] = binding
        return True

    async def tick(self):
        """Query at most one durable binding per tick, including detached runs."""
        if self._query_lock.locked():
            return
        row = self.ports.conn.execute(
            "SELECT m.key,m.value FROM meta m WHERE m.key GLOB ? AND m.key>? "
            "AND NOT EXISTS (SELECT 1 FROM meta d WHERE d.key=? || substr(m.key,?)) ORDER BY m.key LIMIT 1",
            (PREFIX + "*", self._cursor, DONE_PREFIX, len(PREFIX) + 1)).fetchone()
        if row is None:
            self._cursor = ""
            return
        self._cursor = row[0]
        try:
            binding = NativeRun.load(json.loads(row[1]))
        except (TypeError, ValueError, KeyError):
            return
        saved_proof = _get(self.ports.conn, PROOF_PREFIX + binding.training_run)
        if saved_proof is not None:
            try:
                receipt = TrainingCleanup(**(saved_proof["receipt"] | {
                    "members": tuple(saved_proof["receipt"]["members"])}))
            except (TypeError, ValueError, KeyError):
                return
        else:
            async with self._query_lock:
                receipt = await self._query(binding.training_run)
        if receipt is None or not binding.matches(receipt):
            return
        if receipt.state in {"restoring", "complete"}:
            self._open.pop(binding.run_key, None)
            self.ground.applier.fence_native(binding)
        if receipt.state == "complete":
            if await self.ground.applier.settle_native(binding, receipt):
                # Keep immutable history, but do not let completed years of
                # bindings delay the bounded scan of unresolved ownership.
                with self.ports.conn:
                    _put(self.ports.conn, DONE_PREFIX + binding.run_key,
                         dict(training_run=binding.training_run, job_id=binding.job_id))
