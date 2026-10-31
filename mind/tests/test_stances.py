import dataclasses
import random
from collections import Counter

import pytest

from conftest import _base_persona_dict
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict
from leeroy_mind.personas.seed import human_seed, seed_relationships
from leeroy_mind.personas.stances import (STANCE_SHARES, apply_reseeds, misgenders, none_fit,
                                          none_fit_reason, plan_rebalance, plan_reseeds,
                                          slot_stances, stance_targets)
from leeroy_mind.protocol import Member

# Invented identity: the real name lives only in the owner's local mind.toml.
GM = HumanIdentity(1, "Zanthor", "Zan", "", True, pronouns="he")


def person(guid, *, female=True, stance="none", style=None, ext=5, age=30, household="",
           job="", lines=(), approved_lines=(), mode=None, core=True, name=None):
    d = _base_persona_dict(guid, name or f"C{guid}")
    d["core"] = core
    d["identity"]["player_gender"] = "female" if female else "male"
    d["identity"]["age"] = age
    d["big_five"]["extraversion"]["score"] = ext
    d["real_life"]["household"] = household or "lives with a flatmate"
    if job:
        d["real_life"]["job"] = job
    d["romance"] = {"stance": stance, "style": style,
                    "jealousy_mode": None if female else (mode or "teaser")}
    if lines or approved_lines:
        d["sample_lines"] = ([{"text": t, "approved": False} for t in lines]
                             + [{"text": t, "approved": True} for t in approved_lines])
    return persona_from_dict(d)


def counts(stances) -> Counter:
    return Counter(stances)


def final_of(cast, plan) -> dict[int, str]:
    final = {p.guid: p.romance.stance for p in cast}
    for guid, old, new in plan.changes:
        assert final[guid] == old
        final[guid] = new
    return final


def test_targets_are_exact_and_follow_the_shares():
    assert sum(STANCE_SHARES.values()) == 1.0
    assert stance_targets(67) == {"lust": 13, "drawn": 20, "fond": 27, "none": 7}
    for n in range(0, 121):
        t = stance_targets(n)
        assert sum(t.values()) == n and min(t.values()) >= 0
        assert all(abs(t[s] - STANCE_SHARES[s] * n) < 1 for s in t)


def test_slot_stances_fill_the_whole_cast():
    seeds = ["lust"] + ["drawn"] * 2 + ["fond"] * 5 + ["none"] * 4
    slots = slot_stances(seeds, 55)
    assert len(slots) == 55
    assert counts(seeds + slots) == stance_targets(67)


def test_slot_stances_with_an_overfull_seed_stance():
    seeds = ["none"] * 12
    slots = slot_stances(seeds, 55)
    t = stance_targets(67)
    got = counts(seeds + slots)
    assert counts(slots)["none"] == 0
    short = {s: t[s] - got[s] for s in ("lust", "drawn", "fond")}
    assert all(v >= 0 for v in short.values()) and sum(short.values()) == 5
    # the shortfall is shared in proportion to the shares
    assert all(abs(short[s] / STANCE_SHARES[s] - 5 / 0.9) <= 1 / STANCE_SHARES[s] for s in short)


def _cast():
    """20 women (10 none, two of them grandmother types; 6 fond; 4 drawn) and 3 men."""
    women = [
        person(1, ext=9, style="bold"), person(2, ext=8), person(3, ext=7, style="theatrical"),
        person(4, ext=7), person(5, ext=3, style="shy"), person(6, ext=8, style="guarded"),
        person(7, ext=3), person(8, ext=2),
        person(9, ext=8, age=62, household="minds the grandkids on weekends"),
        person(10, ext=4, age=58),
    ]
    women += [person(11 + i, stance="fond", ext=4) for i in range(6)]
    women += [person(17 + i, stance="drawn", style="earnest", ext=3) for i in range(4)]
    men = [person(30 + i, female=False, ext=9, style=None) for i in range(3)]
    return women + men


def test_rebalance_hits_the_mix_with_the_fewest_changes():
    cast = _cast()
    plan = plan_rebalance(cast, random.Random(1))
    final = final_of(cast, plan)
    women = [p for p in cast if p.identity.player_gender == "female"]
    assert counts(final[p.guid] for p in women) == stance_targets(20)  # 4/6/8/2
    assert len(plan.changes) == 8  # none 10 -> 2 is the only surplus
    assert all(old == "none" for _, old, _ in plan.changes)
    assert not plan.shortfall
    assert all(final[p.guid] == "none" for p in cast if p.identity.player_gender == "male")


def test_lust_goes_to_the_bold_and_outgoing_never_the_shy_or_guarded():
    plan = plan_rebalance(_cast(), random.Random(1))
    lust = sorted(g for g, _, new in plan.changes if new == "lust")
    assert lust == [1, 2, 3, 4]  # bold 9, outgoing 8, theatrical 7, outgoing 7
    assert all(g not in (5, 6) for g in lust)  # shy, guarded
    assert plan.lust == [1, 3, 2, 4]  # ranked: styled first, then extraversion


@pytest.mark.parametrize("household", [
    "lives with her nan in Leeds", "looks after her gran on Sundays",
    "inherited her grandmother's bakery", "her grandma taught her to knit",
    "lives with her grandad"])
def test_a_granddaughter_is_not_the_grandmother_type(household):
    young = person(1, ext=8, age=24, household=household)
    assert not none_fit(young) and none_fit_reason(young) is None
    plan = plan_rebalance([young] + [person(i, stance="fond", ext=3) for i in range(2, 6)],
                          random.Random(1))
    assert plan.lust == [1]  # she can still be picked for lust


@pytest.mark.parametrize("age,text,reason", [
    (40, "dotes on her grandkids", "grandchildren"),
    (45, "has two grandchildren", "grandchildren"),
    (44, "a grandson at uni and a granddaughter in school", "grandchildren"),
    (52, "", "age 52"),
    (58, "minds the grandbaby on weekends", "age 58, grandchildren")])
def test_the_grandmother_type(age, text, reason):
    p = person(1, age=age, household=text)
    assert none_fit(p) and none_fit_reason(p) == reason


def test_rebalance_keeps_a_woman_who_already_fits():
    plan = plan_rebalance(_cast(), random.Random(1))
    moved = {g for g, _, _ in plan.changes}
    assert 9 not in moved and 10 not in moved  # the grandmother types stay none


def test_rebalance_is_deterministic_with_the_seed():
    a = plan_rebalance(_cast(), random.Random(7))
    b = plan_rebalance(_cast(), random.Random(7))
    assert a == b


def test_lust_shortfall_never_takes_the_shy_or_guarded():
    cast = [person(i, ext=3, style="shy") for i in range(1, 6)] + \
           [person(i, ext=8, style="guarded") for i in range(6, 11)]
    plan = plan_rebalance(cast, random.Random(1))
    assert not [g for g, _, new in plan.changes if new == "lust"]
    assert plan.shortfall == {"lust": 2}


def test_lust_ranks_current_stance_then_style_then_extraversion():
    # two lust places; an outgoing none woman and an outgoing fond woman lose to two drawn
    # women, and the styled drawn introvert beats the unstyled drawn extravert
    cast = [person(1, stance="none", ext=9), person(2, stance="fond", ext=7),
            person(3, stance="drawn", style="earnest", ext=6),
            person(4, stance="drawn", style="competitive", ext=3),
            person(5, stance="fond", ext=3), person(6, stance="fond", ext=3),
            person(7, stance="drawn", style="shy", ext=3), person(8, stance="none", ext=2),
            person(9, stance="none", ext=2), person(10, stance="fond", ext=2)]
    for seed in range(10):
        plan = plan_rebalance(cast, random.Random(seed))
        assert plan.lust == [4, 3]
        assert counts(final_of(cast, plan).values()) == stance_targets(10)


def test_lust_may_come_from_any_stance():
    cast = ([person(1, stance="fond", ext=9, style="bold")]
            + [person(i, stance="fond", ext=3) for i in range(2, 5)]
            + [person(i, stance="drawn", ext=3, style="shy") for i in range(5, 8)]
            + [person(8, stance="none", ext=9, style="bold")]
            + [person(i, stance="none", ext=2) for i in range(9, 11)])
    plan = plan_rebalance(cast, random.Random(1))
    final = final_of(cast, plan)
    assert final[1] == "lust" and final[8] == "lust"
    assert counts(final.values()) == stance_targets(10)
    assert len(plan.changes) == 3 and not plan.shortfall  # a none woman refills fond


def test_an_unfit_lust_woman_is_moved_out():
    cast = ([person(1, stance="lust", style="shy", ext=3), person(2, ext=9, style="bold"),
             person(3, ext=8)]
            + [person(i, stance="drawn", style="earnest", ext=3) for i in range(4, 7)]
            + [person(i, stance="fond", ext=3) for i in range(7, 10)] + [person(10, ext=2)])
    plan = plan_rebalance(cast, random.Random(1))
    final = final_of(cast, plan)
    assert final[1] == "fond" and final[2] == final[3] == "lust"
    assert counts(final.values()) == stance_targets(10) and len(plan.changes) == 3


def _large_cast():
    """60 invented women: 13 seeds and 47 generated, with none far over its share and drawn
    short, the shape an older mix leaves behind."""
    def w(guid, name, stance, *, core, ext, style=None, age=34, household="", job=""):
        return person(guid, stance=stance, style=style, ext=ext, age=age, household=household,
                      job=job, core=core, name=name)
    cast = [w(1, "Belra", "drawn", core=True, ext=8, style="bold"),
            w(2, "Coros", "drawn", core=True, ext=7, style="theatrical"),
            w(3, "Dunna", "drawn", core=True, ext=5, style="competitive"),
            w(4, "Ilsa", "none", core=True, ext=7, age=46, job="does the books for a garden centre"),
            w(5, "Mirel", "none", core=True, ext=8, age=62,
              household="minds the grandkids most weekends"),
            w(6, "Tova", "drawn", core=True, ext=4, style="earnest")]
    cast += [w(10 + i, f"Sf{i}", "fond", core=True, ext=5) for i in range(5)]
    cast += [w(20 + i, f"Sn{i}", "none", core=True, ext=3) for i in range(2)]
    for i in range(24):
        cast.append(w(1000 + i, f"Gn{i}", "none", core=False,
                      ext=(9 - i % 4) if i < 6 else 2 + i % 3, age=52 if i == 0 else 34))
    for i in range(16):
        cast.append(w(1100 + i, f"Gf{i}", "fond", core=False,
                      ext=(9 - i % 4) if i < 5 else 2 + i % 3))
    styles = ["earnest", "earnest", "bold", "bold", "shy", "guarded", "earnest"]
    for i in range(7):
        cast.append(w(1200 + i, f"Gd{i}", "drawn", core=False, ext=7 if i < 2 else 3,
                      style=styles[i]))
    return cast


def test_a_large_mixed_cast():
    cast = _large_cast()
    assert len(cast) == 60
    by_name = {p.character_name: p.guid for p in cast}
    for seed in range(5):
        plan = plan_rebalance(cast, random.Random(seed))
        final = final_of(cast, plan)
        assert counts(final.values()) == stance_targets(60) and not plan.shortfall
        for name in ("Belra", "Coros", "Dunna"):  # the styled drawn seeds
            assert final[by_name[name]] == "lust", name
        assert final[by_name["Mirel"]] == "none"  # the grandmother type stays none
        assert final[by_name["Gn0"]] == "none"  # 52: none_fit too
        assert final[by_name["Ilsa"]] != "lust"  # an outgoing none woman ranks last
        assert by_name["Mirel"] not in plan.lust and by_name["Gn0"] not in plan.lust


def test_overrides_come_first_and_are_frozen():
    cast = _cast()
    plan = plan_rebalance(cast, random.Random(1), fixed={9: "lust", 1: "none"})
    final = final_of(cast, plan)
    assert final[9] == "lust" and final[1] == "none"
    assert plan.fixed == {9: "lust", 1: "none"}
    assert counts(final[p.guid] for p in cast if p.identity.player_gender == "female") \
        == stance_targets(20)
    assert sorted(plan.lust) == [2, 3, 4, 9]
    large = plan_rebalance(_large_cast(), random.Random(1), fixed={4: "none"})
    assert final_of(_large_cast(), large)[4] == "none"


@pytest.mark.parametrize("fixed", [{999: "lust"}, {30: "lust"}, {1: "smitten"}])
def test_overrides_are_validated(fixed):
    with pytest.raises(ValueError):
        plan_rebalance(_cast(), random.Random(1), fixed=fixed)


def test_misgendering_lines():
    for text in ("zan knows what she's doing", "Zanthor and her mage", "gm said she would",
                 "ZAN carried hers again"):
        assert misgenders(text, GM), text
    for text in ("she pulled again lol", "zan is a legend", "Belra and her pet",
                 "zanzibar her place"):
        assert not misgenders(text, GM), text
    she = dataclasses.replace(GM, pronouns="she")
    assert misgenders("zan said he would", she) and not misgenders("zan said she would", she)
    for pronouns in ("they", ""):  # unset reads as they: nothing is assumed
        assert not misgenders("zan said she would", dataclasses.replace(GM, pronouns=pronouns))


def test_plan_drops_misgendering_sample_lines_only():
    p = person(1, stance="fond", lines=("zan knows what she's doing", "ok pulling",
                                        "gm said she would"),
               approved_lines=("Zanthor and her raid team",))  # the owner's own line stays
    m = person(2, female=False, lines=("Zanthor and her mage lol", "ready"))
    plan = plan_rebalance([p, m], random.Random(1), human=GM)
    assert plan.dropped == {1: ("zan knows what she's doing", "gm said she would"),
                            2: ("Zanthor and her mage lol",)}
    updated = {x.guid: x for x in plan.updated([p, m])}
    assert [s.text for s in updated[1].sample_lines] == ["ok pulling", "Zanthor and her raid team"]
    assert [s.text for s in updated[2].sample_lines] == ["ready"]


# -- the CLI ---------------------------------------------------------------------------------

def _member(guid, name, rank=3, level=80):
    return Member(guid=guid, name=name, race=2, class_=1, gender=0, level=level, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


def _cli_store(tmp_path, cast, *, seed_rows=False):
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    s = Store(open_db(tmp_path / "mind.db"), clock=SystemClock())
    s.upsert_cast([_member(1, "Zanthor", rank=0, level=83)]
                  + [_member(p.guid, p.character_name) for p in cast], 1.0)
    for p in cast:
        save_persona(s, p, source="generated", ts=1.0, race="Orc", class_name="Warrior")
    if seed_rows:
        seed_relationships(s, cast, {}, 1, 5000.0)
    s.close()
    (tmp_path / "mind.toml").write_text('[human]\nnickname = "Zan"\npronouns = "he"\n',
                                        encoding="utf-8")


def _open(tmp_path):
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    return Store(open_db(tmp_path / "mind.db"), clock=SystemClock())


def _shifted():
    return [dataclasses.replace(p, guid=100 + p.guid, character_name=f"C{100 + p.guid}")
            for p in _cast()]


def _run(tmp_path, *args):
    from leeroy_mind.__main__ import main
    return main(["personas", "--data-dir", str(tmp_path), "rebalance-stances", *args])


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    cast = _shifted()
    cast[0] = persona_from_dict({**persona_to_dict(cast[0]), "sample_lines": [
        {"text": "zan knows what she's doing", "approved": False}]})
    _cli_store(tmp_path, cast)
    s = _open(tmp_path)
    before = s.persona_versions()
    s.close()
    assert _run(tmp_path, "--dry-run", "--seed", "1") == 0
    out = capsys.readouterr().out
    assert "C101" in out and "none -> lust" in out and "would change 8" in out
    # every lust pick with its old stance, style, extraversion and origin
    assert "lust: C101 (101) from none, style bold, extraversion 9, seed" in out
    assert "lust: C102 (102) from none, style -, extraversion 8, seed" in out
    assert "C101 (101) drops: zan knows what she's doing" in out  # verbatim
    # the women the grandmother rule keeps out of lust, with the reason
    assert "  C109 (109): age 62, grandchildren" in out and "  C110 (110): age 58" in out
    s = _open(tmp_path)
    assert s.persona_versions() == before
    s.close()


def test_cli_changes_only_the_stance_and_misgendering_lines(tmp_path, capsys):
    cast = _shifted()
    cast[0] = persona_from_dict({**persona_to_dict(cast[0]), "sample_lines": [
        {"text": "zan knows what she's doing", "approved": False},
        {"text": "pulling now", "approved": True}]})
    _cli_store(tmp_path, cast)
    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "changed 8" in out and "zan knows what she's doing" in out
    s = _open(tmp_path)
    try:
        versions = s.persona_versions()
        for old in cast:
            new = load_persona(s, old.guid)
            a, b = persona_to_dict(old), persona_to_dict(new)
            a["romance"].pop("stance"), b["romance"].pop("stance")
            a.pop("sample_lines"), b.pop("sample_lines")
            assert a == b
            changed = (new.romance.stance != old.romance.stance
                       or new.sample_lines != old.sample_lines)
            assert versions[old.guid] == (2 if changed else 1)
        assert [x.text for x in load_persona(s, 101).sample_lines] == ["pulling now"]
        women = [load_persona(s, p.guid) for p in cast
                 if p.identity.player_gender == "female"]
        assert counts(p.romance.stance for p in women) == stance_targets(20)
    finally:
        s.close()


def test_cli_set_overrides(tmp_path, capsys):
    _cli_store(tmp_path, _shifted())
    assert _run(tmp_path, "--set", "109=lust", "--set", "101=none") == 0
    out = capsys.readouterr().out
    assert "none -> lust (set)" in out and "none -> none (set)" in out
    s = _open(tmp_path)
    try:
        assert load_persona(s, 109).romance.stance == "lust"
        assert load_persona(s, 101).romance.stance == "none"
    finally:
        s.close()


@pytest.mark.parametrize("arg,why", [("999=lust", "not a woman in the cast"),
                                     ("130=lust", "not a woman in the cast"),
                                     ("101=smitten", "stance must be one of"),
                                     ("101", "expected GUID=STANCE")])
def test_cli_set_is_validated(tmp_path, capsys, arg, why):
    _cli_store(tmp_path, _shifted())
    s = _open(tmp_path)
    before = s.persona_versions()
    s.close()
    assert _run(tmp_path, "--set", arg) == 1
    assert why in capsys.readouterr().err
    s = _open(tmp_path)
    assert s.persona_versions() == before
    s.close()


def _reseed_cast():
    """10 women: two none women go to lust, three drawn women (the most outgoing) to fond."""
    return ([person(201, ext=9), person(202, ext=8, style="bold")]
            + [person(203 + i, stance="drawn", style="earnest", ext=e)
               for i, e in enumerate((5, 4, 3, 2, 2, 2))]
            + [person(209, stance="fond", ext=3), person(210, ext=2)])


def test_reseeds_are_rechecked_when_written(store):
    cast = _reseed_cast()
    seed_relationships(store, cast, {}, 1, 5000.0)
    plan = plan_rebalance(cast, random.Random(1))
    planned = plan_reseeds(store, plan.changes, 1)
    assert {r.guid for r in planned} == {201, 202, 203, 204, 205}
    # between the plan and the write, 203 talks to him and 205's row is nudged
    store.note_interaction(203, 1, 6000)
    store.put_relationship(dataclasses.replace(store.get_relationship(205, 1), affinity=31.0))
    lived, nudged = store.get_relationship(203, 1), store.get_relationship(205, 1)
    written, skipped = apply_reseeds(store, planned, 1)
    assert {r.guid for r in written} == {201, 202, 204}
    assert {r.guid for r in skipped} == {203, 205}
    assert store.get_relationship(203, 1) == lived and store.get_relationship(205, 1) == nudged
    assert store.get_relationship(201, 1) == dataclasses.replace(
        human_seed(201, 1, "lust"), last_seen=5000)


def test_cli_reseeds_unlived_rows_to_the_new_stance(tmp_path, capsys):
    cast = _reseed_cast()
    _cli_store(tmp_path, cast, seed_rows=True)
    s = _open(tmp_path)
    s.put_relationship(dataclasses.replace(s.get_relationship(202, 1), interactions=2))
    s.put_relationship(dataclasses.replace(s.get_relationship(204, 1), affinity=33.0))
    before = {g: s.get_relationship(g, 1) for g in range(201, 211)}
    s.close()

    assert _run(tmp_path, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "re-seed C201 (201) -> Zan: none -> lust" in out
    assert "re-seed C203 (203) -> Zan: drawn -> fond" in out
    assert "C202 (202) -> Zan" not in out and "C204 (204) -> Zan" not in out
    s = _open(tmp_path)
    assert {g: s.get_relationship(g, 1) for g in range(201, 211)} == before
    s.close()

    assert _run(tmp_path) == 0
    s = _open(tmp_path)
    try:
        final = {g: load_persona(s, g).romance.stance for g in range(201, 211)}
        assert final[201] == final[202] == "lust"
        assert [final[g] for g in (203, 204, 205)] == ["fond"] * 3
        for g, stance in ((201, "lust"), (203, "fond"), (205, "fond")):  # unlived: re-seeded
            assert s.get_relationship(g, 1) == dataclasses.replace(
                human_seed(g, 1, stance), last_seen=before[g].last_seen)
        assert s.get_relationship(203, 1).romance_stage == "none"  # down from notices
        for g in (202, 204, 206, 209, 210):  # lived, nudged or unchanged: untouched
            assert s.get_relationship(g, 1) == before[g]
    finally:
        s.close()


def test_the_seed_breaks_ties_among_equals():
    """Tests lens M5: a new --seed must be able to change who is picked among women who are
    equally fit, or the owner's re-run of the dry run shows the same list for nothing."""
    cast = [person(i, ext=8, style="bold") for i in range(1, 11)]
    picks = {tuple(plan_rebalance(cast, random.Random(s)).lust) for s in range(10)}
    assert len(picks) > 1


def test_cli_reports_a_reseed_skipped_because_the_row_changed(tmp_path, capsys, monkeypatch):
    """Carry L209: the CLI's "skipped re-seed" line had no test."""
    from leeroy_mind.personas import stances as stances_mod
    _cli_store(tmp_path, _reseed_cast(), seed_rows=True)
    real = stances_mod.plan_reseeds

    def plan_then_he_talks_to_her(store, changes, human_guid):
        planned = real(store, changes, human_guid)
        store.note_interaction(203, 1, 6000)  # between the plan and the write
        return planned
    monkeypatch.setattr(stances_mod, "plan_reseeds", plan_then_he_talks_to_her)
    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "skipped re-seed C203 (203): the row changed since the plan" in out
    assert "re-seeded 4 rows" in out
