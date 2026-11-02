"""Bounded roster maintenance; event hooks never wait for bridge replies.

Gear progress belongs to a persisted command, not merely to a familiar GUID.
An interrupted/unknown send keeps that ownership until actual evidence resolves
it. Friends require a read-only snapshot to distinguish our additions from the
owner's existing friends; AddFriend's idempotent ack alone cannot do that.
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

    async def _gear(self, targets, mode, ilvl, epoch):
        if not targets or self._get("gear") or not self._maintenance(epoch):
            return
        command = GearUp(uuid4().hex, tuple(targets), ilvl, self.cfg.level_to)
        record = {"id": command.id, "mode": mode, "guids": targets, "state": "sending", "completed": [], "seen": [],
                  "level": self.cfg.level_to, "minimum": ilvl - 6 if mode == "tc_geared"
                  else self.cfg.gear_up_below_avg_ilvl,
                  "snapshots": {str(g): self._snapshot(g) for g in targets}}
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
            return
        valid = isinstance(ack, Ack) and ack.id == command.id and type(ack.ok) is bool
        with self.ports.conn:
            if valid and not ack.ok and not current["seen"]:
                self._put("gear", None)
            else:
                current["state"] = "accepted" if valid and ack.ok else "unknown"
                self._put("gear", current)

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
            targets = gear_up_targets(current, averages, flags(self.ports.conn, "geared"), self.cfg)
            await self._gear(targets[:MAX_BATCH], "geared", self.cfg.gear_up_ilvl, epoch)

    async def maybe_gear_theorycrafters(self):
        async with self._lock:
            epoch = self._epoch
            if (not self.cfg.theorycrafter_ilvl or not self._maintenance(epoch) or self._get("gear")
                    or not self._due("theorycrafter")):
                return
            targets = theorycrafter_targets(self._cast(), self.ports.persona, flags(self.ports.conn, "tc_geared"))
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
                if ev.guid not in record["seen"]:
                    record["seen"].append(ev.guid)
                self._put("gear", record)
                if ev.level < record["level"] or ev.avg_ilvl < record["minimum"]:
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
