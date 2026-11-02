"""One supported tactic from positive observations, without invented causes."""

from .cards import Directive, Targets


# (failure, condition, directive, call); order breaks equal-evidence ties.
RULES = (
    ("spread", "", Directive("formation", "chaos", Targets(roles=("ranged",))), "ranged spread out more"),
    ("spread", "formation used", Directive("PartyMaxSpread", 125), "ranged spread out more"),
    ("avoid", "spread", Directive("PartyMaxSpread", 125), "ranged spread wider"),
    ("avoid", "melee", Directive("BossEngageRangeFloor", 120), "we pull it from further out"),
    ("aggro", "", Directive("wait for attack time", 6), "dps wait longer on the pull"),
    ("healer_oom", "", Directive("mana save level", 2, Targets(roles=("healer",))), "healers save mana"),
    ("tank_range", "", Directive("HealRepositionMaxRange", 75), "healers stay close to the tanks"),
    ("adds", "", Directive("dps aoe", "+", Targets(roles=("ranged",))), "ranged on the adds"),
    ("raid_damage", "lust_at > 0", Directive("lust_at", 0), "lust on the pull"),
)


def next_change(result, roles, *, used, lust_at=0):
    """Rank supplied positive evidence; incomplete coverage is not a pass.

    Failure.key == 'spread' is the explicit avoid tag contract. Unknown causes
    never become raid_damage. The current checks intentionally produce neither
    avoid damage nor causal death attribution until stronger evidence exists.
    """
    choices = []
    for failure in result.failures:
        if failure.count <= 0:
            continue
        for index, (kind, condition, directive, text) in enumerate(RULES):
            if kind != failure.kind or directive.lever in used:
                continue
            if condition == "formation used" and "formation" not in used:
                continue
            if condition == "spread" and failure.key != "spread":
                continue
            if condition == "melee" and (failure.key == "spread" or roles.get(failure.guid) != "melee"):
                continue
            choices.append((-failure.deaths, -failure.damage_share, index, directive, text))
            break
    if "raid_damage" in result.causes and lust_at > 0 and "lust_at" not in used:
        choices.append((-result.causes.count("raid_damage"), 0, len(RULES) - 1, *RULES[-1][2:]))
    return min(choices, key=lambda choice: choice[:3])[3:] if choices else None
