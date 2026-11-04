"""The worker's lifecycle state machine: pure, single-threaded, no I/O and no clock.

booting -> ready -> resetting -> awaiting_action -> stepping -> awaiting_action | terminal, with aborted and closing
exits. The transport feeds it connections and decoded requests; the worker adapter performs the effects it emits
(begin_reset, begin_step, abort, release, close_connection) and reports completions tagged with the generation they
started under. A completion or callback from an older generation is ignored, so an abandoned operation can never
touch a new episode.

At most one mutating request (reset or step) is in flight. An exact duplicate of the in-flight request is answered
again when it completes; an exact duplicate of a recent result is answered from the bounded cache without touching
the world; a different payload under a known id is a conflicting_duplicate. Every request line gets exactly one
response line.

modules/mod-dojo/src/Lib/DojoLifecycle.* is the same machine in C++; the shared golden fixture checks they agree.
"""

from __future__ import annotations

import copy
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    MAX_OBSERVATION_VALUES, PROTOCOL_VERSION, RECENT_EPISODES, AbortReason, Bounds, Decoded, ErrorCode, Request,
    encode, error, is_id, is_int, is_number, is_text,
)

BOOTING = "booting"
READY = "ready"
RESETTING = "resetting"
AWAITING_ACTION = "awaiting_action"
STEPPING = "stepping"
TERMINAL = "terminal"
ABORTED = "aborted"
CLOSING = "closing"

STATES = (BOOTING, READY, RESETTING, AWAITING_ACTION, STEPPING, TERMINAL, ABORTED, CLOSING)
_LIVE = (RESETTING, AWAITING_ACTION, STEPPING)
_RESETTABLE = (READY, AWAITING_ACTION, TERMINAL, ABORTED)

INFO_KEYS = ("worker", "scenario", "schemas", "actors", "actions", "clock", "rng")
OUTCOME_REASONS = ("death", "simultaneous_death", "time_limit", "external")


@dataclass
class Output:
    send: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False


@dataclass
class _InFlight:
    kind: str
    request: Request
    copies: int = 1
    actions: list[dict[str, int]] = field(default_factory=list)


def check_info(info: dict[str, Any], bounds: Bounds) -> None:
    """Raises ValueError unless info is a usable worker description (see the README's hello_result)."""
    if not isinstance(info, dict) or set(info) != set(INFO_KEYS):
        raise ValueError(f"worker info needs exactly {INFO_KEYS}")
    if not is_id(info["scenario"]):
        raise ValueError("bad scenario")
    schemas = info["schemas"]
    if not isinstance(schemas, dict) or set(schemas) != {"observation", "action"} \
            or not all(is_text(v) for v in schemas.values()):
        raise ValueError("bad schemas")
    actors = info["actors"]
    if not isinstance(actors, list) or not 1 <= len(actors) <= bounds.max_actors:
        raise ValueError("bad actors")
    for slot, actor in enumerate(actors):
        if not isinstance(actor, dict) or actor.get("slot") != slot or not is_int(actor.get("team"), 0, 255):
            raise ValueError("bad actors")
    actions = info["actions"]
    if not isinstance(actions, list) or not actions or actions[0] != "noop" or not all(is_id(a) for a in actions):
        raise ValueError("bad actions: a non-empty list of names whose first is noop")
    for key in ("worker", "clock", "rng"):
        if not isinstance(info[key], dict):
            raise ValueError(f"bad {key}")


class Lifecycle:
    def __init__(self, info: dict[str, Any], bounds: Bounds | None = None, booted: bool = True):
        self.bounds = bounds or Bounds()
        check_info(info, self.bounds)
        self.info = copy.deepcopy(info)
        self.actor_count = len(info["actors"])
        self.catalog_size = len(info["actions"])
        self.state = READY if booted else BOOTING
        self.session: str | None = None
        self.helloed = False
        self.episode: str | None = None
        self.decision: int | None = None
        self.generation = 0
        self.draining: int | None = None
        self.abort_reason: str | None = None
        self._inflight: _InFlight | None = None
        self._masks: list[list[int]] | None = None
        self._cache: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._recent: deque[str] = deque(maxlen=RECENT_EPISODES)

    # --- queries ---

    @property
    def in_flight(self) -> str | None:
        return self._inflight.request.id if self._inflight else None

    def accepts_callback(self, generation: int) -> bool:
        """Whether a world callback started under generation may still touch the episode."""
        return generation == self.generation and self.state in _LIVE

    # --- worker and transport events ---

    def boot_complete(self) -> Output:
        if self.state == BOOTING:
            self.state = READY
        return Output()

    def connect(self, session: str) -> Output:
        if self.session is not None:
            raise RuntimeError("a session is already connected")
        if not is_id(session):
            raise ValueError("bad session id")
        self.session = session
        self.helloed = False
        self._cache.clear()
        self._recent.clear()
        return Output()

    def disconnect(self) -> Output:
        out = Output()
        if self.session is None:
            return out
        self._cut_episode(AbortReason.DISCONNECT, out)
        out.effects.append({"effect": "release", "reason": AbortReason.DISCONNECT})
        self._end_session()
        return out

    def fault(self) -> Output:
        """The worker failed outside a completion (a watchdog, a crashed map): abort whatever is running."""
        out = Output()
        if self.state in (BOOTING, CLOSING):
            self.abort_reason = AbortReason.FAULT
            return out
        if self._inflight is not None:
            failed = error(ErrorCode.ABORTED, self._inflight.request.id, "worker fault", AbortReason.FAULT)
            self._remember(self._inflight.request, failed)
            out.send.extend([failed] * self._inflight.copies)
        self._cut_episode(AbortReason.FAULT, out)
        self.state = ABORTED
        self.abort_reason = AbortReason.FAULT
        self.decision = None
        self._masks = None
        return out

    def drained(self) -> Output:
        """The worker reports that no work from an abandoned operation remains."""
        self.draining = None
        if self.state == CLOSING:
            self.state = READY
        return Output()

    def handle(self, decoded: Decoded) -> Output:
        out = Output()
        if decoded.request is None:
            out.send.append(error(decoded.code or ErrorCode.MALFORMED, decoded.id, decoded.message))
            return out
        req = decoded.request
        if self._inflight is not None and self._inflight.request.id == req.id:
            if self._inflight.request.fingerprint == req.fingerprint:
                self._inflight.copies += 1
            else:
                out.send.append(error(ErrorCode.CONFLICTING_DUPLICATE, req.id, "id is in flight with another payload"))
            return out
        if req.id in self._cache:
            fingerprint, response = self._cache[req.id]
            out.send.append(copy.deepcopy(response) if fingerprint == req.fingerprint
                            else error(ErrorCode.CONFLICTING_DUPLICATE, req.id, "id was used for another payload"))
            return out
        if req.type == "status":
            out.send.append(self._status(req))
            return out
        if req.type == "hello":
            return self._hello(req)
        if not self.helloed:
            out.send.append(error(ErrorCode.NOT_READY, req.id, "say hello first"))
            return out
        if req.body["session"] != self.session:
            out.send.append(error(ErrorCode.BAD_SESSION, req.id, "not this connection's session"))
            return out
        if req.type == "close":
            return self._close(req)
        if self._inflight is not None:
            out.send.append(error(ErrorCode.BUSY, req.id, f"{self._inflight.request.id} is in flight"))
            return out
        return self._reset(req) if req.type == "reset" else self._step(req)

    def complete_reset(self, generation: int, payload: dict[str, Any]) -> Output:
        inflight = self._current("reset", generation)
        if inflight is None:
            return Output(stale=True)
        out = Output()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            reason = payload.get("reason", "reset failed") if isinstance(payload, dict) else "reset failed"
            self._fail(out, ErrorCode.ABORTED, AbortReason.RESET_FAILED, str(reason))
            return out
        sim_ms = payload.get("sim_ms", 0)
        masks = self._check_observations(payload.get("observations"))
        if masks is None or not is_int(sim_ms, 0, 2**63 - 1) or set(payload) - {"ok", "observations", "sim_ms"}:
            self._fail(out, ErrorCode.ABORTED, AbortReason.INVALID_RESULT, "reset result does not fit the schema")
            return out
        req = inflight.request
        response = {
            "v": PROTOCOL_VERSION, "type": "reset_result", "id": req.id, "ok": True, "session": self.session,
            "episode": self.episode, "decision": 0, "generation": self.generation, "scenario": self.info["scenario"],
            "scenario_seed": req.body["scenario_seed"], "sim_ms": sim_ms,
            "observations": copy.deepcopy(payload["observations"]),
        }
        if not self._fits(out, response):
            return out
        self._deliver(out, response)
        self.state = AWAITING_ACTION
        self.decision = 0
        self._masks = masks
        return out

    def complete_step(self, generation: int, payload: dict[str, Any]) -> Output:
        inflight = self._current("step", generation)
        if inflight is None:
            return Output(stale=True)
        out = Output()
        if not isinstance(payload, dict):
            self._fail(out, ErrorCode.ABORTED, AbortReason.INVALID_RESULT, "step result is not an object")
            return out
        if "fault" in payload:
            self._fail(out, ErrorCode.ABORTED, AbortReason.FAULT, str(payload["fault"]))
            return out
        events = payload.get("events")
        if isinstance(events, list) and len(events) > self.bounds.max_events:
            self._fail(out, ErrorCode.RESULT_OVERFLOW, AbortReason.RESULT_OVERFLOW,
                       f"{len(events)} events, the bound is {self.bounds.max_events}")
            return out
        masks = self._check_step_payload(payload)
        if masks is None:
            self._fail(out, ErrorCode.ABORTED, AbortReason.INVALID_RESULT, "step result does not fit the schema")
            return out
        req = inflight.request
        response = {
            "v": PROTOCOL_VERSION, "type": "step_result", "id": req.id, "ok": True, "session": self.session,
            "episode": self.episode, "decision": self.decision, "next_decision": self.decision + 1,
            "generation": self.generation, "requested": copy.deepcopy(inflight.actions),
            "elapsed_ms": payload["elapsed_ms"], "substeps": payload["substeps"], "sim_ms": payload["sim_ms"],
            "observations": copy.deepcopy(payload["observations"]), "events": copy.deepcopy(events),
            "rewards": copy.deepcopy(payload["rewards"]), "terminated": payload["terminated"],
            "truncated": payload["truncated"], "outcome": copy.deepcopy(payload["outcome"]),
        }
        if not self._fits(out, response):
            return out
        self._deliver(out, response)
        self.decision += 1
        self._masks = masks
        self.state = TERMINAL if payload["terminated"] or payload["truncated"] else AWAITING_ACTION
        return out

    # --- requests ---

    def _status(self, req: Request) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION, "type": "status_result", "id": req.id, "ok": True, "state": self.state,
            "session": self.session if self.helloed else None, "episode": self.episode, "decision": self.decision,
            "generation": self.generation, "in_flight": self.in_flight, "draining": self.draining is not None,
            "abort_reason": self.abort_reason,
        }

    def _hello(self, req: Request) -> Output:
        out = Output()
        if self.session is None or self.helloed:
            out.send.append(error(ErrorCode.NOT_READY, req.id, "this connection already has a session"))
            return out
        protocol = req.body["protocol"]
        if not protocol["min"] <= PROTOCOL_VERSION <= protocol["max"]:
            out.send.append(error(ErrorCode.UNSUPPORTED_VERSION, req.id, f"this worker speaks v{PROTOCOL_VERSION}"))
            return out
        mine = {"observation_schema": self.info["schemas"]["observation"],
                "action_schema": self.info["schemas"]["action"], "scenario": self.info["scenario"]}
        for key, expected in req.body.get("expect", {}).items():
            if expected != mine[key]:
                out.send.append(error(ErrorCode.UNSUPPORTED_VERSION, req.id, f"{key} is {mine[key]}"))
                return out
        self.helloed = True
        response = {"v": PROTOCOL_VERSION, "type": "hello_result", "id": req.id, "ok": True, "session": self.session,
                    "protocol": PROTOCOL_VERSION, "state": self.state, "bounds": self.bounds.to_wire()}
        response.update(copy.deepcopy(self.info))
        self._remember(req, response)
        out.send.append(response)
        return out

    def _close(self, req: Request) -> Output:
        out = Output()
        if self._inflight is not None:
            failed = error(ErrorCode.ABORTED, self._inflight.request.id, "closed by the controller", AbortReason.CLOSED)
            out.send.extend([failed] * self._inflight.copies)
        self._cut_episode(AbortReason.CLOSED, out)
        out.send.append({"v": PROTOCOL_VERSION, "type": "close_result", "id": req.id, "ok": True,
                         "session": self.session})
        out.effects.append({"effect": "release", "reason": AbortReason.CLOSED})
        out.effects.append({"effect": "close_connection"})
        self._end_session()
        return out

    def _reset(self, req: Request) -> Output:
        out = Output()
        body = req.body
        if self.state not in _RESETTABLE or self.draining is not None:
            out.send.append(error(ErrorCode.NOT_READY, req.id, f"cannot reset while {self.state}"
                                  + (" (draining)" if self.draining is not None else "")))
            return out
        if body["decision"] != 0:
            out.send.append(error(ErrorCode.WRONG_STEP, req.id, "a reset starts at decision 0"))
            return out
        if "scenario" in body and body["scenario"] != self.info["scenario"]:
            out.send.append(error(ErrorCode.UNSUPPORTED_VERSION, req.id, f"scenario is {self.info['scenario']}"))
            return out
        if body["episode"] == self.episode or body["episode"] in self._recent:
            out.send.append(error(ErrorCode.STALE_EPISODE, req.id, "episode id was already used"))
            return out
        self.generation += 1
        self.episode = body["episode"]
        self._recent.append(body["episode"])
        self.decision = None
        self._masks = None
        self.abort_reason = None
        self._inflight = _InFlight("reset", req)
        self.state = RESETTING
        out.effects.append({"effect": "begin_reset", "generation": self.generation, "episode": self.episode,
                            "scenario": self.info["scenario"], "scenario_seed": body["scenario_seed"]})
        return out

    def _step(self, req: Request) -> Output:
        out = Output()
        body = req.body
        if self.state == ABORTED:
            out.send.append(error(ErrorCode.ABORTED, req.id, "the episode was aborted", self.abort_reason))
            return out
        if self.state != AWAITING_ACTION:
            out.send.append(error(ErrorCode.NOT_READY, req.id, f"cannot step while {self.state}"))
            return out
        if body["episode"] != self.episode:
            out.send.append(error(ErrorCode.STALE_EPISODE, req.id, f"the episode is {self.episode}"))
            return out
        if body["decision"] != self.decision:
            out.send.append(error(ErrorCode.WRONG_STEP, req.id, f"the decision is {self.decision}"))
            return out
        chosen: dict[int, int] = {}
        for a in body["actions"]:
            actor, action = a["actor"], a["action"]
            if actor >= self.actor_count or actor in chosen:
                out.send.append(error(ErrorCode.INVALID_ACTION, req.id, f"actor {actor} is unknown or repeated"))
                return out
            if action >= self.catalog_size or not self._masks[actor][action]:
                out.send.append(error(ErrorCode.INVALID_ACTION, req.id, f"action {action} is not open to {actor}"))
                return out
            chosen[actor] = action
        if len(chosen) != self.actor_count:
            out.send.append(error(ErrorCode.INVALID_ACTION, req.id, "every controlled actor needs a command"))
            return out
        actions = [{"actor": actor, "action": chosen[actor], "action_id": self.decision * self.actor_count + actor}
                   for actor in range(self.actor_count)]
        self._inflight = _InFlight("step", req, actions=actions)
        self.state = STEPPING
        out.effects.append({"effect": "begin_step", "generation": self.generation, "episode": self.episode,
                            "decision": self.decision, "actions": copy.deepcopy(actions)})
        return out

    # --- helpers ---

    def _current(self, kind: str, generation: int) -> _InFlight | None:
        inflight = self._inflight
        if inflight is not None and inflight.kind == kind and generation == self.generation:
            return inflight
        if self.draining is not None and generation == self.draining:
            self.drained()
        return None

    def _cut_episode(self, reason: str, out: Output) -> None:
        """Aborts a live episode: fences its generation and asks the worker to tear it down."""
        if self.state in _LIVE:
            out.effects.append({"effect": "abort", "generation": self.generation, "reason": reason})
            if self._inflight is not None:
                self.draining = self.generation
            self.generation += 1
        self._inflight = None

    def _end_session(self) -> None:
        self.session = None
        self.helloed = False
        self.episode = None
        self.decision = None
        self.abort_reason = None
        self._masks = None
        self._cache.clear()
        self._recent.clear()
        if self.state != BOOTING:
            self.state = CLOSING if self.draining is not None else READY

    def _fail(self, out: Output, code: str, reason: str, message: str) -> None:
        """A completion that cannot be delivered: answer the request with an error and abort the episode."""
        inflight = self._inflight
        failed = error(code, inflight.request.id, message, reason)
        self._remember(inflight.request, failed)
        out.send.extend([failed] * inflight.copies)
        out.effects.append({"effect": "abort", "generation": self.generation, "reason": reason})
        self.generation += 1
        self._inflight = None
        self.decision = None
        self._masks = None
        self.state = ABORTED
        self.abort_reason = reason

    def _fits(self, out: Output, response: dict[str, Any]) -> bool:
        try:
            size = len(encode(response).encode("utf-8"))
        except (ValueError, UnicodeError):
            self._fail(out, ErrorCode.ABORTED, AbortReason.INVALID_RESULT, "result is not encodable")
            return False
        if size > self.bounds.max_line_bytes:
            self._fail(out, ErrorCode.RESULT_OVERFLOW, AbortReason.RESULT_OVERFLOW,
                       f"result is {size} bytes, the bound is {self.bounds.max_line_bytes}")
            return False
        return True

    def _deliver(self, out: Output, response: dict[str, Any]) -> None:
        inflight = self._inflight
        self._remember(inflight.request, response)
        out.send.extend(copy.deepcopy(response) for _ in range(inflight.copies))
        self._inflight = None

    def _remember(self, req: Request, response: dict[str, Any]) -> None:
        self._cache[req.id] = (req.fingerprint, copy.deepcopy(response))
        self._cache.move_to_end(req.id)
        while len(self._cache) > max(self.bounds.result_cache, 1):
            self._cache.popitem(last=False)

    def _check_observations(self, observations: Any) -> list[list[int]] | None:
        if not isinstance(observations, list) or len(observations) != self.actor_count:
            return None
        masks = []
        for slot, o in enumerate(observations):
            if not isinstance(o, dict) or set(o) != {"actor", "obs", "mask"} or o["actor"] != slot:
                return None
            values, mask = o["obs"], o["mask"]
            if not isinstance(values, list) or len(values) > MAX_OBSERVATION_VALUES \
                    or not all(is_number(v) for v in values):
                return None
            if not isinstance(mask, list) or len(mask) != self.catalog_size \
                    or not all(is_int(m, 0, 1) for m in mask) or mask[0] != 1:
                return None
            masks.append(list(mask))
        return masks

    def _check_step_payload(self, payload: dict[str, Any]) -> list[list[int]] | None:
        keys = {"elapsed_ms", "substeps", "sim_ms", "observations", "events", "rewards", "terminated", "truncated",
                "outcome"}
        if set(payload) != keys:
            return None
        if not all(is_int(payload[k], 0, 2**63 - 1) for k in ("elapsed_ms", "substeps", "sim_ms")):
            return None
        events = payload["events"]
        if not isinstance(events, list) or not all(isinstance(e, dict) and isinstance(e.get("kind"), str)
                                                   for e in events):
            return None
        rewards = payload["rewards"]
        if not isinstance(rewards, list) or len(rewards) != self.actor_count:
            return None
        for slot, r in enumerate(rewards):
            if not isinstance(r, dict) or set(r) != {"actor", "terminal", "shaping"} or r["actor"] != slot \
                    or not is_number(r["terminal"]) or not is_number(r["shaping"]):
                return None
        terminated, truncated, outcome = payload["terminated"], payload["truncated"], payload["outcome"]
        if not isinstance(terminated, bool) or not isinstance(truncated, bool) or (terminated and truncated):
            return None
        if not (terminated or truncated):
            if outcome is not None:
                return None
        else:
            if not isinstance(outcome, dict) or set(outcome) != {"reason", "winner"} \
                    or outcome["reason"] not in OUTCOME_REASONS:
                return None
            if truncated != (outcome["reason"] == "external"):
                return None
            winner = outcome["winner"]
            if outcome["reason"] == "death":
                if not is_int(winner, 0, 255):
                    return None
            elif winner is not None:
                return None
        return self._check_observations(payload["observations"])
