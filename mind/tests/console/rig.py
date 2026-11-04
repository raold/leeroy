"""The console tests' rig: a ConsoleApp on a free port with a scratch data dir, a fake mind
handle, fake probes and a tiny HTTP client. Nothing reaches a real process, port or model."""
import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from leeroy_mind.console.app import ConsoleApp, Settings
from leeroy_mind.console.control import Controls
from leeroy_mind.console.ws import WsError

KEY = "k" * 32  # a fixed key; the real one is secrets.token_urlsafe(24)


class FakeMind:
    """The mind handle's surface the app reads: probe, held, launch_exit, stopping."""

    def __init__(self, data, *, found=(), held=False, error=None):
        self.data = data
        self.found, self._held, self.error = list(found), held, error
        self.stopping = False
        self.exit = None
        self.locked = lambda _: self._held
        self.acts: list[str] = []

    def held(self, data=None):
        return self._held

    def probe(self):
        return (None if self.error else list(self.found)), self._held, self.error

    def launch_exit(self):
        return self.exit

    def act(self, action):
        from leeroy_mind.lifecycle import Outcome
        self.acts.append(action)
        return Outcome(0, f"mind {action} done", "running", 101)


def running(data):
    return FakeMind(data, found=[SimpleNamespace(pid=101), SimpleNamespace(pid=100)], held=True)


async def no_voice(*a, **kw):
    raise WsError("cannot connect (ConnectionRefusedError)")


def make_app(data, *, mind=None, controls=None, connect=no_voice, tcp=lambda: None,
             ollama=None, gpu=None, clock=None, **settings) -> ConsoleApp:
    mind = mind or FakeMind(data)
    s = Settings(data_dir=data, root=data.parent, port=0, key=KEY, **settings)
    controls = controls or Controls(enabled=True, mind=mind, voice=SimpleNamespace(
        act=lambda a: None))
    kw = {}
    if clock is not None:
        kw["clock"] = clock
    return ConsoleApp(s, mind=mind, controls=controls, connect=connect, tcp=tcp,
                      ollama=ollama or (lambda url, roles: {"ok": True, "url": url, "models": []}),
                      gpu=gpu or (lambda: {"ok": False, "error": "nvidia-smi not found",
                                           "gpus": []}), **kw)


@dataclass
class Reply:
    status: int
    headers: dict = field(default_factory=dict)  # lower-cased names, every value
    body: bytes = b""

    def header(self, name):
        values = self.headers.get(name.lower(), [])
        return values[0] if values else None

    def json(self):
        return json.loads(self.body)


async def http(app, method, path, *, headers=None, body=None, host=None, key=True,
               origin=False) -> Reply:
    port = app.s.port
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}"]
    if key:
        lines.append(f"X-Console-Key: {KEY}")
    if origin:
        lines.append(f"Origin: {origin if isinstance(origin, str) else app.origin}")
    data = b""
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        lines.append(f"Content-Length: {len(data)}")
        if not any(k.lower() == "content-type" for k in (headers or {})):
            lines.append("Content-Type: application/json")
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + data)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    first, *rest = head.decode("latin-1").split("\r\n")
    out: dict = {}
    for line in rest:
        k, _, v = line.partition(":")
        out.setdefault(k.strip().lower(), []).append(v.strip())
    return Reply(int(first.split(" ")[1]), out, payload)


async def events(app, *, count=1, timeout=10.0):
    """Open /api/events and read `count` events: [(kind, data)], then close."""
    port = app.s.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                  f"X-Console-Key: {KEY}\r\nAccept: text/event-stream\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    assert b" 200 " in head.split(b"\r\n")[0], head
    out = []
    try:
        while len(out) < count:
            block = (await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout)).decode()
            if block.startswith(":"):
                continue
            kind = data = None
            for line in block.splitlines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            out.append((kind, data))
    finally:
        writer.close()
    return out
