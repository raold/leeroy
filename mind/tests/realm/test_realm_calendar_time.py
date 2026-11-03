from datetime import datetime, timezone
from dataclasses import FrozenInstanceError
from zoneinfo import ZoneInfoNotFoundError

import pytest

from leeroy_mind.realm import calendar_time
from leeroy_mind.realm.calendar_time import (
    CADENCE, ProductCalendarUnavailable, local_write_day, product_period,
)
from leeroy_mind.realm.records import ProductPeriod


def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


def test_exact_tuesday_boundary_starts_the_new_half_open_period():
    boundary = _utc(2026, 11, 3, 9)
    previous = product_period(boundary - 0.001)
    current = product_period(boundary)
    assert previous.key == "2026-10-27"
    assert previous.ends_at == current.starts_at == boundary
    assert current == ProductPeriod(CADENCE, "2026-11-03", boundary, _utc(2026, 11, 10, 9))
    assert product_period(current.ends_at - 0.001) == current
    assert product_period(current.ends_at).key == "2026-11-10"
    with pytest.raises(FrozenInstanceError):
        current.key = "2026-11-10"


@pytest.mark.parametrize("sample,key,start,end,hours", [
    (_utc(2026, 3, 8, 12), "2026-03-03", _utc(2026, 3, 3, 9), _utc(2026, 3, 10, 8), 167),
    (_utc(2026, 11, 1, 12), "2026-10-27", _utc(2026, 10, 27, 8), _utc(2026, 11, 3, 9), 169),
])
def test_dst_changes_elapsed_hours_without_moving_the_local_boundary(sample, key, start, end, hours):
    period = product_period(sample)
    assert (period.cadence, period.key, period.starts_at, period.ends_at) == (CADENCE, key, start, end)
    assert (period.ends_at - period.starts_at) / 3600 == hours


def test_local_write_day_changes_at_actual_new_york_midnight():
    source = _utc(2026, 11, 3, 4, 59, 59)
    actual_write = _utc(2026, 11, 3, 5)
    assert local_write_day(source) == "2026-11-02"
    assert local_write_day(actual_write) == "2026-11-03"
    assert product_period(source).key == product_period(actual_write).key == "2026-10-27"


def test_repeated_fall_hour_and_spring_gap_do_not_change_day_or_period():
    assert local_write_day(_utc(2026, 11, 1, 5, 30)) == local_write_day(_utc(2026, 11, 1, 6, 30))
    assert product_period(_utc(2026, 11, 1, 5, 30)) == product_period(_utc(2026, 11, 1, 6, 30))
    assert local_write_day(_utc(2026, 3, 8, 6, 59, 59)) == local_write_day(_utc(2026, 3, 8, 7))


@pytest.mark.parametrize("seconds", [
    True, False, None, "1", -1, float("nan"), float("inf"), float("-inf"),
    253402300800, 10 ** 1000,
])
def test_invalid_or_out_of_range_time_is_rejected(seconds):
    for function in (product_period, local_write_day):
        with pytest.raises(ValueError, match="seconds"):
            function(seconds)


def test_numeric_subclasses_do_not_supply_a_time():
    class Timestamp(float):
        pass

    with pytest.raises(ValueError, match="seconds"):
        local_write_day(Timestamp(1))


def test_epoch_zero_uses_the_named_zone_and_end_of_range_needs_a_complete_period():
    assert local_write_day(0) == "1969-12-31"
    assert product_period(0).key == "1969-12-30"
    assert local_write_day(253402300799.999) == "9999-12-31"
    with pytest.raises(ValueError, match="complete product period"):
        product_period(253402300799.999)


@pytest.mark.parametrize("seconds,day", [
    (2147483648, "2038-01-18"),
    (32503680000, "2999-12-31"),
    (253402300799.999, "9999-12-31"),
])
def test_representable_dates_do_not_depend_on_platform_timestamp_limits(seconds, day):
    assert local_write_day(seconds) == day


def test_missing_named_timezone_is_explicitly_unavailable(monkeypatch):
    requested = []

    def unavailable(key):
        requested.append(key)
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(calendar_time, "ZoneInfo", unavailable)
    for function in (product_period, local_write_day):
        with pytest.raises(ProductCalendarUnavailable, match="America/New_York"):
            function(_utc(2026, 11, 3))
    assert requested == ["America/New_York", "America/New_York"]
