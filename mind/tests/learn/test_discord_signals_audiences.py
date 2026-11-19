"""His Discord signals stay inside their audience: a group DM's and an officer room's reactions
credit only that room and teach no lesson elsewhere, and the replay never reads his voice note's
transcript (beside test_a_dm_reaction_is_never_evidence_for_a_public_lesson). Scratch databases,
invented text."""
import json
from types import SimpleNamespace

from conftest import HUMAN, seed_test_cast
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import LINE_KEY
from leeroy_mind.groups import store as groups
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.learn import arms, audience, bandit, coach
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.learn.reward import reward
from leeroy_mind.learn.weights import ensure as weights
from leeroy_mind.protocol import Chat
from leeroy_mind.signals import backfill

ON = LearnCfg(enabled=True, bandit=True)


def message(store, channel, author, text, *, at, reply_to=None, readers=None):
    mind = author != HUMAN
    with store.transaction():
        mid = dstore.post(store, channel=channel, author=author, origin='mind' if mind else 'human',
                          text=text, reply_to=reply_to, now=at)
        talk = channel if channel.startswith(('dm:', 'gdm:')) else 'discord:' + channel
        peer = int(channel[3:]) if channel.startswith('dm:') else 0
        chat = Chat(0, talk, author, 'Invented', (HUMAN if mind else peer) if peer else 0, text, 0, 0)
        lid = store.insert_chat_line(chat, session_id=None, origin='mind' if mind else 'human', ts=at,
                                     audience=readers)
        store.set_meta(LINE_KEY.format(mid), str(lid))
        if mind:
            store.log_utterance(at, author, talk, 'reply_human', text, [], None, None, to_guid=0)
    return mid, lid


def replay(store, clock):
    sink, facts = backfill.plan(store.conn, since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    _, new = backfill.report(store.conn, sink, facts)
    with store.transaction():
        backfill.write(store.conn, new)
    return new


def recipe_and_reward(store, clock, lid, guid, channel_key):
    value = reward(store.conn, lid, now=clock.now()).value
    with store.transaction():
        arms.ensure(store.conn, now=clock.now())
        weights(store.conn, now=clock.now())
        store.conn.execute("INSERT INTO talk_recipe VALUES (?,?,?,?,?,?,?,?,?)",
                           (lid, "test", "0" * 16, "0" * 64, "fake", "send", json.dumps(["length.0.v1"]),
                            "[]", channel_key))
        version = store.conn.execute("SELECT max(version) FROM learn_weights").fetchone()[0]
        store.conn.execute("INSERT INTO talk_reward VALUES (?,?,?,?,?,?,?,?)",
                           (lid, guid, value, 1., 0, "{}", version, clock.now()))
    return value


def credit(store, lid, clock):
    with store.transaction():
        return bandit.update(store.conn, lid, settings=ON, now=clock.now())


PUBLIC_KEYS = [bandit.Context(c).key for c in ('guild', 'discord:general', 'dm:101', 'whisper', 'party')]


def test_a_group_dm_reaction_is_never_evidence_outside_that_group(store, clock):
    seed_test_cast(store)
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    gdm = groups.create(store, human=HUMAN, members=[101, 102], title='Fixture', created_by=HUMAN,
                        now=t - 10, config=GroupsConfig(enabled=True))
    readers = sorted(groups.readers(store.conn, gdm))
    first, his0 = message(store, gdm, HUMAN, 'who saw the invented comet', at=t, readers=readers)
    mid, hers = message(store, gdm, 101, 'me, the invented comet was green', at=t + 30, reply_to=first,
                        readers=readers)
    _, his = message(store, gdm, HUMAN, 'haha green, really?', at=t + 60, readers=readers)
    with store.transaction():
        dstore.react(store, mid, member=HUMAN, emoji='\U0001F44D', now=t + 70)
    new = replay(store, clock)
    assert {'replied', 'react_positive'} <= {kind for line, kind in new if line == hers}
    assert recipe_and_reward(store, clock, hers, 101, bandit.Context(gdm).key) > 0
    # coaching: the group DM supplies no lesson at all
    assert coach.scope(gdm) is None
    ctx = SimpleNamespace(store=store, window=(t - 7200, t + 7200), clock=clock, human=HUMAN)
    assert coach.evidence(ctx, 101) is None
    # arm credit: only in the group's own context; a mislabelled recipe is refused
    assert credit(store, hers, clock)
    assert {r[0] for r in store.conn.execute('SELECT DISTINCT context FROM bandit_credit')} == {
        bandit.Context(gdm).key}
    for key in PUBLIC_KEYS:
        assert bandit.posterior(store.conn, 101, key) == {}
    for key in PUBLIC_KEYS:
        with store.transaction():
            store.conn.execute('DELETE FROM bandit_credit')
            store.conn.execute('UPDATE talk_recipe SET bandit_context=? WHERE line_id=?', (key, hers))
        assert not credit(store, hers, clock), key
    # the lesson gate on the exchange's lines: never in a public room, a pair DM or another's
    refs = audience.evidence(store.conn, [his0, hers, his])
    assert refs

    def see(guid, channel, to=0, officer=False):
        return audience.visible(store.conn, refs, guid=guid, channel=channel, to_guid=to, officer=officer,
                                human_guid=HUMAN)
    for guid, channel, to in ((101, 'guild', 0), (101, 'discord:general', 0), (101, 'party', 0),
                              (101, 'dm:101', HUMAN), (101, 'whisper', HUMAN), (102, 'dm:102', HUMAN),
                              (103, 'guild', 0)):
        assert not see(guid, channel, to), (guid, channel)
    assert not see(101, 'discord:officer-chat', officer=True)


def test_an_officer_channel_reaction_is_never_evidence_outside_the_officers(store, clock):
    seed_test_cast(store)
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    first, his0 = message(store, 'officer-chat', HUMAN, 'invented question about the bank tab', at=t)
    mid, hers = message(store, 'officer-chat', 101, 'the invented bank tab is full of ore', at=t + 30,
                        reply_to=first)
    _, his = message(store, 'officer-chat', HUMAN, 'lol all ore, why?', at=t + 60)
    with store.transaction():
        dstore.react(store, mid, member=HUMAN, emoji='\U0001F602', now=t + 70)
    new = replay(store, clock)
    assert {'replied', 'react_positive'} <= {kind for line, kind in new if line == hers}
    officer_key = bandit.Context('discord:officer-chat').key
    assert recipe_and_reward(store, clock, hers, 101, officer_key) > 0
    assert coach.scope('discord:officer-chat') is None
    ctx = SimpleNamespace(store=store, window=(t - 7200, t + 7200), clock=clock, human=HUMAN)
    assert coach.evidence(ctx, 101) is None
    assert credit(store, hers, clock)
    assert {r[0] for r in store.conn.execute('SELECT DISTINCT context FROM bandit_credit')} == {officer_key}
    for key in PUBLIC_KEYS:
        with store.transaction():
            store.conn.execute('DELETE FROM bandit_credit')
            store.conn.execute('UPDATE talk_recipe SET bandit_context=? WHERE line_id=?', (key, hers))
        assert not credit(store, hers, clock), key
    refs = audience.evidence(store.conn, [his0, hers, his])
    assert refs

    def see(guid, channel, to=0, officer=False):
        return audience.visible(store.conn, refs, guid=guid, channel=channel, to_guid=to, officer=officer,
                                human_guid=HUMAN)
    for guid, channel, to in ((101, 'guild', 0), (101, 'discord:general', 0), (101, 'party', 0),
                              (101, 'dm:101', HUMAN), (102, 'dm:102', HUMAN)):
        assert not see(guid, channel, to), (guid, channel)
    assert not see(102, 'discord:officer-chat', officer=False)  # a non-officer turn there


def test_a_voice_note_of_his_is_never_read_by_the_replay(store, clock):
    """His voice note's chat line holds its transcript once transcribed: the replay never
    selects that line's text, nor the text of the line it answers."""
    store.set_meta('human_guid', str(HUMAN))
    t = clock.now() - 3600
    _, hers = message(store, 'dm:101', 101, 'the invented parrot learned a word', at=t)
    vmid, vline = message(store, 'dm:101', HUMAN, 'SENTINEL transcript words', at=t + 60)
    info = list(store.conn.execute('PRAGMA table_info(discord_voice)'))
    row = {}
    for _, col, kind, notnull, default, _ in info:
        if col == 'message':
            row[col] = vmid
        elif col == 'origin':
            row[col] = 'human'
        elif col == 'transcript_ok':
            row[col] = 1
        elif notnull and default is None:
            row[col] = 0 if any(k in (kind or '').upper() for k in ('INT', 'REAL')) else 'x'
    with store.transaction():
        store.conn.execute(f"INSERT INTO discord_voice({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                           list(row.values()))
    store.conn.commit()
    read = []

    class Spy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, params=()):
            if 'text' in sql and 'chat_line' in sql:
                read.extend(params)
            return self._c.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._c, name)
    sink, _ = backfill.plan(Spy(store.conn), since=clock.now() - 7200, now=clock.now(), owner=HUMAN)
    assert sink.rows[hers, "replied"][1] == 1 and (hers, "sent_media") in sink.rows
    assert vline not in read and hers not in read
