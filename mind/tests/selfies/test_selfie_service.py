"""The pick service (P2 Task 11): the one writer of profile photo picks while the mind runs. Each
change commits first, then that member's files are made to agree off the loop; a repeated pick
writes nothing, an undo a later change superseded is refused naming it, "4 more" passes the
waiting sets and needs rendering on and the looks in, and sync_all repairs what a crash left at
the mind's start. Every person here is invented."""
import asyncio
import logging
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

from conftest import TEST_CAST, make_persona, run
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.selfies import files
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.files import (candidate, chosen, history, member_dir, set_dir,
                                       staging_dir)
from leeroy_mind.selfies.service import SelfieService
from leeroy_mind.selfies.store import Current, current, pick, picks, queued_sets, ready_set
from leeroy_mind.wow import CLASSES, RACES
from nightly.test_night_enrich import APPEARANCE

from .fakes import JPEG, ready, tagged_jpeg


def _pick(guid, set_no, idx):
    return {"action": "pick", "guid": guid, "set": set_no, "index": idx}


def _decided(store, guid):
    with store.transaction():
        return {r[0]: r[1] for r in store.conn.execute(
            "SELECT set_no, decided FROM selfie_set WHERE guid=? ORDER BY set_no", (guid,))}


def _stamp(path: Path):
    st = path.stat()
    return st.st_ino, st.st_mtime_ns, st.st_size


def _events(caplog, event):
    return [r.leeroy_fields for r in caplog.records if getattr(r, "leeroy_event", None) == event]


def test_a_repeated_pick_writes_nothing_and_a_superseded_undo_names_the_later_changes(svc, store,
                                                                                      data):
    ready(store, data, 101, 1)
    first = run(svc.handle({"action": "pick", "guid": 101, "set": 1, "index": 0}))
    was = _stamp(chosen(data, 101))
    assert run(svc.handle({"action": "pick", "guid": 101, "set": 1, "index": 0})) == {
        "ok": True, "pick": None, "guid": 101, "message": "That is already their photo."}
    assert _stamp(chosen(data, 101)) == was and len(picks(store.conn)) == 1  # nothing written
    second = run(svc.handle({"action": "pick", "guid": 101, "set": 1, "index": 2}))
    refused = run(svc.handle({"action": "undo", "pick": first["pick"]}))
    assert refused == {"ok": False, "error": f"changed since by #{second['pick']}: undo that first"}
    assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 2).read_bytes()


def test_a_pick_sets_chosen_and_history_and_undo_takes_it_back(svc, store, data):
    ready(store, data, 101, 1)
    got = run(svc.handle(_pick(101, 1, 3)))
    n = got["pick"]
    assert got == {"ok": True, "pick": n, "guid": 101, "message": f"Photo set (change #{n})."}
    photo = candidate(data, 101, 1, 3).read_bytes()
    assert chosen(data, 101).read_bytes() == photo == history(data, 101, n).read_bytes()
    assert current(store, 101) == Current(101, 1, 3, n)

    back = run(svc.handle({"action": "undo", "pick": n}))
    u = back["pick"]
    assert back == {"ok": True, "pick": u, "guid": 101, "message": f"Undone (change #{u})."}
    assert u > n and current(store, 101) is None and not chosen(data, 101).exists()
    assert history(data, 101, n).read_bytes() == photo  # history stays
    assert ready_set(store, 101, 1).decided is None  # his choice from this set is open again

    again = run(svc.handle({"action": "undo", "pick": u}))  # the undo undone: the photo is back
    assert again["ok"] and again["guid"] == 101
    assert chosen(data, 101).read_bytes() == photo == history(data, 101, again["pick"]).read_bytes()
    assert not list(member_dir(data, 101).rglob("*.tmp"))


def test_more_queues_once_and_passes_the_waiting_set(svc, store, data):
    ready(store, data, 101, 1)
    assert run(svc.handle({"action": "more", "guid": 101})) == {
        "ok": True, "set": 2, "guid": 101, "message": "Four more will be rendered tonight."}
    assert ready_set(store, 101, 1).decided == "passed"
    assert [(s.guid, s.set_no, s.reason) for s in queued_sets(store)] == [(101, 2, "more")]
    assert run(svc.handle({"action": "more", "guid": 101})) == {
        "ok": False, "error": "four more are already queued for tonight"}
    assert [(s.guid, s.set_no) for s in queued_sets(store)] == [(101, 2)]
    assert current(store, 101) is None and not chosen(data, 101).exists()
    st = run(svc.handle({"action": "status"}))
    assert (st["chosen"], st["waiting"], st["queued"]) == (0, 0, 1)


def test_more_is_refused_while_rendering_is_off_or_the_member_has_no_looks(svc, store, data):
    off = SelfieService(svc.app, SelfieCfg())
    assert run(off.handle({"action": "more", "guid": 101})) == {
        "ok": False, "error": "rendering is off in mind.toml ([selfies] enabled = false)"}
    assert run(svc.handle({"action": "more", "guid": 104})) == {
        "ok": False,
        "error": "no looks yet: the night renders photos once the looks and photo kind are in"}
    _, race, cls = TEST_CAST[105]
    save_persona(store, replace(load_persona(store, 105), appearance=APPEARANCE), source="seed",
                 ts=1001.0, race=RACES[race], class_name=CLASSES[cls])
    svc.app.personas.invalidate(105)
    assert run(svc.handle({"action": "more", "guid": 105})) == {
        "ok": False,
        "error": "no photo kind yet: the night renders photos once the looks and photo kind are in"}
    assert queued_sets(store) == []
    ready(store, data, 101, 1)  # picks still work with rendering off
    assert run(off.handle(_pick(101, 1, 0)))["ok"] is True
    assert run(off.handle({"action": "status"}))["enabled"] is False


def test_the_owner_and_strangers_have_no_photos(svc, store, data):
    owner = svc.app.human_guid
    save_persona(store, make_persona(owner, "Ownerly"), source="seed", ts=1000.0,
                 race=RACES[1], class_name=CLASSES[1])  # even with a persona he is not cast
    store.upsert_cast([Member(guid=106, name="Fenwick", race=1, class_=1, gender=0, level=70,
                              rank=4, online=False, map=0, zone=0, spec_tab=-1),
                       Member(guid=107, name="Gorrin", race=2, class_=1, gender=0, level=80,
                              rank=4, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    save_persona(store, make_persona(106, "Fenwick"), source="seed", ts=1000.0,
                 race=RACES[1], class_name=CLASSES[1])  # under the cast's level
    ready(store, data, owner, 1)
    for guid in (owner, 999, 106):
        for args in (_pick(guid, 1, 0), {"action": "more", "guid": guid}):
            assert run(svc.handle(args)) == {"ok": False, "error": "no such member in the cast"}
    for args in (_pick(107, 1, 0), {"action": "more", "guid": 107}):
        assert run(svc.handle(args)) == {
            "ok": False, "error": "only cast members with a stored persona have photos"}
    assert queued_sets(store) == [] and picks(store.conn) == []
    assert current(store, owner) is None and not chosen(data, owner).exists()


def test_a_set_already_decided_is_refused_unless_it_holds_the_photo(svc, store, data):
    ready(store, data, 101, 1)
    run(svc.handle({"action": "more", "guid": 101}))  # set 1 passed
    ready(store, data, 101, 2)
    assert run(svc.handle(_pick(101, 1, 0))) == {
        "ok": False, "error": "set 1 is no longer waiting for a choice"}
    assert run(svc.handle(_pick(101, 2, 0)))["ok"]
    run(svc.handle({"action": "more", "guid": 101}))  # set 2 holds the photo: stays picked
    ready(store, data, 101, 3)
    c = run(svc.handle(_pick(101, 3, 1)))["pick"]
    assert run(svc.handle(_pick(101, 2, 1))) == {
        "ok": False, "error": "set 2 is no longer waiting for a choice"}
    d = run(svc.handle(_pick(101, 3, 2)))["pick"]  # another of the photo's own set
    assert current(store, 101) == Current(101, 3, 2, d) and d > c
    assert chosen(data, 101).read_bytes() == candidate(data, 101, 3, 2).read_bytes()
    assert _decided(store, 101) == {1: "passed", 2: "picked", 3: "picked"}


def test_status_counts_and_says_truthfully_which_changes_can_be_undone(svc, store, data):
    ready(store, data, 101, 1)
    ready(store, data, 102, 1)
    ready(store, data, 103, 1)
    run(svc.handle({"action": "more", "guid": 103}))  # 103: set 1 passed, set 2 queued
    a = run(svc.handle(_pick(101, 1, 0)))["pick"]
    b = run(svc.handle(_pick(101, 1, 1)))["pick"]
    u = run(svc.handle({"action": "undo", "pick": b}))["pick"]  # a and u both left photo 0
    st = run(svc.handle({"action": "status"}))
    other = sqlite3.connect(data.parent / "mind.db")  # the store fixture's file
    other.execute("INSERT INTO selfie_set(guid, set_no, reason, status, requested) "
                  "VALUES (105, 1, 'first', 'queued', 9.0)")
    other.commit()
    other.close()
    seen = store.conn.execute("SELECT count(*) FROM selfie_set WHERE guid=105").fetchone()[0]
    store.conn.commit()
    assert seen == 1  # status's read ended: no WAL snapshot left pinned
    assert {k: st[k] for k in ("ok", "enabled", "chosen", "waiting", "queued")} == {
        "ok": True, "enabled": True, "chosen": 1, "waiting": 1, "queued": 1}
    assert [(r["id"], r["guid"], r["label"], r["undoable"]) for r in st["recent"]] == [
        (u, 101, f"undo #{b}", True), (b, 101, "pick", False), (a, 101, "pick", True)]
    assert run(svc.handle({"action": "undo", "pick": b})) == {
        "ok": False, "error": f"changed since by #{u}: undo that first"}  # False was the truth
    v = run(svc.handle({"action": "undo", "pick": a}))["pick"]  # and so was True
    st = run(svc.handle({"action": "status"}))
    assert (st["chosen"], st["waiting"], st["queued"]) == (0, 2, 2)  # 105 queued above
    assert [(r["id"], r["undoable"]) for r in st["recent"]] == [
        (v, True), (u, False), (b, False), (a, False)]


def test_a_held_chosen_jpg_is_retried_off_the_loop_and_never_missing(svc, store, data,
                                                                     monkeypatch):
    ready(store, data, 101, 1)
    run(svc.handle(_pick(101, 1, 0)))
    old = chosen(data, 101).read_bytes()
    real, seen = os.replace, []

    def held(src, dst):  # the console's face endpoint holds chosen.jpg for six tries
        if Path(dst).name == "chosen.jpg" and len(seen) < 6:
            seen.append(Path(dst).read_bytes() == old)
            raise PermissionError(32, "in use")
        real(src, dst)
    monkeypatch.setattr(os, "replace", held)

    async def go():
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)
        t = asyncio.create_task(tick())
        try:
            return await svc.handle(_pick(101, 1, 1)), ticks
        finally:
            t.cancel()

    got, ticks = run(go())
    assert got["ok"] and got["message"] == f"Photo set (change #{got['pick']})."
    assert seen == [True] * 6  # never missing while it waited
    assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 1).read_bytes()
    assert ticks >= 10  # 6 x 50 ms of retries never held the loop


def test_a_photo_file_that_stays_held_keeps_the_change_and_says_so(svc, store, data,
                                                                   monkeypatch, caplog):
    ready(store, data, 101, 1)
    run(svc.handle(_pick(101, 1, 0)))
    old = chosen(data, 101).read_bytes()
    real = os.replace

    def held(src, dst):
        if Path(dst).name == "chosen.jpg":
            raise PermissionError(32, "in use")
        real(src, dst)
    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(files, "REPLACE_TRIES", 2)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        got = run(svc.handle(_pick(101, 1, 2)))
    n = got["pick"]
    assert got == {"ok": True, "pick": n, "guid": 101,
                   "message": f"Photo set (change #{n}), but the photo file could not be "
                              "updated yet; the mind tries again at its next start."}
    assert current(store, 101) == Current(101, 1, 2, n)  # the change stands
    assert chosen(data, 101).read_bytes() == old  # never missing or partial
    assert not list(member_dir(data, 101).glob(".chosen.jpg.*"))
    assert _events(caplog, "selfie_sync_failed") == [{"guid": 101, "error": "PermissionError"}]
    monkeypatch.setattr(os, "replace", real)
    assert run(svc.handle(_pick(101, 1, 2)))["message"] == "That is already their photo."
    assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 2).read_bytes()  # repaired


def test_sync_all_repairs_what_a_crash_left(svc, store, data):
    ready(store, data, 101, 1)
    n = pick(store, 101, 1, 2, source="console", now=2.0)  # committed; the crash came first
    ready(store, data, 102, 1)
    stray = chosen(data, 102)  # an undo committed, then the crash: chosen.jpg with no photo
    stray.write_bytes(tagged_jpeg("stray"))
    ready(store, data, 103, 1)
    part = staging_dir(data, 103, 2)  # a render the crash cut short
    part.mkdir(parents=True)
    (part / "c0.jpg").write_bytes(JPEG)
    leftovers = [member_dir(data, 103) / ".chosen.jpg.4242.tmp",
                 member_dir(data, 101) / "history" / f".{n}.jpg.4242.tmp",
                 set_dir(data, 103, 1) / ".c0.jpg.4242.tmp"]
    for p in leftovers:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"half")
    other = data / "selfies" / "notes"  # not a member's folder: never touched
    other.mkdir()
    (other / "keep.tmp").write_bytes(b"x")

    assert svc.sync_all() == 2
    assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 2).read_bytes()
    assert history(data, 101, n).read_bytes() == candidate(data, 101, 1, 2).read_bytes()
    assert not stray.exists() and not part.exists()
    assert not any(p.exists() for p in leftovers)
    assert (other / "keep.tmp").exists()
    assert all(candidate(data, 103, 1, i).exists() for i in range(4))  # a ready set stays
    assert svc.sync_all() == 0


def test_sync_all_goes_on_past_a_member_it_cannot_repair(svc, store, data, monkeypatch, caplog):
    for guid in (101, 102, 103):
        ready(store, data, guid, 1)
        pick(store, guid, 1, 0, source="console", now=2.0)
    shutil.rmtree(set_dir(data, 101, 1))  # its photo is gone from disk, history copy and all
    real = files.sync_chosen

    def held(data_dir, guid, cur):
        if guid == 102:
            raise PermissionError(32, "in use")
        return real(data_dir, guid, cur)
    monkeypatch.setattr(files, "sync_chosen", held)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert svc.sync_all() == 1
    assert chosen(data, 103).read_bytes() == candidate(data, 103, 1, 0).read_bytes()
    assert not chosen(data, 101).exists() and not chosen(data, 102).exists()
    assert _events(caplog, "selfie_sync_failed") == [
        {"guid": 101, "error": "FileNotFoundError"}, {"guid": 102, "error": "PermissionError"}]


def test_logs_carry_the_action_and_outcome_only(svc, store, data, caplog):
    ready(store, data, 101, 1)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        a = run(svc.handle(_pick(101, 1, 0)))["pick"]
        run(svc.handle(_pick(101, 1, 0)))
        run(svc.handle({"action": "more", "guid": 101}))
        run(svc.handle({"action": "undo", "pick": a}))
        run(svc.handle(_pick(999, 1, 0)))
        run(svc.handle({"action": "undo", "pick": 99}))
        run(svc.handle({"action": "status"}))
    assert _events(caplog, "selfie_change") == [
        {"action": "pick", "outcome": "ok"}, {"action": "pick", "outcome": "unchanged"},
        {"action": "more", "outcome": "ok"}, {"action": "undo", "outcome": "ok"},
        {"action": "pick", "outcome": "refused"}, {"action": "undo", "outcome": "refused"}]
    personas = [svc.app.personas.base(g) for g in TEST_CAST]
    for name in [p.character_name for p in personas] + [p.identity.real_name for p in personas]:
        assert name not in caplog.text
    assert "no such member" not in caplog.text and "no change" not in caplog.text
