"""Her feeling right now, kept (`LifeService.state`, present only while `[life] state` is on).

Each member's state (`life.state.State`) is stored in meta `life:state:<guid>` as numbers only
({"v","e","s","o","h","ts"}: valence, energy, stress, social, toward him, and when it was
written) and read through `get`, which eases it toward her baseline for the time since. Nothing
reads it in a prompt yet.

What moves it (`tick` every TICK_S, high-water marks starting at the newest rows on its first
pass, so history is never replayed; `on_event`; `loot_lost`):
- his lines and their tone (`state.tone`): a game whisper moves the member he whispered; any other
  game line the members it names (`talk.select.mentioned`) among those who could read it (the
  row's audience); a Discord DM its member; a group DM or channel post the members he @-named or
  replied to among its readers (a group DM's captured readers, a channel's audience);
- her own lines (TALKED), and being ignored (IGNORED, once per line of hers to him in a whisper
  or DM when he wrote nothing to her in that pair conversation, whisper and DM being one, within
  IGNORED_S while he wrote somewhere else in that window);
- contagion, damped: a member line in guild, party or raid (the row's audience, online now) or a
  public Discord channel (its audience, online on Discord) pulls each other reader's valence
  CONTAGION of the way toward the speaker's, once per reader a tick. Never from a whisper, a DM,
  a group DM, officer chat or an officers' or unknown Discord channel;
- a boss killed or wiped (its members), loot won, loot lost to his claim; a replayed event
  older than STALE_EVENT_S moves nothing.

Logs carry counts only: `life_state_tick impulses= members=` at most once a minute."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

from leeroy_mind.clock import night_of
from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_discord, is_group, is_public
from leeroy_mind.discord.presence import RNG_SEED, local_time, presence_of
from leeroy_mind.logs import log
from leeroy_mind.protocol import Boss, LootWon

from .day import day_plan
from .state import AXES, Impulse, State, apply, baseline, clamped, decayed, tone

STATE_KEY = "life:state:{guid}"
TICK_S = 30.0
IGNORED_S = 1200.0
CONTAGION = 0.03
CONTAGION_GAME = frozenset({"guild", "party", "raid"})
STALE_EVENT_S = 600.0
LOG_EVERY_S = 60.0
SCAN_LIMIT = 1000  # rows per table a tick; the rest wait for the next
SEED_TTL_S = 600.0
SEED_WEIGHT, SEED_CAP = 0.1, 0.3
WARM_TYPES = frozenset({"joy", "pride", "admiration", "gratitude", "hope", "happy_for"})
SORE_TYPES = frozenset({"distress", "shame", "reproach", "anger", "fear", "resentment"})
NEUTRAL = State(0.0, 0.0, 0.0, 0.0, 0.0)
_SHORT = dict(zip(AXES, ("v", "e", "s", "o", "h")))
_BY_TONE = {"warm": Impulse.HIS_WARM, "harsh": Impulse.HIS_HARSH, "plain": Impulse.HIS_PLAIN}
_BOSS = {"killed": Impulse.KILL, "wiped": Impulse.WIPE}


def encode(state: State, ts: float) -> str:
    data = {_SHORT[a]: round(getattr(state, a), 4) for a in AXES}
    data["ts"] = round(float(ts), 3)
    return json.dumps(data, separators=(",", ":"))


def decode(raw: str | None) -> tuple[State, float] | None:
    """A stored state and when it was written; None for none, or one that is not six finite
    numbers."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {*_SHORT.values(), "ts"}:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in data.values()):
        return None
    return clamped(**{a: data[_SHORT[a]] for a in AXES}), float(data["ts"])


def arousal_of(conn, guid: int) -> float:
    """The night's PAD arousal (pad_mood.a), 0 when there is none."""
    try:
        row = conn.execute("SELECT a FROM pad_mood WHERE guid = ?", (guid,)).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(row[0]) if row and row[0] is not None else 0.0


def toward_seed_of(conn, guid: int, human: int, local: datetime) -> float:
    """Last night's feelings of hers about him: SEED_WEIGHT x the sum of sign x intensity / 3
    over her emotion rows with him as target (the latest of tonight's and last night's), within
    +-SEED_CAP."""
    if not human:
        return 0.0
    tonight = night_of(local)
    previous = (date.fromisoformat(tonight) - timedelta(days=1)).isoformat()
    try:
        (night,) = conn.execute("SELECT max(night) FROM emotion WHERE night IN (?, ?)",
                                (tonight, previous)).fetchone()
        if night is None:
            return 0.0
        rows = conn.execute("SELECT type, intensity FROM emotion WHERE guid = ? AND night = ? "
                            "AND target = ?", (guid, night, human)).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    total = sum((1 if kind in WARM_TYPES else -1 if kind in SORE_TYPES else 0)
                * float(intensity or 0) / 3 for kind, intensity in rows)
    return max(-SEED_CAP, min(SEED_CAP, SEED_WEIGHT * total))


def baseline_at(p, *, owner_local: datetime, mood: float, arousal: float,
                toward_seed: float) -> State:
    """Her baseline at the owner's local time, in her own region's clock."""
    her = local_time(owner_local, p.identity.region)
    return baseline(p, mood=mood, arousal=arousal, plan=day_plan(p, her.date()),
                    minute=her.hour * 60 + her.minute, toward_seed=toward_seed)


class StateBook:
    def __init__(self, store, clock, personas, world, human_guid: Callable[[], int]):
        self.store, self.clock, self.personas, self.world = store, clock, personas, world
        self.human_guid = human_guid
        self._marks: tuple[int, int] | None = None  # chat_line, discord_message ids seen
        self._pending: list[tuple[int, float]] = []  # her lines to him: (guid, ts)
        self._last_tick: float | None = None
        self._seeds: dict[int, tuple[float, float]] = {}  # guid -> (good until, seed)
        self._impulses = 0
        self._moved: set[int] = set()
        self._logged_at = -math.inf

    # -- reading -------------------------------------------------------------------------------

    def _member(self, guid) -> bool:
        return (type(guid) is int and guid > 0 and guid != self.human_guid()
                and self.personas.get(guid) is not None)

    def _toward_seed(self, guid: int, now: float) -> float:
        hit = self._seeds.get(guid)
        if hit is not None and hit[0] > now:
            return hit[1]
        seed = toward_seed_of(self.store.conn, guid, self.human_guid(), self.clock.local())
        self._seeds[guid] = (now + SEED_TTL_S, seed)
        return seed

    def baseline(self, guid: int) -> State | None:
        """Where her feeling rests now; None for a member without a persona."""
        p = self.personas.get(guid)
        if p is None:
            return None
        now = self.clock.now()
        return baseline_at(p, owner_local=self.clock.local(),
                           mood=self.store.get_state(guid, lens=True).mood,
                           arousal=arousal_of(self.store.conn, guid),
                           toward_seed=self._toward_seed(guid, now))

    def get(self, guid: int) -> State:
        """Her state now: the stored one eased toward her baseline, else her baseline; neutral
        for a member without a persona."""
        base = self.baseline(guid)
        if base is None:
            return NEUTRAL
        kept = decode(self.store.get_meta(STATE_KEY.format(guid=guid)))
        if kept is None:
            return base
        state, ts = kept
        return decayed(state, base, self.clock.now() - ts)

    # -- writing -------------------------------------------------------------------------------

    def _write(self, states: dict[int, State]) -> None:
        if not states:
            return
        now = self.clock.now()
        with self.store.transaction():
            for guid, state in sorted(states.items()):
                self.store.set_meta(STATE_KEY.format(guid=guid), encode(state, now))

    def _count(self, impulses: int, members) -> None:
        self._impulses += impulses
        self._moved.update(members)

    def impulse(self, guid: int, kind: Impulse, scale: float = 1.0) -> None:
        if not self._member(guid):
            return
        self._write({guid: apply(self.get(guid), kind, scale)})
        self._count(1, (guid,))

    def _many(self, guids, kind: Impulse) -> None:
        moved = {g: apply(self.get(g), kind) for g in dict.fromkeys(guids) if self._member(g)}
        self._write(moved)
        self._count(len(moved), moved)

    def loot_lost(self, guid: int) -> None:
        """He claimed an item she wanted (guild ports' loot_lost)."""
        self.impulse(guid, Impulse.LOOT_LOST)

    def _stale(self, ev) -> bool:
        ts = getattr(ev, "ts", None)
        return type(ts) is int and ts > 0 and self.clock.now() * 1000 - ts > STALE_EVENT_S * 1000

    async def on_event(self, ev) -> None:
        """A boss killed or wiped moves its members; loot won its winner."""
        if self._stale(ev):
            return
        if isinstance(ev, Boss) and ev.state in _BOSS:
            self._many(ev.members, _BOSS[ev.state])
        elif isinstance(ev, LootWon):
            self.impulse(ev.guid, Impulse.LOOT_WON)

    # -- the tick ------------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        if self._last_tick is not None and now - self._last_tick < TICK_S:
            return
        self._last_tick = now
        conn = self.store.conn
        if self._marks is None:
            self._marks = (conn.execute("SELECT coalesce(max(id), 0) FROM chat_line").fetchone()[0],
                           conn.execute("SELECT coalesce(max(id), 0) FROM discord_message")
                           .fetchone()[0])
            return
        human = self.human_guid()
        work: dict[int, State] = {}
        pulled: set[int] = set()
        changes = 0

        def state(g: int) -> State:
            if g not in work:
                work[g] = self.get(g)
            return work[g]

        def push(g, kind: Impulse) -> None:
            nonlocal changes
            if self._member(g):
                work[g] = apply(state(g), kind)
                changes += 1

        def pull(speaker: int, readers) -> None:
            nonlocal changes
            towards = state(speaker).valence
            for g in sorted(readers):
                if g in pulled or g == speaker or not self._member(g):
                    continue
                her = state(g)
                work[g] = clamped(**{a: getattr(her, a) for a in ("energy", "stress", "social",
                                                                 "toward_him")},
                                  valence=her.valence + CONTAGION * (towards - her.valence))
                pulled.add(g)
                changes += 1

        game_mark, discord_mark = self._marks
        for row in self._game_rows(game_mark):
            game_mark = row["id"]
            if is_discord(row["channel"]):
                continue  # the guild Discord's lines are read from its own messages below
            self._game_row(row, human, push, pull)
        for row in self._discord_rows(discord_mark):
            discord_mark = row["id"]
            if row["deleted"] is None:
                self._discord_row(row, human, push, pull)
        self._marks = (game_mark, discord_mark)
        due = [x for x in self._pending if now - x[1] >= IGNORED_S]
        self._pending = [x for x in self._pending if now - x[1] < IGNORED_S]
        for guid, ts in due:
            if human and self._ignored(guid, ts, ts + IGNORED_S, human):
                push(guid, Impulse.IGNORED)
        self._write(work)
        self._count(changes, work)
        if self._impulses and now - self._logged_at >= LOG_EVERY_S:
            log("life_state_tick", impulses=self._impulses, members=len(self._moved))
            self._logged_at = now
            self._impulses, self._moved = 0, set()

    def _game_rows(self, mark: int):
        return self.store.conn.execute(
            "SELECT id, ts, channel, from_guid, to_guid, text, origin, audience FROM chat_line "
            "WHERE id > ? ORDER BY id LIMIT ?", (mark, SCAN_LIMIT)).fetchall()

    def _discord_rows(self, mark: int):
        return self.store.conn.execute(
            "SELECT id, ts, channel, author, origin, text, reply_to, deleted FROM discord_message "
            "WHERE id > ? ORDER BY id LIMIT ?", (mark, SCAN_LIMIT)).fetchall()

    @staticmethod
    def _readers(raw) -> frozenset[int]:
        try:
            values = json.loads(raw) if isinstance(raw, str) else ()
        except ValueError:
            return frozenset()
        return frozenset(g for g in values if type(g) is int) if isinstance(values, list) \
            else frozenset()

    def _names(self, readers) -> dict[int, str]:
        out = {}
        for g in readers:
            p = self.personas.get(g)
            if p is not None and self._member(g):
                out[g] = p.character_name
        return out

    def _game_row(self, row, human, push, pull) -> None:
        channel, frm, origin = row["channel"], row["from_guid"], row["origin"]
        if origin == "human" and human and frm == human:
            if channel == "whisper":
                targets = [row["to_guid"]]
            else:  # only the members it names, among those who could read it
                from leeroy_mind.talk.select import mentioned
                targets = mentioned(row["text"], self._names(self._readers(row["audience"])))
            kind = _BY_TONE[tone(row["text"])]
            for g in targets:
                push(g, kind)
        elif origin == "mind" and self._member(frm):
            push(frm, Impulse.TALKED)
            if channel == "whisper" and human and row["to_guid"] == human:
                self._pending.append((frm, float(row["ts"])))
            elif channel in CONTAGION_GAME:
                pull(frm, self._readers(row["audience"]) & self.world.online - {frm, human})

    def _discord_row(self, row, human, push, pull) -> None:
        channel, author, origin = row["channel"], row["author"], row["origin"]
        peer = layout.dm_peer(channel)
        if origin == "human" and human and author == human:
            kind = _BY_TONE[tone(row["text"])]
            for g in self._his_discord_targets(row, peer, human):
                push(g, kind)
        elif origin == "mind" and self._member(author):
            push(author, Impulse.TALKED)
            if peer is not None:
                if peer == author:
                    self._pending.append((author, float(row["ts"])))
            elif not is_group(channel) and is_public(CHANNEL_PREFIX + channel):
                readers = self.world.audience(CHANNEL_PREFIX + channel, from_guid=author)
                pull(author, {g for g in readers - {author, human} if self._discord_online(g)})

    def _his_discord_targets(self, row, peer, human) -> list[int]:
        if peer is not None:
            return [peer]
        channel = row["channel"]
        if is_group(channel):
            from leeroy_mind.groups.audience import message_readers
            readers = message_readers(self.store.conn, row["id"])
        else:
            readers = self.world.audience(CHANNEL_PREFIX + channel, from_guid=human)
        named = []
        if row["reply_to"] is not None:
            parent = self.store.conn.execute(
                "SELECT author, origin, deleted FROM discord_message WHERE id = ?",
                (row["reply_to"],)).fetchone()
            if parent is not None and parent["origin"] == "mind" and parent["deleted"] is None:
                named.append(parent["author"])
        from leeroy_mind.discord.talk import _MENTION
        names = {n.casefold(): g for g, n in self._names(readers).items()}
        named += [names[m.group(1).casefold()] for m in _MENTION.finditer(row["text"])
                  if m.group(1).casefold() in names]
        return [g for g in dict.fromkeys(named) if g in readers]

    def _discord_online(self, guid: int) -> bool:
        p = self.personas.get(guid)
        if p is None:
            return False
        world = self.world
        m = world.discord_members().get(guid)
        return presence_of(p, local=local_time(self.clock.local(), p.identity.region),
                           body_online=guid in world.online, body_zone=m.zone if m else 0,
                           rng_seed=RNG_SEED,
                           in_his_group=world.human_online and world.in_group(guid)
                           ).state == "online"

    def _ignored(self, guid: int, t0: float, t1: float, human: int) -> bool:
        """He wrote nothing to her in their pair conversation (whisper or DM) after her line
        within the window, while he wrote somewhere else in it."""
        conn, dm = self.store.conn, f"dm:{guid}"
        answered = conn.execute(
            "SELECT 1 FROM chat_line WHERE from_guid = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? AND ((channel = 'whisper' AND to_guid = ?) OR channel = ?) LIMIT 1",
            (human, t0, t1, guid, dm)).fetchone() or conn.execute(
            "SELECT 1 FROM discord_message WHERE author = ? AND origin = 'human' AND channel = ? "
            "AND ts > ? AND ts <= ? LIMIT 1", (human, dm, t0, t1)).fetchone()
        if answered:
            return False
        return bool(conn.execute(
            "SELECT 1 FROM chat_line WHERE from_guid = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? LIMIT 1", (human, t0, t1)).fetchone() or conn.execute(
            "SELECT 1 FROM discord_message WHERE author = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? LIMIT 1", (human, t0, t1)).fetchone())
