"""A fake mod-leeroy bridge server for tests."""

import asyncio
import json
import time
import tomllib
import urllib.parse
from pathlib import Path
from typing import Callable

from .. import config, protocol

CORE_COMMAND_TYPES = frozenset(protocol._CORE_COMMANDS)

_DEFAULT_CAST = [
    {"guid": 101, "name": "Arkon", "race": 2, "class": 1, "gender": 0, "level": 80, "rank": 3,
     "online": True, "map": 0, "zone": 1519, "spec_tab": 0},
    {"guid": 102, "name": "Belra", "race": 8, "class": 5, "gender": 1, "level": 80, "rank": 3,
     "online": True, "map": 0, "zone": 1519, "spec_tab": 1},
]


def _live_ports() -> frozenset[int]:
    """The bridge's and Ollama's ports, from the defaults the service uses."""
    d = tomllib.loads(config.DEFAULT_TOML)
    return frozenset({d["bridge"]["port"], urllib.parse.urlsplit(d["ollama"]["url"]).port or 80})


def load_fixture(path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class FakeBridge:
    """mod-leeroy's bridge as the mind sees it (modules/mod-leeroy/src/Lib/LeeroyBridge.cpp,
    LeeroyWorldScript.cpp):

    - one mind at a time: another gets the error "another mind is connected" and is closed;
    - a mind is greeted with hello, then cast, and only then gets events;
    - the outbox: every event goes out once, in order, and what is published while no mind is
      connected (queue()) waits for the next greeting. The scripted `events` are what the
      first mind finds in it;
    - go_down() and come_up(): the worldserver stops (connections are refused and its outbox
      goes with the process), then listens again on the same port.

    push() writes straight to the greeted mind. With echo_first, a say's echo is written
    before its ack (mod-leeroy writes them from separate queues, so either may come first)."""

    def __init__(self, events: list[dict] | None = None, *, cast: list[dict] | None = None,
                 human_guid: int = 1,
                 ack: Callable[[dict], tuple[bool, str] | None] | None = None,
                 echo_say: bool = True, known_types=None, echo_first: bool = False):
        events = list(events or [])
        if events and events[0].get("type") == "hello":
            self._hello = events.pop(0)
        else:
            self._hello = {"type": "hello", "server": "mod-leeroy", "version": "0.1.0",
                           "guild_id": 1, "human_guid": human_guid}
        if events and events[0].get("type") == "cast":
            self._cast = events.pop(0)
        else:
            self._cast = {"type": "cast", "members": cast if cast is not None else _DEFAULT_CAST}
        self._outbox = events
        self._ack = ack or (lambda cmd: (True, ""))
        self._echo_say = echo_say
        self._echo_first = echo_first
        self._known_types = known_types
        self.commands: list[dict] = []
        self.written: list[str] = []  # the type of every line written to a mind, in order
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._client: asyncio.StreamWriter | None = None
        self._client_ready = asyncio.Event()  # set once the connected mind is greeted
        self._handlers: set[asyncio.Task] = set()
        self._returned: set[int] = set()

    # Never listened on: any free port may be the bridge's or Ollama's while they are down.
    AVOID_PORTS = _live_ports()

    async def start(self, port: int = 0) -> int:
        """Listen on 127.0.0.1:`port` (0: a free port that is not a live one); returns it."""
        for _ in range(20):
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
            self.port = self._server.sockets[0].getsockname()[1]
            if port or self.port not in self.AVOID_PORTS:
                return self.port
            self._server.close()
            await self._server.wait_closed()
        raise RuntimeError("no free port but the live ones")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.wait(self._handlers)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _fill(ev: dict) -> dict:
        out = dict(ev)
        out.setdefault("v", protocol.PROTOCOL_VERSION)
        out.setdefault("ts", int(time.time() * 1000))
        if out.get("type") in ("chat", "text_emote"):
            out.setdefault("via_bridge", False)
        return out

    async def go_down(self) -> None:
        """The worldserver stops: the mind's connection closes, new ones are refused, and the
        events it had not delivered are gone with the process."""
        await self.stop()
        self._set_client(None)
        self._outbox.clear()

    async def come_up(self, *, hello: dict | None = None, cast: dict | None = None) -> int:
        """The worldserver is back on the same port, greeting the next mind with `hello` and
        `cast` when given (he is usually offline after a restart: human_guid 0)."""
        self.set_greeting(hello=hello, cast=cast)
        return await self.start(self.port)

    async def queue(self, event: dict) -> None:
        """An event the server publishes: to the greeted mind at once, else into the outbox
        until the next greeting."""
        if self._client is not None and self._client_ready.is_set():
            await self._write(self._client, event)
        else:
            self._outbox.append(dict(event))

    async def _write(self, writer: asyncio.StreamWriter, ev: dict) -> None:
        self.written.append(ev.get("type", ""))
        writer.write(json.dumps(self._fill(ev), separators=(",", ":")).encode() + b"\n")
        await writer.drain()

    async def push(self, event: dict) -> None:
        await asyncio.wait_for(self._client_ready.wait(), 2.0)
        assert self._client is not None
        await self._write(self._client, event)

    async def push_raw(self, line: bytes) -> None:
        await asyncio.wait_for(self._client_ready.wait(), 2.0)
        assert self._client is not None
        self._client.write(line)
        await self._client.drain()

    def set_greeting(self, *, hello: dict | None = None, cast: dict | None = None) -> None:
        """What the next connections are greeted with (a restart that changed the state)."""
        if hello is not None:
            self._hello = dict(hello)
        if cast is not None:
            self._cast = dict(cast)

    async def drop_client(self) -> None:
        writer = self._client
        self._set_client(None)
        if writer is not None:
            writer.close()

    def _set_client(self, writer: asyncio.StreamWriter | None) -> None:
        self._client = writer
        self._client_ready.clear()

    async def wait_for_command(self, type: str, timeout: float = 2.0) -> dict:
        async def scan() -> dict:
            while True:
                for i, cmd in enumerate(self.commands):
                    if cmd.get("type") == type and i not in self._returned:
                        self._returned.add(i)
                        return cmd
                await asyncio.sleep(0.005)
        return await asyncio.wait_for(scan(), timeout)

    def _is_known(self, type_name) -> bool:
        known = self._known_types
        if known is None:
            known = set(CORE_COMMAND_TYPES) | set(protocol._commands)
        return type_name in known

    def _member_name(self, guid) -> str:
        for m in self._cast["members"]:
            if m["guid"] == guid:
                return m["name"]
        return ""

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        owned = False
        try:
            if self._client is not None:
                await self._write(writer, {"type": "error", "message": "another mind is connected"})
                return
            owned = True
            self.connections += 1
            self._set_client(writer)
            await self._write(writer, self._hello)
            await self._write(writer, self._cast)
            while self._outbox and self._client is writer:  # what waited, once, in order
                await self._write(writer, self._outbox.pop(0))
            if self._client is writer:
                self._client_ready.set()
            first = True
            while True:
                raw = (await reader.readline()).strip()
                if not raw:
                    if reader.at_eof():
                        return
                    continue
                try:
                    cmd = json.loads(raw)
                    if type(cmd) is not dict:
                        raise ValueError
                except ValueError:
                    await self._write(writer, {"type": "error", "message": "bad json"})
                    if first:
                        return  # as-built: error "bad json", then close
                    continue
                first = False
                await self._command(writer, cmd)
        except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            if owned and self._client is writer:
                self._set_client(None)
            writer.close()
            self._handlers.discard(task)

    async def _command(self, writer: asyncio.StreamWriter, cmd: dict) -> None:
        self.commands.append(cmd)
        cmd_id = cmd.get("id") if isinstance(cmd.get("id"), str) else None
        type_name = cmd.get("type")
        if not self._is_known(type_name):
            err = {"type": "error", "message": f"unknown type: {type_name}"}
            if cmd_id is not None:
                err["id"] = cmd_id
            await self._write(writer, err)
            return
        result = self._ack(cmd)
        if result is None:
            return
        ok, error = result
        if ok and type_name == "ping":
            await self._write(writer, {"type": "pong", "id": cmd_id})
        elif ok and type_name == "get_cast":
            await self._write(writer, self._cast)  # the real cast reply carries no id
        else:
            ack = {"type": "ack", "id": cmd_id, "ok": ok, "error": error}
            echo = None
            if ok and self._echo_say and type_name == "say":
                echo = {"type": "chat", "channel": cmd.get("channel", ""),
                        "from_guid": cmd.get("guid", 0),
                        "from_name": self._member_name(cmd.get("guid")),
                        "to_guid": cmd.get("to_guid", 0), "text": cmd.get("text", ""),
                        "map": 0, "zone": 0, "via_bridge": True}
            for line in ([echo, ack] if self._echo_first else [ack, echo]):
                if line is not None:
                    await self._write(writer, line)
