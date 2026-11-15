"""Member drama (2026-11-15): scenes judged for contradiction only, member romances behind
`[drama] member_romance`, his drawing a partner away, jealousy that boils over and cools, and
third-person rumours. Invented members only (TEST_CAST 101-105, the owner 1)."""
import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind.drama import config as drama_config
from leeroy_mind.drama import couples
from leeroy_mind.nightly import encounters as enc
from leeroy_mind.nightly import gossip, knowledge
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
from leeroy_mind.nightly.ground import FICTION_ASK, JUDGE_ASK, Claim, check, ground
from leeroy_mind.nightly.llm import COUPLE_RULE, INVARIANTS, ROMANCE_RULE, system_prompt
from leeroy_mind.nightly.romance import jealousy_level, stage_jealousy
from leeroy_mind.nightly.storyteller import Volition, volitions
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.prompt import relationship_card
from nightly.conftest import yes
from nightly.test_night_encounters import encounter, prompt, reply, seeded

run = asyncio.run
DAY = 86400.0


def drama_on(h):
    h.settings = replace(h.settings, member_romance=True)


def woman(h, guid, stance="none", **over):
    return h.persona(guid, player_gender="female", stance=stance, **over)


def couple(h, a, b, stage, *, attraction=30.0, affinity=20.0, trust=10.0):
    for x, y in ((a, b), (b, a)):
        h.rel(x, y, romance_stage=stage, attraction=attraction, affinity=affinity, trust=trust)


def stage(h, a, b):
    return (h.store.get_relationship(a, b).romance_stage, h.store.get_relationship(b, a).romance_stage)


def pursue(h, guid, *, ago=3600.0, kind="flirt"):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) "
                             "VALUES (?,?,20,1,'chat:1',?,1)", (guid, kind, h.clock.now() - ago))


# -- the switch -------------------------------------------------------------------------------

def test_config_is_off_unless_a_valid_table_turns_it_on(tmp_path):
    path = tmp_path / "mind.toml"
    assert drama_config.load_drama_config(path).member_romance is False
    path.write_bytes(b"[drama]\nmember_romance = true\n")
    assert drama_config.load_drama_config(path).member_romance is True
    for bad in (b"[drama]\nmember_romance = 1\n", b"[drama]\nmember_romance = true\nother = true\n",
                b"drama = 3\n", b"[drama\n"):
        path.write_bytes(bad)
        assert drama_config.load_drama_config(path).member_romance is False


def test_settings_read_the_switch(tmp_path):
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly.settings import load_settings
    assert load_settings(load_config(tmp_path / "mind.toml"), tmp_path).member_romance is False
    (tmp_path / "mind.toml").write_bytes(b"[drama]\nmember_romance = true\n")
    assert load_settings(load_config(tmp_path / "mind.toml"), tmp_path).member_romance is True


def test_attach_adds_the_night_stage_only_when_on(tmp_path):
    from leeroy_mind.nightly.pipeline import STAGES
    from leeroy_mind.nightly.runner import NightRunner
    app = SimpleNamespace(data_dir=tmp_path, night_runner=NightRunner(STAGES, lambda *a: None))
    assert couples.attach(app) is None
    assert "couples" not in [s.name for s in app.night_runner.stages]
    (tmp_path / "mind.toml").write_bytes(b"[drama]\nmember_romance = true\n")
    assert couples.attach(app) is True
    names = [s.name for s in app.night_runner.stages]
    assert names.index("couples") == names.index("romance") + 1
    assert names.index("couples") < names.index("jealousy")


# -- 1. scenes survive the checker ----------------------------------------------------------

def _ev(h):
    ev = Evidence()
    ev.add(Item("encounter:1", "the Orgrimmar bank steps", frozenset({101, 102}), h.T))
    return ev


def test_a_scene_is_judged_for_contradiction_and_other_kinds_for_support(h):
    def judge(text):
        verdict = "no" if "contradicted" in text else "yes"
        return {"verdicts": [{"i": int(n), "support": verdict, "invariants_ok": True, "why": "w"}
                             for n in dict.fromkeys(re.findall(r"\bITEM (\d+)\b", text))]}
    ctx = h.ctx(default=judge)
    claims = [Claim("diary", 101, "Long night.", ["encounter:1"]),
              Claim("scene", 101, "Arkon told Belra about his pet turtle.", ["encounter:1"]),
              Claim("scene", 102, "Belra laughed at the turtle story.", ["encounter:1"])]
    pairs = run(ground(ctx, claims, _ev(h), load_facts(ctx)))
    assert [v.ok for _, v in pairs] == [True, True, True]
    asks = [c["messages"][-1]["content"] for c in ctx.reasoner.llm.calls]
    assert len(asks) == 2
    assert asks[0].startswith(JUDGE_ASK) and "[diary]" in asks[0] and "[scene]" not in asks[0]
    assert asks[1].startswith(FICTION_ASK) and "ITEM 0: [scene]" in asks[1] and "ITEM 1: [scene]" in asks[1]


def test_a_contradicted_or_rule_breaking_scene_is_still_rejected(h):
    def judge(text):
        return {"verdicts": [{"i": 0, "support": "no", "invariants_ok": True, "why": "wrong boss"},
                             {"i": 1, "support": "yes", "invariants_ok": False, "why": "a tablet"},
                             {"i": 2, "support": "partly", "invariants_ok": True, "why": "fine"}]}
    ctx = h.ctx(default=judge)
    claims = [Claim("scene", 101, f"Scene part {i}.", ["encounter:1"]) for i in range(3)]
    reasons = [(v.ok, v.reason) for _, v in run(ground(ctx, claims, _ev(h), load_facts(ctx)))]
    assert reasons == [(False, "contradicted: wrong boss"), (False, "invariants: a tablet"),
                       (True, "fine")]


def test_people_around_may_be_named_in_the_scene_but_not_in_its_memories_evidence(h):
    h.persona(103, outside_raids=["idling"])
    data = reply(scene="Arkon and Belra argued about kiting while Coros watched.")
    ctx = seeded(h, data)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    asks = [c["messages"][-1]["content"] for c in ctx.reasoner.llm.calls if c.get("messages")]
    assert any("ITEM" in a and "People around: Coros." in a for a in asks)
    for owner in (101, 102):
        (memory,) = h.rows(owner)
        assert not any(ref.startswith("around:") for ref in memory.evidence)


def test_a_guildmate_who_is_not_around_still_fails_the_name_check(h):
    h.persona(103, outside_raids=["idling"])
    run(enc.stage_encounters(seeded(h, reply(scene="Arkon and Belra argued while Ebbo watched."))))
    assert encounter(h)["status"] == "rejected"
    reasons = [r[0] for r in h.store.conn.execute("SELECT reason FROM grounding_log")]
    assert "name not in evidence: ebbo" in reasons


def test_the_couple_rule_swaps_only_the_romance_sentence(h):
    plain = system_prompt("encounter", "x", h.settings.data_dir, human=h.ident)
    assert plain.endswith(INVARIANTS.format(human=h.ident.called))
    swapped = system_prompt("encounter", "x", h.settings.data_dir, human=h.ident, couple=("Arkon", "Belra"))
    assert ROMANCE_RULE.format(human=h.ident.called) not in swapped
    assert COUPLE_RULE.format(human=h.ident.called, a="Arkon", b="Belra") in swapped
    assert swapped.replace(COUPLE_RULE.format(human=h.ident.called, a="Arkon", b="Belra"),
                           ROMANCE_RULE.format(human=h.ident.called)) == plain


def test_rule_seven_lets_a_couple_be_romantic_about_each_other_only(h):
    facts = load_facts(h.ctx())
    ev = _ev(h)
    ev.add(Item("encounter:2", "with Coros", frozenset({103}), h.T))
    pair = frozenset({101, 102})
    flirt = Claim("scene", 101, "Arkon flirted with Belra by the bank.", ["encounter:1"])
    assert check(flirt, ev, facts).reason == "romance between members"
    assert check(flirt, ev, facts, couple=pair).ok
    third = Claim("scene", 101, "Arkon flirted with Coros by the bank.", ["encounter:1", "encounter:2"])
    assert check(third, ev, facts, couple=pair).reason == "romance between members"


# -- 2. member romances -----------------------------------------------------------------------

@pytest.mark.parametrize("household,taken", [
    ("Married, two kids", True), ("Lives with her husband in a flat", True),
    ("Two-bed with civil partner and a collie", True), ("Lives with girlfriend and a corgi", True),
    ("Lives alone, no partner", False), ("Divorced, two cats", False),
    ("Lives alone; ex-husband's name still on the deed", False), ("lives with a flatmate", False),
    ("Widowed; her late husband's records on the shelf", False), ("Never married, one cat", False)])
def test_taken_reads_the_household(household, taken):
    assert couples.taken(SimpleNamespace(real_life=SimpleNamespace(household=household))) is taken


def _spark_ready(h, w=102, m=101, affinity=15.0):
    for x, y in ((w, m), (m, w)):
        h.rel(x, y, affinity=affinity)


def test_a_man_and_a_woman_who_like_each_other_spark(h, monkeypatch):
    monkeypatch.setattr(couples, "SPARK_P", 1.0)
    drama_on(h)
    woman(h, 102)
    _spark_ready(h)
    ctx = h.ctx()
    run(couples.stage_couples(ctx))
    assert not ctx.failures
    assert stage(h, 101, 102) == ("notices", "notices")
    assert h.store.get_relationship(101, 102).attraction == couples.SPARK_ATTRACTION
    assert couples.active(h.store, ctx.members(), 1) == [(101, 102, "notices")]
    assert couples.partner(h.store, 102, 1) == (101, "notices")
    run(couples.stage_couples(ctx))  # the unit is done: nothing twice
    assert h.count("rel_delta") == 4


@pytest.mark.parametrize("case", ["married", "two_men", "dislike", "his", "lust", "taken_by_member"])
def test_who_cannot_start_a_member_romance(h, monkeypatch, case):
    monkeypatch.setattr(couples, "SPARK_P", 1.0)
    monkeypatch.setattr(couples, "DRAWN_FACTOR", 0.0)
    drama_on(h)
    woman(h, 102, household="Married, no kids" if case == "married" else "lives with a flatmate",
          stance="lust" if case == "lust" else "none")
    if case == "two_men":
        h.persona(102)
    _spark_ready(h, affinity=5.0 if case == "dislike" else 15.0)
    if case == "his":
        h.rel(102, 1, romance_stage="banter")  # his romance: members never take her
    if case == "taken_by_member":
        h.persona(103)
        couple(h, 102, 103, "banter")
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("none", "none")


def test_a_couple_climbs_by_warm_scenes_at_its_own_pace(h):
    drama_on(h)
    woman(h, 102)
    couple(h, 101, 102, "notices", attraction=20.0, affinity=15.0, trust=5.0)
    ctx = h.ctx()
    scene = {"a": 101, "b": 102, "id": 7}
    with h.store.transaction():
        assert couples.after_scene(h.store, ctx, scene, total=2, valence=0.5) == "step"
    assert stage(h, 101, 102) == ("banter", "banter")
    with h.store.transaction():
        assert couples.after_scene(h.store, ctx, scene, total=2, valence=0.5) == "warmer"  # pace
    assert stage(h, 101, 102) == ("banter", "banter")
    h.clock.advance(2 * DAY)
    with h.store.transaction():
        assert couples.after_scene(h.store, ctx, scene, total=1, valence=0.2) == "step"
    assert stage(h, 101, 102) == ("whispers", "whispers")
    assert h.store.get_relationship(102, 101).attraction == pytest.approx(44.0)


def test_scores_hold_a_couple_where_it_is(h):
    drama_on(h)
    woman(h, 102)
    couple(h, 101, 102, "whispers", attraction=60.0, affinity=10.0, trust=50.0)
    with h.store.transaction():
        assert couples.after_scene(h.store, h.ctx(), {"a": 101, "b": 102, "id": 1},
                                   total=3, valence=0.6) == "warmer"
    assert stage(h, 101, 102) == ("whispers", "whispers")


def test_a_cold_scene_cools_and_an_early_romance_fizzles(h):
    drama_on(h)
    woman(h, 102)
    couple(h, 101, 102, "banter", attraction=18.0)
    with h.store.transaction():
        assert couples.after_scene(h.store, h.ctx(), {"a": 101, "b": 102, "id": 1},
                                   total=-2, valence=-0.5) == "fizzled"
    assert stage(h, 101, 102) == ("none", "none")
    couple(h, 101, 102, "late_nights", attraction=18.0)
    with h.store.transaction():
        assert couples.after_scene(h.store, h.ctx(), {"a": 101, "b": 102, "id": 2},
                                   total=-2, valence=-0.5) == "cooler"
    assert stage(h, 101, 102) == ("late_nights", "late_nights")


def test_nothing_moves_with_the_switch_off(h, monkeypatch):
    monkeypatch.setattr(couples, "SPARK_P", 1.0)
    woman(h, 102)
    couple(h, 101, 102, "notices")
    with h.store.transaction():
        assert couples.after_scene(h.store, h.ctx(), {"a": 101, "b": 102, "id": 1},
                                   total=3, valence=0.5) is None
    assert couples.couple_names(h.ctx(), 101, 102) is None
    assert not [v for vs in volitions(h.ctx()).values() for v in vs if v.kind == "flirt"]


def test_couples_get_flirt_scenes_and_jealousy_gets_confrontations(h):
    drama_on(h)
    woman(h, 102)
    woman(h, 104, "lust")
    couple(h, 101, 102, "banter")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (104,103,'rivalry',85,?,'[]')", (h.T,))
        h.store.conn.execute("INSERT INTO jealousy VALUES (105,1,'envy',95,?,'[]')", (h.T,))
    got = [v for vs in volitions(h.ctx()).values() for v in vs]
    assert {(v.guid, v.target) for v in got if v.kind == "flirt"} <= {(101, 102), (102, 101)}
    assert any(v.kind == "flirt" for v in got)
    (confront,) = [v for v in got if v.kind == "confront"]
    assert (confront.guid, confront.target, confront.ref) == (104, 103, "rule:jealousy:103")
    assert confront.weight == pytest.approx(0.75)


def test_a_couples_scene_may_flirt_and_draws_them_closer(h):
    drama_on(h)
    woman(h, 102)
    couple(h, 101, 102, "banter", attraction=30.0, affinity=15.0, trust=5.0)
    data = reply(scene="Arkon and Belra flirted on the bank steps.",
                 memories=[{"who": "a", "text": "Belra flirted with me.", "importance": 6, "valence": 0.7},
                           {"who": "b", "text": "Arkon flirted with me.", "importance": 6, "valence": 0.7}],
                 deltas=[{"from": "a", "dim": "affinity", "delta": 2}])
    ctx = seeded(h, data, volition="flirt")
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    text = prompt(ctx)
    assert "no romance or flirting between them" not in text
    assert "a little flirting between the two of them is fine" in text
    assert COUPLE_RULE.format(human=h.ident.called, a="Arkon", b="Belra") in text
    assert couples.INTENT["banter"].format(a="Arkon", b="Belra") in text
    assert h.store.get_relationship(102, 101).attraction == pytest.approx(38.0)


def test_without_the_switch_the_same_scene_is_refused(h):
    woman(h, 102)
    couple(h, 101, 102, "banter")
    data = reply(scene="Arkon and Belra flirted on the bank steps.")
    ctx = seeded(h, data)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "rejected"
    assert "no romance or flirting between them" in prompt(ctx)
    reasons = [r[0] for r in h.store.conn.execute("SELECT reason FROM grounding_log")]
    assert "romance between members" in reasons


# -- 3. he draws them away ------------------------------------------------------------------

def _dating(h, stage_="late_nights", *, pull=70.0, held=30.0):
    drama_on(h)
    woman(h, 102, "fond")
    couple(h, 101, 102, stage_, attraction=held, affinity=30.0, trust=30.0)
    h.rel(102, 1, attraction=pull)


def test_his_pursuit_wins_her_and_her_partner_is_hurt(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h)
    pursue(h, 102)
    ctx = h.ctx()
    run(couples.stage_couples(ctx))
    assert not ctx.failures
    assert stage(h, 101, 102) == ("ex", "ex")
    row = h.store.conn.execute("SELECT * FROM breakup").fetchone()
    assert (row["woman"], row["partner"], row["for_guid"], row["stage"], row["feud"]) == (
        102, 101, 1, "late_nights", None)
    root = f"rule:breakup:{row['id']}"
    hurt = [m for m in h.rows(101) if m.key == f"breakup:{row['id']}"]
    assert [m.text for m in hurt] == ["Belra left me for Testy."]
    assert hurt[0].scope == "open" and hurt[0].evidence == (root,)
    assert [m.text for m in h.rows(102) if m.key == f"breakup:{row['id']}"] == ["Left Arkon for Testy."]
    assert jealousy_level(h.store, 101, 1) >= couples.ENVY_SPIKE
    assert h.store.get_relationship(101, 102).affinity == pytest.approx(20.0)
    rumor = h.store.conn.execute("SELECT * FROM rumor WHERE root=?", (root,)).fetchone()
    assert rumor["about"] == 102 and rumor["canonical"] == "Belra left Arkon for Testy."
    assert gossip.third_person(rumor["canonical"], "Belra")
    spreader = h.store.conn.execute("SELECT guid,state FROM rumor_state WHERE rumor=?", (rumor["id"],)).fetchall()
    assert [tuple(r) for r in spreader] == [(101, "spreader")]
    assert {101, 102} <= knowledge.knowers(h.store, root)
    kinds = {(r["type"], r["target"]) for r in h.store.conn.execute("SELECT * FROM emotion WHERE guid=101")}
    assert kinds == {("distress", 102), ("resentment", 1)}


def test_the_rumour_reaches_the_guild_through_gossip_volitions(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h)
    pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    h.settings = replace(h.settings, member_romance=True)
    got = [v for vs in volitions(h.ctx(night="2026-10-30")).values() for v in vs if v.kind == "gossip"]
    assert got and all(v.guid == 101 and v.ref.startswith("rule:rumor:") for v in got)


def test_a_jilted_partner_may_open_a_feud(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 1.0)
    _dating(h)
    pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    feud = h.store.conn.execute("SELECT * FROM feud").fetchone()
    breakup = h.store.conn.execute("SELECT * FROM breakup").fetchone()
    assert (feud["a"], feud["b"], feud["ended"]) == (101, 102, None)
    assert json.loads(feud["evidence"]) == [f"rule:breakup:{breakup['id']}"]
    assert feud["cause"] == "Belra left Arkon for Testy"
    assert breakup["feud"] == feud["id"]


@pytest.mark.parametrize("pursued,pull", [(False, 99.0), (True, 40.0)])
def test_without_pursuit_or_enough_pull_she_stays(h, pursued, pull):
    _dating(h, pull=pull)
    if pursued:
        pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("late_nights", "late_nights") and h.count("breakup") == 0
    expected = 30.0 - couples.EROSION if pursued else 30.0
    assert h.store.get_relationship(102, 101).attraction == pytest.approx(expected)


def test_his_pursuit_always_wins_if_he_keeps_at_it(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h, "confession", pull=40.0, held=60.0)
    for n in range(12):
        pursue(h, 102)
        run(couples.stage_couples(h.ctx(night=f"2026-11-{n + 1:02}")))
        h.clock.advance(DAY)
        if h.count("breakup"):
            break
    assert stage(h, 101, 102) == ("ex", "ex")


def test_before_a_crush_she_just_drifts_away(h):
    _dating(h, "banter")
    pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("none", "none")
    assert h.count("breakup") == 0 and h.count("rumor") == 0


def test_a_woman_with_no_stance_toward_him_is_never_drawn_away(h):
    drama_on(h)
    woman(h, 102)
    couple(h, 101, 102, "late_nights")
    pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("late_nights", "late_nights")


# -- 4. jealousy becomes drama --------------------------------------------------------------

def _rivals(h):
    woman(h, 101, "lust")
    woman(h, 102, "fond")
    for g in (101, 102):
        h.rel(g, 1, romance_stage="banter")


def test_a_saturated_level_comes_down_when_only_its_own_memory_is_left(h):
    _rivals(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',100,?,'[]')", (h.T - DAY,))
    h.mem(101, "Belra keeps going after Testy's attention.", actors=(102, 1),
          key="jealousy:102:2026-W44", kind="reflection")
    levels = []
    for day in range(3):
        run(stage_jealousy(h.ctx(night=f"2026-10-{29 + day}")))
        levels.append(jealousy_level(h.store, 101, 102))
        h.clock.advance(DAY)
    assert levels == pytest.approx([85.0, 72.25, 61.4125])


def test_boiling_rivalry_sours_toward_the_rival_and_envy_means_to_say_so(h):
    drama_on(h)
    _rivals(h)
    h.persona(103, jealousy_mode="grumbler")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',90,?,'[]')", (h.T,))
        h.store.conn.execute("INSERT INTO jealousy VALUES (103,1,'envy',90,?,'[]')", (h.T,))
    for _ in range(3):
        h.mem(103, "Belra fussing over Testy.", actors=(102, 1))
    run(stage_jealousy(h.ctx()))
    assert h.store.get_relationship(101, 102).affinity == -2.0
    (said,) = [m for m in h.rows(103) if m.key and m.key.startswith("envy:")]
    assert said.actors == (1,) and "Testy" in said.text and said.scope == "open"


def test_without_the_switch_jealousy_writes_no_drama(h):
    _rivals(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',90,?,'[]')", (h.T,))
    run(stage_jealousy(h.ctx()))
    assert h.store.get_relationship(101, 102).affinity == 0.0
    assert not [v for vs in volitions(h.ctx()).values() for v in vs if v.ref.startswith("rule:jealousy:")]


@pytest.mark.parametrize("on", [True, False])
def test_a_jealous_blowup_sours_and_opens_a_feud(h, on):
    if on:
        drama_on(h)
    h.rel(101, 102, affinity=-25.0)
    data = reply(scene="Arkon had it out with Belra on the bank steps.",
                 deltas=[{"from": "a", "dim": "affinity", "delta": -3},
                         {"from": "b", "dim": "affinity", "delta": -3}])
    ctx = h.ctx({"encounter": [data], "judge": [yes]})
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,volition,ref) "
                             "VALUES (?,0,101,102,'the Orgrimmar bank steps','planned','confront',"
                             "'rule:jealousy:102')", (ctx.night,))
    ctx.skip("encounters", "plan")
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done" and encounter(h)["blowup"] == 1
    feuds = h.store.conn.execute("SELECT a,b FROM feud").fetchall()
    if on:
        assert h.store.get_relationship(101, 102).affinity == pytest.approx(-35.0)  # -25-3, cap -10
        assert h.store.get_relationship(102, 101).affinity == pytest.approx(-7.0)
        assert [tuple(r) for r in feuds] == [(101, 102)]
        assert "jealous of how Belra keeps after Testy's attention" in prompt(ctx)
    else:
        assert h.store.get_relationship(101, 102).affinity == pytest.approx(-28.0)
        assert feuds == []


# -- 5. rumours in the third person ---------------------------------------------------------

@pytest.mark.parametrize("text,ok", [
    ("Belra says she joined the guild for Testy.", True), ("I came for Testy.", False),
    ("Belra showed me the ropes.", False), ("We all saw Belra do it.", False),
    ("Belra said \"I came for Testy\".", False), ("Coros came for Testy.", False),
    ("Belra’s new mount is shiny.", True), ("I’m sure Belra did it.", False)])
def test_third_person(text, ok):
    assert gossip.third_person(text, "Belra") is ok


def test_a_first_person_telling_reaches_its_hearer_without_a_rumour(h):
    ident = h.mem(101, "Coros took my bracers.", importance=8, valence=-0.6, actors=(103,))
    m = h.store.get_memories([ident])[0]
    ctx = h.ctx()
    enc_row = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status) "
                                   "VALUES (?,0,101,102,'the bank','done')", (h.night,)).lastrowid
    scene = dict(h.store.conn.execute("SELECT * FROM encounter WHERE id=?", (enc_row,)).fetchone())
    data = {"teller": "a", "about": "Coros", "source": m.id, "text": "Coros took my bracers.",
            "claim": "I think Coros took them."}
    with h.store.transaction():
        heard = gossip.after_gossip(h.store, ctx, scene, data, [m], now=h.T)
    assert heard is not None and h.count("rumor") == 0
    data = {**data, "text": "Coros took Arkon's bracers.", "claim": ""}
    with h.store.transaction():
        gossip.after_gossip(h.store, ctx, scene, data, [m], now=h.T)
    assert h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0] == "Coros took Arkon's bracers."


def test_a_rumour_may_name_a_couple_when_the_switch_is_on(h):
    drama_on(h)
    woman(h, 103)
    couple(h, 103, 104, "late_nights")
    ident = h.mem(101, "Coros is dating Dunna now.", importance=8, actors=(103, 104))
    m = h.store.get_memories([ident])[0]
    enc_row = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status) "
                                   "VALUES (?,0,101,102,'the bank','done')", (h.night,)).lastrowid
    scene = dict(h.store.conn.execute("SELECT * FROM encounter WHERE id=?", (enc_row,)).fetchone())
    data = {"teller": "a", "about": "Coros", "source": m.id, "text": "Coros is dating Dunna.",
            "claim": "Coros is dating Dunna."}
    with h.store.transaction():
        assert gossip.after_gossip(h.store, h.ctx(), scene, data, [m], now=h.T) is not None
    assert h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0] == "Coros is dating Dunna."


# -- live talk ------------------------------------------------------------------------------

def test_a_card_between_two_members_names_their_romance_outside_group_turns():
    r = Relationship(a=101, b=102, affinity=30, familiarity=50, romance_stage="late_nights")
    assert "you're dating" in relationship_card("Belra", r, is_human=False)
    assert "you're dating" not in relationship_card("Belra", r, is_human=False, friendship_only=True)
    assert "you broke up" in relationship_card("Belra", replace(r, romance_stage="ex"), is_human=False)
    him = Relationship(a=101, b=1, affinity=30, familiarity=50, romance_stage="late_nights")
    assert "dating" not in relationship_card("Testy", him, is_human=True)


@pytest.mark.parametrize("total,drama,opened", [(-2, True, False), (-6, True, True), (-6, False, False)])
def test_only_a_jealous_blowup_needs_just_the_confronters_side(h, total, drama, opened):
    from leeroy_mind.nightly import feuds
    h.rel(101, 102, affinity=-40.0)
    ctx = h.ctx()
    ident = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,scene,volition,ref) "
                                 "VALUES (?,0,101,102,'the bank','done','They argued.','confront',"
                                 "'rule:jealousy:102')", (ctx.night,)).lastrowid
    row = dict(h.store.conn.execute("SELECT * FROM encounter WHERE id=?", (ident,)).fetchone())
    deltas = [{"from": "a", "dim": "affinity", "delta": total / 2},
              {"from": "b", "dim": "affinity", "delta": total / 2}]
    with h.store.transaction():
        got = feuds.maybe_open(h.store, ctx, row, deltas, (), drama=drama)
    assert (got is not None) is opened
