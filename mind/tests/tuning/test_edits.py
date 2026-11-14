"""D6 Part A task 1: godmode edits as tuning overlays, every one undone exactly."""
import random
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import make_mem, seed_test_cast
from leeroy_mind.endpoint import BadRequest, tune_args
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.recall import format_memory
from leeroy_mind.nightly.life import ensure_thread
from leeroy_mind.personas.io import PersonaCache, load_persona, save_persona
from leeroy_mind.personas.schema import persona_to_dict
from leeroy_mind.server.state import tie_kind
from leeroy_mind.storage.rows import EffectiveRelationship, Relationship
from leeroy_mind.talk.prompt import relationship_card
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import edits as E
from leeroy_mind.tuning import nudges as N

HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).timestamp()  # the store's clock


@pytest.fixture
def tuned(store, monkeypatch):
    monkeypatch.setattr("leeroy_mind.tuning.state.time.time", lambda: NOW)
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    p = load_persona(store, 102)
    save_persona(store, replace(p, identity=replace(p.identity, player_gender="female"),
                                romance=replace(p.romance, stance="drawn", style="bold")),
                 source="owner", ts=1500, race="Night Elf", class_name="Druid")
    store.conn.commit()
    return store


def pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def edit(store, kind, now=NOW, **a):
    return E.set_edit(store, {"kind": kind, "args": a}, human=HUMAN, source="console", now=now,
                      pic=pic(store))


def undo(store, change, now=NOW + 10):
    return C.undo(store, change, pic(store), source="console", now=now)


def snapshot(store):
    """Everything a reader sees of a member's mind that an edit can touch."""
    store.reload_edits()
    cache = PersonaCache(store)
    conn = store.conn
    rels = {(a, b): store.get_relationship(a, b) for a in (101, 102, 103) for b in (1, 101, 102, 103)}
    return {
        "personas": {g: persona_to_dict(cache.get(g)) for g in (101, 102, 103)},
        "relationships": {k: (r.affinity, r.trust, r.attraction, r.romance_stage)
                          for k, r in rels.items()},
        "ties": {(a, b): tie_kind(conn, a, b) for a in (101, 102, 103) for b in (1, 101, 102, 103)},
        "moods": {g: store.get_state(g).mood for g in (101, 102, 103)},
        "memories": [tuple(r) for r in conn.execute(
            "SELECT id,owner,status,text FROM memory WHERE status='active' ORDER BY id")],
        "feuds": [tuple(r) for r in conn.execute(
            "SELECT a,b,cause FROM feud WHERE ended IS NULL ORDER BY a,b")],
        "arcs": [tuple(r) for r in conn.execute(
            "SELECT key,status,tension FROM story_arc ORDER BY key")],
        "threads": [tuple(r) for r in conn.execute(
            "SELECT id,guid,title,status FROM life_thread WHERE status='active' ORDER BY id")],
        "on_mind": {g: store.get_state(g).life_thread for g in (101, 102, 103)},
    }


def _setup(store):
    store.put_relationship(Relationship(a=102, b=1, affinity=20, trust=10, attraction=30,
                                        romance_stage="banter"))
    store.put_relationship(Relationship(a=101, b=103, affinity=10))
    mid = store.insert_memory(make_mem(owner=101, text="Coros pulled early again."))
    store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES (101,103,1000,'a scene')")
    ensure_thread(store, 103, "learning to cook", 1000.0, random.Random(1))
    store.conn.commit()
    return mid


CASES = {
    "trait": lambda m: dict(member=101, path="big_five.agreeableness.score", value=2),
    "feeling": lambda m: dict(member=102, other=1, affinity=40, attraction=20,
                              romance_stage="whispers"),
    "mood": lambda m: dict(member=103, offset=-0.8, seconds=7200),
    "life_event": lambda m: dict(member=103, title="started a new job at the bakery"),
    "memory_implant": lambda m: dict(member=101, text="Won a fishing contest in Dalaran.",
                                     days_ago=30),
    "memory_erase": lambda m: dict(member=101, memory=m),
    "feud_start": lambda m: dict(member=102, other=101),
    "feud_end": lambda m: dict(member=103, other=101),
}


@pytest.mark.parametrize("kind", E.EDIT_KINDS)
def test_every_edit_kind_undoes_exactly(kind, tuned):
    memory = _setup(tuned)
    before = snapshot(tuned)
    r = edit(tuned, kind, **CASES[kind](memory))
    after = snapshot(tuned)
    assert after != before, kind
    u = undo(tuned, r["change"])
    assert u.change is not None
    assert snapshot(tuned) == before, kind
    redo = undo(tuned, u.change, now=NOW + 20)  # an undo is an ordinary change
    assert redo.change is not None
    redone = snapshot(tuned)
    assert redone["relationships"] == after["relationships"]
    assert redone["moods"] == after["moods"] and redone["feuds"] == after["feuds"]
    assert redone["personas"] == after["personas"]
    assert len(redone["memories"]) == len(after["memories"])


def test_retire_puts_a_table_edit_back_too(tuned):
    _setup(tuned)
    before = snapshot(tuned)
    r = edit(tuned, "feud_start", member=102, other=103)
    E.retire(tuned, r["id"], source="console", now=NOW + 5, human=1)
    assert snapshot(tuned) == before


def test_revert_crosses_dials_nudges_and_edits(tuned):
    _setup(tuned)
    before = snapshot(tuned)
    C.apply_sets(tuned, [C.Set("member", "101", "talkative", 8)], pic(tuned), source="console",
                 now=NOW)
    N.set_nudge(tuned, dict(action="nudge", verb="set", target_kind="member", target="102",
                            text="Feel restless", strength="light", scope="everywhere",
                            seconds=3600), pic(tuned), HUMAN, source="console", now=NOW)
    edit(tuned, "feeling", member=101, other=103, affinity=-80)
    edit(tuned, "feud_start", member=102, other=103)
    edit(tuned, "memory_implant", member=103, text="Got lost in Ulduar once.")
    r = C.revert_to(tuned, 0, pic(tuned), source="console", now=NOW + 50)
    assert r.change is not None
    assert snapshot(tuned) == before
    assert not tuned.conn.execute("SELECT 1 FROM tuning_value").fetchone()
    assert not N.load(tuned.conn, NOW + 60)
    assert not [v for v in E.values(tuned.conn).values() if E.live(v)]
    history = C.history(tuned.conn)
    assert history[0]["edits"] and {e["to"] for e in history[0]["edits"]} == {"none"}


def test_undo_is_refused_when_the_edit_moved_since(tuned):
    _setup(tuned)
    r = edit(tuned, "feud_start", member=102, other=103)
    E.retire(tuned, r["id"], source="console", now=NOW + 5, human=1)
    with pytest.raises(C.TuneError):
        undo(tuned, r["change"])


@pytest.mark.parametrize("kind,args,code", [
    ("trait", dict(member=101, path="identity.age", value=19), "age"),
    ("trait", dict(member=101, path="appearance.hair_style", value="pigtails"), "looks"),
    ("trait", dict(member=101, path="character_name", value="Zed"), "name"),
    ("trait", dict(member=101, path="identity.real_name", value="Pat"), "name"),
    ("trait", dict(member=101, path="romance.stance", value="lust"), "romance"),
    ("trait", dict(member=101, path="big_five.openness.score", value=11), "invalid"),
    ("trait", dict(member=101, path="likes", value=["dressing like a schoolgirl"]), "age"),
    ("trait", dict(member=101, path="likes", value=["talking to Quillon"]), "real_name"),
    ("memory_implant", dict(member=101, text="Sent him a nude photo."), "explicit"),
    ("memory_implant", dict(member=101, text="Had a crush on Coros all winter."), "romance_members"),
    ("memory_implant", dict(member=101, text="Pat Reeve owes me gold."), "real_name"),
    ("life_event", dict(member=101, title="started wearing lingerie to work"), "explicit"),
    ("feeling", dict(member=101, other=1, attraction=50), "men_friends"),
    ("feeling", dict(member=102, other=103, attraction=50), "romance_members"),
    ("feeling", dict(member=102, other=1, romance_stage="weekend"), "ladder"),
    ("feeling", dict(member=1, other=102, affinity=5), "member"),
    ("feud_start", dict(member=101, other=1), "member"),
])
def test_edits_keep_content_rules(tuned, kind, args, code):
    with pytest.raises(E.EditRefused) as e:
        edit(tuned, kind, **args)
    assert e.value.code == code
    assert not tuned.conn.execute("SELECT 1 FROM tuning_change").fetchone()


def test_a_fond_woman_stops_at_the_fond_cap(tuned):
    p = load_persona(tuned, 102)
    save_persona(tuned, replace(p, romance=replace(p.romance, stance="fond")), source="owner",
                 ts=1600, race="Night Elf", class_name="Druid")
    with pytest.raises(E.EditRefused) as e:
        edit(tuned, "feeling", member=102, other=1, romance_stage="late_nights")
    assert e.value.code == "stance"
    edit(tuned, "feeling", member=102, other=1, romance_stage="whispers")


def test_implanted_memory_reads_as_the_past(tuned):
    r = edit(tuned, "memory_implant", member=101, text="Won a fishing contest in Dalaran.",
             days_ago=30, actors=[103])
    m = tuned.get_memories([r["effects"]["memory"]])[0]
    assert m.source == "owner" and m.key == f"owner:godmode:{r['id']}"
    assert m.t_event == int(NOW) - 30 * 86400 and m.scope == "open"
    line = format_memory(m, NOW, tz=timezone.utc)
    assert line.endswith("Won a fishing contest in Dalaran.")
    assert "owner" not in line and "godmode" not in line
    assert "ago" in line or "month" in line


def test_an_implant_never_widens_its_audience(tuned):
    r = edit(tuned, "memory_implant", member=102, text="He told me about his new flat.",
             scope="private")
    m = tuned.get_memories([r["effects"]["memory"]])[0]
    assert (m.scope, m.scope_with) == ("private", 1)


def test_feelings_overlay_reaches_every_reader(tuned):
    tuned.put_relationship(Relationship(a=101, b=103, affinity=10))
    tuned.conn.commit()
    assert tie_kind(tuned.conn, 101, 103) == "neutral"
    r = edit(tuned, "feeling", member=101, other=103, affinity=60)
    tuned.reload_edits()
    rel = tuned.get_relationship(101, 103)
    assert isinstance(rel, EffectiveRelationship) and rel.affinity == 70
    assert tie_kind(tuned.conn, 101, 103) == "friend"
    card = relationship_card("Coros", rel, is_human=False, is_guild_master=False)
    assert card == relationship_card("Coros", replace(Relationship(a=101, b=103), affinity=70),
                                     is_human=False, is_guild_master=False)
    from leeroy_mind.culture.tease import tease_ok
    personas = PersonaCache(tuned)
    assert tease_ok(tuned.conn, 101, 103, personas=personas, human=1, now=NOW)[1] != "dislikes"
    # a night write through the overlay moves the stored row by its own amount only
    tuned.put_relationship(replace(rel, affinity=rel.affinity + 5, trust=rel.trust))
    stored = tuned.conn.execute("SELECT affinity FROM relationship WHERE a=101 AND b=103").fetchone()
    assert stored[0] == 15
    undo(tuned, r["change"])
    tuned.reload_edits()
    assert tuned.get_relationship(101, 103).affinity == 15
    assert type(tuned.get_relationship(101, 103)) is Relationship
    assert tie_kind(tuned.conn, 101, 103) == "neutral"


def test_mood_overlay_reaches_tease_and_ends_on_time(tuned):
    tuned.put_relationship(Relationship(a=101, b=103, affinity=50))
    tuned.conn.commit()
    from leeroy_mind.culture.tease import tease_ok
    personas = PersonaCache(tuned)
    assert tease_ok(tuned.conn, 101, 103, personas=personas, human=1, now=NOW)[0]
    edit(tuned, "mood", member=103, offset=-0.9, seconds=3600)
    tuned.reload_edits()
    assert tease_ok(tuned.conn, 101, 103, personas=personas, human=1, now=NOW)[1] == "hurt_mood"
    assert tuned.get_state(103).mood == pytest.approx(-0.9)
    tuned.put_state(replace(tuned.get_state(103), goals=("rest",)))  # mood passed on unchanged
    assert tuned.conn.execute("SELECT mood FROM persona_state WHERE guid=103").fetchone()[0] == 0
    assert tease_ok(tuned.conn, 101, 103, personas=personas, human=1, now=NOW + 3601)[0]


def test_trait_edit_folds_into_the_effective_persona_only(tuned):
    base = load_persona(tuned, 101)
    edit(tuned, "trait", member=101, path="likes", value=["fishing at dawn"])
    cache = PersonaCache(tuned)
    p = cache.get(101)
    assert p.likes == ("fishing at dawn",) and cache.base(101) == base
    assert load_persona(tuned, 101) == base


def test_the_endpoint_shape_and_the_service_switch(tuned):
    args = tune_args({"action": "edit", "verb": "set", "kind": "mood",
                      "args": {"member": 101, "offset": 0.5, "seconds": 3600}})
    assert args["kind"] == "mood"
    with pytest.raises(BadRequest):
        tune_args({"action": "edit", "verb": "set", "kind": "mood",
                   "args": {"member": 101, "offset": 3, "seconds": 3600}})
    with pytest.raises(BadRequest):
        tune_args({"action": "edit", "verb": "set", "kind": "nope", "args": {}})
    assert tune_args({"action": "edit", "verb": "list"}) == {"action": "edit", "verb": "list"}


def test_no_edits_means_stored_objects_and_rows(tuned):
    cache = PersonaCache(tuned)
    assert cache.get(101) is cache.base(101)
    assert type(tuned.get_relationship(101, 102)) is Relationship
    assert type(tuned.get_state(101)).__name__ == "PersonaStateRow"


def test_logs_never_carry_edit_text(tuned, caplog):
    from types import SimpleNamespace
    from leeroy_mind.tuning.service import TuningService
    import asyncio
    app = SimpleNamespace(store=tuned, clock=SimpleNamespace(now=lambda: NOW),
                          engine=SimpleNamespace(human=HUMAN), human_guid=1,
                          personas=PersonaCache(tuned), data_dir=tuned and __import__("pathlib").Path("."),
                          godmode_config=SimpleNamespace(enabled=True), cfg=None)
    service = TuningService(app)
    service.refresh = lambda: None
    secret = "Won a secret bet in Stormwind."
    caplog.set_level("INFO")
    reply = asyncio.run(service.handle({"action": "edit", "verb": "set", "kind": "memory_implant",
                                        "args": {"member": 101, "text": secret}}))
    assert reply["ok"], reply
    assert secret not in caplog.text and "Stormwind" not in caplog.text
    app.godmode_config = SimpleNamespace(enabled=False)
    off = asyncio.run(service.handle({"action": "edit", "verb": "set", "kind": "mood",
                                      "args": {"member": 101, "offset": 0.5, "seconds": 3600}}))
    assert not off["ok"] and off["code"] == "off"
    listed = asyncio.run(service.handle({"action": "edit", "verb": "list"}))
    assert listed["ok"] and listed["active"][0]["kind"] == "memory_implant"


def test_cli_reads_edits_and_undoes_them_with_the_mind_stopped(tmp_path, capsys):
    from conftest import run
    from leeroy_mind.tuning.cli import tuning_main
    from tuning.test_tuning_preview import _app

    async def seed():
        app = _app(tmp_path)
        try:
            app.godmode_config = __import__("types").SimpleNamespace(enabled=True)
            r = await app.tuning.handle({"action": "edit", "verb": "set", "kind": "feud_start",
                                         "args": {"member": 101, "other": 103}})
            assert r["ok"], r
        finally:
            app.store.close()
    run(seed())
    args = ["--data-dir", str(tmp_path)]
    assert tuning_main([*args, "edits"]) == 0
    out = capsys.readouterr().out
    assert "#1 feud_start" in out and "edit #1 feud_start: none -> active" in out
    assert tuning_main([*args, "history"]) == 0
    assert "edit #1 feud_start" in capsys.readouterr().out
    assert tuning_main([*args, "undo", "1"]) == 0
    import sqlite3
    conn = sqlite3.connect(tmp_path / "mind.db")
    try:
        assert conn.execute("SELECT count(*) FROM feud WHERE ended IS NULL").fetchone()[0] == 0
    finally:
        conn.close()
