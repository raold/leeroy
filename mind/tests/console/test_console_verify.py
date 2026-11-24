"""The Ops tab's Verification panel: the ledger read from a synthetic install, never the live one."""
import asyncio
import json
import time
from datetime import datetime

from leeroy_mind.console import opsview as ops

SECRET = "PRIVATE_SENTINEL"


def test_the_panel_is_the_ledger_on_this_install(store, tmp_path):
    data = tmp_path  # the store's mind.db
    folder = tmp_path / "install" / "build" / "bin" / "RelWithDebInfo"
    (folder / "configs" / "modules").mkdir(parents=True)
    (folder / "configs" / "modules" / "playerbots.conf").write_text("Playerbots.Enabled = 1\n", encoding="utf-8")
    stamp = datetime.fromtimestamp(time.time() - 60).strftime("%Y-%m-%d %H:%M:%S")
    (folder / "Server.log").write_bytes(f"{stamp} group_rez_cast guid=1 body=2 spell=7328 {SECRET}\n".encode())
    result = ops.OpsView(tmp_path / "install", data)._source("verify", time.monotonic() + 30, {})
    assert result["status"] == "ok"
    rows = {f["id"]: f for f in result["features"]}
    assert rows["group_rez"]["status"] == "seen_working"
    assert rows["lich_king"]["status"] == "switched_off"  # unset in playerbots.conf, compiled off
    assert result["coverage"]["server"]["files"] == 1
    assert SECRET not in json.dumps(result)


def test_the_ledger_is_read_at_most_every_five_minutes(tmp_path):
    now, calls = [1000.], []

    def ledger():
        calls.append(1)
        return ops.panel(features=[])
    sources = {p: lambda: ops.panel() for p in ops.PANELS}
    sources["verify"] = ledger
    view = ops.OpsView(tmp_path, tmp_path, sources=sources, monotonic=lambda: now[0])
    asyncio.run(view.snapshot())
    now[0] += 299
    asyncio.run(view.snapshot())
    assert len(calls) == 1
    now[0] += 1
    asyncio.run(view.snapshot())
    assert len(calls) == 2
    assert ops.BUDGETS["verify"] >= 4 and "verify" in ops.PANELS
