"""The desk hands his Discord posts and emoji to the owner-response signals as ids and flags
(discord.desk signal_post, _signal_reactions). Scratch databases and invented text only."""
from conftest import HUMAN
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import LINE_KEY
from leeroy_mind.protocol import Chat

from .test_discord_desk import Talk, ask, desk_app, seed_test_cast


class Seen:
    def __init__(self):
        self.calls = []

    def his_discord(self, line, **flags):
        self.calls.append(('post', line, flags))

    def reacted(self, line, **flags):
        self.calls.append(('react', line, flags))


def member_message(app, channel, guid=101, text='an invented member post'):
    """A member's Discord message with its chat line and receipt, as the pipeline stores one."""
    store, now = app.store, app.clock.now()
    with store.transaction():
        mid = dstore.post(store, channel=channel, author=guid, origin='mind', text=text, now=now)
        place = channel if channel.startswith('dm:') else 'discord:' + channel
        line = store.insert_chat_line(Chat(0, place, guid, 'Arkon', HUMAN if channel.startswith('dm:') else 0,
                                           text, 0, 0), session_id=None, origin='mind', ts=now)
        store.set_meta(LINE_KEY.format(mid), str(line))
    return mid, line


def app_with_signals(store, clock):
    seed_test_cast(store)
    return desk_app(store, clock, discord_talk=Talk(), signals=Seen())


def test_his_post_reaches_the_signals_after_its_commit_with_ids_and_flags_only(store, clock):
    app = app_with_signals(store, clock)
    for channel in ('dm:101', 'general'):
        mid, line = member_message(app, channel)
        plain = ask(app, action='post', channel=channel, text='INVENTEDWORDS of mine')
        reply = ask(app, action='post', channel=channel, text='INVENTEDWORDS again', reply_to=mid)
        mine = [int(app.store.get_meta(LINE_KEY.format(r['id']))) for r in (plain, reply)]
        assert app.signals.calls[-2:] == [
            ('post', mine[0], dict(reply_line=0, media=False, voice=False)),
            ('post', mine[1], dict(reply_line=line, media=False, voice=False))]
        # committed before the hook: the capture's own connection must find the line
        assert not app.store.conn.in_transaction or app.store.conn.execute(
            'SELECT 1 FROM chat_line WHERE id=?', (mine[1],)).fetchone()
    assert 'INVENTEDWORDS' not in repr(app.signals.calls)


def test_his_emoji_reach_the_signals_as_they_stand(store, clock):
    app = app_with_signals(store, clock)
    mid, line = member_message(app, 'dm:101')
    assert ask(app, action='react', message=mid, emoji='❤️') == {'ok': True}
    assert ask(app, action='react', message=mid, emoji='👎') == {'ok': True}
    assert ask(app, action='unreact', message=mid, emoji='❤️') == {'ok': True}
    assert ask(app, action='unreact', message=mid, emoji='👎') == {'ok': True}
    assert app.signals.calls == [
        ('react', line, dict(positive=True, negative=False)),
        ('react', line, dict(positive=True, negative=True)),
        ('react', line, dict(positive=False, negative=True)),
        ('react', line, dict(positive=False, negative=False))]


def test_a_failing_signal_hook_never_stops_a_post_and_no_signals_is_fine(store, clock):
    app = app_with_signals(store, clock)

    def boom(*a, **k):
        raise RuntimeError('invented failure')
    app.signals.his_discord = app.signals.reacted = boom
    mid, _ = member_message(app, 'general')
    assert ask(app, action='post', channel='general', text='still posted')['ok'] is True
    assert ask(app, action='react', message=mid, emoji='👍') == {'ok': True}
    app.signals = None
    assert ask(app, action='post', channel='general', text='and again')['ok'] is True
    assert ask(app, action='unreact', message=mid, emoji='👍') == {'ok': True}
