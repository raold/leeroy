"""With [decisions] off (false, invalid, or on without [server]) nothing attaches, the talk engine's
card has no Decisions part and the golden prompts are byte-identical. Absent, it is on (2026-11-25)."""
import hashlib
from types import SimpleNamespace

import pytest

from tuning.scenario import GOLDEN, engine_clock, golden, record
from leeroy_mind.decisions import ledger, polls
from leeroy_mind.decisions.attach import attach_decisions
from leeroy_mind.decisions.service import DecisionsService
from shipped_off import off as shipped_off



@pytest.fixture
def clock():
    """The golden scenario's own clock: each step settles by yielding, never by real time."""
    return engine_clock()


def app(tmp_path, *, server=True):
    registered, handlers, hooks = [], [], []
    return SimpleNamespace(data_dir=tmp_path, server=object() if server else None,
                           engine=SimpleNamespace(), register_background=registered.append, registered=registered,
                           register_event_handler=lambda name, handler: handlers.append((name, handler)),
                           handlers=handlers, register_connection_hook=hooks.append, hooks=hooks)


@pytest.mark.parametrize('config', ['[decisions]\nenabled = false\n',
                                    '[server]\nenabled = false\n[decisions]\nenabled = true\n',
                                    '[server]\nenabled = true\n[decisions]\nenabled = true\nloot_poll_days = 99\n'])
def test_off_preserves_golden_prompts_and_golden_file(store, clock, tmp_path, config):
    (tmp_path / 'mind.toml').write_text(shipped_off(config), encoding='utf-8')
    off = app(tmp_path)
    assert attach_decisions(off) is None
    assert off.registered == [] and off.handlers == [] and off.hooks == []
    assert not hasattr(off.engine, 'decisions_enabled')
    # Stored decisions and an open poll change nothing while the switch is off
    now = clock.now()
    ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'dkp'},
                   positions=[{'guid': 102, 'side': 'dkp', 'why': 'high_dkp'}], now=now)
    polls.open_poll(store, channel='officer-chat', question='Loot rules?', options=('dkp', 'loot_council'),
                    voters=(102, 104), closes=now + 3600, now=now, kind='loot_rule', subject='loot')
    before = hashlib.sha256(GOLDEN.read_bytes()).digest()
    assert record(store, clock, tmp_path) == golden()
    assert hashlib.sha256(GOLDEN.read_bytes()).digest() == before


def test_absent_it_is_on_with_every_kind(tmp_path):
    on = app(tmp_path)
    service = attach_decisions(on)
    assert isinstance(service, DecisionsService) and on.engine.decisions_enabled is True
    assert all(service.kind_on(kind) for kind in ('rank', 'strategy', 'bank'))
    assert [name for name, _ in on.handlers] == ['hello', 'cast']  # rank reviews observe the cast


def test_on_needs_the_server_running_and_then_attaches_one_task(tmp_path):
    (tmp_path / 'mind.toml').write_text('[server]\nenabled = true\n[decisions]\nenabled = true\n'
                                        'rank_reviews = false\n', encoding='utf-8')
    assert attach_decisions(app(tmp_path, server=False)) is None
    on = app(tmp_path)
    service = attach_decisions(on)
    assert isinstance(service, DecisionsService) and on.engine.decisions_enabled is True
    assert on.registered == [service.run]
    # Part B's kinds hold state in game (the picker's roster): sent again on hello, dropped on disconnect
    assert on.handlers[0] == ('hello', service.on_hello)
    # Rank reviews (off here) are the only kind that observes the cast
    assert [name for name, _ in on.handlers] == ['hello']
    assert on.hooks == [service.on_connection]
