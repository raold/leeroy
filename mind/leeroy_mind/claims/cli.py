"""claims status | list GUID | check GUID TEXT | eval [--fake].

Status, list and check open mode=ro and never migrate. Only check embeds its
argument (the configured small embedding model, never reflection). Eval uses a
fresh scratch copy; real reflection holds the service lock via nightly's helpers.
"""
import argparse
import asyncio
import sys
from dataclasses import asdict

import sqlite_vec

from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.llm.scheduler import LLMScheduler, Priority
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.nightly.cli import (_close_client, _context, _data_arg, _default_night,
                                     _identity, _make_client, _positive, _print, _read_store,
                                     _root, _working_store)

from .check import check
from .store import live_claims


def _parser():
    parser = argparse.ArgumentParser(prog='leeroy_mind claims')
    _data_arg(parser)
    subs = parser.add_subparsers(dest='action', required=True)
    for name in ('status', 'list', 'check', 'eval'):
        sub = subs.add_parser(name)
        _data_arg(sub, child=True)
        if name in ('list', 'check'):
            sub.add_argument('guid', type=_positive)
        if name == 'check':
            sub.add_argument('text')
        if name == 'eval':
            sub.add_argument('--fake', action='store_true', help='offline smoke replay with FakeLLM')
    return parser


def _load_vec(conn):
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _status(conn, now):
    rows = conn.execute('SELECT guid,kind,count(*) FROM member_claim WHERE superseded_by IS NULL '
                        'AND (expires IS NULL OR expires>?) GROUP BY guid,kind ORDER BY guid,kind',
                        (now,)).fetchall()
    kinds = dict.fromkeys(('fact', 'opinion', 'plan'), 0)
    members = {}
    for guid, kind, count in rows:
        kinds[kind] += count
        members.setdefault(guid, {})[kind] = count
    return dict(live=sum(kinds.values()), by_kind=kinds, by_member=members)


async def _check(conn, cfg, args, clock, make_llm):
    # This read-only check must not call check_embedding_meta: that helper writes.
    meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('embed_model','embed_dim')"))
    if meta and (meta.get('embed_model') != cfg.models.embedding or meta.get('embed_dim') != '512'):
        raise ValueError('claims_embedding_mismatch')
    llm = _make_client(cfg, make_llm)
    scheduler = LLMScheduler(llm, clock=clock)
    try:
        vec = (await Embedder(scheduler, cfg.models.embedding).embed([args.text], Priority.BACKGROUND))[0]
        return [asdict(c) for c in check(conn, args.guid, args.text, vec, now=clock.now())]
    finally:
        try:
            await scheduler.close()
        finally:
            await _close_client(llm)


async def _eval(root, cfg, clock, args, make_llm):
    from .evaluate import evaluate
    if args.fake:
        from leeroy_mind.testing.fake_llm import FakeLLM
        from .fake import reply
        make_llm = lambda cfg: FakeLLM(reply, models={cfg.models.reflection, cfg.models.embedding})
    with _working_store(root, clock, live=False, label='claims-eval') as (store, path):
        async with _context(root, store, cfg, clock, _default_night(clock), make_llm=make_llm) as ctx:
            await ctx.reasoner.preflight()
            await ctx.arbiter.ensure_model(cfg.models.reflection)
            report = await evaluate(ctx)
            report.update(fake=args.fake, scratch=str(path))
            _print(report)
    return 0


def claims_main(argv=None, *, make_llm=None):
    args = _parser().parse_args(argv)
    clock = SystemClock()
    try:
        root = _root(args)
        cfg = load_config(root / 'mind.toml')
        if args.action == 'eval':
            return asyncio.run(_eval(root, cfg, clock, args, make_llm))
        with _read_store(root / 'mind.db', clock) as store:
            # Even a future reader that selects vec0 must load it on its own connection.
            _load_vec(store.conn)
            if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='member_claim'").fetchone():
                _print({'available': False, 'reason': 'claims_schema_missing'})
                return 1
            if args.action == 'status':
                _print(_status(store.conn, clock.now()))
            else:
                ident = _identity(cfg, store)
                if args.action == 'list':
                    _print([{k: v for k, v in asdict(c).items() if k != 'embedding'}
                            for c in live_claims(store.conn, args.guid, now=clock.now())], ident)
                else:
                    _print(asyncio.run(_check(store.conn, cfg, args, clock, make_llm)), ident)
        return 0
    except KeyboardInterrupt:
        print('claims interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'claims failed: {type(exc).__name__}', file=sys.stderr)
        return 1
