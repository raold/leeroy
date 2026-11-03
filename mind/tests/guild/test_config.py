import dataclasses

import pytest

from leeroy_mind.config import ConfigError
from leeroy_mind.guild.config import DkpConfig, GuildConfig, load_guild_config


def config(tmp_path, text):
    path = tmp_path / "mind.toml"
    path.write_text(text, encoding="utf-8")
    return load_guild_config(path)


def test_guild_config_defaults_and_absent_sections(tmp_path):
    assert load_guild_config(tmp_path / "missing.toml") == GuildConfig()
    assert config(tmp_path, '[human]\nnickname="Synthetic"\n') == GuildConfig()
    assert (GuildConfig().theorycrafter_ilvl, GuildConfig().late_summon_s) == (277, (60, 180))
    assert GuildConfig().offline_rotation == ("icc25", "icc10", "toc25", "ulduar25")
    assert GuildConfig().ranks == {"officer": 1, "raider": 2, "social": 3}
    assert DkpConfig().prices == {"weapon_2h": 80, "weapon_1h": 50, "trinket": 50,
                                  "token": 40, "other": 30}


def test_partial_tables_merge_defaults_and_convert_paths(tmp_path):
    cfg = config(tmp_path, '''[guild]
offline_window = ["00:00", "23:59"]
offline_rotation = ["icc10h", "toc25h", "rs10h", "ony25"]
friends_cap = 50
friends_add = 1.0
friends_remove = 0.0
theorycrafter_ilvl = 0
late_summon_s = [0, 600]
ranks = {raider = 4}
worldserver_conf = "synthetic/server.conf"
mysql_exe = "synthetic/mysql.exe"
[dkp]
kill = 12
prices = {other = 20}
''')
    assert cfg.offline_window == ("00:00", "23:59")
    assert cfg.offline_rotation == ("icc10h", "toc25h", "rs10h", "ony25")
    assert cfg.ranks == {"officer": 1, "raider": 4, "social": 3}
    assert cfg.dkp.prices["other"] == 20 and cfg.dkp.prices["weapon_2h"] == 80
    assert cfg.worldserver_conf.name == "server.conf" and cfg.mysql_exe.name == "mysql.exe"
    assert cfg.dkp.attendance == 5. and cfg.dkp.kill == 12


@pytest.mark.parametrize(("setting", "value"), [
    ("friends_cap", "60"), ("friends_cap", "true"), ("friends_cap", "4.0"),
    ("friends_add", "nan"), ("friends_add", "inf"), ("friends_remove", "0.45"),
    ("friends_add", "false"), ("late_summon_s", "[180,60]"),
    ("late_summon_s", "[0,601]"), ("late_summon_s", "[true,60]"),
    ("late_summon_s", "[0,1,2]"), ("theorycrafter_ilvl", "199"),
    ("theorycrafter_ilvl", "301"), ("theorycrafter_ilvl", "false"),
    ("offline_window", '["24:00","18:00"]'), ("offline_window", '["9:00","18:00"]'),
    ("offline_window", '["09:60","18:00"]'), ("offline_window", '["09:00"]'),
    ("offline_rotation", '["voa25"]'), ("offline_rotation", '["ulduar10h"]'),
    ("offline_rotation", '["icc20"]'), ("offline_rotation", '["icecrown25"]'),
    ("offline_rotation", "[]"), ("offline_rotation", '"icc25"'),
    ("offline_max_wipes", "21"), ("offline_max_wipes_per_boss", "0"),
    ("offline_timeout_min", "361"), ("level_to", "81"), ("ranks", "{officer=0}"),
    ("ranks", "{raider=true}"), ("ranks", "{social=10}"), ("ranks", "{typo=3}"),
    ("raid_call_delay_s", "-1"), ("assemble_timeout_min", "0"),
    ("worldserver_conf", '""'), ("mysql_exe", "false"), ("unknown", "1"),
])
def test_invalid_guild_value_names_the_setting(tmp_path, setting, value):
    with pytest.raises(ConfigError, match=rf"guild\.{setting}"):
        config(tmp_path, f"[guild]\n{setting} = {value}\n")


@pytest.mark.parametrize(("setting", "value"), [
    ("decay", "1.0"), ("decay", "-0.1"), ("decay", "nan"), ("decay", "inf"),
    ("decay", "false"), ("decay_weekday", "7"), ("decay_weekday", "true"),
    ("decay_hour", "24"), ("kill", "-1"), ("attendance", "nan"), ("standby", "true"),
    ("claim_window_s", "-1"), ("prices", "{other=-1}"), ("prices", "{other=30.0}"),
    ("prices", "{other=true}"), ("prices", "{unknown=30}"), ("unknown", "1"),
])
def test_invalid_dkp_value_names_the_setting(tmp_path, setting, value):
    with pytest.raises(ConfigError, match=rf"dkp\.{setting}"):
        config(tmp_path, f"[dkp]\n{setting} = {value}\n")


def test_bad_section_or_malformed_file_is_a_config_error(tmp_path):
    for text in ('guild = "bad"', 'dkp = []', '[guild\n'):
        with pytest.raises(ConfigError):
            config(tmp_path, text)


def test_oversized_numeric_value_still_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match=r"dkp\.kill"):
        config(tmp_path, "[dkp]\nkill = " + "9" * 400 + "\n")


def test_config_is_frozen_including_nested_prices_and_ranks():
    cfg = GuildConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.friends_cap = 1
    with pytest.raises(TypeError):
        cfg.ranks["officer"] = 0
    with pytest.raises(TypeError):
        cfg.dkp.prices["other"] = 0


def test_rotation_keys_use_catalog_and_canonical_storage_keys(tmp_path):
    from leeroy_mind.guild.raids import CATALOG, split_key

    cfg = config(tmp_path, '[guild]\noffline_rotation = ["ICC25", "ToC10H"]\n')
    assert cfg.offline_rotation == ("icc25", "toc10h")
    for raid in CATALOG.values():
        for size in raid.sizes:
            for heroic in (False, True) if raid.heroic else (False,):
                key = f"{raid.key}{size}{'h' if heroic else ''}"
                assert split_key(key) == (raid, size, heroic)
                text = f'[guild]\noffline_rotation = ["{key}"]\n'
                if raid.key == "voa":
                    with pytest.raises(ConfigError, match=r"guild\.offline_rotation"):
                        config(tmp_path, text)
                else:
                    assert config(tmp_path, text).offline_rotation == (key,)
