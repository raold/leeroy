import logging

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.decisions import config as dconfig
from leeroy_mind.decisions.config import DecisionsConfig, load_decisions_config


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(dconfig, '_warned', set())


def write(tmp_path, text):
    path = tmp_path / 'mind.toml'
    path.write_text(text, encoding='utf-8')
    return path


def test_config_needs_server(tmp_path, caplog):
    path = write(tmp_path, '[decisions]\nenabled = true\nloot_poll_days = 7\n')
    with caplog.at_level(logging.WARNING):
        cfg = load_decisions_config(path)
    assert cfg == DecisionsConfig(enabled=False, loot_poll_days=7)
    assert 'decisions_need_server' in caplog.text
    path = write(tmp_path, '[server]\nenabled = true\n[decisions]\nenabled = true\n')
    assert load_decisions_config(path) == DecisionsConfig(enabled=True)


def test_defaults_and_ranges(tmp_path, caplog):
    assert load_decisions_config(tmp_path / 'missing.toml') == DecisionsConfig()
    assert DecisionsConfig() == DecisionsConfig(False, 10, 14, 20)
    for bad, key in (('enabled = "yes"', 'enabled'), ('bank_items_per_day = 51', 'bank_items_per_day'),
                     ('bank_restock_per_week = -1', 'bank_restock_per_week'), ('loot_poll_days = 2', 'loot_poll_days'),
                     ('loot_poll_days = 14.0', 'loot_poll_days'), ('surprise = 1', 'unknown')):
        on = '' if key == 'enabled' else 'enabled = true\n'
        path = write(tmp_path, f'[server]\nenabled = true\n[decisions]\n{on}{bad}\n')
        with caplog.at_level(logging.WARNING):
            assert load_decisions_config(path) == DecisionsConfig()
        assert f'decisions_config_invalid key={key}' in caplog.text
    path = write(tmp_path, '[server]\nenabled = true\n[decisions]\nenabled = true\nbank_restock_per_week = 0\n')
    assert load_decisions_config(path).bank_restock_per_week == 0


def test_the_main_config_leaves_the_table_to_decisions(tmp_path, caplog):
    path = write(tmp_path, '[decisions]\nenabled = false\n')
    with caplog.at_level(logging.WARNING):
        load_config(path)
    assert 'decisions' not in caplog.text
