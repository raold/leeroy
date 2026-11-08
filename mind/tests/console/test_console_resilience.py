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
