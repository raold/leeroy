"""Altered states: what a member has had tonight and how it shows. The state is rolled once
per session (the human's login) and advanced every 10 minutes; it scales typos, line length,
candour, typing delay and how much they talk, and gives the prompt one line about it, written
for real adults: it shows in how they type, and they needn't mention it (final fix wave).

A daily coffee habit is just their usual coffee most nights: only on CAFFEINE_WIRED_P of them
(seeded per member and night) does it make them wired, and wired is a mild lift whose line
varies (the rehearsal had 19 members read the same "wired" line every night)."""
import math
import random
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from leeroy_mind.clock import night_of
from leeroy_mind.personas.schema import Habit, Persona
from leeroy_mind.talk.turns import TurnKind


class Altered(StrEnum):
    SOBER = "sober"
    TIPSY = "tipsy"
    DRUNK = "drunk"
    HIGH = "high"
    WIRED = "wired"
    HUNGOVER = "hungover"


class Effects(NamedTuple):
    typo_mult: float
    length_mult: float
    candour: float
    delay_mult: float
    talk_mult: float


EFFECTS = {
    Altered.SOBER: Effects(1, 1, 1, 1, 1),
    Altered.TIPSY: Effects(1.5, 1.2, 1.3, 1.0, 1.3),
    Altered.DRUNK: Effects(2.5, 1.4, 1.6, 1.1, 1.5),
    Altered.HIGH: Effects(1.2, 1.3, 1.2, 1.6, 0.8),
    Altered.WIRED: Effects(1.1, 1.0, 1.0, 0.9, 1.1),
    Altered.HUNGOVER: Effects(1.0, 0.7, 0.8, 1.2, 0.6),
}

# Raid calls are typed straight whatever the night has been: no drunk typos or length, and no
# state line in the prompt.
RAID_CALLS = frozenset({TurnKind.CALLOUT, TurnKind.URGENT})

HANGOVER_P = 0.6
HANGOVER_WITHIN_S = 20 * 3600
TIPSY_FROM_HOUR = 22  # a drinker who logs in from 22:00 (until 06:00) starts tipsy
TIPSY_AFTER_MIN, TIPSY_P = 60, 0.5
DRUNK_AFTER_MIN, DRUNK_P = 150, 0.3
ONSET_P = 0.5  # weed or caffeine whose window opens mid-session, per 10-minute check
CAFFEINE_WIRED_P = 0.3  # nights a coffee habit makes them wired; the rest it's their usual
DAYS_OFF_SHARE = 3 / 7
# At most this share of the guild in any one altered state a night (fix wave G1).
STATE_SHARE, STATE_CAP_MIN = 0.06, 2

_STATE_OF = {"weed": Altered.HIGH, "caffeine": Altered.WIRED}
_SUBSTANCE_OF = {Altered.TIPSY: "alcohol", Altered.DRUNK: "alcohol",
                 Altered.HUNGOVER: "alcohol", Altered.HIGH: "weed", Altered.WIRED: "caffeine"}


def effects_for(state: Altered, kind: TurnKind | None = None) -> Effects:
    return EFFECTS[Altered.SOBER] if kind in RAID_CALLS else EFFECTS[Altered(state)]


def in_window(when: str, local: datetime, guid: int) -> bool:
    h, wd = local.hour, local.weekday()
    if when == "raid_nights":
        return h >= 17 or h < 3
    if when == "weekends":  # Friday 17:00 to Sunday 23:59
        return (wd == 4 and h >= 17) or wd in (5, 6)
    if when == "days_off":  # 3 days in 7, one answer per member per night
        return random.Random(f"{guid}:{night_of(local)}:off").random() < DAYS_OFF_SHARE
    if when == "after_midnight":
        return h < 4
    return when == "daily"


def _on_tonight(p: Persona, i: int, h: Habit, local: datetime) -> bool:
    """Whether habit i is on tonight: one seeded draw per member, night and habit, so the
    session roll and every later check agree."""
    return random.Random(f"{p.guid}:{night_of(local)}:{i}").random() < h.odds


def _wired_tonight(p: Persona, i: int, local: datetime) -> bool:
    """Whether coffee habit i makes them wired tonight: one seeded draw per member, night and
    habit (CAFFEINE_WIRED_P), never from the session's rng."""
    return random.Random(f"{p.guid}:{night_of(local)}:{i}:wired").random() < CAFFEINE_WIRED_P


def _active(p: Persona, local: datetime):
    """The habits that are on tonight and whose window is open now, in persona order; a coffee
    habit only on a night it makes them wired."""
    return [h for i, h in enumerate(p.habits)
            if in_window(h.when, local, p.guid) and _on_tonight(p, i, h, local)
            and (h.substance != "caffeine" or _wired_tonight(p, i, local))]


def _late(local: datetime) -> bool:
    return local.hour >= TIPSY_FROM_HOUR or local.hour < 6


def roll_session_state(p: Persona, local: datetime, last: tuple[Altered, float] | None,
                       now: float, rng: random.Random) -> Altered:
    """The state at login. `last` is the previous state and when it was last in effect
    (mind-clock seconds). The first active habit that changes the state wins."""
    if (last is not None and last[0] == Altered.DRUNK and now - last[1] < HANGOVER_WITHIN_S
            and rng.random() < HANGOVER_P):
        return Altered.HUNGOVER
    for h in _active(p, local):
        if h.substance in _STATE_OF:
            return _STATE_OF[h.substance]
        if h.substance == "alcohol" and _late(local):
            return Altered.TIPSY
    return Altered.SOBER


def advance_state(p: Persona, current: Altered, minutes_in: float, local: datetime,
                  rng: random.Random) -> Altered:
    """One 10-minute check. A drinker gets tipsy from 60 minutes in (p 0.5) and drunk from 150
    (p 0.3) only when a habit allows "drunk". Weed and caffeine whose window opens during the
    session take hold then (p 0.5). Every other state stays for the session."""
    current = Altered(current)
    if current is Altered.SOBER:
        for h in _active(p, local):
            if h.substance in _STATE_OF:
                if rng.random() < ONSET_P:
                    return _STATE_OF[h.substance]
            elif h.substance == "alcohol" and minutes_in >= TIPSY_AFTER_MIN:
                if rng.random() < TIPSY_P:
                    return Altered.TIPSY
        return current
    if current is Altered.TIPSY and minutes_in >= DRUNK_AFTER_MIN and any(
            h.substance == "alcohol" and h.max_state == "drunk" for h in _active(p, local)):
        if rng.random() < DRUNK_P:
            return Altered.DRUNK
    return current


def state_cap(members: int) -> int:
    """How many members may be in any one altered state on a night: STATE_SHARE of the
    guild, at least STATE_CAP_MIN (rehearsal 2: eleven members wired by the end)."""
    return max(STATE_CAP_MIN, math.ceil(STATE_SHARE * members))


def capped_states(states: dict[int, Altered], cap: int, night: str) -> dict[int, Altered]:
    """The night's states with at most `cap` members in each altered one: the members kept
    are drawn per night (the same ones on a restart), the rest are sober."""
    out = dict(states)
    for state in {s for s in states.values() if s is not Altered.SOBER}:
        members = sorted(g for g, s in states.items() if s is state)
        if len(members) > cap:
            keep = set(random.Random(f"{night}:{state.value}:cap").sample(members, cap))
            out.update({g: Altered.SOBER for g in members if g not in keep})
    return out


def substance_of(p: Persona, state: Altered, local: datetime | None = None) -> str | None:
    """What the state comes from, for state_line. A sober smoker is "tobacco", given `local`
    only on a night the habit is on and its window open (prompts lens, minor 8)."""
    if state in _SUBSTANCE_OF:
        return _SUBSTANCE_OF[state]
    habits = p.habits if local is None else _active(p, local)
    return "tobacco" if any(h.substance == "tobacco" for h in habits) else None


SHOWS = "It shows in how you type, and you don't have to mention it."
# Several wordings a state, picked per member and night (fix wave G1, from rehearsal 2: eleven
# members wired by the end, in three wordings).
_LINES = {
    Altered.TIPSY: ("You've had a couple of drinks: looser and chattier than usual.",
                    "A few beers in: easy-going and talkative.",
                    "You're a couple of drinks in and feeling good.",
                    "You've been drinking a little: relaxed and chatty."),
    Altered.DRUNK: ("You're drunk: looser, louder and more honest than you'd be sober.",
                    "You've had too much to drink: loud, sloppy and honest.",
                    "You're properly drunk tonight: no filter and big feelings."),
    Altered.HIGH: ("You're high: slower and easily amused.",
                   "You're stoned: everything's a bit funny and a bit slow.",
                   "You've smoked: mellow, distracted and easily amused."),
    Altered.WIRED: ("You're wired: quick and restless.", "You're buzzing tonight and can't sit still.",
                    "You're restless and quick tonight."),
    Altered.HUNGOVER: ("You're hungover from last night: short, tired and a bit grumpy.",
                       "Last night is catching up with you: tired and short-tempered.",
                       "Your head still hurts from last night: slow and grumpy."),
}
CAFFEINE_LINES = ("You've had a lot of coffee today.", "Too much coffee: you're a bit jittery.",
                  "You're running on energy drinks tonight.",
                  "Caffeinated, chatty and a little scattered.",
                  "You're on your fourth coffee and it shows.",
                  "A big mug of coffee is keeping you going.",
                  "You had an espresso too many after dinner.",
                  "You're running on coffee and not much sleep.")


def state_line(state: Altered, substance: str | None, *, variant: int = 0) -> str:
    """One line for the prompt, or "" for a plain sober night. `variant` picks the wording
    (the pipeline draws it per member and night)."""
    state = Altered(state)
    if state is Altered.SOBER:
        return "You smoke, and go afk for one now and then." if substance == "tobacco" else ""
    lines = (CAFFEINE_LINES if state is Altered.WIRED and substance == "caffeine"
             else _LINES[state])
    return f"{lines[variant % len(lines)]} {SHOWS}"
