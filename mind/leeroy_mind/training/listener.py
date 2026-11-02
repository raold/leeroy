"""Guild-validated raid events; command work is handed to the training lane."""

from leeroy_mind.guild.boss_calls import call_for, parse_dc_status
from leeroy_mind.wow import BOSS_NAMES


class TrainingListener:
    def __init__(self, ports, ledger, gate, applier=None, *, submit=None):
        self.ports, self.ledger, self.gate, self.applier = ports, ledger, gate, applier
        self.submit = submit
        self.run = None
        self._muster = None
        self._pull = False

    def set_applier(self, applier):
        self.applier = applier

    async def _call(self, name, *args, **kwargs):
        if self.submit is not None:
            self.submit(name, args, kwargs, self.run)
        else:
            await getattr(self.gate, name)(*args, **kwargs)

    async def on_start(self, run):
        if self.run is run:
            return
        if self.run is not None:
            self.invalidate(self.run)
        self.run, self._muster, self._pull = run, None, False
        self.ledger.set_run(run)
        await self._call("on_start", run)

    def invalidate(self, run):
        self.gate.invalidate(run)
        if self.run is run:
            self.run = None
            self.ledger.set_run(None)
            self._muster, self._pull = None, False

    async def on_end(self, outcome, *, run=None):
        ended = run if run is not None else self.run
        if self.run is not None and ended is not self.run:
            return
        self.invalidate(ended)
        await self._call("on_end", outcome, run=ended)

    async def on_ready(self, run):
        pass

    async def on_death(self, ev):
        pass

    def _fresh(self, ev):
        return (self.run is not None and not getattr(self.run.session, "_closing", False)
            and type(ev.ts) is int and self.run.session.started * 1000 <= ev.ts
            and 0 <= self.ports.clock.now() * 1000 - ev.ts <= 60_000)

    async def on_dc_status(self, ev):
        if (not self._fresh(ev) or self._pull or ev.tank_guid != self.run.plan.tank_guid
                or not ev.enabled or ev.paused):
            return
        state = parse_dc_status(ev.payload)
        if state is None or state.state != "resting" or not state.next_entry:
            return
        map_id = self.run.session.ctx.map_id
        name, _ = call_for(map_id, state.next_name)
        indexes = [index for (map_, index), known in BOSS_NAMES.items() if map_ == map_id and known == name]
        if len(indexes) != 1:
            return  # Creature entries are not encounter indexes; unknown names fail closed.
        key = (indexes[0], state.next_entry)
        if key != self._muster:
            self._muster = key
            await self._call("on_muster", self.run, indexes[0])

    async def on_boss(self, ev):
        if (not self._fresh(ev) or ev.map != self.run.session.ctx.map_id
                or ev.instance_id != self.run.session.ctx.instance_id
                or not set(self.run.plan.members).intersection(ev.members)
                or ev.state not in {"engaged", "killed", "wiped"}):
            return
        self._pull = ev.state == "engaged"
        if not self._pull:
            self._muster = None  # Same-boss retries require a new resting boundary.
        await self._call("on_boss", ev)
