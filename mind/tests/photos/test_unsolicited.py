"""[photos] unsolicited: only highly romantic women, only while the GPU is idle, capped, off by
default; the photo goes through the request render (NSFW gate) and her DM with him."""
import asyncio
from dataclasses import replace

import pytest

from conftest import TEST_CAST, run
from photos.test_request_priority import PRIORITY
from photos.test_request_recovery import Worker, make_app
from photos.test_request_service import ON, drive, offline
from selfies.test_scene_choices import PICKS
from leeroy_mind.discord import store as discord
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.photos import unsolicited as U
from leeroy_mind.photos.config import load_photos_config
from leeroy_mind.selfies.scene import scene_from
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.worker import Rendered
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.wow import CLASSES, RACES

UNSOLICITED = (PRIORITY.replace('request_priority=true', 'request_priority=true\nunsolicited=true')
               + '[romance]\ndial=9\n')


@pytest.fixture(autouse=True)
def enum_scene(monkeypatch):
    async def scene(app, guid, words, audience, *, model=True):
        return scene_from(compose_choices({**PICKS, 'audience': audience, 'when': 'any'}))
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', scene)


def romantic(app, guid, *, stance='lust', gender='female', dial=None, stage='none'):
    p = load_persona(app.store, guid)
    _, race, cls = TEST_CAST[guid]
    p = replace(p, identity=replace(p.identity, player_gender=gender),
                romance=replace(p.romance, stance=stance, dial=dial))
    save_persona(app.store, p, source='seed', ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    app.personas.invalidate(guid)
    app.store.put_relationship(Relationship(a=guid, b=app.human_guid, romance_stage=stage))


def worker_factory(app, *, flagged=False):
    made = []
    class Flagging(Worker):
        async def render(self, request, *, rid):
            got = await super().render(request, rid=rid)
            return replace(got, nsfw=.99, flagged=True) if flagged else got
    def factory():
        made.append(Flagging())
        return made[-1]
    app.photos.unsolicited.worker_factory = factory
    return made


async def idle_app(tmp_path, toml=UNSOLICITED):
    app = make_app(tmp_path, toml)
    await offline(app)
    app.night_scheduler.presence.offline_seconds = lambda: 3600.0
    for guid in (101, 102, 103):
        romantic(app, guid, stance='none')
    return app


def sent_photos(app, guid):
    return app.store.conn.execute(
        "SELECT a.snap FROM discord_attachment a JOIN discord_message m ON m.id=a.message "
        "WHERE m.channel=? AND m.author=? AND m.origin='mind'", (f'dm:{guid}', guid)).fetchall()


def test_switch_defaults_on_and_needs_requests(tmp_path):
    path = tmp_path / 'mind.toml'
    path.write_text('[photos]\nsend=true\nrequests=true\n')
    assert load_photos_config(path).unsolicited is True  # on by default (2026-11-25)
    path.write_text('[photos]\nsend=true\nrequests=true\nunsolicited=false\n')
    assert load_photos_config(path).unsolicited is False
    path.write_text('[photos]\nsend=true\nrequests=true\nunsolicited=true\n')
    assert load_photos_config(path).unsolicited is True
    async def go():
        app = make_app(tmp_path / 'off', ON)
        try:
            assert getattr(app.photos, 'unsolicited', None) is None
        finally:
            await app.stop()
        app = make_app(tmp_path / 'norequests', ON.replace('requests=true\n', 'unsolicited=true\n'))
        try:
            assert getattr(app.photos, 'requests', None) is None
            assert getattr(app.photos, 'unsolicited', None) is None
        finally:
            await app.stop()
    (tmp_path / 'off').mkdir()
    (tmp_path / 'norequests').mkdir()
    run(go())


@pytest.mark.parametrize('busy', ['human', 'discord', 'offline_raid', 'voice_gpu', 'pause', 'night',
                                  'request', 'presence'])
def test_idle_only_when_nothing_else_wants_the_gpu(tmp_path, busy):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            app.arbiter.hold('server')  # the server's own pin may stand
            assert service.idle()
            if busy == 'pause':
                app.gpu.gate.paused = True
                app.arbiter.paused = True
            elif busy == 'night':
                app.night_scheduler.due = lambda **_: 'night'
            elif busy == 'request':
                discord.post(app.store, channel='dm:101', author=app.human_guid, origin='human',
                             text='send me a selfie', now=app.clock.now())
                assert app.photos.requests.begin(
                    app.store.conn.execute('SELECT max(id) FROM discord_message').fetchone()[0])
                await asyncio.sleep(0)
            elif busy == 'presence':
                app.night_scheduler.presence.offline_seconds = lambda: 60.0
            else:
                app.arbiter.hold(busy)
            assert not service.idle()
        finally:
            await app.stop()
    run(go())


def test_only_highly_romantic_women_who_are_up(tmp_path):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            now, date = app.clock.now(), app.clock.local().date().isoformat()
            service._up = lambda guid, p, member, now: True
            assert service.candidates(now, date) == []
            romantic(app, 101, stance='lust')
            romantic(app, 102, stance='drawn', stage='banter')  # not far enough on their arc
            romantic(app, 103, stance='lust', gender='male')
            assert service.candidates(now, date) == [(101, 2.0)]
            romantic(app, 102, stance='drawn', stage='late_nights')
            assert dict(service.candidates(now, date)) == {101: 2.0, 102: 1.0}
            romantic(app, 101, stance='lust', dial=3)  # her own dial, set low by hand
            assert dict(service.candidates(now, date)) == {102: 1.0}
            # He has been heating things up with her lately: twice as likely.
            discord.post(app.store, channel='dm:102', author=app.human_guid, origin='human',
                         text='i want you so bad', now=now - 60)
            service._heated = lambda guid, now: guid == 102
            assert dict(service.candidates(now, date)) == {102: 2.0}
            service._up = lambda guid, p, member, now: False  # asleep, away or offline
            assert service.candidates(now, date) == []
        finally:
            await app.stop()
    run(go())


def test_renders_and_sends_one_in_her_dm_within_the_caps(tmp_path):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            romantic(app, 101, stance='lust')
            service._up = lambda guid, p, member, now: True
            made = worker_factory(app)
            date = app.clock.local().date().isoformat()
            task = asyncio.create_task(service.make(101, date))
            await drive(app, task.done)  # her typing and hold run on the fake clock
            assert task.result() == 'sent'
            assert len(made) == 1 and made[0].stops == 1
            assert len(sent_photos(app, 101)) == 1
            assert not app.live_borrow.blocked and app.cfg.models.live in app.arbiter.admin.loaded
            row = app.store.conn.execute("SELECT source,audience,status,channel,request FROM selfie_snap "
                                         "WHERE ref=?", (f'unsolicited:{date}',)).fetchone()
            assert tuple(row) == ('everyday', 'him', 'sent', 'dm', None)
            # One a member a day; the guild's day cap and gap.
            assert await service.make(101, date) == 'cap'
            assert service.candidates(app.clock.now(), date) == []
            assert not service._room(date, app.clock.now())  # GAP_S since the last one
            await app.clock.advance(U.GAP_S + 1, U.GAP_S + 1)
            assert service._room(date, app.clock.now())
            app.store.set_meta(U.DAY_KEY.format(date=date), str(U.DAY_CAP))
            assert not service._room(date, app.clock.now())
            assert len(made) == 1
        finally:
            await app.stop()
    run(go())


def test_flagged_render_is_never_sent(tmp_path):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            romantic(app, 101, stance='lust')
            made = worker_factory(app, flagged=True)
            date = app.clock.local().date().isoformat()
            assert await service.make(101, date) == 'flagged'
            assert made and not sent_photos(app, 101)
            assert app.store.conn.execute("SELECT status FROM selfie_snap WHERE ref=?",
                                          (f'unsolicited:{date}',)).fetchone()[0] == 'expired'
        finally:
            await app.stop()
    run(go())


def test_his_discord_post_stops_a_render_in_progress(tmp_path):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            romantic(app, 101, stance='lust')
            started = asyncio.Event()
            class Slow(Worker):
                async def render(self, request, *, rid):
                    started.set()
                    await asyncio.sleep(30)
            service.worker_factory = Slow
            date = app.clock.local().date().isoformat()
            task = asyncio.create_task(service.make(101, date))
            await asyncio.wait_for(started.wait(), 5)
            app.arbiter.hold('discord')  # he posted on Discord
            outcome = await asyncio.wait_for(task, 10)
            assert outcome == 'interrupted' and not sent_photos(app, 101)
            assert app.store.conn.execute("SELECT status FROM selfie_snap WHERE ref=?",
                                          (f'unsolicited:{date}',)).fetchone()[0] == 'expired'
        finally:
            await app.stop()
    run(go())


def test_tick_does_nothing_with_the_dial_at_zero_or_while_busy(tmp_path):
    async def go():
        app = await idle_app(tmp_path)
        try:
            service = app.photos.unsolicited
            romantic(app, 101, stance='lust')
            service._up = lambda guid, p, member, now: True
            service.rng.random = lambda: 0.0  # every draw would fire
            made = worker_factory(app)
            app.arbiter.hold('human')
            await service.tick()
            assert not made
            await app.arbiter.release_live('human')
            app.cfg = replace(app.cfg, talk=replace(app.cfg.talk, selfies=0))
            await service.tick()
            assert not made
        finally:
            await app.stop()
    run(go())
