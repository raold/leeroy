from dataclasses import FrozenInstanceError

import pytest

from leeroy_mind.config import ConfigError
from leeroy_mind.realm.config import LIMITS, RealmConfig, load_realm_config


def _config(tmp_path, settings):
    path = tmp_path / "realm.toml"
    path.write_text("[realm]\n" + settings, encoding="utf-8")
    return path


def test_missing_file_or_section_defaults_off(tmp_path):
    path = tmp_path / "missing.toml"
    assert load_realm_config(path) == RealmConfig(
        enabled=False, gank_human=True, trial_rank=4, member_cap=120,
        his_birthday="", guild_anniversary="")
    path.write_text('[human]\nnickname = "example"\n', encoding="utf-8")
    assert load_realm_config(path) == RealmConfig()


def test_partial_settings_keep_other_defaults_and_accept_utf8_bom(tmp_path):
    path = tmp_path / "realm.toml"
    path.write_text('[realm]\nenabled = true\n', encoding="utf-8-sig")
    assert load_realm_config(path) == RealmConfig(enabled=True)


@pytest.mark.parametrize("key", ["enabled", "gank_human"])
@pytest.mark.parametrize("value", ['1', '0', '"true"', '[]', '{}'])
def test_switches_require_toml_booleans(tmp_path, key, value):
    with pytest.raises(ConfigError, match=rf"realm\.{key}"):
        load_realm_config(_config(tmp_path, f"{key} = {value}\n"))


@pytest.mark.parametrize("rank", [2, 9])
def test_trial_rank_accepts_both_bounds(tmp_path, rank):
    assert load_realm_config(_config(tmp_path, f"trial_rank = {rank}\n")).trial_rank == rank


@pytest.mark.parametrize("value", ['1', '10', '-1', 'true', '4.0', '"4"'])
def test_trial_rank_rejects_bad_type_or_range_and_names_key(tmp_path, value):
    with pytest.raises(ConfigError, match=r"realm\.trial_rank"):
        load_realm_config(_config(tmp_path, f"trial_rank = {value}\n"))


@pytest.mark.parametrize("cap", [1, 1000])
def test_member_cap_accepts_configured_bounds(tmp_path, cap):
    assert load_realm_config(_config(tmp_path, f"member_cap = {cap}\n")).member_cap == cap


@pytest.mark.parametrize("value", ['0', '1001', '-1', 'true', '120.0', '"120"'])
def test_member_cap_rejects_bad_type_or_range(tmp_path, value):
    with pytest.raises(ConfigError, match=r"realm\.member_cap"):
        load_realm_config(_config(tmp_path, f"member_cap = {value}\n"))


@pytest.mark.parametrize("key", ["his_birthday", "guild_anniversary"])
@pytest.mark.parametrize("value", ["", "01-01", "02-29", "12-31"])
def test_dates_accept_exact_month_day_and_leap_day(tmp_path, key, value):
    result = load_realm_config(_config(tmp_path, f'{key} = "{value}"\n'))
    assert getattr(result, key) == value


@pytest.mark.parametrize("key", ["his_birthday", "guild_anniversary"])
@pytest.mark.parametrize("value", [
    '"3-19"', '"03-9"', '"02-30"', '"04-31"', '"00-01"', '"13-01"',
    '"01-00"', '" 03-19"', '"03-19 "', '"2000-03-19"', '"０３-１９"', 'true', '319',
])
def test_dates_reject_bad_shape_or_calendar_day(tmp_path, key, value):
    with pytest.raises(ConfigError, match=rf"realm\.{key}"):
        load_realm_config(_config(tmp_path, f"{key} = {value}\n"))


def test_unknown_key_fails_without_echoing_its_local_value(tmp_path):
    path = _config(tmp_path, 'unexpected = "local-value-sentinel"\n')
    with pytest.raises(ConfigError, match=r"realm\.unexpected") as caught:
        load_realm_config(path)
    assert "local-value-sentinel" not in str(caught.value)


def test_bad_known_value_is_not_echoed(tmp_path):
    path = _config(tmp_path, 'his_birthday = "local-date-sentinel"\n')
    with pytest.raises(ConfigError, match=r"realm\.his_birthday") as caught:
        load_realm_config(path)
    assert "local-date-sentinel" not in str(caught.value)


def test_realm_must_be_a_table(tmp_path):
    path = tmp_path / "realm.toml"
    path.write_text('realm = "local-value-sentinel"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="realm: expected a table") as caught:
        load_realm_config(path)
    assert "local-value-sentinel" not in str(caught.value)


@pytest.mark.parametrize("raw,error_name", [
    (b'[realm]\nenabled = "local-value-sentinel', "TOMLDecodeError"),
    (b'[realm]\nhis_birthday = "\xff"', "UnicodeDecodeError"),
])
def test_malformed_reads_expose_only_safe_error_type(tmp_path, raw, error_name):
    path = tmp_path / "local-path-sentinel.toml"
    path.write_bytes(raw)
    with pytest.raises(ConfigError) as caught:
        load_realm_config(path)
    assert str(caught.value) == f"realm: could not read configuration ({error_name})"


def test_unreadable_path_is_not_mistaken_for_missing_configuration(tmp_path):
    path = tmp_path / "local-path-sentinel.toml"
    path.mkdir()
    with pytest.raises(ConfigError) as caught:
        load_realm_config(path)
    assert "could not read configuration" in str(caught.value)
    assert "local-path-sentinel" not in str(caught.value)


def test_config_and_limits_are_immutable_and_recall_intervals_are_distinct():
    with pytest.raises(FrozenInstanceError):
        RealmConfig().enabled = True
    with pytest.raises(FrozenInstanceError):
        LIMITS.recall_repeat_s = 1
    assert LIMITS.recall_repeat_s == 45
    assert LIMITS.recall_cooldown_s == 120
    assert LIMITS.recruit_gap_s == 28 * LIMITS.day_s
    assert LIMITS.recruit_silence_s == 72 * LIMITS.hour_s
    assert LIMITS.absence_letter_after_s == LIMITS.week_s
    assert LIMITS.day_block_min_s == LIMITS.hour_s
    assert LIMITS.day_block_max_s == 3 * LIMITS.hour_s
    assert LIMITS.meeting_postpone_max_s == 3 * LIMITS.hour_s
