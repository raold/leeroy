"""Short image observations for already audience-filtered history rows.

The join independently verifies the image's actual outlet. Private/lust scenes cannot become
channel evidence even if an attachment was linked incorrectly. No filenames enter prompts.
"""
import json
from .look import photo_note


def _snaps(conn, column, ids):
    ids = tuple(dict.fromkeys(ids))
    if not ids:
        return {}
    rows = conn.execute(f'SELECT {column},s.scene,s.audience,s.guid,m.channel FROM selfie_snap s '
        'JOIN discord_attachment a ON a.snap=s.id JOIN discord_message m ON m.id=a.message '
        "JOIN meta receipt ON receipt.key='discord:line:'||m.id AND receipt.value=CAST(s.line_id AS TEXT) "
        'JOIN chat_line c ON c.id=s.line_id AND c.from_guid=m.author '
        f'WHERE {column} IN (SELECT value FROM json_each(?)) AND s.status=\'sent\' '
        "AND a.kind='snap' AND m.deleted IS NULL AND m.author=s.guid AND s.sender=s.guid "
        "AND ((s.audience='him' AND s.channel='dm' AND m.channel='dm:'||s.guid AND c.channel=m.channel "
        "AND c.to_guid=CAST((SELECT value FROM meta WHERE key='human_guid') AS INTEGER)) "
        "OR (s.audience='anyone' AND s.channel='discord' AND m.channel IN ('food','pets','irl','pics') "
        "AND c.channel='discord:'||m.channel))",
        (json.dumps(ids),))
    out = {}
    for key, raw, audience, guid, channel in rows:
        try:
            scene = json.loads(raw)
            if scene.get('audience') != audience:
                continue
            if audience == 'anyone' and scene.get('style','plain') != 'plain':
                continue
            if isinstance(scene.get('shows'),str):
                out[key] = photo_note(scene['shows'])
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def by_messages(conn, ids, *, uploads=False, members=False):
    ids = tuple(ids)
    out = _snaps(conn,'m.id',ids) if members else {}
    if uploads and ids:
        from leeroy_mind.discord.store import attachments
        out.update({mid:photo_note(row['look']) for mid,row in attachments(conn,ids).items()
                    if row['kind'] == 'upload'})
    return out


def by_lines(conn, ids):
    return _snaps(conn,'s.line_id',ids)
