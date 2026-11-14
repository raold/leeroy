"""Copy an audience-fit retained image into a request-owned snap, without consuming stock.

Files are bounded, checked reads and exclusive new writes. Originals, stale copies and failed
partial copies are retained. Identity and content digests follow copies across request ids;
ordinary snap deliveries are checked too, so they cannot bypass the 30-day audience gap.
"""
import hashlib
import math

from leeroy_mind.imagefiles import read_image
from leeroy_mind.selfies import files, ration, snaps
from leeroy_mind.selfies.prompt import renderable
from leeroy_mind.selfies.scene import Scene, scene_from, scene_json, scene_renderable
from leeroy_mind.selfies.scene_choices import RECIPE
from leeroy_mind.selfies.store import current, lust_set
from leeroy_mind.selfies.style import LUST, PLAIN, style_of
from .images import ImageRefused, _safe_dir, _write_new, check_jpeg
from . import request_store as records

MAX_GALLERY_CANDIDATES = 128
MAX_RECENT_UNINDEXED = 128


def _score(score, maximum):
    return type(score) in (float, int) and math.isfinite(score) and 0 <= score < maximum


def _member(app, request):
    p = app.personas.get(request.guid)
    cur = current(app.store, request.guid)
    if (p is None or p.appearance is None or cur is None or p.identity.age < ration.SEND_MIN_AGE
            or renderable(p) is not None or app.cfg.talk.selfies <= 0
            or not app.selfies.cfg.enabled or not app.selfies.cfg.snaps
            or app.store.get_meta(f'selfies:variety-pick:{request.guid}') == str(cur.pick)
            or (request.audience == 'anyone' and style_of(p) == LUST)
            or read_image(files.history(app.data_dir, request.guid, cur.pick), app.data_dir) is None):
        return None
    return p, cur


def _images(app, photo):
    image = read_image(files.snap(app.data_dir, photo.guid, photo.id), app.data_dir)
    thumb = read_image(files.snap(app.data_dir, photo.guid, photo.id, thumb=True), app.data_dir)
    if image is None or thumb is None:
        return None
    try:
        check_jpeg(image)
        check_jpeg(thumb)
    except ImageRefused:
        return None
    return image, thumb


def _safe_snap(app, request, photo, *, now, member):
    p, cur = member
    if (photo.guid != request.guid or photo.audience != request.audience
            or photo.status != 'ready' or photo.pick != cur.pick
            or photo.looks != snaps.looks_hash(p.appearance) or now < photo.not_before
            or (photo.source != 'request' and now >= photo.expires)
            or not _score(photo.nsfw, app.selfies.cfg.nsfw_max)):
        return False
    try:
        scene = scene_from(photo.scene)
        return (scene.audience == request.audience and scene_renderable(p, scene) is None
                and (request.audience != 'anyone' or scene.style == PLAIN))
    except (KeyError, ValueError, TypeError):
        return False


def valid(app, request, photo, *, now=None) -> bool:
    """Repeat safety at the delivery transaction, including retained-gallery style provenance."""
    now = app.clock.now() if now is None else now
    request = records.get(app.store, request.message)
    if request is None or request.ended is not None or photo.request != request.message:
        return False
    original = app.store.conn.execute('SELECT channel,deleted FROM discord_message WHERE id=?',
                                      (request.message,)).fetchone()
    if original is None or original['deleted'] is not None or original['channel'] != request.asked_channel:
        return False
    actual = records.snap(app.store, photo.id)
    member = _member(app, request)
    if (actual is None or actual.source != 'request' or actual.request != request.message
            or photo.id not in (request.fresh_snap, request.fallback_snap) or member is None
            or not _safe_snap(app, request, actual, now=now, member=member)):
        return False
    row = app.store.conn.execute('SELECT identity FROM selfie_request_image WHERE snap=?',
                                 (photo.id,)).fetchone()
    if row is not None and row[0].startswith('gallery:'):
        try:
            _, guid, set_no, _ = row[0].split(':')
            was_lust = lust_set(app.store.conn, int(guid), int(set_no))
        except (ValueError, TypeError):
            return False
        if was_lust and style_of(member[0]) != LUST:
            return False
    images = _images(app, actual)
    if images is None:
        return False
    # Another turn may have sent the source after this copy was reserved. Recheck at send,
    # in the same transaction that will commit the new receipt, not only when copying.
    return row is None or not _sent(app, request, identity=row[0], image=images[0], now=now)


def _sent(app, request, *, identity, image, now, cache=None):
    digest = hashlib.sha256(image).hexdigest()
    if records.image_sent(app.store, guid=request.guid, audience=request.audience,
                          identity=identity, digest=digest, now=now):
        return True
    # Ordinary receipts and fresh renders may have no copy identity. Read their retained
    # images; compare bytes rather than snap id so a reused gallery image is recognized too.
    cache = {} if cache is None else cache
    if 'unindexed' not in cache:
        with app.store.transaction():
            rows = app.store.conn.execute('SELECT id FROM selfie_snap s WHERE guid=? AND audience=? '
                "AND status='sent' AND sent>? AND NOT EXISTS "
                '(SELECT 1 FROM selfie_request_image i WHERE i.snap=s.id) ORDER BY sent DESC LIMIT ?',
                (request.guid, request.audience, now - records.REPEAT_WINDOW,
                 MAX_RECENT_UNINDEXED + 1)).fetchall()
        if len(rows) > MAX_RECENT_UNINDEXED:
            # Normal member/ration limits are far below this. An anomalous history must not
            # grant repeat permission merely because its older rows were not inspected.
            cache['unindexed'] = None
        else:
            identities, digests = set(), set()
            for row in rows:
                identities.add(f'snap:{row[0]}')
                previous = read_image(files.snap(app.data_dir, request.guid, row[0]), app.data_dir)
                if previous is not None:
                    digests.add(hashlib.sha256(previous).hexdigest())
            cache['unindexed'] = identities, digests
    prior = cache['unindexed']
    return prior is None or identity in prior[0] or digest in prior[1]


def _gallery_scene(p, audience, was_lust, look=None):
    # A gallery has no situational scene. Describe only what is known: a profile photo.
    # The current profile kind supplies the vetted stored shape, never a fresh-request wish;
    # a varied set's candidate (selfies.varied) records its own composed look instead.
    if look is not None:
        return scene_json(Scene(kind='snapshot', framing=look.framing, setting=look.setting,
            activity='posing for a profile photo', outfit=look.phrase,
            expression=look.expression, when='any', audience=audience,
            shows='a profile photo', style=LUST, recipe=RECIPE))
    photo = p.photo
    return scene_json(Scene(kind='snapshot', framing=photo.framing, setting=photo.setting,
        activity='posing for a profile photo', outfit=photo.outfit, expression=photo.expression,
        when='any', audience=audience, shows='a profile photo',
        style=LUST if was_lust else PLAIN, recipe=RECIPE))


def _varied_look(conn, guid, set_no, idx):
    from leeroy_mind.selfies import varied
    looks = varied.plan_of(conn, guid, set_no)
    return None if looks is None else looks[idx]


def _candidates(app, request, *, now, member):
    for photo in snaps.ready_for(app.store, request.guid, now=now, phase=None):
        if not _safe_snap(app, request, photo, now=now, member=member):
            continue
        images = _images(app, photo)
        if images is not None:
            yield (f'snap:{photo.id}', *images, photo.scene, photo.seed, photo.tries,
                   photo.nsfw, photo.prompt)
    p, cur = member
    with app.store.transaction():
        # A set rendered from an older persona than her cover's set may show looks she no longer has.
        rows = app.store.conn.execute('SELECT c.* FROM selfie_candidate c JOIN selfie_set s '
            "ON s.guid=c.guid AND s.set_no=c.set_no WHERE c.guid=? AND s.status='ready' "
            'AND COALESCE(s.persona_version,0)>=COALESCE((SELECT persona_version FROM selfie_set '
            'WHERE guid=? AND set_no=?),0) ORDER BY s.rendered DESC,c.set_no DESC,c.idx LIMIT ?',
            (request.guid, request.guid, cur.set_no, MAX_GALLERY_CANDIDATES)).fetchall()
    for row in rows:
        was_lust = lust_set(app.store.conn, request.guid, row['set_no'])
        if (not _score(row['nsfw'], app.selfies.cfg.nsfw_max)
                or (was_lust and (style_of(p) != LUST or request.audience != 'him'))):
            continue
        image = read_image(files.candidate(app.data_dir, request.guid, row['set_no'], row['idx']),
                           app.data_dir)
        thumb = read_image(files.candidate(app.data_dir, request.guid, row['set_no'], row['idx'],
                                          thumb=True), app.data_dir)
        if image is None or thumb is None:
            continue
        try:
            check_jpeg(image)
            check_jpeg(thumb)
        except ImageRefused:
            continue
        scene = _gallery_scene(p, request.audience, was_lust,
                               _varied_look(app.store.conn, request.guid, row['set_no'],
                                            row['idx']))
        if scene_renderable(p, scene_from(scene)) is not None:
            continue
        yield (f"gallery:{request.guid}:{row['set_no']}:{row['idx']}", image, thumb,
               scene, row['seed'], row['tries'], row['nsfw'], row['prompt'])


def reserve_fallback(app, request, *, now=None):
    """Copy the first usable retained photo. A failed/partial copy never replaces a file.

    The metadata is bound before disk writes so an interrupted copy cannot reuse an id and
    overwrite its retained files. A ready copy remains bound for restart delivery recovery.
    """
    now = app.clock.now() if now is None else now
    request = records.get(app.store, request.message)
    if request is None or request.ended is not None or now >= request.expires:
        return None
    if request.fallback_snap is not None:
        old = records.snap(app.store, request.fallback_snap)
        return old if old is not None and valid(app, request, old, now=now) else None
    member = _member(app, request)
    if member is None:
        return None
    sent_cache = {}
    for identity, image, thumb, scene, seed, tries, nsfw, prompt in _candidates(
            app, request, now=now, member=member):
        if _sent(app, request, identity=identity, image=image, now=now, cache=sent_cache):
            continue
        p, cur = member
        with app.store.transaction():
            sid = snaps.plan_snap(app.store, kind='situation', guid=request.guid, source='request',
                request=request.message, ref=f'request:{request.message}:fallback', night='request',
                scene=dict(scene), audience=request.audience, phases=records.PHASES,
                not_before=now, expires=request.expires, now=now,
                pick=cur.pick, looks=snaps.looks_hash(p.appearance))
            if sid is None:
                return None
            records.bind_snap(app.store, request.message, sid, kind='fallback')
            records.remember_image(app.store, sid, identity=identity,
                                   digest=hashlib.sha256(image).hexdigest())
        out = files.snap(app.data_dir, request.guid, sid)
        small = files.snap(app.data_dir, request.guid, sid, thumb=True)
        try:
            _safe_dir(app.data_dir, out.parent.relative_to(app.data_dir), create=True)
            _write_new(out, image)
            _write_new(small, thumb)
            _safe_dir(app.data_dir, out.parent.relative_to(app.data_dir), create=False)
            width, height = check_jpeg(image)
            snaps.mark_rendered(app.store, sid, width=width, height=height, seed=seed, tries=tries,
                                nsfw=nsfw, prompt=prompt, now=now)
            photo = records.snap(app.store, sid)
            if valid(app, request, photo, now=now):
                return photo
        except (OSError, ImageRefused):
            pass
        snaps.expire(app.store, sid, 'missing', now=now)
        return None
    return None
