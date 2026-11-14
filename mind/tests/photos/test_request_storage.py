"""Request durability and photo allowances; all people, files and messages are invented."""
import sqlite3
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.discord import store as discord
from leeroy_mind.personas.io import load_persona
from leeroy_mind.photos import request_store as requests, request_stock
from leeroy_mind.photos.send import photo_seeds
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.store import current, pick
from leeroy_mind.storage import db
from leeroy_mind.storage.store import Store
from selfies.fakes import ready, ready_snap, renderable_cast
from selfies.test_scene_choices import PICKS
from photos.test_images import jpeg


@pytest.fixture
def app(store, tmp_path, clock):
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    store.set_meta('human_guid', '1')
    people = {g: load_persona(store, g) for g in (101, 102, 103)}
    instance = SimpleNamespace(store=store, data_dir=tmp_path, clock=clock,
        cfg=SimpleNamespace(talk=SimpleNamespace(selfies=3)),
        selfies=SimpleNamespace(cfg=SelfieCfg(enabled=True, snaps=True)),
        personas=SimpleNamespace(get=people.get), people=people)
    for guid in people:
        ready(store, tmp_path, guid, 1)
        for index in range(4):
            for thumb in (False, True):
                files.candidate(tmp_path, guid, 1, index, thumb=thumb).write_bytes(
                    jpeg() + f'/{guid}/{index}/{thumb}'.encode())
        chosen = pick(store, guid, 1, 0, source='console', now=clock.now())
        path = files.history(tmp_path, guid, chosen)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files.candidate(tmp_path, guid, 1, 0).read_bytes())
    return instance


def claim(app, guid=101, *, now=None, channel=None, audience='him'):
    now = app.clock.now() if now is None else now
    channel = channel or f'dm:{guid}'
    message = discord.post(app.store, channel=channel, author=1, origin='human',
                           text='a photo please', now=now)
    return requests.claim(app.store, message=message, guid=guid, asked_channel=channel,
        thread=None, photo_channel=f'dm:{guid}' if audience == 'him' else channel,
        audience=audience, now=now)


def fresh(app, request):
    return requests.plan_fresh(app.store, request,
        scene=compose_choices({**PICKS, 'audience': request.audience}),
        pick=current(app.store, request.guid).pick,
        looks=snaps.looks_hash(app.people[request.guid].appearance), now=request.created)


def render(app, photo):
    snaps.mark_rendered(app.store, photo.id, width=32, height=24, seed=7, tries=1,
                        nsfw=.01, prompt='invented profile photo', now=photo.created)
    for thumb in (False, True):
        path = files.snap(app.data_dir, photo.guid, photo.id, thumb=thumb)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg() + str(photo.id).encode())
    return requests.snap(app.store, photo.id)


def deliver(app, request, photo, kind):
    store = app.store
    with store.transaction():
        mid = discord.post(store, channel=request.photo_channel, author=request.guid,
            origin='mind', text='here it is', now=app.clock.now(),
            attachment=discord.AttachmentIn('snap', f'selfies/{request.guid}/snaps/{photo.id}.jpg',
                                             32, 24, 100, photo.id))
        # The production pipeline creates the chat line and its link in this same unit.
        line = 1000 + mid
        store.set_meta(f'discord:line:{mid}', str(line))
        assert snaps.mark_sent(store, photo.id, sender=request.guid,
                              channel='dm' if request.audience == 'him' else 'discord',
                              line_id=line, now=app.clock.now())
        assert requests.mark_delivery(store, request.message, kind, mid)
    return mid


def test_claim_is_durable_idempotent_and_member_cap_does_not_reset(store, app):
    req, outcome = claim(app)
    assert outcome == 'accepted'
    assert requests.claim(store, message=req.message, guid=101, asked_channel='dm:101',
        thread=None, photo_channel='dm:101', audience='him', now=req.created + 1) == (req, 'existing')
    assert requests.MEMBER_GAP == 120
    assert claim(app, now=req.created + 119)[1] == 'cooldown'
    assert claim(app, 102, now=req.created + 119)[1] == 'accepted'
    assert claim(app, now=req.created + 120)[1] == 'accepted'
    reopened = Store(db.open_db(app.data_dir / 'mind.db'), clock=app.clock)
    try:
        assert requests.get(reopened, req.message) == req
    finally:
        reopened.close()


def test_unrendered_expiry_keeps_ready_recovery_after_24_hours(app):
    pending, _ = claim(app)
    pending_photo = fresh(app, pending)
    ready_req, _ = claim(app, 102)
    ready_photo = render(app, fresh(app, ready_req))
    before = files.snap(app.data_dir, 102, ready_photo.id).read_bytes()
    gone = requests.expire_unrendered(app.store, now=pending.expires)
    assert [r.message for r in gone] == [pending.message]
    assert requests.snap(app.store, pending_photo.id).ended == 'passed'
    assert [r.message for r in requests.due(app.store, now=pending.expires + 100)] == [ready_req.message]
    assert request_stock.valid(app, ready_req, ready_photo, now=pending.expires + 100)
    assert files.snap(app.data_dir, 102, ready_photo.id).read_bytes() == before


@pytest.mark.parametrize('sent', [False, True])
def test_expiry_closes_unsent_fallback_but_preserves_sent_receipt_and_files(app, sent):
    request, _ = claim(app)
    planned = fresh(app, request)
    fallback = request_stock.reserve_fallback(app, request)
    assert fallback is not None
    message = deliver(app, request, fallback, 'fallback') if sent else None
    before = {path: path.read_bytes() for path in app.data_dir.rglob('*.jpg')}
    requests.expire_unrendered(app.store, now=request.expires)
    expired = requests.get(app.store, request.message)
    assert expired.ended == 'passed' and expired.fallback_message == message
    assert requests.snap(app.store, planned.id).ended == 'passed'
    copied = requests.snap(app.store, fallback.id)
    assert (copied.status, copied.ended) == (('sent', None) if sent else ('expired', 'passed'))
    if sent:
        assert app.store.conn.execute('SELECT snap FROM discord_attachment WHERE message=?',
                                      (message,)).fetchone()[0] == fallback.id
        assert copied.line_id is not None
    assert all(path.read_bytes() == image for path, image in before.items())


def test_initial_words_and_fallback_do_not_complete_fresh_followup(app):
    req, _ = claim(app)
    fresh_photo = render(app, fresh(app, req))
    fallback = request_stock.reserve_fallback(app, req)
    first = deliver(app, req, fallback, 'fallback')
    assert requests.mark_initial(app.store, req.message, first)
    assert requests.get(app.store, req.message).fresh_message is None
    assert [r.message for r in requests.due(app.store, now=req.created)] == [req.message]
    second = deliver(app, req, fresh_photo, 'fresh')
    assert requests.mark_delivery(app.store, req.message, 'followup', second)
    assert requests.due(app.store, now=req.created) == []
    assert requests.mark_initial(app.store, req.message, first)
    assert not requests.mark_initial(app.store, req.message, second)


def test_failed_render_notification_is_separate_and_once(app):
    req, _ = claim(app)
    photo = fresh(app, req)
    initial = discord.post(app.store, channel='dm:101', author=101, origin='mind',
                           text='one moment', now=req.created)
    assert requests.mark_initial(app.store, req.message, initial)
    requests.fail(app.store, req.message, 'worker_gone', now=req.created)
    assert requests.snap(app.store, photo.id).status == 'expired'
    assert requests.due(app.store, now=req.created)[0].failure == 'worker_gone'
    notification = discord.post(app.store, channel='dm:101', author=101, origin='mind',
                                text='the photo did not come out', now=req.created + 1)
    assert requests.mark_delivery(app.store, req.message, 'failure', notification)
    requests.fail(app.store, req.message, 'later_error', now=req.created + 2)
    assert requests.due(app.store, now=req.created + 2) == []
    assert requests.get(app.store, req.message).failure == 'worker_gone'


def test_delivery_rollback_restores_photo_message_and_request_receipts(app):
    req, _ = claim(app)
    photo = render(app, fresh(app, req))
    before = app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]
    with pytest.raises(RuntimeError, match='rollback'):
        with app.store.transaction():
            deliver(app, req, photo, 'fresh')
            raise RuntimeError('rollback')
    assert requests.get(app.store, req.message).fresh_message is None
    assert requests.snap(app.store, photo.id).status == 'ready'
    assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == before
    deliver(app, req, photo, 'fresh')
    wrong = discord.post(app.store, channel='general', author=102, origin='mind',
                         text='unrelated', now=req.created)
    with pytest.raises(requests.RequestError, match='does not belong'):
        requests.mark_initial(app.store, req.message, wrong)


def test_actual_borrow_limits_survive_reopen_and_local_day_boundary(app):
    req, _ = claim(app)
    for hour in range(5):
        at = req.created + hour * 3600
        for _ in range(6):
            assert requests.admit_borrow(app.store, req.message, now=at, local_date='2026-11-13')
        assert not requests.admit_borrow(app.store, req.message, now=at + 3599, local_date='2026-11-13')
    reopened = Store(db.open_db(app.data_dir / 'mind.db'), clock=app.clock)
    try:
        assert not requests.admit_borrow(reopened, req.message, now=at + 3600, local_date='2026-11-13')
        assert requests.admit_borrow(reopened, req.message, now=at + 3600, local_date='2026-11-14')
        assert reopened.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == 31
    finally:
        reopened.close()


def test_requested_rows_never_spend_spontaneous_stock_gap_or_rations(app):
    req, _ = claim(app, channel='general', audience='anyone')
    photo = fresh(app, req)
    assert snaps.planned(app.store, 20) == []
    assert snaps.open_stock(app.store, 101) == 0
    assert snaps.sweep(app.store, now=req.expires + 1, pick_of=lambda _: None,
                       looks_of=lambda _: None) == []
    photo = render(app, photo)
    assert snaps.ready_for(app.store, 101, now=req.created, phase=None) == []
    assert photo_seeds(app.store.conn, now=req.created, local=app.clock.local(), dial=10) == []
    deliver(app, req, photo, 'fresh')
    assert snaps.sent_count(app.store, 0) == 0
    assert snaps.last_sent(app.store, 101) is None
    ordinary = ready_snap(app.store, app.data_dir, 101, scene=photo.scene, pick=photo.pick,
                          looks=photo.looks, audience='anyone')
    assert len(photo_seeds(app.store.conn, now=req.created, local=app.clock.local(), dial=10)) == 1
    assert snaps.open_stock(app.store, 101) == 1
    assert snaps.ready_for(app.store, 101, now=req.created, phase=None)[0].id == ordinary


def test_ready_snap_any_phase_precedes_gallery_and_copy_keeps_original(app):
    req, _ = claim(app)
    source = ready_snap(app.store, app.data_dir, 101, phases=('morning',),
        audience='him', scene=compose_choices({**PICKS, 'audience': 'him'}),
        pick=current(app.store, 101).pick, looks=snaps.looks_hash(app.people[101].appearance))
    for thumb in (False, True):
        files.snap(app.data_dir, 101, source, thumb=thumb).write_bytes(jpeg() + b'original')
    before = {p: p.read_bytes() for p in app.data_dir.rglob('*.jpg')}
    copy = request_stock.reserve_fallback(app, req)
    assert copy.source == 'request' and copy.request == req.message and copy.id != source
    assert requests.snap(app.store, source).status == 'ready'
    assert files.snap(app.data_dir, 101, copy.id).read_bytes() == jpeg() + b'original'
    assert all(p.read_bytes() == value for p, value in before.items())
    assert request_stock.reserve_fallback(app, req).id == copy.id


def test_gallery_copy_identity_survives_new_request_but_is_audience_scoped(app):
    req, _ = claim(app)
    first = request_stock.reserve_fallback(app, req)
    first_bytes = files.snap(app.data_dir, 101, first.id).read_bytes()
    deliver(app, req, first, 'fallback')
    later, _ = claim(app, now=req.created + 600)
    second = request_stock.reserve_fallback(app, later, now=req.created + 600)
    assert files.snap(app.data_dir, 101, second.id).read_bytes() != first_bytes
    public, _ = claim(app, channel='general', audience='anyone', now=req.created + 1200)
    public_copy = request_stock.reserve_fallback(app, public, now=req.created + 1200)
    assert files.snap(app.data_dir, 101, public_copy.id).read_bytes() == first_bytes
    expired_window, _ = claim(app, now=req.created + requests.REPEAT_WINDOW + 1)
    again = request_stock.reserve_fallback(app, expired_window, now=expired_window.created)
    assert files.snap(app.data_dir, 101, again.id).read_bytes() == first_bytes


def test_ordinary_sent_image_also_excludes_matching_gallery_candidate(app):
    req, _ = claim(app)
    source = ready_snap(app.store, app.data_dir, 101, audience='him')
    image = files.candidate(app.data_dir, 101, 1, 0).read_bytes()
    files.snap(app.data_dir, 101, source).write_bytes(image)
    snaps.mark_sent(app.store, source, sender=101, channel='dm', line_id=900, now=req.created)
    copied = request_stock.reserve_fallback(app, req)
    assert files.snap(app.data_dir, 101, copied.id).read_bytes() != image


def test_indexed_fresh_history_above_scan_bound_keeps_unsent_gallery_available(app):
    # One fresh photo an hour is inside both borrow caps. After a little over five days,
    # its 129 recent receipts exceed the bounded scan for old, unindexed images.
    for _ in range(129):
        request, _ = claim(app)
        photo = render(app, fresh(app, request))
        image = files.snap(app.data_dir, 101, photo.id).read_bytes()
        # The renderer records this same identity in its mark_rendered transaction.
        requests.remember_image(app.store, photo.id, identity=f'snap:{photo.id}',
                                digest=hashlib.sha256(image).hexdigest())
        deliver(app, request, photo, 'fresh')
        app.clock._t += 3600
    assert app.store.conn.execute('SELECT count(*) FROM selfie_request_image i '
        "JOIN selfie_snap s ON s.id=i.snap WHERE s.status='sent'").fetchone()[0] == 129
    assert app.store.conn.execute("SELECT count(*) FROM selfie_snap s WHERE status='sent' "
        'AND NOT EXISTS(SELECT 1 FROM selfie_request_image i WHERE i.snap=s.id)').fetchone()[0] == 0
    request, _ = claim(app)
    expected = files.candidate(app.data_dir, 101, 1, 0).read_bytes()
    fallback = request_stock.reserve_fallback(app, request)
    assert fallback is not None and request_stock.valid(app, request, fallback)
    assert files.snap(app.data_dir, 101, fallback.id).read_bytes() == expected


def test_delivery_rechecks_no_repeat_after_another_request_reserved_the_same_image(app):
    first, _ = claim(app)
    a = request_stock.reserve_fallback(app, first)
    second, _ = claim(app, now=first.created + 600)
    b = request_stock.reserve_fallback(app, second, now=second.created)
    assert files.snap(app.data_dir, 101, a.id).read_bytes() == files.snap(app.data_dir, 101, b.id).read_bytes()
    deliver(app, first, a, 'fallback')
    assert not request_stock.valid(app, second, b, now=second.created)


def test_deleted_owner_post_cannot_deliver_a_reserved_copy(app):
    req, _ = claim(app)
    photo = request_stock.reserve_fallback(app, req)
    discord.delete(app.store, req.message, author=1, now=req.created + 1)
    assert not request_stock.valid(app, req, photo)


def test_partial_copy_failure_keeps_files_and_cannot_publish_ready(app, monkeypatch):
    req, _ = claim(app)
    write = request_stock._write_new
    def fail_thumb(path, data):
        if path.name.endswith('.thumb.jpg'):
            raise OSError('invented write failure')
        write(path, data)
    monkeypatch.setattr(request_stock, '_write_new', fail_thumb)
    assert request_stock.reserve_fallback(app, req) is None
    copy = requests.snap(app.store, requests.get(app.store, req.message).fallback_snap)
    assert (copy.status, copy.ended) == ('expired', 'missing')
    assert files.snap(app.data_dir, 101, copy.id).is_file()
    assert files.candidate(app.data_dir, 101, 1, 0).is_file()
    assert request_stock.reserve_fallback(app, req) is None


@pytest.mark.parametrize('change', ['pick', 'looks', 'age', 'nsfw', 'dial', 'missing', 'lust_set'])
def test_gallery_copy_rechecks_current_identity_and_content_at_send(app, change):
    req, _ = claim(app)
    photo = request_stock.reserve_fallback(app, req)
    assert request_stock.valid(app, req, photo)
    if change == 'pick':
        pick(app.store, 101, 1, 1, source='console', now=req.created)
    elif change == 'looks':
        p = app.people[101]
        app.people[101] = replace(p, appearance=replace(p.appearance, hair_colour='black'))
    elif change == 'age':
        p = app.people[101]
        app.people[101] = replace(p, identity=replace(p.identity, age=24))
    elif change == 'nsfw':
        app.selfies.cfg = replace(app.selfies.cfg, nsfw_max=.001)
    elif change == 'dial':
        app.cfg.talk.selfies = 0
    elif change == 'missing':
        files.snap(app.data_dir, 101, photo.id).rename(app.data_dir / 'retained.jpg')
    elif change == 'lust_set':
        app.store.set_meta('selfies:set-lust:101:1', '1')
    assert not request_stock.valid(app, req, photo)
    assert files.snap(app.data_dir, 101, photo.id, thumb=True).exists()


def test_lust_gallery_never_selected_while_member_is_plain(app):
    app.store.set_meta('selfies:set-lust:101:1', '1')
    req, _ = claim(app)
    assert request_stock.reserve_fallback(app, req) is None


def test_migration_preserves_existing_snap_attachment_graph(tmp_path, monkeypatch, clock):
    all_files = db._migration_files()
    new = next((v, p) for v, p in all_files if p.stem.endswith('_selfie_requests'))
    before = [(v, p) for v, p in all_files if v < new[0]]
    with monkeypatch.context() as m:
        m.setattr(db, '_migration_files', lambda: before)
        old = Store(db.open_db(tmp_path / 'old.db'), clock=clock)
    with old.transaction():
        sid = old.conn.execute("INSERT INTO selfie_snap(kind,guid,source,ref,night,status,scene,audience,"
            "phases,pick,looks,created,not_before,expires,rendered) VALUES('situation',101,'life',"
            "'beat:1','night','ready','{}','him','[]',1,'looks',1,1,100,2)").lastrowid
        mid = discord.post(old, channel='dm:101', author=101, origin='mind', text='photo', now=3,
            attachment=discord.AttachmentIn('snap', 'selfies/101/snaps/1.jpg', 32, 24, 100, sid))
    try:
        assert db.migrate(old.conn) == db.latest_version()
        assert old.conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert old.conn.execute('SELECT snap FROM discord_attachment WHERE message=?', (mid,)).fetchone()[0] == sid
        assert old.conn.execute('SELECT request FROM selfie_snap WHERE id=?', (sid,)).fetchone()[0] is None
        indexes = {r[1] for r in old.conn.execute('PRAGMA index_list(selfie_snap)')}
        assert {'selfie_snap_open', 'selfie_snap_sent', 'selfie_snap_request'} <= indexes
        with pytest.raises(sqlite3.IntegrityError):
            with old.transaction():
                old.conn.execute("UPDATE selfie_snap SET source='request',request=99999 WHERE id=?", (sid,))
    finally:
        old.close()
