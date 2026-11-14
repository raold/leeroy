"""The control endpoint's raidlead op and the raid lead in its status, behind the same key."""

from conftest import run

from test_endpoint import ask, served


class FakeRaidLead:
    def __init__(self):
        self.asks = []

    def console_status(self):
        return {"lead": 101, "source": "chat", "his": False, "assistant": True, "raid": True,
                "blocked": None, "candidates": [{"guid": 101, "name": "Arkon", "officer": True,
                                                 "online": True}]}

    async def console_action(self, args):
        self.asks.append(args)
        return {"ok": True, "message": "Arkon leads your group's raid now."}


def test_status_carries_the_raid_lead_only_while_it_is_on(tmp_path):
    async def go():
        async with served(tmp_path) as (app, ep):
            assert "raid_lead" not in await ask(ep, "status")
            refused = await ask(ep, "raidlead", {"action": "clear"})
            assert refused["ok"] is False and "off" in refused["error"]
            app.raidlead = FakeRaidLead()
            status = await ask(ep, "status")
            assert status["raid_lead"]["lead"] == 101
            reply = await ask(ep, "raidlead", {"action": "set", "guid": 101})
            assert reply == {"ok": True, "message": "Arkon leads your group's raid now."}
            assert app.raidlead.asks == [{"action": "set", "guid": 101}]
            bad = await ask(ep, "raidlead", {"action": "set", "guid": "101"})
            assert bad["ok"] is False and app.raidlead.asks == [{"action": "set", "guid": 101}]
            assert (await ask(ep, "raidlead", {"action": "clear"}, key="x" * 43)) == {
                "ok": False, "error": "forbidden"}
    run(go())
