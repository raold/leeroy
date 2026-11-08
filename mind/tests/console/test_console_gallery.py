"""Retained photos are read-only; only the existing selfie op changes a cover."""
import asyncio
from pathlib import Path

import pytest

from conftest import seed_test_cast
from console.rig import http, make_app, running
from console.test_console_selfies import served, _tree
from console.test_console_tuning import FakeLink
from leeroy_mind.console import feed, galleryview
from leeroy_mind.console.control import Controls
from leeroy_mind.selfies import files
from leeroy_mind.selfies.snaps import expire
from leeroy_mind.selfies.store import pick
from selfies.fakes import ready, ready_snap, planned_snap


@pytest.fixture
def data(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def test_gallery_orders_every_rendered_photo_and_marks_the_cover(store, data):
    ready(store, data, 101, 1, now=1)
    ready(store, data, 101, 2, now=3)
    ready(store, data, 102, 1, now=4)
    snap = ready_snap(store, data, 101)
    with store.transaction():
        store.conn.execute("UPDATE selfie_snap SET rendered=2 WHERE id=?", (snap,))
    expire(store, snap, "passed", now=5)
    planned_snap(store, 101)
    pick(store, 101, 1, 2, source="console", now=6)
    files.candidate(data, 101, 2, 3).unlink()
    files.candidate(data, 101, 2, 2, thumb=True).unlink()
    before = _tree(data)

    async def go():
        async with served(make_app(data)) as app:
            response = await http(app, "GET", "/api/selfies/gallery?guid=101")
            assert response.status == 200
            photos = response.json()["view"]["photos"]
            assert [p["rendered"] for p in photos] == [3, 3, 3, 2, 1, 1, 1, 1]
            assert [p["id"] for p in photos if p["cover"]] == ["candidate:1:2"]
            assert photos[2]["thumbnail"] is None
            assert photos[3]["kind"] == "snap"
            assert all("prompt" not in p and "scene" not in p for p in photos)
            for p in photos:
                image = await http(app, "GET", p["image"])
                assert image.status == 200 and image.header("content-type") == "image/jpeg"
                assert image.body.startswith(b"\xff\xd8\xff")
                if p["thumbnail"]:
                    assert (await http(app, "GET", p["thumbnail"])).status == 200
            assert (await http(app, "GET", "/api/selfies/photo?guid=102&kind=snap&id=" + str(snap))).status == 404
    asyncio.run(go())
    assert _tree(data) == before


@pytest.mark.parametrize("path", ["/api/selfies/gallery?guid=101",
                                  "/api/selfies/photo?guid=101&kind=candidate&set=1&index=0"])
def test_gallery_reads_keep_key_host_and_origin_checks(store, data, path):
    ready(store, data, 101, 1)

    async def go():
        async with served(make_app(data)) as app:
            assert (await http(app, "GET", path, key=False)).status == 403
            assert (await http(app, "GET", path, host="wrong.invalid")).status == 403
            assert (await http(app, "GET", path, origin="https://wrong.invalid")).status == 403
            assert (await http(app, "GET", path)).status == 200
    asyncio.run(go())


@pytest.mark.parametrize("query", ["", "guid=101", "guid=../101&kind=snap&id=1",
                                    "guid=101&kind=candidate&set=1&index=4",
                                    "guid=101&kind=snap&id=9223372036854775808",
                                    "guid=101&kind=snap&id=1&thumb=0",
                                    "guid=101&kind=snap&id=1&guid=102"])
def test_bad_photo_ids_are_refused(data, query):
    async def go():
        async with served(make_app(data)) as app:
            assert (await http(app, "GET", "/api/selfies/photo?" + query)).status == 400
    asyncio.run(go())


def test_old_database_without_snaps_never_migrates(store, data):
    ready(store, data, 101, 1)
    with store.transaction():
        store.conn.execute("DROP TABLE selfie_snap")
    view = feed.read(data / "mind.db", galleryview.gallery, data, 101)
    assert view["missing"] == ["selfie_snap"] and len(view["photos"]) == 4
    with store.transaction():
        for table in ("selfie_current", "selfie_pick", "selfie_candidate", "selfie_set"):
            store.conn.execute(f"DROP TABLE {table}")
    view = feed.read(data / "mind.db", galleryview.gallery, data, 101)
    assert view["photos"] == [] and set(view["missing"]) == set(galleryview.TABLES)


def test_gallery_leaves_out_oversized_files_and_refuses_non_jpegs(store, data):
    ready(store, data, 101, 1)
    files.candidate(data, 101, 1, 0).write_bytes(b"x" * (galleryview.MAX_FACE_BYTES + 1))
    files.candidate(data, 101, 1, 1).write_bytes(b"not a jpeg")
    view = galleryview.gallery(store.conn, data, 101)
    assert "candidate:1:0" not in [p["id"] for p in view["photos"]]
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 1) is None
    assert galleryview.photo(store.conn, data, 102, "candidate", 1, 1) is None


def test_cover_still_uses_the_existing_control_gates_and_pick_arguments(data):
    args = {"action": "pick", "guid": 101, "set": 1, "index": 2}
    link = FakeLink({"ok": True, "pick": 1, "guid": 101})

    async def go():
        controls = Controls(enabled=False, reason="control is off (--no-control)", mind=running(data))
        async with served(make_app(data, controls=controls, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/selfie", body=args, origin=True)).status == 403
            state = (await http(app, "GET", "/api/selfies/gallery?guid=101")).json()
            assert state["control"]["enabled"] is False
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "POST", "/api/selfie", body=args)).status == 403
            assert (await http(app, "POST", "/api/selfie", body=args, origin=True)).status == 200
    asyncio.run(go())
