import asyncio
import logging

import pytest

from console.rig import http, make_app, running
from console.test_console_tuning import FakeLink
from leeroy_mind.console.control import Controls
from tuning.test_nudges import ARGS, TEXT


def test_nudge_projection_uses_its_supplied_clock_for_loading_and_badges(tmp_path, monkeypatch):
    from tuning.test_tuning_preview import _app
    from leeroy_mind.console.tuneview import tuning_view
    async def go():
        app = _app(tmp_path, loaded=False)
        try:
            assert (await app.tuning.handle(ARGS))["ok"]
            now = app.clock.now()
            monkeypatch.setattr("leeroy_mind.tuning.state.time.time", lambda: now + 86400)
            view = tuning_view(app.store.conn, now=now)
            member = next(m for m in view["members"] if m["guid"] == 102)
            assert len(member["nudges"]) == 1
            assert len(view["nudges"]["active"]) == 1
        finally:
            app.store.close()
    asyncio.run(go())


def test_default_console_fixture_ops_never_calls_machine_probes(tmp_path, monkeypatch):
    from leeroy_mind.console import opsview
    def forbidden(*args):
        raise AssertionError("A fixture requested a machine probe")
    monkeypatch.setattr(opsview, "health_view", forbidden)
    monkeypatch.setattr(opsview, "schedules_view", forbidden)
    async def go():
        app = make_app(tmp_path)
        result = await app._ops.snapshot()
        assert set(result["panels"]) == set(opsview.PANELS)
    asyncio.run(go())


@pytest.mark.parametrize("verb", ["set", "preview", "retire"])
def test_nudge_actions_keep_console_key_origin_and_no_control_gates(tmp_path, verb, caplog):
    body = {**ARGS, "verb": verb} if verb != "retire" else dict(action="nudge", verb=verb, id=1)
    async def go():
        link = FakeLink({"ok": True, "message": "Saved", "change": 1})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            assert (await http(app, "POST", "/api/tune", body=body)).status == 403
            assert (await http(app, "POST", "/api/tune", body=body, key=False, origin=True)).status == 403
            assert (await http(app, "POST", "/api/tune", body=body, origin="http://elsewhere.invalid")).status == 403
            assert not link.calls
            assert (await http(app, "POST", "/api/tune", body=body, origin=True)).status == 200
            if verb == "preview":
                assert link.calls[-1][-1] >= 40
        finally:
            await app.close()
        off = Controls(enabled=False, reason="control off (--no-control)", mind=running(tmp_path))
        app = make_app(tmp_path, mind=running(tmp_path), link=link, controls=off)
        await app.start()
        try:
            assert (await http(app, "POST", "/api/tune", body=body, origin=True)).status == 403
        finally:
            await app.close()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert TEXT not in caplog.text
