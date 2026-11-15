"""Retained photos are read-only; only the existing selfie op changes a cover."""
import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import TEST_CAST, seed_test_cast
from console.rig import http, make_app, running
from console.test_console_selfies import served, _tree
from console.test_console_tuning import FakeLink
from leeroy_mind.console import feed, galleryview
from leeroy_mind.console.control import Controls
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies import files
from leeroy_mind.selfies.snaps import expire, looks_hash
from leeroy_mind.selfies.store import pick, undo
from leeroy_mind.wow import CLASSES, RACES
from selfies.fakes import APPEARANCE, ready, ready_snap, planned_snap, renderable_cast


@pytest.fixture
def data(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def test_gallery_orders_every_rendered_photo_and_marks_the_cover(store, data):
    renderable_cast(store, 101)
    ready(store, data, 101, 1, now=1)
    ready(store, data, 101, 2, now=3)
    ready(store, data, 102, 1, now=4)
    snap = ready_snap(store, data, 101, looks=looks_hash(APPEARANCE))
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


@pytest.mark.parametrize('marker', [False, True])
def test_promoted_world_sets_are_hidden_but_retained_and_cast_passes_remain(store,data,marker):
    ready(store,data,101,1,now=1)
    ready(store,data,101,2,now=2)
    with store.transaction():
        store.conn.execute("UPDATE selfie_set SET decided='passed',night='2026-11-01' WHERE guid=101")
        if marker:
            store.set_meta('selfies:set-world:101:1','1')
        else:
            store.conn.execute("INSERT INTO night_step VALUES ('2026-11-01','world_selfies','101:1',3)")
    before=_tree(data)
    view=galleryview.gallery(store.conn,data,101)
    assert len(view['photos']) == 4 and {p['set'] for p in view['photos']} == {2}
    assert galleryview.photo(store.conn,data,101,'candidate',1,0) is None
    assert galleryview.photo(store.conn,data,101,'candidate',2,0) is not None
    assert _tree(data) == before


def test_gallery_leaves_out_oversized_files_and_refuses_non_jpegs(store, data):
    ready(store, data, 101, 1)
    files.candidate(data, 101, 1, 0).write_bytes(b"x" * (galleryview.MAX_FACE_BYTES + 1))
    files.candidate(data, 101, 1, 1).write_bytes(b"not a jpeg")
    view = galleryview.gallery(store.conn, data, 101)
    assert "candidate:1:0" not in [p["id"] for p in view["photos"]]
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 1) is None
    assert galleryview.photo(store.conn, data, 102, "candidate", 1, 1) is None


@pytest.mark.parametrize("parent", [False, True])
def test_gallery_refuses_a_file_or_parent_link_outside_selfies(store, data, tmp_path, parent):
    ready(store, data, 101, 1)
    image = files.candidate(data, 101, 1, 0)
    outside = tmp_path / "outside"
    outside.mkdir()
    if parent:
        folder = image.parent
        folder.rename(outside / "set")
        target, link = outside / "set", folder
    else:
        target, link = outside / "photo.jpg", image
        image.rename(target)
    try:
        os.symlink(target, link, target_is_directory=parent)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {type(exc).__name__}")
    view = galleryview.gallery(store.conn, data, 101)
    assert "candidate:1:0" not in [p["id"] for p in view["photos"]]
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 0) is None


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


def test_gallery_hides_a_lust_set_of_a_member_who_left_the_style_unless_it_is_the_cover(store, data):
    ready(store, data, 101, 1, now=1)
    ready(store, data, 101, 2, now=2)
    store.set_meta("selfies:set-lust:101:2", "1")
    seen = galleryview.gallery(store.conn, data, 101)["photos"]
    assert {p["set"] for p in seen} == {1}
    assert galleryview.photo(store.conn, data, 101, "candidate", 2, 0) is None
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 0) is not None
    pick(store, 101, 2, 1, source="console", now=3)  # already her photo (picked while lust-styled)
    seen = galleryview.gallery(store.conn, data, 101)["photos"]
    assert [p["id"] for p in seen if p["cover"]] == ["candidate:2:1"]
    assert galleryview.photo(store.conn, data, 101, "candidate", 2, 1) is not None


def _new_looks(store, guid, **looks) -> str:
    """Her looks changed (a new persona version, as the night's looks pass or an owner edit
    leaves it); the looks hash a situation planned from them carries."""
    _, race, cls = TEST_CAST[guid]
    p = load_persona(store, guid)
    appearance = replace(p.appearance, **looks)
    save_persona(store, replace(p, appearance=appearance), source="owner", ts=2000.0,
                 race=RACES[race], class_name=CLASSES[cls])
    return looks_hash(appearance)


def _rows(store):
    return [tuple(store.conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall())
            for t in ("selfie_set", "selfie_candidate", "selfie_snap")]


def _shown(store, data, guid=101):
    return galleryview.gallery(store.conn, data, guid)["photos"]


def test_gallery_hides_photos_from_before_her_looks_changed_and_keeps_them(store, data):
    renderable_cast(store, 101)  # persona version 2
    ready(store, data, 101, 1, now=1)  # rendered from version 2: a slim woman in a sweater
    old_snap = ready_snap(store, data, 101, looks=looks_hash(APPEARANCE))
    pick(store, 101, 1, 0, source="console", now=2)
    new = _new_looks(store, 101, build="voluptuous", weight_kg=80)  # version 3
    ready(store, data, 101, 2, now=3)
    new_snap = ready_snap(store, data, 101, looks=new)
    # her cover is still from set 1 (version 2): set 2 is newer and shows; the old situation not
    seen = _shown(store, data)
    assert {p["set"] for p in seen if p["kind"] == "candidate"} == {1, 2}
    assert [p["id"] for p in seen if p["kind"] == "snap"] == [f"snap:{new_snap}"]
    assert galleryview.photo(store.conn, data, 101, "snap", old_snap, None) is None
    assert galleryview.photo(store.conn, data, 101, "snap", new_snap, None) is not None

    change = pick(store, 101, 2, 1, source="console", now=4)  # her cover now has the new looks
    before, rows = _tree(data), _rows(store)
    seen = _shown(store, data)
    assert {p["set"] for p in seen if p["kind"] == "candidate"} == {2}
    assert [p["id"] for p in seen if p["cover"]] == ["candidate:2:1"]
    for idx in range(4):
        assert galleryview.photo(store.conn, data, 101, "candidate", 1, idx) is None
        assert galleryview.photo(store.conn, data, 101, "candidate", 1, idx, thumb=True) is None
        assert galleryview.photo(store.conn, data, 101, "candidate", 2, idx) is not None
    assert _tree(data) == before and _rows(store) == rows  # nothing deleted, moved or written
    assert len(rows[0]) == 2 and len(rows[1]) == 8 and len(rows[2]) == 2

    undo(store, change, source="console", now=5)  # undo still works: set 1 is her cover again
    seen = _shown(store, data)
    assert [p["id"] for p in seen if p["cover"]] == ["candidate:1:0"]
    assert {p["set"] for p in seen if p["kind"] == "candidate"} == {1, 2}
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 3) is not None


def test_a_hidden_set_can_still_be_picked_and_then_shows_as_her_cover(store, data):
    renderable_cast(store, 101)
    ready(store, data, 101, 1, now=1)
    _new_looks(store, 101, hair_colour="copper red")
    ready(store, data, 101, 2, now=2)
    pick(store, 101, 2, 0, source="console", now=3)
    assert {p["set"] for p in _shown(store, data)} == {2}
    assert pick(store, 101, 1, 2, source="console", now=4) is not None  # the mind still takes it
    seen = _shown(store, data)
    assert [p["id"] for p in seen if p["cover"]] == ["candidate:1:2"]
    assert {p["set"] for p in seen} == {1, 2}  # set 2 is newer than her cover's set


def test_with_no_cover_every_photo_shows_as_before(store, data):
    renderable_cast(store, 101)
    ready(store, data, 101, 1, now=1)
    old_snap = ready_snap(store, data, 101, looks=looks_hash(APPEARANCE))
    _new_looks(store, 101, build="curvy")
    ready(store, data, 101, 2, now=2)
    seen = _shown(store, data)
    assert {p["set"] for p in seen if p["kind"] == "candidate"} == {1, 2}
    assert [p["id"] for p in seen if p["kind"] == "snap"] == [f"snap:{old_snap}"]
    assert galleryview.photo(store.conn, data, 101, "candidate", 1, 0) is not None
    assert galleryview.photo(store.conn, data, 101, "snap", old_snap, None) is not None


def test_a_cover_set_without_a_recorded_persona_version_hides_nothing(store, data):
    renderable_cast(store, 101)
    ready(store, data, 101, 1, now=1)
    _new_looks(store, 101, build="curvy")
    ready(store, data, 101, 2, now=2)
    pick(store, 101, 2, 0, source="console", now=3)
    with store.transaction():
        store.conn.execute("UPDATE selfie_set SET persona_version=NULL WHERE guid=101 AND set_no=2")
    assert {p["set"] for p in _shown(store, data)} == {1, 2}
    pick(store, 101, 1, 0, source="console", now=4)  # her cover's set has a version: 2 has none
    assert {p["set"] for p in _shown(store, data)} == {1}
    assert galleryview.photo(store.conn, data, 101, "candidate", 2, 0) is None


def test_the_gallery_endpoint_hides_and_refuses_photos_of_her_old_looks(store, data):
    renderable_cast(store, 101)
    ready(store, data, 101, 1, now=1)
    _new_looks(store, 101, build="voluptuous")
    ready(store, data, 101, 2, now=2)
    pick(store, 101, 2, 3, source="console", now=3)

    async def go():
        async with served(make_app(data)) as app:
            response = await http(app, "GET", "/api/selfies/gallery?guid=101")
            assert response.status == 200
            assert {p["set"] for p in response.json()["view"]["photos"]} == {2}
            old = "/api/selfies/photo?guid=101&kind=candidate&set=1&index=0"
            assert (await http(app, "GET", old)).status == 404
            assert (await http(app, "GET", old + "&thumb=1")).status == 404
            assert (await http(app, "GET", old.replace("set=1", "set=2"))).status == 200
    asyncio.run(go())
