"""Her feeling right now, kept (`LifeService.state`, present only while `[life] state` is on).

Each member's state (`life.state.State`) is stored in meta `life:state:<guid>` as numbers only
({"v","e","s","o","h","ts"}: valence, energy, stress, social, toward him, and when it was
written) and read through `get`, which eases it toward her baseline for the time since. Nothing
reads it in a prompt yet. Only cast members move (a guild member at level 80 or more with a
persona, never him).

What moves it (`tick` every TICK_S, read marks starting at the newest rows on its first pass and
advanced row by row, so history is never replayed; `on_event`; `loot_lost`). A row or event more
than STALE_S old when it is read moves nothing, so a bridge replay after a restart applies no old
tone; a row that cannot be read is skipped and logged once (`life_state_row_skipped`).
- his lines and their tone (`state.tone`): a game whisper moves the member he whispered; any other
  game line the members it names (`talk.select.mentioned`) among those who could read it (the
  row's audience); a Discord DM its member; a group DM or channel post the members he @-named
  (`discord.talk.mentioned_names`) or replied to among its readers (a group DM's captured
  readers, a channel's audience);
- her own lines (TALKED): each drains social, but talking alone never takes it more than
  TALK_DRAIN_MAX below her baseline, and a line to him (a whisper or DM to him) or answering him
  (a reply to his post, or a line in a channel he wrote in within ANSWER_S before it) drains
  nothing: engagement with him is not fatigue;
- being ignored (IGNORED), once per burst of her lines to him (whispers and DMs are one
  conversation; lines at most BURST_S apart with no answer of his between) when he wrote nothing
  to her in that conversation from the burst's first line until IGNORED_S after its last, while
  he wrote somewhere else in that window;
- contagion, damped: a member line in guild, party or raid (the row's audience, online now) or a
  public Discord channel (its audience, online on Discord) pulls each other reader's valence
  CONTAGION of the way toward the speaker's, once per reader a tick and at most
  CONTAGION_PER_HOUR of movement per member in any hour (kept in memory), so a busy night still
  leaves members different. Never from a whisper, a DM, a group DM, officer chat or an officers'
  or unknown Discord channel;
- a boss killed or wiped (its members), loot won, loot lost to his claim.

Logs carry ids and counts only: `life_state_tick impulses= members=` at most once a minute."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Callable

from leeroy_mind.clock import night_of
from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_discord, is_group, is_public
from leeroy_mind.discord.presence import RNG_SEED, her_local, presence_of
from leeroy_mind.logs import log
from leeroy_mind.protocol import Boss, LootWon
from leeroy_mind.wow import CAST_MIN_LEVEL

from .day import day_plan
from .state import AXES, IMPULSES, Impulse, State, apply, baseline, clamped, decayed, tone

STATE_KEY = "life:state:{guid}"
TICK_S = 30.0
IGNORED_S = 1200.0
BURST_S = 90.0  # her lines to him this close, with no answer between, are one burst
ANSWER_S = 120.0  # her line this soon after his in the same channel answers him
TALK_DRAIN_MAX = 0.4  # talking alone takes social at most this far below her baseline
CONTAGION = 0.03
CONTAGION_PER_HOUR = 0.3  # the most valence contagion moves a member in any hour
HOUR_S = 3600.0
CONTAGION_GAME = frozenset({"guild", "party", "raid"})
STALE_S = 600.0  # a row or event older than this when read moves nothing
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
    her = her_local(owner_local, p)
    return baseline(p, mood=mood, arousal=arousal, plan=day_plan(p, her.date()),
                    minute=her.hour * 60 + her.minute, toward_seed=toward_seed)


class _Pass:
    """One tick's changes, written together at its end."""

    def __init__(self, book: StateBook, now: float):
        self.book, self.now = book, now
        self.work: dict[int, State] = {}
        self.pulled: set[int] = set()
        self.changes = 0

    def state(self, g: int) -> State:
        if g not in self.work:
            self.work[g] = self.book.get(g)
        return self.work[g]

    def push(self, g, kind: Impulse) -> None:
        if self.book._member(g):
            self.work[g] = apply(self.state(g), kind)
            self.changes += 1

    def talked(self, g) -> None:
        """TALKED, never past TALK_DRAIN_MAX below her baseline's social."""
        if not self.book._member(g):
            return
        her = self.state(g)
        floor = self.book.baseline(g).social - TALK_DRAIN_MAX
        if her.social <= floor:
            return
        drained = her.social + IMPULSES[Impulse.TALKED]["social"]
        self.work[g] = replace(her, social=max(floor, drained))
        self.changes += 1

    def pull(self, speaker: int, readers) -> None:
        """Each reader's valence CONTAGION of the way toward the speaker's, once a tick and
        within what is left of her CONTAGION_PER_HOUR."""
        towards = self.state(speaker).valence
        for g in sorted(readers):
            if g in self.pulled or g == speaker or not self.book._member(g):
                continue
            self.pulled.add(g)
            room = CONTAGION_PER_HOUR - self.book._contagion_used(g, self.now)
            if room <= 0:
                continue
            her = self.state(g)
            delta = max(-room, min(room, CONTAGION * (towards - her.valence)))
            moved = replace(her, valence=max(-1.0, min(1.0, her.valence + delta)))
            if moved.valence == her.valence:
                continue
            self.book._contagion_note(g, self.now, abs(moved.valence - her.valence))
            self.work[g] = moved
            self.changes += 1


class StateBook:
    def __init__(self, store, clock, personas, world, human_guid: Callable[[], int]):
        self.store, self.clock, self.personas, self.world = store, clock, personas, world
        self.human_guid = human_guid
        self._marks: tuple[int, int] | None = None  # chat_line, discord_message ids read
        self._bursts: dict[int, list[list[float]]] = {}  # her bursts to him: [first ts, last ts]
        self._contagion: dict[int, deque[tuple[float, float]]] = {}  # guid -> (ts, moved)
        self._last_tick: float | None = None
        self._seeds: dict[int, tuple[float, float]] = {}  # guid -> (good until, seed)
        self._impulses = 0
        self._moved: set[int] = set()
        self._logged_at = -math.inf

    # -- reading -------------------------------------------------------------------------------

    def _member(self, guid) -> bool:
        """A cast member: in the guild's roster at level 80 or more, with a persona, not him."""
        if type(guid) is not int or guid <= 0 or guid == self.human_guid():
            return False
        m = self.world.discord_members().get(guid)
        return (m is not None and m.level >= CAST_MIN_LEVEL
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

    def _contagion_used(self, guid: int, now: float) -> float:
        moved = self._contagion.get(guid)
        if not moved:
            return 0.0
        while moved and now - moved[0][0] >= HOUR_S:
            moved.popleft()
        return sum(amount for _, amount in moved)

    def _contagion_note(self, guid: int, now: float, amount: float) -> None:
        self._contagion.setdefault(guid, deque()).append((now, amount))

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
        return type(ts) is int and ts > 0 and self.clock.now() * 1000 - ts > STALE_S * 1000

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
        p = _Pass(self, now)
        for row in self._game_rows(self._marks[0]):
            self._marks = (row["id"], self._marks[1])
            if is_discord(row["channel"]):
                continue  # the guild Discord's lines are read from its own messages below
            if now - float(row["ts"]) <= STALE_S:
                self._guarded("chat_line", row["id"], self._game_row, row, human, p)
        for row in self._discord_rows(self._marks[1]):
            self._marks = (self._marks[0], row["id"])
            if row["deleted"] is None and now - float(row["ts"]) <= STALE_S:
                self._guarded("discord_message", row["id"], self._discord_row, row, human, p)
        for guid, bursts in list(self._bursts.items()):
            for burst in [b for b in bursts if now - b[1] >= IGNORED_S]:
                bursts.remove(burst)
                if human and self._ignored(guid, burst[0], burst[1] + IGNORED_S, human):
                    p.push(guid, Impulse.IGNORED)
            if not bursts:
                del self._bursts[guid]
        self._write(p.work)
        self._count(p.changes, p.work)
        if self._impulses and now - self._logged_at >= LOG_EVERY_S:
            log("life_state_tick", impulses=self._impulses, members=len(self._moved))
            self._logged_at = now
            self._impulses, self._moved = 0, set()

    @staticmethod
    def _guarded(table: str, row_id: int, read, *args) -> None:
        """One row read; a row that fails is skipped (its mark is already past it) and
        logged."""
        try:
            read(*args)
        except Exception as exc:
            log("life_state_row_skipped", level=logging.WARNING, table=table, id=row_id,
                reason=type(exc).__name__)

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
        if not isinstance(values, list):
            return frozenset()
        return frozenset(g for g in values if type(g) is int)

    def _names(self, readers) -> dict[int, str]:
        out = {}
        for g in readers:
            p = self.personas.get(g)
            if p is not None and self._member(g):
                out[g] = p.character_name
        return out

    def _game_row(self, row, human, p: _Pass) -> None:
        channel, frm, origin = row["channel"], row["from_guid"], row["origin"]
        if origin == "human" and human and frm == human:
            if channel == "whisper":
                targets = [row["to_guid"]]
            else:  # only the members it names, among those who could read it
                from leeroy_mind.talk.select import mentioned
                targets = mentioned(row["text"], self._names(self._readers(row["audience"])))
            kind = _BY_TONE[tone(row["text"])]
            for g in targets:
                p.push(g, kind)
        elif origin == "mind" and self._member(frm):
            ts = float(row["ts"])
            if channel == "whisper" and human and row["to_guid"] == human:
                self._her_line_to_him(frm, ts, human)
            elif not (human and self._he_wrote(channel, ts, row["id"], human)):
                p.talked(frm)
            if channel in CONTAGION_GAME:
                p.pull(frm, self._readers(row["audience"]) & self.world.online - {frm, human})

    def _discord_row(self, row, human, p: _Pass) -> None:
        channel, author, origin = row["channel"], row["author"], row["origin"]
        peer = layout.dm_peer(channel)
        if origin == "human" and human and author == human:
            kind = _BY_TONE[tone(row["text"])]
            for g in self._his_discord_targets(row, peer, human):
                p.push(g, kind)
        elif origin == "mind" and self._member(author):
            ts = float(row["ts"])
            if peer is not None:  # her DM with him: a line to him
                if peer == author and human:
                    self._her_line_to_him(author, ts, human)
                return
            if not (human and (self._replies_to(row, human)
                               or self._he_posted(channel, ts, row["id"], human))):
                p.talked(author)
            if not is_group(channel) and is_public(CHANNEL_PREFIX + channel):
                readers = self.world.audience(CHANNEL_PREFIX + channel, from_guid=author)
                p.pull(author, {g for g in readers - {author, human} if self._discord_online(g)})

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
            parent = self._parent(row["reply_to"])
            if parent is not None and parent["origin"] == "mind" and parent["deleted"] is None:
                named.append(parent["author"])
        from leeroy_mind.discord.talk import mentioned_names
        names = {n.casefold(): g for g, n in self._names(readers).items()}
        named += [names[n.casefold()] for n in mentioned_names(row["text"])
                  if n.casefold() in names]
        return [g for g in dict.fromkeys(named) if g in readers]

    def _parent(self, message: int):
        return self.store.conn.execute(
            "SELECT author, origin, deleted FROM discord_message WHERE id = ?",
            (message,)).fetchone()

    def _replies_to(self, row, human: int) -> bool:
        """Her post replies to one of his."""
        if row["reply_to"] is None:
            return False
        parent = self._parent(row["reply_to"])
        return parent is not None and parent["origin"] == "human" and parent["author"] == human

    def _he_wrote(self, channel: str, ts: float, line_id: int, human: int) -> bool:
        """He wrote in this game channel within ANSWER_S before line `line_id`."""
        return self.store.conn.execute(
            "SELECT 1 FROM chat_line WHERE from_guid = ? AND origin = 'human' AND channel = ? "
            "AND id < ? AND ts >= ? LIMIT 1",
            (human, channel, line_id, ts - ANSWER_S)).fetchone() is not None

    def _he_posted(self, channel: str, ts: float, message: int, human: int) -> bool:
        """He posted in this Discord channel or group DM within ANSWER_S before `message`."""
        return self.store.conn.execute(
            "SELECT 1 FROM discord_message WHERE author = ? AND origin = 'human' AND channel = ? "
            "AND id < ? AND ts >= ? LIMIT 1",
            (human, channel, message, ts - ANSWER_S)).fetchone() is not None

    def _her_line_to_him(self, guid: int, ts: float, human: int) -> None:
        """Her whisper or DM to him joins her last burst when it is at most BURST_S after its
        last line and he has not answered since; else it starts a burst."""
        bursts = self._bursts.setdefault(guid, [])
        last = bursts[-1] if bursts else None
        if (last is not None and 0 <= ts - last[1] <= BURST_S
                and not self._answered(guid, last[1], ts, human)):
            last[1] = ts
        else:
            bursts.append([ts, ts])

    def _discord_online(self, guid: int) -> bool:
        p = self.personas.get(guid)
        if p is None:
            return False
        world = self.world
        m = world.discord_members().get(guid)
        return presence_of(p, local=her_local(self.clock.local(), p),
                           body_online=guid in world.online, body_zone=m.zone if m else 0,
                           rng_seed=RNG_SEED,
                           in_his_group=world.human_online and world.in_group(guid)
                           ).state == "online"

    def _answered(self, guid: int, t0: float, t1: float, human: int) -> bool:
        """He wrote to her in their pair conversation (a whisper or her DM) in (t0, t1]."""
        conn, dm = self.store.conn, f"dm:{guid}"
        return bool(conn.execute(
            "SELECT 1 FROM chat_line WHERE from_guid = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? AND ((channel = 'whisper' AND to_guid = ?) OR channel = ?) LIMIT 1",
            (human, t0, t1, guid, dm)).fetchone() or conn.execute(
            "SELECT 1 FROM discord_message WHERE author = ? AND origin = 'human' AND channel = ? "
            "AND ts > ? AND ts <= ? LIMIT 1", (human, dm, t0, t1)).fetchone())

    def _ignored(self, guid: int, t0: float, t1: float, human: int) -> bool:
        """He wrote nothing to her in their pair conversation in (t0, t1], while he wrote
        somewhere else in it."""
        if self._answered(guid, t0, t1, human):
            return False
        conn = self.store.conn
        return bool(conn.execute(
            "SELECT 1 FROM chat_line WHERE from_guid = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? LIMIT 1", (human, t0, t1)).fetchone() or conn.execute(
            "SELECT 1 FROM discord_message WHERE author = ? AND origin = 'human' AND ts > ? "
            "AND ts <= ? LIMIT 1", (human, t0, t1)).fetchone())
