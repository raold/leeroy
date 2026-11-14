"""The playbooks keep the training ground's original scoring and the raid lead's stock calls
exactly: the 35 transcribed checks, the no-behind bosses, the watched buffs and the 18 calls,
compared with literal copies taken before the playbooks replaced them."""

from dataclasses import replace

from leeroy_mind.guild.boss_calls import BOSS_CALLS, BossCall, call_for
from leeroy_mind.training import protocol as p
from leeroy_mind.training.checks import CHECKS_VERSION, PLAYBOOK_CHECKS_VERSION, run_checks
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ledger import PullRecord
from leeroy_mind.training.mechanics import (BOSS_MECHANICS, NO_BEHIND, Mechanic, boss_mechanics,
                                            no_behind, watch_names)
from leeroy_mind.training.service import playbooks_on

# training/mechanics.py before the playbooks, verbatim.
OLD_BOSS_MECHANICS: dict[tuple[int, int], tuple[Mechanic, ...]] = {
    (631, 0): (
        Mechanic("avoid", ("Coldflame",), tag="coldflame"),
        Mechanic("avoid", ("Bone Storm",), roles=("ranged", "healer"), max_hits=6, tag="spread"),
        Mechanic("debuff", ("Impaled",), max_count=2)),
    (631, 1): (
        Mechanic("avoid", ("Death and Decay",), max_hits=1),
        Mechanic("adds", entries=(37890, 38393, 38628, 38629, 37949, 38394, 38625, 38626, 38222), max_share=.25),
        Mechanic("debuff", ("Dominate Mind",), max_count=1)),
    (631, 3): (
        Mechanic("debuff", ("Mark of the Fallen Champion",), max_count=3, scope="raid"),
        Mechanic("adds", entries=(38508, 38596, 38597, 38598), max_share=.15),
        Mechanic("spread", roles=("ranged",))),
    (631, 4): (
        Mechanic("buff", ("Inoculated",), min_stack=3, min_pull_s=150),
        Mechanic("avoid", ("Vile Gas",), roles=("ranged",), max_hits=1),
        Mechanic("spread", roles=("ranged",), exempt=("Gas Spore",), exempt_s=12)),
    (631, 5): (
        Mechanic("avoid", ("Slime Spray", "Unstable Ooze Explosion", "Sticky Ooze"), max_hits=1),),
    (631, 6): (
        Mechanic("avoid", ("Malleable Goo", "Slime Puddle", "Choking Gas Bomb"), max_hits=1),
        Mechanic("adds", entries=(37562, 38602, 38760, 38761, 37697, 38604, 38758, 38759), max_share=.2)),
    (631, 7): (
        Mechanic("avoid", ("Shock Vortex", "Empowered Shock Vortex"), max_hits=1),
        Mechanic("adds", entries=(38454, 38775, 38776, 38777), max_share=.1),
        Mechanic("spread", roles=("ranged",))),
    (631, 8): (
        Mechanic("avoid", ("Swarming Shadows",), max_hits=1),
        Mechanic("avoid", ("Bloodbolt Whirl",), max_hits=2),
        Mechanic("spread", roles=("ranged",), exempt=("Pact of the Darkfallen",), exempt_s=15)),
    (631, 11): (
        Mechanic("debuff", ("Instability",), max_stack=5),
        Mechanic("debuff", ("Unchained Magic",), max_count=1),
        Mechanic("avoid", ("Frost Bomb", "Blistering Cold")),
        Mechanic("spread", roles=("ranged",))),
    (631, 12): (
        Mechanic("avoid", ("Defile", "Shadow Trap")),
        Mechanic("debuff", ("Necrotic Plague",), max_count=1),
        Mechanic("adds", entries=(36609, 39120, 39121, 39122, 36701, 39302, 39303, 39304,
                                  37799, 39284, 39285, 39286), max_share=.2)),
    (649, 0): (
        Mechanic("avoid", ("Slime Pool", "Fire Bomb", "Trample"), max_hits=1),
        Mechanic("debuff", ("Paralytic Toxin", "Burning Bile"), max_count=1)),
    (649, 1): (
        Mechanic("avoid", ("Legion Flame", "Infernal Eruption"), max_hits=1),
        Mechanic("adds", entries=(34815, 35262, 35263, 35264, 34826, 35270, 35271, 35272), max_share=.2)),
    (649, 3): (Mechanic("avoid", ("Unleashed Light", "Unleashed Dark"), max_hits=2),),
    (649, 4): (
        Mechanic("debuff", ("Penetrating Cold",), max_count=1),
        Mechanic("avoid", ("Impale",))),
}


OLD_NO_BEHIND = frozenset({(631, 2), (631, 10), (649, 2)})

# guild/boss_calls.py before the playbooks, verbatim.
OLD_BOSS_CALLS: dict[tuple[int, int], BossCall] = {
    (631, 0): BossCall("marrowgar: spread out, kill spikes, run from coldflame"),
    (631, 1): BossCall("deathwhisper: kill adds fast, interrupt frostbolts"),
    (631, 2): BossCall("gunship: jump over, kill the mage, then back to the ship", False),
    (631, 3): BossCall("saurfang: ranged spread, kill blood beasts fast"),
    (631, 4): BossCall("festergut: ranged spread, stack on gas spores"),
    (631, 5): BossCall("rotface: kite the big ooze, merge the little ones"),
    (631, 6): BossCall("putricide: kite the oozes, stay out of puddles"),
    (631, 7): BossCall("princes: ranged spread, shoot the kinetic bombs"),
    (631, 8): BossCall("blood queen: spread, bite on time, take swarm out"),
    (631, 9): BossCall("svalna: grab a spear, free whoever she impales"),
    (631, 10): BossCall("valithria: heal her to full, dps kill the adds", False),
    (631, 11): BossCall("sindragosa: spread, hide behind ice tombs for bombs"),
    (631, 12): BossCall("lich king: stack for infest, move out of defile"),
    (649, 0): BossCall("beasts: kill the snakes, dodge icehowl's charge"),
    (649, 1): BossCall("jaraxxus: interrupt, kill portals and volcanoes"),
    (649, 2): BossCall("champions: focus the healers, cc what you can"),
    (649, 3): BossCall("twins: match your orb colour, swap on vortex"),
    (649, 4): BossCall("anub'arak: kite the spike over frost patches"),
}


def test_the_original_checks_are_unchanged():
    assert BOSS_MECHANICS == OLD_BOSS_MECHANICS
    assert list(BOSS_MECHANICS) == list(OLD_BOSS_MECHANICS)
    assert sum(len(rows) for rows in BOSS_MECHANICS.values()) == 35
    assert boss_mechanics(False) == OLD_BOSS_MECHANICS
    assert NO_BEHIND == no_behind(False) == OLD_NO_BEHIND
    assert watch_names() == watch_names(False) == ("Inoculated",)


def test_playbooks_add_rows_after_the_originals():
    new = boss_mechanics(True)
    for key, rows in OLD_BOSS_MECHANICS.items():
        assert new[key][:len(rows)] == rows
    assert {m for m, _ in new} == {533, 631, 649}
    assert (533, 2) in new and (533, 2) not in BOSS_MECHANICS
    assert no_behind(True) == OLD_NO_BEHIND | {(533, 4), (533, 9), (533, 11), (533, 12)}
    assert watch_names(True) == ("Inoculated",)


def test_the_stock_calls_are_unchanged():
    assert BOSS_CALLS == OLD_BOSS_CALLS
    assert list(BOSS_CALLS) == list(OLD_BOSS_CALLS)
    assert call_for(533, "Patchwerk")[1].pre == "Patchwerk next. drink up, then on the tank"


class Book:
    def __init__(self, *names):
        self.names = {name.casefold(): i + 1 for i, name in enumerate(names)}

    def ids(self, name):
        value = self.names.get(name.casefold())
        return frozenset((value,)) if value else frozenset()


def _record(map_id, boss, *members, seconds=20):
    h = p.Pull(ts=1000 + seconds * 1000, id=f"1000-77-{boss}", map=map_id, instance_id=77,
               boss_id=boss, difficulty=0, size=10, outcome="killed", start_ms=1000,
               end_ms=1000 + seconds * 1000, members=len(members), deaths=0, tracks=0, assisted=False,
               source="tracked", boss_entry=15932, upd_mean_ms=20, damage_basis="requested_pre_ai",
               spell_basis="best_effort_thread_pair")
    return PullRecord(h, tuple(replace(m, pull=h.id) for m in members), (), {}, (), True)


def _member(guid, **kw):
    return p.PullMember(**(dict(ts=0, pull="x", guid=guid, class_=1, spec_tab=2, tank=True, ilvl=232,
        damage=0, damage_boss=0, healing=0, overheal=0, taken=0, deaths=0, interrupts=0, active_ms=0,
        done=(), taken_by=(), debuffs=(), casts=()) | kw))


def test_naxx_is_scored_only_with_playbooks_on():
    book = Book("Mortal Wound")
    tank = _member(1101, debuffs=(p.DebuffTotal(spell=1, count=1, max_stack=8),))
    off = run_checks(_record(533, 2, tank), {1101: "tank"}, book)
    assert off.version == CHECKS_VERSION == 1
    assert "mechanics:generic_only" in off.coverage and not off.failures
    on = run_checks(_record(533, 2, tank), {1101: "tank"}, book, playbooks=True)
    assert on.version == PLAYBOOK_CHECKS_VERSION == 2
    assert "mechanics:generic_only" not in on.coverage
    assert [(f.kind, f.key, f.guid, f.count) for f in on.failures] == [("debuff", "Mortal Wound", 1101, 3)]
    assert run_checks(_record(533, 2, tank), {1101: "tank"}, book, playbooks=True).failed_mechanics == 1


def test_an_icc_pull_scores_the_same_with_playbooks_on():
    book = Book("Coldflame", "Bone Storm", "Impaled")
    m = _member(1101, tank=False, debuffs=(p.DebuffTotal(spell=3, count=4, max_stack=1),))
    off = run_checks(_record(631, 0, m), {1101: "ranged"}, book)
    on = run_checks(_record(631, 0, m), {1101: "ranged"}, book, playbooks=True)
    assert (off.failures, off.coverage, off.failed_mechanics) == (on.failures, on.coverage, on.failed_mechanics)
    assert off.failures and off.version == 1 and on.version == 2


def test_the_switch_is_off_by_default():
    assert TrainingConfig().playbooks is False
    assert playbooks_on(object()) is False
    assert playbooks_on(type("P", (), {"cfg": TrainingConfig(playbooks=True)})()) is True
    assert playbooks_on(type("P", (), {"cfg": TrainingConfig()})()) is False
