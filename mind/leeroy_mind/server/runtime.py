"""Capture runtime inputs on the loop and read conversation evidence in a bounded worker."""
import asyncio
from contextlib import closing
from dataclasses import fields
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from leeroy_mind.console.feed import connect
from leeroy_mind.storage.rows import Relationship
from .state import StateInputs, live_voice_session, pulled_into_play, state_of


async def snapshot(app, now, *, include_seeds=False):
    cast = dict(app.world.discord_members())
    human, local = app.human_guid, app.clock.local()
    personas = {g: p for g in cast if g != human and (p := app.personas.get(g)) is not None}
    officers = frozenset(g for g in cast if app.world.is_officer(g))
    playing, online = pulled_into_play(app), frozenset(app.world.online)
    comms = app.engine.comms
    server, voice_session = getattr(app, 'server', None) is not None, live_voice_session(app)
    observed = {g: StateInputs(p, cast, officers, online, playing, human, local.tzinfo,
                 comms.room_of(g) if comms is not None else None, server, voice_session) for g, p in personas.items()}
    trial_rank = getattr(getattr(getattr(app, 'realm', None), 'cfg', None), 'trial_rank', 4)
    path, library = Path(app.data_dir) / 'mind.db', Path(app.data_dir) / 'library.db'
    photos_on = getattr(app, 'photos', None) is not None
    photo_dial = app.cfg.talk.selfies if photos_on else 0
    groups_config = getattr(getattr(app,'groups',None),'config',None)
    def read():
        deadline = time.monotonic() + 2
        with closing(connect(path)) as conn:
            conn.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
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
            rows = {(r['a'], r['b']): dict(r) for r in conn.execute('SELECT * FROM relationship')}
            ties = {}
            for a in personas:
                for b in (*personas, human):
                    row = rows.get((a, b), {})
                    score = row.get('affinity', 0)
                    ties[a, b] = ('feud' if frozenset((a, b)) in feud else
                        'crush' if row.get('romance_stage', 'none') not in ('none', 'ex') else
                        'friend' if score >= 60 else 'fond' if score >= 30 else 'rival' if score <= -30 else 'neutral')
            relationships = {g: Relationship(**{k:v for k,v in rows.get((g,human), {'a':g,'b':human}).items()
                             if k in {f.name for f in fields(Relationship)} and k not in ('notes', 'pinned', 'label')}) for g in personas}
            seeds, source = [], []
            if include_seeds:
                from .seeds import gather, fresh
                # Only the roster's character name; never his private real-name setting.
                human_name = getattr(cast.get(human), 'name', '')
                source = gather(conn, now=now, local=local, cast=personas, officers=officers,
                                human=human, human_name=human_name, trial_rank=trial_rank)
                seeds = fresh(conn, source, now=now)
                if photos_on:
                    from leeroy_mind.photos.send import photo_seeds
                    seeds += fresh(conn, photo_seeds(conn, now=now, local=local, dial=photo_dial), now=now)
            meta = dict(conn.execute("SELECT key,value FROM meta WHERE key LIKE 'server:%'"))
            group_audiences = {}
            if include_seeds and groups_config is not None and groups_config.enabled:
                from leeroy_mind.groups.conversations import seeds as group_seeds
                extra,group_audiences = group_seeds(conn,source,now=now,local=local,human=human,config=groups_config)
                # Each group owns its subject cooldown. An introduction stays pending
                # until delivery, so a dropped attempt may retry within the daily cap.
                seeds += fresh(conn,(s for s in extra if s.kind != 'group_start'),now=now)
                seeds += [s for s in extra if s.kind == 'group_start']
            return SimpleNamespace(personas=personas, states=states, ties=ties, in_play=playing, officers=officers,
                away=away, classes={g:m.class_ for g,m in cast.items()}, cast=cast, human=human, local=local,
                seeds=seeds, relationships=relationships, meta=meta,group_audiences=group_audiences)
    return await asyncio.to_thread(read)
