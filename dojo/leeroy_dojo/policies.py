"""Controlled baselines over the actor contract (plan Task 8, D25): what an actor sees (its v1 observation) and may do
(its collection-time mask over the v1 catalog), nothing else.

Every policy is a deterministic function of its seed, the episode and the observations and masks it has been given,
so a recorded episode can be replayed without a game client: the same inputs must give the same requested actions.
A policy never asks for a masked action; the noop (entry 0) is always open.

- noop: always entry 0. With both actors idle the match runs to its limit and draws.
- random_legal: uniform over the open actions, from a SplitMix64 seeded by (seed, episode).
- frost_duelist: a small authored Frost Mage script, the controlled baseline for later learning. It never interrupts
  its own cast or pending command, waits out the global cooldown it can see, closes in while the enemy is out of
  Frostbolt range, Frost Novas an enemy within 9 yards, spends Fingers of Frost or a frozen enemy on Ice Lance and
  otherwise casts Frostbolt facing the enemy.

No learned policy is here; none is claimed.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .actions import Catalog
from .fake_game import SplitMix64
from .observation import Schema

NOOP = 0
ENEMY_YARDS = 40          # enemy.distance is yards / 40, clipped at 1
GCD_SCALE = 1500          # self.gcd is ms / 1500
COOLDOWN_SCALE = 30000    # self.kit.<k>.cooldown is ms / 30000
# A request is worth sending once what remains of a wait fits the core's spell queue window after the perceptual and
# command delays (100 + 150 ms) have passed: 400 + 250 ms.
READY_WITHIN_MS = 650
NOVA_YARDS = 9.0


def wire_name(name: str) -> str:
    """The catalog name as the worker's hello lists it ('/' written as '.')."""
    return name.replace("/", ".")


def episode_seed(seed: int, episode: str) -> int:
    digest = hashlib.sha256(f"{seed}:{episode}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


class Policy:
    kind = "scripted"
    name = "policy"
    version = 1

    def __init__(self, schema: Schema, catalog: Catalog, seed: int = 0):
        self.schema = schema
        self.catalog = catalog
        self.seed = seed
        self.episode: str | None = None

    def reset(self, episode: str | None = None) -> None:
        """The episode boundary: whatever the policy kept goes. Without an episode it keeps the one it was given."""
        if episode is not None:
            self.episode = episode

    def act(self, obs: list[float], mask: list[int]) -> int:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "version": self.version, "seed": self.seed}


class Noop(Policy):
    kind = "noop"
    name = "noop"

    def act(self, obs: list[float], mask: list[int]) -> int:
        return NOOP


class RandomLegal(Policy):
    kind = "random_legal"
    name = "random_legal"

    def __init__(self, schema: Schema, catalog: Catalog, seed: int = 0):
        super().__init__(schema, catalog, seed)
        self.rng = SplitMix64(seed)

    def reset(self, episode: str | None = None) -> None:
        super().reset(episode)
        self.rng = SplitMix64(episode_seed(self.seed, self.episode or ""))

    def act(self, obs: list[float], mask: list[int]) -> int:
        open_ = [i for i, bit in enumerate(mask) if bit]
        return open_[self.rng.next() % len(open_)]


class FrostDuelist(Policy):
    kind = "scripted"
    name = "frost_duelist"
    version = 1

    def __init__(self, schema: Schema, catalog: Catalog, seed: int = 0):
        super().__init__(schema, catalog, seed)
        self._index = {f.name: i for i, f in enumerate(schema.fields)}
        self._action = {e.name: i for i, e in enumerate(catalog.entries)}

    def value(self, obs: list[float], name: str) -> float:
        return obs[self._index[name]]

    def first(self, mask: list[int], *names: str) -> int | None:
        for name in names:
            i = self._action.get(name)
            if i is not None and mask[i]:
                return i
        return None

    def ready(self, obs: list[float], spell: str) -> bool:
        cooldown = self.value(obs, f"self.kit.{spell}.cooldown") * COOLDOWN_SCALE
        return cooldown <= READY_WITHIN_MS and self.value(obs, f"self.kit.{spell}.known") == 1

    def act(self, obs: list[float], mask: list[int]) -> int:
        v = lambda name: self.value(obs, name)   # noqa: E731
        if v("self.alive") == 0 or v("self.casting") == 1 or v("self.queued") == 1 or v("self.pending") == 1:
            return NOOP
        if v("enemy.visible") != 1:
            # Out of sight: walk on the way it faces until the enemy shows again.
            return self.first(mask, "none/forward/keep") or NOOP
        distance = v("enemy.distance") * ENEMY_YARDS
        on_gcd = v("self.gcd") * GCD_SCALE > READY_WITHIN_MS
        if not on_gcd:
            if distance <= NOVA_YARDS and v("enemy.rooted") == 0 and self.ready(obs, "frost_nova"):
                found = self.first(mask, "frost_nova/hold/keep")
                if found is not None:
                    return found
            if (v("self.fingers_of_frost") > 0 or v("enemy.frozen") == 1) and self.ready(obs, "ice_lance"):
                found = self.first(mask, "ice_lance/hold/toward")
                if found is not None:
                    return found
            if v("self.kit.frostbolt.in_range") == 1 and self.ready(obs, "frostbolt"):
                found = self.first(mask, "frostbolt/hold/toward")
                if found is not None:
                    return found
        if v("self.kit.frostbolt.in_range") != 1:
            return self.first(mask, "none/forward/toward", "none/forward/keep") or NOOP
        return NOOP


POLICIES = {"noop": Noop, "random_legal": RandomLegal, "frost_duelist": FrostDuelist}


def make(spec: dict[str, Any], schema: Schema, catalog: Catalog) -> Policy:
    """A policy from its description (describe()), as a recording's header keeps it."""
    cls = POLICIES.get(spec.get("name", ""))
    if cls is None:
        raise ValueError(f"unknown policy {spec.get('name')!r}")
    policy = cls(schema, catalog, int(spec.get("seed", 0)))
    if int(spec.get("version", 1)) != policy.version:
        raise ValueError(f"{policy.name} is version {policy.version}, the recording used {spec.get('version')}")
    return policy
