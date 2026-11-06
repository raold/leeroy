from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Member
from nightly.conftest import h  # noqa: F401  (the night's harness, for the selfie stages)

from .fakes import SELFIE_CFG, renderable_cast

OWNER = 100


@pytest.fixture
def data(tmp_path):
    """A mind data dir of the test's own."""
    d = tmp_path / "mind-data"
    d.mkdir()
    return d


@pytest.fixture
def svc(store, clock, data):
    """The pick service over a bare app with rendering on: the test cast seeded (101-105), 101-103
    with looks and a photo kind, 104 and 105 without; the owner (OWNER) on the roster as guild
    master, with no persona."""
    from leeroy_mind.selfies.service import SelfieService
    seed_test_cast(store)
    renderable_cast(store, 101, 102, 103)
    store.upsert_cast([Member(guid=OWNER, name="Ownerly", race=2, class_=1, gender=0, level=83,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    app = SimpleNamespace(store=store, clock=clock, data_dir=data, personas=PersonaCache(store),
                          human_guid=OWNER)
    return SelfieService(app, SELFIE_CFG)
