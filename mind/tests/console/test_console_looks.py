"""His weight and bust edit in the console (owner, 2026-11-15): POST /api/looks is checked as the
endpoint checks it and goes to the running mind's looks op behind the same gates as photo
changes; a done change is announced as a `looks` event; the Personas member detail carries the
form's limits and the page its Looks editor. Every person here is invented."""
import asyncio
import contextlib
from importlib import resources
from pathlib import Path

import pytest

from conftest import HUMAN, seed_test_cast
from console.rig import events, http, make_app, page_source, running
from console.test_console_tuning import FakeLink
from leeroy_mind.console import app as console_app
from leeroy_mind.console.control import Controls
from leeroy_mind.protocol import Member
from selfies.fakes import renderable_cast

SET = {"action": "set", "guid": 101, "weight_kg": 88, "bust": None}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 2.0)
    store.set_meta("human_guid", str(HUMAN))
    return store


@contextlib.asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_a_looks_change_needs_the_key_the_origin_and_json_and_goes_to_the_mind(cast, data):
    link = FakeLink({"ok": True, "guid": 101, "ledger": 4, "set": 2,
                     "message": "Saved (ledger #4). A new set renders tonight."})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/looks", body=SET)).status == 403
            assert (await http(app, "POST", "/api/looks", body=SET, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/looks", body=b"x", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert link.calls == []
            r = await http(app, "POST", "/api/looks", body=SET, origin=True)
            assert r.status == 200 and r.json()["ledger"] == 4
            assert r.json()["message"].endswith("A new set renders tonight.")
            assert link.calls[-1][1:3] == ("looks", SET)
            assert link.calls[-1][3] > console_app.SELFIE_S
            undo = {"action": "undo", "ledger": 4}
            assert (await http(app, "POST", "/api/looks", body=undo, origin=True)).status == 200
            assert link.calls[-1][1:3] == ("looks", undo)
    run(go())


@pytest.mark.parametrize("body", [
    {"action": "write"}, {**SET, "weight_kg": 39}, {**SET, "weight_kg": 181},
    {**SET, "glutes": "huge"}, {**SET, "waist": 2}, {**SET, "thighs": "large"},
    {**SET, "weight_kg": "88"}, {**SET, "bust": "huge"}, {**SET, "extra": 1},
    {"action": "set", "guid": 101, "weight_kg": 88}, {**SET, "guid": "101"},
    {"action": "undo", "ledger": 0}, {"action": "undo", "ledger": True}, ["set"], {}])
def test_a_request_not_in_the_endpoints_form_never_leaves_the_console(cast, data, body):
    link = FakeLink({"ok": True})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/looks", body=body, origin=True)).status == 400
    run(go())
    assert link.calls == []


def test_a_looks_change_is_refused_with_control_off_and_said_plainly_with_the_mind_stopped(
        cast, data):
    async def go():
        off = Controls(enabled=False, reason="process control is off (--no-control)",
                       mind=running(data))
        async with served(make_app(data, mind=running(data), controls=off,
                                   link=FakeLink({}))) as app:
            r = await http(app, "POST", "/api/looks", body=SET, origin=True)
            assert r.status == 403 and "--no-control" in r.json()["error"]
        async with served(make_app(data, link=FakeLink({}))) as app:
            r = await http(app, "POST", "/api/looks", body=SET, origin=True)
            assert r.status == 409 and r.json()["error"] == console_app.LOOKS_DOWN
    run(go())


@pytest.mark.parametrize("said,words", [
    ("bad request: op is one of status, stop, raid, tune, selfie", console_app.LOOKS_OLD_MIND),
    ("another photo change is still being written",
     "Another change is still being saved; try again in a moment."),
    ("Bust is for women only.", "Bust is for women only.")])
def test_the_minds_refusals_are_worded_for_the_page(cast, data, said, words):
    link = FakeLink({"ok": False, "error": said})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "POST", "/api/looks", body=SET, origin=True)
            assert r.status == 409 and r.json() == {"ok": False, "error": words}
    run(go())


def test_only_a_done_change_is_announced(cast, data):
    replies = iter([{"ok": True, "guid": 101, "ledger": 4, "set": 2, "message": "Saved."},
                    {"ok": True, "guid": 101, "ledger": None, "set": None,
                     "message": "Nothing changed."},
                    {"ok": False, "error": "No such member in the cast."},
                    {"ok": True, "guid": 101, "ledger": 4, "set": 2, "message": "Undone."}])
    link = FakeLink(lambda op, args: next(replies) if op == "looks" else {"ok": True})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            listen = asyncio.create_task(events(app, count=3, timeout=3))  # the snapshot first
            await asyncio.sleep(0.2)
            for body in (SET, SET, SET, {"action": "undo", "ledger": 4}):
                await http(app, "POST", "/api/looks", body=body, origin=True)
            try:
                seen = await listen
            except TimeoutError:
                seen = []
            return [e for e in seen if e[0] == "looks"]
    seen = run(go())
    assert seen == [("looks", {"action": "set", "guid": 101}),
                    ("looks", {"action": "undo", "guid": 101})]


def test_the_member_detail_carries_the_forms_limits(cast, data):
    async def go():
        async with served(make_app(data)) as app:
            j = (await http(app, "GET", "/api/persona?guid=101")).json()
            assert j["looks_limits"] == {"weight_kg": [40, 180],
                                         "busts": ["small", "medium", "large", "very_large"],
                                         "glutes": ["small", "medium", "large", "very_large"],
                                         "waists": ["very_narrow", "narrow", "average", "wide"],
                                         "hips": ["narrow", "average", "wide", "very_wide"]}
            assert j["card"]["appearance"]["weight_kg"] == 64
    run(go())


def test_the_page_has_the_looks_editor_and_listens_for_looks_events():
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    looks = page[page.index("<h3>Looks</h3>"):page.index("</article>", page.index("<h3>Looks</h3>"))]
    assert 'id="p-looks"' in looks and 'id="p-looks-edit"' in looks
    source = page_source()
    assert 'post("/api/looks"' in source and "looksEditor(" in source
    assert "looks: 1" in source  # main.mjs passes the event on
    assert "renders tonight" not in resources.files("leeroy_mind.console").joinpath(
        "static", "js", "looks.mjs").read_text("utf-8")  # the mind's own words say it
