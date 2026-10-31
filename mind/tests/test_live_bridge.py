"""The real mod-leeroy bridge (P1a). Deselected by default; run it only with the owner's OK,
never while he plays: the connection takes the bridge's single mind slot and its buffered
events (pre-flight ruling 21). `pytest -m live`."""
import asyncio
import tomllib

import pytest

from conftest import run
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import DEFAULT_TOML
from leeroy_mind.protocol import Cast, Hello, Ping


@pytest.mark.live
def test_live_bridge_smoke():
    bridge = tomllib.loads(DEFAULT_TOML)["bridge"]  # 127.0.0.1:7878, mod-leeroy's default

    async def main():
        got: dict[str, object] = {}

        async def on_event(ev):
            if isinstance(ev, Hello):
                got["hello"] = ev
            elif isinstance(ev, Cast):
                got["cast"] = ev

        client = BridgeClient(bridge["host"], bridge["port"], on_event)
        task = asyncio.create_task(client.run())
        try:
            await client.wait_connected(5.0)

            async def greeted():
                while not {"hello", "cast"} <= got.keys():
                    await asyncio.sleep(0.05)
            await asyncio.wait_for(greeted(), 5.0)
            assert got["hello"].server == "mod-leeroy"
            assert got["cast"].members
            ack = await asyncio.wait_for(client.send(Ping(id="")), 2.0)
            assert ack.ok
        finally:
            await client.close()
            await asyncio.wait({task}, timeout=5.0)
    run(main())
