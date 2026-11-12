import sqlite3

import pytest

from leeroy_mind.discord import store as ds
from leeroy_mind.discord.store import AttachmentIn


def photo(**over):
    return AttachmentIn(**(dict(kind='upload', path='discord/images/1.jpg', width=32,
                                height=24, bytes=100) | over))


def post(store, **over):
    return ds.post(store, **(dict(channel='pics', author=1, origin='human', text='', now=1,
                                  attachment=photo()) | over))


def test_photo_only_post_and_text_only_still_needs_text(store):
    mid = post(store)
    assert ds.attachments(store.conn, [mid])[mid]['width'] == 32
    assert ds.messages(store.conn, 'pics')[0]['text'] == ''
    with pytest.raises(ds.DiscordRefused, match='empty'):
        post(store, attachment=None)


def test_attachment_failure_rolls_back_message(store):
    with pytest.raises(sqlite3.IntegrityError):
        post(store, attachment=photo(kind='snap'))
    assert ds.messages(store.conn, 'pics') == []
    with pytest.raises(sqlite3.IntegrityError):
        post(store, attachment=photo(snap=1))
    assert ds.messages(store.conn, 'pics') == []


def test_deleted_message_hides_look(store):
    mid = post(store)
    ds.set_look(store, mid, 'A dog on a rug.', now=2)
    assert ds.attachments(store.conn, [mid])[mid]['look'] == 'A dog on a rug.'
    ds.delete(store, mid, author=1, now=3)
    assert mid not in ds.attachments(store.conn, [mid])
    ds.set_look(store, mid, 'Late result.', now=4)
    assert store.conn.execute('SELECT look FROM discord_attachment').fetchone()[0] == 'A dog on a rug.'


@pytest.mark.parametrize('origin,author,bot,valid', [
    ('bot', 0, 'loot', True), ('bot', 1, 'loot', False), ('bot', 0, None, False),
    ('mind', 101, 'loot', False), ('human', 0, None, False), ('bot', 0, '', False),
])
def test_bot_schema(origin, author, bot, valid, store):
    def insert():
        with store.transaction():
            store.conn.execute('INSERT INTO discord_message(channel,author,origin,bot,text,ts) '
                               'VALUES(?,?,?,?,?,?)', ('pics', author, origin, bot, 'hello', 1))
    if valid:
        insert()
    else:
        with pytest.raises(sqlite3.IntegrityError):
            insert()
