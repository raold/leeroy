from leeroy_mind.training.cards import Directive, Targets
from leeroy_mind.training.checks import CheckResult, Failure
from leeroy_mind.training.lines import TEMPLATES, template
from leeroy_mind.training.playbook import next_change


def result(*failures, causes=()):
    return CheckResult(tuple(failures), causes, len(failures))


def test_playbook_targets_the_top_cause():
    value = result(Failure("aggro", "aggro", 101, 2, 0, .8),
                   Failure("spread", "spread", 102, 5, 2, .1))
    assert next_change(value, {}, used=set()) == (
        Directive("formation", "chaos", Targets(roles=("ranged",))), "ranged spread out more")


def test_playbook_one_change_per_lever_a_night():
    value = result(Failure("spread", "spread", 102, 4, 0, 0))
    assert next_change(value, {}, used={"formation"})[0] == Directive("PartyMaxSpread", 125)
    assert next_change(value, {}, used={"formation", "PartyMaxSpread"}) is None


def test_playbook_none_when_nothing_fits():
    assert next_change(result(Failure("behind", "behind", 101, 4, 0, 0)), {}, used=set()) is None
    assert next_change(result(causes=("unknown",)), {}, used=set(), lust_at=35) is None


def test_playbook_positive_observations_work_with_incomplete_coverage():
    value = result(Failure("healer_oom", "healer_oom", 101, 10, 0, 0))
    assert not value.complete
    assert next_change(value, {101: "healer"}, used=set())[0] == Directive(
        "mana save level", 2, Targets(roles=("healer",)))
    assert next_change(result(Failure("aggro", "aggro", 101, 0, 0, 0)), {}, used=set()) is None


def test_playbook_avoid_requires_explicit_tag_or_melee_role():
    value = result(Failure("avoid", "spread", 101, 1, 0, .1))
    assert next_change(value, {}, used=set())[0] == Directive("PartyMaxSpread", 125)
    value = result(Failure("avoid", "Coldflame", 101, 1, 0, .1))
    assert next_change(value, {101: "ranged"}, used=set()) is None
    assert next_change(value, {101: "melee"}, used=set())[0] == Directive("BossEngageRangeFloor", 120)


def test_lust_rule_uses_explicit_current_value():
    value = result(causes=("raid_damage",))
    assert next_change(value, {}, used=set()) is None
    assert next_change(value, {}, used=set(), lust_at=35) == (Directive("lust_at", 0), "lust on the pull")
    assert next_change(value, {}, used={"lust_at"}, lust_at=35) is None
    value = result(Failure("spread", "spread", 101, 5, 0, 0), causes=("raid_damage",))
    assert next_change(value, {}, used=set(), lust_at=35)[0] == Directive("lust_at", 0)


def test_every_template_formats():
    facts = dict(boss="saurfang", when="yesterday", call="spread", what="three pulls")
    assert all("{" not in template(key, **facts) for key in TEMPLATES)
    assert template("lust_now") == "lust now"
