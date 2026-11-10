"""Actual MindApp/endpoint/console with FakeBridge, FakeLLM and fake photo worker.

OS start/stop is the existing rehearsal handle; CLI launch executes realm_main in-process
against this fixture's database. No real process/service or model endpoint is used.
"""
import asyncio
import sys
from pathlib import Path

from conftest import HUMAN, run
from console.e2e.harness import Rehearsal
from console.rig import http
from console.test_console_ops_actions import cli_process, post
from selfies.fakes import until
from selfies.test_photo_booth import MemoryWorker, finished_night
from test_opscontrol import add_root, ROOT_ID
from leeroy_mind.realm.action_store import ActionStore
from leeroy_mind.selfies.store import queue_more


class OpsRehearsal(Rehearsal):
    async def start_mind(self):
        path = self.data/"mind.toml"
        text = path.read_text()
        if "ops_actions" not in text:
            path.write_text(text.replace("[selfies]\nenabled = true", "[selfies]\nenabled = true\nbooth = true")+
                            "\n[control]\nops_actions=true\n")
        await super().start_mind()
        await self.bridge.push({"type":"logout","guid":HUMAN,"human":True})
        await until(lambda: not self.app.world.human_online)
        self.app.store.conn.execute("UPDATE guild_raid SET ended=? WHERE ended IS NULL",(self.app.clock.now(),))
        self.app.store.conn.commit()


async def confirmed(c, args):
    preview = (await post(c,{**args,"phase":"preview"})).json()
    assert preview["ok"], preview
    request = {**args,"phase":"confirm","token":preview["token"]}
    if args["action"] == "release": request["typed_id"] = args["id"]
    answer = await post(c,request)
    assert answer.status == 202, answer.json()
    await asyncio.wait_for(asyncio.shield(c._ops_actions.task),30)
    return c._ops_actions.operation


def test_rehearsal_release_restarts_mind_then_snapshot_and_fake_booth(tmp_path,monkeypatch,cli_process):
    async def go():
        rig = OpsRehearsal(tmp_path,monkeypatch)
        try:
            await rig.start()
            c = rig.console
            c.s.ops_actions = True
            c.s.root = Path(__file__).resolve().parents[3]
            rig.handle.python = Path(sys.executable)
            rig.handle.clean_stop = lambda: rig.handle.act("stop")
            add_root(rig.app.store.conn)
            old = rig.app
            result = await confirmed(c,{"action":"release","id":ROOT_ID,"reason":"reviewed_synthetic_hold"})
            assert result["state"] == "done", result
            assert rig.app is not old and rig.handle._held
            assert ActionStore(rig.app.store.conn).get(ROOT_ID).result_reason == "abandoned"
            assert all(ROOT_ID != h.scope_id for h in ActionStore(rig.app.store.conn).holds())
            assert rig.handle.acts == ["stop","start"]
            assert (await confirmed(c,{"action":"snapshot","label":"rehearsal"}))["state"] == "done"
            assert list((rig.data/"snapshots").glob("mind-console-rehearsal-*.db"))
            a = rig.app
            await until(lambda: not a.arbiter.holders)
            a.night_scheduler.presence.changed = a.clock.now()-601
            finished_night(a)
            a.selfie_booth.worker = MemoryWorker
            row = queue_more(a.store,101,now=a.clock.now()-601)
            assert row.set_no > 0
            result = await confirmed(c,{"action":"render"})
            assert result["state"] == "done", result
        finally:
            await rig.close()
    run(go())
