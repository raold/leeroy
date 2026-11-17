"""His reactions in the guild Discord as numeric observations (signals/discord.py, capture.py):
a DM, a group DM and a channel reply. Scratch databases and invented lines only."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from leeroy_mind.protocol import Chat
from leeroy_mind.signals import discord as D
from leeroy_mind.signals import store as S
from leeroy_mind.signals.attach import Signals

from .test_signals import line, observe, saved, worker

DM, GROUP, CHANNEL = 'dm:101', 'gdm:7', 'discord:general'
PLACES = pytest.mark.parametrize('place', [DM, GROUP, CHANNEL])


def hers(store, clock, place, guid=101, text='the invented bakery burned my croissants again'):
    return line(store, clock, guid, place, HUMAN if place.startswith('dm:') else 0, text)


def his(store, clock, place, text, to=101):
    return line(store, clock, HUMAN, place, to if place.startswith('dm:') else 0, text)


def kinds(store, lid):
    return {r[2]: r[3] for r in saved(store) if r[0] == lid}


async def exchange(c, store, clock, place, text, *, wait=60, reply=0, media=False, voice=False,
                   addressed=True, hers_text=None):
    """Her addressed line, then his message `wait` seconds later; her line's id."""
    lid = hers(store, clock, place) if hers_text is None else hers(store, clock, place, text=hers_text)
    observe(c, clock, 'sent', (lid,), (addressed, False, True))
    await clock.advance(wait, step=3600)
    answer = his(store, clock, place, text)
    observe(c, clock, 'discord_owner', (answer, lid if reply else 0), (media, voice))
    await c.flush()
    return lid


@PLACES
def test_his_next_message_is_a_reply_with_normalised_latency_and_length(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            lid = await exchange(c, store, clock, place, 'x' * 50, wait=450)
            assert kinds(store, lid) == {'replied': 1., 'reply_latency_s': 450 / D.WINDOW_S,
                                         'reply_len': 50 / S.REPLY_LEN_SCALE}
    asyncio.run(go())


@PLACES
@pytest.mark.parametrize('text,expected,absent', [
    ('how long did the croissants take though?', {'asked_back', 'topic_kept'}, {'objected', 'topic_changed'}),
    ('lmao the whole tray', {'laughed'}, {'objected'}),
    ('thank you, you are the best', {'affection'}, {'objected', 'laughed'}),
    ('those croissants never stood a chance', {'topic_kept'}, {'asked_back', 'topic_changed'}),
    ('what?', {'objected'}, {'asked_back', 'affection'}),
    ('that makes no sense', {'objected'}, {'asked_back'}),
    ('you already said that', {'objected'}, {'topic_kept'}),
    ('stop', {'objected'}, {'laughed'}),
    ('i could not stop grinning all morning', set(), {'objected'}),
])
def test_what_his_reply_says(store, clock, place, text, expected, absent):
    async def go():
        async with worker(store, clock) as c:
            got = set(kinds(store, await exchange(c, store, clock, place, text)))
            assert expected <= got and not absent & got
            assert {'replied', 'reply_latency_s', 'reply_len'} <= got
    asyncio.run(go())


@PLACES
def test_her_question_met_with_his_unrelated_question_is_a_changed_topic(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            asked = 'did the landlord ever answer about the invented boiler?'
            changed = await exchange(c, store, clock, place, 'anyway which flask should the hunters bring tonight?',
                                     hers_text=asked)
            assert kinds(store, changed)['topic_changed'] == 1 and 'topic_kept' not in kinds(store, changed)
            # An answer in his own words shares no word with her question and is no change of topic.
            await clock.advance(D.WINDOW_S + 1, step=3600)
            answered = await exchange(c, store, clock, place, 'nope, complete silence from them so far', hers_text=asked)
            assert not {'topic_changed', 'objected'} & set(kinds(store, answered))
    asyncio.run(go())


@PLACES
def test_a_photo_or_voice_note_is_media_and_a_voice_note_reads_no_text(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            photo = await exchange(c, store, clock, place, 'look at this', media=True)
            assert kinds(store, photo)['sent_media'] == 1 and 'reply_len' in kinds(store, photo)
            await clock.advance(D.WINDOW_S + 1, step=3600)
            voice = await exchange(c, store, clock, place, '(voice message) what?', media=True, voice=True)
            assert kinds(store, voice) == {'replied': 1., 'reply_latency_s': 60 / D.WINDOW_S, 'sent_media': 1.}
    asyncio.run(go())


@PLACES
def test_the_conversation_going_on_counts_turns_for_her_earlier_lines(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            first = await exchange(c, store, clock, place, 'go on')
            second = await exchange(c, store, clock, place, 'and then')
            third = await exchange(c, store, clock, place, 'no way')
            assert 'thread_continued' not in kinds(store, first) and 'turns_after' not in kinds(store, third)
            assert kinds(store, second)['thread_continued'] == 1 == kinds(store, third)['thread_continued']
            assert kinds(store, first)['turns_after'] == pytest.approx(2 / D.TURNS_SCALE)
            assert kinds(store, second)['turns_after'] == pytest.approx(1 / D.TURNS_SCALE)
            # A new conversation after the window starts its own count.
            await clock.advance(D.WINDOW_S + 1, step=3600)
            later = await exchange(c, store, clock, place, 'back again')
            assert 'thread_continued' not in kinds(store, later)
            assert kinds(store, first)['turns_after'] == pytest.approx(2 / D.TURNS_SCALE)
    asyncio.run(go())


@PLACES
def test_his_emoji_is_a_signal_and_removing_it_takes_it_back(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            lid = hers(store, clock, place)
            observe(c, clock, 'sent', (lid,), (False, False, True))  # no reply owed: an emoji still counts
            observe(c, clock, 'reaction', (lid,), (True, False))
            await c.flush()
            assert kinds(store, lid) == {'react_positive': 1.}
            observe(c, clock, 'reaction', (lid,), (False, True))
            await c.flush()
            assert kinds(store, lid) == {'react_negative': 1.}
            observe(c, clock, 'reaction', (lid,), (False, False))
            await c.flush()
            assert kinds(store, lid) == {}
            mine = his(store, clock, place, 'my own invented post')
            observe(c, clock, 'reaction', (mine,), (True, False))
            await c.flush()
            assert saved(store) == []
    asyncio.run(go())


def test_emoji_classes():
    assert D.reaction_kind('❤️') == D.reaction_kind('❤') == D.reaction_kind('😂') == 'react_positive'
    assert D.reaction_kind('👎') == D.reaction_kind('🙄') == 'react_negative'
    assert D.reaction_kind(':tank:') is None and D.reaction_kind('🧀') is None and D.reaction_kind(None) is None


@PLACES
def test_silence_and_leaving_are_never_negative(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            lid = hers(store, clock, place)
            observe(c, clock, 'sent', (lid,), (True, False, True))
            observe(c, clock, 'presence', values=(True,))
            observe(c, clock, 'combat', values=(False,))
            observe(c, clock, 'presence', values=(False,))  # he logs out of WoW and never answers
            await clock.advance(D.WINDOW_S + 5, step=3600)
            await c.flush()
            assert saved(store) == [] and not c.dpending
            late = his(store, clock, place, 'sorry, fell asleep')
            observe(c, clock, 'discord_owner', (late, 0), (False, False))
            await c.flush()
            assert saved(store) == []
    asyncio.run(go())


@PLACES
def test_his_wow_logout_keeps_a_discord_line_waiting(store, clock, place):
    async def go():
        async with worker(store, clock) as c:
            lid = hers(store, clock, place)
            observe(c, clock, 'sent', (lid,), (True, False, True))
            for kind, values in (('presence', (True,)), ('group', ()), ('afk', (True,)), ('presence', (False,))):
                observe(c, clock, kind, values=values)
            await clock.advance(900, step=3600)
            observe(c, clock, 'discord_owner', (his(store, clock, place, 'here now'), 0), (False, False))
            await c.flush()
            assert kinds(store, lid)['reply_latency_s'] == .5
    asyncio.run(go())


def test_two_messages_running_credit_her_line_once_and_his_first_message_nothing(store, clock):
    async def go():
        async with worker(store, clock) as c:
            opening = his(store, clock, DM, 'hey you')
            observe(c, clock, 'discord_owner', (opening, 0), (False, False))
            lid = await exchange(c, store, clock, DM, 'first')
            before = saved(store)
            observe(c, clock, 'discord_owner', (his(store, clock, DM, 'second, haha'), 0), (False, False))
            await c.flush()
            assert saved(store) == before and set(kinds(store, lid)) == {'replied', 'reply_latency_s', 'reply_len'}
    asyncio.run(go())


def test_a_reply_by_hand_credits_that_line_even_unaddressed(store, clock):
    async def go():
        async with worker(store, clock) as c:
            older = hers(store, clock, CHANNEL, 102)
            observe(c, clock, 'sent', (older,), (False, False, True))
            newer = hers(store, clock, CHANNEL, 101)
            observe(c, clock, 'sent', (newer,), (True, False, True))
            await clock.advance(30, step=3600)
            observe(c, clock, 'discord_owner', (his(store, clock, CHANNEL, 'lol fair'), older), (False, False))
            await c.flush()
            assert set(kinds(store, older)) == {'replied', 'reply_latency_s', 'reply_len', 'laughed'}
            assert kinds(store, newer) == {}
    asyncio.run(go())


def test_unaddressed_channel_talk_is_not_waiting_for_him(store, clock):
    async def go():
        async with worker(store, clock) as c:
            lid = await exchange(c, store, clock, CHANNEL, 'unrelated post of mine', addressed=False)
            assert kinds(store, lid) == {}
    asyncio.run(go())


@pytest.mark.parametrize('place,to', [('dm:102', 102), (CHANNEL, 0), (GROUP, 0), ('whisper', 101)])
def test_a_dm_line_is_credited_only_by_his_message_in_that_dm(store, clock, place, to):
    async def go():
        async with worker(store, clock) as c:
            lid = hers(store, clock, DM)
            observe(c, clock, 'sent', (lid,), (True, False, True))
            other = hers(store, clock, CHANNEL, 103)
            observe(c, clock, 'sent', (other,), (True, False, True))
            await clock.advance(20, step=3600)
            elsewhere = line(store, clock, HUMAN, place, to, 'haha thank you')
            observe(c, clock, 'discord_owner', (elsewhere, 0), (False, False))
            observe(c, clock, 'owner', (elsewhere,), (.1, False))
            await c.flush()
            assert kinds(store, lid) == {}
            assert (kinds(store, other) != {}) == (place == CHANNEL)
    asyncio.run(go())


def test_a_dm_between_two_members_never_counts_as_his(store, clock):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock, 101, 'dm:102', 102, 'an invented aside')
            observe(c, clock, 'sent', (lid,), (True, False, True))
            observe(c, clock, 'reaction', (lid,), (True, False))
            await c.flush()
            assert saved(store) == [] and not c.dpending and not c.drecent
    asyncio.run(go())


@PLACES
def test_rows_hold_numbers_only(store, clock, place):
    secret_hers, secret_his = 'INVENTEDSECRETCROISSANT keep this between us', 'INVENTEDSECRETREPLY haha what happened?'
    async def go():
        async with worker(store, clock) as c:
            lid = await exchange(c, store, clock, place, secret_his, hers_text=secret_hers, media=True)
            observe(c, clock, 'reaction', (lid,), (True, False))
            await c.flush()
    asyncio.run(go())
    rows = store.conn.execute('SELECT * FROM talk_signal').fetchall()
    assert len(rows) >= 5
    for row in rows:
        id_, line_id, act_id, guid, kind, value, ts, source = tuple(row)
        assert kind in S.KINDS and source == 'implicit' and act_id is None
        assert all(type(v) in (int, float) for v in (id_, line_id, guid, value, ts))
    dump = repr([tuple(r) for r in rows]) + repr(store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='talk_signal'").fetchone()[0])
    assert 'INVENTED' not in dump and 'croissant' not in dump.casefold()


def test_old_and_new_kinds_fit_the_table_and_unknown_ones_do_not(store, clock):
    lid = hers(store, clock, DM)
    with store.transaction():
        for kind in S.IMPLICIT:
            assert S.put(store.conn, lid, 101, kind, 1, clock.now()) == 1
        assert S.upsert(store.conn, lid, 101, 'turns_after', .4, clock.now() + 5) == 1
        assert S.drop(store.conn, lid, 'react_positive') == 1
        with pytest.raises(ValueError):
            S.put(store.conn, lid, 101, 'his_words', 1, clock.now())
    assert kinds(store, lid)['turns_after'] == .4 and 'react_positive' not in kinds(store, lid)
    assert set(S.DISCORD) <= set(S.IMPLICIT) <= set(S.KINDS)


@pytest.mark.parametrize('channel,to,owner,visible', [
    ('dm:101', HUMAN, HUMAN, True), ('dm:101', 102, HUMAN, False), ('gdm:3', 0, HUMAN, True),
    ('discord:general', 0, HUMAN, True), ('discord:officer-chat', 0, HUMAN, True), ('dm:101', HUMAN, 0, False)])
def test_discord_visibility_never_asks_his_game_presence(channel, to, owner, visible):
    world = SimpleNamespace(human_online=False, in_group=lambda g: False, is_officer=lambda g: False)
    app = SimpleNamespace(world=world, human_guid=owner, clock=SimpleNamespace(now=lambda: 10.),
                          engine=SimpleNamespace(comms=None))
    seen = []
    signals = Signals.__new__(Signals)
    signals.app, signals.owner_location = app, None
    signals.capture = SimpleNamespace(offer=lambda observation, result=None: seen.append(observation) or True)
    signals.sent(5, Chat(0, channel, 101, 'Arkon', to, 'invented', 0, 0), addressed=True, heard=True)
    assert seen[0].kind == 'sent' and seen[0].values == (True, False, visible)
    signals.his_discord(9, reply_line=5, media=True)
    signals.reacted(5, positive=True, negative=False)
    assert [(o.kind, o.ids, o.values) for o in seen[1:]] == [
        ('discord_owner', (9, 5), (True, False)), ('reaction', (5,), (True, False))]
