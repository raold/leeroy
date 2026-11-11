"""P2 Task 13a: the console's profile photos. GET /api/selfies, /api/selfies/images and
/api/selfies/member read mind.db and the selfie files read-only; POST /api/selfie is checked as
the endpoint checks it and goes to the running mind's selfie op behind the same gates as raid
actions and dial changes."""
import asyncio
import base64
import contextlib
import hashlib
from importlib import resources
from pathlib import Path

import pytest

from conftest import HUMAN, seed_test_cast
from console.rig import events, http, make_app, page_source, running
from console.test_console_tuning import FakeLink
from leeroy_mind.console import app as console_app
from leeroy_mind.console.control import Controls
from leeroy_mind.protocol import Member
from leeroy_mind.selfies import files
from leeroy_mind.selfies.store import pick, queue_more, undo
from selfies.fakes import ready, renderable_cast


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    store.upsert_cast([
        Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=83, rank=0,
               online=False, map=0, zone=0, spec_tab=-1),
        Member(guid=106, name="Fenna", race=1, class_=1, gender=1, level=80, rank=3,
               online=False, map=0, zone=0, spec_tab=-1),
        Member(guid=107, name="Gorr", race=1, class_=1, gender=0, level=60, rank=3,
               online=False, map=0, zone=0, spec_tab=-1)], 2.0)
    store.set_meta("human_guid", str(HUMAN))
    return store


@contextlib.asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def _tree(data):
    """Every file under the data dir (the selfie images included) by content hash."""
    return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(data.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


def test_images_come_as_data_urls_for_a_ready_set_of_that_member_only(cast, data):
    ready(cast, data, 101, 1)
    queue_more(cast, 102, now=1.0)

    async def go():
        async with served(make_app(data)) as app:
            ok = await http(app, "GET", "/api/selfies/images?guid=101&set=1")
            assert ok.status == 200 and ok.json()["guid"] == 101 and ok.json()["set"] == 1
            assert ok.json()["current"] is None
            assert [i["index"] for i in ok.json()["images"]] == [0, 1, 2, 3]
            for i in ok.json()["images"]:
                assert i["src"].startswith("data:image/jpeg;base64,")
                want = files.candidate(data, 101, 1, i["index"], thumb=True).read_bytes()
                assert base64.b64decode(i["src"].split(",", 1)[1]) == want  # the slot's own file
            assert (await http(app, "GET", "/api/selfies/images?guid=102&set=1")).status == 404
            assert (await http(app, "GET", "/api/selfies/images?guid=101&set=2")).status == 404
            assert (await http(app, "GET",
                               "/api/selfies/images?guid=101&set=..%2F1")).status == 400
            assert (await http(app, "GET", "/api/selfies/images?guid=101")).status == 400
            assert (await http(app, "GET", "/api/selfies/images?guid=1%2B1&set=1")).status == 400
    run(go())


def test_an_oversize_or_missing_thumbnail_is_left_out(cast, data):
    ready(cast, data, 101, 1)
    files.candidate(data, 101, 1, 1, thumb=True).write_bytes(b"x" * (300 * 1024 + 1))
    files.candidate(data, 101, 1, 2, thumb=True).unlink()

    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/api/selfies/images?guid=101&set=1")
            assert [i["index"] for i in r.json()["images"]] == [0, 3]
    run(go())


def test_the_current_slot_is_marked_on_its_set(cast, data):
    ready(cast, data, 101, 1)
    pick(cast, 101, 1, 2, source="console", now=5.0)

    async def go():
        async with served(make_app(data)) as app:
            assert (await http(app, "GET", "/api/selfies/images?guid=101&set=1")
                    ).json()["current"] == 2
    run(go())


def test_the_queue_lists_waiting_sets_with_names_and_counts_without_writing(cast, data):
    ready(cast, data, 101, 1, now=1.0)
    ready(cast, data, 102, 1, now=2.0)
    ready(cast, data, 103, 1, now=3.0)
    pick(cast, 103, 1, 0, source="console", now=4.0)  # 103 has a photo and nothing waiting
    queue_more(cast, 101, now=5.0)  # 101 passes its waiting set and queues four more
    before = _tree(data)

    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/api/selfies")
            assert r.status == 200
            v = r.json()["view"]
            assert v["available"] and v["missing"] == []
            # 104 and 105 have a persona without looks; 106 has no persona; 107 is below 80
            assert v["counts"] == {"chosen": 1, "waiting": 1, "queued": 1, "no_persona": 1,
                                   "no_looks": 2, "no_kind": 0}
            assert v["queue"] == [{"guid": 102, "name": "Belra", "set": 1, "reason": "first",
                                   "rendered": 2.0}]
            assert v["recent"][0]["name"] == "Coros" and v["recent"][0]["undoable"] is True
            assert r.json()["mind"]["ok"] is False  # no link here: read-only, and says so
    run(go())
    assert _tree(data) == before


def test_the_mind_status_gives_the_ops_recent_changes_with_each_ones_undoability(cast, data):
    recent = [{"id": 2, "guid": 101, "undoable": True}, {"id": 1, "guid": 101, "undoable": True}]
    link = FakeLink({"ok": True, "enabled": True, "chosen": 1, "waiting": 0, "queued": 0,
                     "recent": recent})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = (await http(app, "GET", "/api/selfies")).json()
            assert r["mind"] == {"ok": True, "enabled": True, "recent": recent}
            assert r["control"]["enabled"] is True
            assert link.calls[-1][1:3] == ("selfie", {"action": "status"})
    run(go())


def test_the_member_view_has_the_photo_in_force_the_sets_and_the_history(cast, data):
    ready(cast, data, 101, 1)
    first = pick(cast, 101, 1, 1, source="console", now=5.0)
    queue_more(cast, 101, now=6.0)
    undo(cast, first, source="console", now=7.0)

    async def go():
        async with served(make_app(data)) as app:
            v = (await http(app, "GET", "/api/selfies/member?guid=101")).json()
            assert v["guid"] == 101 and v["current"] is None
            assert [(s["set"], s["status"], s["decided"]) for s in v["sets"]] == [
                (1, "ready", None), (2, "queued", None)]
            assert [(h["label"], h["undoable"]) for h in v["history"]] == [
                (f"undo #{first}", True), ("pick", False)]
            made = pick(cast, 101, 1, 3, source="console", now=8.0)
            cur = (await http(app, "GET", "/api/selfies/member?guid=101")).json()["current"]
            assert cur == {"set": 1, "index": 3, "pick": made}
            assert (await http(app, "GET", "/api/selfies/member?guid=999")).status == 404
            assert (await http(app, "GET", "/api/selfies/member?guid=x")).status == 400
            assert (await http(app, "GET", "/api/selfies/member")).status == 400
    run(go())


def test_an_older_mind_db_says_photos_are_unavailable(store, data, monkeypatch):
    from leeroy_mind.storage import db
    migrations = db._migration_files()
    old = data / "old"
    old.mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in migrations if n <= 17])
        db.open_db(old / "mind.db").close()

    async def go():
        async with served(make_app(old)) as app:
            r = await http(app, "GET", "/api/selfies")
            assert r.status == 200 and r.json()["view"]["available"] is False
            assert "selfie_set" in r.json()["view"]["missing"]
            assert (await http(app, "GET", "/api/selfies/images?guid=101&set=1")).status == 404
            assert (await http(app, "GET", "/api/selfies/member?guid=101")).status == 404
    run(go())


PICK = {"action": "pick", "guid": 101, "set": 1, "index": 2}


def test_a_selfie_change_needs_the_key_the_origin_and_json_and_goes_to_the_mind(cast, data):
    link = FakeLink({"ok": True, "pick": 3, "guid": 101, "message": "Photo set (change #3)."})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/selfie", body=PICK)).status == 403
            assert (await http(app, "POST", "/api/selfie", body=PICK, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/selfie", body=b"x", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert link.calls == []
            r = await http(app, "POST", "/api/selfie", body=PICK, origin=True)
            assert r.status == 200 and r.json()["pick"] == 3
            assert link.calls[-1][1:3] == ("selfie", PICK)
            # longer than the op can take: a held chosen.jpg retries for about 2 s
            assert link.calls[-1][3] > console_app.SELFIE_S
    run(go())
    assert console_app.SELFIE_S == 10.0


@pytest.mark.parametrize("body", [
    {"action": "write"}, {**PICK, "index": 4}, {**PICK, "extra": 1}, {**PICK, "guid": "101"},
    {"action": "more"}, {"action": "undo", "pick": 0}, {"action": "undo", "pick": True},
    ["pick"], {}])
def test_a_request_not_in_the_endpoints_form_never_leaves_the_console(cast, data, body):
    link = FakeLink({"ok": True})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/selfie", body=body, origin=True)).status == 400
    run(go())
    assert link.calls == []


def test_a_selfie_change_is_refused_with_control_off_and_said_plainly_with_the_mind_stopped(
        cast, data):
    async def go():
        off = Controls(enabled=False, reason="process control is off (--no-control)",
                       mind=running(data))
        async with served(make_app(data, mind=running(data), controls=off,
                                   link=FakeLink({}))) as app:
            r = await http(app, "POST", "/api/selfie", body=PICK, origin=True)
            assert r.status == 403 and "--no-control" in r.json()["error"]
        async with served(make_app(data, link=FakeLink({}))) as app:
            r = await http(app, "POST", "/api/selfie", body=PICK, origin=True)
            assert r.status == 409 and r.json()["error"] == (
                "The mind isn't running, so it can't set photos. Start it from the Mind tab.")
            v = (await http(app, "GET", "/api/selfies")).json()
            assert v["view"]["available"] and v["mind"]["ok"] is False
            assert v["mind"]["error"].startswith("The mind isn't running")
    run(go())


@pytest.mark.parametrize("said,words", [
    ("photos are not running in this mind", "Photos are off in this mind."),
    ("another photo change is still being written",
     "Another change is still being saved; try again in a moment."),
    ("changed since by #4: undo that first", "changed since by #4: undo that first")])
def test_the_minds_refusals_are_worded_for_the_page(cast, data, said, words):
    link = FakeLink({"ok": False, "error": said})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "POST", "/api/selfie", body=PICK, origin=True)
            assert r.status == 409 and r.json() == {"ok": False, "error": words}
            s = (await http(app, "GET", "/api/selfies")).json()["mind"]
            assert s == {"ok": False, "error": words}
    run(go())


def test_a_mind_older_than_the_selfie_op_is_said_in_words(cast, data):
    # final review minor 2: such a mind names the ops it has; the page says what to do instead
    link = FakeLink({"ok": False, "error": "bad request: op is one of status, stop, raid, tune, say"})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "POST", "/api/selfie", body=PICK, origin=True)
            assert r.status == 409 and r.json() == {"ok": False,
                                                    "error": console_app.SELFIE_OLD_MIND}
            s = (await http(app, "GET", "/api/selfies")).json()["mind"]
            assert s == {"ok": False, "error": console_app.SELFIE_OLD_MIND}
    run(go())
    assert console_app.SELFIE_OLD_MIND == ("The running mind is older than this console and "
                                           "cannot set photos: restart it from the Mind tab.")


def test_a_selfie_change_is_announced_to_other_pages(cast, data):
    replies = {"pick": {"ok": True, "pick": 3, "guid": 101, "message": "Photo set."},
               "undo": {"ok": True, "pick": 4, "guid": 101, "message": "Undone."},
               "more": {"ok": True, "set": 2, "guid": 102, "message": "Four more tonight."},
               "status": {"ok": True, "enabled": True, "recent": []}}
    link = FakeLink(lambda op, args: replies[args["action"]])
    bodies = [PICK, {"action": "undo", "pick": 3}, {"action": "more", "guid": 102},
              {"action": "status"}]

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            listen = asyncio.create_task(events(app, count=4, timeout=3))
            await asyncio.sleep(0.2)
            for body in bodies:
                assert (await http(app, "POST", "/api/selfie", body=body,
                                   origin=True)).status == 200
            seen = [e for e in await listen if e[0] == "selfie"]
            assert seen == [("selfie", {"action": "pick", "guid": 101}),
                            ("selfie", {"action": "undo", "guid": 101}),
                            ("selfie", {"action": "more", "guid": 102})]
    run(go())


def test_a_refused_change_is_not_announced(cast, data):
    link = FakeLink({"ok": False, "error": "set 1 is not ready for this member"})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            listen = asyncio.create_task(events(app, count=2, timeout=1.0))
            await asyncio.sleep(0.2)
            assert (await http(app, "POST", "/api/selfie", body=PICK, origin=True)).status == 409
            try:
                seen = await listen
            except TimeoutError:
                seen = []
            assert all(kind != "selfie" for kind, _ in seen)
    run(go())


def _between(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def test_the_page_has_the_choose_selfies_card():
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    personas = _between(page, 'id="tab-personas"', 'id="p-tuning"')
    card = _between(personas, 'id="p-selfies"', "</article>")
    assert personas.index('id="p-selfies"') < personas.index("<h3>The cast</h3>")
    assert "<h3>Choose selfies</h3>" in card and "Recent choices" in card
    for i in ("sf-counts", "sf-lead", "sf-queue", "sf-status", "sf-recent"):
        assert f'id="{i}"' in card, i
    # the keyboard lands on the status line after a change (final review minor 3)
    assert '<p id="sf-status" class="why" role="status" tabindex="-1">' in card
    detail = _between(page, 'id="p-detail"', '<section id="tab-')
    photo = _between(detail, 'id="p-photo"', "</article>")
    assert "<h3>Profile photo</h3>" in photo and ">4 more</button>" in photo
    assert '<p id="p-photostatus" class="why" role="status" tabindex="-1">' in photo
    assert 'title="Pass on these and render four new ones tonight"' in photo
    source = page_source()  # the page and its modules
    assert 'api("/api/selfies", { poll: true })' in source and '"/api/selfies/images?guid="' in source
    assert "refocus(args, refused, status);" in source  # after the redraw a change makes
    assert "forgetMemberShots(obj(d).guid)" in source   # the selfie event: its sets read again
    assert '"/api/selfies/member?guid="' in source and 'post("/api/selfie", ' in source
    for words in ("Use photo ", " of 4 for ", "Pass on these and render four new ones tonight",
                  " four photos waiting. Click the one that fits; it becomes their photo everywhere.",
                  "No photos are waiting. The night renders four for every member without one.",
                  "No photo yet: a coloured initials circle shows instead.",
                  "The mind isn't running, so it can't set photos. Start it from the Mind tab.",
                  "rendering tonight", "no looks yet", "no persona"):
        assert words in source, words
    assert console_app.SELFIE_DOWN in source  # the page says what the console says
    assert "selfie: 1" in source  # the event stream's selfie events reach the page
    assert "photo: \"the night's photo-kind pass\"" in source  # who wrote a persona (Task 3)
    assert '"photo kind"' in source  # the member card's photo kind line (Task 2)
