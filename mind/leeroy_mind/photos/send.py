"""Reserve a retained, screened photo only when its Discord turn actually composes.

Reservations are loop-owned; durable spending belongs to the pipeline's message transaction.
No worker, model, GPU or file writer is started here.
"""
import hashlib
import math

from leeroy_mind.discord.store import AttachmentIn
from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import channel_id, is_discord, is_private
from leeroy_mind.imagefiles import read_image
from leeroy_mind.logs import log
from leeroy_mind.selfies import files, ration, snaps
from leeroy_mind.selfies.scene import scene_from, scene_renderable
from leeroy_mind.selfies.scene_choices import channel_for
from leeroy_mind.selfies.store import current
from leeroy_mind.talk.budget import day_phase
from .offer import PhotoOffer

MAX_IN_FLIGHT = 1


def day_draw(date: str) -> float:
    return int.from_bytes(hashlib.sha256(('photos:'+date).encode()).digest()[:8], 'big') / 2**64


def channel_count(conn, date: str) -> int:
    row = conn.execute('SELECT value FROM meta WHERE key=?', (ration.CHANNEL_KEY.format(date=date),)).fetchone()
    if row is None:
        return 0
    try:
        n = int(row[0])
        return n if n >= 0 else 2**31
    except (ValueError, TypeError):
        return 2**31  # damaged accounting never grants another slot


class PhotoSender:
    def __init__(self, app, config, rng):
        self.app = app
        self.config, self.rng = config, rng
        self._in_flight: dict[int, str] = {}

    def _enabled(self):
        app = self.app
        return (self.config.send and not app.stopping and app.selfies.cfg.enabled and app.selfies.cfg.snaps
                and app.server is not None and app.server.config.enabled)

    def _room(self, audience, now, *, own=None):
        app, dial = self.app, self.app.cfg.talk.selfies
        pending = sum(a == audience and sid != own for sid, a in self._in_flight.items())
        if audience == 'him':
            return ration.room(dial, week=snaps.sent_count(app.store, now-ration.WEEK_S, to_him=True)+pending,
                               day=snaps.sent_count(app.store, now-ration.DAY_S, to_him=True)+pending)
        date = app.clock.local().date().isoformat()
        return ration.channel_room(dial, today=channel_count(app.store.conn, date)+pending, draw=day_draw(date))

    def _fresh(self, snap, now):
        app = self.app
        p = app.personas.get(snap.guid)
        cur = current(app.store, snap.guid)
        why = None
        if not snap.not_before <= now < snap.expires:
            why = 'passed' if now >= snap.expires else None
            if why is None:
                return False
        elif cur is None or cur.pick != snap.pick:
            why = 'photo_changed'
        elif p is None or p.appearance is None or snaps.looks_hash(p.appearance) != snap.looks:
            why = 'looks_changed'
        elif (app.store.get_meta(f'selfies:variety-pick:{snap.guid}') == str(cur.pick)
              or p.identity.age < ration.SEND_MIN_AGE
              or type(snap.nsfw) not in (int, float) or not math.isfinite(snap.nsfw)
              or snap.nsfw >= app.selfies.cfg.nsfw_max):
            why = 'flagged'
        else:
            try:
                scene = scene_from(snap.scene)
                if (scene.audience != snap.audience or scene_renderable(p, scene)
                        or (snap.audience == 'anyone' and scene.style != 'plain')):
                    why = 'flagged'
            except (KeyError, ValueError, TypeError):
                why = 'flagged'
        if why is not None:
            snaps.expire(app.store, snap.id, why, now=now)
            return False
        return day_phase(app.clock.local()).value in snap.phases

    def _attachment(self, snap):
        app = self.app
        path = files.snap(app.data_dir, snap.guid, snap.id)
        data = read_image(path, app.data_dir)
        thumb = read_image(files.snap(app.data_dir, snap.guid, snap.id, thumb=True), app.data_dir)
        if data is None or thumb is None or not snap.width or not snap.height:
            snaps.expire(app.store, snap.id, 'missing', now=app.clock.now())
            return None
        return AttachmentIn('snap', path.relative_to(app.data_dir).as_posix(), snap.width,
                            snap.height, len(data), snap.id)

    def _channel(self, turn, meta):
        app = self.app
        if not is_discord(turn.channel):
            return None
        cid = channel_id(turn.channel)
        if layout.dm_peer(cid) == turn.speaker and turn.to_guid == app.human_guid:
            if meta.why == 'server:dm':
                return 'him'
            if meta.why == 'discord:reply' and meta.discord is not None:
                row = app.store.conn.execute('SELECT author,channel,deleted FROM discord_message WHERE id=?',
                    (meta.discord.reply_to,)).fetchone()
                if row and row[0] == app.human_guid and row[1] == cid and row[2] is None:
                    return 'him'
        if (meta.why == 'server:photo' and meta.photo_seed and not is_private(cid)
                and layout.channel(cid) is not None):
            return 'anyone'
        return None

    def offer(self, turn, meta, now):
        app, dial = self.app, self.app.cfg.talk.selfies
        audience = self._channel(turn, meta)
        if not self._enabled() or audience is None or dial <= 0 or len(self._in_flight) >= MAX_IN_FLIGHT or not self._room(audience, now):
            return None
        last = snaps.last_sent(app.store, turn.speaker)
        if last is not None and now-last < ration.MEMBER_GAP_S:
            return None
        for snap in snaps.ready_for(app.store, turn.speaker, now=now, phase=day_phase(app.clock.local()).value):
            if snap.audience != audience or (audience == 'anyone' and
                    (snap.id != meta.photo_seed or channel_id(turn.channel) != channel_for(snap.scene))):
                continue
            if not self._fresh(snap, now):
                continue
            shows = snap.scene.get('shows')
            if not isinstance(shows, str) or not shows:
                continue  # never reserve a photo whose offer cannot be built
            attachment = self._attachment(snap)
            if attachment is None:
                continue
            if audience == 'him':
                p = app.personas.get(turn.speaker)
                weight = ration.send_weight(p, app.store.get_relationship(turn.speaker, app.human_guid))
                if self.rng.random() >= ration.chance('reply' if meta.why == 'discord:reply' else 'whisper', weight):
                    return None
            self._in_flight[snap.id] = audience
            photo = None
            released = False
            def release():
                nonlocal released
                if released:
                    return
                released = True
                self._in_flight.pop(snap.id, None)
                log('photo_released', snap=snap.id, code=photo.reason if photo else 'withdrawn')
            def valid():
                moment = app.clock.now()
                if (not self._enabled() or self._in_flight.get(snap.id) != audience
                        or self._channel(turn, meta) != audience or not self._room(audience, moment, own=snap.id)):
                    return False
                row = app.store.conn.execute('SELECT status FROM selfie_snap WHERE id=?', (snap.id,)).fetchone()
                last = snaps.last_sent(app.store, turn.speaker)
                return bool(row and row[0] == 'ready' and (last is None or moment-last >= ration.MEMBER_GAP_S)
                            and self._fresh(snap, moment) and self._attachment(snap) is not None)
            def sent(line_id, msg_id):
                if not snaps.mark_sent(app.store, snap.id, sender=turn.speaker,
                                       channel='dm' if audience == 'him' else 'discord',
                                       line_id=line_id, now=app.clock.now()):
                    raise snaps.SnapError('photo is no longer ready')
                if audience == 'anyone':
                    date = app.clock.local().date().isoformat()
                    app.store.set_meta(ration.CHANNEL_KEY.format(date=date), str(channel_count(app.store.conn,date)+1))
                self._in_flight.pop(snap.id, None)
            photo = PhotoOffer(snap, "You're posting a photo of yourself: "+shows+
                ". Write the short message you'd post with it, in your own way; don't describe the photo in detail.",
                sent, release, attachment, valid)
            log('photo_offered', snap=snap.id, channel_kind='dm' if audience == 'him' else 'discord')
            return photo
        return None


def photo_seeds(conn, *, now, local, dial, today=None):
    """Bounded, read-only candidates. Admission repeats file, persona and budget checks."""
    from leeroy_mind.server.seeds import Seed
    date = local.date().isoformat()
    if not ration.channel_room(dial, today=channel_count(conn,date) if today is None else today, draw=day_draw(date)):
        return []
    # Her member gap (offer() checks it too): a seed its opener cannot post is no seed.
    rows = conn.execute(f'SELECT {snaps._COLS} FROM selfie_snap s WHERE kind=\'situation\' '
        "AND audience='anyone' AND status='ready' AND not_before<=? AND expires>? "
        "AND NOT EXISTS (SELECT 1 FROM selfie_snap t WHERE t.kind='situation' AND t.status='sent' "
        "AND t.sender=s.guid AND t.sent>?) ORDER BY expires,id LIMIT 200",
        (now,now,now-ration.MEMBER_GAP_S)).fetchall()
    out = []
    for row in rows:
        try:
            snap = snaps._row(row)
            if (snap.scene.get('audience') != 'anyone' or snap.scene.get('style','plain') != 'plain'
                    or day_phase(local).value not in snap.phases):
                continue
            shows = snap.scene['shows']
            if not isinstance(shows,str) or not shows:
                continue
            out.append(Seed(f'photo:{snap.id}', 'photo', (channel_for(snap.scene),), shows, (snap.guid,)))
        except (KeyError, ValueError, TypeError):
            continue
    return out
