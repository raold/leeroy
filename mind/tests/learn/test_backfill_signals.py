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


def test_replay_matches_live_on_bot_rows_pair_lines_captions_and_voice_notes(store, clock):
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3000
    # a hand reply to a bot's row (no chat line) credits nobody, though a member line waits
    mine, _ = message(store, clock, 'general', HUMAN, 'an invented opening post', at=t - 10)
    _, waiting = message(store, clock, 'general', 101, 'an invented waiting line', reply_to=mine, at=t)
    with store.transaction():
        bot = dstore.post_bot(store, channel='general', bot='signups', text='an invented bot row', now=t + 5)
    message(store, clock, 'general', HUMAN, 'haha noted', reply_to=bot, at=t + 20)
    # her real answer, then a brb/back line: the real answer takes his reply
    _, real = message(store, clock, 'dm:102', 102, 'the invented answer itself', at=t + 100)
    back, pair = message(store, clock, 'dm:102', 102, 'back, sorry', at=t + 110)
    with store.transaction():
        store.conn.execute("UPDATE discord_message SET turn='pair_line:' WHERE id=?", (back,))
    message(store, clock, 'dm:102', HUMAN, 'no worries', at=t + 150)
    # a photo's caption is not an answer that waits
    photo, caption = message(store, clock, 'dm:103', 103, 'an invented caption', at=t + 200)
    with store.transaction():
        store.conn.execute("INSERT INTO discord_attachment(message,kind,path,width,height,bytes) "
                           "VALUES (?,'upload','discord/images/invented.jpg',16,16,1)", (photo,))
    message(store, clock, 'dm:103', HUMAN, 'lol nice', at=t + 230)
    # his transcribed voice note: replied and media, its words unread
    _, asked = message(store, clock, 'dm:104', 104, 'an invented question for him', at=t + 300)
    note, _ = message(store, clock, 'dm:104', HUMAN, 'haha what? thank you', at=t + 330)
    with store.transaction():
        store.conn.execute("INSERT INTO discord_voice VALUES (?,'human','discord/voice/invented.wav',900,10,1)", (note,))
    store.conn.commit()
    sink, _ = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    kinds = lambda lid: {k for (l, k) in sink.rows if l == lid}
    assert kinds(waiting) == set() and kinds(pair) == set() and kinds(caption) == set()
    assert kinds(real) == {'replied', 'reply_latency_s', 'reply_len'}
    assert kinds(asked) == {'replied', 'reply_latency_s', 'sent_media'}


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
    ({'replied': 1, 'turns_after': .2}, .55),   # a further reply never scores below none
    ({'replied': 1, 'turns_after': 1}, .75),
    ({'react_positive': 1}, .5),                # a lone emoji is half a game emote
    ({'react_negative': 1}, -.5),
    ({'replied': 1, 'react_positive': 1}, (.25 * .5 + .15 * .5) / .40),
    ({'replied': 1, 'objected': 1}, .25 * .5 / .65 - .5),
    ({'replied': 1, 'topic_changed': 1}, .5),   # observed, no weight
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


@pytest.mark.parametrize('channel,answered,centre', [
    ('dm:101', 21, .6), ('dm:101', 30, 0.), ('dm:101', 12, 1.), ('discord:general', 1, 1.)])
def test_a_reply_counts_for_how_rare_a_reply_is_in_that_kind_of_room(store, clock, channel, answered, centre):
    earlier = [line(store, clock, channel=channel, ts=clock.now() - 9000 + i) for i in range(30)]
    for lid in earlier[:answered]:
        signals(store, lid, clock.now() - 8000, replied=1)
    other = 'discord:general' if channel.startswith('dm:') else 'dm:101'
    for i in range(30):  # another kind of room never moves this one's rate
        signals(store, line(store, clock, channel=other, ts=clock.now() - 9000 + i), clock.now() - 8000, replied=1)
    bare, laugh, silent, thumb = (line(store, clock, channel=channel) for _ in range(4))
    signals(store, bare, clock.now(), replied=1)
    signals(store, laugh, clock.now(), replied=1, laughed=1)
    signals(store, thumb, clock.now(), explicit=1)
    result = reward(store.conn, bare, now=clock.now())
    assert result.parts['reply_rate'] == pytest.approx(answered / 30) and result.parts['reply_rate_n'] == 30
    assert result.parts['reply_centre'] == pytest.approx(centre)
    # the bare reply's own value against its baseline (1 once earlier replies exist: 0.5)
    assert result.value == pytest.approx(.5 * centre)
    laughed = reward(store.conn, laugh, now=clock.now())  # its own rate: the bare reply is before it
    assert laughed.value == pytest.approx(.75 * laughed.parts['reply_centre'])
    assert laughed.value >= result.value
    assert reward(store.conn, silent, now=clock.now()).value == 0  # silence stays zero, never negative
    assert reward(store.conn, thumb, now=clock.now()).value == 1   # his thumbs are never scaled
    assert all(type(v) in (int, float) for v in result.parts.values())


def test_too_few_earlier_lines_and_game_lines_are_not_centred(store, clock):
    for i in range(10):
        signals(store, line(store, clock, channel='dm:101', ts=clock.now() - 900 + i), clock.now() - 800, replied=1)
    lid = line(store, clock, channel='dm:101')
    signals(store, lid, clock.now(), replied=1)
    assert reward(store.conn, lid, now=clock.now()).parts['reply_centre'] == 1
    for i in range(30):
        signals(store, line(store, clock, channel='whisper', ts=clock.now() - 900 + i), clock.now() - 800, replied=1)
    game = line(store, clock, channel='whisper')
    signals(store, game, clock.now(), replied=1)
    assert 'reply_centre' not in reward(store.conn, game, now=clock.now()).parts


def test_a_removed_emoji_is_changed_evidence(context, store, clock, tmp_path):  # noqa: F811
    enable(tmp_path)
    lid = line(store, clock, channel='dm:101', ts=clock.now() - 300)
    signals(store, lid, clock.now() - 200, react_negative=1, replied=1)
    async def go():
        async with context(learn_enabled=True) as (ctx, _):
            await night.stage_rewards(ctx)
        first = store.conn.execute("SELECT value,revision FROM talk_reward").fetchone()
        with store.transaction():
            S.drop(store.conn, lid, 'react_negative')
        await clock.advance(86400, step=86400)
        async with context(learn_enabled=True, night='2026-10-30') as (ctx, _):
            await night.stage_rewards(ctx)
        return tuple(first), tuple(store.conn.execute("SELECT value,revision FROM talk_reward").fetchone())
    first, second = asyncio.run(go())
    assert first[0] < second[0] == .5 and second[1] == first[1] + 1


def test_night_limit_grows_with_measured_cost_and_stays_bounded(store, clock, monkeypatch):
    assert night.night_limit(store.conn) == night.PER_NIGHT
    with store.transaction():
        store.conn.execute("INSERT INTO reward_timing VALUES ('2026-10-28','lexical',400,20.0)")
        store.conn.execute("INSERT INTO reward_timing VALUES ('2026-10-28','embedding',100,30.0)")
    assert night.night_limit(store.conn) == int(night.BUDGET_S / .3)  # the dearer mode's 0.3 s a unit
    with store.transaction():
        store.conn.execute("UPDATE reward_timing SET seconds=5 WHERE mode='embedding'")
    assert night.night_limit(store.conn) == night.PER_NIGHT * night.BACKLOG_FACTOR  # 0.05 s a unit
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



def test_replay_keeps_a_thread_apart_from_its_channel(store, clock):
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    mine, _ = message(store, clock, 'general', HUMAN, 'who tried the invented recipe', at=t)
    reply, feed = message(store, clock, 'general', 102, 'me, the invented soup', reply_to=mine, at=t + 30)
    thread = dstore.open_thread(store, mine, name='invented', now=t + 40)

    def threaded(author, text, at, reply_to=None):
        mid, lid = message(store, clock, 'general', author, text, reply_to=reply_to, at=at)
        store.conn.execute('UPDATE discord_message SET thread=? WHERE id=?', (thread, mid))
        return mid, lid
    _, inside = threaded(103, 'the invented stew for me', t + 50, reply_to=mine)
    threaded(HUMAN, 'how was the stew', t + 90)
    store.conn.commit()
    sink, _ = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    assert {line for line, _ in sink.rows} == {inside}  # his thread post never credits the feed's line
    message(store, clock, 'general', HUMAN, 'and the soup', at=t + 120)
    sink, _ = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    assert {line for line, _ in sink.rows} == {inside, feed}


def test_replay_reads_a_post_made_while_she_wrote_as_live_does(store, clock):
    """Her answer is stored after a post of his it was not built with (its `discord:built:`
    receipt names only the earlier post): that post is no reply to the answer."""
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    _, opener = message(store, clock, 'dm:101', 101, 'the invented ferry was late again', at=t)
    first, _ = message(store, clock, 'dm:101', HUMAN, 'how late was the ferry', at=t + 60)
    message(store, clock, 'dm:101', HUMAN, 'typed while she writes', at=t + 80)
    answer, late = message(store, clock, 'dm:101', 101, 'an hour, the invented captain overslept',
                           reply_to=first, at=t + 85)
    store.set_meta(f'discord:built:{answer}', str(first))
    store.conn.commit()
    sink, _ = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    assert {line for line, _ in sink.rows} == {opener}
    message(store, clock, 'dm:101', HUMAN, 'poor captain', at=t + 150)
    sink, _ = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    assert sink.rows[late, 'reply_latency_s'][1] == pytest.approx(65 / D.WINDOW_S)
    assert sink.rows[opener, 'reply_latency_s'][1] == pytest.approx(60 / D.WINDOW_S)


def test_a_dm_reaction_is_never_evidence_for_a_public_lesson(store, clock):
    """His reply and emoji in her DM raise that DM line's reward. A lesson coached from the
    exchange carries the DM's lines as its sources, and the lesson gate (learn/audience.py, on
    resolved scopes) lets them into her conversation with him and nowhere else."""
    from leeroy_mind.learn import audience, coach
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    text = 'the invented lighthouse keeper waved back'
    mid, hers = message(store, clock, 'dm:101', 101, text, at=t)
    store.log_utterance(t, 101, 'dm:101', 'reply_human', text, [], None, None, to_guid=HUMAN)
    _, his = message(store, clock, 'dm:101', HUMAN, 'haha did he really wave?', at=t + 60)
    with store.transaction():
        dstore.react(store, mid, member=HUMAN, emoji='\U0001F44D', now=t + 70)
    sink, facts = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    _, new = backfill.report(store.conn, sink, facts)
    with store.transaction():
        backfill.write(store.conn, new)
    assert {'replied', 'laughed', 'react_positive'} <= {kind for line, kind in new if line == hers}
    assert reward(store.conn, hers, now=clock.now()).value > 0
    assert coach.scope('dm:101') == 'whisper' and coach.scope('gdm:7') is None  # never a guild lesson
    refs = audience.evidence(store.conn, [his, hers])
    assert sorted(refs) == sorted([f'chat:{his}', f'chat:{hers}'])
    see = lambda guid, channel, to=0: audience.visible(store.conn, refs, guid=guid, channel=channel, to_guid=to)
    assert see(101, 'dm:101', HUMAN)
    assert not see(101, 'guild') and not see(101, 'discord:general') and not see(101, 'party')
    assert not see(102, 'dm:102', HUMAN) and not see(102, 'guild')
