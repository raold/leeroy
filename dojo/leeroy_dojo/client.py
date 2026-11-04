"""A blocking controller for a dojo worker over loopback NDJSON.

request() sends one line and waits for the response with the same id. On a timeout it resends the identical line,
which the worker treats as an exact duplicate: it never applies twice, and every copy is answered once, so the extra
answers are owed and discarded when they arrive. A lost connection is never retried: reconnecting starts a new session
and the old episode is aborted, so nobody has to guess whether the last step was applied.
"""

from __future__ import annotations

import itertools
import json
import socket
import time
from collections import Counter
from typing import Any, Iterable

from . import __version__
from .protocol import MAX_LINE_BYTES, PROTOCOL_VERSION, encode


class DojoError(Exception):
    """The worker answered with an error line."""

    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.code = response.get("code")
        self.reason = response.get("reason")
        super().__init__(f"{self.code}: {response.get('message', '')}")


class ConnectionLost(Exception):
    pass


class DojoClient:
    def __init__(self, *, timeout: float = 10.0, retries: int = 2, max_line_bytes: int = MAX_LINE_BYTES,
                 id_prefix: str = "q"):
        self.timeout = timeout
        self.retries = retries
        self.max_line_bytes = max_line_bytes
        self.id_prefix = id_prefix
        self.sock: socket.socket | None = None
        self.session: str | None = None
        self.hello_result: dict[str, Any] | None = None
        self.episode: str | None = None
        self.decision: int | None = None
        self.resends = 0
        self._buffer = bytearray()
        self._owed: Counter[str] = Counter()
        self._ids = itertools.count(1)

    # --- connection ---

    def connect(self, host: str, port: int) -> None:
        self.disconnect()
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buffer.clear()
        self._owed.clear()
        self.session = self.hello_result = self.episode = None
        self.decision = None

    def disconnect(self) -> None:
        """Drops the connection without a close request."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def next_id(self, kind: str = "") -> str:
        return f"{self.id_prefix}{kind}{next(self._ids)}"

    # --- lines ---

    def send_line(self, line: str | bytes) -> None:
        if self.sock is None:
            raise ConnectionLost("not connected")
        data = line.encode("utf-8") if isinstance(line, str) else line
        try:
            self.sock.sendall(data if data.endswith(b"\n") else data + b"\n")
        except OSError as e:
            raise ConnectionLost(str(e)) from e

    def read_message(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next response line, or None when nothing arrives in time."""
        if self.sock is None:
            raise ConnectionLost("not connected")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[:end])
                del self._buffer[:end + 1]
                if not line.strip():
                    continue
                return json.loads(line)
            if len(self._buffer) > self.max_line_bytes:
                raise ConnectionLost("the worker sent a line over the bound")
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                return None
            except OSError as e:
                raise ConnectionLost(str(e)) from e
            if not chunk:
                raise ConnectionLost("the worker closed the connection")
            self._buffer += chunk

    def request(self, message: dict[str, Any], *, timeout: float | None = None,
                retries: int | None = None) -> dict[str, Any]:
        """Sends message and returns its result; raises DojoError for an error answer."""
        line = encode(message)
        rid = message["id"]
        timeout = self.timeout if timeout is None else timeout
        retries = self.retries if retries is None else retries
        resent = 0
        self.send_line(line)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                if resent >= retries:
                    raise TimeoutError(f"no answer to {rid} after {resent} resends")
                resent += 1
                self.resends += 1
                self.send_line(line)
                deadline = time.monotonic() + timeout
                continue
            answer = self.read_message(left)
            if answer is None:
                continue
            aid = answer.get("id")
            if aid == rid:
                self._owed[rid] += resent
                self.track(answer)
                if answer.get("type") == "error":
                    raise DojoError(answer)
                return answer
            if aid is not None and self._owed[aid] > 0:
                self._owed[aid] -= 1
                continue
            if answer.get("type") == "error" and aid is None:
                raise DojoError(answer)

    def track(self, answer: dict[str, Any]) -> None:
        """Follows the session, episode and decision a result reports; request() calls it for every answer."""
        kind = answer.get("type")
        if kind == "hello_result":
            self.session = answer["session"]
            self.hello_result = answer
        elif kind == "reset_result":
            self.episode = answer["episode"]
            self.decision = 0
        elif kind == "step_result":
            self.decision = answer["next_decision"]
        elif kind == "close_result":
            self.session = None

    # --- requests ---

    def hello(self, expect: dict[str, str] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "hello", "id": self.next_id("h"),
                                   "protocol": {"min": PROTOCOL_VERSION, "max": PROTOCOL_VERSION},
                                   "client": f"leeroy_dojo {__version__}"}
        if expect:
            message["expect"] = expect
        return self.request(message)

    def reset_message(self, episode: str, scenario_seed: int, scenario: str | None = None) -> dict[str, Any]:
        message = {"v": PROTOCOL_VERSION, "type": "reset", "id": self.next_id("r"), "session": self.session,
                   "episode": episode, "decision": 0, "scenario_seed": scenario_seed}
        if scenario:
            message["scenario"] = scenario
        return message

    def reset(self, episode: str, scenario_seed: int, scenario: str | None = None) -> dict[str, Any]:
        return self.request(self.reset_message(episode, scenario_seed, scenario))

    def step_message(self, actions: Iterable[int]) -> dict[str, Any]:
        """A step for the current decision; actions are catalog indices in actor slot order."""
        return {"v": PROTOCOL_VERSION, "type": "step", "id": self.next_id("s"), "session": self.session,
                "episode": self.episode, "decision": self.decision,
                "actions": [{"actor": slot, "action": action} for slot, action in enumerate(actions)]}

    def step(self, actions: Iterable[int]) -> dict[str, Any]:
        return self.request(self.step_message(actions))

    def status(self) -> dict[str, Any]:
        return self.request({"v": PROTOCOL_VERSION, "type": "status", "id": self.next_id("q")})

    def close(self, reason: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "close", "id": self.next_id("c"),
                                   "session": self.session}
        if reason:
            message["reason"] = reason
        try:
            return self.request(message)
        finally:
            self.disconnect()
