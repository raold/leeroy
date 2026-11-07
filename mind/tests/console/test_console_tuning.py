"""T9: the console's tuning. GET /api/tuning reads mind.db read-only (the dials, groups,
members' values and the history); POST /api/tune is checked as the endpoint checks it and goes
to the running mind's tune op behind the same gates as raid actions. A preview's text is never
logged or broadcast."""
import asyncio
import hashlib
import logging
from pathlib import Path

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.tuning import changes as C
from console.rig import events, http, make_app, page_source, running


class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def locate(self, pids):
        if self.error:
            raise LinkError(self.error)
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args, timeout))
        if self.error:
            raise LinkError(self.error)
        return self.reply(op, args) if callable(self.reply) else self.reply


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store, data):
    seed_test_cast(store)
    store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
    store.set_meta("human_guid", str(HUMAN))
    save_persona(store, make_persona(102, "Belra", player_gender="female", stance="lust"),
                 source="seed", ts=1.0, race="Night Elf", class_name="Druid")
    (data / "mind.toml").write_text("[talk]\nchattiness = 4\n", encoding="utf-8")
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    C.save_group(store, None, "women", {"gender": ["female"]}, source="console", now=1.0)
    C.apply_sets(store, [C.Set("group", "1", "forward", 2), C.Set("member", "103", "humour", 8),
                         C.Set("world", "", "chattiness", 7)], pic, source="console", now=2.0)
    return store


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def served(app):
    await app.start()
    return app


def test_the_tuning_view_shows_dials_groups_values_and_history_without_writing(cast, data):
    async def go():
        app = await served(make_app(data))
        try:
            r = await http(app, "GET", "/api/tuning")
            assert r.status == 200
            v = r.json()["view"]
            assert v["available"] and v["version"] == 2 and v["pinned_members"] == 1
            assert [d["name"] for d in v["dials"]][:2] == ["talkative", "argumentative"]
            assert not {"crude", "ego", "patience", "loot_greed", "raid_comms"} & {
                d["name"] for d in v["dials"] + v["world_dials"]}
            world = {d["name"]: d for d in v["world_dials"]}
            assert (world["chattiness"]["file"], world["chattiness"]["override"],
                    world["chattiness"]["value"]) == (4, 7, 7)
            groups = {g["target"]: g for g in v["groups"]}
            assert groups["everyone"]["builtin"] and groups["everyone"]["members"] == 5
            assert groups["1"]["name"] == "women" and groups["1"]["members"] == 1
            assert groups["1"]["offsets"] == {"forward": 2}
            members = {m["guid"]: m for m in v["members"]}
            assert HUMAN not in members
            belra = members[102]["dials"]
            assert belra["forward"] == {"base": 5, "value": 7, "own": None, "applies": True,
                                        "note": "", "stale": False}
            assert members[103]["dials"]["humour"]["own"] == 8
            assert members[101]["dials"]["forward"]["applies"] is False
            assert "Men stay friends" in members[101]["dials"]["forward"]["note"]
            assert [h["id"] for h in v["history"]] == [2, 1]
            assert "secret" not in r.body.decode()
        finally:
            await app.close()
    before = _digest(data / "mind.db")
    asyncio.run(go())
    assert _digest(data / "mind.db") == before


def test_an_older_mind_db_says_tuning_is_unavailable(store, data, monkeypatch):
    from leeroy_mind.storage import db
    files = db._migration_files()
    old = data / "old"
    old.mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 16])
        db.open_db(old / "mind.db").close()

    async def go():
        app = await served(make_app(old))
        try:
            r = await http(app, "GET", "/api/tuning")
            assert r.status == 200 and r.json()["view"]["available"] is False
            assert "tuning_value" in r.json()["view"]["missing"]
        finally:
            await app.close()
    asyncio.run(go())


SET = {"action": "set", "scope": "member", "target": "102", "dial": "talkative", "value": 8,
       "previewed": True}


def test_a_change_needs_the_key_the_origin_and_json_and_goes_to_the_mind(cast, data):
    link = FakeLink({"ok": True, "change": 3, "version": 3, "reach": 1, "message": "Saved."})

    async def go():
        app = await served(make_app(data, mind=running(data), link=link))
        try:
            assert (await http(app, "POST", "/api/tune", body=SET)).status == 403
            assert (await http(app, "POST", "/api/tune", body=SET, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/tune", body=b"x", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert link.calls == []
            listen = asyncio.create_task(events(app, count=2))
            await asyncio.sleep(0.2)
            r = await http(app, "POST", "/api/tune", body=SET, origin=True)
            assert r.status == 200 and r.json()["change"] == 3
            kinds = await listen
            assert kinds[1] == ("tuning", {"change": 3, "version": 3})
            assert link.calls[-1][1:3] == ("tune", SET)
        finally:
            await app.close()
    asyncio.run(go())


@pytest.mark.parametrize("body", [
    {"action": "write"}, {**SET, "value": 2.5}, {**SET, "extra": 1}, ["set"],
    {"action": "preview", "scope": "member", "target": "102", "dial": "talkative"}])
def test_a_tune_request_not_in_the_endpoints_exact_form_never_leaves_the_console(cast, data,
                                                                                body):
    link = FakeLink({"ok": True})

    async def go():
        app = await served(make_app(data, mind=running(data), link=link))
        try:
            assert (await http(app, "POST", "/api/tune", body=body, origin=True)).status == 400
        finally:
            await app.close()
    asyncio.run(go())
    assert link.calls == []


def test_tuning_is_refused_with_control_off_and_said_plainly_with_the_mind_stopped(cast, data):
    async def go():
        off = Controls(enabled=False, reason="process control is off (--no-control)",
                       mind=running(data))
        app = await served(make_app(data, mind=running(data), controls=off, link=FakeLink({})))
        try:
            r = await http(app, "POST", "/api/tune", body=SET, origin=True)
            assert r.status == 403 and "--no-control" in r.json()["error"]
        finally:
            await app.close()
        app = await served(make_app(data, link=FakeLink({})))
        try:
            r = await http(app, "POST", "/api/tune", body=SET, origin=True)
            assert r.status == 409 and r.json()["error"] == (
                "The mind isn't running, so it can't preview or change dials. Start it from "
                "the Mind tab.")
            v = (await http(app, "GET", "/api/tuning")).json()
            assert v["view"]["available"] and v["mind"]["ok"] is False  # read-only, and says so
        finally:
            await app.close()
    asyncio.run(go())


def test_a_preview_goes_only_to_the_page_that_asked(cast, data, caplog):
    said = "SYNTHETIC-PREVIEW-LINE"
    link = FakeLink({"ok": True, "kind": "lines", "subjects": [
        {"guid": 102, "name": "Belra", "before": {"text": said + " a", "note": ""},
         "after": {"text": said + " b", "note": ""}}]})
    preview = {"action": "preview", "scope": "member", "target": "102", "dial": "humour",
               "value": 9}

    async def go():
        app = await served(make_app(data, mind=running(data), link=link))
        try:
            listen = asyncio.create_task(events(app, count=3, timeout=1.5))
            await asyncio.sleep(0.2)
            r = await http(app, "POST", "/api/tune", body=preview, origin=True)
            assert r.status == 200 and said in r.body.decode()
            assert link.calls[-1][3] >= 40  # a preview's own, longer timeout
            try:
                seen = await listen
            except TimeoutError:
                seen = []
            assert all(kind != "tuning" and said not in str(data) for kind, data in seen)
        finally:
            await app.close()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert said not in caplog.text


def test_the_page_has_the_tuning_and_its_only_write_is_the_tune_route():
    page = page_source()
    assert 'id="p-tuning"' in page and 'api("/api/tuning")' in page
    assert 'post("/api/tune"' in page and 'post("/api/persona' not in page
    assert 'post("/api/selfies' not in page  # the photo pick's one write is post("/api/selfie"
    for later in ("crude", "loot greed", "raid comms", "patience"):
        assert later not in page.casefold()
