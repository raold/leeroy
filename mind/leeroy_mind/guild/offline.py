"""Clock-driven offline raids, with durable command ownership and bounded retries.

Dispatch hooks only enqueue input. The ticker owns bridge, writer and GPU awaits;
an uncertain remote start is watched, never sent again under another command ID.
"""

import asyncio
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta
import logging
import math
from uuid import uuid4

from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.wow import DUNGEONS, RAID_MAPS

from .comp import NoTankError, candidates_from, choose_leads, select_comp
from .lines import public_text
from .loot import _get, _put
from .protocol import StartOfflineRaid, StopOfflineRaid
from .raid_night import RaidRun
from .raids import split_key
from .session import RaidPlan, RaidSession
from .store import attendance_counts, bench_streaks, last_run


log = logging.getLogger(__name__)
HOLDER = "offline_raid"
MAX_ATTEMPTS = 8
RETRY_S = 60
BUSY_RETRY_S = 15 * 60
NO_FRESH_RAID = "setup:starting:no_fresh_encounters"
CLEANUP_GRACE_S = 60
# Native stops a run at its own timeout and then tears down (setup, return and cleanup waits are
# a minute each). A terminal that has not come this long after the deadline is not coming: the
# worldserver restarted, or the report went out while the bridge was down.
LOST_TERMINAL_S = 15 * 60
INPUT_LIMIT = 256
OWNED_KEY = "guild:offline:owned"
DAY_PREFIX = "guild:offline:day:"
LOGOUT_KEY = "guild:offline:last-logout"
REASON_LIMIT = 200
PROGRESS_FRESH_S = 120
# Instance maps a level-80 cast member is found in: the Wrath raids and 5-mans (DUNGEONS lacks
# Utgarde Pinnacle 575, the Oculus 578 and the Pit of Saron 658, which set no boss state).
# Native refuses a start while any requested body is in a dungeon (InstanceConflict).
INSTANCE_MAPS = RAID_MAPS | frozenset(DUNGEONS) | {575, 578, 658}


def is_due(now: datetime, *, human_online: bool, last_logout: datetime | None,
           raided_today: bool, window: tuple[time, time], min_away_min: int,
           raid_active: bool) -> bool:
    """Half-open local wall-clock window; equal endpoints mean an empty window.

    Overnight windows are supported. Aware away durations use elapsed UTC time,
    so the autumn clock repetition does not shorten an hour of actual absence.
    """
    if human_online or raided_today or raid_active or min_away_min < 0:
        return False
    start, end = window
    current = now.time().replace(tzinfo=None)
    if start == end or not (start <= current < end if start < end else current >= start or current < end):
        return False
    if last_logout is None:
        return True
    if (now.tzinfo is None) != (last_logout.tzinfo is None):
        return False
    elapsed = ((now - last_logout).total_seconds() if now.tzinfo is None
               else now.timestamp() - last_logout.timestamp())
    return elapsed >= min_away_min * 60


def retry_after(error: str) -> int | None:
    """Seconds before a refused start may be sent again today; None when it would be refused again.

    The driver logs in within seconds. A busy runner, or a cast body that is in combat, dead,
    travelling, in a dungeon or held by the Realm or training, is usually free within minutes.
    Configuration, map and roster refusals would repeat, so they end the day.
    """
    if error == "driver_pending":
        return RETRY_S
    if (error in {"busy", "human_online", "training_commands_pending", "training_journal_pending"}
            or error.startswith(("busy:", "leader_unavailable:", "in_instance:"))):
        return BUSY_RETRY_S
    return None


def setup_retry_after(reason: str) -> int | None:
    """Seconds before a run whose setup met a busy body may be sent again; None otherwise.

    Native checks the bodies again as it groups and teleports, so a refusal its ack did not
    see ends setup as setup:<stage>:<refusal>. too_few is the leader or members busy at grouping.
    """
    parts = reason.split(":", 2)
    if len(parts) != 3 or parts[0] != "setup" or parts[1] not in {"grouping", "teleporting", "starting"}:
        return None
    return BUSY_RETRY_S if parts[2] == "too_few" or retry_after(parts[2]) is not None else None


# Native start refusals that name one requested body by guid (StartRaidJob, InstanceConflict).
MEMBER_REFUSALS = ("in_instance:", "busy:realm_owner:", "busy:realm_group:", "not_cast:")


def refused_member(error: str, plan: RaidPlan) -> int | None:
    """The member of the sent roster a refusal names, or None when it names nobody in it.

    `leader_unavailable:<why>` names the leader, the plan's tank. `busy:<guid>` is a body a
    training dummy trial owns. A setup failure is read as its inner refusal. A guid outside the
    roster is a disagreement this cannot fix by leaving someone out, so it names nobody.
    """
    parts = error.split(":", 2)
    if len(parts) == 3 and parts[0] == "setup":
        error = parts[2]
    if error.startswith("leader_unavailable:"):
        return plan.tank_guid
    prefix = next((p for p in MEMBER_REFUSALS if error.startswith(p)), "busy:" if error.startswith("busy:") else None)
    tail = error[len(prefix):] if prefix else ""
    if not (tail.isascii() and tail.isdigit() and len(tail) <= 10):
        return None
    guid = int(tail)
    return guid if guid in plan.members else None


def next_raid_key(rotation, last_run: dict[str, float], skip=()) -> str:
    """Never-run first, then oldest; the configured order resolves ties.

    Raids in `skip` proved today that they have no fresh boss to fight.
    """
    keys = []
    for key in rotation:
        raid, size, heroic = split_key(key)
        if raid.key == "voa":
            raise ValueError("offline raid needs boss-state events")
        canonical = f"{raid.key}{size}{'h' if heroic else ''}"
        if canonical not in keys:
            keys.append(canonical)
    if not keys:
        raise ValueError("offline rotation is empty")
    keys = [key for key in keys if key not in skip]
    if not keys:
        raise ValueError("no offline raid left to try today")

    def rank(key):
        value = last_run.get(key)
        return (1, value) if type(value) in (int, float) and math.isfinite(value) else (0, 0)

    return min(keys, key=rank)


class OfflineRaids:
    def __init__(self, ports, cfg, armory, rng):
        self.ports, self.cfg, self.armory, self.rng = ports, cfg, armory, rng
        self.session = None
        self.run = None
        self._listeners = []
        self._lock = asyncio.Lock()
        self._inputs = deque()
        self._votes = []
        last_logout = _get(ports.conn, LOGOUT_KEY)
        self._last_logout = (datetime.fromtimestamp(last_logout, tz=ports.clock.local().tzinfo)
                             if last_logout is not None else None)
        self._online = ports.human_online()
        self._lease = False
        self._owned = _get(ports.conn, OWNED_KEY)
        # A restart must see a new native heartbeat before using any saved observation.
        self._progress = None
        if self.active:
            if self._owned["phase"] == "acquiring":
                # Dispatch was never reached. A new process owns no previous
                # arbiter hold and may safely try a fresh acquisition later.
                self._owned["phase"] = "rejected"
                self._save()
            else:
                # The remote command survives this process, its local model
                # lease does not. Recover accounting/terminal monitoring only.
                self._cancel()
                if self._owned.get("raid_id"):
                    self._restore_session()

    @property
    def active(self):
        return bool(self._owned and self._owned["phase"] not in {"complete", "rejected"})

    def add_listener(self, listener):
        self._listeners.append(listener)

    def _save(self):
        with self.ports.conn:
            _put(self.ports.conn, OWNED_KEY, self._owned)
            if self._owned and self._owned.get("operator_stop") and self._owned.get("phase") == "complete":
                terminal = self._owned.get("terminal") or {}
                _put(self.ports.conn, "ops:offline:last", {"id": self._owned["id"],
                    "stopped": terminal.get("reason") == "stopped"})

    def _day(self):
        return self.ports.clock.local().date().isoformat()

    def _day_state(self, day=None):
        return _get(self.ports.conn, DAY_PREFIX + (day or self._day())) or {"attempts": 0, "next_try": 0, "blocked": False}

    def _update_day(self, **changes):
        day = self._owned["day"] if self._owned else self._day()
        value = self._day_state(day)
        value.update(changes)
        with self.ports.conn:
            _put(self.ports.conn, DAY_PREFIX + day, value)

    def _due(self):
        now = self.ports.clock.local()
        begin = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = begin + timedelta(days=1)
        # Local calendar-day boundaries. A voided run never fought (no fresh boss, or a
        # busy body at setup), so it gives its place to the day's next try.
        day = self._day_state()
        voided = day.get("voided", [])
        void = [entry["raid_id"] for entry in voided if type(entry.get("raid_id")) is int]
        held = self.ports.conn.execute(
            "SELECT COUNT(*) FROM guild_raid WHERE offline=1 AND started>=? AND started<? "
            f"AND raid_id NOT IN ({','.join('?' * len(void))})",
            (begin.timestamp(), end.timestamp(), *void)).fetchone()[0]
        quota = max(held, day.get("accepted", 0) - len(voided)) >= self.cfg.offline_raids_per_day
        active = self.ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL").fetchone() is not None
        return (not day["blocked"] and day["attempts"] < MAX_ATTEMPTS
                and self.ports.clock.now() >= day["next_try"]
                and is_due(now, human_online=self._online or self.ports.human_online(),
                           last_logout=self._last_logout, raided_today=quota,
                           window=tuple(time.fromisoformat(t) for t in self.cfg.offline_window),
                           min_away_min=self.cfg.offline_min_away_min, raid_active=active))

    def _raid_key(self):
        return next_raid_key(self.cfg.offline_rotation, last_run(self.ports.conn),
                             set(self._day_state().get("skipped", [])))

    def _busy(self):
        """Members to leave out of today's plans: those a refusal named today (never sent again
        that day) and those the world already shows in a dungeon."""
        known = getattr(self.ports, "in_dungeon", None)
        try:
            inside = set(known()) if known is not None else set()
        except Exception as e:
            log.warning("offline_raid_dungeon_check_failed error=%s", type(e).__name__)
            inside = set()
        return set(self._day_state().get("excluded", [])), inside

    def _exclude(self, guid):
        """Leave a refused member out of every plan for the rest of the command's day."""
        excluded = self._day_state(self._owned["day"]).get("excluded", [])
        if guid not in excluded:
            self._update_day(excluded=sorted([*excluded, guid]))

    def _plan(self, key, saved=frozenset(), busy=frozenset()):
        """`saved` are members saved to a copy of this raid that he is saved to: native would
        unbind them from his copy, so they are unavailable, neither raiding nor benched. `busy`
        members are unavailable the same way: the bench fills their seats."""
        raid, size, heroic = split_key(key)
        cast = {g: m for g, m in self.ports.cast().items() if g != self.ports.human_guid}
        candidates = candidates_from(cast, {g: self.ports.persona(g) for g in cast},
                                     bench_streaks(self.ports.conn, cast),
                                     attendance_counts(self.ports.conn, cast, self.ports.clock.now() - 30 * 86400),
                                     set(self.ports.away()) | set(saved) | set(busy))
        leads = choose_leads(candidates, requested=None, human_leads=False, human_guid=self.ports.human_guid)
        comp = select_comp(size, candidates, must_include=[leads.tank_guid, leads.loot_guid],
                           human_in_raid=False, seed=self.rng.getrandbits(64))
        if len(comp.members) < size * 4 // 5:
            raise ValueError("too few available participants")
        return RaidPlan(raid.key, size, heroic, True, leads.tank_guid, leads.tank_guid,
                        leads.loot_guid, comp.members, comp.bench, False)

    def _plans_without(self, key, saved):
        try:
            self._plan(key, saved)
        except (ValueError, NoTankError):
            return False
        return True

    async def _notify(self, name, *args):
        for listener in tuple(self._listeners):
            if name != "on_end" and not self._actions_allowed():
                return
            hook = getattr(listener, name, None)
            if hook is not None:
                try:
                    if name == "on_end" and hasattr(listener, "invalidate"):
                        await hook(*args, run=self.run)
                    else:
                        await hook(*args)
                except Exception:
                    log.warning("offline raid listener failed: %s", name)

    def _actions_allowed(self):
        return (self.active and not self._owned["cancelled"] and self._owned.get("terminal") is None
                and not self._online and not self.ports.human_online())

    async def _release(self):
        if self._lease:
            # The arbiter reserves this holder before acquire's first await,
            # including when hand-over later raises or is cancelled.
            await self.ports.release_gpu(HOLDER)
            self._lease = False

    def _cancel(self):
        self._invalidate()
        if self.active and not self._owned["cancelled"]:
            self._owned["cancelled"] = True
            self._owned["cancel_at"] = self.ports.clock.now()
            self._save()

    def _invalidate(self):
        if self.run is not None:
            if hasattr(self.ports, "invalidate_run"):
                self.ports.invalidate_run(self.run)
            for listener in tuple(self._listeners):
                invalidate = getattr(listener, "invalidate", None)
                if invalidate is not None:
                    invalidate(self.run)

    async def on_login(self, ev):
        if ev.guid == self.ports.human_guid and ev.human:
            self._online = True
            self._cancel()

    async def on_logout(self, ev):
        if ev.guid == self.ports.human_guid and ev.human:
            self._online = False
            self._last_logout = self.ports.clock.local()
            with self.ports.conn:
                _put(self.ports.conn, LOGOUT_KEY, self._last_logout.timestamp())

    def _valid_job(self, ev):
        if (not self.active or ev.id != self._owned["id"] or ev.kind != "offline_raid"
                or ev.state not in {"progress", "done", "failed"}
                or ev.ts and ev.ts / 1000 < self._owned["sent_at"] - 5):
            return False
        if ev.state == "progress" and ev.stage not in {"grouping", "teleporting", "starting", "running", "restarting", "tearing_down"}:
            return False
        plan = RaidPlan(**self._owned["plan"])
        raid, _, _ = split_key(f"{plan.raid_key}{plan.size}{'h' if plan.heroic else ''}")
        members, skipped = set(ev.guids), [s.guid for s in ev.skipped if not s.reason.startswith("cleanup:")]
        if (any(type(g) is not int for g in (*ev.guids, *skipped)) or len(members) != len(ev.guids)
                or not members <= set(plan.members) or not set(skipped) <= set(plan.members)
                or members & set(skipped) or len(set(skipped)) != len(skipped)
                or type(ev.instance_id) is not int or not 0 <= ev.instance_id <= 0xFFFFFFFF
                # Encounter-state slots include minibosses (ICC has 13 slots,
                # versus 12 catalog bosses). Match the game runner's bound.
                or type(ev.kills) is not int or not 0 <= ev.kills <= 1024
                or type(ev.wipes) is not int or not 0 <= ev.wipes <= 0xFFFFFFFF
                or ev.kills and not ev.instance_id):
            return False
        accepted = self._owned.get("accepted")
        if accepted is not None and members != set(accepted):
            return False
        bound = self._owned.get("instance")
        if bound and ev.instance_id != bound:
            return False
        if ev.stage in {"starting", "running", "restarting"} and not ev.instance_id:
            return False
        usable = ({plan.tank_guid, plan.loot_guid} <= members and len(members) >= plan.size * 4 // 5
                  and members | set(skipped) == set(plan.members))
        if (ev.stage in {"teleporting", "starting", "running", "restarting"} or ev.kills) and not usable:
            return False
        return True

    async def on_job(self, ev):
        if not self._valid_job(ev) or self._owned.get("terminal") is not None:
            return
        if ev.guids and ev.stage in {"teleporting", "starting", "running", "restarting"}:
            self._owned["accepted"] = list(ev.guids)
        if ev.instance_id:
            self._owned["instance"] = ev.instance_id
        if ev.stage == "running" and ev.state == "progress":
            self._owned["running_seen"] = True
            self._observe_progress(ev)
        elif ev.state == "progress":
            self._progress = None
        if ev.state in {"done", "failed"}:
            self._owned["terminal"] = asdict(ev)
            self._invalidate()
        self._save()
        self._enqueue("job", ev)
        if ev.state == "failed":
            # The day receipt outlives both the next day's job and the server log. It is a
            # diagnostic only: terminal handling above is already saved and queued.
            log.info("offline_raid_failed reason=%s", str(ev.reason)[:REASON_LIMIT])
            try:
                if self._owned.get("day"):
                    self._update_day(failed=str(ev.reason)[:REASON_LIMIT])
            except Exception:
                log.warning("offline raid failure reason was not stored")

    def _unsupported_stop(self):
        if not self._owned.get("stop_unsupported"):
            self._owned["stop_unsupported"] = True
            log.info("offline_raid_stop_unsupported raid_id=%s", self._owned.get("raid_id"))

    def _observe_progress(self, ev):
        now = self.ports.clock.now()
        if all(v is None for v in (ev.members_in_instance, ev.last_progress_ts, ev.boss_engaged)):
            self._unsupported_stop()
            self._progress = None
            return
        previous = self._progress
        # Wire times are milliseconds, on the same host. Require recent, ordered, complete evidence.
        if (type(ev.ts) is not int or not 0 <= now - ev.ts / 1000 <= PROGRESS_FRESH_S
                or type(ev.last_progress_ts) is not int
                or not self._owned["sent_at"] * 1000 - 5000 <= ev.last_progress_ts <= ev.ts
                or type(ev.members_in_instance) is not int or not 0 <= ev.members_in_instance <= len(ev.guids)
                or type(ev.boss_engaged) is not bool):
            self._progress = None
            return
        if previous and ev.ts <= previous["ts"]:
            return
        # Members leaving the instance is native's own stop (too_few, checked before a heartbeat is
        # sent); the mind has no scatter rule of its own.
        self._progress = dict(ts=ev.ts, received=now, last=ev.last_progress_ts / 1000,
                              engaged=ev.boss_engaged, inside=ev.members_in_instance)

    async def _watchdog(self):
        now = self.ports.clock.now()
        progress = self._progress
        if (self._online or self.ports.human_online() or now >= self._owned["deadline"]
                or self._owned.get("stop_unsupported") or self._owned.get("terminal") is not None
                or not progress or not 0 <= now - progress["received"] <= PROGRESS_FRESH_S
                or not 0 <= now - progress["ts"] / 1000 <= PROGRESS_FRESH_S
                or progress["engaged"] or now < self._owned.get("stop_retry_at", 0)):
            return
        stop = self._owned.get("stop")
        if stop and (stop.get("accepted") or stop.get("await_terminal")):
            return
        if self._owned.get("operator_stop") is True:
            # Keep an earlier uncertain attempt's reason, but operator intent
            # survives resumed progress. The freshness/combat fences still apply.
            reason = stop["reason"] if stop else "operator_stopped"
        else:
            reason = ("operator_stopped" if stop and stop.get("reason") == "operator_stopped" else
                      "stalled" if now - progress["last"] >= self.cfg.offline_stall_minutes * 60 else None)
        if reason is None or stop and stop["reason"] != reason:
            return  # New progress cancels automatic stall retries; still watch for their terminal.
        if not stop:
            stop = self._owned["stop"] = dict(reason=reason, accepted=False)
            log.info("offline_raid_stop raid_id=%s reason=%s inside=%d members=%d",
                     self._owned.get("raid_id"), reason, progress["inside"], len(self._owned["accepted"]))
        await self._send_stop(now, stop)

    def stop_problem(self, run_id):
        if not self.active or self._owned.get("id") != run_id or self._owned.get("terminal") is not None:
            return "That offline raid is no longer an active owned run."
        if self._online or self.ports.human_online():
            return "The owner is in game; wait until he is offline."
        if self._owned.get("stop_unsupported"):
            return "This game server does not support safe offline raid stops."
        if (self._owned.get("stop") or {}).get("accepted"):
            return None
        now, progress = self.ports.clock.now(), self._progress
        if (not progress or not 0 <= now-progress["received"] <= PROGRESS_FRESH_S
                or not 0 <= now-progress["ts"]/1000 <= PROGRESS_FRESH_S):
            return "Fresh offline raid progress is required before a stop."
        if progress["engaged"]:
            return "A boss is engaged; wait until combat ends."
        return None

    async def request_stop(self, run_id):
        """Operator stop targets the owned start id, never a new job or a guessed raid."""
        async with self._lock:
            await self._drain()
            if problem := self.stop_problem(run_id):
                raise ValueError(problem)
            self._owned["operator_stop"] = True
            self._save()
            stop = self._owned.get("stop")
            if stop and (stop.get("accepted") or stop.get("await_terminal")):
                return
            if not stop:
                stop = self._owned["stop"] = dict(reason="operator_stopped", accepted=False)
            await self._send_stop(self.ports.clock.now(), stop)

    def operator_result(self, run_id):
        result = _get(self.ports.conn, "ops:offline:last")
        return result if isinstance(result, dict) and result.get("id") == run_id else None

    async def _send_stop(self, now, stop):
        self._owned["stop_retry_at"] = now + RETRY_S
        # The transport forbids reissuing a command id. Native stop is idempotent by raid_id,
        # so a retry gets a new envelope id while its target and recorded reason stay fixed.
        stop["id"] = uuid4().hex
        self._save()
        try:
            ack = await self.ports.send(StopOfflineRaid(stop["id"], self._owned["id"]))
        except Exception as e:
            # Disconnected, timed out or transport off: the stop is uncertain, a later tick retries.
            log.info("offline_raid_stop_send_failed raid_id=%s error=%s",
                     self._owned.get("raid_id"), type(e).__name__)
            return
        if ack is None or ack.id != stop["id"]:
            return
        if ack.ok is True:
            stop["accepted"] = True
            stop["accepted_at"] = now
            self._cancel()
        elif ack.ok is False:
            if ack.error in {"unknown type: offline_raid_stop", "dungeon_clear_unavailable"}:
                self._unsupported_stop()
            if ack.error in {"raid_finished", "raid_finishing", "unknown_raid"}:
                stop["await_terminal"] = True
            # Keep the uncertain attempt's reason: its terminal may arrive after this refusal.
        self._save()

    def _enqueue(self, kind, ev):
        item = (kind, ev)
        if item in self._inputs:
            return
        if len(self._inputs) >= INPUT_LIMIT:
            cosmetic = next((old for old in self._inputs if old[0] in {"death", "dc_status"}), None)
            if cosmetic is not None:
                self._inputs.remove(cosmetic)
            elif kind in {"death", "dc_status"}:
                return
            else:
                self._cancel()
                log.warning("offline raid event queue is full")
                return
        self._inputs.append(item)

    def _event(self, name, ev):
        if not self.active or self._owned.get("terminal") is not None:
            return
        plan = self.session.plan if self.session else RaidPlan(**self._owned["plan"])
        members = set(self._owned["accepted"] or ())
        raid, _, _ = split_key(f"{plan.raid_key}{plan.size}{'h' if plan.heroic else ''}")
        if ev.ts and ev.ts / 1000 < self._owned["sent_at"] - 5:
            return
        if name in {"boss", "loot_drop"}:
            if ev.map != raid.map_id or ev.instance_id != self._owned["instance"]:
                return
            if name == "boss" and (ev.state not in {"engaged", "killed", "wiped"} or not members.intersection(ev.members)):
                return
        elif name == "death" and (ev.guid not in members or ev.map != raid.map_id):
            return
        elif name == "resurrect" and ev.guid not in members:
            return
        elif name == "dc_status" and ev.tank_guid != plan.tank_guid:
            return
        if name == "boss" and self._progress is not None:
            # Correlated boss evidence can arrive between heartbeats. Do not stop using an older idle sample.
            self._progress["last"] = max(self._progress["last"], self.ports.clock.now())
            if ev.state == "engaged":
                self._progress["engaged"] = True
            # Only a new native snapshot proves that every boss is now disengaged.
        # After a restart or a login the run is cancelled and its session gone, but the watchdog
        # still watches it, so the evidence above counts; only the guild-life input is dropped.
        if self._owned["cancelled"] or not self._owned.get("running_seen"):
            return
        self._enqueue(name, ev)

    async def on_boss(self, ev):
        self._event("boss", ev)

    async def on_death(self, ev):
        self._event("death", ev)

    async def on_resurrect(self, ev):
        self._event("resurrect", ev)

    async def on_dc_status(self, ev):
        self._event("dc_status", ev)

    async def on_loot_drop(self, ev):
        self._event("loot_drop", ev)

    def _hold_today(self, **changes):
        with self.ports.conn:
            state = self._day_state()
            state.update(changes)
            _put(self.ports.conn, DAY_PREFIX + self._day(), state)

    async def _start(self):
        try:
            key = self._raid_key()
            raid, _, _ = split_key(key)
        except ValueError:
            self._hold_today(blocked=True)
            return
        cast = tuple(g for g in self.ports.cast() if g != self.ports.human_guid)
        try:
            saved = await asyncio.to_thread(self.armory.bound_with, self.ports.human_guid, raid.map_id, cast)
        except Exception as e:
            # Unproved binds could cost him a lockout: no raid until they can be read.
            log.warning("offline_raid_binds_unreadable error=%s", type(e).__name__)
            attempts = self._day_state()["attempts"] + 1
            self._hold_today(attempts=attempts, next_try=self.ports.clock.now() + BUSY_RETRY_S,
                             blocked=attempts >= MAX_ATTEMPTS)
            return
        excluded, inside = self._busy()
        busy = (excluded | inside) - set(saved)
        try:
            plan = self._plan(key, saved, busy)
        except (ValueError, NoTankError):
            if busy and self._plans_without(key, saved):
                # Only the members a refusal named or a dungeon holds are missing: wait for them,
                # never sending a roster below the minimum or without a leader.
                log.info("offline_raid_waiting reason=too_few excluded=%d in_dungeon=%d",
                         len(busy & excluded), len(busy - excluded))
                self._hold_today(next_try=self.ports.clock.now() + BUSY_RETRY_S)
            elif saved:
                # His copy holds members this raid needed: the next raid in the rotation may not.
                skipped = self._day_state().get("skipped", [])
                self._hold_today(skipped=skipped if key in skipped else [*skipped, key],
                                 next_try=self.ports.clock.now() + RETRY_S)
            else:
                self._hold_today(blocked=True)
            return
        if busy:
            log.info("offline_raid_planned excluded=%d in_dungeon=%d", len(busy & excluded), len(busy - excluded))
        now = self.ports.clock.now()
        self._progress = None
        self._owned = dict(id=uuid4().hex, day=self._day(), phase="acquiring", plan=asdict(plan),
                           sent_at=now, deadline=now + self.cfg.offline_timeout_min * 60,
                           cancelled=False, cancel_at=None, accepted=None, instance=None,
                           raid_id=None, started=False, running_seen=False, attended=False,
                           terminal=None, end_notified=False)
        self._save()
        try:
            self._lease = True
            result = await self.ports.acquire_gpu(HOLDER)
            if result is False:
                self._lease = False  # An explicitly denied acquisition grants no holder.
                self._owned["phase"] = "rejected"
                self._save()
                return
            if self._online or self.ports.human_online() or self._owned["cancelled"] or not self._due():
                self._owned["phase"] = "rejected"
                self._save()
                await self._release()
                return
            self._owned["phase"] = "awaiting_ack"
            self._owned["day"] = self._day()  # Acquisition may have crossed midnight in an overnight window.
            self._owned["sent_at"] = self.ports.clock.now()
            self._owned["deadline"] = self.ports.clock.now() + self.cfg.offline_timeout_min * 60
            self._save()
            state = self._day_state(self._owned["day"])
            self._update_day(attempts=state["attempts"] + 1, next_try=self.ports.clock.now() + RETRY_S)
            assistants = tuple(g for g in plan.members if g != plan.tank_guid
                               and self.ports.persona(g).play.guild_role == "officer")
            raid, _, _ = split_key(f"{plan.raid_key}{plan.size}{'h' if plan.heroic else ''}")
            command = StartOfflineRaid(self._owned["id"], raid.map_id, plan.size, plan.tank_guid,
                                       tuple(g for g in plan.members if g != plan.tank_guid), plan.loot_guid,
                                       heroic=plan.heroic, assistants=assistants,
                                       max_wipes=self.cfg.offline_max_wipes,
                                       max_wipes_per_boss=self.cfg.offline_max_wipes_per_boss,
                                       timeout_min=self.cfg.offline_timeout_min, min_members=plan.size * 4 // 5)
            ack = await self.ports.send(command)
            if ack is None or ack.id != command.id:
                self._owned["phase"] = "uncertain"
                self._owned["cancelled"] = True
                self._update_day(blocked=True)
                await self._release()
            elif ack.ok is not True:
                self._owned["phase"] = "rejected"
                refused = str(ack.error or "")[:REASON_LIMIT]
                member = None if self._owned["cancelled"] else refused_member(str(ack.error or ""), plan)
                delay = None if self._owned["cancelled"] else retry_after(str(ack.error or ""))
                if member is not None and delay is None:
                    delay = RETRY_S  # not_cast: the stale member leaves the plan, nothing else waits.
                log.info("offline_raid_refused error=%s retry=%s excluded=%s", refused, delay is not None,
                         member if member is not None else "")
                changes = dict(blocked=delay is None or self._day_state(self._owned["day"])["attempts"] >= MAX_ATTEMPTS,
                               refused=refused)
                if delay is not None:
                    changes["next_try"] = self.ports.clock.now() + delay
                self._update_day(**changes)
                if member is not None:
                    # The next plan replaces this member from the bench; it is never sent again today.
                    self._exclude(member)
                await self._release()
            else:
                self._owned["phase"] = "accepted"
                state = self._day_state(self._owned["day"])
                self._update_day(accepted=state.get("accepted", 0) + 1)
            self._save()
        except asyncio.CancelledError:
            if self._owned["phase"] in {"acquiring", "rejected"}:
                self._owned["phase"] = "rejected"
            else:
                self._owned["phase"] = "uncertain"
                self._update_day(blocked=True)
            self._cancel()
            self._save()
            await self._release()
            raise
        except Exception:
            if self._owned["phase"] in {"acquiring", "rejected"}:
                self._owned["phase"] = "rejected"
            else:
                self._owned["phase"] = "uncertain"
                self._owned["cancelled"] = True
            self._update_day(blocked=True)
            self._save()
            await self._release()
            log.warning("offline raid start could not complete")

    def _restore_session(self):
        saved = self._owned
        plan = RaidPlan(**saved["actual_plan"])
        row = self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE raid_id=?", (saved["raid_id"],)).fetchone()
        if row is not None and _get(self.ports.conn, f"guild:raid:{saved['raid_id']}:plan") == asdict(plan):
            self.session = RaidSession(self.ports, self.cfg, self.armory, plan, saved["raid_id"])
            self.run = RaidRun(plan, self.session, False, False)

    async def _open(self, ev):
        if self.session is not None:
            if self.run is None:
                self.run = RaidRun(self.session.plan, self.session, False, False)
            return
        if self._owned["cancelled"] or self._online or self.ports.human_online():
            # A late progress packet cannot start local raid activity after a
            # login/cancellation. Existing attendance remains owned until done.
            return
        if self._owned.get("accepted") is None:
            # Grouping can fail after the runner listed eligible participants
            # but before AddMember succeeded. Terminal guids alone prove none.
            return
        plan = RaidPlan(**self._owned["plan"])
        accepted = set(ev.guids)
        if (not {plan.tank_guid, plan.loot_guid} <= accepted
                or len(accepted) < plan.size * 4 // 5
                or accepted | {s.guid for s in ev.skipped if not s.reason.startswith("cleanup:")} != set(plan.members)):
            return
        actual = replace(plan, members=[g for g in plan.members if g in accepted])
        self._owned["accepted"] = list(actual.members)
        self._owned["actual_plan"] = asdict(actual)
        self._save()
        before = {r[0] for r in self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL")}
        try:
            self.session = await RaidSession.open(self.ports, self.cfg, self.armory, actual, attend=False)
        except BaseException:
            for row in self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL").fetchall():
                if row[0] not in before and _get(self.ports.conn, f"guild:raid:{row[0]}:plan") == asdict(actual):
                    self.session = RaidSession(self.ports, self.cfg, self.armory, actual, row[0])
                    self.run = RaidRun(actual, self.session, False, False)
                    self._owned["raid_id"] = row[0]
                    self._save()
            raise
        self._owned["raid_id"] = self.session.raid_id
        self._save()
        self.run = RaidRun(self.session.plan, self.session, False, False)
        if self._owned["cancelled"] or self._online or self.ports.human_online():
            self.ports.set_lead(None)

    async def _job(self, ev):
        if ev.guids and (ev.stage in {"teleporting", "starting", "running", "restarting", "tearing_down"}
                         or ev.state in {"done", "failed"}):
            await self._open(ev)
        if ev.instance_id:
            if self.session is not None and not self.session.bind_instance(ev.instance_id):
                self._cancel()
                return
            self._owned["instance"] = ev.instance_id
            self._save()
        if (ev.stage == "running" and ev.state == "progress" and self.session is not None
                and not self._owned.get("attended")):
            await self._attend()  # The run started: now its members raided.
        if (ev.stage == "running" and ev.state == "progress" and self.run is not None
                and not self._owned["cancelled"] and not self._online and not self.ports.human_online()
                and self._owned.get("terminal") is None
                and not self._owned["started"]):
            self._owned["started"] = True
            self._save()
            await self._notify("on_start", self.run)

    async def _drain(self):
        while self._inputs:
            kind, ev = self._inputs[0]
            if kind == "job":
                await self._job(ev)
            elif self.session is not None:
                actionable = self._actions_allowed()
                # Preserve accepted kill/wipe accounting across cancellation,
                # but never start casts, loot transfers or chatter afterward.
                if kind == "boss":
                    await self.session.on_boss(ev, can_announce=self._actions_allowed)
                elif kind == "loot_drop" and actionable:
                    await self.session.on_loot_drop(ev)
                if kind in {"boss", "death", "resurrect", "dc_status"} and actionable:
                    await self._notify("on_" + kind, ev)
            self._inputs.popleft()

    def _no_fresh_raid(self, terminal):
        """Setup proved this raid's copy had no fresh boss before any boss was fought."""
        return (terminal["state"] == "failed" and terminal["reason"] == NO_FRESH_RAID
                and not self._owned.get("started") and not self._owned.get("running_seen")
                and not terminal.get("kills") and not terminal.get("wipes"))

    async def _attend(self):
        await self.session.attend()
        self._owned["attended"] = True
        self._save()

    def _void_run(self, raid_id, delay, *, skip_raid=False):
        """The command does not use up the day's raid; with skip_raid its raid is passed over today."""
        day = self._day_state(self._owned["day"])
        voided, skipped = day.get("voided", []), day.get("skipped", [])
        if not any(entry["id"] == self._owned["id"] for entry in voided):
            voided = [*voided, dict(id=self._owned["id"], raid_id=raid_id)]
        if skip_raid:
            plan = RaidPlan(**self._owned["plan"])
            key = f"{plan.raid_key}{plan.size}{'h' if plan.heroic else ''}"
            skipped = skipped if key in skipped else [*skipped, key]
        self._update_day(voided=voided, skipped=skipped, next_try=self.ports.clock.now() + delay)

    def _lose_terminal(self):
        """Record the terminal receipt for a run whose native report never came.

        Native has passed its own timeout and teardown by now, so it holds nobody; the ordinary
        finish path then closes the raid row and completes the owned receipt.
        """
        kills = self.session.kills if self.session is not None else 0
        wipes = self.session.wipes if self.session is not None else 0
        plan = self._owned["plan"]
        log.warning("offline_raid_terminal_lost raid_id=%s members=%d",
                    self._owned.get("raid_id"), len(self._owned.get("accepted") or ()))
        self._owned["terminal"] = dict(
            ts=0, id=self._owned["id"], kind="offline_raid", state="failed", reason="terminal_lost",
            stage="", guids=list(self._owned.get("accepted") or ()), skipped=[], kills=kills, wipes=wipes,
            instance_id=self._owned.get("instance") or 0, guid=plan["tank_guid"], level=0, avg_ilvl=0)
        self._invalidate()
        self._save()

    async def _finish(self):
        terminal = self._owned.get("terminal")
        if terminal is None:
            return
        outcome = terminal["reason"] or terminal["state"]
        if outcome == "stopped" and self._owned.get("stop"):
            outcome = self._owned["stop"]["reason"]
        # A restart may retain the terminal receipt but not the in-memory inbox.
        if self.session is None:
            from .protocol import Job, JobSkip
            ev = Job(**{**terminal, "guids": tuple(terminal["guids"]),
                        "skipped": tuple(JobSkip(**s) for s in terminal["skipped"])})
            await self._job(ev)
        if self.run is not None:
            for listener in tuple(self._listeners):
                hook = getattr(listener, "on_before_end", None)
                if hook is None:
                    continue
                try:
                    async with asyncio.timeout(20):
                        ready = await hook(self.run, outcome)
                except Exception:
                    ready = False
                    log.warning("offline raid cleanup remains pending")
                if ready is not True:
                    # This is only the GPU lease. Retain the exact run, terminal
                    # receipt and body identity for a later cleanup attempt.
                    await self._release()
                    return
        no_fresh = self._no_fresh_raid(terminal)
        # Only a run that reached running, or fought, was raided.
        raided = bool(self._owned.get("running_seen") or self._owned.get("started")
                      or terminal.get("kills") or terminal.get("wipes"))
        busy = (None if raided or no_fresh or terminal["state"] != "failed"
                else setup_retry_after(str(terminal.get("reason") or "")))
        try:
            if self.session is not None:
                session = self.session
                with self.ports.conn:
                    self.ports.conn.execute("UPDATE guild_raid SET kills=MAX(kills,?),wipes=MAX(wipes,?) WHERE raid_id=?",
                                            (terminal["kills"], terminal["wipes"], session.raid_id))
                session.kills = max(session.kills, terminal["kills"])
                session.wipes = max(session.wipes, terminal["wipes"])
                if raided and not self._owned.get("attended"):
                    await self._attend()
                vote = await session.close(public_text(self.ports, outcome)[:160],
                                           raided=raided)
                if vote is not None and vote not in self._votes:
                    self._votes.append(vote)
            if self._owned["started"] and not self._owned["end_notified"]:
                self._owned["end_notified"] = True
                self._save()
                await self._notify("on_end", outcome)
        finally:
            await self._release()
        if raided:
            await self._memories()
        raid_id = self.session.raid_id if self.session is not None else None
        if no_fresh:
            # Nobody fought, and the day's one raid is still to come.
            self._void_run(raid_id, RETRY_S, skip_raid=True)
        elif busy is not None:
            self._void_run(raid_id, busy)
            member = refused_member(str(terminal.get("reason") or ""), RaidPlan(**self._owned["plan"]))
            if member is not None:
                log.info("offline_raid_setup_excluded guid=%d", member)
                self._exclude(member)
        elif terminal["state"] == "failed":
            self._update_day(blocked=True)
        self._owned["phase"] = "complete"
        self._save()
        self.session = self.run = None

    async def _memories(self):
        if self.session is None:
            return
        saved, session = self._owned, self.session
        key = f"guild:offline:result:{saved['id']}"
        event_id = _get(self.ports.conn, key)
        if event_id is None:
            import json
            payload = dict(command=saved["id"], raid_id=session.raid_id, map=session.ctx.map_id,
                           instance_id=session.ctx.instance_id or 0, members=session.plan.members,
                           kills=session.kills, wipes=session.wipes)
            with self.ports.conn:
                row = self.ports.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
                                              (self.ports.clock.now(), "guild_offline_raid", 0, json.dumps(payload)))
                event_id = row.lastrowid
                _put(self.ports.conn, key, event_id)
        raid, _, _ = split_key(f"{session.plan.raid_key}{session.plan.size}{'h' if session.plan.heroic else ''}")
        text = public_text(self.ports, f"We raided {raid.name} without {self.ports.human().called}: "
                           f"{session.kills} kills, {session.wipes} wipes.")
        for guid in session.plan.members:
            receipt = key + f":memory:{guid}"
            if _get(self.ports.conn, receipt):
                continue
            await self.ports.remember(MemoryCandidate(guid, "event", text, int(self.ports.clock.now()),
                                      tuple(session.plan.members), 5, 0.4 if session.kills else -0.3,
                                      "game", (f"event:{event_id}",), raid_id=session.raid_id,
                                      key=f"offline-raid:{session.raid_id}"))
            with self.ports.conn:
                _put(self.ports.conn, receipt, True)

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            try:
                for vote in tuple(self._votes):
                    if await vote.tick(self.ports.clock.now()):
                        self._votes.remove(vote)
                if self._lease and not self.active:
                    # A failed local release cannot turn a proved rejection
                    # into an uncertain remotely running command.
                    await self._release()
                if self.active:
                    if (self._online or self.ports.human_online()
                            or self.ports.clock.now() >= self._owned["deadline"]):
                        self._cancel()
                    await self._drain()
                    await self._watchdog()
                    if self._owned.get("terminal") is not None:
                        await self._finish()
                    else:
                        deadline = self._owned["deadline"]
                        if self._owned.get("cancel_at") is not None:
                            deadline = min(deadline, self._owned["cancel_at"])
                        stop = self._owned.get("stop") or {}
                        accepted_at = stop.get("accepted_at") if stop.get("accepted") else None
                        lost_at = (accepted_at if accepted_at is not None
                                   else self._owned["deadline"]) + LOST_TERMINAL_S
                        if self.ports.clock.now() >= lost_at:
                            self._lose_terminal()
                            await self._finish()
                        elif self.ports.clock.now() >= deadline + CLEANUP_GRACE_S:
                            self._cancel()
                            await self._release()
                            if accepted_at is None:
                                # An accepted stop is native teardown in progress, not a lost run.
                                self._update_day(blocked=True)
                            # Keep the old command identity until a terminal is
                            # received; elapsed time is not remote cleanup proof.
                        elif self.session is not None and not self._owned["cancelled"]:
                            await self.session.tick()
                    return
                if self._due():
                    await self._start()
            except asyncio.CancelledError:
                self._cancel()
                await self._release()
                raise
            except Exception:
                self._cancel()
                await self._release()
                raise
