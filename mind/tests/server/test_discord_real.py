"""The guild Discord reads like people, not staged turn-taking (2026-11-15 audit, findings 3
and 5): turn ids that never collide across runs, no member answering her own message, short
messages and quick bursts. Invented text only."""
import asyncio
from dataclasses import replace
import random

import pytest

from conftest import HUMAN, run
from engine_rig import engine_clock, make_rig
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.server import engine as se
from leeroy_mind.server.seeds import Seed
from leeroy_mind.talk import discord_length
from leeroy_mind.talk.pipeline import SELF_NAME_HINT, TurnMeta
from leeroy_mind.talk.realism import BURST_GAP, Realism
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import Turn, TurnKind
from server.test_engine import app, finished  # noqa: F401  (the fixture, with a fake speak)


def converse(app):
    async def go():
        await app.server.engine.tick(app.clock.now())
        assert app.server.engine.live()
        await finished(app.server.engine)
    run(go())
    return app.store.conn.execute('SELECT id,author,reply_to,turn,ts FROM discord_message '
                                  "WHERE origin='mind' ORDER BY id").fetchall()


# -- 1. turn ids -----------------------------------------------------------------------------------

def test_turn_ids_never_repeat_across_runs_of_the_mind(store, tmp_path):
    """Ids restarted at t1 each run, and discord_message.turn kept them: a lookup by id found
    an earlier run's messages too."""
    first = make_rig(store, engine_clock(), tmp_path)
    ids = [first.engine._new_id() for _ in range(50)]
    first.close()
    again = make_rig(store, engine_clock(), tmp_path)  # the mind started again
    later = [again.engine._new_id() for _ in range(50)]
    again.close()
    assert len(set(ids)) == 50 and not set(ids) & set(later)
    assert all(i.startswith('t') and len(i) <= 64 for i in ids + later)  # bandit attempt ids


def test_an_earlier_runs_messages_never_join_the_thread(app):
    """8% of replies answered a message over an hour old: the engine read its turn's messages
    back by turn id, and a restarted run reused the ids. Now the turn's own messages are what
    it stored (TurnMeta.discord_messages), in its channel, since the conversation started."""
    app.server.engine.rng.random = lambda: .9  # an earlier parent, 30% of replies
    stale = [dstore.post(app.store, channel='general', author=104, origin='mind',
                         text='An old invented post', turn=f'test-{i}', now=app.clock.now()-86400)
             for i in range(1, 7)]
    rows = converse(app)
    mine = [r for r in rows if r['id'] not in stale]
    assert len(mine) == 5
    # the collision the old lookup walked into: two messages under each id
    assert app.store.conn.execute("SELECT count(*) FROM discord_message WHERE turn='test-2'").fetchone()[0] == 2
    ids = {r['id'] for r in mine}
    for turn, meta in app.turns:
        assert not set(meta.server_thread) & set(stale)
        assert meta.discord.reply_to is None or meta.discord.reply_to in ids
    assert all(r['reply_to'] in ids for r in mine[1:])


def test_the_turns_own_messages_are_recorded_on_its_meta(store, tmp_path):
    rig = make_rig(store, engine_clock(), tmp_path, reply="sounds good, see you there")
    meta = TurnMeta(rig.engine._new_id(), why='test')
    turn = Turn(kind=TurnKind.CONTINUE, speaker=101, channel='discord:general', addressee=103,
                discord_draw=.99)

    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(60)
        return task.result()
    assert rig.sync(go())
    [row] = store.conn.execute("SELECT id,turn FROM discord_message").fetchall()
    assert meta.discord_messages == [row['id']] and row['turn'] == meta.turn_id
    rig.close()


# -- 2. nobody answers herself ---------------------------------------------------------------------

@pytest.mark.parametrize('cast', [[101, 102], [101, 102, 103]])
def test_no_member_replies_to_her_own_message(app, monkeypatch, cast):
    """An earlier parent was drawn among all the thread's messages, and the speaker only had to
    differ from the last one: 97 replies answered their own author ("mejith being a corporate
    strategist now", posted by her)."""
    engine = app.server.engine
    engine.rng.random = lambda: .9  # always an earlier parent ...
    engine.rng.choice = lambda seq: seq[0]  # ... the opener's
    monkeypatch.setattr(se.rhythm, 'choose_cast', lambda *a, **kw: list(cast))
    monkeypatch.setattr(se.rhythm, 'length', lambda rng: 6)
    rows = converse(app)
    author = {r['id']: r['author'] for r in rows}
    assert len(rows) == 6
    assert all(author[r['reply_to']] != r['author'] for r in rows if r['reply_to'])
    assert all(t.addressee != t.speaker for t, m in app.turns)
    if len(cast) == 3:  # the opener's message is still answered, by someone else
        assert sum(r['reply_to'] == rows[0]['id'] for r in rows) > 1


def test_the_seed_subject_says_it_as_herself(app, monkeypatch):
    """A life beat reads in the third person ("Mejith stood at the sink..."): its owner is told
    it is about her, and so is anyone answering about herself; the others are not."""
    app.observed.seeds = [Seed('life:1', 'life', ('general',), 'Invented beat about a kettle', (101,))]
    monkeypatch.setattr(se.rhythm, 'choose_cast', lambda *a, **kw: [101, 102])
    monkeypatch.setattr(se.rhythm, 'length', lambda rng: 4)
    converse(app)
    assert [t.speaker for t, m in app.turns] == [101, 102, 101, 102]
    for turn, meta in app.turns:
        assert (se.ABOUT_YOU in turn.instruction) is (turn.speaker == 101)


def test_a_discord_line_naming_its_own_speaker_is_asked_again(store, tmp_path):
    rig = make_rig(store, engine_clock(), tmp_path)
    rig.script('Belra is so done with raids this week', "i'm so done with raids this week")
    turn = Turn(kind=TurnKind.CONTINUE, speaker=102, channel='discord:general', addressee=103,
                discord_draw=.99)
    assert rig.speak_turn(turn, seconds=90)
    [row] = store.conn.execute('SELECT text FROM discord_message').fetchall()
    assert 'belra' not in row['text'].casefold()
    calls = rig.chat_calls()
    assert len(calls) == 2
    assert SELF_NAME_HINT.format(name='Belra') in calls[1]['messages'][-1]['content']
    rig.close()


def test_a_line_still_naming_its_speaker_is_dropped_and_the_game_is_untouched(store, tmp_path):
    rig = make_rig(store, engine_clock(), tmp_path)
    rig.script('Belra is so done with raids', 'Belra says she is done with raids')
    turn = Turn(kind=TurnKind.CONTINUE, speaker=102, channel='discord:general', addressee=103,
                discord_draw=.99)
    assert not rig.speak_turn(turn, seconds=90)
    assert not store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]
    assert store.conn.execute("SELECT dropped FROM utterance").fetchone()[0] == 'tells'
    rig.script('Belra is so done with raids')  # game chat keeps its own rules
    assert rig.speak_turn(Turn(kind=TurnKind.CONTINUE, speaker=102, channel='guild', addressee=103), seconds=60)
    rig.close()


def test_an_everyday_word_or_a_running_joke_is_not_her_naming_herself(store, tmp_path):
    from leeroy_mind.talk.pipeline import _names_self
    assert _names_self('Belra is so done with raids', 'Belra')
    assert _names_self('honestly @Belra would never', 'Belra')
    assert not _names_self('the belra in me says no', 'Belra')  # lower case: the ordinary word
    assert not _names_self('Belrae and Belras are not her name', 'Belra')  # whole words only
    assert not _names_self('Dawn raid tomorrow, who is in?', 'Dawn')  # an everyday word
    assert not _names_self('the Belra Bridge strikes again', 'Belra', labels=('Belra Bridge',))
    assert _names_self('the Belra Bridge strikes again, Belra says hi', 'Belra', labels=('Belra Bridge',))
    rig = make_rig(store, engine_clock(), tmp_path)
    now = rig.clock.now()
    store.conn.execute("INSERT INTO lexicon(kind,term,target,origin_ref,originator,born,last_used) "
                       "VALUES ('joke','the Belra Bridge',102,'invented',101,?,?)", (now, now))
    rig.script('the Belra Bridge is open for business lol')
    turn = Turn(kind=TurnKind.CONTINUE, speaker=102, channel='discord:general', addressee=103,
                discord_draw=.99)
    assert rig.speak_turn(turn, seconds=90)
    assert len(rig.chat_calls()) == 1  # no retry for the joke's own label
    rig.close()


@pytest.mark.parametrize('channel', ['discord:general', 'gdm:7'])
def test_an_answer_to_him_never_draws_only_a_few_words(store, tmp_path, channel):
    """The few-words band is for chat among them. An answer to his channel or group-DM post
    (D1, a selfie request) keeps the opener's mix: it owes him something real."""
    rig = make_rig(store, engine_clock(), tmp_path)
    rig.engine._length_rng = random.Random(4)
    to_him, among = [], []
    for _ in range(400):
        to_him.append(rig.engine._discord_words(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101,
                                                     channel=channel, addressee=HUMAN)))
        among.append(rig.engine._discord_words(Turn(kind=TurnKind.CONTINUE, speaker=101,
                                                    channel=channel, addressee=103)))
    assert discord_length.SHORT_WORDS not in to_him
    assert {20, 45, 90} == set(to_him)
    assert discord_length.SHORT_WORDS in among
    rig.close()


def test_an_answer_owed_to_him_is_never_held_up_by_her_name(store, tmp_path):
    rig = make_rig(store, engine_clock(), tmp_path)
    rig.script('Belra reporting in, of course i will be there')
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why='discord:reply')
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel='dm:102', to_guid=HUMAN,
                addressee=HUMAN)

    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(90)
        return task.result()
    assert rig.sync(go())
    assert len(rig.chat_calls()) == 1
    rig.close()


# -- 4. short messages and bursts ------------------------------------------------------------------

class Zero(random.Random):
    def random(self):
        return 0.0


@pytest.mark.parametrize('realism', [True, False])
def test_a_reply_is_drawn_as_a_burst_only_with_realism(app, monkeypatch, realism):
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, realism=realism))
    app.server.engine.shape_rng = Zero()
    converse(app)
    (opener, first), *replies = app.turns
    assert not first.burst and first.single_line
    assert discord_length.band(90, opener.discord_draw) == 20  # never only a few words
    assert replies
    for turn, meta in replies:
        assert meta.burst is realism and meta.single_line is not realism
        if realism:  # a couple of sentences, to split
            assert discord_length.band(90, turn.discord_draw) == 45
        else:
            assert turn.discord_draw is None


def test_bursts_are_occasional():
    rng = random.Random(3)
    share = sum(rng.random() < se.BURST_SHARE for _ in range(10000)) / 10000
    assert .08 < share < .16


SPLIT = 'wait what. they killed it already? no way'


def burst_plan(rig, meta, text=SPLIT, channel='discord:general'):
    e = rig.engine
    turn = Turn(kind=TurnKind.CONTINUE, speaker=101, channel=channel, deadline=10**12)
    return e.realism.plan(e, turn, meta, e.personas.get(101), [Styled(text, text)], [2])


def test_a_drawn_burst_splits_a_short_thought(store, tmp_path):
    rig = make_rig(store, engine_clock(), tmp_path)
    rig.engine.cfg = replace(rig.cfg, talk=replace(rig.cfg.talk, realism=True))
    rig.engine.realism = Realism()
    plain = burst_plan(rig, TurnMeta('t1'))  # 8 words: no natural burst
    assert not plain.burst and len(plain.lines) == 1
    drawn = burst_plan(rig, TurnMeta('t2', burst=True))
    assert drawn.burst and [s.plain for s in drawn.lines] == ['wait what.', 'they killed it already?', 'no way']
    assert all(.5 <= d for d in drawn.delays) and sum(drawn.delays) <= 25 + 2
    assert not burst_plan(rig, TurnMeta('t3', burst=True)).burst  # once per BURST_GAP
    rig.sync(rig.advance(BURST_GAP + 1))
    assert burst_plan(rig, TurnMeta('t4', burst=True)).burst
    single = burst_plan(rig, TurnMeta('t5', burst=True, single_line=True))
    assert not single.burst  # a single line is never split
    rig.close()


def test_a_bursts_follow_ups_are_plain_messages_of_the_same_turn(store, tmp_path, monkeypatch):
    rig = make_rig(store, engine_clock(), tmp_path)
    e = rig.engine
    e.cfg = replace(rig.cfg, talk=replace(rig.cfg.talk, realism=True))
    monkeypatch.setattr(e, '_style', lambda *a, **kw: [Styled(SPLIT, SPLIT)])
    parent = dstore.post(store, channel='general', author=HUMAN, origin='human',
                         text='the drake is down', now=rig.clock.now())
    meta = TurnMeta(e._new_id(), why='test', burst=True, discord=DiscordTarget(parent, None))
    turn = Turn(kind=TurnKind.CONTINUE, speaker=101, channel='discord:general', addressee=103,
                discord_draw=.6)

    async def go():
        task = asyncio.create_task(e._speak(turn, meta))
        await rig.advance(90)
        return task.result()
    said = rig.sync(go())
    rows = store.conn.execute("SELECT id,reply_to,turn,ts FROM discord_message WHERE origin='mind' "
                              'ORDER BY id').fetchall()
    assert len(said) == len(rows) == 3
    assert [r['reply_to'] for r in rows] == [parent, None, None]
    assert {r['turn'] for r in rows} == {meta.turn_id}
    assert meta.discord_messages == [r['id'] for r in rows]
    assert all(0 < b['ts'] - a['ts'] <= 15 for a, b in zip(rows, rows[1:]))  # seconds apart
    rig.close()
