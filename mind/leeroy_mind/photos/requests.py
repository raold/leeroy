"""Explicit owner photo requests: prepare first, then use the ordinary Discord pipeline."""
import asyncio
from contextlib import asynccontextmanager
import time

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import CHANNEL_PREFIX, channel_id
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.ration import SEND_MIN_AGE
from leeroy_mind.selfies.scene import scene_json
from leeroy_mind.selfies.store import current
from leeroy_mind.selfies.style import LUST, style_of
from leeroy_mind.talk.pipeline import COMPOSING, TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from .offer import PhotoOffer
from .request_words import request_target
from . import request_store as records


class RequestPhotos:
    def __init__(self, app, sender):
        self.app, self.sender = app, sender
        self._tasks = {}
        self._reserved = set()
        self._retry_at = {}
        self._closed = False
        self._hello_seen = bool(getattr(app, 'hello', None))
        self._offline_at = app.clock.now() if self.known_offline() else None
        self.worker_factory = None  # fake-only injection; production uses the identity worker.
        app.register_connection_hook(self._connection)
        for kind in ('hello', 'login', 'logout'):
            app.register_event_handler(kind, self._presence)
        app.register_background(self.run)
        app.register_shutdown_drain(self.close)
        app.engine.add_instruction_hook(self.instruction)

    async def _connection(self, up):
        self._hello_seen = False
        self._offline_at = None
        self.app.arbiter.cancel_borrow('presence_unknown')

    async def _presence(self, event):
        from leeroy_mind.protocol import Hello
        if isinstance(event, Hello):
            self._hello_seen = True
        elif not getattr(event, 'human', False):
            return
        if not self.known_offline():
            self._offline_at = None
            self.app.arbiter.cancel_borrow('presence_changed')
        elif self._offline_at is None:
            self._offline_at = self.app.clock.now()

    def known_offline(self):
        app = self.app
        return bool(self._hello_seen and getattr(app.bridge, 'connected', False)
                    and app.night_scheduler.presence.online is False and not app.world.human_online)

    def _ready_member(self, guid):
        app = self.app
        p, pick = app.personas.get(guid), current(app.store, guid)
        if (p is None or p.appearance is None or p.identity.age < SEND_MIN_AGE or pick is None
                or app.store.get_meta(f'selfies:variety-pick:{guid}') == str(pick.pick)
                or not files.history(app.data_dir, guid, pick.pick).is_file()):
            return None
        return p, pick

    def begin(self, message):
        """True means this service will hand the stored post to DiscordTalk exactly once."""
        guid = request_target(self.app, message)
        if guid is None or self._closed:
            return False
        if not self._initial_open(message, guid):
            return True
        if message not in self._tasks:
            self.app.discord_talk.begin_photo()
            task = asyncio.create_task(self._prepare_then_answer(message, guid), name='selfie-request')
            self._tasks[message] = task
            task.add_done_callback(lambda done, mid=message: self._done(mid, done))
        return True

    def _initial_open(self, message, guid):
        request = records.get(self.app.store, message)
        if request is not None:
            return request.initial_message is None and not request.ended and self._source_open(request)
        return self.app.store.conn.execute(
            "SELECT 1 FROM discord_message WHERE reply_to=? AND author=? AND origin='mind' LIMIT 1",
            (message, guid)).fetchone() is None

    def _source_open(self, request):
        row = self.app.store.conn.execute('SELECT channel,thread FROM discord_message WHERE id=?',
                                          (request.message,)).fetchone()
        return bool(row and row['channel'] == request.asked_channel and row['thread'] == request.thread
                    and request_target(self.app, request.message) == request.guid)

    def _cancel(self, request):
        with self.app.store.transaction():
            self.app.store.conn.execute("UPDATE selfie_request SET ended='cancelled' WHERE message=? AND ended IS NULL",
                                       (request.message,))
            latest = records.get(self.app.store, request.message)
            for sid in (latest.fresh_snap, latest.fallback_snap):
                photo = records.snap(self.app.store, sid)
                if photo is not None and photo.status in {'planned', 'ready'}:
                    snaps.expire(self.app.store, sid, 'passed', now=self.app.clock.now())

    def _done(self, message, task):
        if self._tasks.get(message) is task:
            self._tasks.pop(message, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            log('selfie_request_failed', request=message, code=type(error).__name__)

    def _retry_after(self, message, seconds):
        self._retry_at[message] = self.app.clock.now() + seconds
        while len(self._retry_at) > 512:
            self._retry_at.pop(next(iter(self._retry_at)))

    @asynccontextmanager
    async def _typing(self, guid, place):
        app, owner = self.app, asyncio.current_task()
        turn = Turn(TurnKind.REPLY_HUMAN, guid, place,
                    to_guid=app.human_guid if layout.dm_peer(place) is not None else 0)
        prior = app.engine._current.get(owner)
        app.engine._current[owner] = turn
        async def refresh():
            while True:
                app.engine._phases[owner] = (turn, COMPOSING, app.clock.now())
                await app.clock.sleep(5)
        app.engine._phase(turn, COMPOSING)
        refresher = asyncio.create_task(refresh())
        try:
            yield
        finally:
            refresher.cancel()
            await asyncio.gather(refresher, return_exceptions=True)
            app.engine._phase(turn, None)
            if prior is None:
                app.engine._current.pop(owner, None)
            else:
                app.engine._current[owner] = prior

    async def _prepare_then_answer(self, message, guid):
        app = self.app
        row = app.store.conn.execute('SELECT channel,thread,text FROM discord_message WHERE id=?', (message,)).fetchone()
        if row is None:
            return
        place = row['channel'] if layout.dm_peer(row['channel']) is not None else CHANNEL_PREFIX + row['channel']
        request = None
        outcome = 'words'
        try:
            async with self._typing(guid, place):
                p = app.personas.get(guid)
                private = layout.dm_peer(row['channel']) is not None or style_of(p) == LUST
                request, outcome = records.claim(app.store, message=message, guid=guid,
                    asked_channel=row['channel'], thread=row['thread'],
                    photo_channel=f'dm:{guid}' if private else row['channel'],
                    audience='him' if private else 'anyone', now=app.clock.now())
                if request is not None:
                    await self._prepare(request, row['text'], immediate=outcome == 'accepted')
                    request = records.get(app.store, message)
            if request is not None and request.photo_channel != request.asked_channel:
                await self._send(request, 'private')
        except asyncio.CancelledError:
            raise  # restart can resume the durable plan; shutdown never sends an answer.
        except Exception as error:
            outcome = type(error).__name__
            if request is not None:
                records.fail(app.store, message, 'preparation_failed', now=app.clock.now())
        finally:
            log('selfie_request', guid=guid, channel_kind='dm' if layout.dm_peer(row['channel']) else 'discord', outcome=outcome)
        if not self._closed and not app.stopping and (request is None or self._source_open(request)):
            self._retry_after(message, 180)
            answer = app.discord_talk.on_his_post(message, immediate=True)
            if answer is not None:
                # Preparation and its ordinary reply have one owner. The retry timer
                # must not admit a second reply while the first still owns its photo.
                await answer

    async def _prepare(self, request, words, *, immediate=False):
        app = self.app
        if request.ended or request.failure or request.fresh_message is not None:
            return
        if not self._source_open(request):
            self._cancel(request)
            return
        if app.cfg.talk.selfies <= 0:
            records.fail(app.store, request.message, 'dial_zero', now=app.clock.now())
            return
        ready = self._ready_member(request.guid)
        if ready is None:
            records.fail(app.store, request.message, 'no_profile', now=app.clock.now())
            return
        p, pick = ready
        if request.fresh_snap is None:
            from leeroy_mind.selfies.request_scene import choose_scene
            scene = await choose_scene(app, request.guid, words, request.audience)
            if not self._source_open(request):
                self._cancel(request)
                return
            photo = records.plan_fresh(app.store, request, scene=scene_json(scene), pick=pick.pick,
                                      looks=snaps.looks_hash(p.appearance), now=app.clock.now())
        else:
            photo = records.snap(app.store, request.fresh_snap)
        waited = self._offline_at is not None and app.clock.now() - self._offline_at >= 600
        if photo is not None and photo.status == 'planned' and self.known_offline() and (immediate or waited):
            await self._render(request, photo)
        request = records.get(app.store, request.message)
        fresh = records.snap(app.store, request.fresh_snap)
        if (not request.failure and not request.ended and request.fallback_snap is None
                and fresh is not None and fresh.status == 'planned'):
            from .request_stock import reserve_fallback
            reserve_fallback(app, request, now=app.clock.now())

    async def _render(self, request, photo):
        from leeroy_mind.nightly.gpu import LiveLeaseHeld, PausedRoom
        from leeroy_mind.selfies.request_render import render_request
        app = self.app
        if not self.known_offline() or app.gpu.paused or app.nightly.running is not None:
            return False
        if not records.borrow_available(app.store, now=app.clock.now(), local_date=app.clock.local().date().isoformat()):
            return False
        def admitted():
            return (self.known_offline() and self._source_open(request)
                    and app.nightly.running is None and not app.gpu.paused
                    and records.admit_borrow(app.store, request.message, now=app.clock.now(),
                                             local_date=app.clock.local().date().isoformat()))
        started, room, result, outcome = time.perf_counter(), None, None, 'refused'
        try:
            async with app.arbiter.borrow(f'selfie_request:{request.message}', on_admitted=admitted) as room:
                result = await render_request(app, photo, room, worker_factory=self.worker_factory)
                outcome = result.outcome
                if outcome != 'ready':
                    records.fail(app.store, request.message, result.code or 'render_failed', now=app.clock.now())
                return outcome == 'ready'
        except (LiveLeaseHeld, PausedRoom):
            return False
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except Exception as error:
            # A persisted render survives a failed handback. Admission remains
            # closed until recovery finishes; a restart must still send this image.
            stored = records.snap(app.store, photo.id)
            if stored is not None and stored.status == 'ready':
                outcome = 'ready_reload_pending'
                return False
            # Losing presence during a borrow leaves the plan available after logout.
            if type(error).__name__ == 'BorrowInterrupted':
                outcome = 'interrupted'
                return False
            outcome = 'timeout' if isinstance(error, TimeoutError) else 'render_failed'
            records.fail(app.store, request.message, outcome, now=app.clock.now())
            return False
        finally:
            timings = getattr(room, 'timings', {}) if room is not None else {}
            log('selfie_request_render', request=request.message, ms=round((time.perf_counter()-started)*1000),
                start_ms=round(getattr(result, 'start_ms', timings.get('start_ms', 0))),
                render_ms=round(getattr(result, 'render_ms', timings.get('render_ms', 0))),
                unload_ms=round(timings.get('unload_ms', 0)), stop_ms=round(timings.get('stop_ms', 0)),
                reload_ms=round(timings.get('reload_ms', 0)), outcome=outcome)

    def decorate(self, turn, meta, message):
        if request_target(self.app, message) != turn.speaker:
            return
        meta.photo_request = message
        meta.single_line = True
        request = records.get(self.app.store, message)
        if (request is not None and request.photo_channel != request.asked_channel
                and (request.fresh_message or request.fallback_message)):
            # The private photo counts as her turn too. Let the public reply keep
            # the existing shared-channel interval instead of being dropped by it.
            from leeroy_mind.talk.guards import REPLY_S
            turn.delay = max(turn.delay, REPLY_S)
        previous = meta.on_discord_record
        previous_valid = meta.still_valid
        meta.still_valid = lambda: (self._initial_open(message, turn.speaker)
                                    and (previous_valid is None or previous_valid()))
        def receipt(line, posted, now):
            if previous is not None:
                previous(line, posted, now)
            request = records.get(self.app.store, message)
            if request is not None:
                if not self._source_open(request):
                    raise records.RequestError('request source changed')
                if not records.mark_initial(self.app.store, message, posted):
                    raise records.RequestError('request already answered')
                if request.failure is not None and request.failure_message is None:
                    if not records.mark_delivery(self.app.store, message, 'failure', posted):
                        raise records.RequestError('request failure already reported')
        meta.on_discord_record = receipt

    def instruction(self, turn, meta, instruction):
        if meta.photo_request is None:
            return instruction
        request = records.get(self.app.store, meta.photo_request)
        if request is None:
            note = "You aren't sending another photo right now. Answer him naturally; do not claim an attachment."
        elif request.failure:
            note = ("You don't have a usable photo to send right now. Answer in your own words; don't claim an attachment or discuss software."
                    if request.failure in {'no_profile', 'dial_zero'} else
                    "The new photo you meant to send didn't work out. Let him know briefly in your own words; don't claim an attachment or discuss software.")
        elif channel_id(turn.channel) == request.asked_channel and request.photo_channel != request.asked_channel:
            delivered = request.fresh_message or request.fallback_message
            note = ("You sent the photo he asked for to him privately. Mention that in your own words; don't describe it here."
                    if delivered else "The photo he asked for will be private between you two. It isn't sent yet; don't describe it here.")
        elif meta.photo is not None:
            note = ("This is the fresh photo he asked you for earlier; send it with a short follow-up in your own words."
                    if meta.photo_request_part == 'followup' else '')
        else:
            note = "The fresh photo he asked for isn't ready yet. Answer him naturally; you can send it later. Don't claim it is attached."
        return instruction + ('\n' + note if note else '')

    def offer(self, turn, meta, now):
        from .request_stock import valid as stock_valid
        app = self.app
        request = records.get(app.store, meta.photo_request)
        if (request is None or request.guid != turn.speaker or request.ended or request.failure
                or not self._source_open(request)
                or channel_id(turn.channel) != request.photo_channel or not self.sender._enabled()
                or app.cfg.talk.selfies <= 0 or self._ready_member(turn.speaker) is None):
            return None
        if meta.photo_request_part == 'initial' and request.initial_message is not None:
            return None
        choices = ('fresh',) if meta.photo_request_part == 'followup' else ('fresh', 'fallback')
        for kind in choices:
            if getattr(request, kind + '_message') is not None:
                continue
            photo = records.snap(app.store, getattr(request, kind + '_snap'))
            if photo is None or photo.status != 'ready' or photo.id in self._reserved:
                continue
            if not self.sender._fresh(photo, now, request=True) or not stock_valid(app, request, photo, now=now):
                continue
            attachment = self.sender._attachment(photo)
            if attachment is None:
                continue
            self._reserved.add(photo.id)
            def release():
                self._reserved.discard(photo.id)
            def valid():
                latest = records.get(app.store, request.message)
                row = records.snap(app.store, photo.id)
                return bool(latest and not latest.ended and not latest.failure and self._source_open(latest)
                    and row and row.status == 'ready'
                    and getattr(latest, kind + '_message') is None and photo.id in self._reserved
                    and not app.stopping and app.cfg.talk.selfies > 0
                    and channel_id(turn.channel) == latest.photo_channel
                    and stock_valid(app, latest, row, now=app.clock.now())
                    and self.sender._fresh(row, app.clock.now(), request=True)
                    and self.sender._attachment(row) is not None)
            def sent(line, message):
                if not snaps.mark_sent(app.store, photo.id, sender=turn.speaker,
                    channel='dm' if request.audience == 'him' else 'discord', line_id=line, now=app.clock.now()):
                    raise records.RequestError('requested photo is no longer ready')
                if not records.mark_delivery(app.store, request.message, kind, message):
                    raise records.RequestError('requested photo was already delivered')
                release()
            shows = photo.scene['shows']
            return PhotoOffer(photo, "You're sending a photo of yourself: " + shows +
                ". Write the short message you'd send with it, in your own way; don't describe the photo in detail.",
                sent, release, attachment, valid)
        return None

    async def _send(self, request, part):
        app = self.app
        latest = records.get(app.store, request.message)
        if latest is None or latest.ended:
            return
        if not self._source_open(latest):
            self._cancel(latest)
            return
        if part == 'private' and (latest.fresh_message or latest.fallback_message):
            return
        if part == 'followup' and latest.fresh_message:
            return
        if part == 'failure' and (not latest.failure or latest.failure_message):
            return
        photo = records.snap(app.store, latest.fresh_snap)
        fallback = records.snap(app.store, latest.fallback_snap)
        if part != 'failure' and not any(p and p.status == 'ready' for p in (photo, fallback if part == 'private' else None)):
            return
        if part != 'failure':
            from .request_stock import valid as stock_valid
            candidates = [p for p in (photo, fallback if part == 'private' else None) if p and p.status == 'ready']
            if any(p.id in self._reserved for p in candidates):
                return
            if not any(self.sender._fresh(p, app.clock.now(), request=True)
                       and stock_valid(app, latest, p, now=app.clock.now())
                       and self.sender._attachment(p) is not None for p in candidates):
                records.fail(app.store, latest.message, 'photo_unavailable', now=app.clock.now())
                await self._send(records.get(app.store, latest.message), 'failure')
                return
        place = latest.photo_channel if latest.audience == 'him' else CHANNEL_PREFIX + latest.photo_channel
        turn = Turn(TurnKind.REPLY_HUMAN, latest.guid, place,
                    to_guid=app.human_guid if latest.audience == 'him' else 0, addressee=app.human_guid,
                    priority=Priority.REPLY_HUMAN, max_lines=1,
                    instruction="He asked you for a photo of yourself. Answer him in your own words.")
        same = latest.asked_channel == latest.photo_channel
        meta = TurnMeta(app.engine._new_id(), why='selfie:request', answers_human=True,
            single_line=True, keep_channel=True, review_required=True, photo_request=latest.message,
            photo_request_part=part, discord=DiscordTarget(latest.message if same else None, latest.thread if same else None))
        def receipt(line, message, now):
            if not self._source_open(latest):
                raise records.RequestError('request source changed')
            if part == 'failure':
                if not records.mark_delivery(app.store, latest.message, 'failure', message):
                    raise records.RequestError('request failure already reported')
            else:
                delivered = records.get(app.store, latest.message)
                if message not in (delivered.fresh_message, delivered.fallback_message):
                    raise records.RequestError('request photo was not attached')
                if part == 'followup' and not records.mark_delivery(app.store, latest.message, 'followup', message):
                    raise records.RequestError('request followup already delivered')
        def valid():
            row = records.get(app.store, latest.message)
            if row is None or row.ended or not self._source_open(row):
                return False
            if part == 'failure':
                return bool(row.failure and row.failure_message is None)
            return not row.failure and (row.fresh_message is None if part == 'followup'
                                        else row.fresh_message is None and row.fallback_message is None)
        meta.still_valid = valid
        meta.on_discord_record = receipt
        app.discord_talk.begin_photo()
        await app.engine.speak_meta(turn, meta)

    async def tick(self):
        app = self.app
        if self._closed or app.stopping or app.gpu.paused or app.cfg.talk.selfies <= 0:
            return
        if not self.known_offline():
            self._offline_at = None
        elif self._offline_at is None:
            self._offline_at = app.clock.now()
        for request in records.due(app.store, now=app.clock.now()):
            if not self._source_open(request):
                self._cancel(request)
                continue
            if request.message in self._tasks or self._retry_at.get(request.message, 0) > app.clock.now():
                continue
            self._retry_after(request.message, 60)
            if request.initial_message is None:
                self.begin(request.message)
                continue
            if request.failure:
                await self._send(request, 'failure')
                continue
            photo = records.snap(app.store, request.fresh_snap)
            if photo is not None and photo.status == 'expired':
                records.fail(app.store, request.message, 'photo_unavailable', now=app.clock.now())
                await self._send(records.get(app.store, request.message), 'failure')
                continue
            if photo is not None and photo.status == 'planned':
                if (self._offline_at is None or app.clock.now()-self._offline_at < 600
                        or app.nightly.running is not None):
                    continue
                async with self._typing(request.guid, request.photo_channel if request.audience == 'him'
                                        else CHANNEL_PREFIX + request.photo_channel):
                    await self._render(request, photo)
            latest = records.get(app.store, request.message)
            await self._send(latest, 'failure' if latest.failure else 'followup')

    async def run(self):
        while not self._closed and not self.app.stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log('selfie_request_queue_failed', code=type(error).__name__)
            await self.app.clock.sleep(15)

    async def close(self):
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
