import asyncio
import json
from dataclasses import replace
from datetime import date, timedelta, timezone
from types import SimpleNamespace

import pytest

from leeroy_mind.arc.state import play_days, play_days_after
from leeroy_mind.nightly.knowledge import learn
from leeroy_mind.nightly.romance import (can_advance, jealousy_level, jealousy_next,
    jealousy_rows, known_acts, picture_available, stage_jealousy, stage_romance,
    strong_evidence)
from leeroy_mind.nightly.store import ledger_add
from leeroy_mind.storage.rows import ROMANCE_STAGES

run = asyncio.run
DAY = 86400
HOUR = 3600
UTC = timezone.utc


def relation(at=99, af=99, tr=99):
    return SimpleNamespace(attraction=at, affinity=af, trust=tr)


def played_before(h, *days_ago):
    """His sessions, 21:00 to 23:00, the given whole days before the harness time."""
    for n in days_ago:
        h.session(h.T - n * DAY, h.T - n * DAY + 2 * HOUR)


def arc_woman(h, guid, stance, *, style="earnest", stage="notices", depth=2, **scores):
    """A woman on the arc toward him: high scores unless given, opened up to `depth`."""
    h.woman(guid, stance, style=style)
    h.rel(guid, 1, **({"attraction": 99, "affinity": 99, "trust": 99} | scores),
          romance_stage=stage)
    if depth:
        with h.store.transaction():
            h.store.conn.execute("INSERT INTO disclosure(guid,other,depth,response,ts,ref) "
                                 "VALUES (?,1,?,'responsive',?,'chat:1')", (guid, depth, h.T - DAY))


def nights(h, guids, days, *, first=0, played=lambda d: d % 7 != 6):
    """Calendar days `first`.. from the harness night. On a day `played(d)` (6 a week by
    default) he plays 21:00-23:00 and each woman in `guids` keeps a warm memory of him; the
    night's romance stage runs the next morning at 07:00, past its window. Per day: (the play
    days of this call so far, {guid: stage after the night})."""
    log, count = [], 0
    for d in range(first, first + days):
        start = h.T + d * DAY
        if played(d):
            h.session(start, start + 2 * HOUR)
            count += 1
            for g in guids:
                h.mem(g, "Testy stayed up talking with me.", t=start + HOUR, actors=(1,), valence=.8)
        h.clock.set(start + 10 * HOUR)
        ctx = h.ctx(night=(date(2026, 10, 29) + timedelta(days=d)).isoformat())
        run(stage_romance(ctx))
        assert not ctx.failures
        log.append((count, {g: h.store.get_relationship(g, 1).romance_stage for g in guids}))
    return log


def first_play_day(log, guid, stage):
    return next((n for n, stages in log if stages[guid] == stage), None)


def visited(log, guid):
    return list(dict.fromkeys(stages[guid] for _, stages in log))


def test_can_advance_returns_the_stage_its_gate_and_pace_allow():
    args = dict(attitude="drawn", style="earnest", play_days=2, new_evidence=True)
    assert can_advance("notices", relation(30, 15, 5), **args) == "banter"
    assert can_advance("notices", relation(29, 15, 5), **args) is None
    assert can_advance("notices", relation(), **(args | {"play_days": 1.9})) is None
    assert can_advance("notices", relation(), **(args | {"attitude": "lust", "play_days": 1.4})) == "banter"
    assert can_advance("notices", relation(), **(args | {"attitude": "lust", "play_days": 1.3})) is None
    assert can_advance("notices", relation(), **(args | {"attitude": "fond"})) is None
    assert can_advance("notices", relation(), **(args | {"attitude": "fond", "rush": True})) == "banter"
    assert can_advance("notices", relation(), **(args | {"attitude": "fond", "strong": True})) == "banter"
    # rush and strong evidence halve the wait once between them, never to a quarter
    assert can_advance("notices", relation(), **(args | {"attitude": "fond", "play_days": 1,
                                                       "rush": True, "strong": True})) is None
    assert can_advance("notices", relation(), **(args | {"style": "guarded"})) is None
    assert can_advance("notices", relation(), **(args | {"style": "bold", "play_days": 1.6})) == "banter"
    assert can_advance("notices", relation(), **(args | {"style": None})) == "banter"
    assert can_advance("notices", relation(), **(args | {"new_evidence": False})) is None
    assert can_advance("none", relation(), **(args | {"attitude": "none"})) is None
    args.update(play_days=99)
    assert can_advance("whispers", relation(), **args) is None
    assert can_advance("whispers", relation(), depth=1, **args) == "late_nights"
    assert can_advance("banter", relation(), **(args | {"attitude": "fond"})) == "whispers"
    assert can_advance("whispers", relation(), depth=3, **(args | {"attitude": "fond"})) is None
    assert can_advance("late_nights", relation(), depth=3, **(args | {"attitude": "fond"})) is None
    assert can_advance("voice", relation(), depth=2, **args) == "confession"
    assert can_advance("voice", relation(62, 37, 37), depth=2, **args) is None
    assert can_advance("voice", relation(62, 37, 37), depth=1, picture=True, **args) == "picture"
    assert can_advance("picture", relation(), depth=2, **args) == "confession"
    for stage in ("planning", "weekend", "after", "ex"):
        assert can_advance(stage, relation(), depth=3, **args) is None


def test_play_days_counts_the_nights_he_played(h):
    t = h.T  # 21:00, night 10-29
    h.session(t, t + 2 * HOUR)
    h.session(t + 3 * HOUR, t + 4 * HOUR)  # 00:00-01:00: still night 10-29, counted once
    h.session(t + DAY + 8 * HOUR, t + DAY + 10 * HOUR)  # 05:00-07:00: nights 10-30 and 10-31
    h.session(t + 3 * DAY + HOUR, t + 3 * DAY + 9 * HOUR)  # 22:00 to 06:00: night 11-01 only
    open_from = t + 5 * DAY  # 11-03 21:00, never ended: counts to `until`
    h.store.start_session(open_from)
    until = open_from + 8 * HOUR  # 11-04 05:00, still night 11-03
    assert play_days(h.store, t - DAY, until, UTC) == 5
    assert play_days(h.store, t + HOUR, until, UTC) == 5  # a session cut by `since` still counts
    assert play_days(h.store, t + 9 * HOUR, until, UTC) == 4  # 06:00: night 10-29 is over
    assert play_days(h.store, t - DAY, t + 4 * DAY, UTC) == 4  # no open session yet
    assert play_days(h.store, t - DAY, t + DAY + 9 * HOUR, UTC) == 2  # 06:00 10-31 is exclusive
    assert play_days(h.store, until, until, UTC) == 0
    assert play_days(h.store, t - 30 * DAY, t - DAY, UTC) == 0


def test_strong_evidence_by_the_stage_it_speaks_for(h):
    since, until = h.T - HOUR, h.T + 10 * HOUR

    def strong(target, guid=101):
        return strong_evidence(h.store, guid, 1, target, since, until, UTC)

    assert not any(strong(stage) for stage in ROMANCE_STAGES)
    h.say(h.T - 2 * HOUR, 1, "hey you", channel="whisper", to=101)  # before `since`
    h.say(h.T + 4 * HOUR, 1, "still up?", channel="whisper", to=102)  # another woman
    assert not strong("whispers") and not strong("late_nights")
    h.say(h.T, 101, "evening, you", channel="whisper", to=1)  # 21:00
    assert strong("whispers") and not strong("late_nights")
    h.say(h.T + 6 * HOUR + 30 * 60, 1, "can't sleep either", channel="whisper", to=101)  # 03:30
    assert strong("late_nights")
    h.store.log_utterance(h.T + HOUR, 101, "voice:guild_hall", "open", "Evening, all.", [], None, None)
    h.store.log_utterance(h.T + HOUR, 101, "voice:private2", "open", "Just us.", [], "too_long", None)
    h.store.log_utterance(h.T - 2 * HOUR, 101, "voice:private1", "open", "Just us.", [], None, None)
    assert not strong("voice")
    h.store.log_utterance(h.T + HOUR, 101, "voice:private3", "open", "Just us.", [], None, None)
    assert strong("voice") and not strong("voice", 102)
    h.mem(101, "Testy said he likes my jokes.", actors=(1,), valence=.8, importance=6)
    h.mem(101, "Testy and I had a real fight.", actors=(1,), valence=-.6, importance=8)
    h.mem(101, "A big night with my guild.", actors=(102,), valence=.8, importance=8)
    assert not strong("confession")
    h.mem(101, "Testy told me I matter to him.", actors=(1,), valence=.8, importance=7)
    assert strong("confession")
    assert not any(strong(stage) for stage in ("notices", "banter", "picture", "planning"))


def test_a_typical_drawn_woman_reaches_planning_in_about_three_weeks_of_play(h):
    arc_woman(h, 101, "drawn")
    log = nights(h, [101], 26)
    first = first_play_day(log, 101, "planning")
    assert first is not None and 16 <= first <= 21
    assert visited(log, 101) == ["notices", "banter", "whispers", "late_nights", "voice",
                                 "confession", "planning"]


def test_a_typical_lust_woman_reaches_planning_in_about_two_weeks(h):
    arc_woman(h, 102, "lust")
    log = nights(h, [102], 20)
    first = first_play_day(log, 102, "planning")
    assert first is not None and 11 <= first <= 13
    at = next(i for i, (_, stages) in enumerate(log) if stages[102] == "planning")
    assert {stages[102] for _, stages in log[at:]} == {"planning"}  # planning on is the arc's


def test_a_lust_woman_is_faster_and_a_shy_one_slower(h):
    arc_woman(h, 101, "drawn")
    arc_woman(h, 102, "lust")
    arc_woman(h, 103, "drawn", style="shy")
    log = nights(h, [101, 102, 103], 40)
    lust, drawn, shy = (first_play_day(log, g, "planning") for g in (102, 101, 103))
    assert None not in (lust, drawn, shy) and lust < drawn < shy


def test_a_step_carries_only_what_its_night_left_over(h):
    arc_woman(h, 101, "lust")  # banter waits 1.4 play days: the second night leaves 0.6
    arc_woman(h, 102, "lust", attraction=25)  # held below banter's 30 by her score
    arc_woman(h, 103, "lust", stage="whispers")  # late nights wait 2.1
    h.store.add_rel_delta(103, 1, "romance_stage", 1, "banter->whispers", [], h.T - 12 * HOUR,
                          applied=True)
    h.store.set_meta("romance_carry:103:1", json.dumps({"stage": "late_nights", "days": .9,
                                                            "ts": h.T - 12 * HOUR}))
    carry = lambda g: json.loads(h.store.get_meta(f"romance_carry:{g}:1"))
    log = nights(h, [101, 102, 103], 4)
    assert [stages[101] for _, stages in log] == ["notices", "banter", "whispers", "whispers"]
    assert carry(101) == {"stage": "whispers", "days": .2,
                          "ts": h.T + 2 * DAY + 10 * HOUR}  # 1 + 0.6 - 1.4, at the change's ts
    assert log[1][1][103] == "whispers"  # 2 play days and a carry for another stage: not 2.9
    h.rel(102, 1, attraction=99)
    assert nights(h, [102], 1, first=4)[-1][1] == {102: "banter"}
    assert carry(102) == {"stage": "banter", "days": 0.0,
                          "ts": h.T + 4 * DAY + 10 * HOUR}  # 5 - 1.4: held, so nothing carries


def test_a_week_away_pauses_the_arc(h):
    arc_woman(h, 101, "drawn", stage="whispers")
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "banter->whispers", [], h.T - 12 * HOUR,
                          applied=True)
    log = nights(h, [101], 10, played=lambda d: d in (0, 9))
    assert [stages[101] for _, stages in log] == ["whispers"] * 10
    assert nights(h, [101], 1, first=10)[-1][1] == {101: "late_nights"}


def test_fond_women_stop_at_whispers_until_drawn(h):
    arc_woman(h, 101, "fond", stage="banter")
    arc_woman(h, 102, "fond", stage="late_nights")  # stored above the cap: kept, never moved on
    log = nights(h, [101, 102], 14)
    assert first_play_day(log, 101, "whispers") == 4
    assert visited(log, 101) == ["banter", "whispers"] and visited(log, 102) == ["late_nights"]
    h.woman(101, "drawn", style="earnest")
    assert nights(h, [101], 1, first=14)[-1][1] == {101: "late_nights"}


def test_the_picture_stage_is_skipped_until_p4(h):
    assert not picture_available(h.store)
    arc_woman(h, 101, "drawn", stage="voice")
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "late_nights->voice", [], h.T - 12 * HOUR,
                          applied=True)
    assert [stages[101] for _, stages in nights(h, [101], 3)] == ["voice", "voice", "confession"]
    assert [r[0] for r in h.store.conn.execute(
        "SELECT reason FROM rel_delta WHERE a=101 AND reason LIKE 'voice->%'")] == ["voice->confession"]
    with h.store.transaction():  # P4's table, as far as the ladder cares
        h.store.conn.execute("CREATE TABLE selfie_snap (id INTEGER PRIMARY KEY)")
    assert picture_available(h.store)
    arc_woman(h, 102, "drawn", stage="voice")
    h.store.add_rel_delta(102, 1, "romance_stage", 1, "late_nights->voice", [], h.clock.now(),
                          applied=True)
    assert [stages[102] for _, stages in nights(h, [102], 2, first=3)] == ["voice", "picture"]


def test_cooling_stalls_and_never_moves_back(h):
    arc_woman(h, 101, "drawn", stage="late_nights", attraction=52)  # voice wants 55
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "whispers->late_nights", [],
                          h.T - 12 * HOUR, applied=True)
    log = nights(h, [101], 5)
    h.rel(101, 1, attraction=10)  # below every gate
    log += nights(h, [101], 3, first=5)
    assert visited(log, 101) == ["late_nights"]
    h.rel(101, 1, attraction=99)
    assert nights(h, [101], 1, first=8)[-1][1] == {101: "voice"}
    assert h.store.conn.execute("SELECT count(*) FROM rel_delta WHERE dim='romance_stage' "
                                "AND delta<0").fetchone()[0] == 0


def test_private_voice_time_halves_the_wait(h):
    for g in (101, 102):
        arc_woman(h, g, "drawn", stage="late_nights")
        h.store.add_rel_delta(g, 1, "romance_stage", 1, "whispers->late_nights", [],
                              h.T - 12 * HOUR, applied=True)
    h.store.log_utterance(h.T + HOUR, 101, "voice:private1", "open", "Just us in here.", [], None, None)
    h.store.log_utterance(h.T + HOUR, 102, "voice:guild_hall", "open", "Evening, all.", [], None, None)
    assert nights(h, [101, 102], 2)[-1][1] == {101: "voice", 102: "late_nights"}
    assert nights(h, [101, 102], 1, first=2)[-1][1] == {101: "voice", 102: "voice"}


def test_every_attitude_women_only_one_step_and_checkpoint(h):
    played_before(h, 2, 1)  # a fond woman's wait for notices
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
    played_before(h, 3, 2, 1)
    h.woman(101, "lust", style="earnest")
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="whispers")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO disclosure(guid,other,depth,response,ts,ref) "
                             "VALUES (101,1,2,'responsive',?,'chat:1')", (h.T - DAY,))
    h.mem(101, "Testy listened.", valence=.9, actors=(1,))
    for g, stage in ((102, "ex"), (103, "planning"), (104, "weekend"), (105, "after")):
        arc_woman(h, g, "lust", stage=stage, depth=3)  # planning on is the arc's, ex is history
        h.mem(g, "Testy listened.", valence=.9, actors=(1,))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, romance_dial=1)
    run(stage_romance(ctx))
    assert not ctx.failures
    assert [h.store.get_relationship(g, 1).romance_stage for g in range(101, 106)] == [
        "late_nights", "ex", "planning", "weekend", "after"]


def test_evidence_must_be_active_positive_and_newer_than_last_change(h):
    played_before(h, 9, 8, 7)
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
    played_before(h, 3, 1)  # the first is before the wait starts: one play day of banter's two
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
    played_before(h, 1)
    h.session(h.T - 2 * DAY + HOUR, h.T - 2 * DAY + 2 * HOUR)  # lust's 1.4 play days, not fond's 4
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
    played_before(h, 3, 2, 1)
    rivalry(h)
    h.woman(102, "lust", style="earnest")
    h.rel(102, 1, attraction=99, affinity=99, trust=99, romance_stage="confession")
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
    assert h.store.get_relationship(102, 1).romance_stage == "planning"
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
    h.store.add_rel_delta(102, 1, "romance_stage", 1, "confession->planning",
                          [f"memory:{mem}"], h.T, applied=True)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',39,?,'[]')", (h.T,))
    run(stage_jealousy(h.ctx()))
    assert jealousy_level(h.store, 101, 102) == 39
    assert not [m for m in h.rows(101) if m.key and m.key.startswith("jealousy:")]


@pytest.mark.parametrize("stage", ["planning", "weekend", "after"])
def test_women_from_planning_to_after_stay_rivals_and_their_advance_spikes(h, stage):
    for g in (101, 102):
        h.woman(g, "lust")
        h.rel(g, 1, romance_stage=stage)
    line = h.say(h.T, 1, "belra, about that trip")
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps([101, 102]), line))
    mem = h.mem(102, "Testy talked about our trip.", actors=(1,), valence=.8,
                evidence=(f"chat:{line}",))
    before = ROMANCE_STAGES[ROMANCE_STAGES.index(stage) - 1]
    h.store.add_rel_delta(102, 1, "romance_stage", 1, f"{before}->{stage}", [f"memory:{mem}"],
                          h.T, applied=True)
    ctx = h.ctx()
    h.clock.set(ctx.window[1] + 60)
    run(stage_jealousy(ctx))
    assert not ctx.failures
    assert jealousy_level(h.store, 101, 102) == 15


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


def test_a_play_night_split_by_a_stage_change_counts_once(h):
    h.session(h.T, h.T + 5 * HOUR)  # 21:00 to 02:00: night 10-29
    h.session(h.T + DAY, h.T + DAY + 2 * HOUR)  # night 10-30
    change = h.T + 2 * HOUR  # 23:00, inside the first night
    before = play_days(h.store, h.T - DAY, change, UTC)
    after = play_days_after(h.store, change, h.T + 3 * DAY, UTC)
    assert (before, after) == (1, 1)  # plain play_days would give the span after 2
    assert play_days(h.store, change, h.T + 3 * DAY, UTC) == 2
    quiet = h.T - 3 * DAY + 2 * HOUR  # a change in a night he did not play before it
    h.session(quiet + HOUR, quiet + 2 * HOUR)
    assert play_days_after(h.store, quiet, h.T - 2 * DAY, UTC) == 1


def test_a_carry_belongs_to_one_stage_change(h):
    for guid in (101, 102, 103):  # late nights wait 2.1 play days: two nights need a carry
        arc_woman(h, guid, "lust", stage="whispers")
        h.store.add_rel_delta(guid, 1, "romance_stage", 1, "banter->whispers", [],
                              h.T - 12 * HOUR, applied=True)
    carry = lambda g, **kw: h.store.set_meta(f"romance_carry:{g}:1", json.dumps(
        {"stage": "whispers", "days": .9} | kw))
    carry(101, ts=h.T - 13 * HOUR)  # from an earlier change to the same stage (a reseed or reset)
    carry(102)  # no ts at all: the old shape
    carry(103, ts=h.T - 12 * HOUR)
    last = nights(h, [101, 102, 103], 2)[-1][1]
    assert last == {101: "whispers", 102: "whispers", 103: "late_nights"}


def test_a_carry_funds_no_step_on_a_span_he_did_not_play(h):
    arc_woman(h, 101, "lust", style="bold", stage="none")  # notices wait 0.56, a carry is 0.9
    h.store.add_rel_delta(101, 1, "romance_stage", 1, "x->none", [], h.T - 12 * HOUR,
                          applied=True)
    h.store.set_meta("romance_carry:101:1", json.dumps({"stage": "none", "days": .9,
                                                        "ts": h.T - 12 * HOUR}))
    h.mem(101, "Testy stayed up talking with me.", t=h.T - 6 * HOUR, actors=(1,), valence=.8)
    log = nights(h, [101], 2, played=lambda d: False)
    assert [stages[101] for _, stages in log] == ["none", "none"]
    assert nights(h, [101], 1, first=2, played=lambda d: True)[-1][1] == {101: "notices"}


def test_end_romance_is_history_and_closes_her_away(h):
    from leeroy_mind.arc.state import Arc, end_romance, get_arc, put_arc
    arc_woman(h, 101, "drawn", stage="planning")
    put_arc(h.store, Arc(101, 1, plan={"kind": "event"}, plan_state="set", updated=1.0))
    h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (101,'x',1,9)")
    h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at, returned_at) "
                         "VALUES (102,'x',1,9,5)")
    assert end_romance(h.store, 101, 1, by="he_ended", now=h.T) is True
    assert h.store.get_relationship(101, 1).romance_stage == "ex"
    arc = get_arc(h.store, 101, 1)
    assert (arc.ended, arc.plan, arc.plan_state, arc.updated) == ("he_ended", {}, "none", h.T)
    row = h.store.conn.execute(
        "SELECT reason, delta FROM rel_delta WHERE a=101 AND dim='romance_stage'").fetchone()
    assert (row[0], row[1]) == ("planning->ex", -1)
    assert [tuple(r) for r in h.store.conn.execute(
        "SELECT guid, returned_at FROM away ORDER BY guid")] == [(101, h.T), (102, 5)]
    assert end_romance(h.store, 101, 1, by="owner", now=h.T + 1) is False
    assert get_arc(h.store, 101, 1).ended == "he_ended"
    assert end_romance(h.store, 102, 1, by="found_out", now=h.T) is True  # no arc row yet
    assert get_arc(h.store, 102, 1).ended == "found_out"
