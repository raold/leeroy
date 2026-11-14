"""Part B's live side, attached by the server: the world database's event calendar read once a
day, the bridge's game_event start/stop kept in meta, and the raiders' loot wishes worked out
ahead of a posted raid night. No model is called; the game's database is only read."""
import asyncio
from collections import deque
from contextlib import closing
import json
import logging
from pathlib import Path
import time

from . import loot, wowcal
from .config import app_calendar, app_loot

log = logging.getLogger(__name__)
TICK_S = 300.0
PENDING = 512


def _synthetic(app) -> bool:
    """Like the guild armory, an app with an injected model client never reaches the game DB."""
    from leeroy_mind.llm.client import OllamaClient
    return hasattr(app, "llm") and not isinstance(app.llm, OllamaClient)


class CultureLive:
    def __init__(self, app, *, calendar: bool, loot_talk: bool):
        self.app, self.calendar, self.loot = app, calendar, loot_talk
        self.query = None if _synthetic(app) else self._real_query()
        guild = getattr(app, "guild", None)
        self.armory = getattr(guild, "armory", None)
        self._calendar_retry = self._wish_retry = 0.0
        self._closed = False
        self._warned = set()
        self._pending = deque(maxlen=PENDING)  # bridge events seen before the first read

    @staticmethod
    def _real_query():
        from leeroy_mind.library import mysql
        return mysql.query

    def _once(self, code):
        if code not in self._warned:
            self._warned.add(code)
            log.warning("culture_unavailable part=%s", code)

    # -- the bridge's live start/stop ------------------------------------------------------------

    async def on_game_event(self, ev):
        if self._closed or not self.calendar:
            return
        ts = getattr(ev, "ts", None)
        if type(ts) is not int:
            return
        event = (getattr(ev, "event_id", None), getattr(ev, "state", None), min(ts / 1000, self.app.clock.now()))
        try:
            done = wowcal.record(self.app.store, *event)
            if done is None:
                self._pending.append(event)
            elif done:
                log.info("culture_wowcal_event entry=%d state=%s", ev.event_id, ev.state)
        except Exception as error:
            log.warning("culture_wowcal_event_failed code=%s", type(error).__name__)

    async def on_hello(self, ev):
        """A hello from another worldserver process: runs it does not start again end."""
        if self._closed or not self.calendar:
            return
        try:
            if wowcal.note_boot(self.app.store, getattr(ev, "started_ms", 0), self.app.clock.now()):
                log.info("culture_wowcal_boot")
        except Exception as error:
            log.warning("culture_wowcal_boot_failed code=%s", type(error).__name__)

    # -- the daily read --------------------------------------------------------------------------

    async def calendar_tick(self, now: float) -> None:
        if self._closed or not self.calendar or now < self._calendar_retry:
            return
        cached = wowcal.load_calendar(self.app.store.get_meta(wowcal.CAL_KEY))
        if not wowcal.due(cached[0] if cached else None, self.app.clock.local()):
            return
        if self.query is None:
            self._once("calendar")
            self._calendar_retry = now + wowcal.RETRY_S
            return
        started = time.monotonic()
        try:
            rows = await asyncio.to_thread(wowcal.read_calendar, self.app, query=self.query)
        except Exception as error:  # never the client's text: it can carry server output
            self._calendar_retry = now + wowcal.RETRY_S
            log.warning("culture_wowcal_failed code=%s", type(error).__name__)
            return
        if self._closed:
            return
        pending = tuple(self._pending)
        wowcal.store_calendar(self.app.store, rows, self.app.clock.now(), pending)
        self._pending.clear()
        log.info("culture_wowcal_read events=%d scheduled=%d ms=%d", len(rows),
                 sum(r.scheduled for r in rows), int((time.monotonic() - started) * 1000))

    # -- the raiders' wishes ---------------------------------------------------------------------

    def _compute(self, now, roles, human):
        from leeroy_mind.console.feed import connect
        from leeroy_mind.library.core import Library
        path = Path(self.app.data_dir) / "library.db"
        if not path.is_file():
            raise FileNotFoundError("library")
        library = Library(path, clock=self.app.clock, readonly=True)
        try:
            with closing(connect(Path(self.app.data_dir) / "mind.db")) as conn:
                return loot.compute_wishes(conn, library, self.armory, now=now, raid_roles=roles, human=human)
        finally:
            library.close()

    async def wish_tick(self, now: float) -> None:
        app = self.app
        if self._closed or not self.loot or now < self._wish_retry:
            return
        if app.world.human_online:
            return  # his own raid's loot reads use the same armory; wishes can wait
        event = loot.next_raid(app.store.conn, now)
        cached = loot.load_wishes(app.store.get_meta(loot.WISH_KEY))
        if not loot.wish_due(cached, event, now):
            return
        if self.armory is None:
            self._once("loot")
            self._wish_retry = now + wowcal.RETRY_S
            return
        human = app.human_guid
        roles = {g: p.play.raid_role for g in app.world.discord_members()
                 if g != human and (p := app.personas.get(g)) is not None}
        started = time.monotonic()
        try:
            value = await asyncio.to_thread(self._compute, now, roles, human)
        except Exception as error:
            self._wish_retry = now + wowcal.RETRY_S
            log.warning("culture_loot_wish_failed code=%s", type(error).__name__)
            return
        if self._closed or value is None:
            return
        app.store.set_meta(loot.WISH_KEY, json.dumps(value, sort_keys=True))
        log.info("culture_loot_wish event=%d wishes=%d ms=%d", value["event"], len(value["wishes"]),
                 int((time.monotonic() - started) * 1000))

    async def run(self):
        while not self._closed:
            for step in (self.calendar_tick, self.wish_tick):
                try:
                    await step(self.app.clock.now())
                except Exception as error:
                    log.warning("culture_tick_failed step=%s code=%s", step.__name__, type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def close(self):
        self._closed = True


def attach_culture(app) -> CultureLive | None:
    """Nothing at all unless culture is on with its calendar or loot switch."""
    calendar, loot_talk = app_calendar(app), app_loot(app)
    if not calendar and not loot_talk:
        return None
    live = CultureLive(app, calendar=calendar, loot_talk=loot_talk)
    if calendar:
        app.register_event_handler("hello", live.on_hello)
        app.register_event_handler("game_event", live.on_game_event)
        app.engine.culture_calendar = True  # the awareness card lists the server's events
    app.register_background(live.run)
    app.register_shutdown_drain(live.close)
    return live
