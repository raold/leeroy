"""Replies on her own time (one mind per member, 2026-11-12; `[talk] dm_own_time`).

A member answers his Discord DM when she would see it, by her Discord presence (presence.py):
- online, or in a conversation with him (her last line to him, in a whisper or this DM, within
  TALKING_S before his post), or in his WoW group while he plays (with_him: presence shows her
  online and playing then): at once, as before (talk.answer_delay's 2-25 s);
- idle (at work, say): when she next glances at her phone, PHONE_S["idle"] after his post, or a
  little after she comes online, whichever is first;
- do not disturb: the same, with PHONE_S["dnd"];
- offline (asleep, or not about): a little (BACK_S) after her presence is next online or idle.
An urgent post (URGENT_RE) cuts any wait to URGENT_S, asleep included: a phone that keeps
buzzing wakes people. INSIST posts of his in a row cut an idle or dnd wait the same way.

His DMs are answered promptly whatever the dial (owner, 2026-11-17: availability 6 had brought
back waits of 10-20 minutes at work and 72-86 on a do-not-disturb day): with `[life]
dm_wait_cap_s` above 0 (default 75) no wait for an idle or dnd member is longer than the cap,
and none for one asleep or not about longer than twice it, after the availability scaling. A
capped wait is drawn in the cap's upper half (CAP_SPAN), so answers do not all land on one
second. She still says where she was (the LATE shape, or late_note from LATE_OWN_S). `capped`
false (a group DM line that does not name her) keeps her own time.

With `[life] availability` above 0 (life/config.py) her presence is her day's (presence.py), the
glance and back-online waits are PHONE_S and BACK_S scaled by availability / 6 (never under
LIFE_FLOOR_S), and the words for where she was come from her day (life.presence.away_words).

Every draw comes from a generator seeded by his post's id and her guid, so a restart finds the
same moment. A DM still owed after LET_GO_S is let go. Nothing here reads the game: Discord is
not play (R11), and her body being online in the game changes nothing."""
import random
import re
from datetime import datetime, timedelta, timezone

from ..life import config as life_config
from ..life import presence as life_presence
from .presence import OWNER_ZONE, RNG_SEED, local_time, presence_of

URGENT_RE = re.compile(
    r"\b(urgent|asap|emergency|right now|call me|need (?:you|help)|help me|"
    r"(?:are|r) (?:you|u) (?:up|there|around|awake)|(?:you|u) (?:up|there|around|awake)\s*\?)",
    re.IGNORECASE)
TALKING_S = 600.0
INSIST = 3
PHONE_S = {"idle": (60.0, 1200.0), "dnd": (1200.0, 5400.0)}
BACK_S = (120.0, 900.0)
URGENT_S = (45.0, 300.0)
SCAN_STEP_S = 300.0
SCAN_MAX_S = 36 * 3600.0
LET_GO_S = 3 * 86400.0
LATE_S = 600.0  # an answer this long after his post says she is only now seeing it
LIFE_FLOOR_S = 30.0  # [life] availability's scaled waits are never shorter
CAP_SPAN = (0.5, 1.0)  # a capped wait, as a share of the cap
ASLEEP_CAP = 2.0  # asleep or not about: the cap times this
LATE_OWN_S = 45.0  # an own-time answer that waited this long says where she was


def _scaled(bounds: tuple[float, float]) -> tuple[float, float]:
    """A wait's bounds at the availability in force: today's at 0 and at the design point."""
    a = life_config.availability()
    if a <= 0:
        return bounds
    k = max(a, 1) / life_presence.DESIGN_AVAILABILITY
    return tuple(max(LIFE_FLOOR_S, b * k) for b in bounds)


def with_him(world, guid: int) -> bool:
    """She is in his WoW group while he plays (presence_of's in_his_group): she is at her
    keyboard with him, so his DM never waits on her schedule. Pass it as `due_at`'s talking."""
    return bool(world is not None and world.human_online and world.in_group(guid))


def shifted(local: datetime, seconds: float) -> datetime:
    """Owner-local `local` moved by `seconds` of real time, through UTC, so a daylight-saving
    change in between is honoured; a naive owner clock is US Eastern and stays naive."""
    aware = local if local.tzinfo else local.replace(tzinfo=OWNER_ZONE)
    moved = (aware.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(OWNER_ZONE)
    return moved.replace(tzinfo=None) if local.tzinfo is None else moved


def state_at(p, local: datetime) -> str:
    """Her Discord presence state at owner-local time `local` (no game body: Discord only)."""
    return presence_of(p, local=local_time(local, p.identity.region), body_online=False,
                       body_zone=0, rng_seed=RNG_SEED).state


def _next(p, at, start: float, states: frozenset, until: float) -> float | None:
    t = start
    while t <= until:
        if state_at(p, at(t)) in states:
            return t
        t += SCAN_STEP_S
    return None


def cap_for(state: str) -> float:
    """The longest wait for a member in presence `state`, seconds; 0: none ([life]
    dm_wait_cap_s 0, or she is online)."""
    cap = life_config.dm_wait_cap()
    if cap <= 0 or state == "online":
        return 0.0
    return float(cap) if state in PHONE_S else cap * ASLEEP_CAP


def due_at(p, *, post_ts: float, now: float, local_now: datetime, message_id: int,
           urgent: bool, talking: bool, insist: bool, capped: bool = True) -> float:
    """When she answers his DM `message_id` posted at `post_ts` (see the module docstring); a
    time at or before `post_ts` means at once. `capped`: [life] dm_wait_cap_s applies."""
    due = _due_at(p, post_ts=post_ts, now=now, local_now=local_now, message_id=message_id,
                  urgent=urgent, talking=talking, insist=insist)
    if not capped or due <= post_ts:
        return due
    return capped_due(p, due, post_ts=post_ts, now=now, local_now=local_now,
                      message_id=message_id)


def capped_due(p, due: float, *, post_ts: float, now: float, local_now: datetime,
               message_id: int, state: str = "") -> float:
    """`due` no later than [life] dm_wait_cap_s allows for her presence when he wrote (cap_for;
    `state` in its place: "idle" for a member who stepped away from her keyboard); a wait past
    the cap is drawn within CAP_SPAN of it, seeded as due_at's draws are."""
    cap = cap_for(state or state_at(p, shifted(local_now, post_ts - now)))
    if cap <= 0 or due - post_ts <= cap:
        return due
    lo, hi = CAP_SPAN
    draw = random.Random(f"own-time-cap:{message_id}:{p.guid}").random()
    return post_ts + cap * (lo + (hi - lo) * draw)


def _due_at(p, *, post_ts: float, now: float, local_now: datetime, message_id: int,
            urgent: bool, talking: bool, insist: bool) -> float:
    rng = random.Random(f"own-time:{message_id}:{p.guid}")
    glance_draw, back_draw, urgent_draw = (rng.random() for _ in range(3))

    def at(t: float) -> datetime:
        return shifted(local_now, t - now)

    def span(bounds, draw):
        lo, hi = bounds
        return lo + (hi - lo) * draw
    state = state_at(p, at(post_ts))
    if state == "online" or talking:
        return post_ts
    if urgent or (insist and state in PHONE_S):
        return post_ts + span(URGENT_S, urgent_draw)
    if state in PHONE_S:
        glance = post_ts + span(_scaled(PHONE_S[state]), glance_draw)
        back = _next(p, at, post_ts, frozenset({"online"}), glance)
        return glance if back is None else min(glance, back + span(_scaled(BACK_S), back_draw))
    back = _next(p, at, post_ts, frozenset({"online", "idle"}), post_ts + SCAN_MAX_S)
    return (post_ts + SCAN_MAX_S if back is None else back) + span(_scaled(BACK_S), back_draw)


def away_words(p, local: datetime) -> str:
    """Why she did not answer at once, by her presence when he wrote (owner-local `local`)."""
    if life_config.availability() > 0:
        her = local_time(local, p.identity.region)
        return life_presence.away_words(
            life_presence.presence_at(p, her, medium="discord"), "discord")
    state = state_at(p, local)
    hour = local_time(local, p.identity.region).hour
    if state == "offline":
        return "asleep" if hour >= 22 or hour < 10 else "away from your phone"
    if state in PHONE_S and local_time(local, p.identity.region).weekday() < 5 and 9 <= hour < 17:
        return "at work"
    return "busy"


def age_words(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes <= 1:
        return "a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = round(seconds / 3600)
    return "an hour" if hours <= 1 else f"{hours} hours"


def late_note(p, *, post_ts: float, now: float, local_now: datetime, forms,
              why: str = "", after: float = LATE_S) -> str:
    """The instruction's note for an answer `after` seconds (LATE_S) or more after his post; ""
    sooner. `why` says where she was in place of her presence then ("away": she stepped away
    mid-conversation, life/interrupt.py). An answer that waited for her own time passes
    LATE_OWN_S: the cap keeps every wait far under LATE_S, and she still says where she was."""
    if now - post_ts < after:
        return ""
    why = why or away_words(p, shifted(local_now, post_ts - now))
    return (f"{forms[0].capitalize()} wrote this {age_words(now - post_ts)} ago while you were "
            f"{why}; you are only now seeing it, so answer as someone catching up would.")
