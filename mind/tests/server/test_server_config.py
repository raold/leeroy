from datetime import time

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.server.config import ServerConfig, load_server_config


def test_missing_table_is_off(tmp_path):
    path = tmp_path / 'mind.toml'
    assert load_server_config(path) == ServerConfig()
    path.write_text('[talk]\nchattiness=4\n')
    assert load_server_config(path) == ServerConfig()


def test_valid_table(tmp_path):
    path = tmp_path / 'mind.toml'
    path.write_text('[server]\nenabled=true\nquiet_window=["02:30","07:00"]\n'
                    'conversations_per_hour=4\nin_game_share=0.2\ndms_per_day=0\nreactions=false\n')
    load_config(path)  # the main loader delegates this table
    assert load_server_config(path) == ServerConfig(True, (time(2, 30), time(7)), 4., .2, 0, False)


@pytest.mark.parametrize('bad,key', [
    ('quiet_window=["03:00","03:00"]', 'quiet_window'),
    ('quiet_window=["3:00","08:00"]', 'quiet_window'),
    ('quiet_window=["24:00","08:00"]', 'quiet_window'),
    ('quiet_window=[3,8]', 'quiet_window'),
    ('quiet_window="03:00"', 'quiet_window'),
    ('conversations_per_hour=13', 'conversations_per_hour'),
    ('conversations_per_hour=nan', 'conversations_per_hour'),
    ('conversations_per_hour=true', 'conversations_per_hour'),
    ('in_game_share=1.5', 'in_game_share'),
    ('dms_per_day=-1', 'dms_per_day'), ('dms_per_day=3.0', 'dms_per_day'),
    ('reactions=1', 'reactions'), ('private_value="secret text"', 'unknown'),
])
def test_any_bad_key_turns_it_off(tmp_path, bad, key, caplog):
    path = tmp_path / 'mind.toml'
    path.write_text('[server]\nenabled=true\n' + bad)
    assert load_server_config(path) == ServerConfig()
    assert caplog.text.count('server_config_invalid') == 1
    assert f'key={key}' in caplog.text
    assert 'secret text' not in caplog.text
    load_server_config(path)
    assert caplog.text.count('server_config_invalid') == 1


@pytest.mark.parametrize('text', ['server=1', '[server]\nenabled=1', '[server]\nenabled='])
def test_bad_table_and_unreadable_file_are_off(tmp_path, text, caplog):
    path = tmp_path / 'mind.toml'
    path.write_text(text)
    assert load_server_config(path) == ServerConfig()
    assert caplog.text.count('server_config_invalid') == 1
