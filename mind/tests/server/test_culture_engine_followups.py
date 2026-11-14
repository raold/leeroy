"""Culture delivery uses real SQLite transactions and the normal Discord pipeline."""
import random
from types import SimpleNamespace

import pytest

from conftest import run
from server.test_pipeline import rig, turn, meta
from server.test_engine import app, finished
from server.test_rhythm import fixture, draw
from culture.test_culture_seeds import joke_app
from leeroy_mind.server.engine import Conversation, ServerEngine
from leeroy_mind.server.seeds import Seed


def test_joke_cast_filters_before_drawing_opener_and_reply_cast():
    people, states = fixture()
    seed = Seed('joke:1', 'joke', ('general',), 'Invented incident', (101, 102))
    for n in range(100):
        cast = draw(people, states, seed=seed, rng=random.Random(n),
                    tease=lambda a, b: a == 103)
        assert cast == [103, 101]


def test_validity_rebuilds_only_its_own_joke(app, monkeypatch):
    seed = joke_app(app, monkeypatch)
    calls = []
    def current(store, **kwargs):
        calls.append((store, kwargs))
        return [seed]
    monkeypatch.setattr('leeroy_mind.culture.seeds._culture_seeds', current)
    c = Conversation('one', 'general', seed, [101, 102], app.clock.now(), 2)
    assert app.server.engine._joke_valid(c, 101)
    assert calls[0][0] is app.store
    assert calls[0][1]['only'] == 1
    assert calls[0][1]['ident'] is app.engine.human


def test_owner_joke_instruction_uses_his_called_name(app, monkeypatch):
    seed = joke_app(app, monkeypatch)
    from dataclasses import replace
    seed = replace(seed, about=(app.human_guid, 101))
    app.observed.seeds = [seed]
    monkeypatch.setattr('leeroy_mind.culture.seeds._culture_seeds', lambda *a, **kw: [seed])
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert app.turns
    assert f'Tease {app.engine.human.called} about it' in app.turns[0][0].instruction


class Crash(BaseException):
    """Abrupt unwind before commit, without the pipeline's normal error handler."""


@pytest.mark.parametrize('fault', ['none', 'before', 'after', 'crash', 'after_commit'])
def test_joke_post_and_receipts_commit_or_roll_back_together(rig, monkeypatch, fault):
    s, e, now = rig.store, rig.engine, rig.clock.now()
    s.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used) "
                   "VALUES(1,'joke','kettle inspector','chat:1',101,0,0)")
    se = object.__new__(ServerEngine)
    se.app = SimpleNamespace(store=s, human_guid=1)
    seed = Seed('joke:1', 'joke', ('general',), 'Recorded incident', (1, 101))
    c = Conversation('atomic', 'general', seed, [101, 102], now, 3)
    m = meta(server_conversation=c.id, words='invented line')
    m.turn_id = 'atomic-turn'
    def receipt(line, message, at):
        assert m.said == 0 and not c.teased
        assert s.conn.execute('SELECT id FROM discord_message WHERE id=?', (message,)).fetchone()
        assert s.conn.execute('SELECT id FROM chat_line WHERE id=?', (line,)).fetchone()
        if fault == 'before':
            raise RuntimeError('synthetic before receipt')
        se._record_joke(c, line, message, at)
        if fault == 'after':
            raise RuntimeError('synthetic after receipt')
        if fault == 'crash':
            raise Crash()
    m.on_discord_record = receipt
    if fault == 'after_commit':
        def fail(*a, **kw):
            raise RuntimeError('synthetic memory-only hook')
        e.signals = SimpleNamespace(sent=fail)
    if fault == 'crash':
        with pytest.raises(Crash):
            e._deliver_discord(turn(), m, e.personas.get(101), 'invented line', [])
    else:
        result = e._deliver_discord(turn(), m, e.personas.get(101), 'invented line', [])
        assert result == (None if fault == 'none' else 'record_error')
    committed = fault in {'none', 'after_commit'}
    se._settle_joke(c, m)
    assert c.teased is committed
    assert s.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == int(committed)
    assert s.conn.execute('SELECT count(*) FROM chat_line').fetchone()[0] == int(committed)
    assert s.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0] == int(committed)
    assert bool(s.get_meta('culture:server-joke:atomic')) is committed
    assert bool(s.get_meta('server:seed:joke:1')) is committed
    assert bool(s.get_meta('banter:owner-evening')) is committed
    assert m.said == int(committed)
    if not committed:
        assert not e._words and not e._turn_lines and not rig.reactions
        assert not s.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'culture:%' OR key LIKE 'discord:line:%'").fetchone()
    else:
        # Restart/reconciliation cannot spend it twice.
        c.teased = False
        se._settle_joke(c, m)
        assert c.teased
        assert s.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0] == 1
