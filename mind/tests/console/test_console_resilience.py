"""Bounded roster payloads and a dead voice connection, with invented data/loopback only."""
import asyncio
import json

import pytest

from console.rig import http, make_app
from console.test_console_voice import voice_service
from leeroy_mind.console import personaview, voicelink, ws


def test_roster_pages_keep_every_member_and_only_their_optional_badges():
    members = [{"guid": i, "name": f"Member {i:03}", "detail": "x" * 1500} for i in range(151)]
    view = {"members": members, "names": {str(i): m["name"] for i, m in enumerate(members)},
            "missing": [], "insight_badges": {str(i): {"claims": i} for i in range(151)}}
    found = []
    for offset in range(0, 151, 50):
        page = personaview.roster_page(view, offset)
        found.extend(m["guid"] for m in page["members"])
        assert len(json.dumps(page).encode()) < 200_000
        assert set(page["insight_badges"]) == {str(m["guid"]) for m in page["members"]}
        assert page["names"] == view["names"]  # ties can name a member on another page
        assert page["page"]["total"] == 151
    assert found == list(range(151))
    assert personaview.roster_page(view, 999)["page"]["offset"] == 150
    assert len(view["members"]) == 151 and len(view["insight_badges"]) == 151


@pytest.mark.parametrize("query", ["offset=-1", "offset=1.5", "offset=1&offset=2", "offset=9999999"])
def test_roster_refuses_malformed_page_offsets(tmp_path, query):
    async def run():
        app = make_app(tmp_path)
        await app.start()
        try:
            response = await http(app, "GET", "/api/personas?" + query)
            assert response.status == 400
        finally:
            await app.close()
    asyncio.run(run())


def test_silent_voice_socket_times_out_and_closes():
    async def run():
        async with voice_service() as fake:
            client = await ws.WsClient.connect("127.0.0.1", fake.port, "/ws",
                                               origin=f"http://127.0.0.1:{fake.port}")
            assert json.loads(await client.recv())["type"] == "state"
            client._idle_s = 0.01
            with pytest.raises(ws.WsClosed, match="heartbeat timed out"):
                await asyncio.wait_for(client.recv(), 1)
            assert client.closed
    asyncio.run(run())


def test_voice_mirror_cannot_accumulate_departed_members_or_invites():
    mirror = voicelink.Mirror()
    for guid in range(1000):
        mirror.apply({"type": "moved", "guid": guid, "channel": "hall"})
        mirror.apply({"type": "invite", "from": guid, "channel": "hall"})
        mirror.apply({"type": "volume", "guid": guid, "db": 1})
        mirror.apply({"type": "mute", "guid": guid, "on": True})
    assert len(mirror.state["where"]) == voicelink.PEOPLE
    assert len(mirror.state["volumes"]) == voicelink.PEOPLE
    assert len(mirror.state["mutes"]) == voicelink.PEOPLE
    assert len(mirror.state["invites"]) == voicelink.INVITES


def test_roster_page_counts_personas_across_whole_guild():
    members = [{"guid":i,"persona": {"version":1} if i%2 else None} for i in range(120)]
    page = personaview.roster_page({"members":members},50)
    assert page["page"]["total"]==120 and page["page"]["personas"]==60
    assert len(page["members"])==50


def test_voice_restart_interrupts_long_retry_sleep():
    async def run():
        port = None
        relay = voicelink.VoiceRelay(lambda:port,lambda _:None,retry_s=60,jitter=lambda:1)
        relay._attempt = 10
        task = asyncio.create_task(relay.run())
        try:
            await asyncio.sleep(.01)
            async with voice_service() as fake:
                port = fake.port
                await relay.reconnect()
                async def connected():
                    while not relay.connected:
                        await asyncio.sleep(.001)
                await asyncio.wait_for(connected(),1)
                assert relay._attempt==0
        finally:
            await relay.close()
            await task
    asyncio.run(run())


def test_voice_backoff_never_exceeds_five_seconds(monkeypatch):
    async def run():
        relay = voicelink.VoiceRelay(lambda:None,lambda _:None,retry_s=60,jitter=lambda:1)
        relay._attempt = 20
        delays = []
        async def wait(awaitable,timeout):
            awaitable.close()
            delays.append(timeout)
            relay._closing = True
        monkeypatch.setattr(voicelink.asyncio,"wait_for",wait)
        await relay.run()
        assert delays==[5]
    asyncio.run(run())


@pytest.mark.parametrize("connected",[False,True])
@pytest.mark.parametrize("action,wakes",[("start",1),("restart",1),("stop",0)])
def test_console_voice_control_wakes_relay(tmp_path,monkeypatch,action,wakes,connected):
    from types import SimpleNamespace
    from leeroy_mind.console.control import Controls
    from leeroy_mind.lifecycle import Outcome
    from console.rig import FakeMind
    async def run():
        mind = FakeMind(tmp_path)
        voice = SimpleNamespace(act=lambda action:Outcome(0,"fixture done","running",None))
        app = make_app(tmp_path,mind=mind,controls=Controls(enabled=True,mind=mind,voice=voice))
        calls = []
        async def reconnect():
            calls.append(1)
        monkeypatch.setattr(app.relay,"reconnect",reconnect)
        await app.start()
        try:
            app.relay.connected = connected
            expected = 0 if connected and action == "start" else wakes
            result = await http(app,"POST","/api/control",origin=True,
                                body={"service":"voice","action":action,"confirm":True})
            assert result.status==200 and len(calls)==expected
        finally:
            await app.close()
    asyncio.run(run())
