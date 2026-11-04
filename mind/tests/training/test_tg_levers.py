import pytest

from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.training.cards import Directive, LEVERS, Targets
from leeroy_mind.training.levers import MemberInfo, to_commands
from leeroy_mind.training.protocol import CustomStrategy, DcSetting


MEMBERS = {1: MemberInfo(1, "ranged", 8, 2), 101: MemberInfo(101, "tank", 1, 2),
           102: MemberInfo(102, "ranged", 8, 2), 103: MemberInfo(103, "healer", 5, 0),
           104: MemberInfo(104, "melee", 4, 0), 105: MemberInfo(105, "ranged", 9, 0)}


def commands(directive, **overrides):
    return to_commands(directive, MEMBERS, **(dict(tank_guid=101, human_guid=1,
        rotations={"tg_frost": ["often>frostbolt!6"]}) | overrides))


def test_strategy_commands_and_restores():
    applied, = commands(Directive("threat", "+", Targets(roles=("ranged",))))
    assert applied.command == BotDo("", (102, 105), "co", "+threat")
    assert applied.inverse == BotDo("", (102, 105), "restore", "threat")
    assert applied.keys == ((102, "threat"), (105, "threat"))


def test_inverse_restores_stock_and_siblings():
    aoe, = commands(Directive("dps aoe", "+"))
    assert aoe.inverse.param == "dps aoe"
    mana, = commands(Directive("save mana", "-"))
    assert mana.command == BotDo("", (103,), "co", "-save mana")
    assert mana.inverse.action == "restore"


def test_never_touches_p1ds_names_or_human():
    forbidden = ("behind", "boost", "tank face", "mark rti", "ss tank", "ss healer")
    for name, lever in LEVERS.items():
        directive = (Directive(name, "tg_frost", Targets(specs=((8, 2),))) if name == "rotation"
                     else Directive(name, lever.values[0]))
        for applied in commands(directive):
            for command in (applied.command, applied.inverse):
                if isinstance(command, BotDo):
                    assert 1 not in command.guids
                    assert not any(n in command.param for n in forbidden)
            assert applied.inverse is None or isinstance(applied.inverse, DcSetting) or applied.inverse.action == "restore"


def test_wait_for_attack_time_adds_its_strategy():
    applied = commands(Directive("wait for attack time", 6))
    assert [(a.command.action, a.command.param) for a in applied] == [
        ("co", "+wait for attack"), ("value", "wait for attack time 6")]
    assert [a.inverse.param for a in reversed(applied)] == ["wait for attack time", "wait for attack"]
    assert applied[0].keys == applied[1].keys


def test_formation_and_focus_use_values_with_restore():
    formation, = commands(Directive("formation", "circle"))
    assert formation.command.param == "formation circle"
    assert formation.inverse.param == "formation"
    focus = commands(Directive("focus tanks", "on"))
    assert [a.command.param for a in focus] == ["focus heal targets 101", "+focus heal targets"]
    assert all(a.command.guids == (103,) and a.inverse.param == "focus heal targets" for a in focus)


def test_dc_lever_scales_and_skips_when_human_leads():
    dc, = commands(Directive("PartyMaxSpread", 125))
    assert dc.command == DcSetting("", 101, "scale", "PartyMaxSpread", 125)
    assert dc.inverse == DcSetting("", 101, "reset", "PartyMaxSpread")
    assert dc.keys == ((101, "dc:PartyMaxSpread"),)
    assert not commands(Directive("PartyMaxSpread", 125), lead_is_human=True)
    assert not commands(Directive("PartyMaxSpread", 125), tank_guid=1)
    assert not commands(Directive("lust_at", 30))


def test_rotation_primes_then_adds_to_exact_spec():
    prime, add = commands(Directive("rotation", "tg_frost", Targets(specs=((8, 2),))))
    assert prime.command == CustomStrategy("", "tg_frost", 8, ("often>frostbolt!6",), 102)
    assert prime.inverse is None
    assert add.command == BotDo("", (102,), "co", "+custom::tg_frost")
    assert add.inverse.param == "custom::tg_frost"
    assert add.keys == ((102, "rotation"),)


@pytest.mark.parametrize("directive", [Directive("boost", "+"), Directive("threat", "-"),
    Directive("mana save level", True), Directive("rotation", "tg_missing", Targets(specs=((8, 2),))),
    Directive("save mana", "-", Targets(roles=("ranged",)))])
def test_invalid_directive_never_produces_commands(directive):
    with pytest.raises(ValueError):
        commands(directive)
