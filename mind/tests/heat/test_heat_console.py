"""The console's Heat panel: GET /api/tuning carries the heat view beside the tuning's (which is
untouched), the page has the panel, and the session action goes to the mind's tune op exactly
as the endpoint checks it."""
import asyncio

from console.rig import http, make_app, page_source, running
from console.test_console_tuning import FakeLink, cast, data, served  # noqa: F401 (fixtures)
from leeroy_mind import heat as H
from leeroy_mind.personas.io import load_persona
from leeroy_mind.tuning import changes as C


def test_the_tuning_route_carries_the_heat_view_and_leaves_the_tuning_view_alone(cast, data):  # noqa: F811
    async def go(toml=None):
        if toml is not None:
            (data / "mind.toml").write_bytes(toml)
        app = await served(make_app(data))
        try:
            r = await http(app, "GET", "/api/tuning")
            assert r.status == 200
            return r.json()
        finally:
            await app.close()
    off = asyncio.run(go(b"[talk]\nchattiness = 4\n[romance]\ndial = 9\n[heat]\nenabled = false\n"))
    assert off["heat"]["enabled"] is False and "romance dial is in force" in off["heat"]["note"]
    assert [d["name"] for d in off["view"]["dials"]][:2] == ["talkative", "argumentative"]
    assert "heat" not in {d["name"] for d in off["view"]["dials"] + off["view"]["world_dials"]}
    on = asyncio.run(go(b"[talk]\nchattiness = 4\n[romance]\ndial = 9\n[heat]\nenabled = true\n"))
    heat = on["heat"]
    # on and not set: heat governs nobody, and the page says the romance dial is in force
    assert (heat["enabled"], heat["world"], heat["set"], heat["follows"], heat["suggest"]) == (
        True, None, False, 9, 9)
    assert "romance dial (9)" in heat["note"] and "exactly as before" in heat["note"]
    assert heat["rows"][9] == H.explain_row(9) and heat["members"] == []
    assert on["view"] == {**off["view"]}  # the tuning's own view is what it was
    pic = C.picture(cast, {g: load_persona(cast, g) for g in cast.persona_versions()})
    C.apply_sets(cast, [C.Set("world", "", "heat", 9)], pic, source="console", now=3.0)
    heat = asyncio.run(go())["heat"]
    assert (heat["world"], heat["set"], heat["follows"]) == (9, True, None)
    assert heat["changes"][6] == H.what_changes(9, 6)
    assert {m["guid"] for m in heat["members"]} == {102}  # the one woman of this cast


def test_the_session_action_goes_to_the_mind_as_the_endpoint_checks_it(cast, data):  # noqa: F811
    link = FakeLink({"ok": True, "set": 1, "message": "Clean for everyone, until the morning.",
                     "sessions": {"all": {"mode": "clean", "ends": "morning"}, "members": {}}})

    async def go():
        app = await served(make_app(data, mind=running(data), link=link))
        try:
            body = {"action": "heat", "session": "clean"}
            assert (await http(app, "POST", "/api/tune", body=body)).status == 403  # no origin
            r = await http(app, "POST", "/api/tune", body=body, origin=True)
            assert r.status == 200 and r.json()["sessions"]["all"]["mode"] == "clean"
            assert link.calls[-1][1:3] == ("tune", {"action": "heat", "session": "clean",
                                                    "guid": None})
            bad = await http(app, "POST", "/api/tune", body={"action": "heat", "session": "x"},
                             origin=True)
            assert bad.status == 400 and len(link.calls) == 1
        finally:
            await app.close()
    asyncio.run(go())


def test_heat_is_refused_with_control_off_and_never_reaches_the_mind(cast, data):  # noqa: F811
    """--no-control: the session action, a heat set and a heat preview are all refused at the
    console (403) and nothing is sent to the mind; without the key nothing is either. The
    panel's read still answers."""
    from leeroy_mind.console.control import Controls
    link = FakeLink({"ok": True})

    async def go():
        off = Controls(enabled=False, reason="process control is off (--no-control)",
                       mind=running(data))
        app = await served(make_app(data, mind=running(data), link=link, controls=off))
        try:
            for body in ({"action": "heat", "session": "open"},
                         {"action": "heat", "session": "clean", "guid": 102},
                         {"action": "heat", "session": "clear"},
                         {"action": "set", "scope": "world", "target": "", "dial": "heat",
                          "value": 10, "previewed": False},
                         {"action": "preview", "scope": "world", "target": "", "dial": "heat",
                          "value": 10}):
                r = await http(app, "POST", "/api/tune", body=body, origin=True)
                assert r.status == 403 and r.json()["ok"] is False, body
                assert (await http(app, "POST", "/api/tune", body=body, origin=True,
                                   key=False)).status == 403
            assert [c for c in link.calls if c[1] == "tune" and c[2] != {"action": "status"}] == []
            read = await http(app, "GET", "/api/tuning")
            assert read.status == 200 and "heat" in read.json()
        finally:
            await app.close()
    asyncio.run(go())


def test_the_page_has_the_heat_panel_and_writes_only_through_the_tune_route():
    page = page_source()
    assert 'id="t-heat"' in page
    assert "heatPanel($(\"t-heat\")" in page and "heatCard(box" in page
    assert 'dial: "heat"' in page and 'action: "heat"' in page
    assert 'post("/api/heat' not in page  # no route of its own: the tune op's
