"""Group membership changes commit together; historical readers never change."""
import sqlite3
import logging

from leeroy_mind.discord.channels import GROUP_PREFIX, is_group
from leeroy_mind.discord.store import DiscordRefused
from leeroy_mind.discord.text import clean_name

log = logging.getLogger(__name__)

WORDS = {
    'groups_off': 'Group chats are turned off. Enable groups and restart the mind and console.',
    'group_missing': 'That group chat does not exist.',
    'group_closed': 'That group chat is closed. Its history is still readable.',
    'group_members': 'Choose two to four different guild members.',
    'group_full': 'That group already has four members besides you.',
    'groups_full': 'There are too many open groups. Leave one before making another.',
    'group_name': 'Use a group name of 1 to 40 characters.',
    'group_member': 'That member is not in this group.',
}


def ident(channel):
    if not is_group(channel):
        return None
    value = channel[len(GROUP_PREFIX):]
    return int(value) if len(value)<=19 and value.isascii() and value.isdecimal() and 0 < int(value) < 2**63 else None


def get(conn, channel):
    gid = ident(channel)
    if gid is None:
        return None
    try:
        return conn.execute('SELECT * FROM discord_group WHERE id=?',(gid,)).fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' in str(error):
            return None
        raise


def readers(conn, channel, *, at=None, include_closed=False):
    group = get(conn,channel)
    if group is None or (not include_closed and group['closed'] is not None):
        return frozenset()
    where = 'left IS NULL' if at is None else 'joined<=? AND (left IS NULL OR left>?)'
    args = (group['id'],) if at is None else (group['id'],at,at)
    return frozenset(r[0] for r in conn.execute(f'SELECT guid FROM discord_group_member WHERE grp=? AND {where}',args))


def _open(store, channel):
    row = get(store.conn, channel)
    if row is None:
        raise DiscordRefused('group_missing')
    if row['closed'] is not None:
        raise DiscordRefused('group_closed')
    return row


def name(value):
    try:
        value = clean_name(value)
    except (TypeError,ValueError):
        raise DiscordRefused('group_name') from None
    if not 1 <= len(value) <= 40:
        raise DiscordRefused('group_name')
    return value


def create(store, *, human, members, title, created_by, now, config):
    if not config.enabled:
        raise DiscordRefused('groups_off')
    members = tuple(members)
    if (not 2 <= len(members) <= 4 or len(set(members)) != len(members)
            or human in members or any(type(g) is not int or g <= 0 for g in members)
            or created_by not in (human,*members)):
        raise DiscordRefused('group_members')
    title = name(title or 'Group chat')
    with store.transaction():
        if store.conn.execute('SELECT count(*) FROM discord_group WHERE closed IS NULL').fetchone()[0] >= config.max_open:
            raise DiscordRefused('groups_full')
        gid = store.conn.execute('INSERT INTO discord_group(name,created_by,created) VALUES(?,?,?)',
                                 (title,created_by,now)).lastrowid
        store.conn.executemany('INSERT INTO discord_group_member(grp,guid,joined) VALUES(?,?,?)',
                              [(gid,g,now) for g in (human,*members)])
    log.info('group_changed action=create group=%d actor=%d members=%d',gid,created_by,len(members)+1)
    return GROUP_PREFIX+str(gid)


def add(store, channel, guid, *, human, now):
    with store.transaction():
        group = _open(store, channel)
        have = readers(store.conn, channel)
        if guid in have:
            return
        if human not in have or len(have) >= 5:
            raise DiscordRefused('group_full')
        # A remove and re-add at one clock instant still needs a new period.
        prior = store.conn.execute('SELECT max(joined) FROM discord_group_member WHERE grp=? AND guid=?',
                                   (group['id'],guid)).fetchone()[0]
        joined = max(now,prior+0.000001) if prior is not None else now
        store.conn.execute('INSERT INTO discord_group_member(grp,guid,joined) VALUES(?,?,?)',(group['id'],guid,joined))
    log.info('group_changed action=add group=%d guid=%d members=%d',group['id'],guid,len(have)+1)


def membership_epoch(conn, channel):
    """Every join and departure advances the epoch, even at one clock instant."""
    return conn.execute('SELECT count(*)+coalesce(sum(left IS NOT NULL),0) '
                        'FROM discord_group_member WHERE grp=?',(ident(channel),)).fetchone()[0]


def remove(store, channel, guid, *, human, now):
    with store.transaction():
        group = _open(store, channel)
        if guid not in readers(store.conn,channel):
            raise DiscordRefused('group_member')
        store.conn.execute('UPDATE discord_group_member SET left=? WHERE grp=? AND guid=? AND left IS NULL',
                           (now,group['id'],guid))
        if guid == group['created_by']:
            # Her unspoken introduction leaves with her; the remaining friends
            # may still use a group large enough to stay open.
            store.conn.execute('DELETE FROM meta WHERE key=?',(f'groups:first:{group["id"]}',))
        if guid == human or len(readers(store.conn,channel)) < 3:
            store.conn.execute('UPDATE discord_group SET closed=? WHERE id=?',(now,group['id']))
    log.info('group_changed action=remove group=%d guid=%d',group['id'],guid)


def rename(store, channel, title):
    title = name(title)
    with store.transaction():
        group = _open(store,channel)
        store.conn.execute('UPDATE discord_group SET name=? WHERE id=?',(title,group['id']))
    log.info('group_changed action=rename group=%d',group['id'])
