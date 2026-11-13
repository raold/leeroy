from conftest import run
from server.test_engine import app, finished
from leeroy_mind.groups import store as groups
from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups.conversations import day_key
from leeroy_mind.server.seeds import Seed


def test_engine_group_runs_two_to_four_lines_only_between_its_members(app,monkeypatch):
    app.groups.config=GroupsConfig(enabled=True)
    channel=groups.create(app.store,human=1,members=[101,102],title='Fixture group',
        created_by=1,now=app.clock.now(),config=app.groups.config)
    app.observed.group_audiences={channel:frozenset({1,101,102})}
    app.observed.seeds=[Seed('group:1:life','life',(channel,),'A bread recipe',(102,))]
    original=app.engine.speak_meta
    async def checked(turn,meta):
        assert meta.still_valid()
        return await original(turn,meta)
    monkeypatch.setattr(app.engine,'speak_meta',checked)
    async def go():
        await app.server.engine.tick(app.clock.now())
        assert len(app.server.engine.live())==1
        await finished(app.server.engine)
    run(go())
    rows=app.store.conn.execute('SELECT author,channel FROM discord_message ORDER BY id').fetchall()
    assert 2<=len(rows)<=4
    assert rows[0]['author']==102
    assert {r['author'] for r in rows}=={101,102}
    assert all(r['channel']==channel for r in rows)
    assert app.store.get_meta(day_key(app.clock.local().date().isoformat(),channel))=='1'
    assert all(t.channel==channel and m.review_required for t,m in app.turns)
