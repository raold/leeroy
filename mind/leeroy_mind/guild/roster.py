"""Bounded roster maintenance; event hooks never wait for bridge replies.

Gear progress belongs to a persisted command, not merely to a familiar GUID.
An interrupted/unknown send keeps that ownership until actual evidence resolves
it (its terminal, or a hello from a worldserver started after it was sent).
Friends require a read-only snapshot to distinguish our additions from the
owner's existing friends; AddFriend's idempotent ack alone cannot do that.

The regear ([guild] regear_to) takes the cast's bots down to an item level once
per value (meta guild:regear:<ilvl>), in the same windows as gear-up, through
gear_up's regear mode (native, LeeroyGearUpPolicy.h). Never the owner, never a
member outside the cast (under 80, rank 0, left the guild), never a character
native finds on a player's account. The regear alone removes gear above its
cap; its receipt is a band, ilvl - REGEAR_BELOW to ilvl + REGEAR_ABOVE. A native
whose hello lists "regear" runs every other gear-up as a capped incremental pass
that keeps the loot a bot holds, so their receipt is that floor and no ceiling.
A member the regear has not reached yet is left out of gear-up's passes; anyone
else is geared as usual while it runs.
"""

import asyncio
import inspect
import logging
import math
from uuid import uuid4
from weakref import WeakValueDictionary

from leeroy_mind.protocol import Ack, AddFriend, SetRank

from .armory import average_item_level, read_premade_specs
from .loot import _get, _put
from .protocol import GearUp, RemoveFriend, SetSpec
from .roles import premade_spec_no, role_of, spec_tab_for
from .store import flags


log = logging.getLogger(__name__)
HOUR = 3600
OFFLINE_DELAY = 600
FRIEND_DELAY = 120
FRIEND_PERIOD = 1800
MAX_SPECS = 10
MAX_BATCH = 40
REGEAR_FEATURE = "regear"  # mod-leeroy's hello: gear_up understands "regear"; other gear-ups keep loot, capped
REGEAR_LEVEL = 80          # native's cast: guild members at 80 and above, the owner excluded
REGEAR_GAP = 120           # seconds between regear batches
REGEAR_WAIT = 1800         # a member native skipped, or regeared out of bounds, waits this long
REGEAR_TRIES = 3           # regeared out of bounds (or failed) this often: given up, reported
REGEAR_BELOW = 20          # a regeared average may land this far under the target
REGEAR_ABOVE = 6           # and no further above it (a slot kept above the cap adds about 1)
REGEAR_DEADLINE = 3 * 86400  # members still pending this long after the start are given up, reported
REGEAR_DROP = frozenset({"owner", "not_cast", "not_bot"})  # native's refusals of who a member is
_LOCKS = WeakValueDictionary()


def _members(members):
    return members.values() if hasattr(members, "values") else members


def desired_rank(persona, ranks):
    if persona is None:
        return None
    rank = ranks.get(persona.play.guild_role)
    return rank if type(rank) is int and 1 <= rank <= 9 else None


def rank_changes(members, persona_of, human_guid, ranks):
    return [(m.guid, rank) for m in sorted(_members(members), key=lambda m: m.guid)
            if m.guid != human_guid and m.rank != 0
            and (rank := desired_rank(persona_of(m.guid), ranks)) is not None and rank != m.rank]


def gear_up_targets(members, avg_ilvl, geared, cfg):
    return [m.guid for m in sorted(_members(members), key=lambda m: m.guid)
            if m.online and m.rank != 0 and m.guid not in geared
            and (m.level < cfg.level_to or avg_ilvl.get(m.guid, 0) < cfg.gear_up_below_avg_ilvl)]


def regear_key(ilvl):
    return f"guild:regear:{ilvl}"


def regear_members(members, human_guid):
    """Whom a regear may take: the cast's members, never the owner, a rank-0 row or anyone under 80."""
    return sorted(m.guid for m in _members(members)
                  if m.guid != human_guid and m.rank != 0 and m.level >= REGEAR_LEVEL)


def regear_target(persona, cfg):
    """A theorycrafter stays a little ahead: theorycrafter_ilvl when that is above regear_to."""
    tc = cfg.theorycrafter_ilvl
    if tc and persona is not None and persona.game.thinks_as == "theorycrafter":
        return max(cfg.regear_to, tc)
    return cfg.regear_to


def regear_pending(state):
    done, closed = state["done"], set(state["gave_up"]) | set(state["dropped"])
    return [g for g in state["members"] if str(g) not in done and g not in closed]


def _regear_state(raw):
    """The stored regear, or None when it is not one this code wrote (then nothing runs)."""
    keys = {"ilvl": int, "state": str, "started": (int, float), "members": list, "done": dict,
            "kept": dict, "gave_up": list, "dropped": list, "tries": dict, "wait": dict}
    if not isinstance(raw, dict) or any(not isinstance(raw.get(k), t) for k, t in keys.items()):
        return None
    if raw["state"] not in {"running", "done"} or any(type(g) is not int for g in raw["members"]):
        return None
    return raw


def spec_changes(members, persona_of, premade):
    changes = []
    for m in sorted(_members(members), key=lambda m: m.guid):
        p = persona_of(m.guid)
        if not m.online or m.rank == 0 or p is None:
            continue
        tab = spec_tab_for(m.class_, p.play.main_spec, p.play.raid_role)
        spec = premade_spec_no(m.class_, p.play.main_spec, p.play.raid_role, premade)
        if tab is None or spec is None or m.spec_tab == tab:
            continue
        changed = role_of(m.class_, m.spec_tab, p.play.raid_role) != role_of(m.class_, tab, p.play.raid_role)
        changes.append((m.guid, spec, changed))
    return changes


def theorycrafter_targets(members, persona_of, flagged):
    return [m.guid for m in sorted(_members(members), key=lambda m: m.guid)
            if m.online and m.rank != 0 and m.guid not in flagged
            and (p := persona_of(m.guid)) is not None and p.game.thinks_as == "theorycrafter"]


def closeness(affinity, trust, familiarity):
    value = .5 * (affinity + 100) / 200 + .3 * trust / 100 + .2 * familiarity / 100
    return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0


def friends_plan(strength, current, *, cap, add, remove):
    """Plan only the managed set; unknown/current friends have no implied strength."""
    strength = {g: float(s) for g, s in strength.items() if math.isfinite(s)}
    current = set(current)
    removed = {g for g in current if strength.get(g, 0) < remove}
    kept = current - removed
    order = lambda g: (-strength.get(g, 0), g)
    strongest = sorted(kept, key=order)
    removed.update(strongest[max(0, cap):])
    kept -= removed
    additions = sorted((g for g, s in strength.items() if g not in current and s >= add), key=order)
    return additions[:max(0, cap - len(kept))], sorted(removed, key=order)


def _flag(conn, kind, guid, now, value=None):
    # Join a receipt transaction; store.set_flag intentionally commits on its own.
    conn.execute("INSERT INTO guild_flag(kind,guid,ts,value) VALUES (?,?,?,?) "
                 "ON CONFLICT(kind,guid) DO UPDATE SET ts=excluded.ts,value=excluded.value",
                 (kind, guid, now, value))


class Roster:
    def __init__(self, ports, cfg, armory, *, premade=None, friends_of=None, raid_active=None):
        self.ports, self.cfg, self.armory = ports, cfg, armory
        self.premade, self.friends_of = premade, friends_of
        self.raid_active = raid_active or (lambda: False)
        self._human = ports.human_guid
        self._prefix = f"guild:roster:{self._human}:"
        key = (id(ports.conn), self._human)
        self._lock = _LOCKS.get(key)
        if self._lock is None:
            self._lock = asyncio.Lock()
            _LOCKS[key] = self._lock
        self._present = ports.human_online()
        self._offline_since = None if self._present else ports.clock.now()
        self._friend_due = ports.clock.now() + FRIEND_DELAY if self._present else None
        self._epoch = 0
        self._ranks_due = True
        self._ticking = False
        self._regear_native = False  # the last hello listed "regear"
        self._started_ms = 0         # the last hello's worldserver start

    def _get(self, name):
        return _get(self.ports.conn, self._prefix + name)

    def _put(self, name, value):
        _put(self.ports.conn, self._prefix + name, value)

    def _cast(self):
        return {g: m for g, m in self.ports.cast().items()
                if g != self._human and m.guid == g and m.rank != 0}

    def _snapshot(self, guid):
        m, p = self._cast().get(guid), self.ports.persona(guid)
        if m is None or p is None:
            return None
        return [m.class_, p.play.main_spec, p.play.raid_role, p.game.thinks_as]

    def _same_owner(self):
        return self._human == self.ports.human_guid

    def _online(self, epoch):
        return self._same_owner() and epoch == self._epoch and self._present and self.ports.human_online()

    def _maintenance(self, epoch):
        return (self._same_owner() and epoch == self._epoch and not self._present
                and not self.ports.human_online() and self._offline_since is not None
                and self.ports.clock.now() - self._offline_since >= OFFLINE_DELAY
                and not self.raid_active()
                and self.ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone() is None)

    def _due(self, name, period=HOUR):
        now, last = self.ports.clock.now(), self._get("last:" + name)
        if last is not None and now - last < period:
            return False
        with self.ports.conn:
            self._put("last:" + name, now)
        return True

    async def on_hello(self, ev):
        self._ranks_due = True
        self._regear_native = REGEAR_FEATURE in getattr(ev, "features", ())
        started = getattr(ev, "started_ms", 0)
        self._started_ms = started if type(started) is int and started > 0 else 0
        record = self._get("gear") if self._same_owner() else None
        sent = record.get("started_ms") if isinstance(record, dict) else None
        if type(sent) is int and 0 < sent < self._started_ms:
            # The worldserver that took this job has restarted: the job, and any terminal, went with it.
            with self.ports.conn:
                self._put("gear", None)
            log.info("gear_job_released mode=%s reason=restart", record.get("mode"))
        self._presence(self.ports.human_online())

    async def on_login(self, ev):
        if ev.human and ev.guid == self._human:
            self._presence(True)

    async def on_logout(self, ev):
        if ev.human and ev.guid == self._human:
            self._presence(False)

    async def on_personas_changed(self):
        self._ranks_due = True

    def _presence(self, online):
        if online == self._present:
            return
        self._epoch += 1
        self._present = online
        now = self.ports.clock.now()
        self._offline_since = None if online else now
        self._friend_due = now + FRIEND_DELAY if online else None

    async def tick(self):
        if self._ticking or not self._same_owner():
            return
        self._ticking = True
        try:
            self._presence(self.ports.human_online())
            if self._ranks_due:
                await self.sync_ranks()
            if self._maintenance(self._epoch):
                await self.sync_specs()
                await self.maybe_regear()
                await self.maybe_gear_up()
                await self.maybe_gear_theorycrafters()
            if (self._friend_due is not None and self.ports.clock.now() >= self._friend_due
                    and self._online(self._epoch)):
                await self.sync_friends()
        finally:
            self._ticking = False

    async def _send(self, command, name, details, *, on_ok=None):
        """Persist before await. Cancellation/invalid reply never authorizes another send."""
        prior = self._get(name)
        if prior and prior.get("state") in {"sending", "unknown"}:
            return None
        record = {**details, "id": command.id, "state": "sending"}
        with self.ports.conn:
            self._put(name, record)
        try:
            ack = await self.ports.send(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            ack = None
        valid = isinstance(ack, Ack) and ack.id == command.id and type(ack.ok) is bool
        record["state"] = ("ok" if ack.ok else "rejected") if valid else "unknown"
        with self.ports.conn:
            self._put(name, record)
            if valid and ack.ok and on_ok is not None:
                on_ok()
        return ack if valid else None

    async def sync_ranks(self):
        async with self._lock:
            if not self._same_owner():
                return
            epoch = self._epoch
            for guid, m in self._cast().items():
                name = f"rank:{guid}"
                record = self._get(name)
                if record and record.get("state") in {"sending", "unknown"} and m.rank == record.get("rank"):
                    with self.ports.conn:
                        record["state"] = "observed"
                        self._put(name, record)
            changes = rank_changes(self._cast(), self.ports.persona, self._human, self.cfg.ranks)
            self._ranks_due = len(changes) > MAX_BATCH
            for guid, rank in changes[:MAX_BATCH]:
                if not self._same_owner() or epoch != self._epoch:
                    self._ranks_due = True
                    break
                m = self._cast().get(guid)
                if m is None or m.rank == rank or desired_rank(self.ports.persona(guid), self.cfg.ranks) != rank:
                    continue
                await self._send(SetRank(uuid4().hex, guid, rank), f"rank:{guid}", {"rank": rank})

    def _role_queue(self):
        return self._get("role-gear") or {}

    def _queue_role(self, guid, snapshot):
        queue = self._role_queue()
        queue[str(guid)] = snapshot
        self._put("role-gear", queue)

    def _reconcile_specs(self):
        for guid, m in self._cast().items():
            name = f"spec:{guid}"
            record = self._get(name)
            if (not record or record.get("state") not in {"sending", "unknown", "ok"}
                    or m.spec_tab != record.get("tab") or not record.get("snapshot")
                    or m.class_ != record["snapshot"][0]):
                continue
            # A later cast snapshot proves the requested tab exists. No ack-only flag is invented.
            with self.ports.conn:
                record["state"] = "observed"
                self._put(name, record)
                if record.get("role_changed") and self._snapshot(guid) == record["snapshot"]:
                    self._queue_role(guid, record["snapshot"])

    async def sync_specs(self):
        async with self._lock:
            epoch = self._epoch
            if not self._maintenance(epoch) or self._get("gear"):
                return
            self._reconcile_specs()
            if not self._due("spec"):
                return
            try:
                premade = self.premade
                if premade is None:
                    path = self.cfg.worldserver_conf.parent / "modules" / "playerbots.conf"
                    premade = await asyncio.to_thread(read_premade_specs, path)
            except Exception:
                log.warning("guild roster premade configuration unavailable")
                return
            if not self._maintenance(epoch):
                return
            attempts = 0
            for guid, spec, changed in spec_changes(self._cast(), self.ports.persona, premade):
                if attempts >= MAX_SPECS or not self._maintenance(epoch):
                    break
                m = self._cast().get(guid)
                fresh = spec_changes([m], self.ports.persona, premade) if m is not None else []
                if not fresh:
                    continue
                _, spec, changed = fresh[0]
                record = self._get(f"spec:{guid}")
                if record and (record.get("state") in {"sending", "unknown"}
                               or record.get("state") == "ok" and record.get("spec") == spec
                               and record.get("snapshot") == self._snapshot(guid)):
                    continue
                snapshot = self._snapshot(guid)
                m, p = self._cast().get(guid), self.ports.persona(guid)
                if m is None or snapshot is None:
                    continue
                tab = spec_tab_for(m.class_, p.play.main_spec, p.play.raid_role)
                attempts += 1
                def accepted():
                    if self._same_owner() and self._snapshot(guid) == snapshot:
                        _flag(self.ports.conn, "spec", guid, self.ports.clock.now(), spec)
                        if changed:
                            self._queue_role(guid, snapshot)
                await self._send(SetSpec(uuid4().hex, guid, spec, True), f"spec:{guid}",
                                 {"spec": spec, "tab": tab, "snapshot": snapshot, "role_changed": changed},
                                 on_ok=accepted)
            if self._maintenance(epoch):
                await self._gear_roles(epoch)

    async def _gear_roles(self, epoch):
        queue = self._role_queue()
        members = self._cast()
        targets = [int(g) for g, snapshot in sorted(queue.items(), key=lambda item: int(item[0]))
                   if int(g) in members and members[int(g)].online and self._snapshot(int(g)) == snapshot]
        if targets:
            await self._gear(targets[:MAX_SPECS], "role", self.cfg.gear_up_ilvl, epoch)

    async def _gear(self, targets, mode, ilvl, epoch, *, regear=None):
        if not targets or self._get("gear") or not self._maintenance(epoch):
            return None
        command = GearUp(uuid4().hex, tuple(targets), ilvl, self.cfg.level_to, regear=regear is not None)
        record = {"id": command.id, "mode": mode, "guids": targets, "state": "sending", "completed": [], "seen": [],
                  "level": self.cfg.level_to, "minimum": ilvl - 6 if mode == "tc_geared"
                  else self.cfg.gear_up_below_avg_ilvl,
                  "snapshots": {str(g): self._snapshot(g) for g in targets}, "started_ms": self._started_ms}
        if regear is not None:
            # The regear lands near its ilvl, never above it.
            record.update(regear=regear, ilvl=ilvl, minimum=ilvl - REGEAR_BELOW, maximum=ilvl + REGEAR_ABOVE)
        elif self._regear_native:
            # A capped incremental gear-up keeps the loot a bot holds: a floor, no ceiling.
            record.update(ilvl=ilvl, minimum=ilvl - REGEAR_BELOW)
        with self.ports.conn:
            self._put("gear", record)
        try:
            ack = await self.ports.send(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            ack = None
        # on_job can run during send; never replace its recorded progress or resurrect a completed job.
        current = self._get("gear")
        if current is None or current["id"] != command.id:
            return None
        valid = isinstance(ack, Ack) and ack.id == command.id and type(ack.ok) is bool
        with self.ports.conn:
            if valid and not ack.ok and not current["seen"]:
                self._put("gear", None)
            else:
                current["state"] = "accepted" if valid and ack.ok else "unknown"
                self._put("gear", current)
        return ack if valid else None

    def _regear_pending(self):
        """Members a running regear has yet to reach: gear-up's passes leave them to it, and only them."""
        if not self.cfg.regear_to or not self._regear_native:
            return set()
        state = _regear_state(_get(self.ports.conn, regear_key(self.cfg.regear_to)))
        return set(regear_pending(state)) if state is not None and state["state"] == "running" else set()

    async def maybe_regear(self):
        target = self.cfg.regear_to
        if not target or not self._regear_native:
            return
        async with self._lock:
            epoch = self._epoch
            if not self._maintenance(epoch) or self._get("gear"):
                return
            conn, now, key = self.ports.conn, self.ports.clock.now(), regear_key(target)
            raw = _get(conn, key)
            state = _regear_state(raw)
            if raw is not None and state is None:
                if not getattr(self, "_regear_bad", False):
                    self._regear_bad = True
                    log.warning("regear_state_unreadable ilvl=%d", target)
                return
            if state is not None and state["state"] == "done":
                return
            if state is None:
                members = regear_members(self._cast(), self._human)
                state = {"ilvl": target, "state": "running", "started": now, "finished": None,
                         "members": members, "done": {}, "kept": {}, "gave_up": [], "dropped": [],
                         "tries": {}, "wait": {}}
                log.info("regear_started ilvl=%d members=%d", target, len(members))
                if not members:  # nobody eligible: nothing to do, and nothing holds gear-up
                    self._regear_finish(state, now)
                with conn:
                    _put(conn, key, state)
                if not members:
                    return
            if not self._due("regear", REGEAR_GAP):
                return
            cast = self._cast()
            for guid in regear_pending(state):
                m = cast.get(guid)
                if m is None or m.level < REGEAR_LEVEL or guid == self._human:
                    state["dropped"].append(guid)  # left the cast: the regear never reaches past it
                    log.info("regear_dropped guid=%d reason=not_cast", guid)
                elif state["tries"].get(str(guid), 0) >= REGEAR_TRIES:
                    state["gave_up"].append(guid)
                    # Its gear is whatever the last pass left: gear-up looks at it again, by its average.
                    with conn:
                        conn.execute("DELETE FROM guild_flag WHERE kind IN ('geared','tc_geared') AND guid=?",
                                     (guid,))
                    log.info("regear_gave_up guid=%d reason=tries tries=%d", guid, state["tries"][str(guid)])
                elif now - state["started"] >= REGEAR_DEADLINE:
                    state["gave_up"].append(guid)  # never reached in time (offline, grouped, held): reported
                    log.info("regear_gave_up guid=%d reason=deadline", guid)
            pending = regear_pending(state)
            if not pending:
                self._regear_finish(state, now)
                with conn:
                    _put(conn, key, state)
                return
            ready = [g for g in pending if cast[g].online and state["wait"].get(str(g), 0) <= now]
            batch = []
            if ready:
                ilvl = regear_target(self.ports.persona(ready[0]), self.cfg)
                batch = [g for g in ready if regear_target(self.ports.persona(g), self.cfg) == ilvl][:MAX_BATCH]
            with conn:
                _put(conn, key, state)
            if not batch:
                return
            log.info("regear_batch ilvl=%d guids=%d pending=%d", ilvl, len(batch), len(pending))
            ack = await self._gear(batch, "regear", ilvl, epoch, regear=key)
            reason, _, named = (ack.error if ack is not None and not ack.ok else "").partition(":")
            if reason in REGEAR_DROP and named.isdigit() and int(named) in batch:
                # Native refused the whole batch for who one member is; nothing changed. Leave that one out.
                with conn:
                    state = _regear_state(_get(conn, key))
                    if state is not None and int(named) not in state["dropped"]:
                        state["dropped"].append(int(named))
                        _put(conn, key, state)
                log.info("regear_dropped guid=%s reason=%s", named, reason)

    def _regear_finish(self, state, now):
        state["state"], state["finished"] = "done", now
        averages = list(state["done"].values())
        log.info("regear_done ilvl=%d guids=%d avg=%d gave_up=%d dropped=%d kept=%d", state["ilvl"], len(averages),
                 round(sum(averages) / len(averages)) if averages else 0, len(state["gave_up"]),
                 len(state["dropped"]), sum(state["kept"].values()))

    def _regear_progress(self, record, ev, first):
        """In the gear receipt's transaction: one member's regear landed in bounds, or it did not."""
        conn, now = self.ports.conn, self.ports.clock.now()
        state = _regear_state(_get(conn, record["regear"]))
        if state is None or state["state"] != "running" or ev.guid not in state["members"]:
            return
        if str(ev.guid) in state["done"] or not first:
            return
        if ev.level >= record["level"] and record["minimum"] <= ev.avg_ilvl <= record["maximum"]:
            # The regear supersedes the gear-up that set the old flags; a theorycrafter is now at their level.
            conn.execute("DELETE FROM guild_flag WHERE kind IN ('geared','tc_geared') AND guid=?", (ev.guid,))
            _flag(conn, "geared", ev.guid, now, record["ilvl"])
            snapshot = record["snapshots"].get(str(ev.guid)) or [None] * 4
            if snapshot[3] == "theorycrafter":
                _flag(conn, "tc_geared", ev.guid, now, record["ilvl"])
            state["done"][str(ev.guid)] = ev.avg_ilvl
            kept = ev.kept if type(ev.kept) is int and ev.kept > 0 else 0
            if kept:
                state["kept"][str(ev.guid)] = kept
            if ev.guid not in record["completed"]:
                record["completed"].append(ev.guid)
            log.info("regear_member guid=%d ilvl=%d avg=%d kept=%d", ev.guid, record["ilvl"], ev.avg_ilvl, kept)
        else:
            state["tries"][str(ev.guid)] = state["tries"].get(str(ev.guid), 0) + 1
            state["wait"][str(ev.guid)] = now + REGEAR_WAIT
            log.info("regear_out_of_bounds guid=%d ilvl=%d avg=%d", ev.guid, record["ilvl"], ev.avg_ilvl)
        _put(conn, record["regear"], state)
        self._put("gear", record)

    def _regear_terminal(self, record, ev):
        """In the terminal's transaction: members native skipped wait before they are sent again."""
        conn, now = self.ports.conn, self.ports.clock.now()
        state = _regear_state(_get(conn, record["regear"]))
        if state is None or state["state"] != "running":
            return
        for skip in ev.skipped:
            key = str(skip.guid)
            if key in state["done"] or skip.guid not in state["members"]:
                continue
            if skip.reason in REGEAR_DROP:
                if skip.guid not in state["dropped"]:
                    state["dropped"].append(skip.guid)
                continue
            if skip.reason == "failed":
                state["tries"][key] = state["tries"].get(key, 0) + 1
            if skip.reason != "owner_online":  # his login: the next window takes them at once
                state["wait"][key] = now + REGEAR_WAIT
        _put(conn, record["regear"], state)
        log.info("regear_batch_done ilvl=%d done=%d skipped=%d reason=%s", record["ilvl"], len(ev.guids),
                 len(ev.skipped), ev.reason or "-")

    async def maybe_gear_up(self):
        async with self._lock:
            epoch = self._epoch
            if not self._maintenance(epoch) or self._get("gear") or not self._due("gear"):
                return
            members = self._cast()
            before = {g: self._snapshot(g) for g in members}
            try:
                equipped = await asyncio.to_thread(self.armory.equipped, tuple(members))
            except Exception:
                log.warning("guild roster equipment query unavailable")
                return
            if not self._maintenance(epoch):
                return
            current = {g: m for g, m in self._cast().items()
                       if g in before and self._snapshot(g) == before[g]}
            averages = {g: average_item_level(equipped.get(g, {})) for g in current}
            held = self._regear_pending()
            targets = [g for g in gear_up_targets(current, averages, flags(self.ports.conn, "geared"), self.cfg)
                       if g not in held]
            await self._gear(targets[:MAX_BATCH], "geared", self.cfg.gear_up_ilvl, epoch)

    async def maybe_gear_theorycrafters(self):
        async with self._lock:
            epoch = self._epoch
            if (not self.cfg.theorycrafter_ilvl or not self._maintenance(epoch) or self._get("gear")
                    or not self._due("theorycrafter")):
                return
            held = self._regear_pending()
            targets = [g for g in theorycrafter_targets(self._cast(), self.ports.persona,
                                                        flags(self.ports.conn, "tc_geared")) if g not in held]
            await self._gear(targets[:MAX_BATCH], "tc_geared", self.cfg.theorycrafter_ilvl, epoch)

    async def on_job(self, ev):
        """Synchronous DB work only: a serial event dispatcher must not await a command here."""
        record = self._get("gear")
        if not self._same_owner() or not record or ev.kind != "gear_up" or ev.id != record["id"]:
            return
        wanted = set(record["guids"])
        if ev.state == "progress":
            if ev.guid in record["completed"]:
                return
            if (type(ev.guid) is not int or ev.guid not in wanted or type(ev.level) is not int
                    or type(ev.avg_ilvl) is not int or not 0 <= ev.avg_ilvl <= 1000 or not 1 <= ev.level <= 255
                    or ev.guid not in self._cast()
                    or self._snapshot(ev.guid) != record["snapshots"].get(str(ev.guid))):
                return
            with self.ports.conn:
                first = ev.guid not in record["seen"]
                if first:
                    record["seen"].append(ev.guid)
                self._put("gear", record)
                if record["mode"] == "regear":
                    self._regear_progress(record, ev, first)
                    return
                if (ev.level < record["level"] or ev.avg_ilvl < record["minimum"]
                        or ev.avg_ilvl > record.get("maximum", ev.avg_ilvl)):
                    return
                kind = "tc_geared" if record["mode"] == "tc_geared" else "geared"
                _flag(self.ports.conn, kind, ev.guid, self.ports.clock.now())
                if ev.guid not in record["completed"]:
                    record["completed"].append(ev.guid)
                self._put("gear", record)
                if record["mode"] == "role":
                    queue = self._role_queue()
                    if queue.get(str(ev.guid)) == record["snapshots"][str(ev.guid)]:
                        queue.pop(str(ev.guid), None)
                        self._put("role-gear", queue)
        elif ev.state in {"done", "failed", "aborted"}:
            done, skipped = list(ev.guids), [s.guid for s in ev.skipped]
            if (any(type(g) is not int for g in done + skipped) or len(done) != len(set(done))
                    or len(skipped) != len(set(skipped)) or set(done) & set(skipped)
                    or set(done) | set(skipped) != wanted):
                return
            with self.ports.conn:
                self._put("gear", None)
                if record["mode"] == "regear":
                    self._regear_terminal(record, ev)

    async def _friends(self):
        if self.friends_of is None:
            return None
        try:
            if inspect.iscoroutinefunction(self.friends_of):
                result = await self.friends_of()
            else:
                result = await asyncio.to_thread(self.friends_of)
                if inspect.isawaitable(result):
                    result = await result
            if not isinstance(result, (set, frozenset, list, tuple)) or any(
                    type(g) is not int or not 0 < g <= 0xFFFFFFFF for g in result):
                return None
            return set(result)
        except Exception:
            log.warning("guild roster friend snapshot unavailable")
            return None

    def _friend_plan(self, actual):
        # The global flag key is (kind,guid); value identifies whose friend we added.
        # Legacy/unattributed flags are not evidence of ownership for any human.
        owned = {row[0] for row in self.ports.conn.execute(
            "SELECT guid FROM guild_flag WHERE kind='friend' AND value=?", (self._human,))} - {self._human}
        strength = {}
        for guid in self._cast():
            if actual is not None and guid in actual and guid not in owned:
                continue  # Never adopt a friend the owner added independently.
            rel = self.ports.relationship(guid, self._human)
            strength[guid] = closeness(rel.affinity, rel.trust, rel.familiarity)
        cap = self.cfg.friends_cap if actual is None else max(0, self.cfg.friends_cap - len(actual - owned))
        add, remove = friends_plan(strength, owned, cap=cap, add=self.cfg.friends_add, remove=self.cfg.friends_remove)
        # Human and rank-zero members remain untouched, including flags inherited from earlier versions.
        protected = {g for g, m in self.ports.cast().items() if g == self._human or m.rank == 0}
        return ([] if actual is None else add), [g for g in remove if g not in protected]

    async def sync_friends(self):
        async with self._lock:
            epoch = self._epoch
            if not self._online(epoch) or self._friend_due is None or self.ports.clock.now() < self._friend_due:
                return
            self._friend_due = self.ports.clock.now() + FRIEND_PERIOD
            attempted = set()
            for _ in range(MAX_BATCH):
                actual = await self._friends()
                if not self._online(epoch):
                    break
                additions, removals = self._friend_plan(actual)
                choices = [("remove", g) for g in removals] + [("add", g) for g in additions]
                choices = [(action, g) for action, g in choices if (action, g) not in attempted]
                if not choices:
                    break
                action, guid = choices[0]
                attempted.add((action, guid))
                # This snapshot is the last await before send; absence is required before claiming an add.
                if action == "add" and (actual is None or guid in actual):
                    continue
                command = AddFriend(uuid4().hex, guid, "") if action == "add" else RemoveFriend(uuid4().hex, guid)
                # The ack records an actual owned action even if logout happened while waiting for it.
                def accepted():
                    if not self._same_owner():
                        return
                    if action == "add":
                        _flag(self.ports.conn, "friend", guid, self.ports.clock.now(), self._human)
                    else:
                        self.ports.conn.execute("DELETE FROM guild_flag WHERE kind='friend' AND guid=? AND value=?",
                                                (guid, self._human))
                await self._send(command, f"friend-{action}:{guid}", {"guid": guid}, on_ok=accepted)
