from dataclasses import replace

import pytest

from conftest import run, unit
from leeroy_mind.discord import store as ds
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from selfies.fakes import mind_app


@pytest.mark.parametrize('enabled',[False,True])
def test_server_history_shelves_do_not_enter_context_and_semantics_are_gated(tmp_path,enabled):
    app = mind_app(tmp_path,toml=f'[server]\nenabled={str(enabled).lower()}\n[guards]\nenabled=true\n[talk]\naware=true')
    try:
        now = app.clock.now()
        private = ds.post(app.store,channel='dm:101',author=101,origin='mind',text='Private invented secret about the ferry',now=now-20)
        ds.post(app.store,channel='general',author=102,origin='mind',text='Public invented harbor update',now=now-10)
        turn = Turn(TurnKind.OPEN,101,'discord:general')
        meta = TurnMeta('server-test',why='server:purpose',server_anchor='Guild harbor news',discord=DiscordTarget(None,None))
        live = app.engine.live_review
        history, context = live.history(turn,meta)
        assert any(h.id == private for h in history) is enabled
        assert not any(h.id == private for h in context)
        assert 'Private invented secret' not in live.block(turn,meta,close=False)
        assert ('Here:' in live.block(turn,meta,close=False)) is enabled
        calls = []
        async def embed(texts, priority):
            calls.extend(texts)
            return [pack_vec(unit(0 if text == meta.server_anchor else 1)) for text in texts]
        app.engine.recall._embedder.embed = embed
        verdict = run(live.check(turn,meta,'A brand new subject about the harbor'))
        assert ('off_anchor' in verdict.codes) is enabled
        assert bool(calls) is enabled
        if enabled:
            assert meta.server_anchor in calls
            assert 'Private invented secret about the ferry' in calls
        # D1 answers keep the earlier history/guard rules even with server enabled.
        ordinary = replace(meta,why='reply:discord',server_anchor='')
        assert all(h.id != private for h in live.history(turn,ordinary)[0])
    finally:
        run(app.stop())


def test_game_draft_flags_a_discord_claim_with_server_on_only(store):
    from conftest import seed_test_cast
    from guards.test_review import NOW, line, draft, sources
    from leeroy_mind.claims.store import add
    from leeroy_mind.guards import review
    seed_test_cast(store)
    vec = pack_vec(unit(0))
    line(store,'I love Saurfang',channel='discord:general',audience=(1,101,102))
    add(store,101,kind='opinion',subject='saurfang',text='I love Saurfang',polarity=1,
        embedding=vec,source_line=1,now=NOW-1)
    candidate = draft('I hate Saurfang',embedding=vec)
    assert review(sources(store,claims_enabled=True),candidate).action == 'send'
    assert 'claim_conflict' in review(sources(store,claims_enabled=True,cross_medium=True),candidate).codes


def test_server_semantics_cannot_fail_open(tmp_path):
    app = mind_app(tmp_path,toml='[server]\nenabled=true\n[guards]\nenabled=true')
    try:
        async def broken(*args):
            raise RuntimeError('fake embedder unavailable')
        app.engine.recall._embedder.embed = broken
        result = run(app.engine.live_review.check(Turn(TurnKind.OPEN,101,'discord:general'),
            TurnMeta('server-test',why='server:purpose',server_anchor='Guild news'),'A new invented line'))
        assert result.action == 'drop' and result.codes == ('review_unavailable',)
    finally:
        run(app.stop())


def test_cold_server_review_embeds_only_newest_32_texts_and_reuses_cache(tmp_path):
    app = mind_app(tmp_path,toml='[server]\nenabled=true\n[guards]\nenabled=true')
    try:
        now = app.clock.now()
        for i in range(120):
            ds.post(app.store,channel='general',author=101 if i % 2 else 102,origin='mind',
                    text=f'Invented channel evidence {i}',now=now-120+i)
        calls = []
        async def embed(texts, priority):
            calls.append(texts)
            return [pack_vec(unit(0)) for _ in texts]
        app.engine.recall._embedder.embed = embed
        live = app.engine.live_review
        turn = Turn(TurnKind.OPEN,101,'discord:general')
        meta = TurnMeta('bounded',why='server:purpose',server_anchor='Guild news',discord=DiscordTarget(None,None))
        result = run(live.check(turn,meta,'The harbor reopens tomorrow'))
        assert 'review_unavailable' not in result.codes
        assert len(calls) == 1 and len(calls[0]) == 32
        assert set(calls[0]) == {'The harbor reopens tomorrow','Guild news',
                               *(f'Invented channel evidence {i}' for i in range(90,120))}
        run(live.check(turn,meta,'The harbor reopens tomorrow'))
        assert len(calls) == 1
    finally:
        run(app.stop())


def test_here_uses_speaking_channel_and_state_reads_run_off_loop(tmp_path,monkeypatch):
    import threading
    from leeroy_mind.server import state as state_module
    app = mind_app(tmp_path,toml='[server]\nenabled=true\n[talk]\naware=true')
    try:
        now = app.clock.now()
        for channel,author in [('raids',101),('raids',102),('general',103)]:
            ds.post(app.store,channel=channel,author=author,origin='mind',text='Invented update',now=now-5)
        loop_thread, calls = threading.get_ident(), []
        original = state_module.state_of
        def checked(*args,**kw):
            calls.append(threading.get_ident())
            assert calls[-1] != loop_thread
            assert kw['conn'] is not app.store.conn
            return original(*args,**kw)
        monkeypatch.setattr(state_module,'state_of',checked)
        live = app.engine.live_review
        turn = Turn(TurnKind.OPEN,101,'discord:general')
        meta = TurnMeta('here',discord=DiscordTarget(None,None))
        live.observe(turn,meta)
        assert calls == []
        block = run(live.block_async(turn,meta,close=False))
        assert len(calls) == 1
        members = app.world.discord_members()
        assert members[103].name in block and members[102].name not in block
    finally:
        run(app.stop())
