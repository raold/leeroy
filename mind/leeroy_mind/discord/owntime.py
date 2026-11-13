"""Replies on her own time (one mind per member, 2026-11-12; `[talk] dm_own_time`).

A member answers his Discord DM when she would see it, by her Discord presence (presence.py):
- online, or in a conversation with him (her last line to him, in a whisper or this DM, within
  TALKING_S before his post): at once, as before (talk.answer_delay's 2-25 s);
- idle (at work, say): when she next glances at her phone, PHONE_S["idle"] after his post, or a
  little after she comes online, whichever is first;
- do not disturb: the same, with PHONE_S["dnd"];
- offline (asleep, or not about): a little (BACK_S) after her presence is next online or idle.
An urgent post (URGENT_RE) cuts any wait to URGENT_S, asleep included: a phone that keeps
buzzing wakes people. INSIST posts of his in a row cut an idle or dnd wait the same way.

Every draw comes from a generator seeded by his post's id and her guid, so a restart finds the
same moment. A DM still owed after LET_GO_S is let go. Nothing here reads the game: Discord is
not play (R11), and her body being online in the game changes nothing."""
import random
import re
from datetime import datetime, timedelta, timezone

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


def due_at(p, *, post_ts: float, now: float, local_now: datetime, message_id: int,
           urgent: bool, talking: bool, insist: bool) -> float:
    """When she answers his DM `message_id` posted at `post_ts` (see the module docstring); a
    time at or before `post_ts` means at once."""
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
        glance = post_ts + span(PHONE_S[state], glance_draw)
        back = _next(p, at, post_ts, frozenset({"online"}), glance)
        return glance if back is None else min(glance, back + span(BACK_S, back_draw))
    back = _next(p, at, post_ts, frozenset({"online", "idle"}), post_ts + SCAN_MAX_S)
    return (post_ts + SCAN_MAX_S if back is None else back) + span(BACK_S, back_draw)


def away_words(p, local: datetime) -> str:
    """Why she did not answer at once, by her presence when he wrote (owner-local `local`)."""
    state = state_at(p, local)
    hour = local_time(local, p.identity.region).hour
    if state == "offline":
        return "asleep" if hour >= 22 or hour < 10 else "away from your phone"
    if state in PHONE_S and local_time(local, p.identity.region).weekday() < 5 and 9 <= hour < 17:
        return "at work"
    return "busy"


def age_words(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} minutes"
    hours = round(seconds / 3600)
    return "an hour" if hours <= 1 else f"{hours} hours"


def late_note(p, *, post_ts: float, now: float, local_now: datetime, forms) -> str:
    """The instruction's note for an answer LATE_S or more after his post; "" sooner."""
    if now - post_ts < LATE_S:
        return ""
    why = away_words(p, shifted(local_now, post_ts - now))
    return (f"{forms[0].capitalize()} wrote this {age_words(now - post_ts)} ago while you were "
            f"{why}; you are only now seeing it, so answer as someone catching up would.")
