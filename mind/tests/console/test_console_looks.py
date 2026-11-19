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
    {**SET, "owner_hair_colour": "green"}, {**SET, "owner_hair_style": "pigtails"},
    {**SET, "owner_hair_length": "shaved"}, {**SET, "wardrobe": "casual"},
    {**SET, "wardrobe": ["casual", "casual"]}, {**SET, "wardrobe": ["tweed"]},
    {**SET, "wardrobe": ["casual", "sporty", "smart", "edgy", "boho"]},
    {**SET, "palette": ["black", "red", "navy", "gold"]}, {**SET, "palette": None},
    {**SET, "makeup": "heavy"}, {**SET, "glasses": True}, {**SET, "jewellery": "lots"},
    {**SET, "tattoos": "face"}, {**SET, "piercings": "navel"}, {**SET, "hair_colour": "black"},
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
            limits = j["looks_limits"]
            assert {k: limits[k] for k in ("weight_kg", "busts", "glutes", "waists", "hips")} == {
                "weight_kg": [40, 180], "busts": ["small", "medium", "large", "very_large",
                                                "c_cup", "d_cup", "e_cup", "f_cup", "g_cup", "h_cup"],
                "glutes": ["small", "medium", "large", "very_large"],
                "waists": ["very_narrow", "narrow", "average", "wide"],
                "hips": ["narrow", "average", "wide", "very_wide"]}
            # his hair, wardrobe, makeup and accessories: the schema's own values
            from leeroy_mind.personas import schema
            assert limits["hair_colours"] == list(schema.HAIR_COLOURS) and len(limits["hair_colours"]) == 16
            assert limits["hair_lengths"] == ["buzzed", "short", "chin", "shoulder", "long", "very_long"]
            assert limits["hair_styles"] == list(schema.HAIR_STYLES)
            assert limits["men_hair_styles"] == ["crew_cut", "fade", "quiff"]
            assert limits["wardrobes"] == list(schema.WARDROBES) and len(limits["wardrobes"]) == 12
            assert limits["palette"] == list(schema.PALETTE) and len(limits["palette"]) == 12
            assert limits["makeups"] == ["none", "natural", "defined", "glam"]
            assert limits["glasses"] == ["never", "always"]
            assert limits["jewellery"] == ["none", "minimal", "statement"]
            assert limits["tattoos"] == ["none", "small", "sleeve"]
            assert limits["piercings"] == ["none", "ears", "nose", "lip"]
            assert j["card"]["lust_style"] is False  # the swim tag is offered only with it
            assert "owner_hair_colour" not in j["card"]["appearance"]  # unset: not in the card
            assert "wardrobe" not in j["card"]["appearance"]
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


def test_one_request_carries_the_hair_wardrobe_and_accessories_to_the_mind(cast, data):
    body = {**SET, "owner_hair_colour": "dark_blonde", "owner_hair_length": "long",
            "owner_hair_style": None, "wardrobe": ["casual", "sporty"], "palette": [],
            "makeup": "natural", "glasses": "never", "jewellery": None, "tattoos": "small",
            "piercings": "ears"}
    link = FakeLink({"ok": True, "guid": 101, "ledger": 5, "set": 2, "message": "Saved."})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "POST", "/api/looks", body=body, origin=True)
            assert r.status == 200
            assert link.calls[-1][1:3] == ("looks", body)
    run(go())


def test_the_editor_groups_body_hair_and_style_and_says_what_a_save_does():
    files = resources.files("leeroy_mind.console").joinpath("static")
    looks = files.joinpath("js", "looks.mjs").read_text("utf-8")
    model = files.joinpath("js", "looksmodel.mjs").read_text("utf-8")
    css = files.joinpath("console.css").read_text("utf-8")
    assert 'GROUPS = ["Body", "Hair", "Style"]' in model
    assert "SAVE_WORDS" in looks  # the line under the form, when the mind has said nothing yet
    assert "one new persona version" in model and "one new set of 4 photos" in model
    assert "photo booth" in model and "pick one of them" in model
    assert "may not show in back views" in model  # where what he set cannot fit
    assert looks.count('post("/api/looks"') == 1  # one save, one request
    assert ".looks-group" in css and ".looks-set" in css and "minmax(min(100%, 11rem), 1fr)" in css
    for word in ("pigtail", "bunches"):  # never an age-guard word among the options
        assert word not in model and word not in looks
