"""Bounded historical screening, not a claim about a live guard having run."""
from dataclasses import replace
import sqlite3

from leeroy_mind.eval.talk import unawareness
from leeroy_mind.guards import Draft, GuardStores, GuardsCfg, load_guards_config, review
from leeroy_mind.claims.config import load_claims_config
from leeroy_mind.config import ConfigError
from leeroy_mind.nightly.budget import register_counter
from . import store as S

PER_NIGHT = 200
REPETITION = frozenset({'own_repeat_same_channel', 'own_repeat_cross_channel', 'guild_evening_phrase',
                        'repeated_topic', 'stock_phrase_ration', 'gag_ration'})
RELEVANCE = frozenset({'filler_after_question', 'low_parent_and_thread_relevance'})
WORLD = frozenset({'never_killed_after_verified_kill', 'raid_denial_after_recorded_combat',
                   'location_mismatch'})
GAME = frozenset({'party', 'raid', 'raid_warning', 'say', 'yell', 'emote'})


def _rows(conn, ctx):
    return conn.execute('SELECT id,ts,from_guid,to_guid,thread_id,channel,text,map,zone '
        "FROM chat_line WHERE origin='mind' AND from_guid!=? AND ts>=? AND ts<? "
        'ORDER BY ts,id LIMIT ?', (ctx.human, max(ctx.window[0], ctx.window[1] - 86400),
                                 ctx.window[1], PER_NIGHT)).fetchall()


def _count(ctx):
    if not S.available(ctx.store.conn):
        return 0
    return sum(not ctx.done('signals', str(row['id'])) for row in _rows(ctx.store.conn, ctx))


register_counter('signals', _count)


async def stage_signals(ctx):
    conn = ctx.store.conn
    if not S.available(conn):
        return
    config_path = ctx.settings.data_dir / 'mind.toml'
    guards = load_guards_config(config_path)
    try:
        claims = load_claims_config(config_path).enabled
    except ConfigError:
        claims = False
    try:
        facts = unawareness.load_facts(conn)
    except sqlite3.OperationalError:
        facts = None
    rows = {str(row['id']): row for row in _rows(conn, ctx)}

    async def one(unit):
        try:
            row = rows[unit]
            channel = row['channel']
            if channel not in GAME | {'guild', 'officer', 'whisper'} or len(row['text']) > 4000:
                ctx.skip('signals', unit)
                return
            # Only reciprocal directed adjacency is evidence of a parent; plain guild
            # adjacency must not invent a conversation or a private source relationship.
            context = conn.execute('SELECT id,ts,from_guid,to_guid,text FROM chat_line '
                'WHERE thread_id=? AND channel=? AND id<? AND ts BETWEEN ? AND ? '
                'ORDER BY ts DESC,id DESC LIMIT 16', (row['thread_id'], channel, row['id'],
                    row['ts'] - 300, row['ts'])).fetchall() if row['thread_id'] else []
            parent = context[0] if context and row['to_guid'] > 0 and (
                row['to_guid'] == context[0]['from_guid'] and row['from_guid'] == context[0]['to_guid']) else None
            vectors = {}
            if claims or guards.semantic_enabled:
                selected = [row, *(context if parent else [])]
                embedded = await ctx.embed([r['text'] for r in selected])
                if len(embedded) != len(selected):
                    raise ValueError('signal_embedding_count')
                vectors = {r['id']: vector for r, vector in zip(selected, embedded, strict=True)}
            draft = Draft(row['from_guid'], 'game' if channel in GAME else channel, row['text'],
                parent_id=parent['id'] if parent else None,
                thread_ids=tuple(r['id'] for r in context) if parent else (),
                embedding=vectors.get(row['id']), to_guid=row['to_guid'],
                room=channel if channel in GAME else '', now=row['ts'], map=row['map'], zone=row['zone'],
                line_id=row['id'])
            # review is the read-only single-candidate adapter over task12's pure helpers.
            # Its history, private-audience and first_seen fences also apply to these hits.
            stores = GuardStores(conn, GuardsCfg(enabled=True, semantic_enabled=guards.semantic_enabled),
                                 claims_enabled=claims, vectors=vectors, facts=facts)
            detected = review(stores, draft)
            kinds = []
            if REPETITION.intersection(detected.codes):
                kinds.append('judge_repetition')
            if RELEVANCE.intersection(detected.codes):
                kinds.append('judge_nonsequitur')
            if 'claim_conflict' in detected.codes:
                kinds.append('judge_contradiction')
            if WORLD.intersection(detected.codes):
                kinds.append('judge_unawareness')
            if guards.enabled:
                verdict = review(replace(stores, config=guards), draft)
                if verdict.action in ('rewrite', 'drop', 'react'):
                    kinds.append('judge_guard_' + verdict.action)

            def write(store):
                try:
                    for kind in kinds:
                        S.put(store.conn, row['id'], row['from_guid'], kind, 1, ctx.clock.now(), 'judge')
                    S.prune(store.conn, ctx.clock.now())
                except Exception:
                    raise RuntimeError('signals_write_failed') from None

            ctx.commit('signals', unit, write)
        except Exception:
            # NightContext logs exception messages: never forward an adapter/model value.
            raise RuntimeError('signals_judge_failed') from None

    await ctx.map_units('signals', list(rows), one)
