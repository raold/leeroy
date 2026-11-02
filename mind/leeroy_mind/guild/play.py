"""Mild raid positioning and cooldown habits, with owned boost compensation."""

import asyncio
from dataclasses import asdict, dataclass, replace
import logging
import math
from uuid import uuid4

from .protocol import BotDo
from .loot import _get, _put
from .roles import reliability, role_of


log = logging.getLogger(__name__)
RANGED_FORMATION: str | None = None
CHECK_EVERY_S = 300
JOB_TIMEOUT_S = 15
RESTORE_RETRY_S = 60
MAX_RESTORE_ATTEMPTS = 3


def spacing_actions(roles: dict[int, str], raid_roles: dict[int, str]) -> list[tuple[str, str, list[int]]]:
    if RANGED_FORMATION not in {None, "circle", "chaos"}:
        raise ValueError("unapproved ranged formation")
    guids = sorted(g for g in roles if type(g) is int and g > 0)
    actions = []
    for action, param, members in (
        ("co", "+behind", [g for g in guids if roles[g] == "damage" and raid_roles.get(g) == "melee"]),
        ("co", "+tank face", [g for g in guids if roles[g] == "tank"]),
        ("formation", RANGED_FORMATION, [g for g in guids if roles[g] == "healer"
                                         or roles[g] == "damage" and raid_roles.get(g) == "ranged"]),
    ):
        if param is not None and members:
            actions.append((action, param, members))
    return actions


def summon_waves(members, personas, rng, late_s):
    """Stable GUID order makes a seeded wave independent of roster iteration."""
    lo, hi = late_s
    if any(type(n) not in (int, float) or not math.isfinite(n) for n in (lo, hi)) or not 0 <= lo <= hi <= 600:
        raise ValueError("invalid late summon range")
    guids = sorted(set(members))
    if any(type(g) is not int or g <= 0 for g in guids):
        raise ValueError("invalid member GUID")
    on_time, late = [], []
    for guid in guids:
        persona = personas.get(guid)
        if persona is not None and reliability(persona) <= 3:
            late.append((guid, rng.uniform(lo, hi)))
        else:
            on_time.append(guid)
    return on_time, late


def drunk(state) -> bool:
    return state.altered_state == "drunk"


def skips_flask(persona, state, rng) -> bool:
    if persona.game.thinks_as == "theorycrafter" or persona.play.skill == "clutch":
        return False
    return (persona.play.skill == "sloppy" or drunk(state)) and rng.random() < 0.5


@dataclass(frozen=True)
class _Boost:
    raid_id: int
    fingerprint: tuple
    token: str
    needs_remove: bool = False
    restore_attempts: int = 0
    retry_at: float = 0


class Play:
    def __init__(self, ports, rng):
        self.ports, self.rng = ports, rng
        self._human = ports.human_guid
        self._run = None
        self._generation = 0
        self._initial = False
        self._next_check = 0
        self._owned: dict[int, _Boost] = self._load_owned()
        self._restoring = set()
        self._lock = asyncio.Lock()

    def _receipt_key(self):
        return f"guild:play:boost:{self._human}"

    def _load_owned(self):
        restored = {}
        saved = _get(self.ports.conn, self._receipt_key()) or {}
        if not isinstance(saved, dict):
            return restored
        for raw_guid, value in saved.items():
            try:
                guid = int(raw_guid)
                record = _Boost(**{**value, "fingerprint": tuple(value["fingerprint"])})
                if (guid > 0 and guid != self._human and type(record.raid_id) is int and record.raid_id > 0
                        and len(record.fingerprint) == 3 and type(record.restore_attempts) is int
                        and 0 <= record.restore_attempts <= MAX_RESTORE_ATTEMPTS
                        and type(record.retry_at) in (int, float) and math.isfinite(record.retry_at)):
                    restored[guid] = record
            except (TypeError, ValueError, KeyError):
                continue
        return restored

    def _save_owned(self):
        with self.ports.conn:
            _put(self.ports.conn, self._receipt_key(), {str(g): asdict(record) for g, record in self._owned.items()})

    def _active(self, run):
        row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (run.session.raid_id,)).fetchone()
        return row is not None and row[0] is None

    def _valid_run(self, run):
        session = run.session
        instance = session.ctx.instance_id
        prefix = f"guild:raid:{session.raid_id}:"
        return (self._human == self.ports.human_guid
                and bool(self.ports.human_online()) != bool(run.plan.offline)
                and self._active(run) and run.plan == session.plan == session.ctx.plan
                and session.ctx.raid_id == session.raid_id
                and type(instance) is int and 0 < instance <= 0xFFFFFFFF
                and _get(self.ports.conn, prefix + "instance") == instance
                and _get(self.ports.conn, prefix + "plan") == asdict(run.plan))

    def _fingerprint(self, guid):
        if self._human != self.ports.human_guid:
            return None
        member = self.ports.cast().get(guid)
        persona = self.ports.persona(guid)
        if guid == self.ports.human_guid or member is None or persona is None or not member.online:
            return None
        return member.class_, member.spec_tab, persona.play.raid_role

    def _eligible(self, run):
        if not self._valid_run(run):
            return {}
        cast = self.ports.cast()
        return {g: self._fingerprint(g) for g in sorted(set(run.plan.members))
                if g != self.ports.human_guid and g in cast and cast[g].map == run.session.ctx.map_id
                and self._fingerprint(g) is not None}

    def _other_raid(self, guid, raid_id):
        return self.ports.conn.execute(
            "SELECT 1 FROM guild_attendance a JOIN guild_raid r USING(raid_id) "
            "WHERE a.guid=? AND a.status='raided' AND r.ended IS NULL AND r.raid_id<>?",
            (guid, raid_id)).fetchone() is not None

    async def on_start(self, run):
        if self._human != self.ports.human_guid:
            # A newly configured owner has a separate compensation journal.
            # Preserve the previous owner's records without applying them here.
            self._generation += 1
            self._run = None
            self._human = self.ports.human_guid
            self._owned = self._load_owned()
        if self._run is not None and self._run.session.raid_id == run.session.raid_id:
            return
        if not self._valid_run(run):
            return
        self._generation += 1
        self._run, self._initial = run, True
        self._next_check = self.ports.clock.now()
        eligible = self._eligible(run)
        for guid, previous in tuple(self._owned.items()):
            if guid in eligible and eligible[guid] == previous.fingerprint:
                # This is a new run's reconciliation, never a delayed old-run
                # restore. A restore already in flight may need counteracting.
                self._owned[guid] = _Boost(run.session.raid_id, eligible[guid], uuid4().hex,
                                          needs_remove=guid in self._restoring)
        self._save_owned()

    def invalidate(self, run):
        if run is not self._run:
            return
        self._generation += 1
        self._run = None
        self._initial = False
        self._next_check = self.ports.clock.now()

    async def on_end(self, outcome, *, run=None):
        if run is not None and self._run is not None and run is not self._run:
            return
        # Listener end has no run ID. A late end from an older controller cannot
        # retire a newer raid that is still active in the shared session store.
        if run is None and self._run is not None and self._active(self._run):
            return
        self.invalidate(self._run)

    def _current(self, generation):
        return generation == self._generation

    async def _command(self, guids, action, param, generation):
        """Return (applied, definitely skipped); unknown work remains owned."""
        if not guids or not self._current(generation):
            return set(), set()
        command = BotDo(uuid4().hex, tuple(guids), action, param)
        try:
            # Bound the entire exchange, including a bridge send which never
            # returns. Timeout leaves the pre-recorded removal uncertain.
            async with asyncio.timeout(JOB_TIMEOUT_S):
                ack = await self.ports.send(command)
                if ack is None or ack.id != command.id:
                    return set(), set()
                if ack.ok is not True:
                    return set(), set(guids)
                # An ack only queues BotDo. Its matching terminal job says which
                # members changed; malformed/nonterminal jobs prove nothing.
                result = await self.ports.wait_job(command.id, JOB_TIMEOUT_S)
            if result is None or result.id != command.id or result.kind != "bot_do" or result.state != "done":
                return set(), set()
            applied = set(result.guids)
            skipped = [row.guid for row in result.skipped]
            if (len(applied) != len(result.guids) or len(set(skipped)) != len(skipped)
                    or applied & set(skipped) or applied | set(skipped) != set(guids)
                    or any(type(g) is not int for g in (*result.guids, *skipped))):
                return set(), set()
            return applied, set(skipped)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("raid play command outcome is unknown")
            return set(), set()

    async def _remove(self, members, generation):
        run = self._run
        eligible = self._eligible(run) if run is not None else {}
        wanted = [g for g in members if g in eligible and not self._other_raid(g, run.session.raid_id)
                  and (g not in self._owned or self._owned[g].needs_remove)]
        records = {}
        previous = {g: self._owned.get(g) for g in wanted}
        for guid in wanted:
            record = _Boost(run.session.raid_id, eligible[guid], uuid4().hex)
            records[guid] = self._owned[guid] = record
        self._save_owned()  # Unknown send/cancellation must survive a restart.
        _, skipped = await self._command(wanted, "co", "-boost", generation)
        for guid in skipped:
            if self._owned.get(guid) is records[guid]:
                if previous[guid] is None:
                    self._owned.pop(guid)
                else:
                    self._owned[guid] = previous[guid]
        self._save_owned()

    def _can_restore(self, guid, record):
        if (self._fingerprint(guid) != record.fingerprint
                or self._other_raid(guid, record.raid_id)
                or record.restore_attempts >= MAX_RESTORE_ATTEMPTS
                or self.ports.clock.now() < record.retry_at):
            return False
        if self._run is not None:
            if guid in self._run.plan.members:
                return (self._valid_run(self._run) and record.raid_id == self._run.session.raid_id
                        and not drunk(self.ports.persona_state(guid)))
            # An old member can be restored only when no later raid claims it.
        row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (record.raid_id,)).fetchone()
        return row is not None and row[0] is not None

    async def _restore(self, generation, *, check_active):
        records = {g: r for g, r in self._owned.items() if self._can_restore(g, r)
                   and (check_active or self._run is None or g not in self._run.plan.members or r.restore_attempts)}
        if not records:
            return
        attempted = {}
        for guid, record in records.items():
            updated = replace(record, restore_attempts=record.restore_attempts + 1,
                              retry_at=self.ports.clock.now() + RESTORE_RETRY_S)
            attempted[guid] = self._owned[guid] = updated
        self._save_owned()
        self._restoring.update(attempted)
        try:
            applied, _ = await self._command(sorted(attempted), "co", "+boost", generation)
            for guid in applied:
                if self._owned.get(guid) is attempted[guid]:
                    self._owned.pop(guid)
            self._save_owned()
        finally:
            self._restoring.difference_update(attempted)

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            generation = self._generation
            try:
                if self._run is not None and not self._active(self._run):
                    await self.on_end("ended")
                    generation = self._generation
                run = self._run
                now = self.ports.clock.now()
                check_active = run is not None and (self._initial or now >= self._next_check)
                if check_active:
                    initial = self._initial
                    self._initial = False
                    self._next_check = now + CHECK_EVERY_S
                    eligible = self._eligible(run)
                    if initial:
                        roles = {g: role_of(fp[0], fp[1], fp[2]) for g, fp in eligible.items()}
                        raid_roles = {g: fp[2] for g, fp in eligible.items()}
                        for action, param, members in spacing_actions(roles, raid_roles):
                            if not self._current(generation):
                                return
                            current = self._eligible(run)
                            members = [g for g in members if current.get(g) == eligible[g]]
                            await self._command(members, action, param, generation)
                    if not self._current(generation):
                        return
                    current = self._eligible(run)
                    await self._remove([g for g in current if drunk(self.ports.persona_state(g))], generation)
                if self._current(generation):
                    await self._restore(generation, check_active=check_active)
            except asyncio.CancelledError:
                # All removals were recorded before send; a later tick/end can
                # compensate an unknown outcome without resending -boost.
                raise
