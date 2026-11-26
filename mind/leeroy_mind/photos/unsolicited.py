"""[photos] unsolicited: a highly romantic member DMs him a fresh selfie of her own accord, only
while the GPU is idle (owner, 2026-11-25: "when gpu is idle id like bots that are highly romantic
to send selfies unsolicited").

On by default (`[photos] unsolicited`); it needs `requests` (the borrow gate and the request render). Once a minute, while
nothing else wants the GPU (no Free GPU pause; no WoW login, offline raid, voice or Discord desk
lease, only the server's pin; no night running or due; no booth, bulk or request render; no
request waiting; he offline from WoW for ten minutes), it may, at CHANCE a minute, pick one woman
whose attitude to him is lust, or drawn and at least at late nights on their arc, who is up and on
Discord, her own romance dial (or the world's) at MIN_DIAL or more, weighted twice for one he has
been heating up with in their DM lately (talk.romance.his_heat). It renders one photo of her from
the request's enum scene (no words of his; the night's content rules, NSFW gate and age floor),
then she sends it in her DM with a line of her own. His WoW login or Discord post stops a render
in progress. Caps: one a member a local day (also the snap's unique ref), DAY_CAP a local day
across the guild, GAP_S apart, all spent at admission; the selfies dial at 0 means none. An
unsent photo stays her `him` stock until it expires. Logs ids and codes only."""
import asyncio
import time

from leeroy_mind.discord import layout
from leeroy_mind.discord.presence import RNG_SEED, her_local, presence_of
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.selfies import snaps
from leeroy_mind.selfies.ration import SEND_MIN_AGE
from leeroy_mind.storage.rows import ROMANCE_STAGES
from leeroy_mind.talk.turns import Turn, TurnKind
from . import request_store as records

WHY = 'selfie:unsolicited'
TICK_S = 60.0
CHANCE = 1 / 30  # an idle minute's draw: about one an idle half hour, inside the caps
DAY_CAP = 3
GAP_S = 2 * 3600.0
OFFLINE_S = 600.0
HEAT_S = 3 * 86400.0
LIFETIME_S = 6 * 3600.0
MIN_DIAL = 6
DEEP_STAGE = ROMANCE_STAGES.index('late_nights')
DAY_KEY = 'photos:unsolicited:day:{date}'
LAST_KEY = 'photos:unsolicited:last'
MEMBER_KEY = 'photos:unsolicited:member:{guid}'
INSTRUCTION = 'Start a private Discord message to him in your own words.'


def _number(raw, default=0.0):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value == value else default


def _count(store, date: str) -> int:
    """Today's unsolicited photos; a damaged counter reads as the cap (never another one)."""
    raw = store.get_meta(DAY_KEY.format(date=date))
    return 0 if raw is None else int(_number(raw, DAY_CAP))


class UnsolicitedPhotos:
    def __init__(self, app, sender, requests, rng):
        self.app, self.sender, self.requests, self.rng = app, sender, requests, rng
        self._closed = False
        self.worker_factory = None  # fake-only injection; production uses the identity worker
        app.register_background(self.run)
        app.register_shutdown_drain(self.close)

    # -- when ----------------------------------------------------------------------------------

    def idle(self) -> bool:
        """Nothing else wants the GPU (the server's own pin may stand: a borrow coexists with it)."""
        app = self.app
        arbiter, scheduler = app.arbiter, app.night_scheduler
        busy = getattr(arbiter, 'borrow_busy', None)
        gate = getattr(app, 'live_borrow', None)
        return bool(not self._closed and not app.stopping and not app.gpu.paused
                    and not getattr(arbiter, 'paused', False)
                    and not arbiter.holders - {'server'} and arbiter.borrow_holder is None
                    and not (busy is not None and busy())
                    and not (gate is not None and gate.blocked)
                    and app.nightly.running is None and scheduler.due() is None
                    and self.requests.known_offline()
                    and scheduler.presence.offline_seconds() >= OFFLINE_S
                    and not self._requests_open())

    def _requests_open(self) -> bool:
        now = self.app.clock.now()
        with self.app.store.transaction():
            return self.app.store.conn.execute(
                'SELECT 1 FROM selfie_request WHERE ended IS NULL AND fresh_message IS NULL '
                'AND failure_message IS NULL AND expires>? LIMIT 1', (now,)).fetchone() is not None

    def _room(self, date: str, now: float) -> bool:
        store = self.app.store
        return _count(store, date) < DAY_CAP and now - _number(store.get_meta(LAST_KEY)) >= GAP_S

    # -- who -----------------------------------------------------------------------------------

    def _heated(self, guid: int, now: float) -> bool:
        """He turned things up in their DM lately (romance.his_heat on one of his recent lines)."""
        from leeroy_mind.talk.romance import his_heat
        app = self.app
        with app.store.transaction():
            rows = app.store.conn.execute(
                "SELECT text FROM discord_message WHERE channel=? AND author=? AND origin='human' "
                'AND deleted IS NULL AND ts>? ORDER BY ts DESC LIMIT 40',
                (f'dm:{guid}', app.human_guid, now - HEAT_S)).fetchall()
        return any(his_heat(row[0]) for row in rows)

    def _up(self, guid: int, p, member, now: float) -> bool:
        app = self.app
        presence = presence_of(p, local=her_local(app.clock.local(), p),
                               body_online=guid in app.world.online, body_zone=member.zone,
                               rng_seed=RNG_SEED)
        if presence.state not in {'online', 'idle'}:
            return False
        return app.store.conn.execute(
            'SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL '
            'AND (returns_at IS NULL OR returns_at>?) LIMIT 1', (guid, now)).fetchone() is None

    def candidates(self, now: float, date: str) -> list[tuple[int, float]]:
        app = self.app
        human = app.human_guid
        out = []
        for guid, member in app.world.discord_members().items():
            p = app.personas.get(guid)
            if (guid == human or p is None or p.identity.player_gender != 'female'
                    or p.identity.age < SEND_MIN_AGE or self.requests._ready_member(guid) is None):
                continue
            rel = app.store.get_relationship(guid, human, lens=True)
            stage = getattr(rel, 'romance_stage', 'none')
            deep = stage in ROMANCE_STAGES and stage != 'ex' and ROMANCE_STAGES.index(stage) >= DEEP_STAGE
            weight = 2.0 if p.romance.stance == 'lust' else 1.0 if p.romance.stance == 'drawn' and deep else 0.0
            dial = p.romance.dial if p.romance.dial is not None else app.cfg.romance.dial
            if weight <= 0 or dial < MIN_DIAL:
                continue
            last = _number(app.store.get_meta(MEMBER_KEY.format(guid=guid)))
            if (app.store.get_meta(MEMBER_KEY.format(guid=guid) + ':date') == date
                    or now - last < 20 * 3600 or not self._up(guid, p, member, now)):
                continue
            out.append((guid, weight * (2.0 if self._heated(guid, now) else 1.0)))
        return out

    def _draw(self, candidates):
        total = sum(w for _, w in candidates)
        mark = self.rng.random() * total
        for guid, weight in candidates:
            mark -= weight
            if mark < 0:
                return guid
        return candidates[-1][0]

    # -- the photo -----------------------------------------------------------------------------

    async def tick(self) -> None:
        app = self.app
        if (app.cfg.talk.selfies <= 0 or not self.sender._enabled() or not self.idle()):
            return
        now, date = app.clock.now(), app.clock.local().date().isoformat()
        if not self._room(date, now) or self.rng.random() >= CHANCE:
            return
        found = self.candidates(now, date)
        if not found:
            return
        await self.make(self._draw(found), date)

    async def make(self, guid: int, date: str) -> str:
        """Choose, render and send one; the outcome code (also logged)."""
        outcome = 'refused'
        snap_id = None
        try:
            outcome, snap_id = await self._render(guid, date)
            if outcome == 'ready':
                outcome = 'sent' if await self._send(guid, snap_id) else 'unsent'
        finally:
            log('selfie_unsolicited', guid=guid, snap=snap_id, outcome=outcome)
        return outcome

    async def _scene(self, guid: int):
        from leeroy_mind.llm.client import GpuPaused, LLMError
        from leeroy_mind.selfies.request_scene import RequestSceneError, choose_scene
        from leeroy_mind.selfies.scene import scene_json
        try:
            return scene_json(await choose_scene(self.app, guid, 'a photo of yourself, sent to him '
                                                 'unprompted', 'him'))
        except (RequestSceneError, GpuPaused, LLMError):
            return None

    async def _render(self, guid: int, date: str):
        from leeroy_mind.llm.borrow import LiveReloadFailed
        from leeroy_mind.llm.client import GpuPaused
        from leeroy_mind.nightly.gpu import LiveLeaseHeld
        from leeroy_mind.selfies.request_render import render_request
        from leeroy_mind.selfies.store import current
        app = self.app
        scene = await self._scene(guid)
        if scene is None or scene.get('audience') != 'him':
            return 'no_scene', None
        if not self.idle():
            return 'busy', None
        if (not self._room(date, app.clock.now())
                or app.store.get_meta(MEMBER_KEY.format(guid=guid) + ':date') == date):
            return 'cap', None
        ready = self.requests._ready_member(guid)
        if ready is None:
            return 'no_profile', None
        p, pick = ready
        state = {'snap': None}

        def admitted():
            """Inside the arbiter's lock, before any model moves: his Discord desk lease means
            he's chatting (not idle); the caps are spent and the photo planned here, at once."""
            if 'discord' in app.arbiter.holders or app.gpu.paused:
                return False
            now = app.clock.now()
            with app.store.transaction():
                count = _count(app.store, date)
                if (count >= DAY_CAP or now - _number(app.store.get_meta(LAST_KEY)) < GAP_S
                        or app.store.get_meta(MEMBER_KEY.format(guid=guid) + ':date') == date):
                    return False
                sid = snaps.plan_snap(app.store, kind='situation', guid=guid, source='everyday',
                    ref=f'unsolicited:{date}', night=date, scene=scene, audience='him',
                    phases=records.PHASES, not_before=now, expires=now + LIFETIME_S, now=now,
                    pick=pick.pick, looks=snaps.looks_hash(p.appearance))
                if sid is None:
                    return False
                app.store.set_meta(DAY_KEY.format(date=date), str(count + 1))
                app.store.set_meta(LAST_KEY, repr(now))
                app.store.set_meta(MEMBER_KEY.format(guid=guid), repr(now))
                app.store.set_meta(MEMBER_KEY.format(guid=guid) + ':date', date)
            state['snap'] = sid
            return True

        async def watch(arbiter):
            while True:
                await asyncio.sleep(.5)
                if arbiter.holders & {'discord', 'human'} or app.gpu.paused:
                    arbiter.cancel_borrow('owner_active')
                    return

        started, room, result, outcome = time.perf_counter(), None, None, 'refused'
        work_timeout = 600.0 if app.selfies.cfg.backend == 'flux1-dev' else None
        watcher = None
        try:
            async with app.arbiter.borrow(f'selfie_unsolicited:{guid}', on_admitted=admitted,
                                          work_timeout=work_timeout) as room:
                watcher = asyncio.create_task(watch(app.arbiter))
                photo = records.snap(app.store, state['snap'])
                factory = self.worker_factory or self.requests.worker_factory
                result = await render_request(app, photo, room, worker_factory=factory)
                outcome = result.outcome if result.outcome == 'ready' else (result.code or 'failed')
        except (LiveLeaseHeld, GpuPaused):
            outcome = 'busy'
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except Exception as error:
            stored = records.snap(app.store, state['snap']) if state['snap'] else None
            if stored is not None and stored.status == 'ready':
                outcome = 'ready'  # rendered; a failed handback leaves admission to recovery
            elif type(error).__name__ == 'BorrowInterrupted':
                outcome = 'interrupted'
            elif isinstance(error, LiveReloadFailed):
                outcome = 'reload_failed'
            else:
                outcome = 'timeout' if isinstance(error, TimeoutError) else 'render_failed'
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            sid = state['snap']
            photo = records.snap(app.store, sid) if sid else None
            if photo is not None and photo.status == 'planned':
                # never left for the night to render: this photo was for now
                snaps.expire(app.store, sid, 'passed', now=app.clock.now())
            timings = getattr(room, 'timings', {}) if room is not None else {}
            log('selfie_unsolicited_render', guid=guid, snap=sid,
                ms=round((time.perf_counter() - started) * 1000),
                start_ms=round(getattr(result, 'start_ms', timings.get('start_ms', 0))),
                render_ms=round(getattr(result, 'render_ms', timings.get('render_ms', 0))),
                reload_ms=round(timings.get('reload_ms', 0)), outcome=outcome)
        return outcome, state['snap']

    async def _send(self, guid: int, snap_id: int) -> bool:
        from leeroy_mind.talk.pipeline import TurnMeta
        app = self.app
        human = app.human_guid
        if app.stopping or self._closed or layout.dm_peer(f'dm:{guid}') != guid:
            return False
        turn = Turn(TurnKind.OPEN, guid, f'dm:{guid}', to_guid=human, addressee=human,
                    priority=Priority.BACKGROUND, max_lines=1, instruction=INSTRUCTION)
        meta = TurnMeta(app.engine._new_id(), why=WHY, initiates=True, single_line=True,
                        review_required=True, keep_channel=True, photo_seed=snap_id,
                        discord=DiscordTarget(None, None))
        before = app.store.conn.execute("SELECT status FROM selfie_snap WHERE id=?", (snap_id,)).fetchone()
        if before is None or before[0] != 'ready':
            return False
        await app.engine.speak_meta(turn, meta)
        after = app.store.conn.execute("SELECT status FROM selfie_snap WHERE id=?", (snap_id,)).fetchone()
        return after is not None and after[0] == 'sent'

    async def run(self) -> None:
        while not self._closed and not self.app.stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log('selfie_unsolicited_failed', code=type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def close(self) -> None:
        self._closed = True
