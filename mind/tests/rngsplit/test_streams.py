"""Loop-driven consumers draw from streams of their own, split off the app's at attach, so the
talk pipeline's seeded draws do not move with when the loop schedules them."""
import asyncio

import pytest

from selfies.fakes import CLOCK, OWNER, FakeClock, FakeLLM, Member, MindApp, Store, load_config, \
    open_migrated, seed_test_cast, renderable_cast

PHOTOS = ('[selfies]\nenabled=true\nsnaps=true\n[server]\nenabled=true\n[photos]\nsend=true\n'
          '[banter]\nenabled=true\n')


def make(root, seed, toml=PHOTOS):
    (root / "mind.toml").write_text(toml, encoding="utf-8")
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        renderable_cast(s, 101, 102, 103)
        s.upsert_cast([Member(guid=OWNER, name="Ownerly", race=2, class_=1, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(OWNER))
    finally:
        s.close()
    return MindApp(load_config(root / "mind.toml"), root, llm=FakeLLM(), clock=FakeClock(CLOCK),
                   rng_seed=seed)


def consumers(app):
    return {"banter": app.banter.rng, "photos": app.photos.rng}


def draws(rng):
    return [rng.random() for _ in range(5)]


@pytest.fixture
def apps(tmp_path):
    made = []
    def build(name, seed):
        d = tmp_path / name
        d.mkdir()
        made.append(make(d, seed))
        return made[-1]
    yield build
    for a in made:
        asyncio.run(a.stop())


@pytest.mark.parametrize("name", ["banter", "photos"])
def test_the_stream_is_not_the_apps_and_is_seeded(apps, name):
    a, b, c = apps("a", 7), apps("b", 7), apps("c", 8)
    assert a.banter is not None and a.photos is not None
    ra, rb, rc = (consumers(x)[name] for x in (a, b, c))
    assert ra is not a.rng
    assert draws(ra) == draws(rb)
    assert draws(consumers(a)[name]) != draws(rc)


def test_the_streams_are_separate(apps):
    a = apps("a", 7)
    s = consumers(a)
    assert len({id(x) for x in s.values()} | {id(a.rng)}) == 3
    assert len({tuple(draws(x)) for x in s.values()}) == 2
