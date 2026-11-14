"""Pure choices for the Discord server's pace, subjects and participants."""
import math
import random
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import is_group
from leeroy_mind.personas.schema import Persona
from leeroy_mind.talk.budget import DayPhase, day_phase
from leeroy_mind.talk.select import pick_speakers
from leeroy_mind.tuning.effective import dials_of, edits_of
from .seeds import Seed
from .state import DiscordState

PHASE_RATE = {DayPhase.MORNING: 0.6, DayPhase.DAY: 0.8, DayPhase.EVENING: 1.5,
              DayPhase.LATE: 1.0, DayPhase.DEAD: 0.3}
WEEKEND_DAY = 1.3
MIN_GAP_S, MAX_GAP_S = 300.0, 5400.0
LENGTH = (5, 20)
MAX_LIVE = 2
CHANNEL_BASE = {'general': 3.0, 'irl': 1.5, 'food': 1.0, 'pets': 0.6, 'pics': 0.3, 'memes': 0.4,
                'raid-planning': 1.0, 'strategy': 1.0, 'loot-and-dkp': 0.8, 'logs': 0.3,
                'pvp-arena': 0.4, 'professions-and-gold': 0.5, 'achievements-and-mounts': 0.5,
                'lfg-and-alts': 0.5, 'officer-chat': 0.8, 'applications': 0.2, 'bank-requests': 0.2}
CLASS_CHANNEL_BASE = 0.3


def starts_per_hour(local: datetime, *, per_hour: float, chattiness: float,
                    in_game: bool, in_game_share: float) -> float:
    phase = day_phase(local)
    return (max(0.0, per_hour) * PHASE_RATE[phase] *
            (WEEKEND_DAY if local.weekday() >= 5 and phase == DayPhase.DAY else 1.0) *
            max(0.0, chattiness) / 4 * (max(0.0, in_game_share) if in_game else 1.0))


def next_gap(rate: float, rng: random.Random) -> float:
    return min(MAX_GAP_S, max(MIN_GAP_S, rng.expovariate(rate/3600))) if rate > 0 else math.inf


def length(rng: random.Random) -> int:
    n = LENGTH[0]
    while n < LENGTH[1] and rng.random() < .8:
        n += 1
    return n


def _hangout(hangouts, seed: Seed, channel: str) -> float:
    """The mean of the owner's godmode hangout weights (tuning.edits) for this channel over
    the seed's free openers, or over every free member when anyone may open it (an unedited
    member weighs 1); 1.0 without any."""
    if not hangouts:
        return 1.0
    who = [g for g in (seed.openers if seed.openers is not None else hangouts) if g in hangouts]
    if not who:
        return 1.0
    return sum(hangouts[g].channel_weight(channel) for g in who) / len(who)


def choose_seed_and_channel(seeds: Sequence[Seed], *, local: datetime, officers_free: int,
                            rng: random.Random, hangouts=None) -> tuple[Seed, str] | None:
    """`hangouts`: every free member -> their godmode overlays (MemberEdits), given only when
    any has hangout weights; None leaves every weight as it was."""
    options, weights = [], []
    phase = day_phase(local)
    for seed in seeds:
        for index, name in enumerate(seed.channels):
            channel = layout.channel(name)
            if is_group(name):
                from leeroy_mind.groups.conversations import definition
                channel = definition(name)
            if (channel is None or channel.category == 'info' or seed.weight <= 0 or
                    channel.officers_only and officers_free < 2 or seed.officers_only and not channel.officers_only):
                continue
            weight = .3 if is_group(name) else CHANNEL_BASE.get(name, CLASS_CHANNEL_BASE if name in layout.CLASS_CHANNEL.values() else 0)
            weight *= seed.weight * (3 if index == 0 else 1.5)
            if name == 'irl' and (phase == DayPhase.LATE or local.weekday() >= 5):
                weight *= 2
            if name == 'food' and (11 <= local.hour <= 13 or 17 <= local.hour <= 20):
                weight *= 2
            if name == 'raid-planning' and seed.kind == 'raid_before':
                weight *= 3
            if name in {'loot-and-dkp', 'logs'} and seed.kind == 'raid_after':
                weight *= 3
            if hangouts:
                weight *= _hangout(hangouts, seed, name)
            if weight > 0:
                options.append((seed, name))
                weights.append(weight)
    return rng.choices(options, weights=weights, k=1)[0] if options else None


def choose_cast(channel: str, seed: Seed, states: Mapping[int, DiscordState],
                personas: Mapping[int, Persona], *, officers: frozenset[int], human: int,
                in_play: frozenset[int], ties: Callable[[int, int], float], rng: random.Random,
                classes: Mapping[int, int] | None = None, away: frozenset[int] = frozenset(),
                tease: Callable[[int, int], bool] | None = None,
                group_audience: frozenset[int] = frozenset()) -> list[int]:
    definition = layout.channel(channel)
    if is_group(channel):
        from leeroy_mind.groups.conversations import definition as group_definition
        definition = group_definition(channel)
    if definition is None or definition.category == 'info' or seed.officers_only and not definition.officers_only:
        return []
    audience = group_audience if is_group(channel) else layout.audience(channel, cast=personas, officers=officers, human=human)
    weights = {}
    for guid, p in personas.items():
        state = states.get(guid)
        if (guid == human or guid not in audience or guid in in_play or guid in away or state is None
                or state.presence.state not in {'online', 'idle'}):
            continue
        dials = dials_of(p)
        weight = dials.mult('talkative') * (3 if guid in seed.about else 1)
        if channel == 'strategy' or channel in layout.CLASS_CHANNEL.values():
            weight *= dials.mult('theorycraft')
        if channel == 'strategy':
            weight *= dials.mult('lore')
        if channel in {'irl', 'food'}:
            weight *= dials.mult('party')
        if channel in {'loot-and-dkp', 'general'}:
            weight *= dials.mult('drama')
        if classes is not None and layout.CLASS_CHANNEL.get(classes.get(guid)) == channel:
            weight *= 4
        weight *= edits_of(p).channel_weight(channel)  # godmode hangouts; 0: she avoids it
        weights[guid] = weight
    if sum(w > 0 for w in weights.values()) < 2:
        return []
    openers = {g:w for g,w in weights.items() if seed.openers is None or g in seed.openers}
    # A close loot award stings its runner-up: who talks about it passes the same kindness gate.
    butt = (seed.about[0] if seed.kind == 'joke' and seed.about else
            seed.about[1] if seed.kind == 'loot_close' and len(seed.about) > 1 else 0)
    if butt:
        openers = {g:w for g,w in openers.items() if g != butt and tease is not None and tease(g, butt)}
        weights = {g:w for g,w in weights.items() if g == butt or tease is not None and tease(g, butt)}
    starters = pick_speakers(openers, 1, rng)
    if not starters:
        return []
    first = starters[0]
    rest = {g: w * max(0, ties(first, g)) for g, w in weights.items() if g != first}
    chosen = [first]
    size = rng.randint(2, 5)
    while len(chosen) < size:
        eligible = {g:w for g,w in rest.items() if all(ties(g, other)>0 and ties(other, g)>0 for other in chosen)}
        drawn = pick_speakers(eligible, 1, rng)
        if not drawn:
            break
        chosen.extend(drawn)
        rest.pop(drawn[0])
    return chosen if len(chosen) >= 2 else []
