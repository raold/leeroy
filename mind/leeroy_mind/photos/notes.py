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
    request_outlet = '0'
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='selfie_request'").fetchone():
        request_outlet = ("(s.source='request' AND EXISTS (SELECT 1 FROM selfie_request r "
            "WHERE r.message=s.request AND r.guid=s.guid AND r.audience='anyone' "
            "AND r.photo_channel=m.channel AND r.asked_channel=m.channel "
            "AND ((r.fresh_snap=s.id AND r.fresh_message=m.id) OR "
            "(r.fallback_snap=s.id AND r.fallback_message=m.id))))")
    rows = conn.execute(f'SELECT {column},s.scene,s.audience,c.channel FROM selfie_snap s '
        'JOIN discord_attachment a ON a.snap=s.id JOIN discord_message m ON m.id=a.message '
        "JOIN meta receipt ON receipt.key='discord:line:'||m.id AND receipt.value=CAST(s.line_id AS TEXT) "
        'JOIN chat_line c ON c.id=s.line_id AND c.from_guid=m.author '
        f'WHERE {column} IN (SELECT value FROM json_each(?)) AND s.status=\'sent\' '
        "AND a.kind='snap' AND m.deleted IS NULL AND m.author=s.guid AND s.sender=s.guid "
        "AND ((s.audience='him' AND s.channel='dm' AND m.channel='dm:'||s.guid AND c.channel=m.channel "
        "AND c.to_guid=CAST((SELECT value FROM meta WHERE key='human_guid') AS INTEGER)) "
        f"OR (s.audience='anyone' AND s.channel='discord' AND (m.channel IN ('food','pets','irl','pics') OR {request_outlet}) "
        "AND c.channel='discord:'||m.channel))",
        (json.dumps(ids),))
    out = {}
    for key, raw, audience, line_channel in rows:
        try:
            scene = json.loads(raw)
            if scene.get('audience') != audience:
                continue
            if audience == 'anyone' and scene.get('style','plain') != 'plain':
                continue
            if isinstance(scene.get('shows'),str):
                out[key] = (photo_note(scene['shows']), line_channel)
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def by_messages(conn, ids, *, uploads=False, members=False):
    ids = tuple(ids)
    out = {mid: note for mid, (note, _) in _snaps(conn,'m.id',ids).items()} if members else {}
    if uploads and ids:
        from leeroy_mind.discord.store import attachments
        out.update({mid:photo_note(row['look']) for mid,row in attachments(conn,ids).items()
                    if row['kind'] == 'upload'})
    return out


def by_lines(conn, rows):
    """Notes for chat lines given as (chat_line id, its channel) pairs. Only chat_line ids belong
    here, never Discord message ids (by_messages); a row whose channel is not the photo line's own
    never takes its note."""
    rows = {(i, ch) for i, ch in rows}
    return {key: note for key, (note, channel) in _snaps(conn,'s.line_id',[i for i, _ in rows]).items()
            if (key, channel) in rows}
