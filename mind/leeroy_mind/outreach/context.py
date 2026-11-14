"""The exact unanswered DM behind a nudge, within the ordinary pair-context budget."""
from leeroy_mind.discord import layout
from . import store as records


def nudge_lines(outreach, turn, meta, pair, recent, *, limit):
    item = records.get(outreach.app.store, meta.outreach_item)
    if item is None or item.kind != 'dm_nudge' or item.state != 'open':
        return pair
    outreach._via(turn, item)  # no source text is read before the pair boundary
    if not outreach._alive(item):
        return pair
    store = outreach.app.store
    mid = int(item.ref.removeprefix('dm_nudge:'))
    mapped = store.get_meta(f'discord:line:{mid}')
    if not mapped or not mapped.isdecimal() or len(mapped) > 18:
        return pair
    rows = store.chat_lines([int(mapped)])
    source = store.conn.execute('SELECT text FROM discord_message WHERE id=? AND deleted IS NULL',
                                (mid,)).fetchone()
    if not rows or source is None:
        return pair
    line = rows[0]
    if (layout.dm_peer(line.channel) != item.guid or line.from_guid != item.guid
            or line.to_guid != outreach.app.human_guid or line.origin != 'mind'
            or line.text != source[0] or line.id in {r.id for r in recent}):
        return pair
    # Nudges can refer to a three-day-old DM; the normal other-medium window is
    # one day. Add only this verified source, retaining the same row/token limits.
    rest = [r for r in pair if r.id != line.id]
    kept = rest[-(limit-1):] if limit > 1 else []
    return sorted([line, *kept], key=lambda r: (r.ts, r.id))
