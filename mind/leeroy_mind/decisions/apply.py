"""The kind registry: how each kind of decision is carried in game and whether the game can undo
it. Each kind registers its rules when its module attaches (Parts B and C of D4); a kind without
rules is never reversible, so his overrule stops only its future effect. Also the words every
reader shares (the card, the seeds, the console), built from a decision's stored value and its
effect references, never from model text."""
from collections.abc import Mapping
import importlib
import json
from typing import Protocol, runtime_checkable

from leeroy_mind.discord import layout
from .ledger import KINDS

# Kind modules the console imports to learn reversibility: "module:factory" (Parts B and C add theirs)
BUILTIN: tuple[str, ...] = ('leeroy_mind.decisions.lineup:lineup_rules',
                            'leeroy_mind.decisions.loot_rules:loot_rules')
# Bank requests and ranks are the officers' business; their posts live in officers' channels
OFFICER_KINDS = frozenset({'bank', 'rank'})
# Kinds an officers' poll settles: who argued what stays with the officers until the poll is linked
POLLED_KINDS = frozenset({'loot_rule', 'rank'})
SUMMARY_CHARS = 160
LABELS = {'lineup': 'Raid lineup', 'loot_rule': 'Loot rule', 'bank': 'Bank request',
          'strategy': 'Boss strategy', 'rank': 'Rank change'}
STATES = {'proposed': 'proposed', 'settled': 'settled', 'in_effect': 'in effect', 'talk_only': 'talk only',
          'overruled': 'overruled by him', 'superseded': 'superseded', 'expired': 'expired', 'failed': 'failed'}
EFFECT_LABELS = {'refs': 'direct ledger', 'ledger': 'DKP ledger rows', 'receipts': 'receipts', 'raid': 'raid',
                 'rank': 'rank', 'layer': 'training layer', 'roster': 'add-from-guild roster'}


@runtime_checkable
class KindRules(Protocol):
    kind: str

    def reversible(self, row) -> bool: ...

    async def undo(self, app, row) -> None: ...  # only when reversible; restores the prior state


_RULES: dict[str, KindRules] = {}


def register(rules: KindRules) -> None:
    if getattr(rules, 'kind', None) not in KINDS:
        raise ValueError('decision_kind_invalid')
    _RULES[rules.kind] = rules


def unregister(kind: str) -> None:
    _RULES.pop(kind, None)


def rules_for(kind: str) -> KindRules | None:
    return _RULES.get(kind)


def load_builtin() -> None:
    """Register the shipped kinds (a reader outside the mind, such as the console, asks this)."""
    for spec in BUILTIN:
        module, _, factory = spec.partition(':')
        rules = getattr(importlib.import_module(module), factory)()
        if rules.kind not in _RULES:
            register(rules)


def reversible(row) -> bool:
    """Whether the game can undo what this decision changed: only an in-effect one whose kind says so."""
    rules = rules_for(row['kind'])
    try:
        return bool(row['status'] == 'in_effect' and rules is not None and rules.reversible(row))
    except Exception:
        return False


def value_of(row) -> dict:
    try:
        value = json.loads(row['value'])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def effect_of(row) -> dict:
    try:
        value = json.loads(row['effect_ref']) if row['effect_ref'] else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def private_channel(channel: str | None) -> bool:
    """An officers' channel (a poll held there keeps its votes and positions with the officers)."""
    found = layout.channel(channel) if isinstance(channel, str) else None
    return bool(found is not None and found.officers_only)


def officers_only(row, message_channel: str | None = None) -> bool:
    """A bank request or a rank change, or a decision announced in an officers' channel. A rule the
    officers voted on in private but announced to the guild (the loot rule) is the guild's; who
    voted what stays with the officers (private_channel of its poll)."""
    return row['kind'] in OFFICER_KINDS or private_channel(message_channel)


def positions_private(row, message_channel: str | None, poll_channel: str | None) -> bool:
    """Whether who argued what on this decision is the officers' alone: its business is theirs, its
    poll is held in their channel, or it is a kind their poll settles and no poll is linked yet
    (its audience is not known, so it fails closed)."""
    return (officers_only(row, message_channel) or private_channel(poll_channel)
            or row['kind'] in POLLED_KINDS and not poll_channel)


def channels_of(conn, row) -> tuple[str | None, str | None]:
    """(the announcing message's channel, the poll's channel), either None."""
    message = conn.execute('SELECT channel FROM discord_message WHERE id=?', (row['message'],)).fetchone() \
        if row['message'] else None
    poll = conn.execute('SELECT channel FROM guild_poll WHERE id=?', (row['poll'],)).fetchone() if row['poll'] else None
    return (message[0] if message else None), (poll[0] if poll else None)


def _guids(value, key) -> list[int]:
    items = value.get(key, ())
    return [g for g in items if type(g) is int] if isinstance(items, (list, tuple)) else []


def stakeholders(row) -> frozenset[int]:
    """Who a decision touches: its positions, lineup members and bench, a requester, a rank's member."""
    value, out = value_of(row), set()
    out.update(_guids(value, 'members'))
    out.update(_guids(value, 'bench'))
    for key in ('guid', 'requester', 'lead', 'tank', 'loot'):
        if type(value.get(key)) is int:
            out.add(value[key])
    if row['kind'] == 'rank' and row['subject'].startswith('rank:'):
        out.add(int(row['subject'][5:]))
    try:
        for p in json.loads(row['positions']):
            if isinstance(p, Mapping) and type(p.get('guid')) is int:
                out.add(p['guid'])
    except (TypeError, ValueError):
        pass
    return frozenset(g for g in out if 0 < g <= 0xFFFFFFFF)


def touches(row, guid: int) -> bool:
    """A guild-wide rule (the loot rule, a boss strategy) touches every member; the rest touch theirs."""
    return row['kind'] in ('loot_rule', 'strategy') or guid in stakeholders(row)


def _word(value) -> str:
    text = str(value)
    return text if len(text) <= 40 and text.isprintable() else '?'


def summary(row, name=lambda g: f'#{g}', guid: int = 0) -> str:
    """What was decided, from the stored value: codes, counts and names, never free text; at most
    SUMMARY_CHARS, cut with an ellipsis."""
    text = _summary(row, name, guid)
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1].rstrip() + '…'


def _summary(row, name, guid):
    value, kind = value_of(row), row['kind']
    if kind == 'lineup':
        members, bench = _guids(value, 'members'), _guids(value, 'bench')
        text = f'{row["subject"].replace("raid:", "raid night #")}: {len(members)} in, {len(bench)} on the bench'
        if guid in members:
            text += '; you are in it'
        elif guid in bench:
            text += '; you are on the bench'
        return text
    if kind == 'loot_rule':
        text = 'mode ' + _word(value.get('mode', 'dkp'))
        first = value.get('role_first')
        if isinstance(first, Mapping) and first:
            text += '; ' + ', '.join(f'{_word(s)}: ' + '>'.join(_word(r) for r in roles)
                                     for s, roles in sorted(first.items()) if isinstance(roles, (list, tuple)))
        return text
    if kind == 'bank':
        who = value.get('guid') if type(value.get('guid')) is int else value.get('requester')
        what = value.get('kind', 'request')
        count = value.get('count')
        return (f'{_word(what)}' + (f' x{count}' if type(count) is int else '')
                + (f' for {name(who)}' if type(who) is int else ''))
    if kind == 'strategy':
        return f'{row["subject"].removeprefix("strategy:")}: {_word(value.get("change", "change"))}'
    if kind == 'rank':
        member = int(row['subject'][5:]) if row['subject'][5:].isdecimal() else 0
        rank = value.get('rank')
        return f'{name(member)} to rank {rank}' if type(rank) is int else f'{name(member)}'
    return row['subject']


def effects(row) -> list[str]:
    """What changed in game, from effect_ref, rendered by kind for the console."""
    ref = effect_of(row)
    out = []
    if 'talk_only' in ref:
        out.append('talk only: ' + _word(ref['talk_only']))
    if 'failed' in ref:
        out.append('failed: ' + _word(ref['failed']))
    for key, value in sorted(ref.items()):
        if key in ('talk_only', 'failed'):
            continue
        label = EFFECT_LABELS.get(key, key.replace('_', ' '))
        if isinstance(value, (list, tuple)):
            shown = ', '.join(_word(v) for v in value[:8]) + (f' (+{len(value) - 8})' if len(value) > 8 else '')
        elif isinstance(value, Mapping):
            shown = ', '.join(f'{_word(k)} {_word(v)}' for k, v in list(value.items())[:8])
        else:
            shown = _word(value)
        out.append(f'{label}: {shown}')
    return out
