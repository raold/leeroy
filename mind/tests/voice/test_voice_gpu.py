"""Task 21, the mind's half: when the voice service takes the GPU (status.gpu leaves "none"), the
mind pins Orion and the embedding model in Ollama so nothing evicts them under it, with exactly
the options the real calls use so a pin can never reload a model, and lets them go again when
the hold ends. Ollama here is always a fake server on port 0."""
import asyncio
import http.server
import json
import logging
import threading

from conftest import run
from leeroy_mind.llm.client import OllamaClient
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.voice import wire
from leeroy_mind.voice.gpu import OllamaPins


class FakePins:
    def __init__(self):
        self.calls = []
        self.error = None
        self.gate = None  # an asyncio.Event a call waits on

    async def _do(self, what, live, embed):
        self.calls.append((what, live, embed))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error

    async def pin(self, live, embed):
        await self._do("pin", live, embed)

    async def unpin(self, live, embed):
        await self._do("unpin", live, embed)


def status(gpu):
    return wire.Status(True, "ok", gpu, "whisper" if gpu != "none" else "parakeet", True, 0, ())


async def drain(ext):
    while ext._gpu_tasks:
        await asyncio.gather(*list(ext._gpu_tasks))
        await asyncio.sleep(0)


def test_pins_once_per_hold_and_unpins_when_it_ends(vapp):
    pins = FakePins()
    vapp.ext.gpu_pins = pins
    m = vapp.app.cfg.models

    async def go():
        ext = vapp.ext
        await ext.on_message(status("none"))
        await drain(ext)
        assert pins.calls == []
        await ext.on_message(status("whisper"))
        await ext.on_message(status("whisper"))
        await drain(ext)
        assert pins.calls == [("pin", m.live, m.embedding)]
        await ext.on_message(status("chatterbox"))  # still held: nothing new
        await drain(ext)
        assert len(pins.calls) == 1
        await ext.on_message(status("none"))
        await drain(ext)
        assert pins.calls[1:] == [("unpin", m.live, m.embedding)]
        await ext.on_message(status("whisper"))
        await drain(ext)
        assert [c[0] for c in pins.calls] == ["pin", "unpin", "pin"]
    run(go())


def test_a_dropped_connection_forgets_the_hold(vapp):
    pins = FakePins()
    vapp.ext.gpu_pins = pins

    async def go():
        await vapp.ext.on_message(status("whisper"))
        await drain(vapp.ext)
        await vapp.ext.on_connection(False)
        await vapp.ext.on_message(status("whisper"))
        await drain(vapp.ext)
        assert [c[0] for c in pins.calls] == ["pin", "pin"]
    run(go())


def test_a_hanging_pin_never_stalls_the_receive_loop(vapp):
    """I2: on_message returns at once and the next messages are handled while Ollama hangs."""
    pins = FakePins()
    pins.gate = asyncio.Event()
    vapp.ext.gpu_pins = pins

    async def go():
        ext = vapp.ext
        await asyncio.wait_for(ext.on_message(status("whisper")), 1.0)
        await asyncio.wait_for(ext.on_message(status("none")), 1.0)  # queued behind it
        await asyncio.sleep(0.01)
        assert [c[0] for c in pins.calls] == ["pin"]  # one at a time, in order
        pins.gate.set()
        await drain(ext)
        assert [c[0] for c in pins.calls] == ["pin", "unpin"]
    run(go())


def test_a_failing_pin_is_logged_and_never_raised(vapp, caplog):
    pins = FakePins()
    pins.error = OSError("ollama said secret chat text")
    vapp.ext.gpu_pins = pins

    async def go():
        await vapp.ext.on_message(status("whisper"))
        await vapp.ext.on_message(status("none"))
        await drain(vapp.ext)
    with caplog.at_level(logging.WARNING):
        run(go())
    assert "voice_pin_failed" in caplog.text and "OSError" in caplog.text
    assert "secret" not in caplog.text


def test_default_pins_point_at_the_configured_ollama(vapp):
    assert isinstance(vapp.ext.gpu_pins, OllamaPins)
    assert vapp.ext.gpu_pins.url == vapp.app.cfg.ollama.url


# -- the real pins against a fake Ollama --------------------------------------------------------

class Ollama:
    """A fake server: /api/ps lists `loaded`; every POST is kept as (path, body)."""

    def __init__(self, loaded):
        self.loaded = loaded
        self.posts = []
        owner = self

        class H(http.server.BaseHTTPRequestHandler):
            def _reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply({"models": [{"name": n, "size": 1, "size_vram": 1}
                                        for n in owner.loaded]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.posts.append((self.path, body))
                if self.path == "/api/embed":
                    return self._reply({"embeddings": [[0.5] * body.get("dimensions", 512)
                                                       for _ in body["input"]]})
                self._reply({"message": {"content": ""}, "done": True})

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_pins_only_a_model_ollama_already_lists():
    o = Ollama(["orion"])  # the embedding model is not loaded
    try:
        run(OllamaPins(o.url).pin("orion", "emb"))
    finally:
        o.close()
    assert [p for p, _ in o.posts] == ["/api/chat"]


def test_pins_nothing_when_nothing_is_loaded_or_ollama_is_down():
    o = Ollama([])
    try:
        run(OllamaPins(o.url).pin("orion", "emb"))
        run(OllamaPins(o.url).unpin("orion", "emb"))
    finally:
        o.close()
    assert o.posts == []
    run(OllamaPins("http://127.0.0.1:9").pin("orion", "emb"))  # cannot ask: does nothing


def test_the_pin_bodies_are_the_real_calls_with_only_keep_alive_changed():
    """I1: the same options as the live chat call and the embed call, so Ollama reuses the
    runner. Captured from the real client against the same fake."""
    o = Ollama(["orion", "emb:latest"])
    try:
        client = OllamaClient(o.url)
        run(client.chat("orion", [], options=LIVE.options, think=LIVE.think, keep_alive=-1))
        run(client.embed("emb", ["."]))
        real = dict(o.posts)
        o.posts.clear()
        pins = OllamaPins(o.url)
        run(pins.pin("orion", "emb"))
        pinned = dict(o.posts)
        o.posts.clear()
        run(pins.unpin("orion", "emb"))
        unpinned = dict(o.posts)
    finally:
        o.close()
    for sent in (pinned, unpinned):
        assert sent["/api/chat"]["options"] == real["/api/chat"]["options"]
        assert sent["/api/chat"]["options"]["num_ctx"] == 4096
        assert sent["/api/chat"]["think"] == real["/api/chat"]["think"] is False
        assert sent["/api/chat"]["messages"] == [] and sent["/api/chat"]["stream"] is False
        assert sent["/api/embed"]["options"] == real["/api/embed"]["options"] == {"num_ctx": 2048}
        assert sent["/api/embed"]["dimensions"] == real["/api/embed"]["dimensions"] == 512
        assert sent["/api/embed"]["input"] == ["."]
    assert pinned["/api/chat"]["keep_alive"] == -1 and pinned["/api/embed"]["keep_alive"] == -1
    assert unpinned["/api/chat"]["keep_alive"] == "5m"
    assert unpinned["/api/embed"]["keep_alive"] == "5m"
