"""With [decisions] off (absent, false, invalid, or on without [server]) nothing attaches, the talk
engine's card has no Decisions part and the golden prompts are byte-identical."""
import hashlib
from types import SimpleNamespace

import pytest

from tuning.scenario import GOLDEN, golden, record
from leeroy_mind.decisions import ledger, polls
from leeroy_mind.decisions.attach import attach_decisions
from leeroy_mind.decisions.service import DecisionsService


def app(tmp_path, *, server=True):
    registered = []
    return SimpleNamespace(data_dir=tmp_path, server=object() if server else None,
                           engine=SimpleNamespace(), register_background=registered.append, registered=registered)


@pytest.mark.parametrize('config', ['[decisions]\nenabled = false\n', '[decisions]\nenabled = true\n',
                                    '[server]\nenabled = true\n[decisions]\nenabled = true\nloot_poll_days = 99\n'])
def test_off_preserves_golden_prompts_and_golden_file(store, clock, tmp_path, config):
    (tmp_path / 'mind.toml').write_text(config, encoding='utf-8')
    off = app(tmp_path)
    assert attach_decisions(off) is None
    assert off.registered == [] and not hasattr(off.engine, 'decisions_enabled')
    # Stored decisions and an open poll change nothing while the switch is off
    now = clock.now()
    ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'dkp'},
                   positions=[{'guid': 102, 'side': 'dkp', 'why': 'high_dkp'}], now=now)
    polls.open_poll(store, channel='officer-chat', question='Loot rules?', options=('dkp', 'loot_council'),
                    voters=(102, 104), closes=now + 3600, now=now, kind='loot_rule', subject='loot')
    before = hashlib.sha256(GOLDEN.read_bytes()).digest()
    assert record(store, clock, tmp_path) == golden()
    assert hashlib.sha256(GOLDEN.read_bytes()).digest() == before


def test_on_needs_the_server_running_and_then_attaches_one_task(tmp_path):
    (tmp_path / 'mind.toml').write_text('[server]\nenabled = true\n[decisions]\nenabled = true\n', encoding='utf-8')
    assert attach_decisions(app(tmp_path, server=False)) is None
    on = app(tmp_path)
    service = attach_decisions(on)
    assert isinstance(service, DecisionsService) and on.engine.decisions_enabled is True
    assert on.registered == [service.run]
