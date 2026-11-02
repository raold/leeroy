"""Bounded raid-lead actions, with receipts before any remote side effect.

The wiring ticks this listener and forwards Resurrect as well as RaidListener
events. Member has no alive/instance fields: known deaths remain excluded until
resurrection evidence; the game-side job checks actual ownership and castability.
"""

import asyncio
from dataclasses import asdict
import math
from uuid import uuid4

from leeroy_mind.wow import boss_name, faction_known, is_raid_fight

from .boss_calls import BOSS_CALLS, call_for, parse_dc_status
from .lines import member_name, public_text, template
from .loot import _get, _once, _put
from .protocol import BotDo
from .raids import CATALOG
from .roles import role_of


ACTION_TIMEOUT_S = 5.0
MAX_EVENT_AGE_S = 60.0


class CallGap:
    def __init__(self, clock, *, min_gap_s=20.0):
        if not math.isfinite(min_gap_s) or min_gap_s < 0:
            raise ValueError("invalid call gap")
        self.clock, self.min_gap_s = clock, min_gap_s
        self._last = None

    def ok(self, now) -> bool:
        return math.isfinite(now) and (self._last is None or now - self._last >= self.min_gap_s)

    def mark(self, now):
        if not math.isfinite(now):
            raise ValueError("invalid call time")
        self._last = now if self._last is None else max(now, self._last)


class RaidCalls:
    def __init__(self, ports, *, gap: CallGap):
        self.ports, self.gap = ports, gap
        self._run = None
        self._epoch = 0
        self._lock = asyncio.Lock()
        self._go_due = None
        self._dead = {}
        self._resurrected = {}
        self._pull = None
        self._ended = {}
        self._killed = set()
        self._last_pre = None

    def _save(self):
        with self.ports.conn:
            _put(self.ports.conn, self._prefix + "state", {
                "pull": self._pull, "ended": self._ended, "killed": sorted(self._killed),
                "dead": {str(g): ts for g, ts in self._dead.items()},
                "resurrected": {str(g): ts for g, ts in self._resurrected.items()}, "last_pre": self._last_pre})

    def _valid(self, epoch, *, instance=True):
        run = self._run
        if (run is None or epoch != self._epoch or run.lead_is_human
                or run.plan.lead_guid == self.ports.human_guid or self._human != self.ports.human_guid
                or bool(self.ports.human_online()) == bool(run.plan.offline)
                or run.session._closing or asdict(run.plan) != self._plan
                or asdict(run.session.plan) != self._plan):
            return False
        row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (self._raid_id,)).fetchone()
        ctx = run.session.ctx
        if (row is None or row[0] is not None or ctx.raid_id != self._raid_id
                or ctx.map_id != self._map or _get(self.ports.conn, f"guild:raid:{self._raid_id}:plan") != self._plan):
            return False
        if instance and (type(ctx.instance_id) is not int or not 0 < ctx.instance_id <= 0xFFFFFFFF
                         or ctx.instance_id != self._instance
                         or _get(self.ports.conn, f"guild:raid:{self._raid_id}:instance") != ctx.instance_id):
            return False
        return self._eligible(run.plan.lead_guid, living=False) and self._eligible(run.plan.tank_guid, living=False)

    def _eligible(self, guid, *, living=True):
        member = self.ports.cast().get(guid)
        return (guid in self._members and guid != self.ports.human_guid and member is not None
                and member.online and member.map == self._map and member.level >= 80
                and guid not in self.ports.away() and self.ports.persona(guid) is not None
                and (not living or guid not in self._dead))

    def _fresh(self, ts):
        return (type(ts) is int and ts > 0 and self._run.session.started - 5 <= ts / 1000
                and -5 <= self.ports.clock.now() - ts / 1000 <= MAX_EVENT_AGE_S)

    async def _say(self, text, epoch, *, key, urgent=False):
        if not self._valid(epoch):
            return False
        now = self.ports.clock.now()
        if not urgent and not self.gap.ok(now):
            return False
        if not _once(self.ports.conn, self._prefix + "line:" + key):
            return False
        # Reserve the shared gap before awaiting another listener's potential say.
        self.gap.mark(now)
        said = await self.ports.say(self._run.plan.lead_guid, "raid", public_text(self.ports, text))
        return said is True and self._valid(epoch)

    async def _action(self, guids, action, param, epoch, *, key, target=0):
        guids = tuple(guids)
        if (not guids or not self._valid(epoch) or not all(self._eligible(g) for g in guids)
                or target and (target not in self._members or not self._eligible(target, living=False))):
            return False
        receipt = self._prefix + "action:" + key
        if not _once(self.ports.conn, receipt):
            return False  # Includes cancelled, timed-out and otherwise uncertain earlier sends.
        command = BotDo(uuid4().hex, guids, action, param, target)
        issued = self.ports.clock.now()
        try:
            async with asyncio.timeout(ACTION_TIMEOUT_S):
                ack = await self.ports.send(command)
                if (not self._valid(epoch) or ack is None or ack.id != command.id or ack.ok is not True
                        or not all(self._eligible(g) for g in guids)):
                    return False
                result = await self.ports.wait_job(command.id, ACTION_TIMEOUT_S)
        except Exception:
            return False
        if (not self._valid(epoch) or not all(self._eligible(g) for g in guids)
                or target and not self._eligible(target, living=False) or result is None
                or result.id != command.id or result.kind != "bot_do" or result.state != "done"
                or result.skipped or len(result.guids) != len(guids) or set(result.guids) != set(guids)
                or type(result.ts) is not int or not issued - 5 <= result.ts / 1000 <= self.ports.clock.now() + 5):
            return False
        with self.ports.conn:
            _put(self.ports.conn, receipt, {"done": True})
        return True

    async def on_start(self, run):
        # A repeated start of the same raid never resets its attempted-action/death evidence.
        if self._run is not None and self._raid_id == run.session.raid_id:
            return
        self._epoch += 1
        self._run, self._human = run, self.ports.human_guid
        self._raid_id, self._plan = run.session.raid_id, asdict(run.plan)
        self._map = CATALOG[run.plan.raid_key].map_id
        self._instance = run.session.ctx.instance_id
        self._members = frozenset(run.plan.members)
        self._prefix = f"guild:calls:{self._raid_id}:"
        self._go_due = None
        state = _get(self.ports.conn, self._prefix + "state") or {}
        self._dead = {int(g): ts for g, ts in state.get("dead", {}).items()}
        self._resurrected = {int(g): ts for g, ts in state.get("resurrected", {}).items()}
        self._killed = set(state.get("killed", ()))
        self._pull, self._ended, self._last_pre = state.get("pull"), state.get("ended", {}), state.get("last_pre")
        self._roles = {}
        for guid in self._members:
            member, persona = self.ports.cast().get(guid), self.ports.persona(guid)
            if member is not None and persona is not None:
                self._roles[guid] = role_of(member.class_, member.spec_tab, persona.play.raid_role)
        epoch = self._epoch
        async with self._lock:
            if not self._valid(epoch):
                return
            tank = await self._action((run.plan.tank_guid,), "co", "+mark rti", epoch, key="mark-tank")
            damage = tuple(g for g in sorted(self._members) if self._roles.get(g) == "damage" and self._eligible(g))
            marks = await self._action(damage, "rti", "skull", epoch, key="mark-damage") if damage else False
            if tank and marks:
                await self._say(template("marks"), epoch, key="marks")
            warlock = self._caster(9)
            if warlock is not None and await self._action((warlock,), "nc", "+ss tank", epoch, key="soulstone"):
                await self._say(template("ss_on", name=member_name(self.ports, run.plan.tank_guid)), epoch, key="soulstone")

    def _caster(self, cls, *, participants=None):
        for guid in sorted(self._members):
            if (self._eligible(guid) and self.ports.cast()[guid].class_ == cls
                    and (participants is None or guid in participants)):
                return guid
        return None

    async def on_ready(self, run):
        epoch = self._epoch
        if run is not self._run or not self._valid(epoch):
            return
        async with self._lock:
            if await self._say(template("pull_countdown", seconds=5), epoch, key="countdown", urgent=True):
                self._go_due = (self.ports.clock.now() + 5, epoch)

    async def tick(self):
        if self._lock.locked() or self._go_due is None:
            return
        async with self._lock:
            due, epoch = self._go_due
            now = self.ports.clock.now()
            if now < due and self._valid(epoch):
                return
            self._go_due = None
            if due <= now <= due + 5 and self._pull is None:
                await self._say(template("pull_go"), epoch, key="go", urgent=True)

    async def on_dc_status(self, ev):
        epoch = self._epoch
        if (not self._valid(epoch) or ev.tank_guid != self._run.plan.tank_guid or not self._fresh(ev.ts)
                or self._pull is not None):
            return
        state = parse_dc_status(ev.payload)
        if state is None or state.state not in {"resting", "idle"} or not state.next_entry or not state.next_name.strip():
            return
        name, call = call_for(self._map, state.next_name)
        async with self._lock:
            if not self._valid(epoch) or not self._fresh(ev.ts) or name == self._last_pre or self._pull is not None:
                return
            # Drop crowded plans rather than queueing a stale plan for a later boss.
            self._last_pre = name
            self._save()
            await self._say(call.pre, epoch, key=f"pre:{state.next_entry}:{ev.ts}")

    async def on_boss(self, ev):
        epoch = self._epoch
        if (not self._valid(epoch) or not self._fresh(ev.ts) or ev.map != self._map
                or ev.instance_id != self._run.session.ctx.instance_id
                or type(ev.boss_id) is not int or not 0 <= ev.boss_id <= 1024
                or not is_raid_fight(ev.map, ev.boss_id) or self._run.plan.tank_guid not in ev.members
                or not set(ev.members) <= self._members | {self._human}
                or ev.state not in {"engaged", "killed", "wiped"}):
            return
        async with self._lock:
            if not self._valid(epoch) or not self._fresh(ev.ts):
                return
            boss = str(ev.boss_id)
            if ev.state != "engaged":
                if self._pull is None or self._pull["boss"] != ev.boss_id or ev.ts < self._pull["ts"]:
                    return
                self._ended[boss] = ev.ts
                if ev.state == "killed":
                    self._killed.add(ev.boss_id)
                self._pull = None
                self._save()
                return
            if self._pull is not None or ev.boss_id in self._killed or ev.ts <= self._ended.get(boss, 0):
                return
            self._pull = {"boss": ev.boss_id, "ts": ev.ts, "members": sorted(set(ev.members))}
            self._go_due = None
            self._save()
            call = BOSS_CALLS.get((ev.map, ev.boss_id), call_for(ev.map, boss_name(ev.map, ev.boss_id, ev.boss_name))[1])
            shaman = self._caster(7, participants=ev.members)
            if call.lust and shaman is not None:
                faction = faction_known(self.ports.cast()[shaman].race)
                key = f"pull:{ev.boss_id}:{ev.ts}:lust"
                if faction and await self._action((shaman,), "cast", "bloodlust" if faction == "horde" else "heroism", epoch, key=key):
                    await self._say(template("lust_now"), epoch, key=key, urgent=True)

    async def on_death(self, ev):
        epoch = self._epoch
        if (not self._valid(epoch) or not self._fresh(ev.ts) or ev.map != self._map
                or ev.guid not in self._members or ev.ts <= self._resurrected.get(ev.guid, 0)):
            return
        # Record immediately, even when another hook is waiting for a job.
        self._dead[ev.guid] = max(ev.ts, self._dead.get(ev.guid, 0))
        self._save()
        async with self._lock:
            if (not self._valid(epoch) or not self._fresh(ev.ts) or ev.guid not in self._dead
                    or self._pull is None or ev.ts < self._pull["ts"]
                    or ev.guid not in self._pull["members"]
                    or (ev.guid != self._run.plan.tank_guid and self._roles.get(ev.guid) not in {"tank", "healer"})):
                return
            druid = self._caster(11, participants=self._pull["members"])
            key = f"pull:{self._pull['boss']}:{self._pull['ts']}:brez"
            if druid is not None and await self._action((druid,), "cast", "rebirth", epoch, key=key, target=ev.guid):
                await self._say(template("brez", name=member_name(self.ports, ev.guid)), epoch, key=key, urgent=True)

    async def on_resurrect(self, ev):
        if (self._valid(self._epoch) and self._fresh(ev.ts) and self._eligible(ev.guid, living=False)):
            # Keep evidence even if its death has not arrived yet. Replayed death
            # packets must not undo a newer resurrection after listener recovery.
            self._resurrected[ev.guid] = max(ev.ts, self._resurrected.get(ev.guid, 0))
            if self._dead.get(ev.guid, 0) <= self._resurrected[ev.guid]:
                self._dead.pop(ev.guid, None)
            self._save()

    async def on_end(self, outcome):
        self._epoch += 1
        self._go_due = None
        self._run = None
        self._pull = None
        self._dead.clear()
        self._resurrected.clear()
