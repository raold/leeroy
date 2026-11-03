"""The night's relationship changes (P1c Task 9): the cap every stage shares per pair, dimension and
night, the day's pending nudges, co-presence on raid fights and battlegrounds, slow regression by
the weeks that really passed, and the notes and pins live talk shows, written from open memories
only (controller ruling N4). Fakes only: a FakeLLM answers for the reflection model. Every name and
line here is invented."""
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.nightly.relations import (NOTES_SCHEMA, Copresence, apply_capped, capped,
                                           copresence, regress, rewrite_notes, stage_notes,
                                           stage_nudges, stage_regress, stage_tier0)
from nightly.conftest import no_real_name, yes

DAY = 86400.0


def apply(h, night, a, b, dim, d):
    with h.store.transaction():
        return apply_capped(h.store, night=night, a=a, b=b, dim=dim, delta=d, speed=10, reason="t",
                            evidence=["memory:1"], ts=0.0, human=1)


def chats(ctx):
    """The ctx's chat calls (the embedder's calls have no messages)."""
    return [c for c in ctx.reasoner.llm.calls if "messages" in c]


def notes_call(ctx):
    (call,) = [c for c in chats(ctx) if c["format"] == NOTES_SCHEMA]
    return call["messages"][0]["content"], call["messages"][-1]["content"]


def deltas(h, cols="a, b, dim, delta, reason, applied"):
    return [tuple(r) for r in h.store.conn.execute(f"SELECT {cols} FROM rel_delta ORDER BY id")]


# -- the plan's tests ----------------------------------------------------------------------------

def test_cap_limits_net_change_per_night(h):
    assert [apply(h, "n1", 101, 102, "affinity", d) for d in (8, 5, -5)] == [8, 2, -5]
    assert apply(h, "n2", 101, 102, "affinity", 9) == 9


def test_guards_romance_and_human(h):
    assert apply(h, "n1", 1, 101, "affinity", 5) == 0          # the human is never simulated
    assert apply(h, "n1", 101, 102, "attraction", 3) == 0      # members never toward members
    assert apply(h, "n1", 101, 1, "attraction", 3) == 0        # attraction moves only by the curve (Task 12)
    assert apply(h, "n1", 101, 1, "trust", 3) == 3
    assert h.count("rel_delta") == 1


def test_pending_nudges_applied_under_cap(h):
    h.store.add_rel_delta(101, 1, "attraction", 2, "chat extraction", ["chat:6"], h.T, applied=False)
    for d in (3, 3, 3, 3):
        h.store.add_rel_delta(101, 102, "affinity", d, "joke", ["chat:5"], h.T, applied=False)
    run(stage_nudges(h.ctx()))
    assert h.store.get_relationship(101, 102).affinity == 10
    assert [r["dim"] for r in h.store.pending_rel_deltas()] == ["attraction"]           # left for Task 12's curve
    assert tuple(h.store.conn.execute("SELECT delta, reason FROM rel_delta ORDER BY id DESC LIMIT 1").fetchone()) == (1, "joke (capped from 3)")


def test_tier0_from_raid_boss_and_bg_events(h):
    for i, state in enumerate(("wiped", "wiped", "killed")):
        h.boss(h.T + i, 631, 11, state, (101, 102, 103, 999))          # ICC, Sindragosa
    h.boss(h.T + 5, 574, 0, "killed", (101, 105))                     # a 5-man boss: ignored
    h.bg(h.T + 9, (101, 104))
    run(stage_tier0(h.ctx()))
    g = h.store.get_relationship
    assert (g(101, 102).familiarity, g(102, 101).respect, g(101, 104).familiarity, g(101, 105).familiarity) == (2, 1, 1, 0)
    assert h.count("rel_delta") == 14      # 3 pairs x 2 directions x (familiarity, respect) + 2 for the bg pair; none for 999


def test_regress_by_elapsed_weeks(h):
    old = h.clock.now() - 21 * 86400
    h.rel(101, 102, affinity=50, familiarity=30, last_seen=old); h.rel(101, 103, affinity=-40, familiarity=5, last_seen=old)
    h.rel(1, 101, affinity=50); h.rel(104, 1, attraction=60, romance_stage="banter", last_seen=old)
    regress(h.store, weeks=2, now=h.clock.now(), human=1)
    r2, r3 = h.store.get_relationship(101, 102), h.store.get_relationship(101, 103)
    assert (r2.affinity, r2.familiarity) == (pytest.approx(49.005), 28)
    assert (r3.affinity, r3.familiarity) == (pytest.approx(-39.204), 5) and h.store.get_relationship(1, 101).affinity == 50
    r4 = h.store.get_relationship(104, 1)
    assert (r4.attraction, r4.romance_stage) == (60, "banter")


def test_notes_only_from_evidence_and_pins_always(h):
    ids = [h.mem(101, f"Belra out-parsed me, round {i}.", importance=i, actors=(102,)) for i in range(1, 8)]
    ctx = h.ctx({"notes": [{"label": "friendly rival", "notes": "Belra keeps out-parsing me.", "evidence": [1]},
                           {"label": "x", "notes": "Coros is rude.", "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 101, 102))
    r = h.store.get_relationship(101, 102)
    assert (r.label, r.notes) == ("friendly rival", "Belra keeps out-parsing me.") and list(r.pinned) == ids[:1:-1]
    run(rewrite_notes(h.ctx({"notes": [{"label": "x", "notes": "Coros is rude.", "evidence": [1]}]}, night="2026-10-30"), 101, 102))
    assert h.store.get_relationship(101, 102).label == "friendly rival"          # a rejected rewrite changes nothing


def test_notes_leave_private_memories_out(h):                   # controller ruling N4
    # 101 holds an open memory about Belra and a private one about her (scope private, scope_with 1) -> the notes prompt
    # shows only the open one, and pinned never holds the private one's id
    shared = h.mem(101, "Belra carried the heroic run.", importance=3, actors=(102,))
    told = h.mem(101, "Testy told me Belra is planning a surprise for the guild.", importance=9, actors=(1, 102),
                 scope="private", scope_with=1, t=h.T + 60)
    ctx = h.ctx({"notes": [{"label": "friend", "notes": "Belra carried us.", "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 101, 102))
    shown = "\n".join(m["content"] for c in chats(ctx) for m in c["messages"])   # the notes call and the judge's
    assert "Belra carried the heroic run." in shown and "surprise" not in shown
    r = h.store.get_relationship(101, 102)
    assert r.notes == "Belra carried us." and list(r.pinned) == [shared] and told not in r.pinned


# -- beyond the plan: the cap ----------------------------------------------------------------------

def test_the_cap_commits_with_the_unit_and_is_shared(h):
    h.rel(101, 102, trust=50)
    with pytest.raises(RuntimeError):
        with h.store.transaction():                                  # a unit that fails after applying
            assert apply_capped(h.store, night="n1", a=101, b=102, dim="trust", delta=6, speed=10, reason="t",
                                evidence=["memory:1"], ts=0.0, human=1) == 6
            raise RuntimeError("the unit failed")
    assert (h.count("rel_cap"), h.count("rel_delta"), h.store.get_relationship(101, 102).trust) == (0, 0, 50)
    with h.store.transaction():
        assert [capped(h.store, "n1", 101, 102, "trust", 7, 10) for _ in range(3)] == [7, 3, 0]
    assert h.store.get_relationship(101, 102).trust == 50 and h.count("rel_delta") == 0   # capped alone applies nothing
    assert apply(h, "n1", 101, 102, "trust", 2) == 0                 # but what it took counts
    assert [apply(h, "n1", 101, 102, "trust", d) for d in (-15, -10)] == [-15, -5]   # a net change of 10 at most, either way
    assert apply(h, "n1", 101, 102, "affinity", 2) == 2 and apply(h, "n1", 102, 101, "trust", 2) == 2   # its own per dim, per way
    assert [tuple(r) for r in h.store.conn.execute("SELECT a, b, dim, used FROM rel_cap ORDER BY a, dim")] == [
        (101, 102, "affinity", 2), (101, 102, "trust", -10), (102, 101, "trust", 2)]
    assert h.store.get_relationship(101, 102).trust == 30


def test_a_lowered_speed_never_turns_a_change_around(h):
    assert apply(h, "n1", 101, 102, "affinity", 8) == 8             # at speed 10
    with h.store.transaction():                                      # the owner lowers it to 5 mid-night
        up, down = (apply_capped(h.store, night="n1", a=101, b=102, dim="affinity", delta=d, speed=5, reason="t",
                                 evidence=["memory:1"], ts=0.0, human=1) for d in (2, -2))
    assert (up, down) == (0, -2) and h.store.get_relationship(101, 102).affinity == 6


def test_a_score_at_its_limit_takes_only_what_fits(h):
    h.rel(101, 102, trust=99, affinity=-100)
    assert apply(h, "n1", 101, 102, "trust", 3) == 1
    assert apply(h, "n1", 101, 102, "trust", 3) == 0                 # at 100: nothing applied, no row, no cap used
    assert apply(h, "n1", 101, 102, "affinity", -4) == 0
    assert deltas(h, "dim, delta") == [("trust", 1)] and h.store.get_relationship(101, 102).trust == 100
    assert [tuple(r) for r in h.store.conn.execute("SELECT dim, used FROM rel_cap")] == [("trust", 1)]


# -- beyond the plan: nudges -----------------------------------------------------------------------

def test_nudges_guards_reasons_and_the_shared_cap(h):
    assert apply(h, h.night, 103, 104, "familiarity", 10) == 10      # the cap already used tonight
    for a, b, dim, d, reason in ((1, 101, "affinity", 2, "his own view"), (101, 101, "trust", 2, "to myself"),
                                 (101, 102, "romance_stage", 1, "a stage"),
                                 (102, 1, "trust", 3, "disclosure heard"),          # Task 8's pending trust
                                 (101, 102, "affinity", -3, "rude"), (103, 104, "familiarity", 2, "chat extraction"),
                                 (104, 102, "attraction", 2, "chat extraction")):
        h.store.add_rel_delta(a, b, dim, d, reason, ["chat:5"], h.T, applied=False)
    run(stage_nudges(h.ctx()))
    assert deltas(h)[1:] == [
        (1, 101, "affinity", 0, "his own view (not applied: from the human)", 1),
        (101, 101, "trust", 0, "to myself (not applied: toward oneself)", 1),
        (101, 102, "romance_stage", 0, "a stage (not applied: romance_stage is not a night dimension)", 1),
        (102, 1, "trust", 3, "disclosure heard", 1),
        (101, 102, "affinity", -3, "rude", 1),
        (103, 104, "familiarity", 0, "chat extraction (not applied: the night's cap is used up)", 1),
        (104, 102, "attraction", 2, "chat extraction", 0)]            # every attraction row waits for Task 12
    g = h.store.get_relationship
    assert (g(102, 1).trust, g(101, 102).affinity, g(1, 101).affinity, g(103, 104).familiarity) == (3, -3, 0, 10)
    assert apply(h, h.night, 101, 102, "affinity", -10) == -7        # the nudge's -3 counts toward tonight's cap
    again = h.ctx()
    run(stage_nudges(again))                                         # once a night: done, so not even tried
    assert len(deltas(h)) == 9 and g(101, 102).affinity == -10 and again.failures == {}


def test_nudges_tell_a_cut_from_the_limit(h):
    h.rel(101, 102, trust=98)
    h.store.add_rel_delta(101, 102, "trust", 3, "praise", ["chat:5"], h.T, applied=False)
    h.store.add_rel_delta(101, 102, "trust", 2, "more praise", ["chat:6"], h.T, applied=False)
    h.store.add_rel_delta(101, 103, "affinity", 0, "nothing", ["chat:7"], h.T, applied=False)
    run(stage_nudges(h.ctx()))
    assert deltas(h, "delta, reason") == [(2, "praise (capped from 3)"),
                                          (0, "more praise (not applied: already at its limit)"),
                                          (0, "nothing (not applied: no change)")]


# -- beyond the plan: co-presence ------------------------------------------------------------------

def test_copresence_reads_only_the_windows_raid_fights_and_battlegrounds(h):
    w = h.ctx().window
    first = h.bg(w[0], (101, 102))                                   # the window starts at 06:00 sharp
    kill = h.boss(h.T, 631, 11, "killed", (101, 102, 1))             # he is not in the cast
    h.boss(h.T + 1, 631, 11, "engaged", (101, 102))                  # a pull starting is not a fight's end
    h.boss(h.T + 2, 631, 13, "killed", (101, 102))                   # the gauntlet: not a fight
    h.boss(w[0] - 1, 631, 11, "wiped", (101, 102))                   # the night before
    h.boss(w[1], 631, 11, "wiped", (101, 102))                       # the next night's
    bgs = [first] + [h.bg(h.T + 10 * (i + 1), (102, 101, 101)) for i in range(3)]
    assert copresence(h.store, w, {101, 102, 103}) == {
        (101, 102): Copresence(boss_events=1, kills=1, bgs=4, refs=[f"event:{e}" for e in (first, kill, *bgs[1:])])}
    assert copresence(h.store, w, {101, 103}) == {}
    run(stage_tier0(h.ctx()))                                        # a fight and battlegrounds: one familiarity row
    cited = '["' + '", "'.join(f"event:{e}" for e in (first, kill, bgs[1])) + '"]'
    assert deltas(h, "a, b, dim, delta, evidence") == [(101, 102, "familiarity", 2, cited), (101, 102, "respect", 1, cited),
                                                       (102, 101, "familiarity", 2, cited), (102, 101, "respect", 1, cited)]


def test_tier0_credits_away_members_and_cites_the_first_three_events(h):
    with h.store.transaction():                                      # 104 is on a break in the offline world
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (104, 'break', ?, ?)",
                             (h.T - DAY, h.T + DAY))
    events = [h.boss(h.T + i, 631, 11, "wiped", (101, 104, 1)) for i in range(4)]
    two = [h.boss(h.T + 10 + i, 631, 12, "wiped", (102, 103)) for i in range(2)]   # the second +1 needs a third fight
    run(stage_tier0(h.ctx()))
    rows = deltas(h, "a, b, dim, delta, reason, evidence, applied")
    cited = '["' + '", "'.join(f"event:{e}" for e in events[:3]) + '"]'
    pair = '["' + '", "'.join(f"event:{e}" for e in two) + '"]'
    assert rows == [(101, 104, "familiarity", 2, "copresence", cited, 1), (104, 101, "familiarity", 2, "copresence", cited, 1),
                    (102, 103, "familiarity", 1, "copresence", pair, 1), (103, 102, "familiarity", 1, "copresence", pair, 1)]
    assert h.store.get_relationship(101, 1).familiarity == 0         # the human is not in the cast
    again = h.ctx()
    run(stage_tier0(again))                                          # once a night: done, so not even tried
    run(stage_tier0(h.ctx(night="2026-10-30")))                      # the next night's window has no event
    assert len(deltas(h)) == 4 and again.failures == {}


# -- beyond the plan: regression -------------------------------------------------------------------

def test_regression_logs_what_it_moves(h):
    now = h.clock.now()
    h.rel(101, 102, affinity=0.5, familiarity=11, last_seen=0)       # affinity's -0.005 is too small; familiarity to 10
    h.rel(101, 103, affinity=30, familiarity=40, last_seen=now)      # seen this week: familiarity stays
    h.rel(102, 1, affinity=20, attraction=50, romance_stage="whispers", last_seen=0)   # toward him: never attraction
    assert regress(h.store, weeks=1, now=now, human=1) == 3
    assert deltas(h) == [(101, 102, "familiarity", -1, "regression", 1), (101, 103, "affinity", pytest.approx(-0.3), "regression", 1),
                         (102, 1, "affinity", pytest.approx(-0.2), "regression", 1)]
    assert {r[0] for r in h.store.conn.execute("SELECT evidence FROM rel_delta")} == {'["rule:regression"]'}
    g = h.store.get_relationship
    assert (g(101, 102).affinity, g(101, 102).familiarity, g(101, 103).familiarity) == (0.5, 10, 40)
    assert (g(102, 1).attraction, g(102, 1).romance_stage, g(102, 1).familiarity) == (50, "whispers", 0)


def test_regression_rounds_the_weeks_for_familiarity(h):
    now = h.clock.now()
    h.rel(101, 102, affinity=50, familiarity=40, last_seen=0)
    regress(h.store, weeks=0.4, now=now, human=1)                    # under half a week: affinity only
    assert (h.store.get_relationship(101, 102).affinity, h.store.get_relationship(101, 102).familiarity) == (
        pytest.approx(50 * 0.99 ** 0.4), 40)
    regress(h.store, weeks=1.6, now=now, human=1)
    assert h.store.get_relationship(101, 102).familiarity == 38


def test_regression_runs_once_a_week_for_the_weeks_since_the_last(h):
    h.rel(101, 102, familiarity=50, last_seen=0)
    h.rel(101, 103, affinity=100)

    def week(night):
        run(stage_regress(h.ctx(night=night)))
        return h.store.get_relationship(101, 102).familiarity
    assert week("2026-10-29") == 49                                 # the first time: one week
    h.clock.advance(DAY)
    assert week("2026-10-30") == 49                                 # 2026-W44 is done, on whichever night
    h.clock.advance(6 * DAY)
    assert week("2026-11-05") == 48                                 # 2026-W45, a week on
    h.clock.advance(21 * DAY)
    assert week("2026-11-26") == 45                                 # three weeks off count three
    h.clock.advance(20 * 7 * DAY)
    assert week("2027-04-15") == 37                                 # twenty weeks off: capped at 8
    assert h.store.get_relationship(101, 103).affinity == pytest.approx(100 * 0.99 ** 13)
    units = [r[0] for r in h.store.conn.execute("SELECT unit FROM night_step WHERE stage = 'regress' ORDER BY finished")]
    assert units == ["2026-W44", "2026-W45", "2026-W48", "2027-W15"]


# -- beyond the plan: notes ------------------------------------------------------------------------

def test_the_notes_schema_holds_the_plans_values():
    props = NOTES_SCHEMA["properties"]
    assert NOTES_SCHEMA["title"] == "notes" and NOTES_SCHEMA["required"] == ["label", "notes", "evidence"]
    assert (props["label"]["maxLength"], props["notes"]["maxLength"]) == (40, 400)
    check_schema(NOTES_SCHEMA, {"label": "x" * 40, "notes": "y" * 400, "evidence": [1, 2]})
    for bad in ({"label": "x" * 41, "notes": "", "evidence": []}, {"label": "", "notes": "y" * 401, "evidence": []},
                {"label": "", "notes": "", "evidence": ["1"]}, {"label": "", "notes": ""}):
        with pytest.raises(ValueError):
            check_schema(NOTES_SCHEMA, bad)


def test_notes_read_the_twelve_newest_open_memories(h):
    opened = [h.mem(101, f"Belra and I ran heroics, night {i}.", importance=9 if i == 0 else 5, actors=(102,),
                    t=h.T + i) for i in range(14)]                   # the oldest weighs most
    for i in range(12):                                              # newer and weightier, but whispered
        h.mem(101, f"Testy whispered me about Belra, part {i}.", importance=10, actors=(1, 102),
              scope="private", scope_with=1, t=h.T + 100 + i)
    h.rel(101, 102, label="rival", notes="Belra parses well.")
    ctx = h.ctx({"notes": [{"label": "friend", "notes": "Belra runs heroics with me.", "evidence": [1, 12]}],
                 "judge": [yes]})
    run(rewrite_notes(ctx, 101, 102))
    system, user = notes_call(ctx)
    assert [i for i in range(14) if f"night {i}." in user] == list(range(2, 14)) and "whispered" not in user
    assert user.index("night 13.") < user.index("night 2.")         # newest first
    assert 'label "rival", notes "Belra parses well."' in user     # the old notes are shown
    assert system.startswith("You keep Arkon's notes on Belra")
    r = h.store.get_relationship(101, 102)
    assert (r.label, r.notes) == ("friend", "Belra runs heroics with me.")
    assert list(r.pinned) == [opened[0], *opened[:-5:-1]]           # the weightiest first, from all her open ones


def test_notes_on_him_name_him_and_carry_her_attitude(h):
    h.woman(102, "drawn")
    h.persona(103, stance="drawn")                                   # a man's stance is never an attitude
    for g in (102, 103):
        h.mem(g, "Testy helped me finish a quest.", actors=(1,))
    h.rel(102, 1, notes="Quillon is new here.")                      # an old note that names him by his real name

    def asked(g):
        ctx = h.ctx({"notes": [{"label": "", "notes": "Testy always helps with quests.", "evidence": [1]}],
                     "judge": [yes]})
        run(rewrite_notes(ctx, g, 1))
        return notes_call(ctx)
    system, user = asked(102)
    assert system.startswith("You keep Belra's notes on Testy") and "Belra is clearly into Testy." in user
    assert "notes \"Testy is new here.\"" in user and "Quillon" not in system + user
    _, user = asked(103)
    assert "Coros" in user and "into Testy" not in user
    assert h.store.get_relationship(102, 1).notes == "Testy always helps with quests." and no_real_name(h.store) == []
    h.mem(102, "Coros healed me through the last pull.", actors=(103,))
    ctx = h.ctx({"notes": [{"label": "friend", "notes": "Coros keeps me alive.", "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 102, 103))                                # her notes on a member carry no attitude
    assert "clearly into" not in notes_call(ctx)[1] and h.store.get_relationship(102, 103).notes == "Coros keeps me alive."


def test_notes_read_tonights_attitudes(h):
    h.woman(102, "fond")
    h.mem(102, "Testy helped me finish a quest.", actors=(1,))
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO rel_cap(night, a, b, dim, used) VALUES (?, 102, 1, 'trust', 3)", (h.night,))
    ctx = h.ctx({"notes": [{"label": "", "notes": "Testy always helps with quests.", "evidence": [1]}], "judge": [yes]})
    assert ctx.attitudes() == {102: "fond"}                          # read once and kept for the night...
    h.woman(102, "lust")                                             # ...but tonight's attraction stage moved her band
    run(stage_notes(ctx))
    _, user = notes_call(ctx)
    assert "Belra openly wants Testy." in user and "warm toward" not in user


def test_the_label_is_screened_like_the_notes(h):
    h.mem(101, "Belra out-parsed me again.", actors=(102,))
    h.rel(101, 102, label="rival", notes="Belra parses well.")
    for night, label in (("2026-10-30", "Quillon's rival"), ("2026-10-31", "Coros's sidekick")):
        ctx = h.ctx({"notes": [{"label": label, "notes": "Belra keeps out-parsing me.", "evidence": [1]}]}, night=night)
        run(rewrite_notes(ctx, 101, 102))                            # no judge call: the label fails by rule
        assert ctx.done("notes", "101:102") and len(chats(ctx)) == 1
    no = {"verdicts": [{"i": 0, "support": "no", "invariants_ok": True, "why": "not what it says"}]}
    run(rewrite_notes(h.ctx({"notes": [{"label": "friend", "notes": "Belra keeps out-parsing me.", "evidence": [1]}],
                             "judge": [no]}, night="2026-11-01"), 101, 102))   # the rules pass, the judge does not
    r = h.store.get_relationship(101, 102)
    assert (r.label, r.notes) == ("rival", "Belra parses well.") and no_real_name(h.store) == []
    log = h.store.conn.execute("SELECT kind, text, verdict, reason FROM grounding_log ORDER BY id").fetchall()
    assert [tuple(x) for x in log] == [("notes", "Belra keeps out-parsing me.", "rejected", "label: real name"),
                                       ("notes", "Belra keeps out-parsing me.", "rejected",
                                        "label: name not in evidence: coros"),
                                       ("notes", "Belra keeps out-parsing me.", "rejected", "unsupported: not what it says")]


def test_notes_without_open_memories_ask_nothing(h):
    h.rel(101, 103, notes="Coros is quiet.", pinned=(77,))
    h.mem(101, "Testy told me Coros is moving house.", actors=(1, 103), scope="private", scope_with=1)
    ctx = h.ctx()
    run(rewrite_notes(ctx, 101, 103))
    r = h.store.get_relationship(101, 103)
    assert chats(ctx) == [] and (r.notes, r.pinned) == ("Coros is quiet.", ()) and ctx.done("notes", "101:103")
    for g in (1, 101):
        h.mem(g, "Arkon held aggro all night.", actors=(101,))       # open, but held by him or about oneself
    run(rewrite_notes(ctx, 1, 101))                                  # the human is never simulated
    run(rewrite_notes(ctx, 101, 101))
    assert chats(ctx) == [] and ctx.done("notes", "1:101") and h.store.get_relationship(1, 101).pinned == ()


def test_notes_go_to_the_pairs_that_moved_most(h):
    used = {(101, 102): {"affinity": 3.0}, (103, 104): {"trust": -1.0, "respect": 1.5}, (102, 1): {"trust": 2.0},
            (105, 101): {"familiarity": 1.0}, (1, 101): {"affinity": 5.0}, (101, 999): {"affinity": 4.0},
            (999, 101): {"affinity": 4.0}}
    with h.store.transaction():
        for (a, b), dims in used.items():
            for dim, u in dims.items():
                h.store.conn.execute("INSERT INTO rel_cap(night, a, b, dim, used) VALUES (?, ?, ?, ?, ?)",
                                     (h.night, a, b, dim, u))
        h.store.conn.execute("INSERT INTO rel_cap(night, a, b, dim, used) VALUES ('2026-10-28', 104, 105, 'trust', 9)")

    def units():
        return sorted(r[0] for r in h.store.conn.execute("SELECT unit FROM night_step WHERE stage = 'notes'"))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, notes_per_night=2)
    run(stage_notes(ctx))
    assert units() == ["101:102", "103:104"] and chats(ctx) == []    # the two that moved most; nothing to write from
    run(stage_notes(h.ctx()))
    assert units() == ["101:102", "102:1", "103:104"]               # never from him, never outside the cast


# -- fix round 1 (review-task9): no romance between members, and the notes stage priced right ---

MEMBER_ROMANCE = ("I have a crush on her.", "girlfriend", "my secret lover")   # they name no member


@pytest.mark.parametrize("words", MEMBER_ROMANCE)
def test_notes_and_labels_on_a_member_hold_no_romance(h, words):   # I1: P1b's relationship-note rule
    h.mem(101, "Belra and I ran heroics all night.", actors=(102,))
    h.rel(101, 102, label="rival", notes="Belra parses well.")
    for night, label, notes in (("2026-10-30", "friend", words), ("2026-10-31", words, "Belra runs heroics with me.")):
        ctx = h.ctx({"notes": [{"label": label, "notes": notes, "evidence": [1]}]}, night=night)
        run(rewrite_notes(ctx, 101, 102))                            # refused by rule: the judge is never asked
        assert len(chats(ctx)) == 1 and ctx.done("notes", "101:102")
    r = h.store.get_relationship(101, 102)
    assert (r.label, r.notes) == ("rival", "Belra parses well.")
    log = h.store.conn.execute("SELECT kind, text, verdict, reason FROM grounding_log ORDER BY id").fetchall()
    assert [tuple(x) for x in log] == [("notes", words, "rejected", "romance between members"),
                                       ("notes", "Belra runs heroics with me.", "rejected", "label: romance between members")]


@pytest.mark.parametrize("words", MEMBER_ROMANCE)
def test_the_same_words_about_him_are_hers_to_keep(h, words):     # romance aimed at him is allowed
    h.woman(102, "drawn")
    h.mem(102, "Testy and I ran heroics all night.", actors=(1,))
    ctx = h.ctx({"notes": [{"label": words, "notes": words, "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 102, 1))
    r = h.store.get_relationship(102, 1)
    assert (r.label, r.notes) == (words, words)


@pytest.mark.parametrize("notes", ["Belra always has my back in raids.", "I get jealous when she flirts with Testy."])
def test_a_friendly_note_and_rivalry_over_him_pass(h, notes):      # rivalry over him names him: allowed
    h.mem(101, "Belra and I ran heroics all night.", actors=(102,))
    ctx = h.ctx({"notes": [{"label": "friend", "notes": notes, "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 101, 102))
    r = h.store.get_relationship(101, 102)
    assert (r.label, r.notes) == ("friend", notes)


def test_the_budget_prices_a_notes_rewrite_at_the_calls_it_makes(h):   # I2
    h.mem(101, "Belra out-parsed me again.", actors=(102,))
    ctx = h.ctx({"notes": [{"label": "rival", "notes": "Belra keeps out-parsing me.", "evidence": [1]}],
                 "judge": [yes]})
    run(rewrite_notes(ctx, 101, 102))
    assert h.store.get_relationship(101, 102).notes == "Belra keeps out-parsing me."   # a rewrite that passes...
    calls = chats(ctx)
    assert [c["format"]["title"] for c in calls] == ["notes", "judge"] and not any(c["think"] for c in calls)
    assert budget.UNIT_CALLS["notes"] == {"json": len(calls)}       # ...is priced at its two JSON calls
    assert budget.estimate({"notes": 1}, measured={}) == {"notes": len(calls) * budget.PLAN_S["json"]}


# -- fix round 2 (review-task9 Minor 3): romance toward him only from a woman with an attitude ---

TOWARD_HIM = ("I have a crush on him.", "Testy is basically my boyfriend.", "I flirt with Testy every raid.")


@pytest.mark.parametrize("words", TOWARD_HIM)
def test_romance_toward_him_needs_her_attitude(h, words):
    h.persona(103, stance="fond")                                    # a man: never an attitude, whatever his stance
    h.woman(104, "none")
    h.woman(102, "drawn")
    for g in (102, 103, 104):
        h.mem(g, "Testy and I ran heroics all night.", actors=(1,))
    for g in (103, 104):
        for night, label, notes in (("2026-10-30", "friend", words), ("2026-10-31", words, "Testy runs a good raid.")):
            ctx = h.ctx({"notes": [{"label": label, "notes": notes, "evidence": [1]}]}, night=night)
            run(rewrite_notes(ctx, g, 1))                            # refused by rule: the judge is never asked
            assert len(chats(ctx)) == 1 and ctx.done("notes", f"{g}:1")
        assert (h.store.get_relationship(g, 1).label, h.store.get_relationship(g, 1).notes) == ("", "")
    log = h.store.conn.execute("SELECT owner, text, verdict, reason FROM grounding_log ORDER BY id").fetchall()
    assert [tuple(x) for x in log] == [
        (g, text, "rejected", reason) for g in (103, 104)
        for text, reason in ((words, "romance without an attitude"),
                             ("Testy runs a good raid.", "label: romance without an attitude"))]
    ctx = h.ctx({"notes": [{"label": words, "notes": words, "evidence": [1]}], "judge": [yes]})
    run(rewrite_notes(ctx, 102, 1))                                  # hers to write: she is drawn to him
    assert (h.store.get_relationship(102, 1).label, h.store.get_relationship(102, 1).notes) == (words, words)


def test_a_mans_friendly_note_on_him_passes(h):
    h.persona(103, stance="fond")                                    # the men's stances: fond, and none
    for g, notes in ((103, "Testy runs a tight raid and I respect that."), (105, "Testy always has time for a heroic.")):
        h.mem(g, "Testy and I ran heroics all night.", actors=(1,))
        ctx = h.ctx({"notes": [{"label": "friend", "notes": notes, "evidence": [1]}], "judge": [yes]})
        run(rewrite_notes(ctx, g, 1))
        assert (h.store.get_relationship(g, 1).label, h.store.get_relationship(g, 1).notes) == ("friend", notes)
