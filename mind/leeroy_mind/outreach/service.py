"""One delivery-owned list across the existing game and Discord clocks."""
import asyncio
import hashlib
import json
import math
import random
import re

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import is_discord, is_group
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.storage.rows import cosine, unpack_vec
from . import store as records
from .sources import source_alive, source_embedding

NUDGE = "You sent him a DM he hasn't answered. Ask whether he saw it, without repeating the message."
TOPIC = "bring up the one thing you mean to tell {them}"


def _named_item(about, text):
    names = re.findall(r'\[([^\]]{3,100})\]|["“]([^"”]{3,100})["”]', about)
    return any((a or b).casefold() in text.casefold() for a, b in names)


class Outreach:
    def __init__(self, app):
        self.app = app
        self._held = {}
        self._bound = {}
        self._tasks = set()
        self._checking = {}
        self._vectors = {}
        self._lately_events = {}
        self._closed = False
        app.engine.outreach = self
        app.engine.add_instruction_hook(self.instruction)
        app.register_event_handler('login', self.presence)
        app.register_event_handler('logout', self.presence)
        app.register_connection_hook(self.connection)
        app.register_shutdown_drain(self.close)
        # A nudge belongs to one witnessed login, never a restart's stale session.
        self._end_nudges()

    def owes(self, guid):
        return bool(self.app.discord_talk.owes(guid))

    def eligible(self, guid):
        return (not self._closed and self.app.engine.cfg.talk.chattiness > 0
                and not self.owes(guid) and not self._checking.get(guid))

    def fallback_seed(self, guid, seed):
        # A tracked source belongs to the list even after it ends or while another
        # transport holds it. The old seed fallback must not bypass that receipt.
        parts = seed.key.split(':')
        ref = ':'.join(parts[:2]) if seed.kind == 'plan' else seed.key
        return self.app.store.conn.execute(
            'SELECT 1 FROM outreach_item WHERE guid=? AND ref=?', (guid, ref)).fetchone() is None

    def note_lately(self, guid, what, event):
        """Keep the exact native kill behind an opener note, not its rendered name."""
        if what != 'kill':
            return
        key = (guid, what)
        self._lately_events.pop(key, None)
        self._lately_events[key] = (event.ts, event.map, event.instance_id,
                                    event.boss_id, tuple(event.members))
        while len(self._lately_events) > 512:
            self._lately_events.pop(next(iter(self._lately_events)))

    def fallback_lately(self, guid, what):
        identity = self._lately_events.get((guid, what))
        if identity is None:
            return True
        native_ts, map_id, instance, boss, members = identity
        # Resolve only this member's tracked event references by the event primary key.
        # A different kill of the same boss has its own native receipt and remains eligible.
        row = self.app.store._one(
            'SELECT 1 FROM outreach_item i JOIN game_event e '
            'ON e.id=CAST(substr(i.ref,9) AS INTEGER) '
            "WHERE i.guid=? AND i.kind='his_day' AND i.ref='his_day:'||e.id AND e.type='boss' "
            "AND json_extract(e.payload,'$.state')='killed' "
            "AND json_extract(e.payload,'$.ts')=? AND json_extract(e.payload,'$.map')=? "
            "AND json_extract(e.payload,'$.instance_id')=? AND json_extract(e.payload,'$.boss_id')=? "
            "AND json_extract(e.payload,'$.members')=json(?) LIMIT 1",
            (guid, native_ts, map_id, instance, boss, json.dumps(members)))
        return row is None

    def _alive(self, item, *, delivered_line=None):
        return source_alive(self.app.store, item, now=self.app.clock.now(),
            human=self.app.human_guid, local=self.app.clock.local(),
            persona=self.app.personas.get(item.guid), delivered_line=delivered_line,
            ident=self.app.engine.human, library=getattr(self.app, 'library', None))

    def select(self, guid, via):
        if via not in records.VIA or not self.eligible(guid):
            return None
        now = self.app.clock.now()
        records.expire(self.app.store, now)
        for item in records.open_items(self.app.store, guid):
            if not self._alive(item):
                records.drop(self.app.store, item.id, 'superseded')
        for item in sorted(records.open_items(self.app.store, guid), key=lambda r: (-r.weight, r.created, r.id)):
            if (item.not_before <= now < item.expires and item.id not in self._held
                    and (item.audience == 'guild' or via in {'whisper', 'dm'})
                    and (item.kind != 'dm_nudge' or via == 'whisper')):
                return item
        return None

    def _via(self, turn, item):
        human = self.app.human_guid
        if turn.speaker != item.guid or turn.addressee != human:
            raise AssertionError('outreach recipient changed')
        if turn.channel == 'whisper' and turn.to_guid == human:
            via = 'whisper'
        elif layout.dm_peer(turn.channel) == item.guid and turn.to_guid == human:
            via = 'dm'
        elif (turn.channel in {'guild', 'party'} and item.audience == 'guild'
              and turn.to_guid in (0, human)):
            via = turn.channel
        else:
            raise AssertionError('outreach audience widened')
        if item.kind == 'dm_nudge' and via != 'whisper':
            raise AssertionError('DM nudge outside whisper')
        return via

    def bind(self, turn, meta, item):
        via = self._via(turn, item)  # fail closed before any private text joins instructions
        if item.id in self._held:
            return False
        self._held[item.id] = meta.turn_id
        previous = meta.still_valid
        self._bound[meta.turn_id] = (meta.topic, meta.recall_query, meta.server_anchor,
                                     meta.single_line, previous)
        meta.outreach_item = item.id
        meta.single_line = True
        # The instruction hook alone puts the item's words in the prompt; recall still looks for them.
        meta.topic = TOPIC.format(them=self.app.engine.human.forms[1])
        meta.recall_query = item.about
        if via in {'guild', 'party'}:
            meta.keep_channel = True  # typed where its receipt can name it, never moved into a voice room
        if is_discord(turn.channel):
            meta.server_anchor = item.about
        meta.still_valid = lambda: (self.valid(turn, meta) and (previous is None or previous()))
        return True

    def unbind(self, meta):
        """A photo goes out on this turn instead: the item stays open for a later one."""
        saved = self._bound.get(meta.turn_id)
        if meta.outreach_item is None or saved is None:
            return
        self.release(meta)
        meta.outreach_item = None
        meta.topic, meta.recall_query, meta.server_anchor, meta.single_line, meta.still_valid = saved

    def valid(self, turn, meta):
        item = records.get(self.app.store, meta.outreach_item)
        if (item is None or item.state != 'open' or not self.eligible(item.guid)
                or self._held.get(item.id) != meta.turn_id
                or not item.not_before <= self.app.clock.now() < item.expires):
            return False
        self._via(turn, item)
        if not self._alive(item):
            records.drop(self.app.store, item.id, 'superseded')
            return False
        return True

    def instruction(self, turn, meta, instruction):
        if meta.outreach_item is None:
            return instruction
        item = records.get(self.app.store, meta.outreach_item)
        if item is None:
            raise AssertionError('missing outreach item')
        self._via(turn, item)
        return instruction + '\nThis is the one thing you mean to tell him now, in your own words: ' + item.about

    def record(self, turn, meta, line_id, now):
        """DB-only, called inside the same transaction as the delivered chat line."""
        if meta.outreach_item is None or meta.said:
            return
        item = records.get(self.app.store, meta.outreach_item)
        if item is None:
            return
        via = self._via(turn, item)
        if item.state == 'open':
            if now >= item.expires:
                records.drop(self.app.store, item.id, 'expired')
                return
            if not self._alive(item, delivered_line=line_id):
                records.drop(self.app.store, item.id, 'superseded')
                return
            if not records.told(self.app.store, item.id, via=via, line_id=line_id, now=now):
                raise RuntimeError('outreach receipt refused')
        # A source can expire or be raised while a native ack is in flight. Its
        # terminal state still prevents reuse; keep the actual delivered chat row.

    def delivered(self, meta, line_id):
        item = records.get(self.app.store, meta.outreach_item)
        if item is not None and item.state == 'told' and item.line_id == line_id:
            log('outreach_told', guid=item.guid, kind=item.kind, via=item.told_via)
        self.release(meta, pending=False)

    def release(self, meta, *, pending=True):
        if meta.outreach_item is None:
            return
        if pending and any(parts[2] is meta for parts in self.app.engine._unacked.values()):
            return  # native may still confirm this exact delivery by echo
        self._bound.pop(meta.turn_id, None)
        if self._held.get(meta.outreach_item) == meta.turn_id:
            self._held.pop(meta.outreach_item, None)

    def _end_nudges(self):
        for item in records.open_items(self.app.store):
            if item.kind == 'dm_nudge':
                records.drop(self.app.store, item.id, 'expired')

    async def connection(self, up):
        if not up:
            self._end_nudges()

    async def presence(self, event):
        if not event.human or self.app.is_backlog(event):
            return
        from leeroy_mind.protocol import Logout
        if isinstance(event, Logout):
            self._end_nudges()
            return
        self._end_nudges()
        now, app = self.app.clock.now(), self.app
        for guid in app.world.discord_members():
            if guid == app.human_guid or app.personas.get(guid) is None:
                continue
            row = app.store.conn.execute('SELECT id,author,origin,ts,deleted FROM discord_message '
                'WHERE channel=? ORDER BY id DESC LIMIT 1', (f'dm:{guid}',)).fetchone()
            if (row is None or row['author'] != guid or row['origin'] != 'mind'
                    or row['deleted'] is not None
                    or not now-3*86400 < row['ts'] <= now):
                continue
            delay = random.Random(f'outreach:{guid}:{row["id"]}').uniform(120, 600)
            records.add(app.store, guid=guid, kind='dm_nudge', ref=f'dm_nudge:{row["id"]}',
                about=NUDGE, audience='pair', weight=6, created=now,
                not_before=now+delay, expires=now+3*86400)

    def _readers(self, row):
        human = self.app.human_guid
        if row.channel == 'whisper':
            return {row.to_guid} if row.from_guid == human else set()
        if (peer := layout.dm_peer(row.channel)) is not None:
            return {peer}
        readers = set(row.audience or ()) - {human}
        if is_group(row.channel):
            return readers  # his group DM is to everyone in it
        if row.to_guid and row.to_guid != human:
            return {row.to_guid} & readers
        from leeroy_mind.talk.select import mentioned
        names = {g: m.name for g, m in self.app.world.discord_members().items() if g in readers}
        return set(mentioned(row.text, names))  # a line to nobody in particular raises nothing

    def _line_alive(self, row):
        rows = self.app.store.chat_lines([row.id])
        if not rows or rows[0].text != row.text or rows[0].from_guid != self.app.human_guid:
            return False
        if is_discord(row.channel):
            current = self.app.store.conn.execute('SELECT m.deleted,m.text FROM discord_message m '
                "JOIN meta x ON x.key='discord:line:'||m.id WHERE x.value=?", (str(row.id),)).fetchone()
            return bool(current and current['deleted'] is None and current['text'] == row.text)
        return True

    def on_his_line(self, line_id):
        try:
            self._on_his_line(line_id)
        except Exception as error:
            log('outreach_match_failed', code=type(error).__name__)

    def _on_his_line(self, line_id):
        if self._closed:
            return
        rows = self.app.store.chat_lines([line_id])
        if (not rows or rows[0].origin != 'human' or rows[0].from_guid != self.app.human_guid
                or not rows[0].text.strip() or not self._line_alive(rows[0])):
            return
        row = rows[0]
        items = [item for guid in self._readers(row) for item in records.open_items(self.app.store, guid)
                 if item.created <= row.ts < item.expires and self._alive(item)]
        if not items:
            return
        pending = []
        for item in items:
            if _named_item(item.about, row.text):
                records.drop(self.app.store, item.id, 'he_raised')
            else:
                pending.append(item)
        if not pending:
            return
        loop = asyncio.get_running_loop()
        guids = {item.guid for item in pending}
        for guid in guids:
            self._checking[guid] = self._checking.get(guid, 0) + 1
        task = loop.create_task(self._raised(row, pending, guids), name='outreach-raised')
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _raised(self, row, items, guids):
        try:
            query = (await self.app.embedder.embed([row.text], Priority.EXTRACTION))[0]
            for item in items:
                if not self._line_alive(row):
                    return
                current = records.get(self.app.store, item.id)
                if current is None or current.state != 'open' or not self._alive(current):
                    continue
                vector = source_embedding(self.app.store, current)
                key = hashlib.sha256(current.about.encode()).digest()
                if vector is None:
                    vector = self._vectors.get(key)
                if vector is None:
                    vector = (await self.app.embedder.embed([current.about], Priority.EXTRACTION))[0]
                    self._vectors[key] = vector
                    while len(self._vectors) > 256:
                        self._vectors.pop(next(iter(self._vectors)))
                score = cosine(unpack_vec(query), unpack_vec(vector))
                if math.isfinite(score) and score >= .6 and self._line_alive(row) and self._alive(current):
                    records.drop(self.app.store, item.id, 'he_raised')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log('outreach_match_failed', code=type(error).__name__)
        finally:
            for guid in guids:
                count = self._checking.get(guid, 1)-1
                if count:
                    self._checking[guid] = count
                else:
                    self._checking.pop(guid, None)

    async def close(self):
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
