from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .fakes import FakeTrainingPorts


@pytest.fixture
def fake(tmp_path, clock):
    ports = FakeTrainingPorts(tmp_path, clock)
    try:
        yield ports
    finally:
        ports.close()


@pytest.fixture
def rig(store, clock, tmp_path):
    from engine_rig import make_rig
    return make_rig(store, clock, tmp_path)


@pytest.fixture
def app_stub(fake, tmp_path):
    from leeroy_mind.config import load_config
    from leeroy_mind.storage.store import Store
    app = SimpleNamespace(cfg=load_config(tmp_path / "mind.toml"), data_dir=tmp_path,
        clock=fake.clock, store=Store(fake.conn, clock=fake.clock), human_guid=fake.human_guid,
        handlers={}, backgrounds=[], connections=[], before={}, drains=[], receipts={},
        world=SimpleNamespace(human_online=fake.online),
        personas=SimpleNamespace(get=fake.persona), writer=SimpleNamespace(write=AsyncMock()),
        bridge=SimpleNamespace(send=fake.send), guild=None, voice=None, nightly=None,
        engine=SimpleNamespace(say_as=fake.say, propose=fake.propose))
    def register(name, handler, *, before=False):
        (app.before if before else app.handlers).setdefault(name, []).append(handler)
    app.register_event_handler = register
    app.register_background = app.backgrounds.append
    app.register_shutdown_drain = app.drains.append
    app.register_shutdown_receipt = lambda name, handler: app.receipts.setdefault(name, []).append(handler)
    app.register_connection_hook = app.connections.append
    return app


@pytest.fixture
def app(tmp_path, clock):
    """An unstarted real app; fake model/send only, never opens bridge sockets."""
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_llm import FakeLLM
    instance = MindApp(load_config(tmp_path / "mind.toml"), tmp_path,
                       llm=FakeLLM(), clock=clock, rng_seed=1)
    instance.bridge.send = AsyncMock()
    try:
        yield instance
    finally:
        if instance.library is not None:
            instance.library.close()
        instance.store.conn.close()
