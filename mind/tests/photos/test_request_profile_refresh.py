"""Profile edits must not turn an ordinary pending photo request into a refusal."""
from dataclasses import replace

import pytest

from conftest import run
from photos.test_request_storage import app, claim, fresh, render  # noqa: F401
from photos.test_request_recovery import (Worker, make_app, queued, initial_words, offline,
                                         tick, restart, workers, ready as ready_photo)
from selfies.fakes import ready
from selfies.test_request_scene import save
from leeroy_mind.photos import request_store as records
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.store import current, pick


def refresh(app, request, photo, **kwargs):
    return records.refresh_fresh(app.store, request, photo, pick=photo.pick,
        looks='changed-looks', now=app.clock.now(), **kwargs)


@pytest.mark.parametrize('rendered', [False, True])
def test_refresh_preserves_scene_deadline_and_old_files_and_rejects_stale_replay(app, rendered):
    request, _ = claim(app)
    photo = fresh(app, request)
    if rendered:
        photo = render(app, photo)
    before = {p: p.read_bytes() for p in app.data_dir.rglob('*.jpg')}
    replacement = refresh(app, request, photo)
    assert replacement.id != photo.id and replacement.status == 'planned'
    assert replacement.scene == photo.scene and replacement.expires == request.expires
    assert replacement.request == request.message and replacement.audience == request.audience
    assert records.get(app.store, request.message).fresh_snap == replacement.id
    assert records.snap(app.store, photo.id).ended == 'looks_changed'
    assert refresh(app, request, photo) is None
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('terminal', ['failure', 'ended', 'expired', 'flagged', 'sent'])
def test_refresh_cannot_reopen_terminal_or_delivered_work(app, terminal):
    request, _ = claim(app)
    photo = fresh(app, request)
    if terminal == 'failure':
        records.fail(app.store, request.message, 'worker', now=app.clock.now())
    elif terminal == 'ended':
        app.store.conn.execute("UPDATE selfie_request SET ended='cancelled' WHERE message=?", (request.message,))
    elif terminal == 'flagged':
        snaps.expire(app.store, photo.id, 'flagged', now=app.clock.now())
    elif terminal == 'sent':
        from photos.test_request_storage import deliver
        photo = render(app, photo)
        deliver(app, request, photo, 'fresh')
    at = request.expires if terminal == 'expired' else request.created
    assert records.refresh_fresh(app.store, request, photo, pick=photo.pick,
                                looks='changed', now=at) is None
    assert records.get(app.store, request.message).fresh_snap == photo.id


def test_refresh_limit_is_durable_and_transactional(app):
    request, _ = claim(app)
    photo = fresh(app, request)
    with pytest.raises(RuntimeError):
        with app.store.transaction():
            assert refresh(app, request, photo) is not None
            raise RuntimeError('rollback')
    assert records.get(app.store, request.message).fresh_snap == photo.id
    assert records.snap(app.store, photo.id).status == 'planned'
    for n in range(records.PROFILE_REFRESHES):
        photo = records.refresh_fresh(app.store, request, photo, pick=photo.pick,
                                     looks=f'changed-{n}', now=app.clock.now())
        assert photo is not None
    assert refresh(app, request, photo) is None


def test_refresh_keeps_a_deferred_photos_not_before(app):
    request, _ = claim(app)
    photo = fresh(app, request)
    later = request.created + 3600
    app.store.conn.execute('UPDATE selfie_snap SET not_before=? WHERE id=?', (later, photo.id))
    replacement = refresh(app, request, photo)
    assert replacement.not_before == later
    assert replacement.expires == photo.expires


def change_profile(app, change):
    if change == 'looks':
        p = app.personas.get(101)
        save(app, replace(p, appearance=replace(p.appearance, hair_colour='silver')))
    else:
        ready(app.store, app.data_dir, 101, 2)
        pick(app.store, 101, 2, 0, source='console', now=app.clock.now())
        files.sync_chosen(app.data_dir, 101, current(app.store, 101))


@pytest.mark.parametrize('change', ['looks', 'pick'])
@pytest.mark.parametrize('timing', ['before', 'during', 'ready'])
@pytest.mark.parametrize('priority', [False, True])
def test_profile_change_recovers_through_delivery_and_restart(tmp_path, change, timing, priority):
    from photos.test_request_priority import PRIORITY, online

    async def allow_render(instance):
        if priority:
            await online(instance)
            assert instance.photos.requests._offline_at is None
        else:
            await offline(instance)
            await instance.clock.advance(601, 601)

    async def scenario():
        instance = make_app(tmp_path, PRIORITY) if priority else make_app(tmp_path)
        try:
            request, photo = queued(instance)
            await initial_words(instance, request)
            if timing == 'ready':
                ready_photo(instance, photo)
            if timing != 'during':
                change_profile(instance, change)
            made = []
            class ChangingWorker(Worker):
                async def render(self, req, *, rid):
                    result = await super().render(req, rid=rid)
                    if timing == 'during' and len(made) == 1:
                        change_profile(instance, change)
                    return result
            def factory():
                worker = ChangingWorker()
                made.append(worker)
                return worker
            instance.photos.requests.worker_factory = factory
            await allow_render(instance)
            await tick(instance)
            latest = records.get(instance.store, request.message)
            assert latest.failure is None and latest.fresh_snap != photo.id
            assert records.snap(instance.store, photo.id).ended == (
                'looks_changed' if change == 'looks' else 'photo_changed')
            if timing == 'during':
                assert latest.fresh_message is None
                assert not files.snap(instance.data_dir, 101, photo.id).exists()
                instance = await restart(instance)
                made_after = workers(instance)
                await allow_render(instance)
                await tick(instance)
                assert len(made_after) == 1
            delivered = records.get(instance.store, request.message)
            assert delivered.fresh_message is not None and delivered.failure_message is None
            assert delivered.followup_message == delivered.fresh_message
            assert instance.store.conn.execute('SELECT count(*) FROM discord_attachment WHERE snap=?',
                                               (delivered.fresh_snap,)).fetchone()[0] == 1
            instance = await restart(instance)
            after = workers(instance)
            await tick(instance)
            assert not after
            assert records.get(instance.store, request.message).fresh_message == delivered.fresh_message
        finally:
            await instance.stop()
    run(scenario())
