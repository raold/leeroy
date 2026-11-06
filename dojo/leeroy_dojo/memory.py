"""The controller's side of an episode boundary (plan Task 7, D05 and D26).

Every actor's history (its observations, masks, requested actions and their action ids) and every policy's memory
(a recurrent state, a scripted policy's plan) belong to exactly one episode. EpisodeMemory clears them all at once,
when a reset result arrives, and refuses anything from another episode or an older generation afterwards: a late step
result of the last episode cannot become part of the next one, the same rule the worker applies to its own
callbacks. An aborted episode keeps its history for inspection until the next reset clears it; it never continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol


class StaleResult(Exception):
    """A result from another episode or an older generation than the memory's."""


class Policy(Protocol):
    def reset(self) -> None: ...


@dataclass
class ActorHistory:
    observations: list[list[float]] = field(default_factory=list)
    masks: list[list[int]] = field(default_factory=list)
    requested: list[int] = field(default_factory=list)
    action_ids: list[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.observations)


class EpisodeMemory:
    def __init__(self, actors: int, policies: Iterable[Policy] = (), limit: int = 100_000):
        self.actors = actors
        self.policies = list(policies)
        self.limit = limit
        self.episode: str | None = None
        self.generation: int | None = None
        self.decision = 0
        self.ended: str | None = None
        self.resets = 0
        self.histories: list[ActorHistory] = [ActorHistory() for _ in range(actors)]

    def begin(self, reset_result: dict[str, Any]) -> None:
        """The boundary: drop every actor's history and every policy's memory, then keep the new first observations."""
        generation = int(reset_result["generation"])
        if self.generation is not None and generation <= self.generation:
            raise StaleResult(f"reset generation {generation} is not newer than {self.generation}")
        self.histories = [ActorHistory() for _ in range(self.actors)]
        for policy in self.policies:
            policy.reset()
        self.episode = reset_result["episode"]
        self.generation = generation
        self.decision = int(reset_result.get("decision", 0))
        self.ended = None
        self.resets += 1
        self._keep(reset_result["observations"])

    def check(self, result: dict[str, Any]) -> None:
        if self.generation is None:
            raise StaleResult("no episode has begun")
        if result.get("generation") != self.generation:
            raise StaleResult(f"generation {result.get('generation')} is not the memory's {self.generation}")
        if "episode" in result and result["episode"] != self.episode:
            raise StaleResult(f"episode {result['episode']!r} is not the memory's {self.episode!r}")
        if self.ended is not None:
            raise StaleResult(f"episode {self.episode!r} already ended ({self.ended})")

    def record(self, step_result: dict[str, Any]) -> None:
        """One decision's result: what each actor asked for and what it now observes."""
        self.check(step_result)
        if step_result.get("decision") != self.decision:
            raise StaleResult(f"decision {step_result.get('decision')} is not the expected {self.decision}")
        for r in step_result["requested"]:
            h = self.histories[r["actor"]]
            h.requested.append(int(r["action"]))
            h.action_ids.append(int(r["action_id"]))
        self.decision = int(step_result["next_decision"])
        self._keep(step_result["observations"])
        if step_result.get("terminated") or step_result.get("truncated"):
            self.ended = (step_result.get("outcome") or {}).get("reason", "ended")

    def abort(self, reason: str) -> None:
        self.ended = f"aborted: {reason}"

    def _keep(self, observations: list[dict[str, Any]]) -> None:
        if len(observations) != self.actors:
            raise ValueError(f"{len(observations)} observations for {self.actors} actors")
        for o in observations:
            h = self.histories[o["actor"]]
            if len(h) >= self.limit:
                raise OverflowError(f"actor {o['actor']} history is full ({self.limit}); the episode must end first")
            h.observations.append(list(o["obs"]))
            h.masks.append(list(o["mask"]))


class Controller:
    """A protocol client whose resets and steps keep an EpisodeMemory (and its policies) at the episode boundary."""

    def __init__(self, client, memory: EpisodeMemory):
        self.client = client
        self.memory = memory

    def reset(self, episode: str, scenario_seed: int, scenario: str | None = None) -> dict[str, Any]:
        result = self.client.reset(episode, scenario_seed, scenario)
        self.memory.begin(result)
        return result

    def step(self, actions: list[int]) -> dict[str, Any]:
        result = self.client.step(actions)
        self.memory.record(result)
        return result
