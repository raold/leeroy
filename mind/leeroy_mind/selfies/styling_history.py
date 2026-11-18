"""Bounded private photo history and the owner's current explicit feedback.

No writes or text logging. Re-reading the source votes makes withdrawal, deletion and a
changed vote effective on the next choice. Public photos never teach a private preference.
"""
import json
from dataclasses import dataclass

from leeroy_mind.discord.channels import DM_PREFIX, WHISPER
from leeroy_mind.learn.control import effective

LIMIT = 64
RECENT = 8
WINDOW = 90 * 86400
FIELDS = ("outfit", "expression", "romance_pose", "romance_light")


@dataclass(frozen=True)
class History:
    recent: tuple[dict, ...] = ()
    rated: tuple[tuple[dict, int], ...] = ()


def read(store, guid: int, owner: int, *, now: float, learn=None) -> History:
    """The newest 64 private recipes within 90 days; eight for repeat avoidance. Feedback
    needs a sent photo's exact private line, and on Discord its live attachment and receipt.
    Only an owner's thumbs-up/down reaction or the existing feedback control gives credit.
    A control vote takes precedence; opposite emoji together count as a downvote.
    """
    with store.transaction():
        conn = store.conn
        rows = conn.execute(
            "SELECT id,scene,status,line_id,channel,sender FROM selfie_snap WHERE guid=? "
            "AND kind='situation' AND audience='him' AND status IN ('planned','ready','sent') "
            "AND created BETWEEN ? AND ? AND (status!='sent' OR sent<=?) ORDER BY id DESC LIMIT ?",
            (guid, now - WINDOW, now, now, LIMIT)).fetchall()
        scenes = {}
        for row in rows:
            try:
                data = json.loads(row['scene'])
                if data.get('audience') != 'him' or not data.get('romance_style'):
                    continue
                scenes[row['id']] = {k: data.get(k, '') for k in FIELDS
                                     if isinstance(data.get(k, ''), str)}
            except (ValueError, TypeError, AttributeError):
                continue
        if not scenes:
            return History()
        cfg = effective(conn, learn)
        apply = cfg.apply and not cfg.freeze and guid not in cfg.frozen_members
        # The join is restricted to these recipe IDs. It verifies both stores' provenance,
        # never matching nearby timestamps, text or the numeric IDs of different tables.
        posts = conn.execute(
            "SELECT s.id,m.id AS message FROM selfie_snap s "
            "JOIN discord_attachment a ON a.snap=s.id AND a.kind='snap' "
            "JOIN discord_message m ON m.id=a.message "
            "JOIN meta receipt ON receipt.key='discord:line:'||m.id "
            "AND receipt.value=CAST(s.line_id AS TEXT) "
            "JOIN chat_line c ON c.id=s.line_id "
            "WHERE s.id IN (SELECT value FROM json_each(?)) AND s.status='sent' "
            "AND s.channel='dm' AND s.sender=? AND m.author=? AND m.origin='mind' "
            "AND m.channel=? AND m.deleted IS NULL AND m.ts<=? "
            "AND c.channel=m.channel AND c.from_guid=? AND c.to_guid=? AND c.origin='mind'",
            (json.dumps(list(scenes)), guid, guid, DM_PREFIX + str(guid), now, guid, owner)).fetchall()
        messages = {r['id']: r['message'] for r in posts}
        recent, rated = [], []
        for row in rows:
            scene = scenes.get(row['id'])
            if scene is None:
                continue
            line = None
            if row['status'] == 'sent':
                line = conn.execute("SELECT channel,from_guid,to_guid,origin FROM chat_line WHERE id=? AND ts<=?",
                                    (row['line_id'], now)).fetchone()
                if (line is None or line['from_guid'] != guid or line['to_guid'] != owner
                        or line['origin'] != 'mind' or row['sender'] != guid):
                    continue
                if row['channel'] == 'dm':
                    if row['id'] not in messages:
                        continue
                elif row['channel'] != WHISPER or line['channel'] != WHISPER:
                    continue
            if len(recent) < RECENT:
                recent.append(scene)
            if not apply or line is None:
                continue
            vote = conn.execute(
                "SELECT value FROM talk_signal WHERE line_id=? AND guid=? AND kind='explicit' "
                "AND source='explicit' AND ts<=?", (row['line_id'], guid, now)).fetchone()
            value = int(vote[0]) if vote and vote[0] in (-1, 1) else 0
            if not value and row['id'] in messages:
                emoji = {r[0].translate(dict.fromkeys([0xfe0f, *range(0x1f3fb, 0x1f400)]))
                         for r in conn.execute("SELECT emoji FROM discord_reaction "
                             "WHERE message=? AND member=? AND ts<=?",
                             (messages[row['id']], owner, now))}
                value = -1 if '👎' in emoji else 1 if '👍' in emoji else 0
            if value:
                rated.append((scene, value))
        return History(tuple(recent), tuple(rated))
