"""A deterministic two-actor training worker with no AzerothCore behind it.

The loopback transport feeds the lifecycle state machine, and the state machine's effects drive fake_game.FakeDuel,
exactly as the native worker will drive the core: the world advances only on an accepted step, an abort or a
disconnect tears the episode down, and completions carry the generation they started under. hold_steps() parks
accepted steps without completing them, which is how tests make a step stay in flight.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from . import fake_game
from .lifecycle import Lifecycle, Output
from .protocol import Bounds
from .transport import Event, LoopbackServer


class FakeWorker:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, bounds: Bounds | None = None,
                 time_limit_ms: int = fake_game.TIME_LIMIT_MS, inbox_capacity: int = 16, outbox_limit: int = 64):
        self.bounds = bounds or Bounds()
        self.game = fake_game.FakeDuel(time_limit_ms)
        self.lifecycle = Lifecycle(fake_game.info(time_limit_ms), self.bounds)
        self.server = LoopbackServer(self.bounds, inbox_capacity=inbox_capacity, outbox_limit=outbox_limit)
        self.host = host
        self.requested_port = port
        self.nonce = secrets.token_hex(3)
        self.applied: Counter[tuple[str, int]] = Counter()     # (episode, decision) -> times the world advanced
        self.resets: list[str] = []
        self.aborted: list[tuple[str, str]] = []                # (episode, reason)
        self.lock = threading.RLock()
        self._episodes: dict[int, str] = {}
        self._connection = 0
        self._held: deque[dict[str, Any]] = deque()
        self._holding = False
        self._paused = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- control ---

    def start(self) -> int:
        port = self.server.start(self.host, self.requested_port)
        self._thread = threading.Thread(target=self._run, name="dojo-fake-worker", daemon=True)
        self._thread.start()
        return port

    def stop(self) -> None:
        self._stop.set()
        self.server.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self.server.port

    def hold_steps(self) -> None:
        """Accepted steps stay in flight until release_steps()."""
        with self.lock:
            self._holding = True

    def release_steps(self) -> None:
        with self.lock:
            self._holding = False

    def pause(self) -> None:
        """Stops taking events from the transport, as a world thread busy elsewhere would."""
        with self.lock:
            self._paused = True

    def resume(self) -> None:
        with self.lock:
            self._paused = False

    def write_receipt(self, path: Path) -> None:
        """The process receipt a launcher records: where this worker listens and what it is."""
        receipt = {"kind": "leeroy-dojo-receipt", "worker": "fake", "pid": os.getpid(), "host": self.server.host,
                   "port": self.server.port, "protocol": 1, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                      time.gmtime())}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # --- the worker loop ---

    def _run(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                paused = self._paused
            if paused:
                time.sleep(0.01)
            else:
                event = self.server.poll(0.02)
                if event is not None:
                    with self.lock:
                        self._handle(event)
            with self.lock:
                while self._held and not self._holding:
                    self._run_step(self._held.popleft())

    def _handle(self, event: Event) -> None:
        if event.kind == "connected":
            self._connection = event.connection
            self.lifecycle.connect(f"s{event.connection}-{self.nonce}")
        elif event.kind == "line":
            if event.connection == self._connection:
                self._apply(self.lifecycle.handle(event.decoded))
        elif event.kind == "disconnected" and event.connection == self._connection:
            self._apply(self.lifecycle.disconnect())
            self._connection = 0

    def _apply(self, out: Output) -> None:
        connection = self._connection
        for message in out.send:
            self.server.send(connection, message)
        for effect in out.effects:
            kind = effect["effect"]
            if kind == "begin_reset":
                self._episodes[effect["generation"]] = effect["episode"]
                self.resets.append(effect["episode"])
                payload = self.game.reset(effect["scenario_seed"])
                self._apply(self.lifecycle.complete_reset(effect["generation"], payload))
            elif kind == "begin_step":
                if self._holding:
                    self._held.append(effect)
                else:
                    self._run_step(effect)
            elif kind == "abort":
                self.game.abort()
                self.aborted.append((self._episodes.get(effect["generation"], "?"), effect["reason"]))
            elif kind == "release":
                self.game.abort()
            elif kind == "close_connection":
                self.server.close(connection)

    def _run_step(self, effect: dict[str, Any]) -> None:
        if not self.lifecycle.accepts_callback(effect["generation"]):
            # Abandoned while parked: report it so the state machine stops draining; the world never moves.
            self._apply(self.lifecycle.complete_step(effect["generation"], {"fault": "abandoned"}))
            return
        self.applied[(effect["episode"], effect["decision"])] += 1
        self._apply(self.lifecycle.complete_step(effect["generation"], self.game.step(effect["actions"])))
