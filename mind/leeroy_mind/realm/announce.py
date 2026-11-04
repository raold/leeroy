"""The guild's own kill announcements: at most one server message per slot, never retried.

A slot is (guild, map, boss, difficulty, product week). The first kill of a boss mode in a
week reads first="week". When the guild has no other stored kill of that boss in an
overlapping mode at all, and the mode is not canon, it reads first="ever" instead. A kill that
qualifies for both is announced once, as "ever": the first kill ever is also the first of its
week, one kill gets one send, and the "ever" row takes that week's slot as well.

Suppression is order independent and errs towards silence. Any other stored kill of the boss
in an overlapping mode suppresses: a native realm_kill, public or private, or a core Boss
"killed" row from another kill. Difficulty -1 (unknown) overlaps every mode and is never
"ever"; the core Boss row names no mode, so it overlaps every one. Canon is matched on its
own mode, as the plan's (map, boss, difficulty) key says.

The plan is written inside the kill's own Store unit and is authority to send only once that
unit commits. before_send is the synchronous final barrier: it commits 'dispatched' before
transport, or settles the row and refuses. Every outcome is final. The native side still
requires its own recent kill proof before it sends anything. Attempts are always 0 for now
(native refuses others); _attempts is the one place a native count will be read.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import logging
import sqlite3

from leeroy_mind.protocol import Ack
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import BOSS_NAMES, is_raid_fight

from .actions import ActionContext
from .calendar_time import ProductCalendarUnavailable, product_period
from .protocol import Announce, RealmKill
from .records import FactRow, RealmObservation

log = logging.getLogger(__name__)

MAX_OUTSTANDING = 4
WINDOW_S = 30
# Send errors raised before the bridge writes anything: the message cannot have gone out.
_PRETRANSPORT = frozenset({"RealmDisabled", "NotConnected"})
_OVERLAP = "(? = -1 OR difficulty = -1 OR difficulty = ?)"
_COLUMNS = ("command_id,kill_id,guild,human,map,boss_id,difficulty,cadence,period_key,first,"
            "payload_json,payload_sha256,process_id,generation,connection_epoch,prepared_at,"
            "expires_at,dispatched_at,settled_at,state,safe_reason")


class AnnouncementRefused(RuntimeError):
    """The final barrier refused a send; the row, when there is one, already says why."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class AnnouncementPlan:
    command_id: str
    kill_id: str
    native_epoch: int
    guild: int
    human: int
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    heroic: bool
    boss_name: str
    attempts: int
    fight_s: int
    first: str
    period_key: str
    expires_at: float


def _payload(cmd: Announce) -> str:
    return json.dumps({f.name: getattr(cmd, f.name) for f in fields(Announce)}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _command_id(kill_id: str) -> str:
    return "announce_" + hashlib.sha256(b"realm-announce-v1\0" + kill_id.encode("ascii")).hexdigest()[:40]


def _fight_s(event: RealmKill) -> int:
    """Whole seconds, as native checks them. RealmKill measures a fight (fight_ms >= 0) only
    with the human present, so an offline raid's kill has no time (M27)."""
    return max(event.fight_ms, 0) // 1000


def _attempts(event: RealmKill) -> int:
    """The attempts this kill took; 0 leaves them out of the message.

    Native refuses any other value until its attempt counter exists. The counter will arrive
    with the kill evidence, and this is the one place that reads it into the plan.
    """
    return 0


class Announcer:
    def __init__(self, store: Store, *, process_id: str, clock):
        if not isinstance(store, Store):
            raise ValueError("the announcer needs the mind's Store")
        self._store = store
        self._process_id = process_id
        self._clock = clock

    @property
    def store(self):
        return self._store

    def _require_unit(self):
        if not self._store.in_unit:
            raise RuntimeError("the announcer requires an active Store unit")

    def _own_unit(self):
        if self._store.in_unit:
            raise RuntimeError("an announcement transition commits its own Store unit")

    # -- the plan, inside the kill's unit ---------------------------------------------------

    def prepare_in_unit(self, observation, fact) -> AnnouncementPlan | None:
        """The kill's one announcement decision, written in the kill's own unit.

        Only a public guild kill of a catalogued raid boss is announced, and only while its
        30 s window is open and fewer than MAX_OUTSTANDING others may still be sent. None
        means nothing will ever be sent for this kill.
        """
        self._require_unit()
        if (type(observation) is not RealmObservation or type(observation.event) is not RealmKill
                or type(fact) is not FactRow or fact.public is not True):
            return None
        event, context = observation.event, observation.context
        if (type(context) is not ActionContext or context.enabled is not True
                or context.process_id != self._process_id or context.connection_epoch < 1
                or (context.guild, context.human) != (event.guild_id, event.human_guid)):
            return None
        conn = self._store.conn
        kill = conn.execute("SELECT fact FROM realm_kill WHERE kill_id=?", (event.kill_id,)).fetchone()
        if kill is None or kill["fact"] != fact.id:
            return None  # A private kill is never announced.
        if conn.execute("SELECT 1 FROM realm_announcement WHERE kill_id=?", (event.kill_id,)).fetchone():
            return None  # This kill's one decision is already made.
        if (event.map, event.boss_id) not in BOSS_NAMES or not is_raid_fight(event.map, event.boss_id):
            return None
        killed_at = event.killed_at_ms / 1000
        try:
            period = product_period(killed_at)
        except (ProductCalendarUnavailable, ValueError):
            log.warning("realm_announcement_unavailable: product calendar")
            return None
        first = self._first_in_unit(event, period)
        if first is None:
            return None
        now = self._clock.now()
        plan = AnnouncementPlan(_command_id(event.kill_id), event.kill_id, event.native_epoch,
            event.guild_id, event.human_guid, event.map, event.instance_id, event.boss_id,
            event.difficulty, event.size, event.heroic, BOSS_NAMES[(event.map, event.boss_id)],
            _attempts(event), _fight_s(event), first, period.key, killed_at + WINDOW_S)
        skipped = ("expired" if now >= plan.expires_at
                   else "clock_conflict" if killed_at > now + WINDOW_S
                   else "capacity" if self._outstanding_in_unit(now) >= MAX_OUTSTANDING else None)
        if skipped is not None:
            # No row: the stored kill itself already suppresses every later one of its slot.
            log.info("realm_announcement_skipped: reason=%s", skipped)
            return None
        payload = _payload(self.command(plan))
        values = (plan.command_id, plan.kill_id, plan.guild, plan.human, plan.map, plan.boss_id,
                  plan.difficulty, period.cadence, period.key, first, payload, _sha256(payload),
                  self._process_id, context.generation, str(context.connection_epoch), now,
                  plan.expires_at, None, None, "prepared", "none")
        try:
            # A savepoint: a slot conflict undoes only this row, never the kill's own fact.
            with self._store.transaction():
                conn.execute(f"INSERT INTO realm_announcement({_COLUMNS}) "
                             f"VALUES ({','.join('?' for _ in values)})", values)
        except sqlite3.IntegrityError:
            log.warning("realm_announcement_slot_conflict")
            return None
        return plan

    def _first_in_unit(self, event: RealmKill, period) -> str | None:
        """'ever', 'week' or None. Every slot row belongs to a stored kill of its own mode and
        week, so the stored kills alone decide; the table's unique keys back them."""
        start_ms, end_ms = int(period.starts_at * 1000), int(period.ends_at * 1000)
        if self._other_kill(event, start_ms, end_ms) or self._core_kill(event, start_ms, end_ms, ever=False):
            return None
        if (event.difficulty == -1 or self._canon(event) or self._other_kill(event)
                or self._core_kill(event, start_ms, end_ms, ever=True)):
            return "week"
        return "ever"

    def _exists(self, sql, params) -> bool:
        return self._store.conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def _other_kill(self, event, start_ms=None, end_ms=None) -> bool:
        """Another stored native kill of the boss, public or private, in an overlapping mode."""
        sql = ("SELECT 1 FROM realm_kill WHERE guild=? AND map=? AND boss_id=? AND kill_id<>? AND "
               + _OVERLAP)
        params = [event.guild_id, event.map, event.boss_id, event.kill_id, event.difficulty, event.difficulty]
        if start_ms is not None:
            sql += " AND killed_ms>=? AND killed_ms<?"
            params += [start_ms, end_ms]
        return self._exists(sql, params)

    def _core_kill(self, event, start_ms, end_ms, *, ever) -> bool:
        """A core Boss "killed" row of the boss from another kill: another instance, or the
        same instance in another week (a reused id). It names no mode, so it overlaps all.
        The bridge only sends Boss rows for fights with a cast member or the human."""
        sql = ("SELECT 1 FROM game_event WHERE type='boss' AND json_valid(payload) "
               "AND json_extract(payload,'$.state')='killed' AND json_extract(payload,'$.map')=? "
               "AND json_extract(payload,'$.boss_id')=? AND NOT (json_extract(payload,'$.instance_id')=? "
               "AND json_extract(payload,'$.ts')>=? AND json_extract(payload,'$.ts')<?)")
        params = [event.map, event.boss_id, event.instance_id, start_ms, end_ms]
        if not ever:
            sql += " AND json_extract(payload,'$.ts')>=? AND json_extract(payload,'$.ts')<?"
            params += [start_ms, end_ms]
        return self._exists(sql, params)

    def _canon(self, event) -> bool:
        """The guild's canon kills are its own history: never announced as a first (I14)."""
        return self._exists("SELECT 1 FROM realm_canon_kill WHERE map=? AND boss_id=? AND " + _OVERLAP,
                            (event.map, event.boss_id, event.difficulty, event.difficulty))

    def _outstanding_in_unit(self, now) -> int:
        """Rows that may still reach the wire: dispatched, or prepared and not yet expired."""
        return self._store.conn.execute(
            "SELECT count(*) FROM realm_announcement WHERE state='dispatched' "
            "OR (state='prepared' AND expires_at>?)", (now,)).fetchone()[0]

    # -- the command and its transitions ----------------------------------------------------

    @staticmethod
    def command(plan: AnnouncementPlan) -> Announce:
        return Announce(plan.command_id, plan.kill_id, plan.native_epoch, plan.guild, plan.human,
                        plan.map, plan.instance_id, plan.boss_id, plan.difficulty, plan.size,
                        plan.heroic, plan.boss_name, attempts=plan.attempts, fight_s=plan.fight_s,
                        first=plan.first)

    def _row(self, cmd):
        """The stored row of exactly this command: same id and the same complete payload."""
        if type(cmd) is not Announce:
            return None
        try:
            payload = _payload(cmd)
        except (TypeError, ValueError):
            return None
        return self._store.conn.execute(
            f"SELECT {_COLUMNS} FROM realm_announcement WHERE command_id=? AND payload_json=? "
            "AND payload_sha256=?", (cmd.id, payload, _sha256(payload))).fetchone()

    def _move(self, row, state, reason, now):
        """One transition from the row's current state; a transport time only for dispatch."""
        column = "dispatched_at" if state == "dispatched" else "settled_at"
        changed = self._store.conn.execute(
            f"UPDATE realm_announcement SET state=?,safe_reason=?,{column}=? "
            "WHERE command_id=? AND state=?",
            (state, reason, now, row["command_id"], row["state"])).rowcount
        if changed != 1:
            raise RuntimeError("announcement row changed during its transition")

    def owns_command(self, cmd) -> bool:
        with self._store.transaction():
            return self._row(cmd) is not None

    def _current(self, row, context) -> bool:
        return (type(context) is ActionContext and context.enabled is True
                and context.connected is True and context.presence_known is True
                and context.process_id == row["process_id"] == self._process_id
                and context.generation == row["generation"]
                and str(context.connection_epoch) == row["connection_epoch"]
                and (context.guild, context.human) == (row["guild"], row["human"]))

    def before_send(self, cmd, *, context) -> None:
        """The synchronous final barrier before transport; never await here.

        It commits 'dispatched' for the one prepared row of exactly this command. Otherwise it
        settles that row, when there is one, and raises: a refusal is never tried again.
        """
        self._own_unit()
        now = self._clock.now()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None:
                raise AnnouncementRefused("not_owned")
            if row["state"] != "prepared":
                raise AnnouncementRefused("settled")
            state, reason = "dispatched", "none"
            if not self._current(row, context):
                state, reason = "abandoned", "context_unavailable"
            elif now >= row["expires_at"]:
                state, reason = "expired", "expired"
            self._move(row, state, reason, now)
        if state != "dispatched":
            raise AnnouncementRefused(reason)

    def on_ack(self, cmd, ack, *, now) -> bool:
        """Settle a dispatched row from its native ack, also while disabled; True if it did."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if (row is None or row["state"] != "dispatched" or type(ack) is not Ack
                    or ack.id != cmd.id or type(ack.ok) is not bool or type(ack.error) is not str):
                return False
            self._move(row, "sent" if ack.ok else "refused", "none" if ack.ok else "native_refused", now)
        return True

    def on_send_error(self, cmd, error_type, *, now) -> bool:
        """A failed transport is final: refused when nothing was written, otherwise unknown."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None or row["state"] != "dispatched":
                return False
            if error_type in _PRETRANSPORT:
                self._move(row, "refused", "pretransport_refused", now)
            else:
                self._move(row, "unknown", "transport_unknown", now)
        return True

    def abandon_unsent(self, cmd, *, now) -> bool:
        """Close a row whose send never reached the barrier (cancelled, disabled, failed)."""
        self._own_unit()
        with self._store.transaction():
            row = self._row(cmd)
            if row is None or row["state"] != "prepared":
                return False
            if now >= row["expires_at"]:
                self._move(row, "expired", "expired", now)
            else:
                self._move(row, "abandoned", "context_unavailable", now)
        return True

    def recover(self, *, process_id, now) -> tuple[int, int]:
        """At start, before any send: an earlier process's rows are never sent or retried.

        A prepared row is abandoned. A dispatched one may have reached the wire, so it is
        unknown. Returns how many of each.
        """
        self._own_unit()
        with self._store.transaction():
            conn = self._store.conn
            abandoned = conn.execute(
                "UPDATE realm_announcement SET state='abandoned',safe_reason='recovered_prepared',"
                "settled_at=? WHERE state='prepared' AND process_id<>?", (now, process_id)).rowcount
            unknown = conn.execute(
                "UPDATE realm_announcement SET state='unknown',safe_reason='recovered_dispatched',"
                "settled_at=? WHERE state='dispatched' AND process_id<>?", (now, process_id)).rowcount
        if abandoned or unknown:
            log.info("realm_announcement_recovered: abandoned=%d unknown=%d", abandoned, unknown)
        return abandoned, unknown
