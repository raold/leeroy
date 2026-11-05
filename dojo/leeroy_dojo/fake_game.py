"""A deterministic two-actor duel for exercising the protocol without AzerothCore.

It is not WoW: integer hit points and mana, one delayed command per actor, a bolt with a cast time and an instant
lance under a shared global cooldown. What it shares with the planned core adapter is the shape of the contract:
fixed 10 ms substeps inside 200 ms decisions, a 150 ms command delay so commands can execute after the decision that
requested them, execution events that name their originating action id, terminal outcomes (death, simultaneous death,
time limit), the +1/-1/0 terminal reward with optional potential shaping, and a worker-lifetime simulation clock that
a reset does not rewind. Combat randomness comes from a seeded SplitMix64, so the same scenario seed and the same
commands replay exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import __version__

SUBSTEP_MS = 10
DECISION_MS = 200
COMMAND_DELAY_MS = 150
TIME_LIMIT_MS = 180_000
MAX_HP = 100
MAX_MANA = 100
GCD_MS = 600
BOLT_CAST_MS = 600
BOLT_COST = 12
BOLT_DAMAGE = (14, 18)
LANCE_COST = 6
LANCE_DAMAGE = (5, 7)
REGEN_EVERY_MS = 400
SHAPING_SCALE = 0.05

ACTIONS = ("noop", "stop_cast", "bolt", "lance", "move")
NOOP, STOP_CAST, BOLT, LANCE, MOVE = range(len(ACTIONS))
OBSERVATION_FIELDS = ("hp", "mana", "casting", "cast_left", "gcd", "pending", "enemy_hp", "enemy_casting",
                      "time_left")


class SplitMix64:
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def between(self, low: int, high: int) -> int:
        return low + self.next() % (high - low + 1)


@dataclass
class _Pending:
    action: int
    action_id: int
    due_ms: int


@dataclass
class _Actor:
    slot: int
    team: int
    hp: int = MAX_HP
    mana: int = MAX_MANA
    gcd_ms: int = 0
    cast_left_ms: int = 0
    cast_action_id: int | None = None
    pending: _Pending | None = None

    @property
    def casting(self) -> bool:
        return self.cast_action_id is not None


def info(time_limit_ms: int = TIME_LIMIT_MS) -> dict[str, Any]:
    """The worker description a Lifecycle needs and hello_result publishes."""
    return {
        "worker": {"kind": "fake", "module": f"leeroy_dojo {__version__}", "core": None, "playerbots": None},
        "scenario": "fake_duel_v1",
        "schemas": {"observation": "dojo.fake.obs/1", "action": "dojo.fake.act/1"},
        "actors": [{"slot": 0, "team": 0}, {"slot": 1, "team": 1}],
        "actions": list(ACTIONS),
        "clock": {"mode": "virtual", "substep_ms": SUBSTEP_MS, "decision_ms": DECISION_MS,
                  "command_delay_ms": COMMAND_DELAY_MS, "time_limit_ms": time_limit_ms, "paced": False},
        "rng": {"scenario_seed": True, "combat_replay": True},
    }


class FakeDuel:
    def __init__(self, time_limit_ms: int = TIME_LIMIT_MS):
        self.time_limit_ms = time_limit_ms
        self.sim_ms = 0                    # worker lifetime; a reset never rewinds it
        self.origin_ms = 0                 # this episode's start
        self.actors: list[_Actor] = []
        self.rng = SplitMix64(0)
        self.over = True

    # --- the contract ---

    def reset(self, scenario_seed: int) -> dict[str, Any]:
        self.rng = SplitMix64(scenario_seed)
        self.actors = [_Actor(0, 0), _Actor(1, 1)]
        self.origin_ms = self.sim_ms
        self.over = False
        return {"ok": True, "observations": self.observations(), "sim_ms": self.sim_ms}

    def abort(self) -> None:
        self.actors = []
        self.over = True

    def step(self, actions: list[dict[str, int]]) -> dict[str, Any]:
        if self.over or not self.actors:
            return {"fault": "no episode is running"}
        events: list[dict[str, Any]] = []
        before = self._potential()
        now = self.sim_ms - self.origin_ms
        for command in actions:
            self._request(self.actors[command["actor"]], command["action"], command["action_id"], now, events)

        start = self.sim_ms
        outcome = None
        for _ in range(DECISION_MS // SUBSTEP_MS):
            self.sim_ms += SUBSTEP_MS
            t = self.sim_ms - self.origin_ms
            # Timers first, then commands that fall due: what starts in this substep runs its full length.
            for actor in self.actors:
                if actor.casting:
                    actor.cast_left_ms -= SUBSTEP_MS
                    if actor.cast_left_ms <= 0:
                        origin, actor.cast_action_id, actor.cast_left_ms = actor.cast_action_id, None, 0
                        self._hit(actor, self._enemy(actor), self.rng.between(*BOLT_DAMAGE), origin, t, events)
            for actor in self.actors:
                actor.gcd_ms = max(0, actor.gcd_ms - SUBSTEP_MS)
                if t % REGEN_EVERY_MS == 0:
                    actor.mana = min(MAX_MANA, actor.mana + 1)
            for actor in self.actors:
                if actor.pending is not None and actor.pending.due_ms <= t:
                    pending, actor.pending = actor.pending, None
                    self._execute(actor, pending, t, events)
            outcome = self._outcome(t, events)
            if outcome is not None:
                break

        terminated = outcome is not None
        self.over = terminated
        after = 0.0 if terminated else self._potential()
        rewards = []
        for actor in self.actors:
            sign = 1 if actor.team == 0 else -1
            terminal = 0
            if outcome is not None and outcome["winner"] is not None:
                terminal = 1 if outcome["winner"] == actor.team else -1
            rewards.append({"actor": actor.slot, "terminal": terminal,
                            "shaping": round(SHAPING_SCALE * sign * (after - before), 6)})
        elapsed = self.sim_ms - start
        return {"elapsed_ms": elapsed, "substeps": elapsed // SUBSTEP_MS, "sim_ms": self.sim_ms,
                "observations": self.observations(), "events": events, "rewards": rewards,
                "terminated": terminated, "truncated": False, "outcome": outcome}

    def observations(self) -> list[dict[str, Any]]:
        t = self.sim_ms - self.origin_ms
        out = []
        for actor in self.actors:
            enemy = self._enemy(actor)
            values = [max(actor.hp, 0) / MAX_HP, actor.mana / MAX_MANA, 1.0 if actor.casting else 0.0,
                      actor.cast_left_ms / BOLT_CAST_MS, actor.gcd_ms / GCD_MS, 1.0 if actor.pending else 0.0,
                      max(enemy.hp, 0) / MAX_HP, 1.0 if enemy.casting else 0.0,
                      max(self.time_limit_ms - t, 0) / self.time_limit_ms]
            out.append({"actor": actor.slot, "obs": values, "mask": self.mask(actor)})
        return out

    def mask(self, actor: _Actor) -> list[int]:
        return [1, 1 if actor.casting else 0, 1 if actor.mana >= BOLT_COST else 0,
                1 if actor.mana >= LANCE_COST else 0, 1]

    # --- mechanics ---

    def _enemy(self, actor: _Actor) -> _Actor:
        return self.actors[1 - actor.slot]

    def _request(self, actor: _Actor, action: int, action_id: int, t: int, events: list[dict[str, Any]]) -> None:
        if action == NOOP:
            return   # a noop keeps whatever is pending
        if actor.pending is not None:
            events.append({"kind": "superseded", "t_ms": t, "actor": actor.slot,
                           "action_id": actor.pending.action_id, "by": action_id})
        actor.pending = _Pending(action, action_id, t + COMMAND_DELAY_MS)

    def _execute(self, actor: _Actor, pending: _Pending, t: int, events: list[dict[str, Any]]) -> None:
        def note(kind: str, **extra: Any) -> None:
            events.append({"kind": kind, "t_ms": t, "actor": actor.slot, "action_id": pending.action_id, **extra})

        if pending.action == STOP_CAST:
            if not actor.casting:
                note("rejected", reason="not_casting")
                return
            events.append({"kind": "cast_cancelled", "t_ms": t, "actor": actor.slot,
                           "action_id": actor.cast_action_id, "reason": "stop_cast"})
            actor.cast_action_id, actor.cast_left_ms = None, 0
            note("executed")
        elif pending.action == MOVE:
            if actor.casting:
                events.append({"kind": "cast_cancelled", "t_ms": t, "actor": actor.slot,
                               "action_id": actor.cast_action_id, "reason": "moved"})
                actor.cast_action_id, actor.cast_left_ms = None, 0
            note("moved")
        else:
            cost = BOLT_COST if pending.action == BOLT else LANCE_COST
            if actor.casting:
                note("rejected", reason="casting")
            elif actor.gcd_ms > 0:
                note("rejected", reason="gcd")
            elif actor.mana < cost:
                note("rejected", reason="no_mana")
            elif pending.action == BOLT:
                actor.mana -= cost
                actor.gcd_ms = GCD_MS
                actor.cast_left_ms = BOLT_CAST_MS
                actor.cast_action_id = pending.action_id
                note("cast_start", cast_ms=BOLT_CAST_MS)
            else:
                actor.mana -= cost
                actor.gcd_ms = GCD_MS
                note("executed")
                self._hit(actor, self._enemy(actor), self.rng.between(*LANCE_DAMAGE), pending.action_id, t, events)

    def _hit(self, source: _Actor, target: _Actor, amount: int, action_id: int, t: int,
             events: list[dict[str, Any]]) -> None:
        target.hp -= amount
        events.append({"kind": "damage", "t_ms": t, "actor": source.slot, "target": target.slot,
                       "action_id": action_id, "amount": amount})

    def _outcome(self, t: int, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        dead = [a for a in self.actors if a.hp <= 0]
        for actor in dead:
            events.append({"kind": "death", "t_ms": t, "actor": actor.slot})
        if len(dead) == len(self.actors):
            return {"reason": "simultaneous_death", "winner": None}
        if dead:
            return {"reason": "death", "winner": next(a.team for a in self.actors if a.hp > 0)}
        if t >= self.time_limit_ms:
            return {"reason": "time_limit", "winner": None}
        return None

    def _potential(self) -> float:
        """Team 0's mean remaining health fraction minus team 1's, dead members counted as zero."""
        def mean(team: int) -> float:
            members = [a for a in self.actors if a.team == team]
            return sum(max(a.hp, 0) / MAX_HP for a in members) / len(members)
        return mean(0) - mean(1)


# --- small authored policies over the actor's own observation and mask ---

def caster(obs: list[float], mask: list[int]) -> int:
    """Bolt when possible, lance otherwise; waits out casts, pending commands and the global cooldown."""
    if obs[2] > 0 or obs[5] > 0 or obs[4] > 0:
        return NOOP
    if mask[BOLT]:
        return BOLT
    return LANCE if mask[LANCE] else NOOP


def lancer(obs: list[float], mask: list[int]) -> int:
    """Lance whenever the cooldown allows."""
    if obs[5] > 0 or obs[4] > 0:
        return NOOP
    return LANCE if mask[LANCE] else NOOP


POLICIES = {"caster": caster, "lancer": lancer}
