"""Owned offline dummy experiments, independent of the event dispatcher.

Only ``tick`` awaits the bridge. Event handlers enqueue bounded receipts. A lost
ACK, process restart or unproved native cleanup never authorizes another job.
The native dummy deliberately absorbs health damage: its requested_pre_ai
counter is a requested-damage proxy, not actual DPS or field improvement.
"""

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
import json
import logging
import math
import re
from uuid import uuid4

from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.offline import LOGOUT_KEY
from leeroy_mind.guild.protocol import Job, JobSkip
from leeroy_mind.protocol import Ack

from . import store
from .lab import _DAMAGE_SPECS, halving_rounds, paired_score, propose
from .protocol import CustomStrategy, DummyResult, DummyTrial
from .vocab import ClassVocab, refresh


log = logging.getLogger(__name__)
OWNER = "training"
OWNED_KEY = "tg:lab:owned"
CURSOR_KEY = "tg:lab:cursor"
ATTEMPT_PREFIX = "tg:lab:attempt:"
SUMMARY_PREFIX = "tg:lab:summary:"
INPUT_LIMIT = 64
ACK_SECONDS = 60
CLEANUP_SECONDS = 600
SCHEDULE_MAX_AGE = 60
DAMAGE_BASIS = "requested_pre_ai"
SPELL_BASIS = "best_effort_thread_pair"


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _in_window(now, window):
    start, end = (time.fromisoformat(value) for value in window)
    current = now.time().replace(tzinfo=None)
    return start != end and (start <= current < end if start < end else current >= start or current < end)


def next_window(now: datetime, window: tuple[str, str], *, weekday=None):
    """Conservative earliest window occupancy, including overnight windows."""
    start, end = (time.fromisoformat(value) for value in window)
    if start == end:
        return None
    for offset in range(-1, 8):
        day = now.date() + timedelta(days=offset)
        if weekday is not None and day.weekday() != weekday:
            continue
        begin = datetime.combine(day, start, now.tzinfo)
        finish = datetime.combine(day + timedelta(days=int(end < start)), end, now.tzinfo)
        if finish.timestamp() > now.timestamp():
            return max(now.timestamp(), begin.timestamp())
    return None


@dataclass(frozen=True, slots=True)
class LabSchedule:
    """Fresh scheduler proof. None due times mean *known* no scheduled work.

    The service adapter must supply real scheduler activity, including work
    acquiring bodies. Absence of an adapter is unknown and prohibits admission.
    """

    observed_at: float
    last_logout: float | None
    offline_active: bool
    benchmark_active: bool
    offline_due_at: float | None
    benchmark_due_at: float | None


def guild_schedule(ports, guild_cfg, *, offline_active: bool,
                   benchmark_active: bool, benchmark_due_at: float | None) -> LabSchedule:
    """Adapter using installed GuildConfig fields and its persisted logout key.

    Conservatively reserve the entire configured offline window, even after a
    daily quota has been met. Benchmark ownership/due time come from its owner.
    This function never guesses scheduler attributes or treats missing as idle.
    """
    due = (next_window(ports.clock.local(), guild_cfg.offline_window)
           if guild_cfg.offline_raids_per_day > 0 else None)
    return LabSchedule(ports.clock.now(), _get(ports.conn, LOGOUT_KEY), offline_active,
                       benchmark_active, due, benchmark_due_at)


class LabRunner:
    def __init__(self, ports, cfg, ground, applier, *, schedule=None):
        self.ports, self.cfg, self.ground, self.applier = ports, cfg, ground, applier
        self.schedule = schedule
        self._lock = asyncio.Lock()
        self._inputs = deque()
        self._stopping = False
        self._owned = _get(ports.conn, OWNED_KEY)
        self.reason = ""
        if self.active:
            # A new process cannot prove what a previous bridge await changed.
            self._owned["halt"] = "process_restart"
            self._owned["phase"] = "blocked"
            self._save()

    @property
    def active(self):
        return bool(self._owned and self._owned["phase"] != "complete")

    def members(self) -> set[int]:
        return set(self._owned["guids"]) if self.active else set()

    def _save(self):
        with self.ports.conn:
            _put(self.ports.conn, OWNED_KEY, self._owned)

    def _block(self, reason):
        self.reason = reason
        if self.active:
            self._owned["halt"] = reason
            self._owned["phase"] = "blocked"
            self._save()

    def _due(self):
        if self._stopping or not self.cfg.enabled or not self.cfg.lab or self.ports.human_online():
            return False
        if not _in_window(self.ports.clock.local(), self.cfg.lab_window):
            return False
        try:
            state = self.schedule() if self.schedule is not None else None
        except Exception:
            log.warning("training_lab_schedule_failed")
            return False
        now = self.ports.clock.now()
        if (not isinstance(state, LabSchedule) or not _number(state.observed_at)
                or not 0 <= now - state.observed_at <= SCHEDULE_MAX_AGE
                or not _number(state.last_logout) or not 1800 <= now - state.last_logout
                or type(state.offline_active) is not bool or type(state.benchmark_active) is not bool
                or state.offline_active or state.benchmark_active):
            return False
        for due in (state.offline_due_at, state.benchmark_due_at):
            if due is not None and (not _number(due) or due <= now + 3600):
                return False
        if self.ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone():
            return False
        return True

    def _desired(self, guids):
        """Malformed standing state is unknown, so it also blocks restoration."""
        desired = set()
        for guid, row in store.latest_setups(self.ports.conn).items():
            if guid not in guids:
                continue
            try:
                if (row["rotation"] is not None or row["premade"] is not None
                        or json.loads(row["toggles"]) or json.loads(row["vals"])):
                    desired.add(guid)
            except (ValueError, TypeError):
                desired.add(guid)
        return desired

    def _eligible(self):
        away = self.ports.away()
        groups = {}
        desired = self._desired(set(self.ports.cast()))
        for guid, member in sorted(self.ports.cast().items()):
            spec = (member.class_, member.spec_tab)
            if (type(guid) is int and guid > 0 and guid != self.ports.human_guid
                    and member.online is True and member.level == 80 and spec in _DAMAGE_SPECS
                    and guid not in away and guid not in desired and self.ports.holder(guid) is None):
                groups.setdefault(spec, []).append(guid)
        return {spec: guids[:min(10, self.cfg.lab_bots)] for spec, guids in groups.items()
                if len(guids) >= 2 and self.cfg.lab_bots >= 2}

    def _still_ours(self, guids):
        cast, away = self.ports.cast(), self.ports.away()
        spec = tuple(self._owned["spec"])
        return (not self._desired(set(guids)) and all(
            guid in cast and cast[guid].online is True
            and (cast[guid].class_, cast[guid].spec_tab) == spec and guid not in away
            and self.ports.holder(guid) in (None, OWNER) for guid in guids))

    def _renew(self):
        guids = self._owned_bodies()
        if not guids:
            return True
        # None is legitimate for LivePorts before the Realm body arbiter exists.
        if self.ports.claim(OWNER, guids, until=self.ports.clock.now() + CLEANUP_SECONDS + 180) != guids:
            self._block("body_claim_incomplete")
            return False
        return True

    def _owned_bodies(self):
        """A preempted body is never reclaimed or restored by this session."""
        members = self.members()
        previous = set(self._owned.get("preempted", ())) if self.active else set()
        preempted = previous | {g for g in members if self.ports.holder(g) not in (None, OWNER)}
        if preempted != previous:
            self._owned["preempted"] = sorted(preempted)
            self._owned["alive"] = [g for g in self._owned["alive"] if g not in preempted]
            self._save()
        return members - preempted

    async def _ack(self, command):
        ack = await asyncio.wait_for(self.ports.send(command), ACK_SECONDS)
        if not isinstance(ack, Ack) or ack.id != command.id or type(ack.ok) is not bool:
            raise ValueError("uncorrelated lab acknowledgement")
        return ack

    async def _start(self):
        book = self.ground.book()
        if book is None:
            self.reason = "spellbook_unavailable"
            return
        groups = self._eligible()
        if not groups:
            self.reason = "no_restorable_idle_damage_pair"
            return
        night = self._night()
        if _get(self.ports.conn, SUMMARY_PREFIX + night) is not None:
            return
        cursor = _get(self.ports.conn, CURSOR_KEY)
        specs = sorted(groups)
        spec = next((s for s in specs if cursor is None or list(s) > cursor), specs[0])
        guids = groups[spec]
        claimed = self.ports.claim(OWNER, guids, until=self.ports.clock.now() + CLEANUP_SECONDS + 180)
        if set(claimed) != set(guids):
            self.ports.release(OWNER, claimed)
            self.reason = "body_claim_incomplete"
            return
        self._owned = dict(id=uuid4().hex, phase="preparing", night=night, spec=list(spec),
            guids=guids, alive=list(guids), current=None, candidates=[], remaining=[], round=0,
            candidate=0, batch=0, leg=0, before={}, middle={}, scores={}, totals={}, rounds=[],
            attempts=0, gaps=[], halt="", preempted=[], round_scores=[], started=self.ports.clock.now())
        self._save()
        try:
            snapshot = await refresh(self.ports, probes=guids, book=book)
            if (not snapshot.complete or not self._due() or not self._still_ours(guids)
                    or any(snapshot.probe_specs.get(g) != spec for g in guids)):
                await self._finish("unproved_probe_spec_or_admission_changed")
                return
            cvs = [snapshot.probes[g] for g in guids]
            common = ClassVocab(frozenset.intersection(*(v.strategies for v in cvs)),
                frozenset.intersection(*(v.actions for v in cvs)),
                frozenset.intersection(*(v.spells for v in cvs)),
                all(v.spells_checked is True for v in cvs),
                frozenset.intersection(*(v.triggers for v in cvs)), spec[0])
            candidates = propose(*spec, book, common)
            if len(candidates) < 2:
                await self._finish("insufficient_checked_candidates")
                return
            for candidate in candidates:
                if not self._due() or not self._still_ours(guids) or not self._renew():
                    await self._finish("admission_changed_before_prime")
                    return
                row = self.ports.conn.execute("SELECT * FROM tg_rotation WHERE name=?", (candidate.name,)).fetchone()
                if row is not None:
                    if (row["class"], row["spec_tab"], tuple(json.loads(row["lines"]))) != (*spec, candidate.lines):
                        await self._finish("rotation_identity_conflict")
                        return
                    rotation = row["id"]
                else:
                    rotation = store.put_rotation(self.ports.conn, dict(name=candidate.name,
                        class_=spec[0], spec_tab=spec[1], lines=candidate.lines, origin="lab",
                        parent=None, status="candidate", created=self.ports.clock.now()))
                ack = await self._ack(CustomStrategy(uuid4().hex, candidate.name, spec[0], candidate.lines, guids[0]))
                if not ack.ok:
                    await self._finish("custom_strategy_rejected")
                    return
                self._owned["candidates"].append(dict(name=candidate.name, rotation=rotation))
                self._save()
            self._owned["remaining"] = [c["name"] for c in self._owned["candidates"]]
            self._owned["rounds"] = halving_rounds(len(candidates))
            self._owned["phase"] = "ready"
            self._save()
        except asyncio.CancelledError:
            self._block("preparation_cancelled")
            raise
        except Exception:
            self._block("preparation_uncertain")
            log.warning("training_lab_preparation_failed")

    def _night(self):
        now = self.ports.clock.local()
        start, end = (time.fromisoformat(v) for v in self.cfg.lab_window)
        date = now.date() - timedelta(days=int(end < start and now.time().replace(tzinfo=None) < end))
        return date.isoformat()

    async def on_job(self, event):
        self._enqueue("job", event, Job)

    async def on_result(self, event):
        self._enqueue("result", event, DummyResult)

    def feed(self, event):
        """No await: preserve receipts while the application's shutdown drains."""
        if isinstance(event, Job):
            self._enqueue("job", event, Job)
        elif isinstance(event, DummyResult):
            self._enqueue("result", event, DummyResult)

    def _enqueue(self, kind, event, cls):
        current = self._owned.get("current") if self.active else None
        if not isinstance(event, cls) or current is None or event.id != current["id"]:
            return
        if kind == "job" and (event.kind != "dummy_trial" or event.state not in {"done", "failed"}):
            return
        if len(self._inputs) >= INPUT_LIMIT:
            self._block("event_queue_overflow")
            return
        self._inputs.append((kind, event))

    def _drain(self):
        current = self._owned.get("current") if self.active else None
        while self._inputs:
            kind, event = self._inputs.popleft()
            if current is None or event.id != current["id"]:
                continue
            if (type(event.ts) is not int or event.ts < current["sent_ms"]
                    or event.ts > int(self.ports.clock.now() * 1000) + 5000):
                self._bad_receipt(current, "receipt_timestamp_invalid")
                continue
            if kind == "result":
                if type(event.guid) is not int or event.guid not in current["guids"]:
                    self._bad_receipt(current, "result_roster_mismatch")
                    continue
                if (any(type(v) is not int or v < 0 for v in (event.damage, event.ms, event.hits))
                        or type(event.dropped) is not str or type(event.damage_basis) is not str
                        or type(event.spell_basis) is not str):
                    self._bad_receipt(current, "result_shape_invalid")
                    continue
                value = asdict(event)
                prior = current["results"].get(str(event.guid))
                if prior is not None and prior != value:
                    self._bad_receipt(current, "conflicting_result")
                else:
                    current["results"][str(event.guid)] = value
            else:
                if (type(event.guids) is not tuple or type(event.skipped) is not tuple
                        or not all(isinstance(s, JobSkip) and type(s.guid) is int
                                   and type(s.reason) is str for s in event.skipped)
                        or type(event.reason) is not str or event.stage != ""
                        or any(type(v) is not int or v != 0 for v in
                               (event.kills, event.wipes, event.instance_id, event.guid, event.level, event.avg_ilvl))):
                    self._bad_receipt(current, "terminal_shape_invalid")
                    continue
                value = asdict(event)
                if current["terminal"] is not None and current["terminal"] != value:
                    self._bad_receipt(current, "conflicting_terminal")
                else:
                    current["terminal"] = value
        if current is not None:
            self._save()

    @staticmethod
    def _bad_receipt(current, reason):
        current["bad"] = reason
        if reason not in current.setdefault("faults", []):
            current["faults"].append(reason)

    def _terminal_proof(self, current):
        event = current["terminal"]
        if event is None:
            return False
        if ({current["bad"], *current.get("faults", [])}
                & {"conflicting_terminal", "terminal_shape_invalid", "conflicting_result"}):
            self._block("native_terminal_conflicted")
            return False
        good, skipped = event["guids"], event["skipped"]
        ids = [*good, *(s["guid"] for s in skipped)]
        if (any(type(g) is not int for g in ids) or len(ids) != len(set(ids))
                or set(ids) != set(current["guids"])):
            self._block("terminal_roster_mismatch")
            return False
        reasons = [event["reason"], *(s["reason"] for s in skipped)]
        if any(type(r) is not str or r.startswith("cleanup_failed") for r in reasons):
            self._block("native_cleanup_unproved")
            return False
        dropped = {str(s["guid"]): s["reason"] for s in skipped}
        for guid, result in current["results"].items():
            if result["dropped"].startswith("cleanup_failed"):
                self._block("native_cleanup_unproved")
                return False
            if result["dropped"] != dropped.get(guid, ""):
                self._block("result_terminal_conflicted")
                return False
            if result["ts"] > event["ts"]:
                self._block("result_after_terminal")
                return False
        # Native PublishTerminal(false) is reached only after AllReturned().
        # PublishTerminal(true) emits cleanup_failed and recovery has no new
        # event: such a failure cannot be cleared by a Python timeout.
        return True

    def _rate(self, result):
        return (result["damage"] * 1000 // result["ms"]) if result["ms"] > 0 else 0

    def _valid_result(self, current, result):
        terminal = current["terminal"]
        skipped = {str(s["guid"]): s["reason"] for s in terminal["skipped"]}
        return (not current["bad"] and not result["dropped"]
            and result["guid"] not in self._owned.get("preempted", ())
            and str(result["guid"]) not in skipped and result["guid"] in terminal["guids"]
            and terminal["state"] == "done" and not terminal["reason"]
            and result["damage_basis"] == DAMAGE_BASIS and result["spell_basis"] == SPELL_BASIS
            and current["seconds"] * 1000 <= result["ms"] <= current["seconds"] * 1000 + 5000
            and result["ms"] <= result["ts"] - current["sent_ms"]
            and result["ts"] <= terminal["ts"] and self._rate(result) > 0)

    def _record_attempt(self, current):
        """Commit raw receipts, gaps and legacy proxy rows atomically once."""
        key = ATTEMPT_PREFIX + current["id"]
        saved = _get(self.ports.conn, key)
        if saved is not None:
            return saved["rows"]
        rows = {}
        rotation = next((c["rotation"] for c in self._owned["candidates"]
                         if c["name"] == current["candidate_name"]), None)
        record = dict(current, lab=self._owned["id"], spec=self._owned["spec"],
                      round=self._owned["round"], rows=rows,
                      metric="dummy_requested_damage_proxy", field_performance="unavailable")
        with self.ports.conn:
            for guid in current["guids"]:
                result = current["results"].get(str(guid))
                gap = "missing_result" if result is None else (result["dropped"] or
                    ("" if self._valid_result(current, result) else "unscoreable_receipt"))
                # A gap row is an attempted run, never a zero measurement.
                values = (rotation, guid, self._owned["round"], current["seconds"],
                    self._rate(result) if result else 0, 0, gap, current["sent_ms"] / 1000,
                    result["damage_basis"] if result else "unknown",
                    result["spell_basis"] if result else "unknown")
                rows[str(guid)] = self.ports.conn.execute(
                    "INSERT INTO tg_dummy(rotation,guid,round,seconds,dps,stock_dps,dropped,ts,damage_basis,spell_basis) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", values).lastrowid
            if rotation is not None:
                self.ports.conn.execute("UPDATE tg_rotation SET status='dummy' WHERE id=? AND status='candidate'", (rotation,))
            _put(self.ports.conn, key, record)
        return rows

    def _restoration_receipt(self, current, state):
        current["restoration"] = state
        current["restoration_at"] = self.ports.clock.now()
        with self.ports.conn:
            _put(self.ports.conn, OWNED_KEY, self._owned)
            key = ATTEMPT_PREFIX + current["id"]
            attempt = _get(self.ports.conn, key)
            if attempt is not None:
                attempt["restoration"] = state
                attempt["restoration_at"] = current["restoration_at"]
                _put(self.ports.conn, key, attempt)

    async def _restore_returned(self, current):
        """Restore once after native return and before another leg can begin.

        Persist pending before awaiting. An interrupted attempt is unknown, not
        a request to run the mutation again. There are no desired setups at
        admission; if one appeared, current outside-raid zero cannot restore it.
        """
        state = current.get("restoration")
        if state == "complete_no_desired_setup":
            return True
        if state is not None:
            self._block("standing_restoration_unproved")
            return False
        guids = set(current["guids"]) & self._owned_bodies()
        if self._desired(guids):
            self._restoration_receipt(current, "blocked_desired_setup")
            self._block("standing_restoration_unavailable")
            return False
        self._restoration_receipt(current, "pending")
        try:
            await asyncio.wait_for(self.applier.apply_standing(tuple(sorted(guids))), ACK_SECONDS)
        except asyncio.CancelledError:
            self._block("standing_restoration_cancelled")
            raise
        except Exception:
            self._block("standing_restoration_uncertain")
            log.warning("training_lab_restoration_uncertain")
            return False
        if self._desired(guids & self._owned_bodies()):
            self._restoration_receipt(current, "blocked_desired_setup")
            self._block("standing_changed_during_cleanup")
            return False
        self._restoration_receipt(current, "complete_no_desired_setup")
        return True

    async def _settle(self):
        current = self._owned["current"]
        if not self._terminal_proof(current):
            return
        # Normal native emission is results then terminal. Allow reordered
        # dispatch briefly; missing data is then an explicit gap, never a pair.
        if len(current["results"]) < len(current["guids"]) and self.ports.clock.now() * 1000 < current["terminal"]["ts"] + 5000:
            return
        rows = self._record_attempt(current)
        if not await self._restore_returned(current):
            return
        valid = {guid: result for guid, result in current["results"].items()
                 if self._valid_result(current, result)}
        for guid in current["guids"]:
            if str(guid) not in valid and guid in self._owned["alive"]:
                self._owned["alive"].remove(guid)
        leg = self._owned["leg"]
        if leg == 0:
            self._owned["before"] = valid
        elif leg == 1:
            self._owned["middle"] = dict(results=valid, rows=rows)
        else:
            middle = self._owned["middle"]
            name = self._owned["remaining"][self._owned["candidate"]]
            for guid in sorted(set(valid) & set(self._owned["before"]) & set(middle.get("results", {}))):
                candidate = self._rate(middle["results"][guid])
                stock = (self._rate(self._owned["before"][guid]) + self._rate(valid[guid])) // 2
                if stock <= 0:
                    continue
                pair = [candidate, stock]
                self._owned["scores"].setdefault(name, []).append(pair)
                self._owned["totals"].setdefault(name, []).append(pair)
                with self.ports.conn:
                    self.ports.conn.execute("UPDATE tg_dummy SET stock_dps=? WHERE id=?", (stock, middle["rows"][guid]))
        self._owned["attempts"] += 1
        if len(valid) != len(current["guids"]):
            self._owned["gaps"].append(current["id"])
        self._owned["current"] = None
        self._owned["phase"] = "ready"
        self._save()
        if self._owned["halt"] or self._stopping or not self._due() or not self._owned["alive"]:
            await self._finish(self._owned["halt"] or "admission_ended")
            return
        self._owned["leg"] = (leg + 1) % 3
        if leg == 2:
            self._owned["before"], self._owned["middle"] = {}, {}
            self._owned["batch"] += 1
            if self._owned["batch"] >= len(self._batches()):
                self._owned["batch"] = 0
                self._owned["candidate"] += 1
            if self._owned["candidate"] >= len(self._owned["remaining"]):
                ranked = sorted(self._owned["scores"], key=lambda name: (-self._score(self._owned["scores"][name]), name))
                self._owned.setdefault("round_scores", []).append(dict(
                    round=self._owned["round"], scores=self._owned["scores"]))
                self._owned["remaining"] = ranked[:(len(self._owned["remaining"]) + 1) // 2]
                self._owned["scores"] = {}
                self._owned["round"] += 1
                self._owned["candidate"] = 0
                if len(ranked) < 2 or self._owned["round"] >= len(self._owned["rounds"]):
                    await self._finish("dummy_rounds_complete")
                    return
        self._save()

    def _batches(self):
        # Freeze batch identity across all three legs even if someone drops.
        return [self._owned["guids"][i:i + 5] for i in range(0, len(self._owned["guids"]), 5)]

    @staticmethod
    def _score(pairs):
        return paired_score([p[0] for p in pairs], [p[1] for p in pairs])

    async def _dispatch(self):
        if not self._due() or not self._still_ours(self._owned["alive"]):
            await self._finish("admission_changed")
            return
        batch = self._batches()[self._owned["batch"]]
        guids = [g for g in batch if g in self._owned["alive"]]
        if not guids:
            # Do not invent legs for a completely dropped batch.
            await self._finish("batch_no_longer_available")
            return
        seconds = self._owned["rounds"][self._owned["round"]][1]
        name = self._owned["remaining"][self._owned["candidate"]] if self._owned["leg"] == 1 else ""
        now = self.ports.clock.now()
        current = dict(id=uuid4().hex, guids=guids,
            co=["+custom::" + name if name else ""] * len(guids), candidate_name=name, seconds=seconds,
            sent_ms=int(now * 1000), deadline=now + seconds + CLEANUP_SECONDS,
            results={}, terminal=None, bad="", ack="pending")
        self._owned["current"] = current
        self._owned["phase"] = "waiting"
        self._save()  # Durable ownership precedes anything that can move a bot.
        try:
            ack = await self._ack(DummyTrial(current["id"], tuple(guids), tuple(current["co"]), 0, seconds))
            current["ack"] = "accepted" if ack.ok else "rejected"
            self._save()
            if not ack.ok:
                # An explicit rejection did not acquire native bodies.
                with self.ports.conn:
                    _put(self.ports.conn, ATTEMPT_PREFIX + current["id"], dict(current, rejected=True))
                self._owned["attempts"] += 1
                self._owned["current"] = None
                await self._finish("dummy_command_rejected")
        except asyncio.CancelledError:
            self._block("dummy_send_cancelled")
            raise
        except Exception:
            self._block("dummy_ack_uncertain")
            log.warning("training_lab_dispatch_uncertain")

    async def _finish(self, reason):
        if self._owned["current"] is not None:
            self._block("finish_without_native_return")
            return
        self._owned["phase"] = "restoring"
        self._owned["halt"] = reason
        self._save()
        guids = self._owned_bodies()
        if self._desired(guids):
            self._block("standing_restoration_unavailable")
            return
        # Zero is expected only because no selected bot has a desired setup.
        # It is never interpreted as proof that a desired setup was restored.
        await asyncio.wait_for(self.applier.apply_standing(tuple(sorted(guids))), ACK_SECONDS)
        if self._desired(self._owned_bodies()):
            self._block("standing_changed_during_cleanup")
            return
        totals = self._owned["totals"]
        survivors = self._owned["remaining"]
        rounds = self._owned.get("round_scores", [])
        last_scores = rounds[-1]["scores"] if rounds and reason == "dummy_rounds_complete" else {}
        # The last completed comparison decides the winner. Earlier rounds
        # remain evidence, but cannot outweigh a reversal in the final round.
        ranked = sorted((name for name in survivors if name in last_scores
                         and len(last_scores[name]) >= 4 and self._score(last_scores[name]) >= .02),
                        key=lambda name: (-self._score(last_scores[name]), name))
        winner = ranked[0] if ranked else None
        pairs = len(last_scores[winner]) if winner is not None else 0
        summary = dict(lab=self._owned["id"], spec=self._owned["spec"], winner=winner,
            pairs=pairs, aggregate_pairs=len(totals[winner]) if winner is not None else 0,
            winner_round=rounds[-1]["round"] if winner is not None else None,
            attempts=self._owned["attempts"], gaps=list(self._owned["gaps"]), reason=reason,
            status="dummy/provisional", metric="dummy_requested_damage_proxy",
            field_performance="unavailable", ts=self.ports.clock.now())
        self.ports.release(OWNER, self._owned_bodies())
        self._owned["phase"] = "complete"
        self.reason = reason
        with self.ports.conn:
            _put(self.ports.conn, OWNED_KEY, self._owned)
            _put(self.ports.conn, SUMMARY_PREFIX + self._owned["night"], summary)
            _put(self.ports.conn, CURSOR_KEY, self._owned["spec"])

    async def tick(self):
        async with self._lock:
            if self.active:
                self._drain()
                renewed = self._renew()
                current = self._owned["current"]
                if current is not None:
                    if current["terminal"] is not None:
                        await self._settle()
                    elif self.ports.clock.now() >= current["deadline"]:
                        self._block("native_terminal_missing")
                    return
                if not renewed:
                    return
                if self._owned["phase"] == "ready":
                    await self._dispatch()
                return
            if self._due():
                await self._start()

    async def disconnect(self):
        """Fence future legs; queued exact receipts remain available for recovery."""
        async with self._lock:
            if self.active:
                self._drain()
                if self._owned["current"] is None:
                    await self._finish("connection_lost")
                else:
                    self._block("connection_lost")

    async def close(self):
        """Stop admission; persist unresolved bodies for operator/native recovery.

        The service may keep ticking to receive an already-running native return.
        No standalone stop command exists for a DummyTrial on this protocol.
        """
        self._stopping = True
        async with self._lock:
            if self.active:
                self._drain()
                if self._owned["current"] is None:
                    await self._finish("shutdown")
                else:
                    self._block("shutdown_awaiting_native_return")

    def rotation_facts(self, question: str) -> list[str]:
        if not isinstance(question, str) or not re.search(r"\b(rotation|dummy|training|lab)\b", question, re.I):
            return []
        names = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest",
                 6: "death knight", 7: "shaman", 8: "mage", 9: "warlock", 11: "druid"}
        asked = {cls for cls, name in names.items() if re.search(r"\b" + name + r"\b", question, re.I)}
        facts = []
        rows = self.ports.conn.execute("SELECT value FROM meta WHERE key LIKE 'tg:lab:summary:%' ORDER BY key DESC LIMIT 8")
        for row in rows:
            summary = json.loads(row[0])
            cls, tab = summary["spec"]
            if asked and cls not in asked:
                continue
            if summary["winner"] is not None:
                facts.append(f"Training Ground {names[cls]} talent tab {tab}: {summary['pairs']} paired dummy attempts. "
                    "Candidate remains provisional; requested-damage proxy only, field performance unmeasured.")
            else:
                facts.append(f"Training Ground {names[cls]}: {summary['attempts']} attempts; no comparable dummy winner. "
                             "Field performance is unmeasured.")
            if len(facts) == 2:
                break
        return facts
