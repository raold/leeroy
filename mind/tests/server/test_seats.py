from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from conftest import make_persona
from leeroy_mind.discord.presence import Presence
from leeroy_mind.server.seats import away_seats


def test_away_seats_follow_thinks_as_and_only_online_members(monkeypatch):
    monkeypatch.setattr('leeroy_mind.server.seats.VOICE_SHARE', 1)
    personas = {g: make_persona(g, f'Test{g}') for g in range(101, 106)}
    kinds = ['lore', 'collector', 'competitor', 'theorycrafter', 'social']
    personas = {g: replace(p, game=replace(p.game, thinks_as=kinds[g-101])) for g,p in personas.items()}
    states = {g: Presence('online', False, 0) for g in personas}
    now = datetime(2026,11,12,19)
    assert away_seats(personas, states, now, class_rooms={104:'mages'}) == {
        'lore':(101,), 'achievements':(102,), 'pvp':(103,), 'mages':(104,), 'kitchen':(105,)}
    states[101] = Presence('offline',False,0)
    assert 'lore' not in away_seats(personas,states,now)
    assert away_seats(personas,states,now.replace(hour=12)) == {}


def test_no_seats_while_a_voice_session_runs(tmp_path):
    import json
    from conftest import run
    from selfies.fakes import mind_app
    from leeroy_mind.server.seats import refresh_seats, SEATS_KEY
    app = mind_app(tmp_path,toml='[server]\nenabled=true')
    try:
        app.voice = SimpleNamespace(rooms=SimpleNamespace(human_room=lambda: 'guild_hall'))
        refresh_seats(app,app.clock.now())
        assert json.loads(app.store.get_meta(SEATS_KEY))['rooms'] == {}
    finally:
        app.voice = None
        run(app.stop())


def test_seat_snapshot_is_hidden_off_stale_and_in_real_voice(store):
    import json
    from leeroy_mind.server.seats import SEATS_KEY, read_seats
    from leeroy_mind.console.discordview import discord_view
    now = datetime(2026,11,12,19)
    value = {'at':now.timestamp(),'rooms':{'lore':[101]}}
    store.set_meta(SEATS_KEY,json.dumps(value))
    assert read_seats(store.conn,now.timestamp()) == value
    assert read_seats(store.conn,now.timestamp()+901) == {}
    assert 'voice_seats' not in discord_view(store.conn,owner_now=now,trial_rank=4,server=False)
    assert discord_view(store.conn,owner_now=now,trial_rank=4,server=True)['voice_seats'] == value
    assert discord_view(store.conn,owner_now=now,trial_rank=4,server=True)['voice_seats_now'] == now.timestamp()
    store.conn.execute("INSERT INTO voice_presence(guid,room,since,by) VALUES(1,'guild_hall',?,0)",(now.timestamp(),))
    assert read_seats(store.conn,now.timestamp(),human=1) == {}


def test_disabled_attach_registers_no_seat_tick_or_meta(tmp_path):
    from conftest import run
    from selfies.fakes import mind_app
    from leeroy_mind.server.seats import SEATS_KEY
    app = mind_app(tmp_path,toml='[server]\nenabled=false')
    try:
        assert app.server is None
        assert app.store.get_meta(SEATS_KEY) is None
        assert 'seats_tick' not in {factory.__name__ for factory in app._background}
    finally:
        run(app.stop())


def test_evening_roll_is_stable_across_midnight_and_social_boundaries(monkeypatch):
    import leeroy_mind.server.seats as seats
    p = make_persona(105,'Test105')
    p = replace(p,game=replace(p.game,thinks_as='social'))
    online = {105:Presence('online',False,0)}
    monkeypatch.setattr(seats,'VOICE_SHARE',1)
    for hour,room in [(17,'kitchen'),(19,'kitchen'),(20,'guild_hall'),(22,'guild_hall'),(23,'irl'),(0,'irl'),(2,'irl')]:
        assert seats.away_seats({105:p},online,datetime(2026,11,12,hour)) == {room:(105,)}
    for hour in (3,16):
        assert seats.away_seats({105:p},online,datetime(2026,11,12,hour)) == {}
    monkeypatch.setattr(seats,'VOICE_SHARE',.4)
    evening = datetime(2026,11,12,23)
    overnight = datetime(2026,11,13,1)
    assert seats.away_seats({105:p},online,evening) == seats.away_seats({105:p},online,overnight)


def seat_app(store, *, hour=19):
    from conftest import TEST_CAST, seed_test_cast
    from leeroy_mind.testing.fake_clock import FakeClock
    people = seed_test_cast(store)
    people = {g:replace(p,identity=replace(p.identity,region='US'),
                       real_life=replace(p.real_life,night_owl=False)) for g,p in people.items()}
    cast = {g:SimpleNamespace(class_=TEST_CAST[g][2],zone=0) for g in people}
    return SimpleNamespace(store=store,personas=SimpleNamespace(get=people.get),voice=None,
        human_guid=1,guild=None,clock=FakeClock(datetime(2026,11,12,hour,tzinfo=timezone.utc)),
        world=SimpleNamespace(discord_members=lambda:cast,online=set(),human_online=False,group=None))


def test_refresh_writes_evening_seats_and_excludes_playing_and_away(store,monkeypatch):
    import json
    from leeroy_mind.server.seats import SEATS_KEY, refresh_seats
    monkeypatch.setattr('leeroy_mind.server.seats.VOICE_SHARE',1)
    app=seat_app(store)
    app.guild=SimpleNamespace(members_in_play=lambda:{101,103})
    store.conn.execute('INSERT INTO away(guid,left_at,returns_at) VALUES(102,?,?)',
                       (app.clock.now()-60,app.clock.now()+60))
    refresh_seats(app,app.clock.now())
    value=json.loads(store.get_meta(SEATS_KEY))
    assert value['at']==app.clock.now()
    assert {g for guids in value['rooms'].values() for g in guids}=={104,105}
    app.world.human_online=True
    # This also covers playing without the optional voice extension attached.
    refresh_seats(app,app.clock.now())
    assert json.loads(store.get_meta(SEATS_KEY))['rooms']=={}


def test_refresh_outside_evening_clears_without_reading_members(store):
    import json
    from leeroy_mind.server.seats import SEATS_KEY, refresh_seats
    app=seat_app(store,hour=12)
    store.set_meta(SEATS_KEY,json.dumps({'at':app.clock.now(),'rooms':{'lore':[101]}}))
    def unexpected():
        raise AssertionError('daytime seat tick read the roster')
    app.world.discord_members=unexpected
    refresh_seats(app,app.clock.now())
    assert json.loads(store.get_meta(SEATS_KEY))['rooms']=={}


def test_fresh_seats_recover_after_a_crashed_voice_session(store):
    import json
    from leeroy_mind.server.seats import SEATS_KEY, read_seats
    store.conn.execute("INSERT INTO voice_presence(guid,room,since,by) VALUES(1,'guild_hall',1000,0)")
    store.set_meta(SEATS_KEY,json.dumps({'at':999,'rooms':{'lore':[101]}}))
    assert read_seats(store.conn,1000,human=1)=={}
    fresh={'at':1001,'rooms':{'lore':[101]}}
    store.set_meta(SEATS_KEY,json.dumps(fresh))
    assert read_seats(store.conn,1001,human=1)==fresh


def test_refresh_with_60000_messages_keeps_loop_block_under_50ms(store,monkeypatch):
    import asyncio
    from conftest import run
    from leeroy_mind.server.seats import refresh_seats
    app=seat_app(store)
    monkeypatch.setattr('leeroy_mind.server.seats.VOICE_SHARE',1)
    with store.transaction():
        store.conn.executemany('INSERT INTO discord_message(channel,author,origin,text,ts) '
            "VALUES('general',?,'mind',?,?)",
            ((101+i%4,'Invented harbor note @Test105',app.clock.now()-i) for i in range(60000)))
    queries=[]
    store.conn.set_trace_callback(queries.append)
    async def go():
        loop=asyncio.get_running_loop()
        resumed=loop.create_future()
        started=loop.time()
        loop.call_soon(lambda:resumed.set_result(loop.time()-started))
        refresh_seats(app,app.clock.now())
        stalled=await resumed
        print(f'SEAT_REFRESH_60000 loop_block_ms={stalled*1000:.3f}')
        assert stalled<.050
    try:
        run(go())
    finally:
        store.conn.set_trace_callback(None)
    assert not any('discord_message' in query for query in queries)
