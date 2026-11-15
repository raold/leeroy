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
from leeroy_mind.nightly.ground import (FICTION_ASK, FICTION_SYSTEM, JUDGE_ASK, JUDGE_SYSTEM,
                                        Claim, check, ground)
from leeroy_mind.nightly.llm import COUPLE_RULE, INVARIANTS, ROMANCE_RULE, system_prompt
from leeroy_mind.nightly.romance import jealousy_level, stage_jealousy
from leeroy_mind.nightly.storyteller import Volition, volitions
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.prompt import relationship_card
from nightly.conftest import consistent
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


def pursue(h, guid, *, ago=3600.0, kind="flirt", channel="guild"):
    """An act of his toward her `ago` seconds back, on a line of his in `channel` (a whisper is
    to her); the clock is left where it was. Returns the act's ref."""
    now = h.clock.now()
    private = channel == "whisper" or channel.startswith("dm:")
    line = h.say(now - ago, 1, "you're gorgeous", channel=channel, to=guid if private else 0)
    h.clock.set(now)
    if kind:
        with h.store.transaction():
            h.store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) "
                                 "VALUES (?,?,20,1,?,?,1)", (guid, kind, f"chat:{line}", now - ago))
    return f"chat:{line}"


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
        return {"verdicts": [{"i": int(n), "support": "yes", "invariants_ok": True, "why": "w"}
                             for n in dict.fromkeys(re.findall(r"\bITEM (\d+)\b", text))]}
    ctx = h.ctx({"judge": [judge], "judge_scene": [consistent]})
    claims = [Claim("diary", 101, "Long night.", ["encounter:1"]),
              Claim("scene", 101, "Arkon told Belra about his pet turtle.", ["encounter:1"]),
              Claim("scene", 102, "Belra laughed at the turtle story.", ["encounter:1"])]
    pairs = run(ground(ctx, claims, _ev(h), load_facts(ctx)))
    assert [v.ok for _, v in pairs] == [True, True, True]
    calls = [c["messages"] for c in ctx.reasoner.llm.calls if c.get("messages")
             and "ITEM" in c["messages"][-1]["content"]]
    assert len(calls) == 2
    (sys0, ask0), (sys1, ask1) = [(m[0]["content"], m[-1]["content"]) for m in calls]
    assert sys0.startswith(JUDGE_SYSTEM) and ask0.startswith(JUDGE_ASK) and "[diary]" in ask0
    assert "[scene]" not in ask0
    assert sys1.startswith(FICTION_SYSTEM) and "supported" not in sys1.split("\n")[0]
    assert ask1.startswith(FICTION_ASK) and "ITEM 0: [scene]" in ask1 and "ITEM 1: [scene]" in ask1
    # The question's natural answer is the verdict: is it contradicted? true rejects.
    assert "is it contradicted by its EVIDENCE?" in FICTION_ASK


def test_a_contradicted_or_rule_breaking_scene_is_still_rejected(h):
    def judge(text):
        return {"verdicts": [
            {"i": 0, "contradicted": True, "invariants_ok": True, "why": "wrong boss"},
            {"i": 1, "contradicted": False, "invariants_ok": False, "why": "a tablet"},
            {"i": 2, "contradicted": False, "invariants_ok": True, "why": "fine"}]}
    ctx = h.ctx({"judge_scene": [judge]})
    claims = [Claim("scene", 101, f"Scene part {i}.", ["encounter:1"]) for i in range(3)]
    got = [(v.ok, v.label, v.reason) for _, v in run(ground(ctx, claims, _ev(h), load_facts(ctx)))]
    assert got == [(False, "contradicted", "contradicted: wrong boss"),
                   (False, "consistent", "invariants: a tablet"), (True, "consistent", "fine")]


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
    woman(h, 104, "fond")
    _spark_ready(h, w=104, m=103)
    run(couples.stage_couples(h.ctx()))  # the stage itself checks the night's switch
    assert stage(h, 103, 104) == ("none", "none") and h.count("rel_delta") == 0
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


def test_his_public_pursuit_wins_her_and_the_guild_hears_why(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h)
    pursue(h, 102)
    ctx = h.ctx()
    run(couples.stage_couples(ctx))
    assert not ctx.failures
    assert stage(h, 101, 102) == ("ex", "ex")
    row = h.store.conn.execute("SELECT * FROM breakup").fetchone()
    assert (row["woman"], row["partner"], row["for_guid"], row["stage"], row["feud"], row["public"]) == (
        102, 101, 1, "late_nights", None, 1)
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


def test_a_whispered_pursuit_keeps_why_she_left_between_her_and_him(h, monkeypatch):
    """Ruling P2-4: the split is public, the reason is not when all his pursuit was private."""
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 1.0)
    _dating(h)
    whisper = pursue(h, 102, channel="whisper")
    run(couples.stage_couples(h.ctx()))
    row = h.store.conn.execute("SELECT * FROM breakup").fetchone()
    assert stage(h, 101, 102) == ("ex", "ex") and row["public"] == 0
    key = f"breakup:{row['id']}"
    (hurt,) = [m for m in h.rows(101) if m.key == key]
    assert hurt.text == "Belra has been distant. I think there's someone else."
    assert hurt.actors == (102,) and hurt.scope == "open" and "Testy" not in hurt.text
    (left,) = [m for m in h.rows(102) if m.key == key]
    assert left.text == "Left Arkon for Testy." and (left.scope, left.scope_with) == ("private", 1)
    assert whisper in left.evidence
    rumor = h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0]
    assert rumor == "Belra and Arkon split up."
    assert h.store.conn.execute("SELECT cause FROM feud").fetchone()[0] == "Belra and Arkon split up"
    assert jealousy_level(h.store, 101, 1) == 0
    assert {r["target"] for r in h.store.conn.execute("SELECT target FROM emotion WHERE guid=101")} == {102}
    fact = h.store.conn.execute("SELECT text FROM fact WHERE kind='feud_cause'").fetchone()[0]
    assert "Testy" not in fact


def test_one_public_act_makes_the_reason_public(h, monkeypatch):
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h)
    pursue(h, 102, channel="whisper")
    pursue(h, 102, ago=7200.0)
    run(couples.stage_couples(h.ctx()))
    assert h.store.conn.execute("SELECT public FROM breakup").fetchone()[0] == 1
    assert h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0] == "Belra left Arkon for Testy."


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


@pytest.mark.parametrize("pursued,pull", [(False, 99.0), (True, 39.0)])
def test_without_pursuit_or_enough_pull_she_stays(h, pursued, pull):
    _dating(h, pull=pull)
    if pursued:
        pursue(h, 102)
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("late_nights", "late_nights") and h.count("breakup") == 0
    expected = 30.0 - couples.EROSION if pursued else 30.0
    if pursued:
        assert pull < 30.0 - couples.EROSION + couples.HOLD["late_nights"] + couples.MARGIN
    assert h.store.get_relationship(102, 101).attraction == pytest.approx(expected)


def test_his_pursuit_always_wins_if_he_keeps_at_it(h, monkeypatch):
    """A couple at together with a warm scene of their own every night, against his steady
    pursuit: she leaves within about two weeks (ruling P2-1)."""
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h, "confession", pull=40.0, held=64.0)
    nights = None
    for n in range(30):
        ctx = h.ctx(night=f"2026-12-{n + 1:02}")
        with h.store.transaction():
            couples.after_scene(h.store, ctx, {"a": 101, "b": 102, "id": 1000 + n},
                                total=2, valence=0.5)
        pursue(h, 102)
        run(couples.stage_couples(ctx))
        h.clock.advance(DAY)
        if h.count("breakup"):
            nights = n + 1
            break
    assert stage(h, 101, 102) == ("ex", "ex")
    assert nights is not None and nights <= 14


def test_while_he_pursues_her_their_scenes_do_not_warm_her(h):
    _dating(h, "late_nights", pull=20.0, held=40.0)
    pursue(h, 102)
    with h.store.transaction():
        assert couples.after_scene(h.store, h.ctx(), {"a": 101, "b": 102, "id": 9},
                                   total=3, valence=0.5) == "distracted"
    assert h.store.get_relationship(102, 101).attraction == 40.0
    assert h.store.get_relationship(101, 102).attraction == 40.0 + couples.FLIRT_GAIN


@pytest.mark.parametrize("kind,channel", [("quest_together", "guild"), ("answered", "guild"),
                                          ("remembered", "raid")])
def test_raid_time_and_guild_replies_are_not_pursuit(h, monkeypatch, kind, channel):
    """A raid with her in it (quest_together) and his guild-chat replies (answered) are not his
    deliberate attention: they never draw her away (ruling P1-1)."""
    monkeypatch.setattr(couples, "_feud_chance", lambda ctx, guid: 0.0)
    _dating(h, "whispers", pull=99.0, held=10.0)
    pursue(h, 102, kind=kind, channel=channel)
    h.boss(h.clock.now() - 600, 533, 1, "killed", [1, 101, 102])
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("whispers", "whispers") and h.count("breakup") == 0
    assert h.store.get_relationship(102, 101).attraction == 10.0  # no erosion either


@pytest.mark.parametrize("how", ["flirt", "compliment", "emote", "whisper", "dm"])
def test_what_counts_as_his_pursuit(h, how):
    _dating(h)
    now = h.clock.now()
    if how in ("flirt", "compliment"):
        pursue(h, 102, kind="flirt" if how == "flirt" else "public_compliment")
        public = True
    elif how == "emote":
        event = h.emote(now - 60, 1, "kiss", 102)
        with h.store.transaction():
            h.store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) "
                                 "VALUES (102,'flirt',20,1,?,?,0)", (f"event:{event}", now - 60))
        public = True
    else:
        pursue(h, 102, kind=None, channel="whisper" if how == "whisper" else "dm:102")
        public = False
    acts = couples._pursuit(h.store, 102, 1, h.clock.now())
    assert len(acts) == 1 and acts[0][1] is public


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
    ctx = h.ctx({"encounter": [data], "judge_scene": [consistent]})
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
    data = {**data, "claim": "Coros took Arkon's bracers."}
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

def test_a_card_between_two_members_names_their_romance_outside_group_turns(monkeypatch):
    r = Relationship(a=101, b=102, affinity=30, familiarity=50, romance_stage="late_nights")
    assert "dating" not in relationship_card("Belra", r, is_human=False)  # the mind started off
    monkeypatch.setattr(drama_config, "_PINNED", True)
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


# -- the review's fixes (#315, FIX_FIRST) --------------------------------------------------------

def test_exes_rest_and_feuding_pairs_never_spark(h, monkeypatch):
    monkeypatch.setattr(couples, "SPARK_P", 1.0)
    drama_on(h)
    woman(h, 102)
    woman(h, 104)
    _spark_ready(h, w=102, m=101)
    _spark_ready(h, w=104, m=103)
    couple(h, 101, 102, "ex", affinity=15.0)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO breakup(woman,partner,for_guid,stage,night,ts,public) "
                             "VALUES (102,101,1,'late_nights','2026-10-20',?,1)", (h.T - 10 * DAY,))
        h.store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES (103,104,?,'x')", (h.T - DAY,))
    run(couples.stage_couples(h.ctx()))
    assert stage(h, 101, 102) == ("ex", "ex") and stage(h, 103, 104) == ("none", "none")
    h.clock.advance(couples.EX_COOLDOWN_DAYS * DAY)
    run(couples.stage_couples(h.ctx(night="2026-12-01")))
    assert stage(h, 101, 102) == ("notices", "notices")  # the rest is over
    assert stage(h, 103, 104) == ("none", "none")        # the feud is still open


def test_breakup_rumours_never_take_a_live_rumour_slot(h):
    from nightly.test_night_gossip import scene, source, tell
    with h.store.transaction():
        for i in range(4):
            h.store.conn.execute("INSERT INTO rumor(root,about,canonical,source,created,last_told) "
                                 "VALUES (?,103,'Dunna and Ebbo split up.',0,?,?)",
                                 (f"rule:breakup:{i + 1}", h.T, h.T))
    assert len(gossip.organic_rumors(h.store)) == 0
    tell(h, source(h))
    assert h.store.conn.execute("SELECT count(*) FROM rumor WHERE root NOT LIKE 'rule:breakup:%'").fetchone()[0] == 1


@pytest.mark.parametrize("extraversion,fallback", [(5.5, True), (8.0, False)])
def test_only_a_plain_retelling_may_stand_in_for_the_claim(h, monkeypatch, extraversion, fallback):
    """Ruling P2-3: a sharpened or bent retelling never becomes the rumour's canonical."""
    from nightly.test_night_gossip import scene, source
    monkeypatch.setattr(gossip, "trait_today", lambda *args: {
        "extraversion": extraversion, "neuroticism": 5.5, "conscientiousness": 5.5})
    m = source(h)
    retold = "Coros stole the bracers and sold them on the auction house."
    data = {"teller": "a", "about": "Coros", "source": m.id, "text": retold, "claim": ""}
    with h.store.transaction():
        assert gossip.after_gossip(h.store, h.ctx(), scene(h, a=m.owner), data, [m], now=h.T)
    row = h.store.conn.execute("SELECT canonical FROM rumor").fetchone()
    assert (row is not None and row[0] == retold) is fallback and (row is None) is not fallback


def test_a_correction_tells_the_source_never_the_claim(h, monkeypatch):
    from nightly.test_night_gossip import scene, source, tell
    monkeypatch.setattr(gossip, "trait_today", lambda *args: {
        "extraversion": 5.5, "neuroticism": 5.5, "conscientiousness": 5.5})
    m = source(h)
    h.rel(102, 101, trust=60)
    tell(h, m, claim="Coros stole the bracers and sold them.")
    assert h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0] == \
        "Coros stole the bracers and sold them."
    enc = scene(h, a=103)
    enc["_shown_refs"] = (f"memory:{m.id}",)
    with h.store.transaction():
        fixed = gossip.after_correction(h.store, h.ctx(), enc, 1, 102, now=h.T)
    assert h.store.get_memories([fixed])[0].text == "Turns out Coros took my bracers."


def test_the_scene_schema_requires_a_claim_with_any_gossip():
    shape = enc.ENCOUNTER_SCHEMA["properties"]["gossip"]
    assert "claim" in shape["required"] and shape["type"] == ["object", "null"]


def test_the_switch_is_pinned_at_mind_start(h, monkeypatch):
    from nightly.test_night_wiring import app_for, context
    monkeypatch.setattr(drama_config, "_PINNED", False)
    path = h.settings.data_dir / "mind.toml"
    path.write_bytes(b"[drama]\nmember_romance = true\n")
    app = app_for(h)
    try:
        path.write_bytes(b"[drama]\nmember_romance = false\n")
        assert context(app).settings.member_romance is True
        assert drama_config.pinned() is True
        assert "couples" in {s.name for s in app.night_runner.stages}
    finally:
        run(app.stop())
