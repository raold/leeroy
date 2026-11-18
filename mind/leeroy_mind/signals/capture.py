"""A bounded, content-free inbox with one owned SQLite writer thread."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import logging
import re
import sqlite3

from leeroy_mind.discord.channels import is_discord

from . import discord as D
from . import store as S

log = logging.getLogger(__name__)
QUEUE_LIMIT = 512
PENDING_LIMIT = 512
COMBAT_MAX_AGE_S = 5.0
CHAIN_LIMIT = 8  # her earlier answered lines in one conversation that still count his further replies
MAIN_KINDS = frozenset({'discord_owner', 'reaction'})  # written on the mind's own connection
BUSY_S = 2.0  # the writer thread may wait out a night stage's write; the send path never waits


def direct(channel):
    return channel == 'whisper' or channel.startswith(('dm:', 'discord_dm:'))


@dataclass(frozen=True)
class Observation:
    kind: str
    at: float
    owner: int
    ids: tuple = ()
    values: tuple = ()
    epoch: int = 0


def scope(row):
    ch = row['channel']
    if direct(ch):
        return (ch, *sorted((row['from_guid'], row['to_guid'])))
    if ch in ('party', 'raid', 'raid_warning'):
        return ('group',)
    if ch in ('say', 'yell'):
        return (ch, row['map'], row['zone'])
    return (ch,)


def dscope(row):
    """A Discord line's conversation: its room and, in a channel, its thread (0: the room's own
    feed). A post in a thread never answers a line in the feed, nor the other way round."""
    return (*scope(row), row.get('dthread') or 0)


class Capture:
    def __init__(self, path, clock, store=None):
        self.path, self.clock = path, clock
        # The mind's Store. With it, what his Discord actions write goes through the mind's own
        # connection on the event loop, between its units: a commit from this capture's second
        # connection while the pipeline holds a read snapshot makes the pipeline's next write
        # fail at once ("database is locked"), and that write may be an answer he is owed.
        self.store = store
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='talk-signals')
        self.inflight = None
        self.conn = None  # opened, used and closed only by the owned executor
        self.pending = OrderedDict()
        self.recent = OrderedDict()
        self.exchanges = OrderedDict()
        # The guild Discord's own attribution state: his WoW presence, AFK, group and combat
        # never touch it (he reads Discord whether or not he plays).
        self.dpending = OrderedDict()
        self.drecent = OrderedDict()
        self.dexchanges = OrderedDict()
        self.chains = OrderedDict()
        self.sink = S  # put, upsert, drop: the backfill's dry run counts instead of writing
        self.active = float('-inf')
        self.online = self.afk = False
        self.combat = None
        self.combat_at = float('-inf')
        self.closed = False
        self.dropped = 0
        self.worker_epoch = 0
        self.last_prune = float('-inf')
        self.current_result = None

    def offer(self, observation, result=None):
        if self.closed:
            return False
        try:
            self.queue.put_nowait((replace(observation, epoch=self.dropped), result))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning('signals_queue_full: dropped=%d', self.dropped)
            return False

    def _on_main(self, item):
        if self.worker_epoch != self.dropped:
            self._reset()
            self.worker_epoch = self.dropped
        if item.epoch != self.worker_epoch:
            return {'ok': False, 'error': 'Signal capture lost observations.'}
        try:
            with self.store.transaction():
                if not S.available(self.store.conn):
                    return {'ok': False, 'error': 'Signals are not set up in this database.'}
                self._expire_discord(item.at)
                self._apply(self.store.conn, item)
            return {'ok': True}
        except Exception as exc:
            for state in (self.dpending, self.drecent, self.dexchanges, self.chains):
                state.clear()
            log.warning('signals_capture_failed: kind=%s error=%s', item.kind, type(exc).__name__)
            return {'ok': False, 'error': 'Signal capture could not save this observation.'}

    async def _work(self, observation):
        if self.store is not None and observation.kind in MAIN_KINDS:
            return self._on_main(observation)  # the worker is idle here: run() takes one at a time
        self.inflight = asyncio.get_running_loop().run_in_executor(self.executor, self._process, observation)
        return await asyncio.shield(self.inflight)

    async def run(self):
        while not self.closed:
            try:
                observation, result = await asyncio.wait_for(self.queue.get(), .25)
            except TimeoutError:
                await self._work(Observation('tick', self.clock.now(), 0, epoch=self.dropped))
                continue
            try:
                self.current_result = result
                reply = await self._work(observation)
                if result is not None and not result.done():
                    result.set_result(reply)
                self.current_result = None
            finally:
                self.queue.task_done()

    async def request(self, observation):
        result = asyncio.get_running_loop().create_future()
        if not self.offer(observation, result):
            return {'ok': False, 'error': 'Signal capture is busy or stopping.'}
        return await result

    async def flush(self):
        return await self.request(Observation('barrier', self.clock.now(), 0))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.inflight is not None:
            reply = await asyncio.shield(self.inflight)
            if self.current_result is not None and not self.current_result.done():
                self.current_result.set_result(reply)
        while not self.queue.empty():
            observation, result = self.queue.get_nowait()
            try:
                reply = await self._work(observation)
                if result is not None and not result.done():
                    result.set_result(reply)
            finally:
                self.queue.task_done()
        await self._work(Observation('close', self.clock.now(), 0))
        self.executor.shutdown(wait=False)

    def _open(self):
        if self.conn is None:
            self.conn = sqlite3.connect(self.path, timeout=BUSY_S, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')
        return self.conn

    def _process(self, item):
        if item.kind == 'close':
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            return {'ok': True}
        if self.worker_epoch != self.dropped:
            # Missing events invalidate attribution, including a lost reply/group/combat
            # transition. Discard queued evidence from before the gap, then start fresh.
            self._reset()
            self.worker_epoch = self.dropped
        if item.epoch != self.worker_epoch:
            return {'ok': False, 'error': 'Signal capture lost observations.'}
        if item.kind == 'tick' and not self.pending:
            self._expire_discord(item.at)  # in memory only: an idle Discord wait takes no lock
            return {'ok': True}
        try:
            conn = self._open()
            if not S.available(conn):
                return {'ok': False, 'error': 'Signals are not set up in this database.'}
            conn.execute('BEGIN IMMEDIATE')
            try:
                reply = self._apply(conn, item)
                self._expire(conn, item.at)
                pruning = item.at - self.last_prune >= 3600
                if pruning:
                    S.prune(conn, item.at)
                conn.commit()
                if pruning:
                    self.last_prune = item.at
                return reply or {'ok': True}
            except BaseException:
                conn.rollback()
                raise
        except Exception as exc:
            self._reset()
            log.warning('signals_capture_failed: kind=%s error=%s', item.kind, type(exc).__name__)
            return {'ok': False, 'error': 'Signal capture could not save this observation.'}

    def _sent_discord(self, row, owner, addressed, heard, thread=0):
        if not (self._visible(row, owner) and heard):
            return
        held = dict(row) | {'dthread': thread or 0}
        self.drecent[row['id']] = held
        while len(self.drecent) > PENDING_LIMIT:
            self.drecent.popitem(last=False)
        if addressed:
            self.dpending[row['id']] = dict(held)
            while len(self.dpending) > PENDING_LIMIT:
                self.dpending.popitem(last=False)

    def _his_discord(self, conn, item):
        """His Discord message as his reply to a member's line: the one he replied to by hand,
        else the newest line addressed to him in that conversation (the room and, in a channel,
        the thread: item.ids[2]) within D.WINDOW_S. A message
        of his with no such line before it (he wrote first, or twice running) says nothing."""
        now, owner = item.at, item.owner
        row = self._row(conn, item.ids[0])
        if (row is None or row['origin'] != 'human' or row['from_guid'] != owner
                or not is_discord(row['channel'])):
            return
        media, voice = item.values
        place = (*scope(row), (item.ids[2] if len(item.ids) > 2 else 0) or 0)
        his = '' if voice else (conn.execute('SELECT text FROM chat_line WHERE id=?', (row['id'],)).fetchone() or ('',))[0]
        if item.ids[1]:
            # He replied to one message by hand: that member line or nobody. A bot row, a line
            # from before a restart or one of another conversation (-1 or an id not held) never
            # falls back to whoever else is waiting.
            answered = self.drecent.get(item.ids[1])
            if answered is None or dscope(answered) != place or answered['id'] >= row['id'] or now < answered['ts']:
                return
        else:
            candidates = [r for r in self.dpending.values()
                          if 0 <= now - r['ts'] <= D.WINDOW_S and r['id'] < row['id'] and dscope(r) == place]
            if not candidates:
                return
            if not direct(row['channel']) and len({r['from_guid'] for r in candidates}) > 1:
                # Several members wait in one room: the one he names, else nobody.
                words = his.casefold()
                named = {r['from_guid'] for r in candidates if r['from_name'] and re.search(
                    r'(?<!\w)' + re.escape(r['from_name'].casefold()) + r'(?!\w)', words)}
                if len(named) != 1:
                    return
                candidates = [r for r in candidates if r['from_guid'] in named]
            answered = max(candidates, key=lambda r: (r['ts'], r['id']))
        lid, guid = answered['id'], answered['from_guid']
        put = self.sink.put
        put(conn, lid, guid, 'replied', 1, now)
        put(conn, lid, guid, 'reply_latency_s', min((now - answered['ts']) / D.WINDOW_S, 1), now)
        if media:
            put(conn, lid, guid, 'sent_media', 1, now)
        if not voice:  # a voice note's line is a placeholder until it is transcribed
            hers = (conn.execute('SELECT text FROM chat_line WHERE id=?', (lid,)).fetchone() or ('',))[0]
            put(conn, lid, guid, 'reply_len', min(len(his) / S.REPLY_LEN_SCALE, 1), now)
            for kind, value in D.read(his, hers).items():
                put(conn, lid, guid, kind, value, now)
        key = (place, guid)
        previous = self.dexchanges.get(key)
        running = previous is not None and 0 <= now - previous <= D.WINDOW_S
        if running:
            put(conn, lid, guid, 'thread_continued', 1, now)
        self.dexchanges[key] = now
        self.dexchanges.move_to_end(key)
        chain = [(line, count + 1) for line, count in (self.chains.get(key, []) if running else [])]
        for line, count in chain:
            self.sink.upsert(conn, line, guid, 'turns_after', min(count / D.TURNS_SCALE, 1), now)
        self.chains[key] = [*chain, (lid, 0)][-CHAIN_LIMIT:]
        self.chains.move_to_end(key)
        for store in (self.dexchanges, self.chains):
            while len(store) > PENDING_LIMIT:
                store.popitem(last=False)
        for other in [k for k, r in self.dpending.items()
                      if dscope(r) == place and (r['ts'], r['id']) <= (answered['ts'], lid)
                      and (direct(row['channel']) or r['from_guid'] == guid)]:
            self.dpending.pop(other)

    def _expire_discord(self, now):
        # Silence is no observation: a Discord line he never answered just stops waiting.
        for lid, row in list(self.dpending.items()):
            if now - row['ts'] > D.WINDOW_S:
                self.dpending.pop(lid)

    @staticmethod
    def _row(conn, line_id):
        return conn.execute('SELECT id,ts,channel,from_guid,from_name,to_guid,map,zone,thread_id,origin '
                            'FROM chat_line WHERE id=?', (line_id,)).fetchone()

    @staticmethod
    def _visible(row, owner):
        return (row is not None and row['origin'] == 'mind' and row['from_guid'] != owner
                and (not direct(row['channel']) or row['to_guid'] == owner))

    def _apply(self, conn, item):
        kind, now, owner = item.kind, item.at, item.owner
        if kind == 'sent':
            row = self._row(conn, item.ids[0])
            addressed, grouped, heard = item.values[:3]
            if row is not None and is_discord(row['channel']):
                self._sent_discord(row, owner, addressed, heard, item.values[3] if len(item.values) > 3 else 0)
            elif self._visible(row, owner) and heard:
                self.recent[row['id']] = dict(row)
                while len(self.recent) > PENDING_LIMIT:
                    self.recent.popitem(last=False)
                if addressed or grouped:
                    resting = self.combat is False and 0 <= now - self.combat_at <= COMBAT_MAX_AGE_S
                    self.pending[row['id']] = (dict(row), bool(addressed), not resting)
                    while len(self.pending) > PENDING_LIMIT:
                        self.pending.popitem(last=False)
        elif kind == 'owner':
            self.active, self.online = now, True
            if item.values[1]:  # command activity is not a conversational reply
                return
            row = self._row(conn, item.ids[0])
            if row is None or row['origin'] != 'human' or row['from_guid'] != owner:
                return
            candidates = [entry[0] for entry in self.pending.values()
                          if 0 <= now - entry[0]['ts'] <= S.REPLY_SECONDS
                          and entry[0]['id'] < row['id'] and scope(entry[0]) == scope(row)
                          and (not direct(row['channel']) or row['to_guid'] == entry[0]['from_guid'])]
            if not candidates:
                return
            answered = max(candidates, key=lambda r: (r['ts'], r['id']))
            lid, guid = answered['id'], answered['from_guid']
            S.put(conn, lid, guid, 'replied', 1, now)
            S.put(conn, lid, guid, 'reply_latency_s', (now - answered['ts']) / S.REPLY_SECONDS, now)
            S.put(conn, lid, guid, 'reply_len', item.values[0], now)
            key = (scope(row), row['thread_id'], guid)
            previous = self.exchanges.get(key)
            if previous is not None and 0 <= now - previous <= S.REPLY_SECONDS:
                S.put(conn, lid, guid, 'thread_continued', 1, now)
            self.exchanges[key] = now
            self.exchanges.move_to_end(key)
            while len(self.exchanges) > PENDING_LIMIT:
                self.exchanges.popitem(last=False)
            self.pending.pop(lid, None)
        elif kind == 'discord_owner':
            self._his_discord(conn, item)
        elif kind == 'reaction':
            row = self._row(conn, item.ids[0])
            if self._visible(row, owner) and is_discord(row['channel']):
                for name, on in zip(('react_positive', 'react_negative'), item.values, strict=True):
                    if on:
                        self.sink.put(conn, row['id'], row['from_guid'], name, 1, now)
                    else:
                        self.sink.drop(conn, row['id'], name)
        elif kind == 'emote':
            guid = item.ids[0]
            row = self._latest(conn, guid, owner, now, 60)
            if row is not None:
                S.put(conn, row['id'], guid, item.values[0], 1, now)
        elif kind == 'activity':
            self.active = now
        elif kind == 'presence':
            self.online = bool(item.values[0])
            if not self.online:
                self._reset(lost=False)
        elif kind == 'afk':
            self.afk = bool(item.values[0])
            if self.afk:
                self._interrupt()
        elif kind == 'combat':
            self.combat, self.combat_at = item.values[0], now
            if self.combat is not False:
                self._interrupt()
        elif kind == 'group':
            # Never let a group/thread change attribute an old group's line to the new one.
            self.pending.clear()
            self.exchanges.clear()
            self.recent.clear()
        elif kind == 'muted':
            row = conn.execute('SELECT line_id FROM voice_line WHERE speak_id=? AND state=? '
                               'AND reason=?', (item.ids[0], 'dropped', 'muted')).fetchone()
            line = self._row(conn, row[0]) if row else None
            if self._visible(line, owner):
                S.put(conn, line['id'], line['from_guid'], 'muted', 1, now)
                self.pending.pop(line['id'], None)
                self.recent.pop(line['id'], None)
        elif kind in ('dial_down', 'nudged_against'):
            for guid in item.ids:
                row = self._latest(conn, guid, owner, now, S.REPLY_SECONDS, item.values[0] if item.values else 'everywhere')
                if row is not None:
                    S.put(conn, row['id'], guid, kind, 1, now)
        elif kind == 'feedback':
            return S.feedback(conn, {'line_id': item.ids[0], 'value': item.values[0],
                                     'reasons': list(item.values[1])}, owner, now)

    def _interrupt(self):
        self.pending = OrderedDict((key, (row, addressed, True))
                                   for key, (row, addressed, _) in self.pending.items())

    def _reset(self, lost=True):
        if lost:  # a gap in the observations; his WoW logout alone leaves Discord's as they are
            for store in (self.dpending, self.drecent, self.dexchanges, self.chains):
                store.clear()
        self.pending.clear()
        self.recent.clear()
        self.exchanges.clear()
        self.combat, self.combat_at = None, float('-inf')
        self.active = float('-inf')

    def _latest(self, conn, guid, owner, now, seconds, place='everywhere'):
        # Only runtime channel-visible lines: a database row alone cannot prove the
        # owner was in that party, officer audience, local area or Discord room.
        rows = sorted((r for r in self.recent.values() if r['from_guid'] == guid
                       and 0 <= now - r['ts'] <= seconds), key=lambda r: (r['ts'], r['id']), reverse=True)
        for row in rows:
            channel = row['channel']
            fits = (place == 'everywhere' or place == 'whispers' and direct(channel)
                    or place == 'discord' and channel.startswith(('discord:', 'dm:', 'discord_dm:'))
                    or place == 'game' and not channel.startswith(('discord:', 'dm:', 'discord_dm:')))
            if fits and self._visible(row, owner):
                return row
        return None

    def _expire(self, conn, now):
        self._expire_discord(now)
        for lid, (row, addressed, interrupted) in list(self.pending.items()):
            if now - row['ts'] < S.REPLY_SECONDS:
                continue
            if (addressed and not interrupted and self.online and not self.afk and self.combat is False
                    and 0 <= now - self.combat_at <= COMBAT_MAX_AGE_S
                    and 0 <= now - self.active <= S.REPLY_SECONDS):
                S.put(conn, lid, row['from_guid'], 'ignored', 1, now)
            self.pending.pop(lid, None)
