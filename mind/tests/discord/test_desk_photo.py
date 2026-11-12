from pathlib import Path

import pytest

from conftest import run, seed_test_cast
from discord.test_discord_desk import Talk, desk_app
from leeroy_mind.photos import images, uploads


@pytest.fixture
def app(store, clock):
    root = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    (root/'mind.toml').write_text('[photos]\nenabled=true')
    seed_test_cast(store)
    return desk_app(store, clock, data_dir=root, discord_talk=Talk())


def args(app):
    from photos.test_images import jpeg
    return {'action':'post', 'channel':'pics', 'text':'', 'image':images.stage(app.data_dir, jpeg())}


def test_reused_token_writes_no_second_post(app):
    post = args(app)
    assert run(app.discord.handle(post))['ok']
    assert not run(app.discord.handle(post))['ok']
    assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == 1
    assert uploads.day_count(app.store.conn, app.clock.local().date().isoformat()) == 1


def test_daily_quota_survives_restart_and_leaves_staging(app):
    from leeroy_mind.discord.desk import DiscordDesk
    post = args(app)
    day = app.clock.local().date().isoformat()
    app.store.set_meta(uploads.DAY_KEY.format(day), '200')
    before = {p:p.read_bytes() for p in (app.data_dir/'discord').rglob('*') if p.is_file()}
    result = run(DiscordDesk(app).handle(post))
    assert not result['ok'] and '200' in result['error']
    assert before == {p:p.read_bytes() for p in (app.data_dir/'discord').rglob('*') if p.is_file()}
    assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == 0


def test_bad_parent_leaves_token_usable(app):
    post = args(app)
    assert not run(app.discord.handle(post | {'reply_to':99}))['ok']
    assert run(app.discord.handle(post))['ok']


def test_rollback_keeps_files_and_does_not_block_next_post(app, monkeypatch):
    original = app.discord._his_line
    def fail(*args):
        raise RuntimeError('synthetic post failure')
    monkeypatch.setattr(app.discord, '_his_line', fail)
    with pytest.raises(RuntimeError):
        run(app.discord.handle(args(app)))
    assert not app.store.conn.execute('SELECT * FROM discord_message').fetchall()
    before = {p:p.read_bytes() for p in (app.data_dir/'discord').rglob('*') if p.is_file()}
    monkeypatch.setattr(app.discord, '_his_line', original)
    assert run(app.discord.handle(args(app)))['ok']
    assert all(p.read_bytes() == b for p,b in before.items())


def test_disabled_desk_refuses_upload_token(app):
    from leeroy_mind.discord.desk import DiscordDesk
    post = args(app)
    (app.data_dir/'mind.toml').write_text('')
    assert not run(DiscordDesk(app).handle(post))['ok']


def test_mind_enforces_four_posts_per_minute(app):
    for _ in range(4):
        assert run(app.discord.handle(args(app)))['ok']
    assert not run(app.discord.handle(args(app)))['ok']
