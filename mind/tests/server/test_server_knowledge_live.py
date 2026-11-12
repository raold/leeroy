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
