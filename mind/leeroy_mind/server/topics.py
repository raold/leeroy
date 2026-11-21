"""[server] channel_topics (default true): a conversation opens only in a channel its subject fits,
and no channel floods (owner, 2026-11-21: "the discord channels like #memes are not topical at
all its just random prattling"). Pure but for `posted_today`'s one SELECT.

- `route`: a seed with only the channels its subject fits (discord/layout.py's purposes). A life
  beat, a weekend or a member's plan goes where its words take it (#food, #pets, and for a plan a
  WoW content channel; discord/topics.py `subjects`), else #irl when it is her real life, and
  #general; a joke never opens in #memes (only a meme does: a text meme, or the night's meme
  picture); a channel's purpose seeds no conversation where talk rests on something posted
  (`event_only`: #pics, #memes, #logs, #applications, #bank-requests; a trial member's review
  still goes to #applications); a seed its own module placed (loot, raids, calendar, photos)
  keeps the channels that fit it. One of the guild's own business (EXEMPT: his godmode intents
  go where he said, decisions, bank requests, heroics, a group's introduction) is never touched.
  A public seed with no channel left falls back to #general; a meme with nowhere to go, or an
  event-only purpose, is None.
- `room` and `DAILY`: a channel's member posts left today (from local midnight; `posted_today`
  counts them), so a conversation opens only where MIN_ROOM more fit, runs no longer than the
  room left, ends (`channel_full`) once the channel is full, and draws a reply as a burst only
  while BURST_POSTS posts still fit; EXEMPT seeds are never held back by it, and answers to him
  (D1) are counted but never refused.
- #memes: a conversation there is a meme post and MEME_REPLIES short reactions to it."""
from __future__ import annotations

from dataclasses import replace

from leeroy_mind.discord import layout, topics
from leeroy_mind.discord.channels import is_group

MEMES = "memes"
MEME_KINDS = frozenset({"meme", "meme_image"})
TOPIC_KINDS = frozenset({"life", "plan", "weekend"})
EXEMPT = frozenset({"intent", "decision", "bank_request", "restock", "lfg", "heroic_result",
                    "group_start"})
# What an event-only channel takes: the seeds posted about its own kind of event (EXEMPT seeds
# go where their own module put them)
EVENT_KINDS = {"pics": frozenset({"photo"}), MEMES: MEME_KINDS,
               "logs": frozenset({"raid_after"})}
TRIAL_KEY = "officer:trial:"  # a trial member's review belongs in #applications
# A channel's member posts in a local day; a channel not named takes DEFAULT_DAILY, a class
# channel CLASS_DAILY. The live week before (2026-11-12..21, member posts a day): #general 82,
# #irl 78, #loot-and-dkp 37, #officer-chat 33, #food 23, #professions-and-gold 11.
DAILY = {"general": 60, "irl": 30, "loot-and-dkp": 30, "officer-chat": 25, "food": 15,
         "pets": 12, "pics": 12, MEMES: 8, "raid-planning": 20, "strategy": 15}
DEFAULT_DAILY = 15
CLASS_DAILY = 10
MIN_ROOM = 3  # a conversation opens only where this many more posts still fit today
MEME_REPLIES = (1, 3)  # the reactions a meme post draws, at most
SCAN_ROWS = 5000  # posted_today reads at most this many of the newest messages
# The most posts one server turn makes: [talk] realism's burst of 2-3 (or 2 and a correction);
# every other server turn is one line. A burst is drawn only where this many still fit today.
BURST_POSTS = 3


def daily(cid: str) -> int:
    """The most member posts layout channel `cid` takes in a local day."""
    if cid in layout.CLASS_CHANNEL.values():
        return CLASS_DAILY
    return DAILY.get(cid, DEFAULT_DAILY)


def room(cid: str, posted) -> int:
    """The member posts `cid` still takes today, given `posted` (posted_today's counts); a group
    DM or a channel the layout lacks is never held back here (its own limits apply)."""
    if is_group(cid) or layout.channel(cid) is None:
        return 1 << 30
    return max(0, daily(cid) - int((posted or {}).get(cid, 0)))


def posted_today(conn, since: float) -> dict[str, int]:
    """The members' posts in each channel since `since` (local midnight), from the store: one
    SELECT over the newest SCAN_ROWS messages (by id, so it never reads the whole history; a day
    holds a few hundred), counts only."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT channel,count(*) FROM (SELECT channel,ts,origin,deleted FROM discord_message "
        "ORDER BY id DESC LIMIT ?) WHERE ts>=? AND origin='mind' AND deleted IS NULL "
        "GROUP BY channel", (SCAN_ROWS, since))}


def _fits(cid: str, seed) -> bool:
    ch = layout.channel(cid)
    if ch is None or ch.category == "info":
        return False
    if cid == MEMES:
        return seed.kind in MEME_KINDS
    if seed.kind in MEME_KINDS:
        return False
    if ch.event_only:
        return (seed.kind in EVENT_KINDS.get(cid, frozenset())
                or cid == "applications" and seed.key.startswith(TRIAL_KEY))
    return True


def route(seed):
    """`seed` with only the channels its subject fits, best first (the module docstring); None
    when it fits nowhere."""
    if seed.kind in EXEMPT or any(is_group(c) for c in seed.channels):
        return seed  # placed by its own module, or a group DM's own conversation
    if seed.kind == "purpose":
        return seed if seed.channels and _fits(seed.channels[0], seed) else None
    if seed.kind in TOPIC_KINDS and not seed.officers_only:
        found = topics.subjects(seed.fact)
        if seed.kind != "plan":  # her day is never a WoW channel's
            found = tuple(c for c in found if c in ("food", "pets"))
        life = seed.kind != "plan" or (topics.real_life(seed.fact) and not topics.game(seed.fact))
        channels = (*found, *(("irl",) if life else ()), "general")
        return replace(seed, channels=tuple(dict.fromkeys(channels)))
    kept = tuple(c for c in seed.channels if _fits(c, seed))
    if not kept and not seed.officers_only and seed.kind not in MEME_KINDS:
        kept = ("general",)
    return replace(seed, channels=kept) if kept else None


def routed(seeds, posted) -> list:
    """The seeds a conversation may open on today: routed, each keeping only the channels with
    MIN_ROOM posts of room left (one for an officers' bank announcement); EXEMPT seeds keep
    theirs whatever the room."""
    out = []
    for seed in seeds:
        seed = route(seed)
        if seed is None:
            continue
        if seed.kind not in EXEMPT:
            seed = replace(seed, channels=tuple(
                c for c in seed.channels
                if room(c, posted) >= (1 if seed.bank_announcement(c) else MIN_ROOM)))
        if seed.channels:
            out.append(seed)
    return out


def capped(seed) -> bool:
    """A conversation on `seed` stops when its channel is full (never an EXEMPT one)."""
    return seed.kind not in EXEMPT


__all__ = ["BURST_POSTS", "CLASS_DAILY", "DAILY", "DEFAULT_DAILY", "EXEMPT", "MEMES", "MEME_KINDS", "SCAN_ROWS",
           "MEME_REPLIES", "MIN_ROOM", "capped", "daily", "posted_today", "room", "route",
           "routed"]
