from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from conftest import make_persona
from leeroy_mind.discord.presence import Presence
from leeroy_mind.server.seats import away_seats


def test_away_seats_follow_thinks_as_and_only_online_members(monkeypatch):
    monkeypatch.setattr('leeroy_mind.server.seats.VOICE_SHARE', 1)
    personas = {g: make_persona(g, f'Test{g}') for g in range(101, 106)}
    kinds = ['lore', 'collector', 'competitor', 'theorycrafter', 'social']
    personas = {g: replace(p, game=replace(p.game, thinks_as=kinds[g-101])) for g,p in personas.items()}
    states = {g: SimpleNamespace(presence=Presence('online', False, 0)) for g in personas}
    now = datetime(2026,11,12,19)
    assert away_seats(personas, states, now, class_rooms={104:'mages'}) == {
        'lore':(101,), 'achievements':(102,), 'pvp':(103,), 'mages':(104,), 'kitchen':(105,)}
    states[101] = SimpleNamespace(presence=Presence('offline',False,0))
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
    finally:
        run(app.stop())


def test_evening_roll_is_stable_across_midnight_and_social_boundaries(monkeypatch):
    import leeroy_mind.server.seats as seats
    p = make_persona(105,'Test105')
    p = replace(p,game=replace(p.game,thinks_as='social'))
    online = {105:SimpleNamespace(presence=Presence('online',False,0))}
    monkeypatch.setattr(seats,'VOICE_SHARE',1)
    for hour,room in [(17,'kitchen'),(19,'kitchen'),(20,'guild_hall'),(22,'guild_hall'),(23,'irl'),(0,'irl'),(2,'irl')]:
        assert seats.away_seats({105:p},online,datetime(2026,11,12,hour)) == {room:(105,)}
    for hour in (3,16):
        assert seats.away_seats({105:p},online,datetime(2026,11,12,hour)) == {}
    monkeypatch.setattr(seats,'VOICE_SHARE',.4)
    evening = datetime(2026,11,12,23)
    overnight = datetime(2026,11,13,1)
    assert seats.away_seats({105:p},online,evening) == seats.away_seats({105:p},online,overnight)
