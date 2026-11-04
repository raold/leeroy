"""The raid lead's question after his kick, his answer and the one re-admission it allows.

He kicks a cast member himself: the native guard's his_kick. With the Realm enabled, that
event's own Store unit retains the episode, bound to its exact game_event row, and the lead
may ask for 72 h from the kick. The standing raid lead is the one cast tank whose title reads
"raid lead", never him, the kicked member or a former member; with none or several nobody
asks. That lead whispers him once, typed and unstyled (the text exactly as reserved, no typos
or emoticon), through say_as. Two TurnMeta hooks make the send exact: before_transport
commits dispatched_ms immediately before the bridge writes the line, and on_recorded binds
the stored line to its ack and to the reserved text's hash. The service's barrier first
requires, synchronously, the live model's latch (app._send would refuse the say) and his
presence (native would refuse a whisper to him offline); nothing awaits between that and the
write, so a line held back there never reached the wire. A question that never reached that
barrier, or was refused by it, is refused and may be asked again after RETRY_MS. One that
passed it, without a recorded ack, is unknown: never answered, asked again or replaced until
every window it had has passed.

His answer is his own live whisper to that lead, exactly "yes" or "no" once stripped and
casefolded, within 72 h of delivery, bound to its chat_line row, and with no other whisper
between the lead and him (either way) stored after the question and before it: his yes could
answer that line instead. Such an answer is refused and consumes nothing; the question stays
open, unanswerable, until its window passes. A spoken (push-to-talk) yes never counts: the
voice service's transcript is injected into the dispatcher without the native wire origin an
answer is bound to, and is ignored without a word; in a private room with the lead it is also
stored as a whisper between them, so it unties any typed answer after it. A yes asks native for one
guild_readmit bound to the kick's own ts and rank (an officer, rank 1, comes back at 2; a
rank never learned, 0, sends nothing). Native keeps its kick receipts in memory only, so after
a worldserver restart it refuses (not_kicked); on any refusal the lead tells him plainly to
invite the member himself. No, silence or any doubt: nothing is ever sent.

Every transition is one synchronous Store unit, with no await inside. Recovery runs at
start, before anything can send: a reserved question becomes unknown, a prepared admission is
refused and a dispatched one unknown. Neither is ever sent again.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import logging
import re
import uuid

from leeroy_mind.ingress import RowReceipt, WireOrigin, chat_row_binding, game_row_binding
from leeroy_mind.protocol import Ack, Chat
from leeroy_mind.storage.store import Store

from .actions import ActionContext
from .ledger import former_members_in_unit
from .ports import _title_matches
from .protocol import Guard as GuardEvent, GuildReadmit
from .records import QuestionIntent

log = logging.getLogger(__name__)

WINDOW_MS = 72 * 3600 * 1000  # The schema's: to ask after the kick, and to answer after delivery.
RETRY_MS = 5 * 60 * 1000  # A question refused before transport may be asked again after this.
LEAD_TITLE = "raid lead"
QUESTION = "saw {name} got kicked, want them back? (yes/no)"
FALLBACK = "couldn't get {name} back in myself, you'll have to invite them"
# The fallback is his answer's reply, so ordinary speech guards (the lead's own reply to his
# yes, a busy guild) can hold it back: it is tried a few times, and once only on the wire.
FALLBACK_TRIES = 3
FALLBACK_RETRY_S = 20
ANSWERS = ("yes", "no")
# Send errors raised before the bridge writes anything: the command cannot have gone out.
_PRETRANSPORT = frozenset({"RealmDisabled", "NotConnected"})
# Native refusals after an add it could not prove either way: it may have happened.
_UNPROVED = frozenset({"guild_join_unproved"})
_REASON = re.compile(r"[a-z0-9_]{1,64}")
_NAME = re.compile(r"[^\W\d_]{2,12}")
_MAX_MS = 253402300799999
_U32 = 0xFFFFFFFF
_CHAT_KEYS = frozenset({"v", "type", *(f.name for f in fields(Chat))})
_CHAT_REQUIRED = frozenset({"v", "type", "ts", "channel", "from_guid", "to_guid", "text"})
_COLUMNS = ("episode_id,source,guid,guild,human,kick_ms,old_rank,lead,ask_id,ask_line,ask_text_sha256,"
            "ask_state,ask_expires_ms,attempted_ms,dispatched_ms,delivered_ms,reply_expires_ms,answer,"
            "consent_state,admission_command_id,admission_state,admission_reason,"
            "admission_dispatched_ms")


class GuardRefused(RuntimeError):
    """The final barrier refused a send; the row, when there is one, already says why."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Admission:
    """His yes, committed: the one readmission it allows, or why none is sent (refused)."""
    episode_id: str
    command_id: str
    guid: int
    kick_ms: int
    rank: int  # The readmission rank; 0 when the kick's rank was never learned.
    lead: int
    human: int
    name: str
    state: str  # 'prepared' or 'refused'


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _episode_id(source_key: str) -> str:
    return "kick:" + hashlib.sha256(b"realm-kick-episode-v1\0" + source_key.encode("ascii")).hexdigest()


def _command_id(episode_id: str) -> str:
    return "guild_readmit_" + hashlib.sha256(
        b"realm-readmit-v1\0" + episode_id.encode("ascii")).hexdigest()[:40]


def readmit_rank(old_rank: int) -> int:
    """Native's ReadmitRank: the kick's rank, 2 for an officer (1), 0 (none) when unknown."""
    if type(old_rank) is not int or old_rank < 1 or old_rank > 9:
        return 0
    return max(old_rank, 2)


def _u32(value) -> bool:
    return type(value) is int and 0 < value <= _U32


def _ms(value) -> bool:
    return type(value) is int and 0 < value <= _MAX_MS


def _guard_payload(ev) -> dict:
    return {f.name: getattr(ev, f.name) for f in fields(GuardEvent)}


def _bound_kick(conn, receipt, ev) -> bool:
    """The receipt is the dispatcher's of exactly this event's stored game_event row."""
    if type(receipt) is not RowReceipt or receipt.row_kind != "game_event" or type(receipt.wire) is not WireOrigin:
        return False
    payload, wire = _guard_payload(ev), receipt.wire
    if (wire.event_type != "guard" or wire.protocol_version != 1 or wire.server_ms != ev.ts
            or wire.top_keys != tuple(sorted({"v", "type", *payload}))):
        return False
    row = conn.execute("SELECT id,type,payload FROM game_event WHERE id=?", (receipt.row_id,)).fetchone()
    if row is None or row["type"] != "guard":
        return False
    try:
        stored = json.loads(row["payload"])
    except (TypeError, ValueError):
        return False
    return (type(stored) is dict and stored == payload
            and game_row_binding(row["id"], "guard", stored) == receipt.row_sha256)


def _bound_answer(conn, receipt, ev) -> bool:
    """The receipt is the dispatcher's of exactly this live whisper's stored chat_line row."""
    if (type(receipt) is not RowReceipt or receipt.row_kind != "chat_line" or receipt.backlog is not False
            or type(receipt.wire) is not WireOrigin):
        return False
    wire, keys = receipt.wire, set(receipt.wire.top_keys)
    if (wire.event_type != "chat" or wire.protocol_version != 1 or wire.server_ms != ev.ts
            or not _CHAT_REQUIRED <= keys or not keys <= _CHAT_KEYS):
        return False
    row = conn.execute("SELECT id,channel,from_guid,from_name,to_guid,text,map,zone,origin FROM chat_line "
                       "WHERE id=?", (receipt.row_id,)).fetchone()
    if (row is None or row["origin"] != "human" or row["channel"] != "whisper"
            or (row["from_guid"], row["to_guid"]) != (ev.from_guid, ev.to_guid)
            or type(row["text"]) is not str or row["text"].strip().casefold() != ev.text.strip().casefold()):
        return False
    return chat_row_binding(row["id"], **{key: row[key] for key in (
        "channel", "from_guid", "from_name", "to_guid", "text", "map", "zone", "origin")}) == receipt.row_sha256


def _untied(conn, row, answer_line) -> bool:
    """His answer is not tied to the question: it was stored before the question's own line,
    or a whisper between the lead and him, either way, lies between the two. His yes or no
    may answer that line instead (an unrelated "u coming to icc tonight?"), so it answers
    nothing; the question stays open, and the same line keeps untying every later answer."""
    ask_line = row["ask_line"]
    if type(ask_line) is not int or type(answer_line) is not int or answer_line <= ask_line:
        return True
    lead, human = row["lead"], row["human"]
    return conn.execute(
        "SELECT 1 FROM chat_line WHERE id>? AND id<? AND channel='whisper' "
        "AND ((from_guid=? AND to_guid=?) OR (from_guid=? AND to_guid=?)) LIMIT 1",
        (ask_line, answer_line, lead, human, human, lead)).fetchone() is not None


def _insert_source(conn, receipt, event_type, context) -> int | None:
    """A new immutable realm_source for this exact row; None when its envelope already has one."""
    wire = receipt.wire
    if conn.execute("SELECT 1 FROM realm_source WHERE source_key=?", (wire.envelope_sha256,)).fetchone():
        return None
    row_column = "game_event_id" if receipt.row_kind == "game_event" else "chat_line_id"
    top = json.dumps(list(wire.top_keys), ensure_ascii=False, separators=(",", ":"))
    cursor = conn.execute(
        f"INSERT INTO realm_source(source_key,protocol_version,event_type,{row_column},row_sha256,"
        "server_ms,received_at,guild,human,top_keys_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (wire.envelope_sha256, wire.protocol_version, event_type, receipt.row_id, receipt.row_sha256,
         wire.server_ms, wire.received_at, context.guild, context.human, top))
    return cursor.lastrowid


class KickGuard:
    def __init__(self, store: Store, ports, *, process_id: str, clock):
        if not isinstance(store, Store):
            raise ValueError("the kick guard needs the mind's Store")
        self._store = store
        self._ports = ports
        self._process_id = process_id
        self._clock = clock

    @property
    def store(self):
        return self._store

    def _own_unit(self):
        if self._store.in_unit:
            raise RuntimeError("a kick guard transition commits its own Store unit")

    def _now_ms(self) -> int:
        return int(self._clock.now() * 1000)

    def _live(self, context) -> bool:
        return (type(context) is ActionContext and context.enabled is True and context.connected is True
                and context.presence_known is True and context.process_id == self._process_id
                and context.connection_epoch > 0 and _u32(context.guild) and _u32(context.human))

    def _episode(self, where, params):
        return self._store.conn.execute(
            f"SELECT {_COLUMNS} FROM realm_kick_episode WHERE {where}", params).fetchone()

    # -- the kick ----------------------------------------------------------------------------

    def retain(self, ev, receipt, context) -> str | None:
        """Keep his kick's episode, bound to its own source row; replays change nothing."""
        self._own_unit()
        if (type(ev) is not GuardEvent or ev.action != "his_kick" or not _u32(ev.guid) or not _ms(ev.ts)
                or type(ev.rank) is not int or not 0 <= ev.rank <= 255 or type(ev.reason) is not str
                or ev.ts + WINDOW_MS > _MAX_MS or not self._live(context) or ev.guid == context.human):
            return None
        if type(receipt) is not RowReceipt or type(receipt.wire) is not WireOrigin:
            return None
        with self._store.transaction():
            conn = self._store.conn
            known = conn.execute(
                "SELECT e.episode_id FROM realm_source s JOIN realm_kick_episode e ON e.source=s.id "
                "WHERE s.source_key=?", (receipt.wire.envelope_sha256,)).fetchone()
            if known is not None:
                return known[0]  # A replay of the same kick: its episode already stands.
            if not _bound_kick(conn, receipt, ev):
                return None
            source = _insert_source(conn, receipt, "guard", context)
            if source is None:
                return None
            episode = _episode_id(receipt.wire.envelope_sha256)
            conn.execute(
                "INSERT INTO realm_kick_episode(episode_id,source,guid,guild,human,kick_ms,old_rank,"
                "ask_state,ask_expires_ms,consent_state) VALUES (?,?,?,?,?,?,?,'unasked',?,'none')",
                (episode, source, ev.guid, context.guild, context.human, ev.ts, ev.rank, ev.ts + WINDOW_MS))
        log.info("realm_guard_kick_retained: guid=%d", ev.guid)
        return episode

    # -- the question ------------------------------------------------------------------------

    def _standing_lead_in_unit(self, human):
        """The one cast tank titled raid lead, never him or a former member; else None."""
        former = former_members_in_unit(self._store, human_guid=human)
        leads = []
        for member in self._ports.cast():
            guid = member.guid
            persona = self._ports.persona(guid)
            if (guid == human or guid in former or persona is None or persona.play.raid_role != "tank"
                    or not _title_matches(persona.play.officer_title, LEAD_TITLE)):
                continue
            leads.append(guid)
        return leads[0] if len(leads) == 1 else None

    def standing_lead(self, human) -> int | None:
        with self._store.transaction():
            return self._standing_lead_in_unit(human)

    def _name_in_unit(self, guid):
        persona = self._ports.persona(guid)
        name = persona.character_name if persona is not None else None
        if name is None:
            row = self._store.conn.execute("SELECT name FROM cast_member WHERE guid=?", (guid,)).fetchone()
            name = None if row is None else row[0]
        return name if type(name) is str and _NAME.fullmatch(name) else None

    def reserve(self, context, *, ready) -> QuestionIntent | None:
        """Reserve the next due question for the standing lead, committed before any send.

        Due: unasked, or refused before transport at least RETRY_MS ago, inside its 72 h
        window. The lead must be ready (online, not away) and hold no other open question.
        """
        self._own_unit()
        if not self._live(context):
            return None
        now = self._now_ms()
        with self._store.transaction():
            conn = self._store.conn
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM realm_kick_episode WHERE guild=? AND human=? AND answer IS NULL "
                "AND ask_expires_ms>? AND (ask_state='unasked' OR (ask_state='refused' AND attempted_ms<=?)) "
                "ORDER BY kick_ms,episode_id", (context.guild, context.human, now, now - RETRY_MS)).fetchall()
            if not rows:
                return None
            lead = self._standing_lead_in_unit(context.human)
            if lead is None or not ready(lead):
                return None
            if conn.execute(
                    "SELECT 1 FROM realm_kick_episode WHERE guild=? AND human=? AND lead=? AND answer IS NULL "
                    "AND ask_state IN ('reserved','delivered','unknown') LIMIT 1",
                    (context.guild, context.human, lead)).fetchone():
                return None  # One open question per lead: his answer must name exactly one.
            for row in rows:
                name = self._name_in_unit(row["guid"])
                if row["guid"] == lead or name is None:
                    continue
                text = QUESTION.format(name=name)
                intent = QuestionIntent(row["episode_id"], "guard_ask_" + uuid.uuid4().hex, row["guid"], lead,
                                        context.human, context.guild, text, _sha256(text),
                                        row["ask_expires_ms"], context)
                changed = conn.execute(
                    "UPDATE realm_kick_episode SET ask_state='reserved',lead=?,ask_id=?,ask_text_sha256=?,"
                    "ask_process_id=?,ask_generation=?,ask_connection_epoch=?,attempted_ms=?,dispatched_ms=NULL "
                    "WHERE episode_id=? AND ask_state=? AND answer IS NULL",
                    (lead, intent.ask_id, intent.text_sha256, self._process_id, context.generation,
                     str(context.connection_epoch), now, row["episode_id"], row["ask_state"])).rowcount
                if changed != 1:
                    raise RuntimeError("kick episode changed during its reservation")
                log.info("realm_guard_question_reserved: guid=%d lead=%d", row["guid"], lead)
                return intent
        return None

    def _reserved(self, intent):
        return self._episode("episode_id=? AND ask_id=?", (intent.episode_id, intent.ask_id))

    def before_transport(self, intent, *, context) -> bool:
        """The synchronous final barrier right before the line's Say is written; never await.

        It commits dispatched_ms for exactly this reserved, undispatched question while its
        reserving context still holds. Otherwise the question is refused (it never left). The
        service calls it only once the model latch and his presence hold (Realm._speakable).
        """
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            row = self._reserved(intent)
            if row is None or row["ask_state"] != "reserved" or row["dispatched_ms"] is not None:
                return False
            current = (context == intent.context and self._live(context)
                       and row["attempted_ms"] <= now < row["ask_expires_ms"])
            change = "dispatched_ms=?" if current else "ask_state='refused'"
            changed = self._store.conn.execute(
                f"UPDATE realm_kick_episode SET {change} WHERE episode_id=? AND ask_id=? "
                "AND ask_state='reserved' AND dispatched_ms IS NULL",
                ((now,) if current else ()) + (intent.episode_id, intent.ask_id)).rowcount
            if changed != 1:
                raise RuntimeError("kick question changed at its barrier")
        if not current:
            log.info("realm_guard_question_refused: guid=%d reason=context_unavailable", intent.target_guid)
        return current

    def on_recorded(self, intent, line_id, ack) -> bool:
        """The acked line is stored: the question is delivered and his 72 h start. Only the
        lead's whisper to him whose text is exactly the reserved question's (its sha256)."""
        self._own_unit()
        if type(ack) is not Ack or ack.ok is not True or type(ack.ts) is not int or type(line_id) is not int:
            return False
        now = self._now_ms()
        with self._store.transaction():
            conn = self._store.conn
            row = self._reserved(intent)
            if row is None or row["ask_state"] != "reserved" or row["dispatched_ms"] is None:
                return False
            line = conn.execute("SELECT channel,from_guid,to_guid,origin,text FROM chat_line WHERE id=?",
                                (line_id,)).fetchone()
            # The stored line is the text that went out: its hash must be the reserved question's.
            if (line is None or tuple(line)[:4] != ("whisper", intent.lead_guid, intent.human_guid, "mind")
                    or type(line["text"]) is not str or _sha256(line["text"]) != row["ask_text_sha256"]):
                return False
            dispatched = row["dispatched_ms"]
            received = max(now, dispatched)
            # The ack was made between the barrier's commit and its receipt here; native's own
            # ts is used inside that interval and bounded by it otherwise (one machine's clocks).
            acked = min(max(ack.ts, dispatched, 1), received)
            changed = conn.execute(
                "UPDATE realm_kick_episode SET ask_state='delivered',ask_line=?,ack_received_ms=?,ask_ack_ms=?,"
                "delivered_ms=?,reply_expires_ms=? WHERE episode_id=? AND ask_id=? AND ask_state='reserved' "
                "AND dispatched_ms=?", (line_id, received, acked, acked, acked + WINDOW_MS,
                                        intent.episode_id, intent.ask_id, dispatched)).rowcount
            if changed != 1:
                raise RuntimeError("kick question changed during its delivery")
        log.info("realm_guard_question_delivered: guid=%d lead=%d", intent.target_guid, intent.lead_guid)
        return True

    def settle_question(self, intent) -> str | None:
        """After the send: never at the barrier is refused; at it but unrecorded is unknown."""
        self._own_unit()
        with self._store.transaction():
            row = self._reserved(intent)
            if row is None:
                return None
            if row["ask_state"] != "reserved":
                return row["ask_state"]
            state = "refused" if row["dispatched_ms"] is None else "unknown"
            changed = self._store.conn.execute(
                "UPDATE realm_kick_episode SET ask_state=? WHERE episode_id=? AND ask_id=? AND ask_state='reserved'",
                (state, intent.episode_id, intent.ask_id)).rowcount
            if changed != 1:
                raise RuntimeError("kick question changed during its settlement")
        log.info("realm_guard_question_%s: guid=%d", state, intent.target_guid)
        return state

    # -- his answer ----------------------------------------------------------------------------

    def answer(self, ev, receipt, context) -> Admission | str | None:
        """His exact live yes or no to the lead holding his open delivered question, with no
        whisper between them since that question (_untied).

        Returns the Admission of a yes, "no" for a no, or None when this line is no answer.
        """
        self._own_unit()
        if (type(ev) is not Chat or ev.channel != "whisper" or ev.via_bridge is not False
                or type(ev.text) is not str or not self._live(context) or ev.from_guid != context.human
                or not _u32(ev.to_guid) or not _ms(ev.ts)):
            return None
        word = ev.text.strip().casefold()
        if word not in ANSWERS or type(receipt) is not RowReceipt or type(receipt.wire) is not WireOrigin:
            return None
        now = self._now_ms()
        received = int(receipt.wire.received_at * 1000)
        with self._store.transaction():
            conn = self._store.conn
            row = self._episode("guild=? AND human=? AND lead=? AND answer IS NULL AND ask_state='delivered'",
                                (context.guild, context.human, ev.to_guid))
            if (row is None or not row["delivered_ms"] <= received < row["reply_expires_ms"]
                    or now >= row["reply_expires_ms"] or not _bound_answer(conn, receipt, ev)):
                return None
            if _untied(conn, row, receipt.row_id):  # Refused before anything is consumed.
                log.info("realm_guard_answer_refused: guid=%d reason=intervening_whisper", row["guid"])
                return None
            source = _insert_source(conn, receipt, "chat", context)
            if source is None:
                return None
            name = self._name_in_unit(row["guid"]) or "them"
            admission = None
            if word == "yes":
                rank = readmit_rank(row["old_rank"])
                state = "prepared" if rank else "refused"
                admission = Admission(row["episode_id"], _command_id(row["episode_id"]), row["guid"],
                                      row["kick_ms"], rank, row["lead"], context.human, name, state)
                conn.execute(
                    "UPDATE realm_kick_episode SET answer='yes',answer_source=?,answered_ms=?,"
                    "consent_state='pending_native',admission_command_id=?,admission_state=?,"
                    "admission_reason=?,admission_settled_ms=? WHERE episode_id=? AND answer IS NULL "
                    "AND ask_state='delivered'",
                    (source, received, admission.command_id, state, None if rank else "rank_unknown",
                     None if rank else max(now, 1), row["episode_id"]))
            else:
                conn.execute(
                    "UPDATE realm_kick_episode SET answer='no',answer_source=?,answered_ms=?,"
                    "consent_state='declined' WHERE episode_id=? AND answer IS NULL AND ask_state='delivered'",
                    (source, received, row["episode_id"]))
        log.info("realm_guard_answer: guid=%d answer=%s", row["guid"], word)
        return admission if admission is not None else "no"

    # -- the readmission -----------------------------------------------------------------------

    @staticmethod
    def admission_command(admission: Admission) -> GuildReadmit:
        return GuildReadmit(admission.command_id, admission.guid, admission.kick_ms, admission.rank)

    def _admission(self, cmd):
        """The row of exactly this command: its id, the kick's guid, ts and readmission rank."""
        if type(cmd) is not GuildReadmit:
            return None
        row = self._episode("admission_command_id=? AND answer='yes'", (cmd.id,))
        if (row is None or (row["guid"], row["kick_ms"]) != (cmd.guid, cmd.kick_ts)
                or readmit_rank(row["old_rank"]) != cmd.rank):
            return None
        return row

    def _settle(self, row, state, reason, now, *, dispatch=False):
        column = "admission_dispatched_ms" if dispatch else "admission_settled_ms"
        value = now if dispatch else max(now, row["admission_dispatched_ms"] or 0, 1)
        changed = self._store.conn.execute(
            f"UPDATE realm_kick_episode SET admission_state=?,admission_reason=?,{column}=? "
            "WHERE episode_id=? AND admission_state=?",
            (state, reason, value, row["episode_id"], row["admission_state"])).rowcount
        if changed != 1:
            raise RuntimeError("kick admission changed during its transition")

    def owns_admission(self, cmd) -> bool:
        with self._store.transaction():
            return self._admission(cmd) is not None

    def before_admission(self, cmd, *, context) -> None:
        """The synchronous final barrier before the readmission's transport; never await.

        It commits 'dispatched' for the one prepared admission of exactly this command, while
        the Realm is live for his guild. Otherwise it settles that row, when there is one,
        and raises: a refusal is never tried again.
        """
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            row = self._admission(cmd)
            if row is None:
                raise GuardRefused("not_owned")
            if row["admission_state"] != "prepared":
                raise GuardRefused("settled")
            if self._live(context) and (context.guild, context.human) == (row["guild"], row["human"]):
                self._settle(row, "dispatched", None, max(now, 1), dispatch=True)
                return
            self._settle(row, "refused", "context_unavailable", now)
        raise GuardRefused("context_unavailable")

    def on_admission_ack(self, cmd, ack) -> bool:
        """Settle a dispatched admission from its native ack, also while disabled."""
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            row = self._admission(cmd)
            if (row is None or row["admission_state"] != "dispatched" or type(ack) is not Ack
                    or ack.id != cmd.id or type(ack.ok) is not bool or type(ack.error) is not str):
                return False
            if ack.ok:
                self._settle(row, "admitted", None, now)
            else:
                reason = ack.error if _REASON.fullmatch(ack.error) else "native_refused"
                self._settle(row, "unknown" if reason in _UNPROVED else "refused", reason, now)
        log.info("realm_guard_readmit: guid=%d ok=%s reason=%s", row["guid"], ack.ok,
                 "none" if ack.ok else reason)
        return True

    def on_admission_send_error(self, cmd, error_type) -> bool:
        """A failed transport is final: refused when nothing was written, otherwise unknown."""
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            row = self._admission(cmd)
            if row is None or row["admission_state"] != "dispatched":
                return False
            if error_type in _PRETRANSPORT:
                self._settle(row, "refused", "pretransport_refused", now)
            else:
                self._settle(row, "unknown", "transport_unknown", now)
        return True

    def abandon_admission(self, cmd) -> bool:
        """Close an admission whose send never reached the barrier (cancelled, disabled, failed)."""
        self._own_unit()
        now = self._now_ms()
        with self._store.transaction():
            row = self._admission(cmd)
            if row is None or row["admission_state"] != "prepared":
                return False
            self._settle(row, "refused", "context_unavailable", now)
        return True

    def admission_state(self, episode_id) -> tuple[str, str | None] | None:
        with self._store.transaction():
            row = self._episode("episode_id=?", (episode_id,))
            return None if row is None else (row["admission_state"], row["admission_reason"])

    # -- windows and recovery ------------------------------------------------------------------

    def sweep(self) -> int:
        """Close what can no longer matter: an unasked or refused question past its ask window,
        a delivered one past its reply window and an unknown one past the longest reply
        window it could have had. Never a reserved one: its own send settles it."""
        self._own_unit()
        now = self._now_ms()
        where = ("answer IS NULL AND ((ask_state IN ('unasked','refused') AND ask_expires_ms<=?) "
                 "OR (ask_state='delivered' AND reply_expires_ms<=?) "
                 "OR (ask_state='unknown' AND ask_expires_ms+?<=?))")
        params = (now, now, WINDOW_MS, now)
        with self._store.transaction():
            conn = self._store.conn
            # Read first: an ordinary tick writes nothing.
            expired = (conn.execute(f"UPDATE realm_kick_episode SET ask_state='expired',consent_state='expired' "
                                    f"WHERE {where}", params).rowcount
                       if conn.execute(f"SELECT 1 FROM realm_kick_episode WHERE {where} LIMIT 1",
                                       params).fetchone() else 0)
        if expired:
            log.info("realm_guard_expired: episodes=%d", expired)
        return expired

    def recover(self, *, process_id) -> tuple[int, int, int]:
        """At start, before any send: nothing an earlier process began is sent or asked again.

        A reserved question may have reached the wire, so it is unknown. A prepared admission
        never reached its barrier and is refused; a dispatched one is unknown.
        """
        self._own_unit()
        now = max(self._now_ms(), 1)
        with self._store.transaction():
            conn = self._store.conn
            questions = conn.execute(
                "UPDATE realm_kick_episode SET ask_state='unknown' WHERE ask_state='reserved' "
                "AND ask_process_id<>?", (process_id,)).rowcount
            prepared = conn.execute(
                "UPDATE realm_kick_episode SET admission_state='refused',admission_reason='recovered_prepared',"
                "admission_settled_ms=? WHERE admission_state='prepared'", (now,)).rowcount
            dispatched = conn.execute(
                "UPDATE realm_kick_episode SET admission_state='unknown',admission_reason='recovered_dispatched',"
                "admission_settled_ms=max(?,admission_dispatched_ms) WHERE admission_state='dispatched'",
                (now,)).rowcount
        if questions or prepared or dispatched:
            log.info("realm_guard_recovered: questions=%d prepared=%d dispatched=%d",
                     questions, prepared, dispatched)
        return questions, prepared, dispatched
