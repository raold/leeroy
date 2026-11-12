import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from conftest import make_persona, run
from leeroy_mind.discord.presence import Presence, status_text, STATUS_PHRASES
from leeroy_mind.server.state import PLAY_KEY, refresh_play, state_of, tie_kind
from selfies.fakes import mind_app


def test_status_is_generic_deterministic_and_none_offline():
    p = make_persona(101, 'Arkon')
    local = datetime(2026, 11, 12, 19)
    offline = Presence('offline', False, 0)
    assert status_text(p, local, offline, rng_seed=0) is None
    states = [status_text(p, local, Presence('online', False, 0), rng_seed=n) for n in range(50)]
    assert any(s is None for s in states) and any(s is not None for s in states)
    assert set(states) <= {None, *STATUS_PHRASES['evening']}
    assert states == [status_text(p, local, Presence('online', False, 0), rng_seed=n) for n in range(50)]


@pytest.mark.parametrize('hour,block', [(10, 'work'), (12, 'work'), (17, 'commute'), (19, 'evening'), (23, 'late')])
def test_status_follows_the_block(hour, block):
    p = make_persona(101, 'Arkon')
    values = {status_text(p, datetime(2026, 11, 12, hour), Presence('idle', False, 0), rng_seed=n) for n in range(50)}
    assert values <= {None, *STATUS_PHRASES[block]}
    if hour != 12:
        assert 'lunch break' not in values


def test_pulled_into_play_forces_playing_presence(tmp_path):
    app = mind_app(tmp_path, toml='[server]\nenabled=true')
    try:
        app.world.human_online = True
        app.world.group = SimpleNamespace(members=(app.human_guid, 101), leader_guid=app.human_guid)
        refresh_play(app, app.clock.now())
        assert 101 in json.loads(app.store.get_meta(PLAY_KEY))['guids']
        assert state_of(app, 101, app.clock.now()).presence.playing
        app.world.human_online = False
        refresh_play(app, app.clock.now())
        assert 101 not in json.loads(app.store.get_meta(PLAY_KEY))['guids']
    finally:
        run(app.stop())


def test_tie_kind_bands_and_feud_and_crush(store):
    for score, label in [(60, 'friend'), (30, 'fond'), (-30, 'rival'), (0, 'neutral')]:
        store.conn.execute('INSERT OR REPLACE INTO relationship(a,b,affinity) VALUES (101,102,?)', (score,))
        assert tie_kind(store.conn, 101, 102) == label
    store.conn.execute("UPDATE relationship SET romance_stage='notices' WHERE a=101 AND b=102")
    assert tie_kind(store.conn, 101, 102) == 'crush'
    assert tie_kind(store.conn, 102, 101) == 'neutral'
    store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (101,102,1,'test','[]')")
    assert tie_kind(store.conn, 101, 102) == 'feud'
    assert tie_kind(store.conn, 102, 101) == 'feud'


@pytest.mark.parametrize('private', [False, True])
def test_state_of_reports_reading_mentions_and_company_without_dm_leaks(tmp_path,private):
    from leeroy_mind.discord.store import post
    app = mind_app(tmp_path,toml='[server]\nenabled=true')
    try:
        now = app.clock.now()
        channel = 'dm:101' if private else 'general'
        post(app.store,channel=channel,author=101,origin='mind',text='Hello there',now=now-100)
        for destination,author,text in [(channel,102,'Hey @Arkon'),('officer-chat',104,'Private @Arkon'),
                                         ('dm:102',102,'Secret @Arkon'),('general',103,'@Arkonson')]:
            post(app.store,channel=destination,author=author,origin='mind',text=text,now=now-20)
        # The fixture's name is authoritative; never count a partial name.
        member = app.world.discord_members()[101]
        post(app.store,channel=channel,author=app.human_guid,origin='human',
             text='Hi @'+member.name,now=now-10)
        state = state_of(app,101,now)
        assert state.reading == channel
        assert state.last_said == (channel,now-100,app.human_guid if private else 0)
        assert state.with_her == (() if private else (102,103))
        assert state.unread_mentions == (2 if member.name == 'Arkon' else 1)
        assert state_of(app,101,now+1801).reading is None
    finally:
        run(app.stop())


def test_play_override_expires_and_server_off_preserves_console_payload(tmp_path):
    from leeroy_mind.console.discordview import discord_view
    from leeroy_mind.server.state import read_play
    app = mind_app(tmp_path,toml='[server]\nenabled=true')
    try:
        now = app.clock.now()
        app.store.set_meta(PLAY_KEY,json.dumps({'guids':[101],'at':now}))
        off = discord_view(app.store.conn,owner_now=app.clock.local(),trial_rank=5)
        on = discord_view(app.store.conn,owner_now=app.clock.local(),trial_rank=5,server=True)
        assert all('status' not in m for m in off['members'])
        assert next(m for m in on['members'] if m['guid']==101)['presence']['playing']
        assert read_play(app.store.conn,now+180) == frozenset({101})
        assert read_play(app.store.conn,now+181) == frozenset()
        assert read_play(app.store.conn,now-1) == frozenset()
        assert discord_view(app.store.conn,owner_now=app.clock.local(),trial_rank=5,server=False) == off
    finally:
        run(app.stop())


def test_officer_company_cannot_reach_a_public_here_card(tmp_path):
    from leeroy_mind.discord.store import post
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    app = mind_app(tmp_path,toml='[server]\nenabled=true\n[talk]\naware=true')
    try:
        now = app.clock.now()
        for author in (102,104):
            post(app.store,channel='officer-chat',author=author,origin='mind',text='Private meeting',now=now-5)
        assert state_of(app,102,now).with_her == ()
        block = app.engine.live_review.block(Turn(TurnKind.OPEN,102,'discord:general'),TurnMeta('test'),close=False)
        assert 'Dunna' not in block and 'Private meeting' not in block
    finally:
        run(app.stop())


@pytest.mark.parametrize('phase', ['called','forming','assembling','running','ended'])
def test_members_in_play_covers_raid_night_and_offline_raid(phase):
    from leeroy_mind.guild.service import GuildLife
    from leeroy_mind.guild.raid_night import State
    guild = SimpleNamespace(ports=SimpleNamespace(human_guid=1),
        raid_night=SimpleNamespace(state=State(phase),run=SimpleNamespace(plan=SimpleNamespace(members=(1,101,102))),
                                   _forming_members=frozenset({103})),
        offline=SimpleNamespace(active=True,_owned={'plan':{'members':[104,105]}}))
    assert GuildLife.members_in_play(guild) == frozenset({104,105} if phase=='ended' else {101,102,103,104,105})
