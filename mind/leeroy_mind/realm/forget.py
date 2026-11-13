"""Operator abandonment receipts release eligible retained native reservations.

This sends a fresh envelope targeting an old root, never the original command.
The original action journal is read-only here. A timeout waits for reconnection;
only an explicit busy:teleport answer permits another attempt on the next tick.
"""
import asyncio
from dataclasses import dataclass
import json
import logging
import uuid

from .action_store import ABANDONED, ABANDON_RECEIPT, SKIPPING_KINDS
from .protocol import RealmForget, RealmForgotten

log = logging.getLogger(__name__)
FORGOTTEN_RECEIPT = "realm:action:forgotten:"


@dataclass
class _Attempt:
    command: RealmForget
    ticket: tuple
    ack: object = None
    result: object = None


class ForgetReceipts:
    def __init__(self, store, send, context, clock):
        self.store, self.send, self.context, self.clock = store, send, context, clock
        self.ticket = None
        self.attempted = set()
        self.pending = {}
        self.tasks = set()
        self.closed = False

    def _ticket(self):
        c = self.context()
        return (c.connection_epoch, c.guild, c.human) if c.connected and c.presence_known else None

    def _released(self, root_id):
        """A present operator receipt, matching the still-abandoned original root."""
        conn = self.store.conn
        row = conn.execute("SELECT value FROM meta WHERE key=?", (ABANDON_RECEIPT + root_id,)).fetchone()
        if row is None or conn.execute("SELECT 1 FROM meta WHERE key=?", (FORGOTTEN_RECEIPT + root_id,)).fetchone():
            return False
        r = self.store.get(root_id)
        if (r is None or r.role != "root" or r.parent_id is not None or r.policy != "ordinary"
                or r.kind not in SKIPPING_KINDS or r.state != "completed" or r.result_reason != ABANDONED
                or r.transport_state != "settled" or (r.guild, r.human) != self.ticket[1:]
                or conn.execute("SELECT 1 FROM realm_action WHERE parent_id=?", (root_id,)).fetchone()):
            return False
        try:
            receipt = json.loads(row[0]); evidence = json.loads(r.evidence)
            return (type(receipt["seq"]) is int and receipt["seq"] == r.seq and receipt["kind"] == r.kind and receipt["policy"] == r.policy
                    and receipt["members"] == list(r.reserved_manifest) and receipt["at"] == evidence["abandoned"]
                    and not evidence.get("conflict"))
        except (ValueError, KeyError, TypeError):
            return False

    def tick(self):
        ticket = self._ticket()
        if self.closed or ticket is None:
            return
        if ticket != self.ticket:
            self.ticket = ticket
            self.attempted.clear()
            self.pending.clear()
        # Bound discovery and in-flight work. A refusal stays attempted until a
        # new connection; settled receipts are excluded by SQL on the next tick.
        rows = self.store.conn.execute(
            "SELECT a.command_id FROM realm_action a JOIN meta m ON m.key=? || a.command_id "
            "LEFT JOIN meta f ON f.key=? || a.command_id WHERE f.key IS NULL "
            "AND a.role='root' AND a.policy='ordinary' AND a.kind IN ('leave_pvp','rearm') "
            "AND a.state='completed' AND a.result_reason='abandoned' AND a.guild=? AND a.human=? "
            "ORDER BY a.seq LIMIT 128", (ABANDON_RECEIPT, FORGOTTEN_RECEIPT, *ticket[1:])).fetchall()
        for (root_id,) in rows:
            if len(self.tasks) >= 8:
                break
            if root_id in self.attempted or not self._released(root_id):
                continue
            attempt = _Attempt(RealmForget(uuid.uuid4().hex, root_id), ticket)
            self.attempted.add(root_id)
            self.pending[attempt.command.id] = attempt
            task = asyncio.create_task(self._send(attempt))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def _current(self, attempt):
        return (not self.closed and self._ticket() == attempt.ticket == self.ticket
                and self.pending.get(attempt.command.id) is attempt)

    async def _send(self, attempt):
        cmd = attempt.command
        try:
            if not self._current(attempt) or not self._released(cmd.origin_id):
                return
            # No await before BridgeClient's writer entry. This command owns its
            # envelope ID; neither ACK nor result is routed to the action queue.
            ack = await self.send(cmd, preserve_id=True)
            if not self._current(attempt) or ack.id != cmd.id:
                return
            attempt.ack = ack
            if attempt.result is not None and ((ack.ok and attempt.result.outcome not in {"released", "not_held"})
                    or (not ack.ok and attempt.result.outcome != ack.error)):
                return
            if not ack.ok and ack.error == "unknown type: realm_forget":
                self._settle(attempt, "unsupported", 0)
            elif not ack.ok and ack.error in {"running", "kind", "busy:teleport"}:
                self._answer(attempt, ack.error, 0)
            elif ack.ok and attempt.result is not None:
                self._answer(attempt, attempt.result.outcome, attempt.result.members)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("realm_forget_transport id=%s error=%s", cmd.origin_id, type(error).__name__)

    def observe(self, event):
        if type(event) is not RealmForgotten:
            return
        attempt = self.pending.get(event.id)
        if attempt is None or not self._current(attempt) or event.origin_id != attempt.command.origin_id:
            return
        if attempt.result is not None and attempt.result != event:
            return  # No contradictory answer grants authority.
        attempt.result = event
        if attempt.ack is not None and attempt.ack.ok and event.outcome in {"released", "not_held"}:
            self._answer(attempt, event.outcome, event.members)

    def _answer(self, attempt, outcome, members):
        if outcome in {"released", "not_held"}:
            self._settle(attempt, outcome, members)
        else:
            log.warning("realm_forget id=%s outcome=%s members=0", attempt.command.origin_id, outcome)
            self.pending.pop(attempt.command.id, None)
            if outcome == "busy:teleport":
                self.attempted.discard(attempt.command.origin_id)

    def _settle(self, attempt, outcome, members):
        if not self._current(attempt) or not self._released(attempt.command.origin_id):
            return
        with self.store.conn:
            self.store.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (
                FORGOTTEN_RECEIPT + attempt.command.origin_id,
                json.dumps({"outcome": outcome, "members": members, "at": self.clock.now()}, sort_keys=True)))
        self.pending.pop(attempt.command.id, None)
        log.info("realm_forget id=%s outcome=%s members=%d", attempt.command.origin_id, outcome, members)

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
