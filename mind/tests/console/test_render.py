"""The console's Render selfies route and its strip state: fake links only, no mind or GPU."""
import asyncio

import pytest

from conftest import run
from leeroy_mind.console import app as console_app
from console.rig import http, make_app, running
from console.test_console_library import FakeLink

IDLE = {"selfies": True, "snaps": True, "running": False, "stopping": False, "progress": None,
        "backlog": {"first": 1, "more": 2, "snaps": 0, "held": 0, "total": 3, "images": 12,
                    "estimate_s": 200},
        "refusal": None, "code": None, "last": None}
STARTED = {**IDLE, "running": True, "backlog": None,
           "progress": {"done": 0, "total": 3, "phase": "sets", "first": 1, "more": 2, "snaps": 0}}


def test_route_gates_and_no_control_never_forward(tmp_path):
    async def go():
        link = FakeLink({"ok": True, "render": STARTED})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            await app.refresh(force=True)
            for kwargs in ({"key": False, "origin": True}, {"origin": False},
                           {"origin": "https://elsewhere.invalid"},
                           {"origin": True, "host": "wrong.invalid"},
                           {"origin": True, "headers": {"Sec-Fetch-Site": "cross-site"}}):
                r = await http(app, "POST", "/api/selfies/render", body={"action": "render"}, **kwargs)
                assert r.status == 403
            assert not link.calls
            for body in ({"action": "pick", "guid": 1, "set": 1, "index": 0}, {"action": "render", "x": 1},
                         {"action": "erase"}):
                r = await http(app, "POST", "/api/selfies/render", origin=True, body=body)
                assert r.status == 400 and r.json()["error"] == "invalid_action"
            assert not link.calls
            for action in ("render", "render_stop"):  # only through its own route
                r = await http(app, "POST", "/api/selfie", origin=True, body={"action": action})
                assert r.status == 400
            assert not link.calls
            app.controls.enabled = False
            app.controls.reason = "Control is off: this console runs with --no-control."
            for action in ("render", "render_stop"):
                r = await http(app, "POST", "/api/selfies/render", origin=True, body={"action": action})
                assert r.status == 403 and r.json()["error"] == "control_disabled"
                assert r.json()["message"] == app.controls.reason
                assert r.json()["render"]["enabled"] is False
            assert app.strip()["render"]["refusal"] == app.controls.reason
            assert not link.calls
            app.controls.enabled = True
            r = await http(app, "POST", "/api/selfies/render", origin=True, body={"action": "render"})
            assert r.status == 200 and r.json()["ok"] and r.json()["render"]["running"]
            assert link.calls[-1][1:] == ("selfie", {"action": "render"})
            assert link.timeouts[-1] >= 75  # a stop waits up to 60 s for the worker tree
            assert app.strip()["render"]["progress"]["total"] == 3
            link.reply = {"ok": False, "error": "in_game", "message": "Log out of WoW first.",
                          "render": {**IDLE, "code": "in_game", "refusal": "Log out of WoW first."}}
            r = await http(app, "POST", "/api/selfies/render", origin=True, body={"action": "render"})
            assert r.status == 409 and r.json()["message"] == "Log out of WoW first."
            assert app.strip()["render"]["refusal"] == "Log out of WoW first."
        finally:
            await app.close()
    run(go())


def test_mind_down_or_unanswered_says_so_without_forwarding(tmp_path):
    async def go():
        link = FakeLink({"ok": True})
        app = make_app(tmp_path, link=link)
        await app.start()
        try:
            await app.refresh(force=True)
            r = await http(app, "POST", "/api/selfies/render", origin=True, body={"action": "render"})
            assert r.status == 409 and r.json()["error"] == "mind_unavailable"
            assert r.json()["render"]["running"] is False and r.json()["render"]["code"] == "mind"
            assert not link.calls
        finally:
            await app.close()
        link = FakeLink(error="the control endpoint did not answer (OSError)")
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            await app.refresh(force=True)
            r = await http(app, "POST", "/api/selfies/render", origin=True, body={"action": "render_stop"})
            assert r.status == 409 and "did not confirm" in r.json()["message"]
        finally:
            await app.close()
    run(go())


@pytest.mark.parametrize("reply,expect", [
    ({"ok": True, "selfie_render": STARTED}, {"running": True, "selfies": True}),
    ({"ok": True}, {"running": False, "selfies": False, "code": "off"}),
    ({"ok": True, "selfie_render": None}, {"running": False, "code": "unknown"}),
])
def test_the_strip_reads_the_mind_status(tmp_path, reply, expect):
    async def go():
        link = FakeLink({**reply, "typing": [], "gpu": None})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            app._subs.add(console_app._Subscriber())  # a page listening: the strip asks the status
            try:
                await app.refresh(force=True)
            finally:
                app._subs.clear()
            view = app.strip()["render"]
            assert {k: view[k] for k in expect} == expect
            assert view["enabled"] is True and view["mind_down"] is False
        finally:
            await app.close()
    run(go())
