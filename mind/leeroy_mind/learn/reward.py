"""Bounded, read-only reward calculation from numeric evidence; no text is returned."""
from dataclasses import dataclass
from datetime import datetime
import math
from statistics import median
import time
from leeroy_mind.signals.stage import SCREENED_CHANNELS as GAME_SCREENED

from leeroy_mind.discord.channels import discord_sql, is_discord, is_private, private_sql
from .weights import available, read
from .generation import lines, observations

DAY = 86400
BASELINE_DAYS = 14
BASELINE_LIMIT = 2048
MEMBER_DAILY_CAP = 10.0
MEMBER_DECAY = 1 - 1 / MEMBER_DAILY_CAP
HALF_LIFE_DAYS = 21
METRICS = ("replied", "speed", "length", "continued")
JUDGES = ("judge_repetition", "judge_nonsequitur", "judge_unawareness", "judge_contradiction")


@dataclass(frozen=True)
class Reward:
    value: float
    parts: dict[str, float]
    weights_version: int = 0
    cap_weight: float = 1.


def channel_kind(channel):
    if is_discord(channel):
        return "dm" if is_private(channel) else "discord"
    if channel.startswith("voice:"):
        return "voice"
    return {"raid_warning": "raid", "say": "local", "yell": "local"}.get(channel, channel
        if channel in {"guild", "officer", "party", "raid", "whisper", "emote"} else "other")


def _scope(kind):
    discord = discord_sql("c.channel")
    if kind in ("discord", "dm"):
        private = private_sql("c.channel")
        return f"({discord}) AND {'NOT ' if kind == 'discord' else ''}({private})", ()
    if kind == "voice":
        return f"NOT ({discord}) AND substr(c.channel,1,6)='voice:'", ()
    channels = {"raid": ("raid", "raid_warning"), "local": ("say", "yell")}.get(kind, (kind,))
    return f"NOT ({discord}) AND c.channel IN ({','.join('?' for _ in channels)})", channels


def _metrics(signals):
    out = {}
    if "replied" in signals:
        out["replied"] = max(0., signals["replied"])
    if "reply_latency_s" in signals:
        out["speed"] = 1 - max(0., signals["reply_latency_s"])
    if "reply_len" in signals:
        out["length"] = max(0., signals["reply_len"])
    if "thread_continued" in signals:
        out["continued"] = max(0., signals["thread_continued"])
    return out


def _baseline(conn, line_id, ts, channel):
    condition, params = _scope(channel_kind(channel))
    rows = conn.execute(
        "SELECT c.id,s.kind,s.value FROM (SELECT c.id,c.ts FROM chat_line c "
        "WHERE c.origin='mind' AND c.ts>=? AND (c.ts<? OR (c.ts=? AND c.id<?)) "
        "AND (CAST(c.ts/3600 AS INTEGER)%24)=? AND " + condition +
        " AND EXISTS(SELECT 1 FROM talk_signal s WHERE s.line_id=c.id AND s.source='implicit' "
        "AND s.kind IN ('replied','ignored','reply_latency_s','reply_len','thread_continued') AND s.ts<?) "
        "ORDER BY c.ts DESC,c.id DESC LIMIT ?) c JOIN talk_signal s ON s.line_id=c.id "
        "WHERE s.source='implicit' AND s.ts<?",
        (ts - BASELINE_DAYS * DAY, ts, ts, line_id, int(ts // 3600) % 24, *params,
         ts, BASELINE_LIMIT, ts)).fetchall()
    grouped = {}
    for lid, kind, value in rows:
        grouped.setdefault(lid, {})[kind] = value
    samples = [_metrics(values) for values in grouped.values()]
    return {key: [s[key] for s in samples if key in s] for key in METRICS}


def decayed(value, *, age_seconds):
    """Read-time age discount for reports/consumers, separate from raw line value."""
    return value * 2 ** (-max(0., age_seconds) / (HALF_LIFE_DAYS * DAY))


def reward(conn, line_id, *, now=None, schema_available=None, local_at=None, screening=None) -> Reward:
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("invalid_reward_time")
    if not (available(conn) if schema_available is None else schema_available):
        return Reward(0., {"available": 0.})
    ids = lines(conn, line_id)
    line_id = ids[0]
    row = conn.execute("SELECT ts,from_guid,channel,origin,length(text) FROM chat_line WHERE id=?",
                       (line_id,)).fetchone()
    if row is None or row[3] != "mind" or row[0] > now:
        return Reward(0., {"available": 0.})
    ts, guid, channel, _, chars = row
    weights = read(conn, at=now, schema_available=True)
    raw, evidence_count = observations(conn, ids, guid, now)
    signals = {kind: value for (kind, source), value in raw.items() if source == "implicit"}
    explicit = raw.get(("explicit", "explicit"), 0.)
    observed, baselines = _metrics(signals), _baseline(conn, line_id, ts, channel)
    parts = {"available": 1., "explicit": explicit, "engagement_measured": float(len(observed)),
             "evidence_count": float(evidence_count), "line_count": float(len(ids))}
    if screening is not None:
        # Fresh shared screening replaces historical judge observations, without
        # pretending they are new external evidence or an actual live guard.
        raw = {key:value for key,value in raw.items() if key[1] != "judge"}
        raw.update({(key,"judge"):1. for key in screening})
    normalized = []
    for key in METRICS:
        samples = baselines[key]
        center = median(samples) if samples else 0.
        parts["baseline_" + key] = center
        parts["baseline_n_" + key] = float(len(samples))
        value = (min(1., observed[key] / (2 * center)) if center > 0 else .5) if key in observed else 0.
        parts[key] = value
        if key in observed:
            normalized.append(value)
    engagement = sum(normalized) / len(normalized) if normalized else 0.
    emote = max(-1., min(1., signals.get("emote_friendly", 0.) +
                        signals.get("emote_romantic", 0.) - signals.get("emote_rude", 0.)))
    # A missing detector hit is not evidence of a clean line. The completed signals
    # receipt proves bounded screening only on that stage's supported, non-skipped rows.
    screened = screening is not None or channel in GAME_SCREENED and 0 < chars <= 4000 and conn.execute(
        "SELECT 1 FROM night_step WHERE stage='signals' AND unit=? AND finished<=? LIMIT 1",
        (str(line_id), now)).fetchone() is not None
    recipe = conn.execute("SELECT guard_verdict FROM talk_recipe WHERE line_id=?", (line_id,)).fetchone()
    verdict = recipe[0] if recipe else "not_run"
    actual_guard = verdict in {"send", "rewrite", "react", "drop"}
    hits = sum(raw.get((key, "judge"), 0.) for key in JUDGES)
    guard_penalty = max((raw.get(("judge_guard_" + action, "judge"), 0.) * amount
                         for action, amount in (("rewrite", .25), ("react", .5), ("drop", 1))), default=0.)
    guard_penalty = max(guard_penalty, {"rewrite": .25, "react": .5, "drop": 1.}.get(verdict, 0.))
    quality_measured = bool(screened or actual_guard or any(source=="judge" for _,source in raw))
    quality = -min(1., max(0., hits + guard_penalty)) if quality_measured else 0.
    penalties = (signals.get("muted", 0.) + .5 * signals.get("dial_down", 0.) +
                 .5 * signals.get("nudged_against", 0.) + .1 * signals.get("ignored", 0.))
    components = dict(explicit=explicit, engagement=engagement, emote=emote, quality=quality)
    measured = dict(explicit=("explicit","explicit") in raw or
                    any(key in signals for key in ("muted","dial_down","nudged_against")),
                    engagement=bool(normalized),
                    emote=any(key in signals for key in ("emote_friendly","emote_romantic","emote_rude")))
    measured_weight = sum(weights.values[k] for k in measured if measured[k])
    owner_part = (sum(weights.values[k] * components[k] for k in measured if measured[k]) /
                  measured_weight if measured_weight else 0.) - penalties
    quality_deduction = weights.values["quality"] * quality
    total = owner_part + quality_deduction
    if explicit < 0:
        total = min(total, -.40)  # the owner's veto is outside editable weights
    # Naive local timestamps use the OS zone's offset at midnight, including a
    # DST change later that morning. Injected clocks carry their own zone rules.
    local = local_at(ts) if local_at else datetime.fromtimestamp(ts)
    day = local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    medium = discord_sql("channel")
    medium = f"({medium})" if is_discord(channel) else f"NOT ({medium})"
    rank = conn.execute("SELECT count(*) FROM chat_line WHERE origin='mind' AND from_guid=? "
        "AND ts>=? AND (ts<? OR (ts=? AND id<?)) AND " + medium,
        (guid, day, ts, ts, line_id)).fetchone()[0]
    factor = MEMBER_DECAY ** rank
    # Raw evidence is independent of volume; consumers choose how to apply the cap.
    value = max(-1., min(1., total))
    parts.update(components, quality_measured=float(quality_measured),
                 explicit_measured=float(measured["explicit"]),emote_measured=float(measured["emote"]),
                 measured_weight=measured_weight,owner_part=owner_part,quality_deduction=quality_deduction,
                 detector_hits=hits, guard_penalty=guard_penalty, penalties=penalties,
                 member_day_rank=float(rank), member_decay=factor, unclipped=total)
    return Reward(value, parts, weights.version, factor)
