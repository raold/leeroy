#!/usr/bin/env python3
"""Run the candidate mind's actual playbook feed as an isolated integration adapter.

The dojo package stays standard-library-only. This operator script integrates
the sibling mind package; no model or live mind service runs. stdin EOF stops
the connection; stdout carries status only.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE / "dojo"), str(SOURCE / "mind")]

from leeroy_dojo.realm.layout import load
from leeroy_dojo.realm.mechanics import feed_arguments
from leeroy_dojo.realm.world import preflight
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.protocol import Hello, register_command
from leeroy_mind.raidplay.model import load as playbooks
from leeroy_mind.raidplay.respond import MechanicFeed, RaidMechanics


def status(ready=False, **fields):
    print(json.dumps(dict(kind="dojo-mechanic-feed", ready=ready, **fields)), flush=True)


async def run(layout, guild, map_id):
    register_command("raid_mechanics", RaidMechanics)

    async def connection(connected):
        if not connected:
            status()
        await feed.connection(connected)

    async def event(ev):
        if not isinstance(ev, Hello):
            return
        status()
        if ev.guild_id != guild:
            status(error="bridge greeted a different guild")
            return
        if "mechanics" not in ev.features or (map_id == 649 and "mechanics_cast_radius" not in ev.features):
            status(error="required native mechanic capability is missing")
            return
        await feed.event(ev)
        key = (map_id, 0)
        if key not in feed.tables or not await feed.send(key):
            status(error="native did not acknowledge the candidate mechanic table")
            return
        status(True, features=sorted(ev.features), rules=len(feed.tables[key]))

    client = BridgeClient("127.0.0.1", layout.ports["leeroy"], event, on_connection=connection)
    feed = MechanicFeed(SimpleNamespace(bridge=client, is_backlog=lambda ev: False), playbooks())
    worker = asyncio.create_task(client.run())
    try:
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await client.close()
        await asyncio.gather(worker, return_exceptions=True)


def main():
    args = feed_arguments()
    layout = load(args.layout)
    if Path(layout.roots["source"]).resolve() != SOURCE or args.guild <= 0:
        raise ValueError("candidate source or guild mismatch")
    preflight(layout)
    layout.check_port(layout.ports["leeroy"], "leeroy")
    # This check includes free ports: acknowledge it before the parent starts
    # the world. BridgeClient can then wait for the isolated listener to open.
    status(preflight=True)
    asyncio.run(run(layout, args.guild, args.map))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        status(error=type(error).__name__)
        raise SystemExit(1) from None
