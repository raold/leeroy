from datetime import datetime, timezone
import json

import pytest

from leeroy_mind.discord.store import post, react
from leeroy_mind.server import records

DAY='2026-11-12'
NOW=datetime(2026,11,12,19,tzinfo=timezone.utc).timestamp()


def test_post_and_its_counts_roll_back_together(store):
    with pytest.raises(RuntimeError,match='later_failure'):
        with store.transaction():
            post(store,channel='general',author=101,origin='mind',text='A test line',now=NOW)
            records.posted(store,DAY,NOW,conversation='c1')
            raise RuntimeError('later_failure')
    assert store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]==0
    assert records.counts(store,DAY)==dict.fromkeys(records.COUNTS,0)
    assert store.get_meta('server:conversation:c1') is None


def test_dm_receipts_and_daily_limit_roll_back_with_the_post(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            post(store,channel='dm:101',author=101,origin='mind',text='A private test line',now=NOW)
            records.posted(store,DAY,NOW,dm_guid=101)
            raise RuntimeError('failed_record')
    assert store.get_meta('server:dm:open:101') is None
    assert store.get_meta('server:dm:day:'+DAY+':101') is None
    assert records.counts(store,DAY)['dms']==0


def test_messages_count_each_post_but_a_conversation_only_once_across_days(store):
    for day in (DAY,DAY,'2026-11-13'):
        with store.transaction():
            post(store,channel='general',author=101,origin='mind',text='Another test line',now=NOW)
            records.posted(store,day,NOW,conversation='same')
    assert records.counts(store,DAY)==dict(conversations=1,messages=2,reactions=0,dms=0)
    assert records.counts(store,'2026-11-13')==dict(conversations=0,messages=1,reactions=0,dms=0)


@pytest.mark.parametrize('arguments',[{'conversation':'outside'},{'dm_guid':101},{}])
def test_posted_refuses_outside_a_transaction_before_any_receipt_is_written(store,arguments):
    with pytest.raises(RuntimeError,match='server_receipt_without_transaction'):
        records.posted(store,DAY,NOW,**arguments)
    assert not store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'server:%'").fetchall()


def test_reaction_and_count_share_the_same_transaction(store):
    mid=post(store,channel='general',author=101,origin='mind',text='A line',now=NOW)
    with pytest.raises(ValueError,match='abort'):
        with store.transaction():
            assert react(store,mid,member=102,emoji='👍',now=NOW)
            records.increment(store,DAY,reactions=1)
            raise ValueError('abort')
    assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
    assert records.counts(store,DAY)['reactions']==0


def test_invalid_receipts_do_not_replace_good_counts(store):
    with store.transaction():
        records.increment(store,DAY,messages=2)
    for changes in ({'messages':True},{'messages':-1},{'unknown':1}):
        with pytest.raises(ValueError,match='invalid_server_count'):
            with store.transaction():
                records.increment(store,DAY,**changes)
    assert records.counts(store,DAY)['messages']==2


def test_prune_only_removes_old_owned_server_receipts(store):
    old=str(NOW-8*86400)
    keep={
        'server:day:2026-11-05':'{}', 'server:day:2026-11-12':'{}',
        'server:dm:day:2026-11-05:101':old,'server:seed:recent':str(NOW),
        'server:seed:bad':'not-a-timestamp','server:day:bad':'{}',
        'server:playing':'{"at":1}','server:recent':'[]','server:voice_seats':'{}',
        'server:future:2020-01-01':old,'other:seed:old':old,'serverish:day:2020-01-01':'{}'}
    remove={
        'server:day:2026-11-04':'{}','server:dm:day:2026-11-04:101':old,
        'server:seed:life:1':old,'server:conversation:old':old,'server:dm:open:101':old}
    for key,value in {**keep,**remove}.items():
        store.set_meta(key,value)
    records.prune(store,DAY,NOW)
    assert all(store.get_meta(key)==value for key,value in keep.items())
    assert all(store.get_meta(key) is None for key in remove)


def test_recent_ring_keeps_the_last_twenty_without_changing_counters(store):
    for index in range(24):
        records.ended(store,dict(id=str(index),channel='general',seed='purpose',messages=0,started=NOW,reason='dry'))
    assert [r['id'] for r in json.loads(store.get_meta('server:recent'))]==[str(i) for i in range(4,24)]
    assert records.counts(store,DAY)==dict.fromkeys(records.COUNTS,0)
