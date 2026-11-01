import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from leeroy_mind.nightly.knowledge import learn
from leeroy_mind.nightly.romance import (can_advance, jealousy_level, jealousy_next,
    jealousy_rows, known_acts, stage_jealousy, stage_romance)
from leeroy_mind.nightly.store import ledger_add

run = asyncio.run
DAY = 86400


def relation(at=99, af=99, tr=99):
    return SimpleNamespace(attraction=at, affinity=af, trust=tr)


def test_can_advance_attitude_style_rush_depth_and_evidence():
    args = dict(attitude="drawn", style="earnest", days_since_change=4, new_evidence=True)
    assert can_advance(1, relation(36, 21, 11), **args)
    assert not can_advance(1, relation(), **(args | {"attitude": "fond"}))
    assert can_advance(1, relation(), **(args | {"attitude": "fond", "rush": True}))
    assert can_advance(1, relation(), **(args | {"attitude": "lust", "style": "bold", "days_since_change": 2}))
    assert not can_advance(1, relation(), **(args | {"attitude": "lust", "style": "bold", "days_since_change": 1.5}))
    assert not can_advance(1, relation(), **(args | {"style": "guarded"}))
    assert not can_advance(1, relation(), **(args | {"new_evidence": False}))
    assert not can_advance(1, relation(30), **args)
    args.update(days_since_change=99)
    assert not can_advance(2, relation(), **args)
    assert can_advance(2, relation(), depth=1, **args)
    assert not can_advance(6, relation(), depth=3, **args)
    assert not can_advance(0, relation(), **(args | {"attitude": "none"}))


def test_every_attitude_women_only_one_step_and_checkpoint(h):
    for g, stance in ((101, "drawn"), (102, "fond"), (103, "lust"), (104, "none")):
        h.woman(g, stance, style="earnest" if stance != "none" else None)
        h.rel(g, 1, attraction=90, affinity=90, trust=90)
        h.mem(g, "Testy laughed at my joke.", valence=.8, actors=(1,))
    h.persona(105, stance="fond", player_gender="male", style="earnest")
    h.rel(105, 1, attraction=90, affinity=90, trust=90)
    h.mem(105, "Testy laughed at my joke.", valence=.8, actors=(1,))
    ctx = h.ctx()
    run(stage_romance(ctx))
    run(stage_romance(ctx))
    assert not ctx.failures
    assert [h.store.get_relationship(g, 1).romance_stage for g in range(101, 106)] == [
        "notices", "notices", "notices", "none", "none"]
    rows = h.store.conn.execute("SELECT evidence FROM rel_delta WHERE dim='romance_stage'").fetchall()
    assert len(rows) == 3 and all(json.loads(r[0])[0].startswith("memory:") for r in rows)


def test_dial_never_caps_progress_and_ex_never_moves(h):
    h.woman(101, "lust", style="earnest")
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="whispers")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO disclosure(guid,other,depth,response,ts,ref) "
                             "VALUES (101,1,2,'responsive',?,'chat:1')", (h.T - DAY,))
    h.mem(101, "Testy listened.", valence=.9, actors=(1,))
    h.woman(102, "lust")
    h.rel(102, 1, attraction=99, affinity=99, trust=99, romance_stage="ex")
    h.mem(102, "Testy listened.", valence=.9, actors=(1,))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, romance_dial=1)
    run(stage_romance(ctx))
    assert not ctx.failures
    assert h.store.get_relationship(101, 1).romance_stage == "vulnerable"
    assert h.store.get_relationship(102, 1).romance_stage == "ex"


def test_evidence_must_be_active_positive_and_newer_than_last_change(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=90, affinity=90, trust=90, romance_stage="notices")
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "none->notices", [], h.T - 10 * DAY, applied=True)
    h.mem(101, "old pleasant memory", t=h.T - 11 * DAY, actors=(1,), valence=.8)
    h.mem(101, "archived memory", actors=(1,), valence=.8, status="archived")
    h.mem(101, "future memory", t=h.T + DAY, actors=(1,), valence=.8)
    h.mem(101, "bad memory", actors=(1,), valence=-.8)
    h.mem(101, "someone else", actors=(102,), valence=.8)
    run(stage_romance(h.ctx()))
    assert h.store.get_relationship(101, 1).romance_stage == "notices"


def test_oldest_delta_starts_the_wait_and_rush_halves_it(h):
    h.woman(101, "drawn", style="earnest")
    h.rel(101, 1, attraction=90, affinity=90, trust=90, romance_stage="notices")
    h.store.add_rel_delta(101, 1, "affinity", 1, "first met", [], h.T - 2 * DAY, applied=True)
    h.mem(101, "Testy remembered my story.", actors=(1,), valence=.8)
    h.seen(101)
    with h.store.transaction():
        h.store.conn.execute("UPDATE attraction_state SET rush_until=? WHERE guid=101", (h.T + 1800,))
    run(stage_romance(h.ctx()))
    assert h.store.get_relationship(101, 1).romance_stage == "banter"


def test_jealousy_formula():
    assert jealousy_next(0, nights=1, obs=2, spike=False, knob=5) == 8.
    assert jealousy_next(50, nights=1, obs=5, spike=True, knob=0) == 0.
    assert jealousy_next(40, nights=7, obs=0, spike=False, knob=5) == pytest.approx(12.823, abs=.001)
    assert jealousy_next(0, nights=1, obs=1, spike=False, knob=5, mult=.5) == 2.
    assert jealousy_next(95, nights=0, obs=5, spike=True, knob=10) == 100.


def rivalry(h):
    h.woman(101, "lust")
    h.woman(102, "fond")
    for g in (101, 102):
        h.rel(g, 1, romance_stage="banter")


def test_rivalry_envy_modes_and_no_relationship_writes(h):
    rivalry(h)
    h.persona(103, stance="none", player_gender="male", jealousy_mode="sulker")
    h.persona(104, stance="none", player_gender="male", jealousy_mode=None)
    h.persona(105, stance="fond", player_gender="male", jealousy_mode="teaser")
    for _ in range(5):
        h.mem(101, "Belra buffed Testy first again.", actors=(102, 1))
    for g in (103, 105):
        h.mem(g, "Belra fussing over Testy.", actors=(102, 1))
    before = h.count("rel_delta"), h.count("relationship")
    ctx = h.ctx()
    run(stage_jealousy(ctx))
    assert not ctx.failures
    assert jealousy_level(h.store, 101, 102) == 20.
    assert jealousy_level(h.store, 102, 101) == 0.
    assert jealousy_level(h.store, 103, 1) == pytest.approx(4.8)
    assert jealousy_level(h.store, 104, 1) == 0.
    assert jealousy_level(h.store, 105, 1) == pytest.approx(3.2)
    assert (h.count("rel_delta"), h.count("relationship")) == before
    assert not [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]
    assert jealousy_rows(h.store, 10) == [(101, 102, "rivalry", 20.)]


def test_bridge_once_after_three_nights_and_resumes(h):
    rivalry(h)
    levels = []
    for day in range(3):
        for _ in range(5):
            h.mem(101, "Belra buffed Testy first again.", t=h.clock.now(), actors=(102, 1))
        ctx = h.ctx(night=f"2026-10-{29 + day}")
        run(stage_jealousy(ctx))
        run(stage_jealousy(ctx))
        assert not ctx.failures
        levels.append(jealousy_level(h.store, 101, 102))
        h.clock.advance(DAY)
    assert levels == pytest.approx([20, 37, 51.45])
    rows = [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]
    assert len(rows) == 1
    assert rows[0].key == "jealousy:102:2026-W44"
    assert rows[0].text == "Belra keeps going after Testy's attention."
    assert rows[0].scope == "open" and len(rows[0].evidence) == 3


def act(h, guid, line, *, kind="flirt"):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) "
                             "VALUES (?,?,20,1,?,?,1)", (guid, kind, f"chat:{line}", h.clock.now()))


def test_known_acts_audience_private_gossip_and_duplicate_nudge(h):
    rivalry(h)
    public = h.say(h.T, 1, "belra you're cute")
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps([101]), public))
    act(h, 102, public)
    h.store.add_rel_delta(101, 1, "attraction", -2,
        f"saw flirt (rivalry, not hurt: chat:{public})", [f"chat:{public}"], h.T, applied=True)
    private = h.say(h.T + 1, 1, "you're cute", channel="whisper", to=102)
    act(h, 102, private)
    assert known_acts(h.store, 101, 102, h.ctx().window) == [f"chat:{public}"]
    learn(h.store, f"chat:{private}", 101, "told", via=102, ts=h.T + 2)
    assert known_acts(h.store, 101, 102, h.ctx().window) == [f"chat:{public}", f"chat:{private}"]
    assert known_acts(h.store, 103, 102, h.ctx().window) == []


def test_private_gossip_bridge_stays_private_without_partner(h):
    rivalry(h)
    private = h.say(h.T, 1, "you're cute", channel="whisper", to=102)
    act(h, 102, private)
    learn(h.store, f"chat:{private}", 101, "told", via=102, ts=h.T + 1)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',39,?,'[]')", (h.T,))
    ctx = h.ctx()
    run(stage_jealousy(ctx))
    assert not ctx.failures
    (memory,) = [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]
    assert (memory.scope, memory.scope_with) == ("private", 0)
    assert memory.evidence == (f"chat:{private}",)


def test_band_spike_requires_observed_act_and_undo_cancels_it(h):
    rivalry(h)
    row = ledger_add(h.store, guid=102, kind="stance_band", text="fond -> drawn: act flirt",
                     evidence=[], now=h.T, score_before=30)
    ctx = h.ctx()
    run(stage_jealousy(ctx))
    assert jealousy_level(h.store, 101, 102) == 0
    public = h.say(h.T + DAY, 1, "belra you're cute")
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience='[101]' WHERE id=?", (public,))
    act(h, 102, public)
    row = ledger_add(h.store, guid=102, kind="stance_band", text="fond -> drawn: act flirt",
                     evidence=[f"chat:{public}"], now=h.T + DAY, score_before=30)
    ledger_add(h.store, guid=102, kind="undo", text="undone", evidence=[], ref=row, now=h.T + DAY)
    run(stage_jealousy(h.ctx(night="2026-10-30")))
    assert jealousy_level(h.store, 101, 102) == 4


def test_romance_uses_fresh_stored_stance(h):
    h.woman(101, "fond", style="earnest")
    h.rel(101, 1, attraction=90, affinity=90, trust=90, romance_stage="notices")
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "none->notices", [], h.T - 2 * DAY, applied=True)
    h.mem(101, "Testy remembered my story.", actors=(1,), valence=.8)
    ctx = h.ctx()
    assert ctx.attitudes()[101] == "fond"
    h.woman(101, "lust", style="earnest")
    run(stage_romance(ctx))
    assert h.store.get_relationship(101, 1).romance_stage == "banter"


def test_jealousy_rollback_and_retry_are_atomic(h, monkeypatch):
    rivalry(h)
    for _ in range(5):
        h.mem(101, "Belra buffed Testy first.", actors=(102, 1))
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',39,?,'[]')", (h.T,))
    ctx = h.ctx()
    put = ctx.writer.put
    monkeypatch.setattr(ctx.writer, "put", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("failed write")))
    run(stage_jealousy(ctx))
    assert ctx.failures["jealousy"] == 1 and not ctx.done("jealousy", "all")
    assert jealousy_level(h.store, 101, 102) == 39
    monkeypatch.setattr(ctx.writer, "put", put)
    run(stage_jealousy(ctx))
    assert ctx.done("jealousy", "all") and jealousy_level(h.store, 101, 102) == 59


@pytest.mark.parametrize("private", [False, True])
def test_same_run_advancement_after_window_is_known_only_to_witnesses(h, private):
    rivalry(h)
    h.woman(102, "lust", style="earnest")
    h.rel(102, 1, attraction=99, affinity=99, trust=99, romance_stage="vulnerable")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO disclosure(guid,other,depth,response,ts,ref) "
                             "VALUES (102,1,2,'responsive',?,'chat:1')", (h.T - DAY,))
    line = h.say(h.T, 1, "belra i remembered your birthday",
                 channel="whisper" if private else "guild", to=102 if private else 0)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?",
                             (json.dumps([102] if private else [101, 102]), line))
    h.mem(102, "Testy remembered my birthday.", actors=(1,), valence=.8,
          evidence=(f"chat:{line}",))
    ctx = h.ctx()
    h.clock.set(ctx.window[1] + 60)
    run(stage_romance(ctx))
    assert h.store.get_relationship(102, 1).romance_stage == "invitation"
    assert json.loads(h.store.get_meta("romance_changes:2026-10-29"))
    # A new context represents a process resumed between the two committed stages.
    resumed = h.ctx()
    run(stage_jealousy(resumed))
    assert not resumed.failures
    expected = 0 if private else 15
    assert jealousy_level(h.store, 101, 102) == expected
    h.clock.advance(DAY)
    run(stage_jealousy(h.ctx(night="2026-10-30")))
    # The previous run's after-window delta must not spike again in this window.
    assert jealousy_level(h.store, 101, 102) == pytest.approx(expected * .85)


def test_unseen_private_stage_cannot_cross_threshold_or_create_open_memory(h):
    rivalry(h)
    private = h.say(h.T, 1, "a private invitation", channel="whisper", to=102)
    mem = h.mem(102, "Testy invited me.", actors=(1,), valence=.8, evidence=(f"chat:{private}",))
    h.store.add_rel_delta(102, 1, "romance_stage", 1, "vulnerable->invitation",
                          [f"memory:{mem}"], h.T, applied=True)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',39,?,'[]')", (h.T,))
    run(stage_jealousy(h.ctx()))
    assert jealousy_level(h.store, 101, 102) == 39
    assert not [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]


def test_fourth_private_observation_still_controls_bridge_scope(h):
    rivalry(h)
    for _ in range(3):
        h.mem(101, "Belra buffed Testy first.", actors=(102, 1))
    private = h.say(h.T, 1, "something personal", channel="whisper", to=102)
    h.mem(101, "Belra told me what Testy whispered.", actors=(102, 1), evidence=(f"chat:{private}",),
          scope="private", scope_with=0)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',25,?,'[]')", (h.T,))
    run(stage_jealousy(h.ctx()))
    (memory,) = [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]
    assert len(memory.evidence) == 3
    assert (memory.scope, memory.scope_with) == ("private", 0)
