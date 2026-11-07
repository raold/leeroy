import asyncio
import json
import logging
from dataclasses import replace

from conftest import run
from leeroy_mind.claims import stage
from leeroy_mind.claims.extract import MAX_CLAIMS, Line, screen
from leeroy_mind.claims.store import cursor, live_claims
from leeroy_mind.llm.client import LLMError
from leeroy_mind.nightly.budget import _counts, plan_night
from leeroy_mind.nightly.store import step_done

from .conftest import raw, say
from .test_store import put


def test_own_lines_only_once_and_cursor_resumes(context, store, clock):
    first = say(store, 101, 'I love Saurfang')
    say(store, 1, 'I hate Saurfang', origin='human')
    say(store, 102, 'Ashlen hates Saurfang')
    say(store, 101, 'I hate Saurfang', origin='game')

    async def go():
        async with context(lambda *_: json.dumps({'claims': [raw(source_line=first)]})) as (ctx, llm):
            # Use 101 only here; 102 is legitimately another unit, never evidence for 101.
            ctx.settings = replace(ctx.settings, claims_per_night=1)
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == first
            second = say(store, 101, 'I enjoy Saurfang')
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == first
            calls = [c for c in llm.calls if 'messages' in c]
            assert len(calls) == 1 and 'I hate Saurfang' not in calls[0]['messages'][1]['content']
            assert live_claims(store.conn, 101)[0].seen == 1
        async with context(lambda *_: json.dumps({'claims': [raw(source_line=second)]}),
                           night='2026-10-30') as (ctx, llm):
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == second
            assert live_claims(store.conn, 101)[0].seen == 2
    run(go())


def test_cap_survives_rerun_and_backlog_is_not_lost(context, store):
    ids = [say(store, 101, 'I love Saurfang') for _ in range(43)]
    say(store, 102, 'I enjoy raids')
    say(store, 103, 'I enjoy tea')

    async def go():
        async with context(claims_per_night=1) as (ctx, llm):
            await stage.stage_claims(ctx)
            await stage.stage_claims(ctx)
            assert len([c for c in llm.calls if 'messages' in c]) == 1
            assert cursor(store.conn, 101) == ids[39]
            assert cursor(store.conn, 102) == 0
            assert len(json.loads(llm.calls[0]['messages'][1]['content'].split('\n\n')[0])['lines']) == 40
        async with context(night='2026-10-30', claims_per_night=1) as (ctx, _):
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == ids[-1]
            assert cursor(store.conn, 102) == 0
    run(go())


def test_cancelled_call_keeps_cursor_and_retries(context, store):
    first = say(store, 101, 'I love Saurfang')

    async def go():
        async with context() as (ctx, llm):
            gate = asyncio.Event()
            llm._gate = gate
            task = asyncio.create_task(stage.stage_claims(ctx))
            while not llm.calls:
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert cursor(store.conn, 101) == 0
            assert not step_done(store, ctx.night, 'claims', '101')
            gate.set()
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == first
    run(go())


def test_rollback_keeps_claim_cursor_and_receipt_together(context, store, monkeypatch):
    first = say(store, 101, 'I love Saurfang')
    real = stage.advance

    def fail(*args):
        raise RuntimeError('private payload must not be logged')

    async def go():
        async with context(lambda *_: json.dumps({'claims': [raw(source_line=first)]})) as (ctx, _):
            monkeypatch.setattr(stage, 'advance', fail)
            await stage.stage_claims(ctx)
            assert live_claims(store.conn, 101) == [] and cursor(store.conn, 101) == 0
            assert not ctx.done('claims', '101')
            monkeypatch.setattr(stage, 'advance', real)
            await stage.stage_claims(ctx)
            assert len(live_claims(store.conn, 101)) == 1 and cursor(store.conn, 101) == first
    run(go())


def test_stage_expires_plans_even_without_new_lines(context, store, clock):
    put(store, kind='plan', expires=clock.now() + 1)
    run(clock.advance(2, step=2))

    async def go():
        async with context() as (ctx, llm):
            await stage.stage_claims(ctx)
            assert llm.calls == []
            assert store.conn.execute('SELECT count(*) FROM member_claim_vec').fetchone()[0] == 0
    run(go())


def test_budget_counts_only_admitted_stage_and_degrades(context, store):
    say(store, 101, 'I love Saurfang')

    async def go():
        async with context(budget_floor_hours=0) as (ctx, _):
            assert 'claims' not in _counts(ctx, set(), frozenset())
            assert _counts(ctx, set(), frozenset({'claims'}))['claims'] == 1
            await ctx.clock.advance(172800, step=172800)
            plan = await plan_night(ctx, stages=frozenset({'claims'}))
            assert ctx.settings.claims_per_night == 0 and 'claims' in plan['steps']
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == 0
    run(go())


def test_screens_and_cap(context):
    async def go():
        async with context() as (ctx, _):
            line = Line(7, 1, 'I love Saurfang and I like Brelva')
            values = [raw(text='loves Avery Example'), raw(subject='Avery Example'),
                      raw(text='loves avery example'),
                      raw(subject=''), raw(text='x' * 201), raw(text='Brelva loves Saurfang'),
                      raw(text='I love Saurfang'), raw(source_line=99),
                      raw(text='as an AI loves Saurfang'), raw(text='loves space travel'),
                      raw(text='likes Brelva', subject='brelva')]
            kept = screen({'claims': values}, [line], ctx.ident, ['Brelva'])
            assert [c.text for c in kept] == ['likes Brelva']
            values = [raw(text=f'loves Saurfang on day {n}') for n in range(MAX_CLAIMS + 3)]
            assert len(screen({'claims': values}, [line], ctx.ident)) == MAX_CLAIMS
            assert not screen({'claims': [raw()]}, [Line(7, 1, 'Brelva said "I love Saurfang"')], ctx.ident)
    run(go())


def test_logs_never_carry_text_or_backend_errors(context, store, caplog):
    first = say(store, 101, 'I love Saurfang PRIVATE_SENTINEL')

    async def go():
        async with context(lambda *_: json.dumps({'claims': [raw(source_line=first)]})) as (ctx, llm):
            await stage.stage_claims(ctx)
        say(store, 101, 'I like tea')
        async with context(night='2026-10-30') as (ctx, llm):
            async def broken(*a, **k):
                raise LLMError('PRIVATE_SENTINEL Avery Example loves the Saurfang fight')
            llm.chat = broken
            await stage.stage_claims(ctx)
            assert ctx.failures['claims'] == 1
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        run(go())
    for private in ('PRIVATE_SENTINEL', 'Avery Example', 'Saurfang', 'I like tea'):
        assert private not in caplog.text
    assert 'claims_extracted' in caplog.text and 'claims_model_failed' in caplog.text


def test_uncited_claim_keeps_null_provenance_and_plan_deadline(context, store, clock):
    say(store, 101, 'I plan to move house')

    async def go():
        reply = raw(kind='plan', subject='moving house', text='plans to move house', polarity=0)
        async with context(lambda *_: json.dumps({'claims': [reply]})) as (ctx, _):
            await stage.stage_claims(ctx)
            [claim] = live_claims(store.conn, 101, now=clock.now())
            assert claim.source_line is None
            assert claim.expires == clock.now() + 7 * 86400
    run(go())


def test_embedding_failure_does_not_advance_or_log_text(context, store, caplog):
    first = say(store, 101, 'I love Saurfang')

    async def go():
        async with context(lambda *_: json.dumps({'claims': [raw(source_line=first)]})) as (ctx, llm):
            async def broken(*a, **k):
                assert not store.in_unit
                raise LLMError('PRIVATE_EMBED_TEXT')
            llm.embed = broken
            await stage.stage_claims(ctx)
            assert cursor(store.conn, 101) == 0 and not ctx.done('claims', '101')
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        run(go())
    assert 'PRIVATE_EMBED_TEXT' not in caplog.text
    assert 'claims_unit_failed' in caplog.text
