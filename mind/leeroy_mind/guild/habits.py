"""Between-pull consumables with durable attempts and ticker-owned command waits.

Native class strategies handle class buffs. The generic playerbots ``buff``
action only lists items, so this listener explicitly uses its selected flask.
Event hooks record/invalidate work without waiting on the serial dispatcher.
"""

import asyncio
from dataclasses import asdict
from datetime import date, datetime
from uuid import uuid4

from leeroy_mind.clock import night_of
from leeroy_mind.protocol import Ack
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import is_raid_fight

from .boss_calls import parse_dc_status
from .lines import instruction, public_text, template
from .loot import _get, _once, _put
from .play import skips_flask
from .protocol import BotDo, Job, StockItems
from .raids import CATALOG
from .roles import is_cook, role_of, stat_family


FLASK_FOR = {"tank": 46379, "healer": 46376, "int": 46376, "agi": 46377, "str": 46377}
FEASTS = (43015, 34753)
FEAST_GAP_S = 600
RECOVERY_S = 60
COMMAND_TIMEOUT_S = 5
MAX_COMMANDS_PER_TICK = 8
MAX_EVENT_AGE_S = 60


class _Stale(Exception):
    pass


class _Budget(Exception):
    pass


class BetweenPulls:
    def __init__(self, ports, rng, *, gap):
        self.ports, self.rng, self.gap = ports, rng, gap
        self._run = None
        self._generation = 0
        self._revision = 0
        self._lock = asyncio.Lock()
        self._used = 0
        self.flask_skips = set()

    def _get(self, key):
        return _get(self.ports.conn, self._prefix + key)

    def _put(self, key, value):
        _put(self.ports.conn, self._prefix + key, value)

    def _save(self):
        with self.ports.conn:
            self._put("state", {"pull": self._pull, "ended": self._ended, "killed": sorted(self._killed),
                                "dead": self._dead, "resurrected": self._resurrected,
                                "recovery": self._recovery, "drink_seen": sorted(self._drink_seen)})

    def _valid(self):
        run = self._run
        if (run is None or self.ports.human_guid != self._human or run.session._closing
                or bool(self.ports.human_online()) == bool(run.plan.offline)
                or asdict(run.plan) != self._plan or asdict(run.session.plan) != self._plan
                or asdict(run.session.ctx.plan) != self._plan):
            return False
        ctx = run.session.ctx
        prefix = f"guild:raid:{self._raid}:"
        row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (self._raid,)).fetchone()
        return (row is not None and row[0] is None and ctx.raid_id == self._raid and ctx.map_id == self._map
                and type(ctx.instance_id) is int and 0 < ctx.instance_id <= 0xFFFFFFFF
                and ctx.instance_id == self._instance and _get(self.ports.conn, prefix + "instance") == self._instance
                and _get(self.ports.conn, prefix + "plan") == self._plan)

    def _eligible(self, guid, *, living=True):
        m = self.ports.cast().get(guid)
        return (guid in self._members and guid != self._human and m is not None and m.guid == guid
                and m.online and m.map == self._map and m.level >= 80 and guid not in self.ports.away()
                and self.ports.persona(guid) is not None and (not living or str(guid) not in self._dead))

    def _fingerprint(self, guid):
        m, p = self.ports.cast().get(guid), self.ports.persona(guid)
        return [m.class_, m.spec_tab, p.play.raid_role] if m is not None and p is not None else None

    def _fresh(self, ts):
        return (type(ts) is int and ts > 0 and self._run.session.started - 5 <= ts / 1000
                and -5 <= self.ports.clock.now() - ts / 1000 <= MAX_EVENT_AGE_S)

    def _check(self, token, members=(), *, cook=False):
        if token != (self._generation, self._revision) or not self._valid() or self._pull is not None:
            raise _Stale
        for guid, fingerprint in members:
            if (not self._eligible(guid) or self._fingerprint(guid) != fingerprint
                    or cook and not is_cook(self.ports.persona(guid))):
                raise _Stale

    def _reserve(self):
        if self._used >= MAX_COMMANDS_PER_TICK:
            raise _Budget
        self._used += 1

    async def on_start(self, run):
        if self._run is not None and self._raid == run.session.raid_id:
            return
        self._generation += 1
        self._revision += 1
        self._run, self._human = run, self.ports.human_guid
        self.flask_skips = set()
        self._raid, self._plan = run.session.raid_id, asdict(run.plan)
        self._map, self._instance = CATALOG[run.plan.raid_key].map_id, run.session.ctx.instance_id
        self._members = frozenset(run.plan.members)
        self._prefix = f"guild:habits:{self._raid}:"
        saved = self._get("state") or {}
        self._pull, self._ended = saved.get("pull"), saved.get("ended", {})
        self._killed = set(saved.get("killed", ()))
        self._dead, self._resurrected = saved.get("dead", {}), saved.get("resurrected", {})
        self._recovery, self._drink_seen = saved.get("recovery"), set(saved.get("drink_seen", ()))
        self._drink_pending = None
        if not self._valid():
            return
        self._night = self._get("night")
        if self._night is None:
            logical = datetime.fromtimestamp(run.session.started, tz=self.ports.clock.local().tzinfo)
            self._night = night_of(logical)
            with self.ports.conn:
                self._put("night", self._night)
        self._day = date.fromisoformat(self._night).toordinal()
        if self._get("flasks") is None:
            choices = {}
            with self.ports.conn:
                for guid in sorted(self._members):
                    if guid == self._human or self.ports.persona(guid) is None or guid not in self.ports.cast():
                        continue
                    p, m = self.ports.persona(guid), self.ports.cast()[guid]
                    key = f"guild:habits:skip:{self._night}:{guid}"
                    skip = _get(self.ports.conn, key)
                    if skip is None:
                        skip = bool(skips_flask(p, self.ports.persona_state(guid), self.rng))
                        _put(self.ports.conn, key, skip)
                    if not skip:
                        role = role_of(m.class_, m.spec_tab, p.play.raid_role)
                        category = role if role in {"tank", "healer"} else stat_family(m.class_, m.spec_tab, role)
                        choices[str(guid)] = {"item": FLASK_FOR[category], "fingerprint": self._fingerprint(guid)}
                self._put("flasks", choices)
        self.flask_skips = {guid for guid in self._members
            if _get(self.ports.conn, f"guild:habits:skip:{self._night}:{guid}") is True}

    async def on_ready(self, run):
        pass

    async def _stock(self, guid, item, key, token, fingerprint, *, cook=False):
        members = [(guid, fingerprint)]
        self._check(token, members, cook=cook)
        prior = self._get(key)
        if prior is not None:
            return prior.get("state") == "done" and prior.get("guid") == guid and prior.get("item") == item
        self._reserve()
        command = StockItems(uuid4().hex, guid, (item,), 2)
        receipt = self._prefix + key
        record = {"id": command.id, "guid": guid, "item": item, "state": "sending"}
        with self.ports.conn:
            _put(self.ports.conn, receipt, record)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                ack = await self.ports.send(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            ack = None
        valid = isinstance(ack, Ack) and ack.id == command.id and type(ack.ok) is bool
        record["state"] = ("done" if ack.ok else "failed") if valid else "unknown"
        with self.ports.conn:
            _put(self.ports.conn, receipt, record)
        self._check(token, members, cook=cook)
        return record["state"] == "done"

    async def _use(self, members, item, key, token, *, cook=False):
        self._check(token, members, cook=cook)
        guids = tuple(g for g, _ in members)
        if not guids:
            return False
        prior = self._get(key)
        if prior is not None:
            return prior.get("state") == "done" and prior.get("guids") == list(guids) and prior.get("item") == item
        self._reserve()
        command = BotDo(uuid4().hex, guids, "use", str(item))
        receipt = self._prefix + key
        issued = self.ports.clock.now()
        record = {"id": command.id, "guids": list(guids), "item": item, "state": "sending"}
        with self.ports.conn:
            _put(self.ports.conn, receipt, record)
        result = None
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                ack = await self.ports.send(command)
                self._check(token, members, cook=cook)
                if isinstance(ack, Ack) and ack.id == command.id and ack.ok is True:
                    result = await self.ports.wait_job(command.id, COMMAND_TIMEOUT_S)
        except (asyncio.CancelledError, _Stale):
            raise
        except Exception:
            pass
        success = (isinstance(result, Job) and result.id == command.id and result.kind == "bot_do"
                   and result.state == "done" and not result.skipped
                   and all(type(g) is int for g in result.guids) and len(result.guids) == len(guids)
                   and set(result.guids) == set(guids) and type(result.ts) is int
                   and issued - 5 <= result.ts / 1000 <= self.ports.clock.now() + 5)
        record["state"] = "done" if success else "unknown"
        with self.ports.conn:
            _put(self.ports.conn, receipt, record)
        self._check(token, members, cook=cook)
        return success

    def _selected(self, participants=None):
        choices = self._get("flasks") or {}
        return [(int(g), c) for g, c in sorted(choices.items(), key=lambda pair: int(pair[0]))
                if self._eligible(int(g)) and self._fingerprint(int(g)) == c["fingerprint"]
                and (participants is None or int(g) in participants)]

    async def _flasks(self, cycle, token, *, initial=False, participants=None):
        selected = self._selected(participants)
        if initial:
            for guid, choice in selected:
                await self._stock(guid, choice["item"], f"stock:{guid}", token, choice["fingerprint"])
        grouped = {}
        for guid, choice in self._selected(participants):
            stock = self._get(f"stock:{guid}")
            if stock is not None and stock.get("state") == "done" and stock.get("item") == choice["item"]:
                grouped.setdefault(choice["item"], []).append((guid, choice["fingerprint"]))
        for item, members in sorted(grouped.items()):
            await self._use(members, item, f"{cycle}:flask:{item}", token)

    async def _feast(self, cycle, token):
        self._check(token)
        plan_key = f"{cycle}:feast"
        plan = self._get(plan_key)
        if plan is None:
            now, last = self.ports.clock.now(), self._get("feast-attempt")
            if last is not None and now - last < FEAST_GAP_S:
                return
            cooks = [g for g in sorted(self._members) if self._eligible(g) and is_cook(self.ports.persona(g))]
            if not cooks:
                return
            if self._used >= MAX_COMMANDS_PER_TICK:
                raise _Budget
            cook = cooks[self._day % len(cooks)]
            plan = {"guid": cook, "fingerprint": self._fingerprint(cook), "item": FEASTS[0]}
            with self.ports.conn:
                self._put(plan_key, plan)
                self._put("feast-attempt", now)  # Unknown use outcomes also reserve the cooldown.
        guid, item, fp = plan["guid"], plan["item"], plan["fingerprint"]
        if not self._eligible(guid) or not is_cook(self.ports.persona(guid)) or self._fingerprint(guid) != fp:
            return
        if not await self._stock(guid, item, plan_key + ":stock", token, fp, cook=True):
            return
        if not await self._use([(guid, fp)], item, plan_key + ":use", token, cook=True):
            return
        self._check(token, [(guid, fp)], cook=True)
        if _once(self.ports.conn, self._prefix + plan_key + ":line"):
            self.ports.propose(guid, "raid", public_text(self.ports, instruction("feast_down", feast="Fish Feast")),
                               kind=TurnKind.ASIDE)

    async def tick(self):
        if self._lock.locked() or not self._valid() or self._pull is not None:
            return
        async with self._lock:
            token = (self._generation, self._revision)
            self._used = 0
            try:
                self._check(token)
                if self._recovery is not None and self.ports.clock.now() < self._recovery["due"]:
                    return
                if not self._get("initial-done"):
                    await self._flasks("initial", token, initial=True)
                    await self._feast("initial", token)
                    self._check(token)
                    with self.ports.conn:
                        self._put("initial-done", True)
                recovery = self._recovery
                if recovery is not None:
                    await self._flasks(recovery["key"], token, participants=recovery["members"])
                    await self._feast(recovery["key"], token)
                    self._check(token)
                    self._recovery = None
                    self._save()
                if self._drink_pending is not None:
                    pending, self._drink_pending = self._drink_pending, None
                    if pending["revision"] != self._revision or not self._fresh(pending["ts"]):
                        return
                    speaker = self._run.plan.lead_guid
                    if speaker == self._human:
                        speaker = self._run.plan.tank_guid
                    now = self.ports.clock.now()
                    if not self._eligible(speaker) or not self.gap.ok(now):
                        return
                    self.gap.mark(now)
                    async with asyncio.timeout(COMMAND_TIMEOUT_S):
                        await self.ports.say(speaker, "raid", public_text(self.ports, template("drink_up")))
            except (_Stale, _Budget, TimeoutError):
                return

    async def on_boss(self, ev):
        if (not self._valid() or not self._fresh(ev.ts) or ev.map != self._map or ev.instance_id != self._instance
                or type(ev.boss_id) is not int or not is_raid_fight(ev.map, ev.boss_id)
                or self._run.plan.tank_guid not in ev.members or not set(ev.members) <= self._members | {self._human}
                or ev.state not in {"engaged", "wiped", "killed"}):
            return
        boss = str(ev.boss_id)
        if ev.state == "engaged":
            if self._pull is not None or ev.boss_id in self._killed or ev.ts <= self._ended.get(boss, 0):
                return
            self._pull = {"boss": ev.boss_id, "ts": ev.ts, "members": sorted(set(ev.members) & self._members)}
            self._recovery = None
        else:
            if self._pull is None or self._pull["boss"] != ev.boss_id or ev.ts < self._pull["ts"]:
                return
            self._ended[boss] = ev.ts
            if ev.state == "wiped":
                self._recovery = {"key": f"wipe:{ev.boss_id}:{self._pull['ts']}:{ev.ts}",
                                  "due": ev.ts / 1000 + RECOVERY_S, "members": self._pull["members"]}
            else:
                self._killed.add(ev.boss_id)
                self._recovery = None
            self._pull = None
        self._revision += 1
        self._drink_pending = None
        self._save()

    async def on_death(self, ev):
        if (not self._valid() or not self._fresh(ev.ts) or ev.map != self._map or ev.guid not in self._members
                or ev.ts <= self._resurrected.get(str(ev.guid), 0)):
            return
        key = str(ev.guid)
        self._dead[key] = max(ev.ts, self._dead.get(key, 0))
        self._save()

    async def on_resurrect(self, ev):
        if not self._valid() or not self._fresh(ev.ts) or not self._eligible(ev.guid, living=False):
            return
        key = str(ev.guid)
        self._resurrected[key] = max(ev.ts, self._resurrected.get(key, 0))
        if self._dead.get(key, 0) <= self._resurrected[key]:
            self._dead.pop(key, None)
        self._save()

    async def on_dc_status(self, ev):
        if (not self._valid() or not self._fresh(ev.ts) or self._pull is not None
                or ev.tank_guid != self._run.plan.tank_guid or not ev.enabled or ev.paused):
            return
        state = parse_dc_status(ev.payload)
        if (state is None or state.state != "resting" or not state.next_entry or not state.next_name.strip()
                or state.next_entry in self._drink_seen):
            return
        self._drink_seen.add(state.next_entry)
        self._drink_pending = {"ts": ev.ts, "revision": self._revision}
        self._save()

    def invalidate(self, run):
        """Immediately retire only this run; safe in a serial logout/end hook."""
        if self._run is not run:
            return
        self._generation += 1
        self._revision += 1
        self._run = None
        self._drink_pending = None

    async def on_end(self, outcome, *, run=None):
        if run is not None and self._run is not run:
            return
        if self._run is not None:
            self.invalidate(self._run)
