"""Culture Part B: the server's own event calendar. A fake query stands in for MySQL; every row
and name is invented, and no test reaches a database server."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.awareness import CardContext, card
from leeroy_mind.culture import wowcal
from leeroy_mind.culture.config import load_culture_config
from leeroy_mind.culture.live import CultureLive, attach_culture
from leeroy_mind.library import mysql
from leeroy_mind.realm.protocol import GameEvent
from leeroy_mind.server import seeds

UTC = timezone.utc
SUNDAY = datetime(2026, 11, 1, 14, 0, tzinfo=UTC).timestamp()   # a Sunday, 14:00
WEEK = 7 * 86400


def at(*args):
    return datetime(*args, tzinfo=UTC).timestamp()


def row(entry=15, name="Invented Fishing Contest", *, start=SUNDAY, end=None, occurence=10080, length=120,
        holiday=301, stage=0):
    """One mysql --batch row of wowcal.SQL, as strings (None for NULL)."""
    return [str(entry), None if start is None else str(int(start)), None if end is None else str(int(end)),
            str(occurence), str(length), str(holiday), str(stage), name]


def cache(store, rows, fetched=None):
    wowcal.store_calendar(store, wowcal.parse_rows(rows), store.clock.now() if fetched is None else fetched)


def test_occurrences_repeat_and_respect_end_time():
    rows = wowcal.parse_rows([row(end=SUNDAY + 3 * WEEK + 3600),
                              row(16, "Invented Holiday", start=None, occurence=525600, length=21600, stage=2)])
    assert [r.scheduled for r in rows] == [True, False]
    found = wowcal.occurrences(rows, now=at(2026, 11, 4, 12), ahead_days=40)
    assert [e.starts for e in found] == [SUNDAY + WEEK, SUNDAY + 2 * WEEK, SUNDAY + 3 * WEEK]
    assert found[0].ends == SUNDAY + WEEK + 7200 and found[-1].ends == SUNDAY + 3 * WEEK + 3600
    running = wowcal.occurrences(rows, now=SUNDAY + WEEK + 600, ahead_days=1)
    assert running[0].starts == SUNDAY + WEEK and running[0].starts < SUNDAY + WEEK + 600 < running[0].ends
    assert wowcal.occurrences(rows, now=SUNDAY + 4 * WEEK, ahead_days=40) == []


def test_rows_the_core_does_not_schedule_from_the_table_get_no_dates():
    rows = wowcal.parse_rows([
        row(24, "Invented Brewing Fair", start=None, holiday=372, stage=2),
        row(25, "Invented Fair Building (Somewhere)", start=None, holiday=372, stage=1),
        row(26, "Invented Old Row", start=at(2000, 1, 1, 14), holiday=0),
        row(18, "Call to Arms: Invented Valley!", start=at(2010, 5, 7, 13), occurence=60480, length=6240,
            holiday=283, stage=1),
        row(64, "Invented Derby Fishing Pools", start=at(2014, 3, 22, 14), length=60, holiday=424)])
    by = {r.entry: r for r in rows}
    assert 25 not in by  # a preparation stage is no event of its own
    assert not by[24].scheduled and not by[26].scheduled and not by[18].scheduled
    assert by[18].name == "Call to Arms: Invented Valley" and by[64].name == "Invented Derby"
    assert by[64].scheduled and wowcal.occurrences(rows, now=at(2026, 11, 4), ahead_days=7)[0].entry == 64


def test_active_from_bridge_wins_over_the_schedule(store):
    rows = [row(), row(24, "Invented Brewing Fair", start=None, holiday=372, stage=2, length=21600),
            row(30, "Invented Weekly Hunt", start=SUNDAY + 86400, length=180, holiday=0)]
    cache(store, rows)
    now = SUNDAY + WEEK + 1800  # the fishing contest runs by the table
    def listed():
        values = {k: store.get_meta(k) for k in (wowcal.CAL_KEY, wowcal.ACTIVE_KEY, wowcal.STOPPED_KEY)}
        return {e.entry: (e, on) for e, on in wowcal.from_meta(values, now=now, ahead_days=7)}
    assert listed()[15][1] is True and 24 not in listed()
    # the server stopped the contest early: it is over, whatever the table says
    assert wowcal.record(store, 15, "stop", now - 60)
    assert listed()[15][1] is False and listed()[15][0].starts == SUNDAY + 2 * WEEK
    # a holiday the table cannot date is on once the bridge says it started; its end is unknown
    assert wowcal.record(store, 24, "start", now - 2 * 86400)
    brew, on = listed()[24]
    assert on and brew.ends is None
    assert wowcal.describe(brew, on, datetime.fromtimestamp(now, UTC)) == "Invented Brewing Fair is on now"
    # the bridge's start of an event the table says is not running yet wins too
    assert wowcal.record(store, 30, "start", now - 60)
    assert listed()[30][1] is True
    # a start whose stop was never seen (a worldserver restart) ages out after its length
    assert wowcal.record(store, 30, "start", now - 2 * 86400)
    assert listed()[30][1] is False
    # entries the cached calendar does not hold are never written
    before = store.get_meta(wowcal.ACTIVE_KEY)
    assert not wowcal.record(store, 73, "start", now) and store.get_meta(wowcal.ACTIVE_KEY) == before
    assert not wowcal.record(store, 0, "start", now) and not wowcal.record(store, 15, "begin", now)


@pytest.fixture
def fake_query():
    """Stands in for library.mysql.query: records each call, never reaches a server."""
    def query(db, sql, mysql_exe, *, timeout, max_ms):
        mysql.check_select(sql)  # one SELECT, no comment, no client command
        query.calls.append((db.name, db.password, sql, timeout, max_ms))
        return [row(), row(3, "Invented Faire (Somewhere)", start=None, holiday=374, stage=2)]
    query.calls = []
    return query


def test_read_uses_world_db_read_only(fake_query, tmp_path, caplog):
    conf = tmp_path / "worldserver.conf"
    conf.write_text('CharacterDatabaseInfo = "127.0.0.1;3306;user;chars-secret;acore_characters"\n'
                    'WorldDatabaseInfo = "127.0.0.1;3306;user;invented-secret;acore_world"\n', encoding="utf-8")
    app = SimpleNamespace(guild=SimpleNamespace(cfg=SimpleNamespace(worldserver_conf=conf, mysql_exe=Path("mysql.exe"))),
                          data_dir=tmp_path)
    rows = wowcal.read_calendar(app, query=fake_query)
    assert [r.entry for r in rows] == [3, 15]
    assert fake_query.calls == [("acore_world", "invented-secret", wowcal.SQL, 10, 5000)]
    assert wowcal.SQL.startswith("SELECT ") and "Kalu''ak" in wowcal.SQL
    # a failing read logs its class only: never the server's text or the password
    def broken(db, sql, mysql_exe, **kw):
        raise mysql.QueryError("mysql failed (1): invented-secret")
    live = CultureLive.__new__(CultureLive)
    live.app = SimpleNamespace(store=None, clock=SimpleNamespace(local=lambda: datetime.now(UTC),
                               now=lambda: 0.0), guild=app.guild, data_dir=tmp_path)
    live.calendar, live.loot, live.query, live._closed = True, False, broken, False
    live._calendar_retry = 0.0
    live.app.store = SimpleNamespace(get_meta=lambda key: None)
    with caplog.at_level(logging.INFO):
        asyncio.run(live.calendar_tick(1000.0))
    assert "culture_wowcal_failed code=QueryError" in caplog.text
    assert "secret" not in caplog.text and live._calendar_retry == 1000.0 + wowcal.RETRY_S


def test_daily_read_is_due_after_0805_once():
    morning = datetime(2026, 11, 4, 8, 4, tzinfo=UTC)
    assert wowcal.due(None, morning)
    assert not wowcal.due(at(2026, 11, 3, 9), morning)
    assert wowcal.due(at(2026, 11, 3, 9), morning + timedelta(minutes=2))
    assert not wowcal.due(at(2026, 11, 4, 8, 6), morning + timedelta(hours=10))


def test_card_lists_holidays_when_on_only(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    now = store.clock.now()
    cache(store, [row(start=now + 3600), row(24, "Invented Brewing Fair", start=None, holiday=372, stage=2,
                                             length=21600)])
    wowcal.record(store, 24, "start", now - 86400)
    names = {101: "Arkon"}
    off = card(store.conn, 101, context=CardContext(channel="guild", now=now, names=names))
    assert "Server event" not in "\n".join(p.render() for p in off.parts)
    on = card(store.conn, 101, context=CardContext(channel="guild", now=now, names=names, holidays=True))
    lines = next(p.lines for p in on.parts if p.key == "calendar")
    assert lines == ("Server event: Invented Brewing Fair, on now",
                     "Server event: Invented Fishing Contest, starts in 1 hour")


def test_wow_event_seeds_only_with_the_calendar_switch(store):
    cast = seed_test_cast(store)
    store.conn.execute("ATTACH DATABASE ':memory:' AS library")
    store.conn.execute("CREATE TABLE library.chunks(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, text TEXT)")
    store.conn.execute("CREATE TABLE library.item(id INTEGER, name TEXT)")
    store.conn.execute("INSERT INTO library.chunks(kind,title,text) VALUES ('event','Invented Faire',"
                       "'The Invented Faire is a travelling carnival. It moves every month.')")
    now = store.clock.now()
    cache(store, [row(start=now + 86400), row(3, "Invented Faire (Somewhere)", start=None, holiday=374, stage=2)])
    wowcal.record(store, 3, "start", now - 3600)
    def gather(**flags):
        return [s for s in seeds.gather(store.conn, now=now, local=store.clock.local(), cast=cast,
                                        officers=frozenset(), human=1, human_name='', trial_rank=4, **flags)
                if s.kind == "wow_event"]
    assert gather() == [] and gather(culture_enabled=True, culture_store=store) == []
    found = {s.fact: s for s in gather(culture_enabled=True, culture_store=store, culture_calendar=True)}
    faire = found["Invented Faire (Somewhere) is on now. The Invented Faire is a travelling carnival."]
    assert faire.channels == ("achievements-and-mounts", "general") and not faire.officers_only
    fishing = found["Invented Fishing Contest starts tomorrow at 20:00"]
    assert fishing.channels[0] == "professions-and-gold"
    assert all(s.key.startswith("wow_event:") for s in found.values())


@pytest.mark.parametrize("table", ['[culture]\nenabled=true\ncalendar="yes"\n', '[culture]\nenabled=true\nloot=1\n'])
def test_invalid_switch_turns_all_culture_off(tmp_path, table):
    path = tmp_path / "mind.toml"
    path.write_text("[server]\nenabled=true\n" + table, encoding="utf-8")
    assert not load_culture_config(path).enabled
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\n", encoding="utf-8")
    cfg = load_culture_config(path)
    assert cfg.enabled and not cfg.calendar and not cfg.loot


def test_attach_adds_nothing_until_a_part_b_switch(tmp_path):
    from conftest import run
    from selfies.fakes import mind_app
    for name in ("off", "on"):
        (tmp_path / name).mkdir()
    off = mind_app(tmp_path / "off", toml="[server]\nenabled=true\n[culture]\nenabled=true\n")
    try:
        assert off.server.culture is None and not getattr(off.engine, "culture_calendar", False)
        assert not any(getattr(h, "__self__", None).__class__ is CultureLive
                       for handlers in off._handlers.values() for h in handlers)
    finally:
        run(off.stop())
    on = mind_app(tmp_path / "on", toml="[server]\nenabled=true\n[culture]\nenabled=true\ncalendar=true\n")
    try:
        live = on.server.culture
        assert live is not None and live.calendar and not live.loot
        assert live.on_game_event in on._handlers["game_event"] and on.engine.culture_calendar is True
        assert live.query is None  # a synthetic model client never reaches the game database
        cache(on.store, [row()])
        run(live.on_game_event(GameEvent(ts=int(on.clock.now() * 1000), event_id=15, state="start")))
        assert json.loads(on.store.get_meta(wowcal.ACTIVE_KEY)) == {"15": on.clock.now()}
    finally:
        run(on.stop())


@pytest.mark.parametrize("calendar", [False, True])
def test_snapshot_gathers_server_events_only_with_the_switch(store, calendar):
    from leeroy_mind.culture.config import CultureConfig
    from leeroy_mind.server import runtime
    from server.test_runtime import app_for
    app, _, _ = app_for(store)
    app.culture_config = CultureConfig(enabled=True, calendar=calendar)
    app.engine.human = None
    now = store.clock.now()
    cache(store, [row(start=now + 3600)])
    store.conn.commit()
    observed = asyncio.run(runtime.snapshot(app, now, include_seeds=True))
    found = [s.fact for s in observed.seeds if s.kind == "wow_event"]
    assert found == (["Invented Fishing Contest starts today at 21:00"] if calendar else [])


def test_the_daily_read_caches_once_and_keeps_bridge_times_of_known_entries(tmp_path, fake_query, caplog):
    from conftest import run
    from selfies.fakes import mind_app
    conf = tmp_path / "worldserver.conf"
    conf.write_text('WorldDatabaseInfo = "127.0.0.1;3306;user;invented-secret;acore_world"\n', encoding="utf-8")
    toml = ("[server]\nenabled=true\n[culture]\nenabled=true\ncalendar=true\n"
            f"[guild]\nworldserver_conf='{conf}'\nmysql_exe='mysql.exe'\n")
    app = mind_app(tmp_path, toml=toml)
    try:
        live = app.server.culture
        live.query = fake_query
        now = app.clock.now()
        # before any calendar is cached nothing is written; the events wait for the first read
        for entry, state in ((99, "start"), (3, "start"), (15, "start"), (15, "stop")):
            run(live.on_game_event(GameEvent(ts=int((now - 60) * 1000), event_id=entry, state=state)))
        assert app.store.get_meta(wowcal.ACTIVE_KEY) is None and len(live._pending) == 4
        with caplog.at_level(logging.INFO):
            run(live.calendar_tick(now))
            run(live.calendar_tick(now + 600))
        assert len(fake_query.calls) == 1 and "culture_wowcal_read events=2 scheduled=1" in caplog.text
        assert fake_query.calls[0][:2] == ("acore_world", "invented-secret")  # this test's conf only
        fetched, rows = wowcal.load_calendar(app.store.get_meta(wowcal.CAL_KEY))
        assert fetched == now and [r.entry for r in rows] == [3, 15]
        assert wowcal.load_times(app.store.get_meta(wowcal.ACTIVE_KEY)) == {3: now - 60}  # 99 is no calendar event
        assert wowcal.load_times(app.store.get_meta(wowcal.STOPPED_KEY)) == {15: now - 60} and not live._pending
        assert "secret" not in caplog.text
    finally:
        run(app.stop())
