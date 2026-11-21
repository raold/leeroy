"""Capture runtime inputs on the loop and read conversation evidence in a bounded worker.

The read runs on its own read-only connection, in a WAL snapshot, under a CPU budget. A read
that meets another connection (`busy`, `locked`) or its budget (`interrupted`) is asked again,
with a larger budget, before the caller sees a failure: a server tick, a reaction or a
conversation turn is never dropped for one unlucky read."""
import asyncio
import logging
from contextlib import closing
from dataclasses import fields
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from leeroy_mind.console.feed import connect
from leeroy_mind.storage.db import TRANSIENT_CODES, error_code
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from .state import StateInputs, live_voice_session, pulled_into_play, state_of

log = logging.getLogger('leeroy_mind')
# The worker's own CPU seconds for one read, per attempt: a warm read of 145 members takes about
# 0.3 s, the first after a start more (cold pages), so a retry gets a larger share.
READ_CPU_S = (2.0, 4.0, 8.0)
RETRY_WAIT_S = (0.25, 1.0)
BUSY_MS = 5000  # as the mind's own connection waits (storage/db.py)
# the ties need two columns of every pair; only his rows are read whole
_TIE_SQL = 'SELECT a,b,affinity,romance_stage FROM relationship'
_HIS_FIELDS = tuple(f.name for f in fields(Relationship) if f.name not in ('notes', 'pinned', 'label'))
_HIS_SQL = 'SELECT ' + ','.join(_HIS_FIELDS) + ' FROM relationship WHERE b=?'


async def snapshot(app, now, *, include_seeds=False):
    cast = dict(app.world.discord_members())
    human, local = app.human_guid, app.clock.local()
    personas = {g: p for g in cast if g != human and (p := app.personas.get(g)) is not None}
    officers = frozenset(g for g in cast if app.world.is_officer(g))
    playing, online = pulled_into_play(app), frozenset(app.world.online)
    comms = app.engine.comms
    server, voice_session = getattr(app, 'server', None) is not None, live_voice_session(app)
    from .state import heroic_players
    heroic = heroic_players(app)
    observed = {g: StateInputs(p, cast, officers, online, playing, human, local.tzinfo,
                 comms.room_of(g) if comms is not None else None, server, voice_session, g in heroic) for g, p in personas.items()}
    trial_rank = getattr(getattr(getattr(app, 'realm', None), 'cfg', None), 'trial_rank', 4)
    path, library = Path(app.data_dir) / 'mind.db', Path(app.data_dir) / 'library.db'
    from leeroy_mind.culture.config import app_calendar, app_enabled, app_loot, app_memes
    culture_on = app_enabled(app)
    calendar_on, loot_on = app_calendar(app), app_loot(app)
    memes_on = app_memes(app)
    identity = app.engine.human if culture_on else None
    photos_on = getattr(app, 'photos', None) is not None
    photo_dial = app.cfg.talk.selfies if photos_on else 0
    groups_config = getattr(getattr(app,'groups',None),'config',None)
    decisions_on = getattr(app, 'decisions', None) is not None
    decisions_kinds = getattr(app.decisions, 'kinds_on', None) if decisions_on else None
    day_plans = getattr(getattr(getattr(app, 'cfg', None), 'life', None), 'day_plans', False)
    dungeons = getattr(getattr(getattr(app, 'cfg', None), 'life', None), 'dungeons', False)
    intents_on = getattr(app, 'godmode', None) is not None
    # Imported here, on the loop: a first import inside the worker spent its CPU budget on
    # compiling modules (about 1 s of the 2 s at the first read after a start).
    from leeroy_mind.tuning.edits import apply_feelings, feeling_overlay
    from . import seats  # noqa: F401  (state_of reads the seats lazily)
    from .topics import posted_today
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if include_seeds:
        from .seeds import gather, fresh
        if photos_on:
            from leeroy_mind.photos.send import photo_seeds
        if groups_config is not None and groups_config.enabled:
            from leeroy_mind.groups.conversations import seeds as group_seeds
    def read(budget):
        worker_store = Store(connect(path), clock=app.clock)
        with closing(worker_store.conn) as conn, worker_store.transaction():
            conn.execute(f'PRAGMA busy_timeout={BUSY_MS}')
            # the worker's own CPU seconds, not the wall clock: a loaded machine that does not schedule
            # the thread for 2 s is not a runaway query, and interrupting it dropped a whole server tick.
            # Counted from here, so opening the connection is not part of it.
            deadline = time.thread_time() + budget
            conn.set_progress_handler(lambda: time.thread_time() >= deadline, 1000)
            if library.is_file():
                try:
                    conn.execute('ATTACH DATABASE ? AS library', (library.resolve().as_uri() + '?mode=ro',))
                except sqlite3.Error:
                    pass
            conn.execute('BEGIN')
            states = {g: state_of(None, g, now, conn=conn, observed=o) for g, o in observed.items()}
            away = frozenset(r[0] for r in conn.execute('SELECT guid FROM away WHERE returned_at IS NULL '
                              'AND (returns_at IS NULL OR returns_at>?)', (now,)))
            feud = {frozenset((r[0], r[1])) for r in conn.execute('SELECT a,b FROM feud WHERE ended IS NULL')}
            rows = {(r[0], r[1]): {'a': r[0], 'b': r[1], 'affinity': r[2], 'romance_stage': r[3]}
                    for r in conn.execute(_TIE_SQL)}
            rows.update(((r['a'], r['b']), dict(r)) for r in conn.execute(_HIS_SQL, (human,)))
            for pair, offsets in feeling_overlay(conn).items():  # the owner's godmode feelings
                rows[pair] = apply_feelings(rows.get(pair, {'a': pair[0], 'b': pair[1]}), offsets)
            ties = {}
            for a in personas:
                for b in (*personas, human):
                    row = rows.get((a, b), {})
                    score = row.get('affinity', 0)
                    ties[a, b] = ('feud' if frozenset((a, b)) in feud else
                        'crush' if row.get('romance_stage', 'none') not in ('none', 'ex') else
                        'friend' if score >= 60 else 'fond' if score >= 30 else 'rival' if score <= -30 else 'neutral')
            relationships = {g: Relationship(**{k:v for k,v in rows.get((g,human), {'a':g,'b':human}).items()
                             if k in _HIS_FIELDS}) for g in personas}
            seeds, source = [], []
            if include_seeds:
                # Only the roster's character name; never his private real-name setting.
                human_name = getattr(cast.get(human), 'name', '')
                source = gather(conn, now=now, local=local, cast=personas, officers=officers,
                                human=human, human_name=human_name, trial_rank=trial_rank,
                                culture_enabled=culture_on, culture_store=worker_store, ident=identity,
                                culture_calendar=calendar_on, culture_loot=loot_on,
                                decisions_enabled=decisions_on, intents_enabled=intents_on,
                                **({'decisions_kinds': decisions_kinds} if decisions_kinds is not None else {}),
                                culture_memes=memes_on, **({'day_plans': True} if day_plans else {}),
                                **({'dungeons': True} if dungeons else {}))
                seeds = fresh(conn, source, now=now)
                if photos_on:
                    seeds += fresh(conn, photo_seeds(conn, now=now, local=local, dial=photo_dial), now=now)
            meta = dict(conn.execute("SELECT key,value FROM meta WHERE key LIKE 'server:%'"))
            # each channel's member posts today ([server] channel_topics' daily room)
            posted = posted_today(conn, midnight)
            group_audiences = {}
            if include_seeds and groups_config is not None and groups_config.enabled:
                extra,group_audiences = group_seeds(conn,source,now=now,local=local,human=human,config=groups_config)
                # Each group owns its subject cooldown. An introduction stays pending
                # until delivery, so a dropped attempt may retry within the daily cap.
                seeds += fresh(conn,(s for s in extra if s.kind != 'group_start'),now=now)
                seeds += [s for s in extra if s.kind == 'group_start']
            return SimpleNamespace(personas=personas, states=states, ties=ties, in_play=playing, officers=officers,
                away=away, classes={g:m.class_ for g,m in cast.items()}, cast=cast, human=human, local=local,
                seeds=seeds, relationships=relationships, meta=meta,group_audiences=group_audiences,
                posted=posted)
    for attempt, budget in enumerate(READ_CPU_S):
        try:
            return await asyncio.to_thread(read, budget)
        except sqlite3.Error as error:
            code = error_code(error)
            if code not in TRANSIENT_CODES or attempt + 1 == len(READ_CPU_S):
                log.warning('server_read_failed code=%s attempts=%d seeds=%d', code, attempt + 1, include_seeds)
                raise
            log.info('server_read_retry code=%s attempt=%d seeds=%d', code, attempt + 1, include_seeds)
            await asyncio.sleep(RETRY_WAIT_S[min(attempt, len(RETRY_WAIT_S) - 1)])
