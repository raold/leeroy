"""`eval talk`: no source writes, no live-db switch, no text in terminal output."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

from leeroy_mind.config import load_config
from leeroy_mind.nightly.cli import (_child, _context, _data_arg, _default_night, _positive,
                                     _root, _working_store)

from . import contradiction, nonsequitur, repetition, unawareness
from .common import mark_scratch, read_lines
from .report import build, compare, print_summary

EMBED_BATCH = 64
EMBED_MAX_CHARS = 4000
EXCERPT_CHARS = 160


def add_parser(parser):
    sub = parser.add_subparsers(dest='eval_action').add_parser('talk', help='measure stored talk on a scratch copy')
    _data_arg(sub, child=True)
    sub.add_argument('--since', type=_positive, default=14, metavar='DAYS')
    sub.add_argument('--fake', action='store_true', help='offline smoke measurement, not a calibrated model')
    sub.add_argument('--judge', action='store_true', help='confirm contradiction candidates with the reflection model')
    sub.add_argument('--json', nargs='?', const='', metavar='NAME.json', help='write a report inside data-dir/scratch')
    sub.add_argument('--baseline', type=Path, metavar='FILE')
    sub.add_argument('--show-text', action='store_true', help='include short excerpts only in the JSON scratch report')


class _CodesOnly(logging.Filter):
    def filter(self, record):
        # Dependency diagnostics may quote config keys, model responses or paths.
        record.msg, record.args = 'talk_eval_dependency_event', ()
        record.exc_info = record.exc_text = record.stack_info = None
        record.leeroy_event, record.leeroy_fields = 'talk_eval_dependency_event', {}
        return True


@contextmanager
def private_logs():
    logger = logging.getLogger('leeroy_mind')
    targets = {logger, *logger.handlers, *logging.getLogger().handlers}
    if logging.lastResort:
        targets.add(logging.lastResort)
    redactor = _CodesOnly()
    for target in targets:
        target.addFilter(redactor)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(redactor)


def output_path(root, name):
    name = name or 'talk-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.json', name):
        raise ValueError('report_name_invalid')
    scratch = _child(root, 'scratch')
    scratch.mkdir(exist_ok=True)
    target = _child(scratch, name)
    if target.exists():
        raise ValueError('report_already_exists')
    return target


def write_report(target, report):
    temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as handle:
            json.dump(report, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        # Publish the finished inode without replacing a competing report. A preceding
        # exists() check cannot make POSIX rename no-clobber. Both paths share a directory;
        # if that filesystem cannot hardlink, fail without an unsafe rename fallback.
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise ValueError('report_already_exists') from None
    finally:
        temporary.unlink(missing_ok=True)


async def evaluate(root, cfg, clock, args, make_llm=None):
    if args.fake:
        from leeroy_mind.claims.fake import reply
        from leeroy_mind.testing.fake_llm import FakeLLM
        make_llm = lambda cfg: FakeLLM(reply, models={cfg.models.reflection, cfg.models.embedding})
    until = clock.now()
    since = until - args.since * 86400
    label = 'talk-eval-' + uuid.uuid4().hex[:12]
    with _working_store(root, clock, live=False, label=label) as (store, _path):
        mark_scratch(store)
        async with _context(root, store, cfg, clock, _default_night(clock), make_llm=make_llm) as ctx:
            await ctx.reasoner.preflight()
            await ctx.arbiter.ensure_model(cfg.models.reflection)
            lines, context = read_lines(store.conn, since, until, ctx.human)
            usable = [line for line in context if len(line.text) <= EMBED_MAX_CHARS]
            vectors = {}
            for start in range(0, len(usable), EMBED_BATCH):
                batch = usable[start:start + EMBED_BATCH]
                embeddings = await ctx.embed([line.text for line in batch])
                vectors.update((line.id, vec) for line, vec in zip(batch, embeddings, strict=True))
            results = dict(
                contradiction=await contradiction.detect(ctx, lines, vectors, use_judge=args.judge),
                repetition=repetition.detect(store, lines, vectors),
                nonsequitur=nonsequitur.detect(lines, context, vectors),
                unawareness=unawareness.detect(store.conn, lines))
            fingerprint = hashlib.sha256(('fake-wordhash-512-v1' if args.fake else cfg.models.embedding).encode()).hexdigest()
            report = build(lines, results, since=since, until=until, days=args.since, fake=args.fake,
                           judged=args.judge, embedding_fingerprint=fingerprint)
            report['embedding_coverage'] = dict(context_lines=len(context), embedded_lines=len(vectors),
                                                 skipped_long_lines=len(context) - len(usable), max_chars=EMBED_MAX_CHARS)
            report['parameters'] = dict(
                repetition={key: getattr(repetition, key) for key in (
                    'NGRAM_SIZE', 'MIN_WORDS', 'NGRAM_OVERLAP', 'EMBEDDING_COSINE', 'NEIGHBOURS',
                    'GUILD_PHRASE_WORDS', 'GUILD_MEMBERS', 'STOCK_WORDS', 'STOCK_COUNT', 'STOCK_SHARE')},
                stop_phrases=['_'.join(phrase) for phrase in repetition.STOP_PHRASES],
                nonsequitur={key: getattr(nonsequitur, key) for key in (
                    'PARENT_COSINE', 'THREAD_COSINE', 'CONTEXT_SECONDS', 'THREAD_LINES', 'MIN_REPLY_WORDS')},
                fillers=sorted(nonsequitur.FILLERS), position_max_age_seconds=unawareness.POSITION_MAX_AGE_SECONDS,
                claim_batch_lines=contradiction.MAX_LINES, claim_max_chars=contradiction.LINE_CHARS,
                embed_max_chars=EMBED_MAX_CHARS)
            if args.show_text:
                texts = {line.id: line.text[:EXCERPT_CHARS] for line in lines}
                for detector in report['detectors'].values():
                    for hit in detector['hits']:
                        hit['excerpt'] = texts[hit['line_id']]
            return report


def run(args, clock, *, make_llm=None):
    try:
        if args.fake and args.judge:
            raise ValueError('judge_requires_real_model')
        if args.show_text and args.json is None:
            raise ValueError('show_text_requires_json')
        with private_logs():
            root = _root(args)
            target = output_path(root, args.json) if args.json is not None else None
            baseline = json.loads(args.baseline.read_text(encoding='utf-8')) if args.baseline else None
            cfg = load_config(root / 'mind.toml')
            report = asyncio.run(evaluate(root, cfg, clock, args, make_llm))
            if baseline is not None:
                report['baseline_changes'] = compare(report, baseline)
            if target is not None:
                write_report(target, report)
        print_summary(report)
        if target is not None:
            print('talk_eval report_written=1')
        return 0
    except KeyboardInterrupt:
        print('talk_eval_interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        code = type(exc).__name__
        if isinstance(exc, ValueError) and re.fullmatch(r'baseline_[a-z_]+', str(exc)):
            code += ':' + str(exc)
        print(f'talk_eval_failed code={code}', file=sys.stderr)
        return 1
