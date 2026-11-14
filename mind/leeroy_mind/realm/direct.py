"""The realm's direct commands: one exactly-once intent per command that moves no body.

Actor sets, speech, renames, bank money and tabs, grants, calendar posts and mail never
reserve a body, so they bypass the body ledger and the paced action queue (native still fences
each one). Each owner names what it wants done by a ref of its own, and the ledger keeps at
most one live intent per (owner, ref):

  prepared   decided, not yet at the barrier; it never reached the wire.
  dispatched committed by before_send just before the bridge writes it.
  acked      native did it. One that expects a job waits here, unsettled, until the job's
             value comes in; a job may also come before the ack, and settles it the same way.
  refused    native refused it (by ack or by its job), or the bridge refused it before writing
             anything. Never done: the owner may prepare the ref again.
  unknown    it may have reached native, and nothing says whether it was done. Never sent
             again and never replaced: the ref stays taken for good.
  abandoned  it never reached the wire (cancelled, refused at the barrier, or found prepared
             at a restart). The owner may prepare the ref again.

An acked or unknown intent keeps its ref, so a ref is never dispatched twice. prune removes
refused and abandoned rows (their refs are free already), all but the newest 512, except a calendar
post's for RETAIN_POST_REFUSALS_S: the desk reads their reasons, so that a busy refusal never uses
up one of a post's tries (realm/calendar.py). prune_acked removes acked rows of the
refs in RETAIN_ACKED a month after they settled: calendar signups (only ever asked for a coming
event, and the event is long past) and speech's lines and emotes (each ref is one moment's,
never asked again). Every other acked row, and every unknown one, is kept. Every transition
after prepare is one synchronous Store unit of its own, with no await inside. The routing of
on_send, on_ack, on_job and on_send_error to this ledger is the service's.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

from leeroy_mind.protocol import Ack
from leeroy_mind.storage.store import Store

from .action_policy import classify
from .actions import ActionContext
from .protocol import RealmJob

log = logging.getLogger(__name__)

OWNERS = frozenset({"actors", "speech", "step", "canon", "calendar", "mail"})
# Body moves stay on the action queue; announcements and re-admissions have their own ledgers.
NOT_DIRECT = frozenset({"realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_team",
                        "arena_queue", "duel", "bot_do", "announce", "get_realm_pvp", "guild_readmit"})
LIVE = ("prepared", "dispatched", "acked", "unknown")
PRUNE_EVERY_S = 3600
# (owner, ref prefix) whose acked rows are removed RETAIN_ACKED_S after they settled: refs no
# owner can ask for again by then. A step's items, a mail, a grant or a post keep theirs.
RETAIN_ACKED = (("calendar", "signup:"), ("speech", "line:"), ("speech", "emote:"))
RETAIN_ACKED_S = 30 * 86400
# A calendar post is never prepared for an event more than about a week away, so its refusals are
# read only within this.
RETAIN_POST_REFUSALS_S = 30 * 86400
# Send errors raised before the bridge writes anything: the command cannot have gone out.
_PRETRANSPORT = frozenset({"RealmDisabled", "NotConnected"})
_ID = re.compile(r"[0-9a-f]{32}")
_REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# Native's reasons: [a-z0-9_], with ':' before an owner or a guid (busy:training_owner,
# level:<guid>). The column holds [a-z0-9_] only, so a ':' is stored as '_' (busy_training_owner).
_REASON = re.compile(r"[a-z0-9_:]{1,64}")
_U32 = 0xFFFFFFFF
_U64 = 0xFFFFFFFFFFFFFFFF
_MAX_MS = 253402300799999
_COLUMNS = ("command_id,owner,ref,kind,payload_sha256,guild,human,process_id,expects_job,state,"
            "reason,value,prepared_ms,dispatched_ms,settled_ms")


class DirectRefused(RuntimeError):
    """The final barrier refused a send; the row, when there is one, already says why."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classified(cmd):
    """The command's kind and payload hash, or None when it is no direct command."""
    try:
        classified = classify(cmd)
    except (ValueError, TypeError, AttributeError):
        return None
    if classified.kind in NOT_DIRECT or type(cmd.id) is not str or not _ID.fullmatch(cmd.id):
        return None
    return classified.kind, _sha256(classified.payload)


def _reason(text, fallback):
    return text.replace(":", "_") if type(text) is str and _REASON.fullmatch(text) else fallback


def busy(reason) -> bool:
    """Native's busy:<owner> refusal, as the ledger stores it: refused before anything moved."""
    return type(reason) is str and reason.startswith(("busy_", "busy:"))


class DirectLedger:
    def __init__(self, store: Store, *, process_id: str, clock):
        if not isinstance(store, Store):
            raise ValueError("the direct ledger needs the mind's Store")
        if type(process_id) is not str or not 1 <= len(process_id.encode("utf-8")) <= 256:
            raise ValueError("invalid process_id")
        self._store = store
        self._process_id = process_id
        self._clock = clock
        self._pruned = None

    @property
    def store(self):
        return self._store

    def _own_unit(self):
        if self._store.in_unit:
            raise RuntimeError("a direct command transition commits its own Store unit")

    def _now_ms(self) -> int:
        return min(max(int(self._clock.now() * 1000), 1), _MAX_MS)

    def _live(self, context) -> bool:
        return (type(context) is ActionContext and context.enabled is True and context.connected is True
                and context.presence_known is True and context.process_id == self._process_id
                and context.connection_epoch > 0 and 0 < context.guild <= _U32 and 0 < context.human <= _U32)

    def _row(self, cmd):
        """The stored row of exactly this command: its id, kind and complete payload."""
        classified = _classified(cmd)
        if classified is None:
            return None
        row = self._store.conn.execute(
            f"SELECT {_COLUMNS} FROM realm_direct WHERE command_id=?", (cmd.id,)).fetchone()
        if row is None or (row["kind"], row["payload_sha256"]) != classified:
            return None
        return row

    def _move(self, row, state, *, reason=None, value=None, dispatch=False, settle=True):
        """One transition from the row's current state. Times never run backwards in a row."""
        now = max(self._now_ms(), row["prepared_ms"], row["dispatched_ms"] or 0)
        dispatched = now if dispatch else row["dispatched_ms"]
        settled = now if settle else None
        changed = self._store.conn.execute(
            "UPDATE realm_direct SET state=?,reason=?,value=?,dispatched_ms=?,settled_ms=? "
            "WHERE command_id=? AND state=? AND settled_ms IS ?",
            (state, reason, value, dispatched, settled, row["command_id"], row["state"],
             row["settled_ms"])).rowcount
        if changed != 1:
            raise RuntimeError("direct command row changed during its transition")

    # -- the intent ---------------------------------------------------------------------------

    def prepare(self, cmd, *, owner, ref, context, expects_job) -> str | None:
        """Record the one live intent of (owner, ref) for exactly this command; its id.

        The command keeps its own id, 32 lower-case hex digits. None when that ref already has
        a live intent (prepared, dispatched, acked or unknown) or the realm is not live for this
        process: nothing will be sent. Inside the caller's Store unit it commits with that
        unit; outside one it commits by itself.
        """
        if type(owner) is not str or owner not in OWNERS:
            raise ValueError("invalid direct owner")
        if type(ref) is not str or not _REF.fullmatch(ref):
            raise ValueError("invalid direct ref")
        if type(expects_job) is not bool:
            raise ValueError("expects_job must be bool")
        classified = _classified(cmd)
        if classified is None:
            raise ValueError("not a direct command")
        if not self._live(context):
            return None
        kind, payload_sha256 = classified
        conn = self._store.conn
        with self._store.transaction():
            if conn.execute("SELECT 1 FROM realm_direct WHERE command_id=?", (cmd.id,)).fetchone():
                raise ValueError("direct command identity already used")
            if conn.execute(f"SELECT 1 FROM realm_direct WHERE owner=? AND ref=? AND state IN "
                            f"({','.join('?' for _ in LIVE)})", (owner, ref, *LIVE)).fetchone():
                return None
            try:
                with self._store.transaction():
                    conn.execute(
                        "INSERT INTO realm_direct(command_id,owner,ref,kind,payload_sha256,guild,human,"
                        "process_id,expects_job,state,prepared_ms) VALUES (?,?,?,?,?,?,?,?,?,'prepared',?)",
                        (cmd.id, owner, ref, kind, payload_sha256, context.guild, context.human,
                         self._process_id, int(expects_job), self._now_ms()))
            except sqlite3.IntegrityError:
                return None
        return cmd.id

    def owns_command(self, cmd) -> bool:
        with self._store.transaction():
            return self._row(cmd) is not None

    def state(self, owner, ref) -> tuple[str, str | None, int | None] | None:
        """(state, reason, value) of the ref's live intent, else of its newest one; None when
        it never had one. value is the job's, when one came in."""
        with self._store.transaction():
            row = self._store.conn.execute(
                "SELECT state,reason,value FROM realm_direct WHERE owner=? AND ref=? "
                f"ORDER BY state IN ({','.join('?' for _ in LIVE)}) DESC, rowid DESC LIMIT 1",
                (owner, ref, *LIVE)).fetchone()
        if row is None:
            return None
        return row["state"], row["reason"], None if row["value"] is None else int(row["value"])

    # -- the wire -----------------------------------------------------------------------------

    def before_send(self, cmd, *, context) -> None:
        """The synchronous final barrier before transport; never await here.

        It commits 'dispatched' for the one prepared row of exactly this command, while the
        realm is live for this process and the row's guild and human. Otherwise it settles that
        row, when there is one, and raises: a refusal is never tried again.
        """
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None:
                raise DirectRefused("not_owned")
            if row["state"] != "prepared":
                raise DirectRefused("settled")
            if (self._live(context) and row["process_id"] == self._process_id
                    and (context.guild, context.human) == (row["guild"], row["human"])):
                self._move(row, "dispatched", dispatch=True, settle=False)
                return
            self._move(row, "abandoned", reason="context_unavailable")
        raise DirectRefused("context_unavailable")

    def on_ack(self, cmd, ack) -> bool:
        """Settle a dispatched intent from its native ack, also while disabled; True if it did."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if (row is None or row["state"] != "dispatched" or type(ack) is not Ack
                    or ack.id != cmd.id or type(ack.ok) is not bool or type(ack.error) is not str):
                return False
            if not ack.ok:
                self._move(row, "refused", reason=_reason(ack.error, "native_refused"))
            else:
                self._move(row, "acked", settle=not row["expects_job"])
        return True

    def on_job(self, ev) -> bool:
        """Settle an intent that expects a job from its result, before or after the ack."""
        self._own_unit()
        if type(ev) is not RealmJob or ev.pvp_owner != "":
            return False
        with self._store.transaction():
            row = self._store.conn.execute(
                f"SELECT {_COLUMNS} FROM realm_direct WHERE command_id=?", (ev.id,)).fetchone()
            if (row is None or row["kind"] != ev.kind or row["expects_job"] != 1
                    or not (row["state"] == "dispatched"
                            or (row["state"] == "acked" and row["settled_ms"] is None))):
                return False
            if ev.state == "done" and type(ev.value) is int and 0 <= ev.value <= _U64:
                self._move(row, "acked", value=str(ev.value))
            else:
                self._move(row, "refused", reason=_reason(ev.reason, "job_failed"))
        return True

    def on_send_error(self, cmd, error_type) -> bool:
        """A failed transport is final: refused when nothing was written, otherwise unknown."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None or row["state"] != "dispatched":
                return False
            if error_type in _PRETRANSPORT:
                self._move(row, "refused", reason="pretransport_refused")
            else:
                self._move(row, "unknown", reason="transport_unknown")
        return True

    def abandon_unsent(self, cmd) -> bool:
        """Close an intent whose send never reached the barrier (cancelled, disabled, failed)."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None or row["state"] != "prepared":
                return False
            self._move(row, "abandoned", reason="unsent")
        return True

    # -- restart and retention ----------------------------------------------------------------

    def recover(self, *, process_id) -> tuple[int, int]:
        """At start, before anything can send: nothing an earlier process began is sent again.

        A prepared intent never reached the wire, so it is abandoned and its ref is free. A
        dispatched one may have reached native, so it is unknown and its ref stays taken. An
        acked one still waiting for its job keeps waiting: the job may yet come. Returns how
        many were abandoned and how many became unknown.
        """
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            conn = self._store.conn
            abandoned = conn.execute(
                "UPDATE realm_direct SET state='abandoned',reason='recovered_prepared',"
                "settled_ms=max(?,prepared_ms) WHERE state='prepared' AND process_id<>?",
                (now, process_id)).rowcount
            unknown = conn.execute(
                "UPDATE realm_direct SET state='unknown',reason='recovered_dispatched',"
                "settled_ms=max(?,dispatched_ms) WHERE state='dispatched' AND process_id<>?",
                (now, process_id)).rowcount
        if abandoned or unknown:
            log.info("realm_direct_recovered: abandoned=%d unknown=%d", abandoned, unknown)
        return abandoned, unknown

    def prune(self, keep=512) -> int:
        """Drop all but the newest `keep` refused and abandoned rows; their refs are free
        already. A calendar post's stay RETAIN_POST_REFUSALS_S after they settled whatever their
        count. A live row (prepared, dispatched, acked, unknown) is never removed: it is what
        keeps its ref from being sent twice. Returns how many were removed."""
        if type(keep) is not int or not 0 <= keep <= 65536:
            raise ValueError("invalid keep")
        self._own_unit()
        recent = self._now_ms() - RETAIN_POST_REFUSALS_S * 1000
        with self._store.transaction():
            removed = self._store.conn.execute(
                "DELETE FROM realm_direct WHERE state IN ('refused','abandoned') "
                "AND NOT (kind='calendar_post' AND coalesce(settled_ms,prepared_ms)>=?) AND rowid NOT IN "
                "(SELECT rowid FROM realm_direct WHERE state IN ('refused','abandoned') "
                "ORDER BY rowid DESC LIMIT ?)", (recent, keep)).rowcount
        if removed:
            log.debug("realm_direct_pruned: rows=%d", removed)
        return removed

    def prune_acked(self) -> int:
        """Drop the acked, settled rows of RETAIN_ACKED's refs that settled more than
        RETAIN_ACKED_S ago; returns how many were removed. An acked row still waiting for its
        job, and every unknown one, stays."""
        self._own_unit()
        before = self._now_ms() - RETAIN_ACKED_S * 1000
        removed = 0
        with self._store.transaction():
            for owner, prefix in RETAIN_ACKED:
                removed += self._store.conn.execute(
                    "DELETE FROM realm_direct WHERE owner=? AND state='acked' AND settled_ms IS NOT NULL "
                    "AND settled_ms<? AND substr(ref,1,?)=?", (owner, before, len(prefix), prefix)).rowcount
        if removed:
            log.debug("realm_direct_pruned_acked: rows=%d", removed)
        return removed

    # -- as a realm part ----------------------------------------------------------------------

    async def tick(self):
        """Prune once an hour; nothing else is periodic."""
        now = self._clock.now()
        if self._pruned is None or now - self._pruned >= PRUNE_EVERY_S:
            self._pruned = now
            self.prune()
            self.prune_acked()

    async def on_event(self, ev):
        """Receipts reach the ledger through the service's own routing, never as a part."""


def attach(realm) -> DirectLedger:
    """The realm's direct ledger, as a part: bound to its store, process and clock.

    No I/O: the service recovers it once (recover(process_id=...)) before anything can send,
    and routes on_send, on_ack, on_job and on_send_error of the commands it owns to it.
    """
    return DirectLedger(realm.app.store, process_id=realm._process_id, clock=realm.app.clock)
