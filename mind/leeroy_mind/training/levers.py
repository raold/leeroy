"""Pure, bounded tactic-to-command translation. Restoration uses server journals."""

from dataclasses import dataclass
import re

from leeroy_mind.guild.protocol import BotDo

from .cards import Directive, LEVERS, _targets_ok
from .protocol import CustomStrategy, DcSetting


@dataclass(frozen=True, slots=True)
class MemberInfo:
    guid: int
    role: str
    cls: int
    spec_tab: int


@dataclass(frozen=True, slots=True)
class Applied:
    command: object
    inverse: object | None
    keys: tuple[tuple[int, str], ...]


def to_commands(d: Directive, members: dict[int, MemberInfo], *, tank_guid: int,
                human_guid: int, rotations: dict[str, list[str]],
                lead_is_human: bool = False) -> list[Applied]:
    """Translate one already vocabulary-checked directive, validating its shape too.

    Logical keys deliberately group both wait commands, both focus commands, and
    all rotation names. Different rotation names cannot bypass layer precedence.
    """
    lever = LEVERS.get(d.lever) if isinstance(d, Directive) else None
    if lever is None or not _targets_ok(d.targets, lever):
        raise ValueError("invalid training directive")
    if lever.kind == "rotation":
        valid = (type(d.value) is str and re.fullmatch(r"tg_[a-z0-9_]{1,40}", d.value)
                 and d.value in rotations)
        if valid:
            lines = rotations[d.value]
            valid = (isinstance(lines, (list, tuple)) and 1 <= len(lines) <= 12
                     and all(type(line) is str and len(line) <= 120
                             and re.fullmatch(r"[a-z0-9 ]{1,40}>[a-z0-9 ]{1,40}![5-9](?:\.[0-9])?", line)
                             for line in lines))
    else:
        valid = any(type(d.value) is type(v) and d.value == v for v in lever.values)
    if not valid:
        raise ValueError("invalid training value")
    if lever.kind == "call":
        return []
    if lever.kind == "dc":
        if lead_is_human or tank_guid == human_guid or tank_guid not in members:
            return []
        return [Applied(DcSetting("", tank_guid, "scale", d.lever, d.value),
                        DcSetting("", tank_guid, "reset", d.lever),
                        ((tank_guid, "dc:" + d.lever),))]
    guids = tuple(sorted(g for g, m in members.items() if g != human_guid and m.guid == g
        and (not lever.roles or m.role in (d.targets.roles or lever.roles))
        and (not d.targets.classes or m.cls in d.targets.classes)
        and (not d.targets.specs or (m.cls, m.spec_tab) in d.targets.specs)))
    if not guids:
        return []
    keys = tuple((g, d.lever) for g in guids)

    def bot(action, param, restore):
        return Applied(BotDo("", guids, action, param), BotDo("", guids, "restore", restore), keys)

    if lever.kind == "strategy":
        return [bot("co", d.value + d.lever, d.lever)]
    if d.lever == "wait for attack time":
        return [bot("co", "+wait for attack", "wait for attack"),
                bot("value", f"wait for attack time {d.value}", "wait for attack time")]
    if d.lever == "focus tanks":
        tanks = sorted(g for g, m in members.items() if m.role == "tank")
        if not tanks:
            return []
        if len(tanks) > 5:
            raise ValueError("focus targets exceed the protocol bound")
        return [bot("value", "focus heal targets " + ",".join(map(str, tanks)), "focus heal targets"),
                bot("co", "+focus heal targets", "focus heal targets")]
    if lever.kind == "rotation":
        return [Applied(CustomStrategy("", d.value, d.targets.specs[0][0],
                                       tuple(rotations[d.value]), guids[0]), None, ()),
                bot("co", "+custom::" + d.value, "custom::" + d.value)]
    return [bot("value", f"{d.lever} {d.value}", d.lever)]
