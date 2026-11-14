"""Durable request recovery through the real Discord answer and photo-delivery pipeline."""
import asyncio
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import FakeAdmin
from photos.test_images import jpeg
from photos.test_request_service import make_app as base_app, post, offline, drive, ON
from selfies.fakes import ready_snap
from selfies.test_scene_choices import PICKS
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.discord.talk import ANSWERED_KEY
from leeroy_mind.discord import store as discord
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos import request_stock
from leeroy_mind.photos.send import channel_count
from leeroy_mind.selfies import files, ration, snaps
from leeroy_mind.selfies.scene import scene_from
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.store import current
from leeroy_mind.selfies.worker import Rendered, WorkerError
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM


def varied_answers(app):
    # An actual follow-up must pass the ordinary repetition guard. Reusing the helper's
    # one constant caption would test that guard rather than request recovery.
    phrases = (
        'the quiet afternoon made a good excuse for a photo',
        'managed to find some decent light beside the window',
        'the kettle interrupted me twice but I got there eventually',
        'a small update from this end of the desk',
        'the camera cooperated for once somehow',
        'caught a break before the rain arrived',
        'the dog has finally stopped investigating my shoes',
        'a little proof that I left the keyboard today',
    )
    index = app.store.conn.execute("SELECT count(*) FROM discord_message WHERE origin='mind'").fetchone()[0]
    def answer(*args):
        nonlocal index
        text = phrases[index % len(phrases)]
        index += 1
        return text
    app.llm._script = answer


def make_app(tmp_path, toml=ON):
    app = base_app(tmp_path, toml)
    varied_answers(app)
    return app


class Worker:
    """Only the identity worker boundary is fake; borrowing and delivery stay real."""
    def __init__(self, *, fail=False):
        self.fail, self.calls, self.stops = fail, [], 0

    async def start(self):
        return None

    async def render(self, request, *, rid):
        assert request.identity is not None
        self.calls.append((request, rid))
        if self.fail:
            raise WorkerError('gone')
        request.out.write_bytes(jpeg() + rid.encode())
        request.thumb.write_bytes(jpeg() + rid.encode())
        return Rendered(False, .01, 'a safe invented portrait', 10, .01, 12., 832, 1216)

    async def stop(self):
        self.stops += 1


def workers(app, *, fail=False):
    made = []
    def factory():
        worker = Worker(fail=fail)
        made.append(worker)
        return worker
    app.photos.requests.worker_factory = factory
    return made


def queued(app, *, guid=101, created=None):
    message = post(app, f'dm:{guid}')
    request, outcome = records.claim(app.store, message=message, guid=guid,
        asked_channel=f'dm:{guid}', thread=None, photo_channel=f'dm:{guid}', audience='him',
        now=app.clock.now() if created is None else created)
    assert outcome == 'accepted'
    photo = records.plan_fresh(app.store, request,
        scene=compose_choices({**PICKS, 'audience': 'him', 'when': 'any'}),
        pick=current(app.store, guid).pick, looks=snaps.looks_hash(app.personas.get(guid).appearance),
        now=request.created)
    return request, photo


def ready(app, photo):
    """The durable boundary left by a successful render just before the process stops."""
    for thumb in (False, True):
        path = files.snap(app.data_dir, photo.guid, photo.id, thumb=thumb)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg() + f'/fresh/{photo.id}'.encode())
    snaps.mark_rendered(app.store, photo.id, width=32, height=24, seed=7, tries=1, nsfw=.01,
                        prompt='a safe invented portrait', now=app.clock.now())


async def initial_words(app, request):
    # Already queued without a ready fallback: the ordinary answer is words only.
    app.discord_talk.on_his_post(request.message, immediate=True)
    await drive(app, lambda: records.get(app.store, request.message).initial_message is not None)
    assert records.get(app.store, request.message).fallback_message is None


async def restart(app):
    path, at = app.data_dir, app.clock.local()
    await app.stop()
    fresh = MindApp(load_config(path / 'mind.toml'), path, llm=FakeLLM(), clock=FakeClock(at))
    fresh.arbiter.admin = FakeAdmin([fresh.cfg.models.live, fresh.cfg.models.embedding])
    fresh.llm.set_models({fresh.cfg.models.live, fresh.cfg.models.embedding})
    varied_answers(fresh)
    return fresh


async def tick(app):
    task = asyncio.create_task(app.photos.requests.tick())
    try:
        await drive(app, task.done)
        task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def advance_to(app, at):
    if app.clock.now() < at:
        await app.clock.advance(at - app.clock.now(), at - app.clock.now())


def replies(app, message):
    return [tuple(row) for row in app.store.conn.execute(
        "SELECT id,text FROM discord_message WHERE reply_to=? AND origin='mind' ORDER BY id",
        (message,))]


@pytest.mark.parametrize('initial_delivered', [False, True])
def test_ready_unsent_photo_recovers_once_across_two_restarts(tmp_path, initial_delivered):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            if initial_delivered:
                await initial_words(app, request)
            ready(app, photo)
            image = files.snap(app.data_dir, 101, photo.id).read_bytes()
            # The 24-hour deadline expires unrendered work, never this already-ready photo.
            await advance_to(app, request.expires + 1)
            app = await restart(app)
            made = workers(app)
            await tick(app)
            await drive(app, lambda: records.get(app.store, request.message).fresh_message is not None)
            delivered = records.get(app.store, request.message)
            assert delivered.initial_message is not None
            if initial_delivered:
                assert delivered.followup_message == delivered.fresh_message
                assert delivered.initial_message != delivered.fresh_message
            else:
                assert delivered.initial_message == delivered.fresh_message
            assert len(replies(app, request.message)) == (2 if initial_delivered else 1)
            assert not made
            assert app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == 0
            assert files.snap(app.data_dir, 101, photo.id).read_bytes() == image
            app = await restart(app)
            made = workers(app)
            await tick(app)
            await app.clock.advance(61, 61)
            await tick(app)
            assert records.get(app.store, request.message).fresh_message == delivered.fresh_message
            assert len(replies(app, request.message)) == (2 if initial_delivered else 1)
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment WHERE snap=?',
                                          (photo.id,)).fetchone()[0] == 1
            assert not made
        finally:
            await app.stop()
    run(scenario())


def test_unrendered_request_expires_silently_at_24_hours(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            await initial_words(app, request)
            before = replies(app, request.message)
            await advance_to(app, request.expires)
            made = workers(app)
            await tick(app)
            expired = records.get(app.store, request.message)
            assert expired.ended == 'passed' and expired.failure is None and expired.failure_message is None
            assert records.snap(app.store, photo.id).ended == 'passed'
            assert replies(app, request.message) == before
            app = await restart(app)
            await tick(app)
            assert replies(app, request.message) == before
            assert not made
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 0
        finally:
            await app.stop()
    run(scenario())


def test_own_time_answered_marker_cannot_hide_uncommitted_photo_reply_after_restart(tmp_path):
    async def scenario():
        app = make_app(tmp_path, ON + 'dm_own_time=true\n')
        try:
            request, photo = queued(app)
            ready(app, photo)
            # Discord's own-time queue marker was durable before the crashed turn's actual
            # message. Request receipts, rather than that scheduling marker, own recovery.
            app.store.set_meta(ANSWERED_KEY.format(guid=101), str(request.message))
            assert app.discord_talk._already(request.message)
            app = await restart(app)
            made = workers(app)
            assert app.discord_talk.own_time() and app.discord_talk._already(request.message)
            await tick(app)
            await drive(app, lambda: records.get(app.store, request.message).initial_message is not None)
            delivered = records.get(app.store, request.message)
            assert delivered.initial_message == delivered.fresh_message
            assert len(replies(app, request.message)) == 1 and not made
        finally:
            await app.stop()
    run(scenario())


def test_begin_on_completed_request_cannot_enqueue_an_extra_ordinary_answer(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            ready(app, photo)
            assert app.photos.requests.begin(request.message)
            await drive(app, lambda: records.get(app.store, request.message).fresh_message is not None)
            await drive(app, lambda: not app.photos.requests._tasks and not app.engine._current)
            before = replies(app, request.message)
            calls = len(app.llm.calls)
            for _ in range(3):
                assert app.photos.requests.begin(request.message)
            # A broken duplicate begin creates its task synchronously; settle any such task
            # and ordinary answer before checking both durable and model-call evidence.
            await app.clock.advance(30, 1)
            assert not app.photos.requests._tasks and not app.engine._current
            assert replies(app, request.message) == before
            assert len(app.llm.calls) == calls
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 1
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('rendered', [False, True])
def test_cancelled_request_closes_bound_open_snaps_and_preserves_files(tmp_path, rendered):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, fresh_photo = queued(app)
            fallback = request_stock.reserve_fallback(app, request)
            assert fallback is not None
            if rendered:
                ready(app, fresh_photo)
            before = {path: path.read_bytes() for path in tmp_path.rglob('*.jpg')}
            discord.delete(app.store, request.message, author=1, now=app.clock.now())
            made = workers(app)
            await tick(app)
            assert records.get(app.store, request.message).ended == 'cancelled'
            for sid in (fresh_photo.id, fallback.id):
                row = records.snap(app.store, sid)
                assert (row.status, row.ended) == ('expired', 'passed')
            assert all(path.read_bytes() == image for path, image in before.items())
            assert replies(app, request.message) == [] and not made
            app = await restart(app)
            await tick(app)
            assert records.due(app.store, now=app.clock.now()) == []
            assert all(path.read_bytes() == image for path, image in before.items())
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('failure', ['no_profile', 'worker'])
def test_lust_channel_failure_is_explained_before_marking_it_notified(tmp_path, monkeypatch, failure):
    async def scenario():
        app = make_app(tmp_path)
        try:
            original_get = app.personas.get
            p = original_get(101)
            lust = replace(p, identity=replace(p.identity, player_gender='female'),
                           romance=replace(p.romance, stance='lust'))
            monkeypatch.setattr(app.personas, 'get', lambda guid: lust if guid == 101 else original_get(guid))
            name = app.world.discord_members()[101].name
            message = post(app, 'general', f'@{name} send me a selfie')
            request, outcome = records.claim(app.store, message=message, guid=101,
                asked_channel='general', thread=None, photo_channel='dm:101', audience='him',
                now=app.clock.now())
            assert outcome == 'accepted'
            # This is the durable boundary after preparation/render failed, before the
            # initial channel answer committed. Recovery must explain that failure.
            records.fail(app.store, message, failure, now=app.clock.now())
            made = workers(app)
            assert app.photos.requests.begin(message)
            await drive(app, lambda: records.get(app.store, message).failure_message is not None)
            delivered = records.get(app.store, message)
            assert delivered.failure_message == delivered.initial_message
            assert len(replies(app, message)) == 1
            assert app.store.conn.execute('SELECT channel FROM discord_message WHERE id=?',
                                          (delivered.failure_message,)).fetchone()[0] == 'general'
            instructions = '\n'.join(m['content'] for call in app.llm.calls
                                     for m in call.get('messages', ()))
            expected = "don't have a usable photo" if failure == 'no_profile' else "didn't work out"
            assert expected in instructions
            assert 'The photo he asked for will be private between you two' not in instructions
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 0
            await app.clock.advance(61, 61)
            await tick(app)
            assert len(replies(app, message)) == 1 and not made
        finally:
            await app.stop()
    run(scenario())


def test_render_failure_is_notified_once_after_initial_answer_and_restart(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            await initial_words(app, request)
            await offline(app)
            made = workers(app, fail=True)
            await app.clock.advance(600, 600)
            await tick(app)
            failed = records.get(app.store, request.message)
            assert failed.failure and failed.failure_message
            assert failed.failure_message != failed.initial_message
            assert failed.fresh_message is None
            assert len(made) == 1 and len(made[0].calls) == 1 and made[0].stops == 1
            assert records.snap(app.store, photo.id).status == 'expired'
            assert len(replies(app, request.message)) == 2
            assert app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == 1
            app = await restart(app)
            after_restart = workers(app, fail=True)
            await offline(app)
            await app.clock.advance(601, 601)
            await tick(app)
            assert records.get(app.store, request.message).failure_message == failed.failure_message
            assert len(replies(app, request.message)) == 2
            assert not after_restart
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 0
        finally:
            await app.stop()
    run(scenario())


def test_existing_planned_request_without_initial_receipt_waits_full_offline_grace(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            app = await restart(app)
            made = workers(app)
            await offline(app)
            offline_at = app.clock.now()
            await tick(app)
            await drive(app, lambda: records.get(app.store, request.message).initial_message is not None)
            assert records.snap(app.store, photo.id).status == 'planned'
            assert not made
            assert app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == 0
            await advance_to(app, offline_at + 599)
            await tick(app)
            assert not made
            # A queue retry interval may delay admission, but it can never shorten the grace.
            await advance_to(app, offline_at + 660)
            await tick(app)
            delivered = records.get(app.store, request.message)
            assert delivered.fresh_message == delivered.followup_message
            assert delivered.fresh_message is not None
            assert len(made) == 1 and made[0].stops == 1
            borrowed = app.store.conn.execute('SELECT started FROM selfie_request_borrow').fetchone()[0]
            assert borrowed >= offline_at + 600
            assert len(replies(app, request.message)) == 2
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('channel', ['dm:101', 'general'])
@pytest.mark.parametrize('cap', ['hour', 'day'])
def test_borrow_caps_take_fallback_without_spending_spontaneous_ration(tmp_path, monkeypatch, channel, cap):
    async def scene(app, guid, words, audience):
        return scene_from(compose_choices({**PICKS, 'audience': audience, 'when': 'any'}))
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', scene)
    async def scenario():
        app = make_app(tmp_path)
        try:
            now, date = app.clock.now(), app.clock.local().date().isoformat()
            prior, _ = queued(app, guid=102, created=now - 8 * 3600)
            if cap == 'hour':
                for _ in range(records.HOUR_BORROWS):
                    assert records.admit_borrow(app.store, prior.message, now=now, local_date=date)
            else:
                for hour in range(records.DAY_BORROWS // records.HOUR_BORROWS):
                    for _ in range(records.HOUR_BORROWS):
                        assert records.admit_borrow(app.store, prior.message,
                            now=now - (6 - hour) * 3600, local_date=date)
            cap_count = app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0]
            # Fill the normal private ration and the channel counter; give this member a
            # recent ordinary send too. None is allowed to block or be spent by a request.
            for index in range(ration.per_week(app.cfg.talk.selfies)):
                guid = 101 if index == 0 else 103
                sid = ready_snap(app.store, app.data_dir, guid, audience='him')
                assert snaps.mark_sent(app.store, sid, sender=guid, channel='dm',
                                       line_id=9000 + index, now=now)
            app.store.set_meta(ration.CHANNEL_KEY.format(date=date), '12')
            budget = snaps.sent_count(app.store, 0), snaps.last_sent(app.store, 101), channel_count(app.store.conn, date)
            await offline(app)
            made = workers(app)
            name = app.world.discord_members()[101].name
            posted = await app.discord.handle({'action': 'post', 'channel': channel,
                                               'text': f'@{name} send me a selfie'})
            assert posted['ok']
            await drive(app, lambda: bool((r := records.get(app.store, posted['id'])) and r.initial_message))
            request = records.get(app.store, posted['id'])
            assert request.initial_message == request.fallback_message
            assert request.fallback_message is not None
            assert records.snap(app.store, request.fresh_snap).status == 'planned'
            assert not made
            assert app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == cap_count
            assert (snaps.sent_count(app.store, 0), snaps.last_sent(app.store, 101),
                    channel_count(app.store.conn, date)) == budget
            # Keep the ordinary ten-second channel interval, while remaining well inside
            # this request's independent ten-minute member cap.
            await app.clock.advance(15, 15)
            again = await app.discord.handle({'action': 'post', 'channel': channel,
                                              'text': f'@{name} selfie?'})
            await drive(app, lambda: bool(replies(app, again['id'])))
            assert records.get(app.store, again['id']) is None
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 1
            assert not made
        finally:
            await app.stop()
    run(scenario())
