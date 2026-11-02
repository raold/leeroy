"""Roles from current talents; guild responsibilities from persona titles."""

import re


_SPECS = {
    1: ("arms", "fury", "protection"),
    2: ("holy", "protection", "retribution"),
    3: ("beast mastery", "marksmanship", "survival"),
    4: ("assassination", "combat", "subtlety"),
    5: ("discipline", "holy", "shadow"),
    6: ("blood", "frost", "unholy"),
    7: ("elemental", "enhancement", "restoration"),
    8: ("arcane", "fire", "frost"),
    9: ("affliction", "demonology", "destruction"),
    11: ("balance", "feral", "restoration"),
}
_PREMADE = {
    "arms": "arms", "fury": "fury", "protection": "prot", "holy": "holy",
    "retribution": "ret", "beast mastery": "bm", "marksmanship": "mm", "survival": "surv",
    "assassination": "as", "combat": "combat", "subtlety": "subtlety", "discipline": "disc",
    "shadow": "shadow", "blood": "blood", "frost": "frost", "unholy": "unholy",
    "elemental": "ele", "enhancement": "enh", "restoration": "resto", "arcane": "arcane",
    "fire": "fire", "affliction": "affli", "demonology": "demo", "destruction": "destro",
    "balance": "balance",
}


def role_of(cls: int, spec_tab: int, raid_role: str | None) -> str:
    if spec_tab == -1:
        return raid_role if raid_role in {"tank", "healer"} else "damage"
    if cls == 11 and spec_tab == 1:
        return "tank" if raid_role == "tank" else "damage"
    if (cls, spec_tab) in {(1, 2), (2, 1), (6, 0)}:
        return "tank"
    if (cls, spec_tab) in {(5, 0), (5, 1), (2, 0), (7, 2), (11, 2)}:
        return "healer"
    return "damage"


def stat_family(cls, spec_tab, role) -> str:
    if cls in {1, 6} or cls == 2 and spec_tab in {1, 2}:
        return "str"
    if cls in {3, 4} or cls in {7, 11} and spec_tab == 1:
        return "agi"
    return "int"


def _title(persona):
    return persona.play.officer_title.casefold()


def _officer(persona):
    return persona.play.guild_role == "officer"


def is_raid_lead(persona) -> bool:
    return _officer(persona) and "raid lead" in _title(persona)


def is_loot_officer(persona) -> bool:
    return _officer(persona) and any(word in _title(persona) for word in ("loot", "dkp"))


def is_cook(persona) -> bool:
    return re.search(r"\b(baker|chef|cook)\b", _title(persona)) is not None


def is_pvp_lead(persona) -> bool:
    return _officer(persona) and "pvp" in _title(persona)


def is_achievement_hunter(persona) -> bool:
    return "achievement" in _title(persona)


def reliability(persona) -> int:
    return persona.big_five.conscientiousness.score


def spec_tab_for(cls, main_spec, raid_role) -> int | None:
    if not isinstance(main_spec, str):
        return None
    name = " ".join(main_spec.casefold().split())
    tabs = _SPECS.get(cls, ())
    return tabs.index(name) if name in tabs else None


def premade_spec_no(cls, main_spec, raid_role, premade: dict[int, list[str]]) -> int | None:
    tab = spec_tab_for(cls, main_spec, raid_role)
    if tab is None:
        return None
    name = _SPECS[cls][tab]
    prefix = ("bear" if raid_role == "tank" else "cat") if name == "feral" else _PREMADE[name]
    for index, candidate in enumerate(premade.get(cls, ())):
        text = candidate.strip().casefold()
        if text.startswith(prefix) and re.search(r"\bpve\b", text):
            return index
    return None
