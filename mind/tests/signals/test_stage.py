import asyncio

from tests.claims.conftest import context
from leeroy_mind.signals import stage
from .test_signals import line, saved


def test_night_detects_repetition_and_filler_and_labels_guard_replay(context, store, clock, tmp_path):
    async def go():
        (tmp_path / 'mind.toml').write_text('[guards]\nenabled=true\n', encoding='utf-8')
        async with context() as (ctx, llm):
            ctx.window = (clock.now() - 3600, clock.now() + 1)
            line(store, clock, text='the invented wheel rolls behind the stable')
            repeat = line(store, clock, text='the invented wheel rolls behind the stable')
            tid = store.open_thread('whisper:1:101', clock.now())
            line(store, clock, 1, 'whisper', 101, 'where is the invented wheel?', tid)
            filler = line(store, clock, 101, 'whisper', 1, 'lol', tid)
            await stage.stage_signals(ctx)
            found = saved(store)
            assert (repeat, 101, 'judge_repetition', 1., 'judge') in found
            assert (repeat, 101, 'judge_guard_drop', 1., 'judge') in found
            assert (filler, 101, 'judge_nonsequitur', 1., 'judge') in found
            assert (filler, 101, 'judge_guard_react', 1., 'judge') in found
            assert not llm.calls  # lexical checks need no model or embeddings
            await stage.stage_signals(ctx)
            assert saved(store) == found
    asyncio.run(go())


def test_private_history_does_not_flag_public_and_guards_off_stay_off(context, store, clock):
    async def go():
        async with context() as (ctx, llm):
            ctx.window = (clock.now() - 1, clock.now() + 1)
            line(store, clock, 101, 'whisper', 1, 'the invented wheel rolls behind the stable')
            public = line(store, clock, text='the invented wheel rolls behind the stable')
            await stage.stage_signals(ctx)
            assert not [r for r in saved(store) if r[0] == public]
            assert not [r for r in saved(store) if r[2].startswith('judge_guard')]
    asyncio.run(go())


def test_night_cap_and_old_database(context, store, clock, monkeypatch):
    async def go():
        async with context() as (ctx, llm):
            ctx.window = (clock.now() - 1, clock.now() + 1)
            monkeypatch.setattr(stage, 'PER_NIGHT', 2)
            ids = [line(store, clock, text='the invented wheel rolls behind the stable') for _ in range(4)]
            await stage.stage_signals(ctx)
            await stage.stage_signals(ctx)
            assert ctx.done('signals', str(ids[1]))
            assert not ctx.done('signals', str(ids[2]))
            assert stage._count(ctx) == 0
            with store.transaction():
                store.conn.execute('DROP TABLE talk_signal')
            await stage.stage_signals(ctx)
    asyncio.run(go())


def test_night_budget_admits_signals_only_on_enabled_runner(context, store, clock):
    from leeroy_mind.nightly import budget
    async def go():
        async with context() as (ctx, _):
            line(store, clock)
            assert 'signals' in budget.EXTENSION_STAGES
            assert 'signals' not in budget._counts(ctx, set(), frozenset())
            assert budget._counts(ctx, set(), frozenset({'signals'}))['signals'] == 1
            assert budget.estimate({'signals': 1}, measured={}) == {'signals': 0}
    asyncio.run(go())


def test_night_shared_claim_and_world_helpers_keep_time_and_pair_fences(context, store, clock, tmp_path, monkeypatch):
    from leeroy_mind.claims.store import add
    from leeroy_mind.storage.rows import pack_vec
    from leeroy_mind.eval.talk.unawareness import Facts

    async def go():
        (tmp_path / 'mind.toml').write_text('[claims]\nenabled=true\n', encoding='utf-8')
        async with context() as (ctx, llm):
            ctx.window = (clock.now() - 1, clock.now() + 1)
            vector = pack_vec([1.] + [0.] * 511)
            async def embed(texts):
                return [vector for _ in texts]
            monkeypatch.setattr(ctx, 'embed', embed)
            source = line(store, clock, 101, 'whisper', 1, 'I love Saurfang')
            add(store, 101, kind='opinion', subject='saurfang', text='I love Saurfang', polarity=1,
                embedding=vector, source_line=source, now=clock.now()-1)
            private = line(store, clock, 101, 'whisper', 1, 'I hate Saurfang')
            public = line(store, clock, 101, text='I hate Saurfang')
            old_world = line(store, clock, text='We have never killed invented boss.')
            future_world = line(store, clock, text='We have never killed future boss.')
            monkeypatch.setattr(stage.unawareness, 'load_facts', lambda _: Facts(
                {'invented boss': [(clock.now()-1, 7)], 'future boss': [(clock.now()+1, 8)]}, {}, {}, {}))
            await stage.stage_signals(ctx)
            rows = saved(store)
            assert (private, 101, 'judge_contradiction', 1., 'judge') in rows
            assert not [r for r in rows if r[0] == public and r[2] == 'judge_contradiction']
            assert (old_world, 101, 'judge_unawareness', 1., 'judge') in rows
            assert not [r for r in rows if r[0] == future_world and r[2] == 'judge_unawareness']
    asyncio.run(go())
