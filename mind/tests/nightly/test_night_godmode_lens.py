"""An edit is a lens, never a write (godmode, review of #298 P1-1). For every night writer of
relationships, persona state, attraction and the romance stage: a night run with godmode edits in
force, then the edits undone, leaves exactly the stored rows the same night leaves without them.
Each test runs the writer on the harness (edits in force, then reverted) and on a twin copy of
the same database taken before the edits (no edits), and compares the night-owned tables."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from dbtemplate import open_migrated
from nightly.conftest import NightHarness, StepClock
from nightly.test_night_attraction import neglect_night
from nightly.test_night_romance import arc_woman, nights
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import EffectiveRelationship, EffectiveState, Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import edits as E

run = asyncio.run
OWNER = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)
TABLES = {
    "relationship": "SELECT * FROM relationship ORDER BY a, b",
    "persona_state": "SELECT * FROM persona_state ORDER BY guid",
    "attraction_state": "SELECT * FROM attraction_state ORDER BY guid",
    "persona_core": "SELECT guid, json, version FROM persona_core ORDER BY guid",
    "rel_delta": "SELECT a, b, dim, delta, reason, applied FROM rel_delta ORDER BY id",
    "pad_mood": "SELECT * FROM pad_mood ORDER BY guid",
    "romance_arc": "SELECT * FROM romance_arc ORDER BY guid",
}


def twin(h, tmp_path) -> NightHarness:
    """A copy of the harness's database as it stands, on its own clock at the same time."""
    path = tmp_path / "twin.db"
    h.store.conn.commit()
    dst = sqlite3.connect(path)
    h.store.conn.backup(dst)
    dst.close()
    clock = StepClock(datetime.fromtimestamp(h.clock.now(), timezone.utc))
    return NightHarness(Store(open_migrated(path), clock=clock), h.settings, clock)


def stored(h) -> dict:
    return {name: [tuple(r) for r in h.store.conn.execute(sql)] for name, sql in TABLES.items()}


def _pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def lens_on(h, *, women=(), members=(), stage=None):
    """Feelings toward him (and a romance stage for a woman on the arc), feelings between
    members and a mood edit for every one of them: everything a lens can fold."""
    pic = _pic(h.store)
    now = h.clock.now()

    def edit(kind, **a):
        E.set_edit(h.store, {"kind": kind, "args": a}, human=OWNER, source="cli", now=now,
                   pic=pic)
    for g in women:
        a = dict(affinity=60, trust=40, attraction=50)
        if stage:
            a["romance_stage"] = stage
        edit("feeling", member=g, other=1, **a)
        edit("mood", member=g, offset=-0.9, seconds=14 * 86400)
    for g, other in members:
        edit("feeling", member=g, other=other, affinity=-150, trust=-50)
        edit("mood", member=g, offset=0.8, seconds=14 * 86400)
    h.store.reload_edits()


def lens_off(h):
    C.revert_to(h.store, 0, _pic(h.store), source="cli", now=h.clock.now())
    h.store.reload_edits()


def check(h, h2, writer):
    """`writer(harness)` with the lens in force then reverted, against the twin without it."""
    writer(h)
    lens_off(h)
    writer(h2)
    assert stored(h) == stored(h2)


@pytest.fixture
def pair(h, tmp_path):
    for g in (101, 102):
        h.store.put_relationship(Relationship(a=g, b=103, affinity=20, trust=30, familiarity=40,
                                              last_seen=int(h.T - 30 * 86400)))
    return h


def test_the_romance_ladder_climbs_underneath_the_lens(h, tmp_path):
    arc_woman(h, 101, "drawn", stage="notices", attraction=45, affinity=30, trust=25)
    h2 = twin(h, tmp_path)
    lens_on(h, women=(101,), stage="planning")
    assert h.store.get_relationship(101, 1, lens=True).romance_stage == "planning"
    check(h, h2, lambda hh: nights(hh, [101], 8))


def test_the_night_attraction_stage_decays_the_stored_score(h, tmp_path):
    h.woman(103, "drawn")
    h.store.put_relationship(Relationship(a=103, b=1, attraction=40, affinity=20))
    h.seen(103)
    h2 = twin(h, tmp_path)
    lens_on(h, women=(103,))

    def night(hh):
        from leeroy_mind.nightly.attraction import stage_attraction
        neglect_night(hh, hh.T + 3600)
        run(stage_attraction(hh.ctx()))
    check(h, h2, night)


def test_apply_act_and_its_stance_band_read_the_stored_score(h, tmp_path):
    h.woman(101, "drawn")
    h.store.put_relationship(Relationship(a=101, b=1, attraction=60))
    h.seen(101)
    h2 = twin(h, tmp_path)
    lens_on(h, women=(101,))

    def act(hh):
        from leeroy_mind.nightly.attraction import apply_act
        apply_act(hh.store, human=1, guid=101, act="flirt", amount=12, mult=1.0, ref="chat:1",
                  ts=hh.clock.now(), live=False, stance="drawn")
    check(h, h2, act)
    assert load_persona(h.store, 101).romance.stance == load_persona(h2.store, 101).romance.stance


def test_reconcile_and_restore_stance_write_absolute_scores_underneath(h, tmp_path):
    h.woman(102, "fond")
    h.store.put_relationship(Relationship(a=102, b=1, attraction=10))
    h2 = twin(h, tmp_path)
    lens_on(h, women=(102,))

    def write(hh):
        from leeroy_mind.nightly.attraction import reconcile, restore_stance, set_stance
        reconcile(hh.store, 102, now=hh.clock.now())
        set_stance(hh.store, 102, "drawn", now=hh.clock.now(), reason="test", evidence=[],
                   score_before=hh.store.get_relationship(102, 1).attraction)
        row = hh.store.conn.execute("SELECT * FROM persona_ledger WHERE guid=102 "
                                    "ORDER BY id DESC LIMIT 1").fetchone()
        assert restore_stance(hh.store, row, now=hh.clock.now())
    check(h, h2, write)


def test_relations_gains_and_regression_move_the_stored_rows(pair, tmp_path):
    h = pair
    h2 = twin(h, tmp_path)
    lens_on(h, members=((101, 103), (102, 103)))

    def write(hh):
        from leeroy_mind.nightly.relations import apply_capped, regress
        apply_capped(hh.store, night=hh.night, a=101, b=103, dim="affinity", delta=3, speed=1.0,
                     reason="copresence", evidence=("chat:1",), ts=hh.clock.now(), human=1)
        apply_capped(hh.store, night=hh.night, a=102, b=103, dim="trust", delta=-2, speed=1.0,
                     reason="gossip", evidence=("chat:2",), ts=hh.clock.now(), human=1)
        regress(hh.store, weeks=4.0, now=hh.clock.now(), human=1)
    check(h, h2, write)


def test_breakups_and_weekends_write_absolute_stages_underneath(h, tmp_path):
    arc_woman(h, 101, "drawn", stage="planning")
    arc_woman(h, 102, "lust", stage="weekend")
    with h.store.transaction():
        away = h.store.conn.execute(
            "INSERT INTO away(guid,reason,left_at,returns_at,returned_at) VALUES "
            "(102,'weekend',?,?,?)", (h.T - 3 * 86400, h.T - 86400, h.T - 86400)).lastrowid
        h.store.conn.execute("INSERT INTO arc_meetup(absence,guid,away_id,kind,written) "
                             "VALUES (1,102,?,'weekend',?)", (away, h.T - 3 * 86400))
    h2 = twin(h, tmp_path)
    lens_on(h, women=(101, 102), stage="whispers")
    assert h.store.get_relationship(101, 1, lens=True).romance_stage == "whispers"

    def write(hh):
        from leeroy_mind.arc.state import end_romance
        from leeroy_mind.arc.weekend import settle
        assert end_romance(hh.store, 101, 1, by="owner", now=hh.clock.now())
        settle(hh.ctx())
    check(h, h2, write)
    assert h.store.get_relationship(101, 1).romance_stage == "ex"
    assert h.store.get_relationship(102, 1).romance_stage == "after"


def test_the_mood_stage_and_state_writers_keep_the_stored_mood(h, tmp_path):
    h.woman(101, "drawn")
    with h.store.transaction():
        for g in (101, 102):
            h.store.put_state(replace(h.store.get_state(g), mood=0.2))
    h2 = twin(h, tmp_path)
    lens_on(h, women=(101,), members=((102, 103),))
    assert isinstance(h.store.get_state(101, lens=True), EffectiveState)

    def write(hh):
        from leeroy_mind.nightly import mood
        from leeroy_mind.nightly.growth import _store_note
        from leeroy_mind.nightly.wowlife import refresh_on_mind
        run(mood.stage_mood(hh.ctx()))
        refresh_on_mind(hh.store, 101, dial=5)
        _store_note(hh.store, 102, {"text": "keeps a tidy bank", "evidence": [], "added": 1.0},
                    hh.clock.now())
        # the talk engine's altered-state writes: read, change one field, write
        hh.store.put_state(replace(hh.store.get_state(101), altered_state="tipsy"))
    check(h, h2, write)


def test_reseeds_read_the_stored_row(h, tmp_path):
    from leeroy_mind.personas.seed import human_seed
    from leeroy_mind.personas.stances import apply_reseeds, plan_reseeds
    h.woman(104, "fond")
    seed = human_seed(104, 1, "fond", "stance", "")
    h.store.put_relationship(seed)
    h2 = twin(h, tmp_path)
    lens_on(h, women=(104,))

    def write(hh):
        apply_reseeds(hh.store, plan_reseeds(hh.store, [(104, "fond", "drawn")], 1), 1)
    check(h, h2, write)


def test_a_lens_is_never_written(h):
    h.store.put_relationship(Relationship(a=101, b=103, affinity=10))
    lens_on(h, members=((101, 103),))
    lensed = h.store.get_relationship(101, 103, lens=True)
    assert isinstance(lensed, EffectiveRelationship) and lensed.affinity == -100
    assert type(h.store.get_relationship(101, 103)) is Relationship
    with pytest.raises(TypeError):
        h.store.put_relationship(replace(lensed, affinity=5))
    with pytest.raises(TypeError):
        h.store.put_state(h.store.get_state(101, lens=True))
    assert h.store.get_relationship(101, 103).affinity == 10


def test_the_night_reads_her_persona_without_trait_edits(h):
    pic = _pic(h.store)
    E.set_edit(h.store, {"kind": "trait", "args": {"member": 101, "path": "humour",
                                                   "value": "absurd"}},
               human=OWNER, source="cli", now=h.clock.now(), pic=pic)
    ctx = h.ctx()
    assert ctx.persona(101).humour == load_persona(h.store, 101).humour
    assert ctx.personas.get(101).humour == "absurd"  # the talk side's lens


def test_lens_readers_skip_a_malformed_edit(h, caplog):
    h.store.set_meta(E.CURRENT + "99", json.dumps({"kind": "feeling", "retired": None}))
    h.store.reload_edits()
    assert h.store.get_relationship(101, 102, lens=True) == Relationship(a=101, b=102)
    assert E.feeling_overlay(h.store.conn) == {}
    assert "godmode_edit_malformed" in caplog.text
