"""Races against the owed-answer store (pipeline._deliver_discord: Store.write_first and one
retry): two answers at once, a lock held past both tries, the mind stopping before the commit,
and a lock in the separate record after the commit. Scratch databases and invented text only."""
import logging
import sqlite3

from discord.test_discord_talk import answer, answers, clock, post, rig_for, talk_for  # noqa: F401
from leeroy_mind.discord import covered
from leeroy_mind.talk import pipeline


def second_connection(store, timeout=0):
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    return sqlite3.connect(path, timeout=timeout, isolation_level=None)


def test_two_answers_at_once_each_meet_a_writer_and_each_is_stored_once(store, clock, tmp_path,
                                                                         monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='an invented answer')
    talk = talk_for(rig)
    other, real, tried = second_connection(store), pipeline.store_line, []

    def interleaved(st, *a, **k):
        st.conn.execute('SELECT count(*) FROM discord_message').fetchall()
        try:
            other.execute("INSERT INTO meta(key,value) VALUES ('invented:w:%d','1')" % len(tried))
            tried.append('wrote')
        except sqlite3.OperationalError:
            tried.append('waited')
        return real(st, *a, **k)
    monkeypatch.setattr(pipeline, 'store_line', interleaved)
    a, b = post(rig, 'dm:102', 'invented one'), post(rig, 'dm:103', 'invented two')

    async def go():
        talk.on_his_post(a)
        talk.on_his_post(b)
        await rig.advance(200.0)
    rig.sync(go())
    assert len(answers(rig, 'dm:102', a)) == 1 and len(answers(rig, 'dm:103', b)) == 1
    assert tried and set(tried) == {'waited'}
    assert not any('store_error' in m or 'discord_store_retry' in m for m in caplog.messages)
    other.close()
    rig.close()


def test_a_lock_held_past_both_tries_loses_nothing_and_a_rescue_answers_once(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='an invented answer')
    talk = talk_for(rig)
    mid = post(rig, 'dm:102', 'invented question')
    store.conn.commit()
    holder = second_connection(store)
    holder.execute('BEGIN IMMEDIATE')  # held across both of her tries (busy_timeout each)
    answer(rig, talk, mid)
    assert answers(rig, 'dm:102', mid) == []
    assert any('discord_store_retry' in m for m in caplog.messages)
    # the drop's own utterance row waits on the same lock: say_dropped or talk_job_failed
    assert any(('say_dropped' in m and 'store_error' in m) or 'talk_job_failed' in m for m in caplog.messages)
    assert not covered.read(store.conn, 'dm:102', any_thread=True).has(mid)  # still owed: the watch rescues
    holder.execute('ROLLBACK')
    holder.close()
    answer(rig, talk, mid)  # the rescue (or her retry) once the lock is gone
    assert len(answers(rig, 'dm:102', mid)) == 1
    answer(rig, talk, mid)  # handed again: covered, never a second answer
    assert len(answers(rig, 'dm:102', mid)) == 1
    rig.close()


class Stop(BaseException):
    """The mind stopping mid-write (a BaseException, as KeyboardInterrupt is)."""


def test_the_mind_stopping_after_her_insert_and_before_commit_leaves_nothing(store, clock, tmp_path,
                                                                           monkeypatch):
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='an invented answer')
    talk = talk_for(rig)
    real, stopped = pipeline.store_line, []

    def stop_after(st, *a, **k):
        mid = real(st, *a, **k)
        if not stopped:
            stopped.append(mid)
            raise Stop()
        return mid
    monkeypatch.setattr(pipeline, 'store_line', stop_after)
    mid = post(rig, 'dm:102', 'invented question')
    try:
        answer(rig, talk, mid)
    except Stop:
        pass
    store.conn.rollback()
    assert stopped and answers(rig, 'dm:102', mid) == []
    assert not covered.read(store.conn, 'dm:102', any_thread=True).has(mid)
    answer(rig, talk, mid)
    assert len(answers(rig, 'dm:102', mid)) == 1
    rig.close()


def test_a_lock_in_the_record_after_commit_keeps_the_answer_and_never_doubles_it(store, clock, tmp_path,
                                                                                monkeypatch, caplog):
    """Her message and its covered receipt commit; then the separate chat-line record fails on a
    lock. The answer stays (he sees it) and is never sent again; only its chat line is missing."""
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply='an invented answer')
    talk = talk_for(rig)
    real, failed = pipeline.Pipeline._record, []

    def locked_once(self, turn, meta, *a, **k):
        if not failed and k.get('discord_id') is not None and k.get('after_commit') is None:
            failed.append(k['discord_id'])
            raise sqlite3.OperationalError('database is locked')
        return real(self, turn, meta, *a, **k)
    monkeypatch.setattr(pipeline.Pipeline, '_record', locked_once)
    mid = post(rig, 'dm:102', 'invented question')
    answer(rig, talk, mid)
    got = answers(rig, 'dm:102', mid)
    if failed:
        assert len(got) == 1
        assert covered.read(store.conn, 'dm:102', any_thread=True).has(mid)
        answer(rig, talk, mid)
        assert len(answers(rig, 'dm:102', mid)) == 1
    rig.close()
