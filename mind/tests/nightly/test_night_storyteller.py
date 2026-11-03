from dataclasses import replace
from datetime import date, timedelta

import pytest

from conftest import make_persona
from leeroy_mind.nightly.storyteller import (
    BLOWUP_DAYS, BLOWUP_DELTA, INTENT, LAYER_TRUST, NIGHT_OWL_DROP, RESOLVE_DAYS,
    VOLITIONS, Volition, arc_key, arc_tension, choose, reset_want, volitions,
)
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member


def feud(h, a, b, days, *, ended=None, arc=True):
    a, b = sorted((a, b))
    started = h.T - days * 86400
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO feud(a,b,started,ended,cause) VALUES (?,?,?,?,?)",
                             (a, b, started, ended, "a disagreement"))
        if arc:
            h.store.conn.execute(
                "INSERT INTO story_arc(kind,key,opened,last_beat,status) VALUES ('feud',?,?,?,?)",
                (arc_key("feud", a, b), started, started, "open" if ended is None else "resolved"))


def want(h, guid, *names, deficit=1.2):
    with h.store.transaction():
        for name in names:
            h.store.conn.execute("INSERT OR REPLACE INTO motive VALUES (?,?,0.5,?,?)",
                                 (guid, name, deficit, h.T - 10))


def ext(h, guid, value):
    with h.store.transaction():
        h.store.conn.execute("INSERT OR REPLACE INTO trait_state VALUES (?,?,'extraversion',?)",
                             (h.night, guid, value))


def rumor(h, *, root="event:999", about=103, status="live", op="sharpen"):
    with h.store.transaction():
        rid = h.store.conn.execute(
            "INSERT INTO rumor(root,about,canonical,created,last_told,status) VALUES (?,?,?,?,?,?)",
            (root, about, "Coros missed the pull.", h.T, h.T, status)).lastrowid
        version = h.store.conn.execute(
            "INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts) VALUES (?,101,102,?,'A telling',?)",
            (rid, op, h.T)).lastrowid
        h.store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,updated) VALUES (?,101,'spreader',?)",
                             (rid, h.T))
        h.store.conn.execute(
            "INSERT INTO rumor_state(rumor,guid,state,version,updated) VALUES (?,102,'stifler',?,?)",
            (rid, version, h.T))
        for guid in (101, 102, 104):
            h.store.conn.execute("INSERT INTO knows(root,guid,how,learned) VALUES (?,?,'told',?)",
                                 (root, guid, h.T))
    return rid


def blowup(h, nights):
    night = (date.fromisoformat(h.night) - timedelta(days=nights)).isoformat()
    with h.store.transaction():
        h.store.conn.execute(
            "INSERT INTO encounter(night,idx,a,b,setting,status,blowup) "
            "VALUES (?,0,103,104,'the bank','done',1)", (night,))


def flatten(result):
    return [v for values in result.values() for v in values]


def test_constants_and_intents_are_the_contract():
    assert VOLITIONS == ("confront", "make_up", "confide", "gossip", "correct", "recruit",
                         "needle", "bond", "mentor", "tease")
    assert LAYER_TRUST == {"surface": 20.0, "middle": 45.0, "deep": 65.0}
    assert (NIGHT_OWL_DROP, RESOLVE_DAYS, BLOWUP_DAYS, BLOWUP_DELTA) == (10.0, 10, 7, -4)
    assert INTENT == {
        "confront": "{a} wants to have it out with {b} about what's been bugging them.",
        "make_up": "{a} wants to patch things up with {b}.",
        "confide": "{a} wants to tell {b} something personal.",
        "gossip": "{a} has something to pass on to {b}.",
        "correct": "{a} wants to set the record straight with {b}.",
        "recruit": "{a} wants {b} along for a mount run.",
        "needle": "{a} wants to needle {b} about the meters.",
        "bond": "{a} just wants to hang out with {b}.",
        "mentor": "{a} is showing {b} the ropes.",
        "tease": "{a} wants to bring up the running joke with {b}.",
    }


def test_volitions_from_state_and_today_trait_caps(h):
    feud(h, 101, 102, 2)
    feud(h, 103, 104, 12)
    h.persona(105, disclosures=(("surface", "I keep a garden."),
                               ("deep", "I am afraid of letting my family down.")))
    ext(h, 105, 3)
    h.rel(105, 101, trust=25)
    want(h, 102, "competition")
    h.boss(h.T, 631, 0, "killed", [101, 102])
    ext(h, 102, 6)
    first = volitions(h.ctx())
    assert first[101] == [Volition(101, "confront", 102, 1.0)]
    assert first[102] == [Volition(102, "confront", 101, 1.0)]
    assert first[103][0].kind == "make_up"
    assert first[103][0].weight == pytest.approx(3.9)
    assert first[105] == [Volition(105, "confide", 101, 0.9)]
    ext(h, 102, 8)
    second = volitions(h.ctx())
    assert second[102] == [Volition(102, "confront", 101, 1.0), Volition(102, "needle", 101, 0.7)]
    assert h.count("volition") == len(flatten(second))
    assert all(1 not in (v.guid, v.target) for v in flatten(second))


@pytest.mark.parametrize("days,weight", [(4.99, None), (5, 0.6), (9, 1.0), (10, 3.3), (20, 4.5)])
def test_makeup_age_and_resolution_pressure(h, days, weight):
    feud(h, 101, 102, days)
    matches = [v for v in volitions(h.ctx())[101] if v.kind == "make_up"]
    if weight is None:
        assert matches == []
    else:
        assert matches[0].weight == pytest.approx(weight)


@pytest.mark.parametrize("age,applied,dim,affinity,expected", [
    (0, True, "affinity", -30, True), (2, True, "trust", -40, True),
    (3, True, "affinity", -40, False), (0, False, "affinity", -40, False),
    (0, True, "affinity", -29, False), (0, True, "romance_stage", -40, False),
])
def test_confront_needs_recent_landed_nonromantic_delta(h, age, applied, dim, affinity, expected):
    h.rel(101, 102, affinity=affinity)
    h.store.add_rel_delta(101, 102, dim, -1, "test", [], h.T - age * 86400, applied=applied)
    result = volitions(h.ctx())
    assert (Volition(101, "confront", 102, 0.8) in result.get(101, [])) == expected
    assert 102 not in result  # A directed change does not give the other member that intention.


@pytest.mark.parametrize("owl,trust,opened,expected", [
    (False, 19, (), False), (False, 20, (), True), (True, 10, (), True),
    (True, 9, (), False), (False, 44, (0,), False), (False, 45, (0,), True),
    (False, 64, (0, 1), False), (False, 65, (0, 1), True),
    (True, 55, (0, 1), True), (False, 70, (0, 1, 2), False),
])
def test_confide_uses_next_unopened_layer_and_night_owl_trust(h, owl, trust, opened, expected):
    h.persona(101, night_owl=owl,
              disclosures=(("surface", "I keep a garden."), ("middle", "My old job was hard."),
                           ("deep", "I am afraid of letting my family down.")))
    ext(h, 101, 3)
    h.rel(101, 102, trust=trust)
    with h.store.transaction():
        for layer in opened:
            h.store.conn.execute("INSERT INTO layer_open VALUES (101,102,?,?,'encounter:1')",
                                 (layer, h.T - 86400))
    assert (Volition(101, "confide", 102, 0.9) in volitions(h.ctx()).get(101, [])) == expected


def test_rumors_use_live_spreaders_unknown_targets_and_informed_corrections(h):
    h.persona(104, guild_role="officer")
    h.persona(105, guild_role="officer")
    rid = rumor(h)
    result = volitions(h.ctx())
    ref = f"rule:rumor:{rid}"
    assert result[101] == [Volition(101, "gossip", 103, 0.7, ref), Volition(101, "gossip", 105, 0.7, ref)]
    assert result[103] == [Volition(103, "correct", 102, 0.8, ref)]
    assert result[104] == [Volition(104, "correct", 102, 0.8, ref)]
    assert 102 not in result and 105 not in result
    with h.store.transaction():
        h.store.conn.execute("UPDATE rumor SET status='faded'")
    assert volitions(h.ctx()) == {}


@pytest.mark.parametrize("op", ["level", "correct"])
def test_corrections_only_target_a_current_distorted_version(h, op):
    rumor(h, op=op)
    assert not any(v.kind == "correct" for v in flatten(volitions(h.ctx())))


@pytest.mark.parametrize("text", ["got the new job", "keep this secret: got the new job"])
def test_private_and_secret_tellings_never_generate_rumor_volitions(h, text):
    line = h.say(h.T, 1, text, channel="whisper", to=101)
    rumor(h, root=f"chat:{line}")  # Even a malformed legacy rumor row cannot reveal the whisper.
    assert volitions(h.ctx()) == {}


def test_private_ledger_copy_alone_cannot_generate_a_rumor(h):
    line = h.say(h.T, 1, "got the new job", channel="whisper", to=101)
    h.mem(101, "A private update", scope="private", scope_with=1, evidence=[f"chat:{line}"])
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO knows(root,guid,how,learned) VALUES (?,101,'told',?)",
                             (f"chat:{line}", h.T))
    assert volitions(h.ctx()) == {}


@pytest.mark.parametrize("kind,motive", [("recruit", "discovery"), ("bond", "socialising")])
def test_shared_interests_or_pair_groups_extend_beyond_inner_layer(h, kind, motive):
    h.persona(101, outside_raids=["fishing"], raid_role="healer")
    for guid, name in ((106, "Fenn"), (107, "Gelda"), (108, "Herro")):
        h.store.upsert_cast([Member(guid=guid, name=name, race=1, class_=1, gender=0, level=80,
                                   rank=3, online=False, map=0, zone=0, spec_tab=-1)], h.T)
        p = make_persona(guid, name, outside_raids=["fishing"] if guid == 107 else ["dungeons"],
                         raid_role="healer" if guid == 107 else "tank")
        save_persona(h.store, p, source="seed", ts=h.T, race="Human", class_name="Warrior")
    for target in range(102, 107):
        h.rel(101, target, affinity=40)
    want(h, 101, motive)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, volitions_per_member=20)
    result = volitions(ctx)[101]
    assert [v.target for v in result] == list(range(102, 108))
    assert all(v.kind == kind for v in result)


def test_needle_uses_lowest_other_eligible_same_role_raid_participant(h):
    want(h, 103, "competition")
    h.persona(101, raid_role="healer")
    h.boss(h.T, 631, 0, "killed", [103, 105, 104, 101, 1])
    h.boss(h.T, 574, 0, "killed", [102])  # A five-player dungeon is not a raid fight.
    h.boss(h.T - 2 * 86400, 631, 0, "killed", [102])
    assert volitions(h.ctx())[103] == [Volition(103, "needle", 104, 0.7)]


def test_active_mentors_and_joke_adopters_only(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (101,102,?,?)",
                             (h.T - 10, h.T + 10))
        h.store.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (103,104,?,?)",
                             (h.T - 100, h.T))
        lid = h.store.conn.execute(
            "INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used) "
            "VALUES ('joke','bank tax','event:1',104,?,?)", (h.T, h.T)).lastrowid
        h.store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use) VALUES (?,105,?)",
                             (lid, h.T))
        h.store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use,active) VALUES (?,103,?,0)",
                             (lid, h.T))
    assert volitions(h.ctx()) == {101: [Volition(101, "mentor", 102, 0.8)],
                                  105: [Volition(105, "tease", 104, 0.5)]}
    with h.store.transaction():
        h.store.conn.execute("UPDATE lexicon SET status='dormant'")
        h.store.conn.execute("UPDATE mentor SET ended=?", (h.T,))
    assert volitions(h.ctx()) == {}


def test_ties_use_kind_then_target_and_drafts_replace(h):
    want(h, 101, "discovery")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (101,102,?,?)",
                             (h.T - 10, h.T + 10))
    ctx = h.ctx()
    assert volitions(ctx)[101] == [Volition(101, "recruit", 102, 0.8), Volition(101, "recruit", 103, 0.8)]
    assert volitions(ctx) == volitions(ctx)
    assert h.count("volition") == 2
    reset_want(h.store, 101, "recruit", now=h.T)
    assert volitions(ctx)[101] == [Volition(101, "mentor", 102, 0.8)]
    assert h.count("volition") == 1


def test_duplicate_rumor_intentions_keep_one_stable_ref_per_target(h):
    first = rumor(h, root="event:991")
    rumor(h, root="event:992")
    result = volitions(h.ctx())
    assert result[101] == [Volition(101, "gossip", 103, 0.7, f"rule:rumor:{first}"),
                           Volition(101, "gossip", 105, 0.7, f"rule:rumor:{first}")]
    assert h.count("volition") == len(flatten(result))


def test_human_away_and_unknown_members_are_never_actors_or_targets(h):
    feud(h, 1, 101, 2)
    feud(h, 102, 105, 2)
    feud(h, 103, 999, 2)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (105,'a break',0,9e9)")
    want(h, 101, "socialising")
    result = flatten(volitions(h.ctx()))
    assert result and all({v.guid, v.target}.isdisjoint({1, 105, 999}) for v in result)
    invalid = [Volition(a, "confront", b, 100) for a, b in ((1, 101), (101, 1), (105, 102),
               (102, 105), (999, 103), (103, 999), (101, 101))]
    assert choose(h.ctx(), {101: invalid}) == []


@pytest.mark.parametrize("nights,blocked", [(1, True), (5, True), (7, True), (8, False)])
def test_no_two_blowups_a_week(h, nights, blocked):
    blowup(h, nights)
    v = Volition(101, "confront", 102, 1.0)
    assert choose(h.ctx(), {101: [v]}) == ([] if blocked else [v])


def test_quiet_night_after_new_feud_still_allows_makeup(h):
    feud(h, 103, 104, 1)
    confrontation = Volition(101, "confront", 102, 1.0)
    makeup = Volition(103, "make_up", 104, 0.6)
    assert choose(h.ctx(), {101: [confrontation], 103: [makeup]}) == [makeup]


@pytest.mark.parametrize("other_kind", ["make_up", "bond"])
def test_disjoint_conflicts_share_one_blowup_allowance_and_keep_other_scenes(h, other_kind):
    first = Volition(101, "confront", 102, 1.0)
    second = Volition(103, "confront", 104, 0.9)
    other = Volition(103, other_kind, 104, 0.6)
    ctx = h.ctx()
    assert choose(ctx, {103: [second, other], 101: [first]}) == [first, other]
    assert choose(ctx, {101: [first], 103: [other, second]}) == [first, other]
    chosen = [tuple(r) for r in h.store.conn.execute(
        "SELECT guid,kind,target FROM volition WHERE chosen=1 ORDER BY guid")]
    assert chosen == [(101, "confront", 102), (103, other_kind, 104)]


def test_arc_tension_resolved_feuds_and_live_rumors(h):
    feud(h, 102, 101, 2)
    key = arc_key("feud", 102, 101)
    assert key == "feud:101:102"
    assert arc_tension(h.store, key, h.T) == pytest.approx(0.7)
    assert arc_tension(h.store, key, h.T + 30 * 86400) == 1.5
    assert arc_tension(h.store, "feud:103:104", h.T) == 0
    rid = rumor(h)
    assert arc_tension(h.store, arc_key("rumor", rid), h.T) == 0.5
    with h.store.transaction():
        h.store.conn.execute("UPDATE story_arc SET status='resolved'")
        h.store.conn.execute("UPDATE rumor SET status='faded'")
    assert arc_tension(h.store, key, h.T) == 0
    assert arc_tension(h.store, arc_key("rumor", rid), h.T) == 0
    assert arc_tension(h.store, "life:1", h.T) == 0


def test_arcs_prioritize_old_makeup_and_score_rumors(h):
    feud(h, 101, 102, 11)
    feud(h, 103, 104, 0)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, volition_scenes=1)
    chosen = choose(ctx, volitions(ctx))
    assert len(chosen) == 1 and chosen[0].kind == "make_up"
    assert {chosen[0].guid, chosen[0].target} == {101, 102}
    rid = rumor(h)
    g = Volition(103, "gossip", 104, 0.7, f"rule:rumor:{rid}")
    bond = Volition(101, "bond", 102, 1.0)
    assert choose(ctx, {101: [bond], 103: [g]}) == [g]  # .7 * 1.5 beats 1.0.


def test_one_volition_scene_per_member_and_scene_cap_and_flags(h):
    values = [Volition(101, "bond", 102, 0.6), Volition(103, "bond", 101, 0.7),
              Volition(101, "bond", 104, 0.5), Volition(104, "bond", 105, 0.6)]
    ctx = h.ctx()
    chosen = choose(ctx, {101: values})
    assert chosen == [values[1], values[3]]
    assert len({g for v in chosen for g in (v.guid, v.target)}) == 2 * len(chosen)
    ctx.settings = replace(ctx.settings, volition_scenes=1)
    assert choose(ctx, {101: list(reversed(values))}) == [values[1]]
    assert h.store.conn.execute("SELECT sum(chosen) FROM volition").fetchone()[0] == 1
    assert choose(ctx, {}) == []
    assert h.store.conn.execute("SELECT sum(chosen) FROM volition").fetchone()[0] == 0


@pytest.mark.parametrize("kind,names", [
    ("recruit", ("discovery", "customisation")), ("needle", ("competition",)),
    ("bond", ("socialising", "teamwork")), ("confide", ("relationship",)),
])
def test_chosen_want_resets_only_its_existing_deficits(h, kind, names):
    want(h, 101, *names, "advancement")
    want(h, 102, *names)
    before = h.count("motive")
    reset_want(h.store, 101, kind, now=h.T)
    rows = {(r["guid"], r["motive"]): (r["deficit"], r["updated"])
            for r in h.store.conn.execute("SELECT * FROM motive")}
    assert all(rows[101, name] == (0, h.T) for name in names)
    assert all(rows[102, name][0] == 1.2 for name in names)
    assert rows[101, "advancement"][0] == 1.2
    reset_want(h.store, 105, kind, now=h.T)
    reset_want(h.store, 101, "confront", now=h.T)
    assert h.count("motive") == before


def test_plan_commit_preserves_chosen_rows_after_motive_reset(h):
    want(h, 101, "discovery")
    ctx = h.ctx()
    selected = []

    def write(store):
        selected.extend(choose(ctx, volitions(ctx)))
        for v in selected:
            reset_want(store, v.guid, v.kind, now=h.T)

    assert ctx.commit("encounters", "plan", write)
    assert selected == [Volition(101, "recruit", 102, 0.8)]
    before = [tuple(r) for r in h.store.conn.execute("SELECT * FROM volition ORDER BY guid,target")]
    assert volitions(ctx)[101][0] == selected[0]
    assert choose(ctx, {}) == selected
    assert before == [tuple(r) for r in h.store.conn.execute("SELECT * FROM volition ORDER BY guid,target")]


def test_volitions_selection_and_deficit_reset_roll_back_together(h):
    want(h, 101, "discovery")
    ctx = h.ctx()
    with pytest.raises(RuntimeError, match="crash"):
        with h.store.transaction():
            chosen = choose(ctx, volitions(ctx))
            reset_want(h.store, chosen[0].guid, chosen[0].kind, now=h.T)
            raise RuntimeError("crash")
    assert h.count("volition") == 0
    assert h.store.conn.execute("SELECT deficit FROM motive WHERE guid=101").fetchone()[0] == 1.2
