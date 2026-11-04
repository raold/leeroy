"""The console's WebSocket client and its relay to the voice service's page socket, against a
fake voice service built from the same frame helpers. Invented names only."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from console.rig import KEY, events, http, make_app, running
from leeroy_mind.console import voicelink, ws
from leeroy_mind.console.voicelink import Mirror, VoiceRelay, page_message

STATE = {"type": "state", "tree": [{"id": "hall", "name": "Guild Hall", "parent": "",
                                    "locked": False, "capacity": 0},
                                   {"id": "raid", "name": "Raid", "parent": "", "locked": True,
                                    "capacity": 25}],
         "where": {"101": "hall"}, "names": {"101": "Arkon", "900": "Owner"}, "room": "",
         "human": 900, "speaking": [], "ptt": False, "volumes": {}, "mutes": [], "invites": [],
         "transcript": [{"who": "Arkon", "text": "hello", "ts": 1000, "kind": "say"}],
         "mind": True, "status": {"ok": True, "mic": "ok"}}


class FakeVoice:
    """A page socket: checks the handshake as the voice service does, sends a state, records
    what it is sent, answers a join with what `on_join` says."""

    def __init__(self, *, state=STATE, refuse_origin=False):
        self.state = state
        self.refuse_origin = refuse_origin
        self.received: list[dict] = []
        self.heads: list[str] = []
        self.peers: list[asyncio.StreamWriter] = []
        self.on_join = lambda channel: {"type": "moved", "guid": 900, "channel": channel}
        self.pongs: list[bytes] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        for w in self.peers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    async def send(self, msg, writer=None):
        frame = ws.encode_frame(ws.TEXT, json.dumps(msg).encode(), mask=False)
        for w in [writer] if writer else self.peers:
            w.write(frame)
            await w.drain()

    async def _client(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        self.heads.append(head)
        headers = {}
        for line in head.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        origin_ok = headers.get("origin") == f"http://127.0.0.1:{self.port}"
        if self.refuse_origin or not origin_ok or headers.get("host") != f"127.0.0.1:{self.port}":
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        accept = ws.accept_key(headers["sec-websocket-key"])
        writer.write((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.peers.append(writer)
        await self.send(self.state, writer)
        try:
            while True:
                fin, op, payload = await ws.read_frame(reader, max_size=4096, masked=True)
                if op == ws.PONG:
                    self.pongs.append(payload)
                elif op == ws.CLOSE:
                    break
                elif op == ws.TEXT:
                    msg = json.loads(payload)
                    self.received.append(msg)
                    if msg.get("type") == "join":
                        reply = self.on_join(msg["channel"])
                        if reply:
                            await self.send(reply, writer)
        except ws.WsError:
            pass
        finally:
            writer.close()


@asynccontextmanager
async def voice_service(**kw):
    fake = FakeVoice(**kw)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


async def until(check, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


# -- the client ----------------------------------------------------------------------------------

def test_the_client_names_the_pages_origin_and_reads_the_state():
    async def go():
        async with voice_service() as fake:
            c = await ws.WsClient.connect("127.0.0.1", fake.port, "/ws",
                                          origin=f"http://127.0.0.1:{fake.port}")
            assert json.loads(await c.recv())["type"] == "state"
            await c.send('{"type":"mute","guid":101,"on":true}')
            await until(lambda: fake.received)
            assert fake.received == [{"type": "mute", "guid": 101, "on": True}]
            assert f"Origin: http://127.0.0.1:{fake.port}" in fake.heads[0]
            await c.close()
    asyncio.run(go())


def test_the_client_answers_pings_and_ends_on_close():
    async def go():
        async with voice_service() as fake:
            c = await ws.WsClient.connect("127.0.0.1", fake.port, "/ws",
                                          origin=f"http://127.0.0.1:{fake.port}")
            await c.recv()
            w = fake.peers[0]
            w.write(ws.encode_frame(ws.PING, b"beat", mask=False))
            w.write(ws.encode_frame(ws.TEXT, b'{"type":"ptt",', mask=False, fin=False))
            w.write(ws.encode_frame(ws.CONT, b'"down":true}', mask=False))
            w.write(ws.encode_frame(ws.CLOSE, b"\x03\xe8", mask=False))
            await w.drain()
            assert json.loads(await c.recv()) == {"type": "ptt", "down": True}
            with pytest.raises(ws.WsClosed):
                await c.recv()
            await until(lambda: fake.pongs)
            assert fake.pongs == [b"beat"]
    asyncio.run(go())


def test_the_client_refuses_a_bad_upgrade_and_bad_frames():
    async def go():
        async with voice_service(refuse_origin=True) as fake:
            with pytest.raises(ws.WsError, match="HTTP 403"):
                await ws.WsClient.connect("127.0.0.1", fake.port, "/ws", origin="x")
        async with voice_service() as fake:
            c = await ws.WsClient.connect("127.0.0.1", fake.port, "/ws",
                                          origin=f"http://127.0.0.1:{fake.port}", max_size=64)
            with pytest.raises(ws.WsError, match="too large"):
                await c.recv()  # the state is bigger than 64 bytes
        async with voice_service() as fake:
            c = await ws.WsClient.connect("127.0.0.1", fake.port, "/ws",
                                          origin=f"http://127.0.0.1:{fake.port}")
            await c.recv()
            fake.peers[0].write(ws.encode_frame(ws.TEXT, b"masked", mask=True))
            await fake.peers[0].drain()
            with pytest.raises(ws.WsError, match="masking"):
                await c.recv()
        with pytest.raises(ws.WsError, match="cannot connect"):
            await ws.WsClient.connect("127.0.0.1", 1, "/ws", origin="x", timeout=1)
    asyncio.run(go())


# -- the mirror and the page's three messages ----------------------------------------------------

def test_page_messages_are_checked_as_the_voice_service_checks_them():
    assert page_message({"type": "join", "channel": "raid"}) == {"type": "join",
                                                                 "channel": "raid"}
    assert page_message({"type": "volume", "guid": 101, "db": 40}) == {
        "type": "volume", "guid": 101, "db": 12.0}
    assert page_message({"type": "mute", "guid": 101, "on": False})["on"] is False
    for bad in ({"type": "join", "channel": ""}, {"type": "join", "channel": "x" * 65},
                {"type": "join", "channel": "a\nb"}, {"type": "volume", "guid": 0, "db": 1},
                {"type": "volume", "guid": 1, "db": float("nan")},
                {"type": "volume", "guid": True, "db": 1}, {"type": "mute", "guid": 1, "on": 1},
                {"type": "speak", "text": "x"}, {}):
        assert page_message(bad) is None


def test_the_mirror_follows_the_pages_events():
    m = Mirror()
    m.apply(STATE)
    m.apply({"type": "speaking", "guid": 101, "on": True})
    m.apply({"type": "ptt", "down": True})
    m.apply({"type": "invite", "from": 101, "channel": "raid"})
    m.apply({"type": "invite", "from": 101, "channel": "raid"})
    assert m.state["speaking"] == [101] and m.state["ptt"] is True
    assert m.state["invites"] == [{"from": 101, "channel": "raid"}]
    m.apply({"type": "moved", "guid": 900, "channel": "raid"})
    assert m.state["room"] == "raid" and m.state["invites"] == []
    m.apply({"type": "invite", "from": 101, "channel": "hall"})
    m.apply({"type": "moved", "guid": 101, "channel": ""})  # its sender left the invited room
    assert m.state["invites"] == [] and "101" not in m.state["where"]
    for i in range(250):
        m.apply({"type": "transcript", "who": "Arkon", "text": str(i), "ts": i, "kind": "say"})
    assert len(m.state["transcript"]) == voicelink.TRANSCRIPT
    m.apply({"type": "status", "ok": False, "mic": "missing"})
    m.apply({"type": "mind", "up": False})
    assert m.state["status"] == {"ok": False, "mic": "missing"} and m.state["mind"] is False
    m.apply({"type": "volume", "guid": 101, "db": 3.0})
    m.apply({"type": "mute", "guid": 101, "on": True})
    assert m.state["volumes"] == {"101": 3.0} and m.state["mutes"] == [101]
    snap = m.snapshot()
    snap["mutes"].append(5)
    assert m.state["mutes"] == [101]


def test_the_relay_connects_mirrors_forwards_and_reconnects():
    async def go():
        async with voice_service() as fake:
            got = []
            relay = VoiceRelay(lambda: fake.port, got.append, retry_s=0.05)
            task = asyncio.create_task(relay.run())
            await until(lambda: relay.connected)
            assert got[0] == {"type": "link", "up": True} and got[1]["type"] == "state"
            assert await relay.send({"type": "join", "channel": "hall"}) is None
            await until(lambda: relay.mirror.state["room"] == "hall")
            assert await relay.send({"type": "explode"}) is not None
            for w in fake.peers:
                w.close()
            await until(lambda: {"type": "link", "up": False} in got)
            assert relay.snapshot() is None
            await until(lambda: relay.connected)  # it comes back
            await relay.close()
            task.cancel()
    asyncio.run(go())


# -- through the console -------------------------------------------------------------------------

@pytest.fixture
def data(store, tmp_path):
    return tmp_path


def test_a_join_through_the_console_reaches_the_voice_service_and_its_answer_the_page(data):
    async def go():
        async with voice_service() as fake:
            fake.on_join = lambda channel: {"type": "refused", "channel": channel,
                                            "reason": "offline"}
            app = make_app(data, mind=running(data), voice_ui_port=fake.port, voice_enabled=True,
                           connect=ws.WsClient.connect)
            await app.start()
            try:
                await until(lambda: app.relay.connected)
                reader = asyncio.create_task(events(app, count=2))
                await until(lambda: app._subs)
                await asyncio.sleep(0.1)
                r = await http(app, "POST", "/api/voice", origin=True,
                               body={"type": "join", "channel": "raid"})
                assert r.status == 200 and r.json() == {"ok": True}
                (k1, snap), (k2, refused) = await reader
                assert k1 == "snapshot" and snap["voice"]["names"]["101"] == "Arkon"
                assert snap["status"]["voice"] == {"enabled": True, "service": True,
                                                   "link": True}
                assert (k2, refused) == ("voice", {"type": "refused", "channel": "raid",
                                                   "reason": "offline"})
                assert fake.received == [{"type": "join", "channel": "raid"}]
            finally:
                await app.close()
    asyncio.run(go())


def test_the_console_refuses_a_join_before_asking_when_the_mind_is_not_linked(data):
    async def go():
        async with voice_service(state={**STATE, "mind": False}) as fake:
            app = make_app(data, mind=running(data), voice_ui_port=fake.port, voice_enabled=True,
                           connect=ws.WsClient.connect)
            await app.start()
            try:
                await until(lambda: app.relay.connected)
                r = await http(app, "POST", "/api/voice", origin=True,
                               body={"type": "join", "channel": "raid"})
                assert r.status == 409 and "not connected to the voice service" in r.json()["error"]
                r = await http(app, "POST", "/api/voice", origin=True,
                               body={"type": "volume", "guid": 101, "db": -3})
                assert r.status == 200
                await until(lambda: fake.received)
                assert fake.received == [{"type": "volume", "guid": 101, "db": -3.0}]
                state = (await http(app, "GET", "/api/state")).json()
                assert state["voice"]["volumes"] == {"101": -3.0}
            finally:
                await app.close()
    asyncio.run(go())


def test_a_page_without_the_key_cannot_drive_the_voice_service(data):
    async def go():
        async with voice_service() as fake:
            app = make_app(data, mind=running(data), voice_ui_port=fake.port, voice_enabled=True,
                           connect=ws.WsClient.connect)
            await app.start()
            try:
                await until(lambda: app.relay.connected)
                r = await http(app, "POST", "/api/voice", key=False, origin=True,
                               body={"type": "join", "channel": "hall"})
                assert r.status == 403
                r = await http(app, "POST", "/api/voice", origin="http://127.0.0.1:9",
                               body={"type": "join", "channel": "hall"})
                assert r.status == 403
                await asyncio.sleep(0.1)
                assert fake.received == []
            finally:
                await app.close()
    asyncio.run(go())
