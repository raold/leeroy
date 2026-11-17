"""`learn backfill-signals`: stored Discord exchanges replayed into the signals, the rewards that
follow, and the night's backlog limit. Scratch databases and invented lines only."""
import asyncio
import json
import sqlite3

import pytest

from conftest import HUMAN
from tests.claims.conftest import context  # noqa: F401  (fixture)
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import LINE_KEY
from leeroy_mind.learn import night
from leeroy_mind.learn.cli import backfill_main, learn_main
from leeroy_mind.learn.reward import reward
from leeroy_mind.protocol import Chat
from leeroy_mind.signals import backfill
from leeroy_mind.signals import discord as D
from leeroy_mind.signals import store as S

from .test_night_cli import enable
from .test_reward import line, signals


def message(store, clock, channel, author, text, *, reply_to=None, at=None):
    """A stored Discord message with its chat line and receipt; (message id, line id)."""
    now = clock.now() if at is None else at
    mind = author != HUMAN
    private = channel.startswith(('dm:', 'gdm:'))
    with store.transaction():
        mid = dstore.post(store, channel=channel, author=author, origin='mind' if mind else 'human', text=text,
                          reply_to=reply_to, now=now)
        peer = int(channel[3:]) if channel.startswith('dm:') else 0
        chat = Chat(0, channel if private else 'discord:' + channel, author, 'Invented',
                    (HUMAN if mind else peer) if peer else 0, text, 0, 0)
        lid = store.insert_chat_line(chat, session_id=None, origin='mind' if mind else 'human', ts=now)
        store.set_meta(LINE_KEY.format(mid), str(lid))
    return mid, lid


def seed(store, clock):
    """A DM exchange, a channel reply to him and one emoji, an hour ago; the member line ids."""
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    _, dm1 = message(store, clock, 'dm:101', 101, 'the invented kiln cracked my teapot', at=t)
    message(store, clock, 'dm:101', HUMAN, 'oh no, which teapot was it?', at=t + 90)
    _, dm2 = message(store, clock, 'dm:101', 101, 'the blue one from the market', at=t + 120)
    message(store, clock, 'dm:101', HUMAN, 'haha that market again', at=t + 200)
    mine, _ = message(store, clock, 'general', HUMAN, 'who is around tonight', at=t + 300)
    reply, ch = message(store, clock, 'general', 102, 'me, after the invented dinner', reply_to=mine, at=t + 330)
    _, idle = message(store, clock, 'general', 103, 'unrelated invented chatter', at=t + 340)
    message(store, clock, 'general', HUMAN, 'nice, see you then', at=t + 400)
    with store.transaction():
        dstore.react(store, reply, member=HUMAN, emoji='👍', now=t + 410)
    store.conn.commit()
    return dm1, dm2, ch, idle


def table(store):
    store.conn.commit()
    return {(r[0], r[1]): r[2] for r in store.conn.execute(
        "SELECT line_id,kind,value FROM talk_signal WHERE source='implicit'")}


def test_replay_finds_the_exchanges_and_writes_numbers_once(store, clock):
    dm1, dm2, ch, idle = seed(store, clock)
    sink, facts = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    result, new = backfill.report(store.conn, sink, facts)
    assert result['his_messages'] == 4 and result['answered_lines'] == 3 and result['his_reactions'] == 1
    assert result['lines_by_family'] == {'channel': 1, 'dm': 2} and result['members'] == 2
    assert result['new_observations'] == len(new) == sum(result['by_kind'].values())
    kinds = lambda lid: {k for (l, k) in new if l == lid}
    assert kinds(dm1) == {'replied', 'reply_latency_s', 'reply_len', 'asked_back', 'topic_kept', 'turns_after'}
    assert kinds(dm2) == {'replied', 'reply_latency_s', 'reply_len', 'laughed', 'topic_kept', 'thread_continued'}
    assert kinds(ch) == {'replied', 'reply_latency_s', 'reply_len', 'react_positive'}
    assert kinds(idle) == set()  # nobody addressed him there
    assert new[dm1, 'reply_latency_s'][1] == pytest.approx(90 / D.WINDOW_S)
    assert table(store) == {}  # a plan and a report write nothing
    with store.transaction():
        assert backfill.write(store.conn, new) == len(new)
    again, _ = backfill.report(store.conn, *backfill.plan(store.conn, since=clock.now() - 7200,
                                                          now=clock.now(), owner=HUMAN))
    assert again['new_observations'] == 0 and again['observations'] == len(new)
    assert 'invented' not in repr(table(store)) + json.dumps(result)


def test_replay_starts_at_since_and_never_reaches_past_the_retention(store, clock):
    seed(store, clock)
    late = backfill.plan(store.conn, since=clock.now() - 3600 + 150, now=clock.now(), owner=HUMAN)[0]
    assert {kind for _, kind in late.rows} >= {'replied'} and len({l for l, _ in late.rows}) == 2
    old = clock.now() - (backfill.RETENTION_DAYS + 5) * 86400
    message(store, clock, 'dm:101', 101, 'an invented ancient line', at=old)
    message(store, clock, 'dm:101', HUMAN, 'an invented ancient answer', at=old + 30)
    sink, facts = backfill.plan(store.conn, since=0, now=clock.now(), owner=HUMAN)
    assert facts['since'] == clock.now() - backfill.RETENTION_DAYS * 86400 and facts['his_messages'] == 4


def test_cli_dry_run_writes_nothing_and_go_snapshots_then_writes(store, clock, tmp_path, capsys):
    seed(store, clock)
    before = (tmp_path / 'mind.db').stat().st_mtime_ns
    assert backfill_main(tmp_path, clock.now() - 7200, now=clock.now()) == 0
    out = capsys.readouterr().out
    assert 'dry run, nothing written' in out and json.loads(out.splitlines()[0])['new_observations'] > 0
    assert table(store) == {} and not (tmp_path / 'snapshots').exists()
    assert (tmp_path / 'mind.db').stat().st_mtime_ns == before
    assert backfill_main(tmp_path, clock.now() - 7200, go=True, now=clock.now()) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result['written'] == result['new_observations'] == len(table(store)) > 0
    assert list((tmp_path / 'snapshots').glob('mind-pre-backfill-signals-*.db')) and result['snapshot']
    assert backfill_main(tmp_path, clock.now() - 7200, go=True, now=clock.now()) == 0
    assert 'nothing new' in capsys.readouterr().out
    assert len(list((tmp_path / 'snapshots').glob('*.db'))) == 1


def test_cli_go_is_refused_while_the_mind_holds_its_lock(store, clock, tmp_path, capsys):
    from leeroy_mind.app import acquire_instance_lock
    seed(store, clock)
    with acquire_instance_lock(tmp_path):
        assert backfill_main(tmp_path, clock.now() - 7200, go=True, now=clock.now()) == 1
        assert backfill_main(tmp_path, clock.now() - 7200, now=clock.now()) == 0  # a dry run needs no lock
    assert 'stop the mind' in capsys.readouterr().out and table(store) == {}
    assert learn_main(['backfill-signals', '--data-dir', str(tmp_path / 'absent'), '--since', '2026-11-13']) == 1
    with pytest.raises(SystemExit):
        learn_main(['backfill-signals', '--data-dir', str(tmp_path), '--since', 'yesterday'])


def test_go_refuses_a_database_older_than_the_discord_kinds(store, clock, tmp_path, capsys):
    seed(store, clock)
    with store.transaction():
        store.conn.execute("ALTER TABLE talk_signal RENAME TO talk_signal_new")
        store.conn.execute("CREATE TABLE talk_signal(id INTEGER PRIMARY KEY, line_id INTEGER, act_id INTEGER, "
                           "guid INTEGER, kind TEXT CHECK(kind IN ('replied')), value REAL, ts REAL, source TEXT)")
    store.conn.commit()
    assert backfill_main(tmp_path, clock.now() - 7200, go=True, now=clock.now()) == 1
    assert 'db migrate' in capsys.readouterr().out and table(store) == {}


def test_backfilled_evidence_is_scored_by_the_next_rewards_night(context, store, clock, tmp_path):  # noqa: F811
    enable(tmp_path)
    dm1, dm2, ch, idle = seed(store, clock)
    async def go():
        async with context(learn_enabled=True) as (ctx, _):
            await night.stage_rewards(ctx)
        values = dict(store.conn.execute("SELECT line_id,value FROM talk_reward"))
        assert values == {dm1: 0, dm2: 0, ch: 0, idle: 0}  # nothing observed: silence scores zero
        with store.transaction():
            sink, facts = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
            backfill.write(store.conn, backfill.report(store.conn, sink, facts)[1])
        await clock.advance(86400, step=86400)
        async with context(learn_enabled=True, night='2026-10-30') as (ctx, _):
            await night.stage_rewards(ctx)
        return dict(store.conn.execute("SELECT line_id,value FROM talk_reward"))
    values = asyncio.run(go())
    assert values[dm1] > 0 and values[dm2] > 0 and values[ch] > 0 and values[idle] == 0


@pytest.mark.parametrize('channel', ['dm:101', 'gdm:4', 'discord:general'])
@pytest.mark.parametrize('values,expected', [
    ({'replied': 1}, .5),
    ({'replied': 1, 'laughed': 1}, .75),
    ({'replied': 1, 'asked_back': 1, 'affection': 1, 'topic_kept': 1, 'sent_media': 1}, .9),
    ({'replied': 1, 'turns_after': .4}, .45),
    ({'react_positive': 1}, 1.),
    ({'react_negative': 1}, -1.),
    ({'replied': 1, 'react_positive': 1}, (.25 * .5 + .15) / .40),
    ({'replied': 1, 'objected': 1}, .25 * .5 / .65 - .5),
    ({'replied': 1, 'topic_changed': 1}, .4),
    ({'explicit': 1, 'replied': 1, 'objected': 1}, (.4 + .25 * .5) / .65 - .5),
    ({'explicit': -1, 'replied': 1, 'laughed': 1, 'react_positive': 1}, -.4),
])
def test_reward_of_his_discord_reactions(store, clock, channel, values, expected):
    lid = line(store, clock, channel=channel)
    signals(store, lid, clock.now(), **values)
    result = reward(store.conn, lid, now=clock.now())
    assert result.value == pytest.approx(expected)
    assert result.parts['quality_measured'] == 0  # Discord borrows no game quality screen
    assert all(type(v) in (int, float) for v in result.parts.values())


def test_a_removed_emoji_is_changed_evidence(context, store, clock, tmp_path):  # noqa: F811
    enable(tmp_path)
    lid = line(store, clock, channel='dm:101', ts=clock.now() - 300)
    signals(store, lid, clock.now() - 200, react_positive=1, replied=1)
    async def go():
        async with context(learn_enabled=True) as (ctx, _):
            await night.stage_rewards(ctx)
        first = store.conn.execute("SELECT value,revision FROM talk_reward").fetchone()
        with store.transaction():
            S.drop(store.conn, lid, 'react_positive')
        await clock.advance(86400, step=86400)
        async with context(learn_enabled=True, night='2026-10-30') as (ctx, _):
            await night.stage_rewards(ctx)
        return tuple(first), tuple(store.conn.execute("SELECT value,revision FROM talk_reward").fetchone())
    first, second = asyncio.run(go())
    assert first[0] > second[0] == .5 and second[1] == first[1] + 1


def test_night_limit_grows_with_measured_cost_and_stays_bounded(store, clock, monkeypatch):
    assert night.night_limit(store.conn) == night.PER_NIGHT
    with store.transaction():
        store.conn.execute("INSERT INTO reward_timing VALUES ('2026-10-28','lexical',400,20.0)")
        store.conn.execute("INSERT INTO reward_timing VALUES ('2026-10-28','embedding',100,30.0)")
    assert night.night_limit(store.conn) == night.PER_NIGHT * night.BACKLOG_FACTOR  # 0.1 s a unit
    with store.transaction():
        store.conn.execute("UPDATE reward_timing SET seconds=600 WHERE mode='embedding'")
    assert night.night_limit(store.conn) == int(night.BUDGET_S / (620 / 500))
    with store.transaction():
        store.conn.execute("UPDATE reward_timing SET seconds=60000 WHERE mode='embedding'")
    assert night.night_limit(store.conn) == night.PER_NIGHT
    assert night.night_limit(sqlite3.connect(':memory:')) == night.PER_NIGHT


def test_a_backlog_drains_and_answered_lines_go_first(context, store, clock, tmp_path, monkeypatch):  # noqa: F811
    enable(tmp_path)
    monkeypatch.setattr(night, 'PER_NIGHT', 3)
    ids = [line(store, clock, channel='discord:general', ts=clock.now() - 5000 + i) for i in range(30)]
    signals(store, ids[0], clock.now() - 400, replied=1)  # the oldest line is the one he answered
    async def go():
        async with context(learn_enabled=True) as (ctx, _):
            await night.stage_rewards(ctx)
        first = [r[0] for r in store.conn.execute("SELECT line_id FROM talk_reward ORDER BY line_id")]
        assert first == [ids[0], *ids[-2:]]  # the unmeasured first night keeps the old cap
        await clock.advance(86400, step=86400)
        async with context(learn_enabled=True, night='2026-10-30') as (ctx, _):
            await night.stage_rewards(ctx)
        return store.conn.execute("SELECT count(*) FROM talk_reward").fetchone()[0]
    assert asyncio.run(go()) == 3 + 3 * night.BACKLOG_FACTOR
