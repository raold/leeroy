from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind.clock import night_of
from leeroy_mind.server.clock import in_window, night_key, window_bounds

NY = ZoneInfo('America/New_York')
WINDOW = (time(3), time(8))


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=NY)


@pytest.mark.parametrize('hour,inside', [(2, False), (3, True), (7, True), (8, False), (23, False)])
def test_half_open(hour, inside):
    assert in_window(at(f'2026-11-12 {hour:02}:00'), WINDOW) is inside


@pytest.mark.parametrize('value,start,end', [
    ('2026-11-12 02:00', '2026-11-12 03:00', '2026-11-12 08:00'),
    ('2026-11-12 03:00', '2026-11-12 03:00', '2026-11-12 08:00'),
    ('2026-11-12 07:59', '2026-11-12 03:00', '2026-11-12 08:00'),
    ('2026-11-12 08:00', '2026-11-13 03:00', '2026-11-13 08:00'),
])
def test_bounds_containing_or_next(value, start, end):
    assert window_bounds(at(value), WINDOW) == (at(start).timestamp(), at(end).timestamp())


@pytest.mark.parametrize('value,inside,start,end', [
    ('2026-11-12 23:30', True, '2026-11-12 23:00', '2026-11-13 02:00'),
    ('2026-11-13 01:00', True, '2026-11-12 23:00', '2026-11-13 02:00'),
    ('2026-11-13 02:00', False, '2026-11-13 23:00', '2026-11-14 02:00'),
    ('2026-11-13 12:00', False, '2026-11-13 23:00', '2026-11-14 02:00'),
])
def test_overnight(value, inside, start, end):
    window = (time(23), time(2))
    assert in_window(at(value), window) is inside
    assert window_bounds(at(value), window) == (at(start).timestamp(), at(end).timestamp())


def test_six_oclock_matches_old_key():
    for hour in range(24):
        local = at(f'2026-11-12 {hour:02}:15')
        assert night_key(local, (time(6), time(11))) == night_of(local)


@pytest.mark.parametrize('day,prior', [('2026-11-12', '2026-11-11'),
                                      ('2026-03-08', '2026-03-07'),
                                      ('2026-11-01', '2026-10-31')])
def test_key_and_bounds_across_dst(day, prior):
    assert night_key(at(day + ' 02:59'), WINDOW) == prior
    assert night_key(at(day + ' 03:00'), WINDOW) == day
    start, end = window_bounds(at(day + ' 04:00'), WINDOW)
    assert end - start == 5 * 3600


@pytest.mark.parametrize('day,hours', [('2026-03-08', 4), ('2026-11-01', 6)])
def test_window_spanning_dst_uses_wall_clock(day, hours):
    start, end = window_bounds(at(day + ' 03:30'), (time(0), time(5)))
    assert end - start == hours * 3600


@pytest.mark.parametrize('day,hours', [('2026-03-08', 4), ('2026-11-01', 6)])
def test_system_clock_resolves_each_boundary_in_the_host_zone(day, hours, monkeypatch):
    import os
    import time as system_time
    from leeroy_mind.clock import SystemClock
    clock = SystemClock()
    old = os.environ.get('TZ')
    try:
        if hasattr(system_time, 'tzset'):
            monkeypatch.setenv('TZ', 'America/New_York')
            system_time.tzset()
        elif datetime.fromisoformat(day + ' 00:30').astimezone().utcoffset() != at(day + ' 00:30').utcoffset():
            pytest.skip('Windows host is not on the tested New York timezone')
        monkeypatch.setattr(clock, 'now', lambda: at(day + ' 00:30').timestamp())
        local = clock.local()  # production fixed-offset datetime, not a ZoneInfo injection
        start, end = window_bounds(local, (time(0), time(5)), clock=clock)
        assert end - start == hours * 3600
        assert end == at(day + ' 05:00').timestamp()
    finally:
        if hasattr(system_time, 'tzset'):
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            system_time.tzset()
