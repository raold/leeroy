"""An answer he is owed is never lost to a lock (pipeline._deliver_discord, Store.write_first):
another connection writing between her unit's reads and her insert, and a store SQLite refuses
as busy once. Scratch databases and invented text only."""
import logging
import sqlite3
import time

import pytest

from discord.test_discord_talk import answer, answers, clock, post, rig_for, talk_for  # noqa: F401
from leeroy_mind.storage.store import Store
from leeroy_mind.talk import pipeline


def second_connection(store):
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    return sqlite3.connect(path, timeout=0, isolation_level=None)


def test_a_writer_between_her_reads_and_her_insert_waits_and_her_answer_is_stored(
        store, clock, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='hey, an invented answer')
    talk = talk_for(rig)
    other, real, tried = second_connection(store), pipeline.store_line, []

    def interleaved(st, *a, **k):
        # Her unit has read by now. A capture write on a second connection would commit here
        # and make her insert fail at once on a stale snapshot; with the write lock taken first
        # it cannot get in, and nothing of hers is lost.
        st.conn.execute('SELECT count(*) FROM discord_message').fetchall()
        try:
            other.execute("INSERT INTO meta(key,value) VALUES ('invented:other:%d','1')" % len(tried))
            tried.append('wrote')
        except sqlite3.OperationalError:
            tried.append('waited')
        return real(st, *a, **k)
    monkeypatch.setattr(pipeline, 'store_line', interleaved)
    mid = post(rig, 'dm:102', 'you around later?')
    answer(rig, talk, mid)
    assert [m['author'] for m in answers(rig, 'dm:102', mid)] == [102]
    assert tried and set(tried) == {'waited'}
    assert not any('say_dropped' in m or 'discord_store_failed' in m for m in caplog.messages)
    other.close()
    rig.close()


def test_a_store_refused_as_busy_is_tried_once_more_and_stored_once(store, clock, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='hey, an invented answer')
    talk = talk_for(rig)
    real, calls = pipeline.store_line, []

    def busy_once(st, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            real(st, *a, **k)  # written, then refused: the retry must not leave two messages
            raise sqlite3.OperationalError('database is locked')
        return real(st, *a, **k)
    monkeypatch.setattr(pipeline, 'store_line', busy_once)
    mid = post(rig, 'dm:102', 'you around later?')
    answer(rig, talk, mid)
    assert len(answers(rig, 'dm:102', mid)) == 1 and len(calls) == 2
    assert any('discord_store_retry' in m for m in caplog.messages)
    assert not any('say_dropped' in m for m in caplog.messages)
    rig.close()


def test_a_store_busy_twice_is_reported_and_not_retried_forever(store, clock, tmp_path, monkeypatch, caplog):
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='hey, an invented answer')
    talk = talk_for(rig)
    calls = []

    def always(st, *a, **k):
        calls.append(1)
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(pipeline, 'store_line', always)
    mid = post(rig, 'dm:102', 'you around later?')
    answer(rig, talk, mid)
    assert answers(rig, 'dm:102', mid) == [] and len(calls) % 2 == 0 and calls
    rig.close()


def test_both_tries_together_wait_no_longer_than_the_one_try_before(store, clock, tmp_path, monkeypatch):
    """Re-review 359: each try's lock wait is bounded, so a lock held past both tries stalls the
    event loop no longer than the single try did (the connection's busy_timeout)."""
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='hey, an invented answer')
    talk = talk_for(rig)
    waits, real = [], Store.write_first

    def spy(self, *, wait_s=None):
        waits.append(wait_s)
        return real(self, wait_s=wait_s)

    def always(st, *a, **k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(Store, 'write_first', spy)
    monkeypatch.setattr(pipeline, 'store_line', always)
    mid = post(rig, 'dm:102', 'you around later?')
    answer(rig, talk, mid)
    busy_s = store.conn.execute('PRAGMA busy_timeout').fetchone()[0] / 1000
    stores = [w for w in waits if w is not None]
    assert len(stores) == 2 and sum(stores) <= busy_s
    rig.close()


def test_write_first_waits_only_as_long_as_asked_and_keeps_busy_timeout(store):
    busy = store.conn.execute('PRAGMA busy_timeout').fetchone()[0]
    store.conn.commit()
    holder = second_connection(store)
    holder.execute('BEGIN IMMEDIATE')
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError), store.transaction():
            store.write_first(wait_s=.2)
    finally:
        holder.execute('ROLLBACK')
        holder.close()
    assert time.monotonic() - started < busy / 1000 / 2
    assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == busy
    with store.transaction():
        store.write_first(wait_s=.2)  # free again: taken at once
