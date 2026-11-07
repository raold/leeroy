"""How many photos the guild sends (P4): the selfies world dial's ration and each member's odds."""
from __future__ import annotations

import math

from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.romance import FOND_SHARE, WHISPER_WEIGHT, forward
from leeroy_mind.tuning.effective import dials_of

PER_WEEK = (0, 1, 2, 3, 5, 7, 10, 14, 20, 28, 40)
MEMBER_STOCK = 2
MEMBER_GAP_S = 172800.0
WEEK_S = 604800.0
DAY_S = 86400.0
BASE = {"whisper": 0.35, "reply": 0.25, "guild": 0.04, "guild_photo": 0.5}
COLD_MULT = 0.25
EX_MULT = 0.1
CHANCE_CAP = 0.9


def per_week(dial: int) -> int:
    return PER_WEEK[max(0, min(10, dial))]


def per_day(dial: int) -> int:
    week = per_week(dial)
    return 0 if week == 0 else max(1, math.ceil(week / 3))


def stock_target(dial: int) -> int:
    return min(40, 2 * per_week(dial))


def send_weight(p, rel: Relationship | None) -> float:
    """The talkative dial x (1 + her flirt) x the chill: 0.25 when they feel cold toward him,
    0.1 when she is his ex. Her flirt is the whisper stance weight x her forward dial / 3."""
    flirt = 0.0
    if p.identity.player_gender == "female":
        stance = p.romance.stance
        if stance == "fond":
            weight = FOND_SHARE * WHISPER_WEIGHT["lust"]
        else:
            weight = WHISPER_WEIGHT.get(stance, 0.0)
        flirt = weight * forward(p) / 3
    chill = 1.0
    if rel is not None:
        if rel.affinity < 0:
            chill *= COLD_MULT
        if rel.romance_stage == "ex":
            chill *= EX_MULT
    return dials_of(p).mult("talkative") * (1 + flirt) * chill


def chance(context: str, weight: float) -> float:
    return min(CHANCE_CAP, BASE[context] * weight)


def room(dial: int, *, week: int, day: int) -> bool:
    return week < per_week(dial) and day < per_day(dial)
