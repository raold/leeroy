from leeroy_mind.voice.rooms import ROOMS


def test_every_room_but_the_quiet_ones_has_a_topic():
    quiet = {'class_halls', 'afk', 'private1', 'private2', 'private3', 'penalty_box'}
    assert all(bool(r.topic) == (r.id not in quiet) for r in ROOMS)


def test_elsewhere_carries_the_topic_only_with_server_on(vrig):
    import random
    from conftest import run
    from leeroy_mind.voice.elsewhere import ElsewhereTalk
    v = vrig
    v.him('guild_hall')
    v.ext.comms = v.comms
    v.ext.on_comms = {103,104}
    for g in v.ext.on_comms:
        v.seat(g,'lore')
    seen = []
    async def capture(turn, **kw):
        seen.append((turn.instruction,kw['topic']))
        return []
    v.engine.speak = capture
    for server,room in ((None,'lore'),(object(),'lore'),(object(),'penalty_box')):
        v.ext.app.server = server
        for g in v.ext.on_comms:
            v.seat(g,room)
        policy = ElsewhereTalk(v.ext, lines_per_10min=6,rng=random.Random(7))
        run(policy._scene(room))
    # Leave OPEN's default evening subject intact; the room guidance appears once.
    assert seen == [('', ''), ("You're in the Lore & Strategy voice room: Lore and boss mechanics", ''), ('','')]
