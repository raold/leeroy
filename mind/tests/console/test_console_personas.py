"""The console's Personas tab: its read-only questions to mind.db (personaview), its two routes
and their gates, and that persona content stays out of the console's log. Scratch databases and
synthetic personas only."""
import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_mem, make_persona
from console.rig import http, make_app, running
from leeroy_mind.console import feed, personaview
from leeroy_mind.nightly.attraction import set_stance
from leeroy_mind.nightly.growth import undo
from leeroy_mind.nightly.store import commit_unit, enqueue_review, ledger_add
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.protocol import Member
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.wow import CLASSES, RACES

NOW = 1_793_304_000.0  # 2026-10-29 20:00 UTC, the store fixture's clock
DAY = 86400.0
SECRET = "SECRETTEXT"


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _member(guid, name, cls, spec_tab, *, race=2, rank=3, online=True):
    return Member(guid=guid, name=name, race=race, class_=cls, gender=0, level=80, rank=rank,
                  online=online, map=571, zone=4395, spec_tab=spec_tab)


def _save(store, p, source, ts, guid=None):
    m = store.cast().get(guid or p.guid)
    return save_persona(store, p, source=source, ts=ts, race=RACES[m.race] if m else "",
                        class_name=CLASSES[m.class_] if m else "")


def _persona(guid, name, *, scores=None, **over):
    p = make_persona(guid, name, **over)
    five = p.big_five
    for trait, score in (scores or {}).items():
        five = replace(five, **{trait: replace(getattr(five, trait), score=score)})
    return replace(p, big_five=five, secret=f"{SECRET} {name}", sample_lines=(),
                   mock_interview="INTERVIEWTEXT")


@pytest.fixture
def cast(store):
    """A synthetic guild: the owner (1) and members whose personas were last written by each
    writer (seed, the owner, a stance move, an undo, the night's looks pass, the generator),
    one without a persona, one whose stored persona cannot be read and one that left the cast.
    Arkon (301) carries the moods, ties, memories, drift flags and trait samples."""
    store.set_meta("human_guid", "1")
    store.upsert_cast([_member(1, "Owner", 8, 0, rank=0), _member(301, "Arkon", 1, 2),
                       _member(302, "Belra", 5, 1), _member(303, "Coros", 4, 1, rank=1),
                       _member(304, "Dunna", 8, 2, online=False), _member(305, "Ebbo", 7, 0),
                       _member(306, "Fenn", 3, 1), _member(307, "Gutt", 9, 0),
                       _member(309, "Hessa", 11, 2)], NOW - 3600)
    _save(store, _persona(301, "Arkon", scores={"openness": 4}), "seed", NOW - 30 * DAY)
    _save(store, _persona(302, "Belra", player_gender="female", stance="fond", style="shy"),
          "seed", NOW - 30 * DAY)
    set_stance(store, 302, "drawn", now=NOW - 2 * DAY, reason="act flirt", evidence=["chat:1"],
               score_before=30.0)
    coros = _persona(303, "Coros")
    _save(store, coros, "seed", NOW - 30 * DAY)
    _save(store, replace(coros, humour="puns"), "owner", NOW - 5 * DAY)
    _save(store, _persona(304, "Dunna"), "generated", NOW - 20 * DAY)
    ebbo = _persona(305, "Ebbo")
    _save(store, ebbo, "seed", NOW - 30 * DAY)
    looks = Appearance(height_cm=180, build="athletic", weight_kg=80, hair_colour="brown",
                       hair_length="short", hair_style="neat", skin_tone="fair",
                       eye_colour="green", face=("glasses",), at_the_pc="a grey hoodie")

    def looks_pass(st):  # as the night's looks pass commits a persona with its unit
        _save(st, replace(load_persona(st, 305), appearance=looks,
                                     photo=ProfilePhoto(**PHOTO)), "generated", NOW - DAY + 0.4)
    commit_unit(store, "2026-10-28", personaview.LOOKS_STAGE, "305", looks_pass, now=NOW - DAY)
    with store.transaction():
        store.conn.execute("INSERT INTO persona_core(guid, json, card, card_tokens, version, "
                           "source, updated) VALUES (307, '{\"schema\": 99}', '', 0, 4, "
                           "'generated', ?)", (NOW - 3 * DAY,))
    _save(store, _persona(308, "Gone"), "seed", NOW - 40 * DAY)  # no longer in the cast
    _save(store, _persona(309, "Hessa", player_gender="female", stance="fond", style="bold"),
          "seed", NOW - 30 * DAY)
    set_stance(store, 309, "lust", now=NOW - 3 * DAY, reason="act flirt", evidence=["chat:2"],
               score_before=40.0)
    move = store.conn.execute("SELECT id FROM persona_ledger WHERE guid = 309").fetchone()["id"]
    assert undo(store, move, now=NOW - DAY)
    with store.transaction():
        for guid, (p, a, d) in {301: (0.3, -0.2, 0.1), 302: (-0.4, 0.5, -0.3)}.items():
            store.conn.execute("INSERT INTO pad_mood(guid,p,a,d,updated) VALUES (?,?,?,?,?)",
                               (guid, p, a, d, NOW - 14 * 3600))
        store.conn.execute("INSERT INTO guild_mood(night,value,reasons) VALUES (?,?,?)",
                           ("2026-10-27", -0.1, json.dumps(["3 wipes"])))
        store.conn.execute("INSERT INTO guild_mood(night,value,reasons) VALUES (?,?,?)",
                           ("2026-10-28", 0.25, json.dumps(["2 kills", "1 wipe"])))
        for night, values in {"2026-10-27": (5, 5, 5, 5, 5),
                              "2026-10-28": (4.6, 6.2, 8.4, 4.9, 1.7)}.items():
            for trait, value in zip(("openness", "conscientiousness", "extraversion",
                                     "agreeableness", "neuroticism"), values):
                store.conn.execute("INSERT INTO trait_state(night,guid,trait,value) "
                                   "VALUES (?,?,?,?)", (night, 301, trait, value))
    for b, (aff, fam, label) in {302: (60.0, 50.0, "friend"), 303: (-45.0, 30.0, "friction"),
                                 304: (10.0, 80.0, ""), 305: (-10.0, 5.0, "")}.items():
        store.put_relationship(Relationship(a=301, b=b, affinity=aff, familiarity=fam,
                                            trust=20.0, label=label, interactions=4,
                                            last_seen=int(NOW - DAY), notes=f"notes on {b}"))
    store.note_interaction(301, 306, NOW - DAY)  # a row with no score: not a tie
    store.put_relationship(Relationship(a=301, b=1, affinity=25.0, attraction=0.0,
                                        familiarity=40.0, romance_stage="none"))
    store.put_relationship(Relationship(a=302, b=1, affinity=35.0, attraction=55.0,
                                        familiarity=30.0, romance_stage="banter"))
    for kind, age, status in (("observation", 1, "active"), ("observation", 2, "active"),
                              ("observation", 20, "active"), ("reflection", 3, "active"),
                              ("conversation", 1, "superseded")):
        store.insert_memory(make_mem(owner=301, kind=kind, status=status,
                                     t_created=int(NOW - age * DAY), text=f"MEMTEXT {kind}"))
    review = enqueue_review(store, guid=301, kind="drift_line", text="DRIFTLINE open",
                            evidence=[], reason="r", now=NOW - DAY)
    ledger_add(store, guid=301, kind="drift_flag", text="DRIFTLINE open", evidence=[],
               ref=review, now=NOW - DAY)
    review = enqueue_review(store, guid=301, kind="drift_line", text="DRIFTLINE resolved",
                            evidence=[], reason="r", now=NOW - DAY)
    ledger_add(store, guid=301, kind="drift_flag", text="DRIFTLINE resolved", evidence=[],
               ref=review, now=NOW - DAY)
    with store.transaction():
        store.conn.execute("UPDATE review_queue SET resolved = ? WHERE id = ?", (NOW, review))
    flag = ledger_add(store, guid=301, kind="drift_flag", text="DRIFTLINE undone", evidence=[],
                      now=NOW - 2 * DAY)
    ledger_add(store, guid=301, kind="undo", text="undid drift_flag: DRIFTLINE undone",
               evidence=[], ref=flag, now=NOW - DAY)
    ledger_add(store, guid=301, kind="drift_flag", text="DRIFTLINE old", evidence=[],
               now=NOW - 9 * DAY)
    ledger_add(store, guid=301, kind="note_added", text="likes a plan", evidence=[],
               now=NOW - 4 * DAY)
    return store


def _digest(path: Path) -> str:
    return "".join(hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(path.parent.glob(path.name + "*")))


PHOTO = {"kind": "snapshot", "framing": "upper_body", "setting": "on the deck of a small boat",
         "outfit": "orange rain bib over a grey hoodie", "expression": "squinting into the wind"}


def _roster(data):
    view = feed.read(data / "mind.db", personaview.roster_view, now=NOW)
    return view, {m["guid"]: m for m in view["members"]}


# -- the questions -------------------------------------------------------------------------------

def test_the_roster_lists_the_cast_and_who_last_wrote_each_persona(cast, data):
    view, by = _roster(data)
    assert view["human"] == 1 and view["missing"] == [] and view["recent_days"] == 7
    assert [m["name"] for m in view["members"]] == [
        "Arkon", "Belra", "Coros", "Dunna", "Ebbo", "Fenn", "Gone", "Gutt", "Hessa"]
    assert 1 not in by  # the owner is not cast
    assert {g: (m["persona"] or {}).get("by") for g, m in by.items()} == {
        301: "seed", 302: "stance", 303: "owner", 304: "generator", 305: "night", 306: None,
        307: "generator", 308: "seed", 309: "undo"}
    assert by[302]["persona"] == {"version": 2, "source": "generated", "updated": NOW - 2 * DAY,
                                  "by": "stance", "readable": True}
    assert by[309]["persona"]["version"] == 3 and by[309]["stance"] == "fond"  # undone
    assert by[305]["persona"]["updated"] == NOW - DAY + 0.4
    assert by[306]["persona"] is None and by[306]["stance"] is None  # no persona yet
    assert by[307]["persona"]["readable"] is False and by[307]["stance"] is None
    assert by[308]["in_cast"] is False and by[308]["class"] is None and by[308]["name"] == "Gone"
    arkon = by[301]
    assert (arkon["class"], arkon["spec"], arkon["role"], arkon["rank"]) == (1, "protection",
                                                                           "tank", 3)
    assert arkon["stance"] == "none" and by[302]["stance"] == "drawn"


def test_moods_ties_memories_and_drift_are_what_the_rows_hold(cast, data):
    view, by = _roster(data)
    arkon = by[301]
    assert arkon["mood"] == {"p": 0.3, "a": -0.2, "d": 0.1, "octant": "relaxed",
                             "updated": NOW - 14 * 3600}
    assert by[302]["mood"]["octant"] == "anxious"
    assert by[303]["mood"] is None  # the night's mood stage has not reached them
    assert view["guild_mood"] == {"night": "2026-10-28", "value": 0.25,
                                  "reasons": ["2 kills", "1 wipe"]}
    ties = arkon["ties"]
    assert ties["total"] == 4  # the scoreless row (an interaction only) is not a tie
    assert [(t["guid"], t["affinity"], t["label"]) for t in ties["top"]] == [
        (302, 60.0, "friend"), (303, -45.0, "friction"), (304, 10.0, "")]
    assert "notes" not in ties["top"][0]  # the roster carries no notes
    assert arkon["you"]["affinity"] == 25.0 and by[302]["you"]["romance_stage"] == "banter"
    assert by[303]["you"] is None and by[303]["ties"] == {"top": [], "total": 0}
    assert arkon["memories"] == {"total": 4, "recent": 3}  # the superseded one is not held
    assert by[302]["memories"] == {"total": 0, "recent": 0}
    drift = arkon["drift"]
    assert drift["flags"] == 1  # the open one: not resolved, undone or older than 7 days
    assert drift["traits"]["night"] == "2026-10-28"
    assert drift["traits"]["values"]["extraversion"] == {"base": 7, "sampled": 8.4, "shift": 1.4}
    assert drift["traits"]["values"]["openness"] == {"base": 4, "sampled": 4.6, "shift": 0.6}
    assert by[302]["drift"] == {"flags": 0, "traits": None}  # not sampled that night


def test_a_member_in_detail_shows_the_card_summary_and_nothing_private(cast, data):
    view = feed.read(data / "mind.db", personaview.member_view, 301, now=NOW)
    assert view["member"]["name"] == "Arkon" and view["names"]["302"] == "Belra"
    card = view["card"]
    assert card["big_five"]["extraversion"]["score"] == 7
    assert card["typing"]["case"] == "lower" and card["game"]["motivation"] == "achiever"
    assert card["play"]["raid_role"] == "tank" and card["appearance"] is None
    assert card["photo"] is None
    assert card["identity"] == {"age": 34, "player_gender": "male", "region": "UK"}
    assert card["romance"] == {"stance": "none", "style": None, "jealousy_mode": None}
    ties = view["member"]["ties"]["top"]
    assert len(ties) == 4 and ties[0]["notes"] == "notes on 302"
    assert view["memories"] == {"observation": {"total": 3, "recent": 2},
                                "reflection": {"total": 1, "recent": 1}}
    kinds = [(r["kind"], r["text"], r["undone"]) for r in view["ledger"]]
    assert kinds[:2] == [("undo", "undid drift_flag: DRIFTLINE undone", False),
                         ("drift_flag", "DRIFTLINE resolved", False)]
    assert ("drift_flag", "DRIFTLINE undone", True) in kinds and len(kinds) == 6
    text = json.dumps(view)
    for private in (SECRET, "INTERVIEWTEXT", "MEMTEXT", "BITINSTR", "mention the weather",
                    "real_name", "Sam Carter", "warehouse", "tea beats coffee"):
        assert private not in text
    assert feed.read(data / "mind.db", personaview.member_view, 305,
                     now=NOW)["card"]["appearance"]["build"] == "athletic"
    assert feed.read(data / "mind.db", personaview.member_view, 305,
                     now=NOW)["card"]["photo"] == PHOTO
    unreadable = feed.read(data / "mind.db", personaview.member_view, 307, now=NOW)
    assert unreadable["card"] is None and unreadable["member"]["persona"]["readable"] is False
    assert feed.read(data / "mind.db", personaview.member_view, 1, now=NOW) is None  # the owner
    assert feed.read(data / "mind.db", personaview.member_view, 999, now=NOW) is None


def test_the_roster_never_carries_private_persona_fields(cast, data):
    text = json.dumps(feed.read(data / "mind.db", personaview.roster_view, now=NOW))
    for private in (SECRET, "INTERVIEWTEXT", "MEMTEXT", "notes on", "DRIFTLINE", "likes a plan",
                    "Sam Carter", "warehouse"):
        assert private not in text


def test_the_questions_never_write(cast, data):
    cast.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _digest(data / "mind.db")
    feed.read(data / "mind.db", personaview.roster_view, now=NOW)
    for guid in (301, 305, 307, 999):
        feed.read(data / "mind.db", personaview.member_view, guid, now=NOW)
    assert _digest(data / "mind.db") == before


def test_an_empty_database_says_none_yet_and_an_older_one_says_what_it_lacks(store, data,
                                                                              tmp_path):
    store.upsert_cast([_member(401, "Ivo", 1, 0)], NOW)
    view, by = _roster(data)
    assert view["missing"] == [] and view["guild_mood"] is None
    assert by[401]["persona"] is None and by[401]["mood"] is None and by[401]["you"] is None
    assert by[401]["memories"] == {"total": 0, "recent": 0}
    assert by[401]["drift"] == {"flags": 0, "traits": None}
    older = tmp_path / "older"
    older.mkdir()
    with contextlib.closing(sqlite3.connect(older / "mind.db")) as conn:
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE cast_member(guid INTEGER PRIMARY KEY, name TEXT, race INTEGER, "
            "class INTEGER, gender INTEGER, level INTEGER, rank INTEGER, online INTEGER, "
            "map INTEGER, zone INTEGER, spec_tab INTEGER, updated REAL);"
            "INSERT INTO cast_member VALUES (401, 'Ivo', 2, 1, 0, 80, 3, 1, 0, 0, 0, 0);")
    view = feed.read(older / "mind.db", personaview.roster_view, now=NOW)
    assert view["missing"] == ["persona_core", "pad_mood", "guild_mood", "relationship",
                               "memory", "persona_ledger", "trait_state"]
    [ivo] = view["members"]
    assert (ivo["mood"], ivo["ties"], ivo["memories"], ivo["drift"], ivo["you"]) == (
        None, None, None, None, None)
    detail = feed.read(older / "mind.db", personaview.member_view, 401, now=NOW)
    assert (detail["card"], detail["memories"], detail["ledger"]) == (None, None, None)
    bare = tmp_path / "bare"
    bare.mkdir()
    sqlite3.connect(bare / "mind.db").close()
    assert feed.read(bare / "mind.db", personaview.roster_view, now=NOW)["members"] == []


def test_the_roster_reads_the_memory_counts_from_the_index(cast, data):
    with contextlib.closing(feed.connect(data / "mind.db")) as conn:
        plan = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT owner, count(*) AS total, sum(t_created >= ?) AS recent "
            "FROM memory WHERE status = 'active' GROUP BY owner", (NOW,)))
    assert "COVERING INDEX memory_owner_status" in plan


# -- the routes ----------------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_the_personas_routes_need_the_key_the_host_and_no_foreign_origin(cast, data):
    async def go():
        async with served(make_app(data, mind=running(data))) as app:
            for path in ("/api/personas", "/api/persona?guid=301"):
                assert (await http(app, "GET", path, key=False)).status == 403
                assert (await http(app, "GET", path, origin="http://127.0.0.1:1")).status == 403
                assert (await http(app, "GET", path, origin="null")).status == 403
                for host in (f"localhost:{app.s.port}", "evil.example", "127.0.0.1:1"):
                    assert (await http(app, "GET", path, host=host)).status == 403
                assert (await http(app, "GET", path,
                                   headers={"Sec-Fetch-Site": "cross-site"})).status == 403
                assert (await http(app, "POST", path, body={}, key=False,
                                   origin=True)).status == 403
                for method in ("POST", "PUT", "DELETE"):
                    r = await http(app, method, path, body={}, origin=True)
                    assert r.status == 405  # read-only: there is no write path
            r = await http(app, "GET", "/api/personas", origin=True)
            assert r.status == 200 and len(r.json()["members"]) == 9
            r = await http(app, "GET", "/api/persona?guid=302")
            assert r.status == 200 and r.json()["member"]["persona"]["by"] == "stance"
    asyncio.run(go())


@pytest.mark.parametrize("query, status", [
    ("", 400), ("?guid=", 400), ("?guid=abc", 400), ("?guid=-3", 400), ("?guid=1&guid=2", 400),
    ("?guid=%C2%B2", 400), ("?guid=12345678901", 400), ("?guid=999", 404), ("?guid=1", 404)])
def test_a_member_is_asked_for_by_one_plain_guid(cast, data, query, status):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/api/persona" + query)
            assert r.status == status and r.json()["ok"] is False
    asyncio.run(go())


def test_the_tab_works_while_the_mind_is_down_and_says_when_mind_db_is_missing(cast, data,
                                                                               tmp_path):
    async def go():
        async with served(make_app(data)) as app:  # the mind is stopped
            assert (await http(app, "GET", "/api/personas")).status == 200
        empty = tmp_path / "empty"
        empty.mkdir()
        async with served(make_app(empty)) as app:
            for path in ("/api/personas", "/api/persona?guid=301"):
                r = await http(app, "GET", path)
                assert r.status == 503 and r.json()["error"].startswith("mind.db could not be read")
        assert not (empty / "mind.db").exists()  # a read-only open makes no database
    asyncio.run(go())


def test_persona_content_never_reaches_the_log(cast, data, tmp_path, caplog):
    async def go():
        async with served(make_app(data)) as app:
            await http(app, "GET", "/api/personas")
            await http(app, "GET", "/api/persona?guid=301")
        empty = tmp_path / "empty"
        empty.mkdir()
        async with served(make_app(empty)) as app:
            await http(app, "GET", "/api/personas")
            await http(app, "GET", "/api/persona?guid=301")
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "console_personas_unread: error=OperationalError" in logged
    for private in ("Arkon", "Belra", SECRET, "DRIFTLINE", "notes on", "relaxed", "MEMTEXT"):
        assert private not in logged


def test_the_page_has_the_personas_tab_and_no_way_to_change_a_persona():
    from importlib import resources
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    assert 'id="tab-personas" class="tab mindtab"' in page and 'api("/api/personas")' in page
    assert 'post("/api/persona' not in page and "placeholder" not in page


def test_a_photo_kind_write_is_shown_as_the_photo_pass(cast, data):
    def photo_pass(st):  # as the night's photo-kind pass commits a persona with its unit
        _save(st, replace(load_persona(st, 301), photo=ProfilePhoto(**PHOTO)), "generated",
              NOW - 100 + 0.4)
    commit_unit(cast, "2026-10-29", "photo", "301", photo_pass, now=NOW - 100)
    _, by = _roster(data)
    assert by[301]["persona"]["by"] == "photo" and by[305]["persona"]["by"] == "night"
