"""Fixtures of the voice tests. P1b's helpers come from tests/conftest.py (pytest has already
put that directory first on sys.path, so `from conftest import ...` reaches it)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import clock  # noqa: F401  (a fixture, re-exported)
from conftest import make_persona, seed_test_cast
from leeroy_mind.app import GUILD_ID, MindApp
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Hello, Login, Logout, Member
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.testing.fake_voice import FakeVoiceClient
from leeroy_mind.wow import CLASSES, RACES


def write_spec(data_dir: Path, guid: int, name: str, **extra) -> None:
    folder = data_dir / "voices"
    folder.mkdir(parents=True, exist_ok=True)
    spec = {"version": 1, "guid": guid, "name": name, "engine": "kokoro", **extra}
    (folder / f"{guid}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture
def voice_data(tmp_path, monkeypatch):
    """LEEROY_VOICE_DATA at a tmp dir, so nothing reaches the real voice-data."""
    path = tmp_path / "voice-data"
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(path))
    return path


HUMAN = 1
CAST = (101, 102, 103, 104, 105)
OFFICERS = (102, 104)
HUMAN_ROW = Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0, level=80, rank=0,
                   online=False, map=0, zone=0, spec_tab=-1)


class VApp:
    """A MindApp with [voice] enabled whose client is a FakeVoiceClient (`fake.sent` is what
    the service would have received), seeded with P1b's invented cast, voice specs for
    101-105, and 102 and 104 as officers. Events go through app.handle, as the bridge's do."""

    def __init__(self, app: MindApp, fake: FakeVoiceClient, clock):
        self.app, self.fake, self.clock = app, fake, clock
        self.ext = app.voice

    @property
    def sent(self) -> list:
        return self.fake.sent

    def types(self) -> list[str]:
        from leeroy_mind.voice import wire
        return [wire.type_name(m) for m in self.fake.sent]

    async def login(self) -> None:
        await self.app.handle(Login(self._ts(), HUMAN, True))

    async def logout(self) -> None:
        await self.app.handle(Logout(self._ts(), HUMAN, True))

    async def hello(self, human: int = HUMAN) -> None:
        await self.app.handle(Hello(self._ts(), "mod-leeroy", "0.1.0", GUILD_ID, human))

    def _ts(self) -> int:
        return int(self.clock.now() * 1000)

    def rows(self, sql: str, params=()) -> list:
        conn = self.app.store.conn
        out = conn.execute(sql, params).fetchall()
        conn.commit()
        return out


@pytest.fixture
def vapp(tmp_path, clock, voice_data):
    (tmp_path / "mind.toml").write_text("[voice]\nenabled = true\nport = 7999\n",
                                        encoding="utf-8")
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
    seed_test_cast(app.store)
    app.store.upsert_cast([HUMAN_ROW], 1000.0)
    for guid in OFFICERS:
        name = {102: "Belra", 104: "Dunna"}[guid]
        from conftest import TEST_CAST
        _, race, cls = TEST_CAST[guid]
        save_persona(app.store, make_persona(guid, name, guild_role="officer"), source="seed",
                     ts=1001.0, race=RACES[race], class_name=CLASSES[cls])
    for guid in CAST:
        write_spec(tmp_path, guid, f"M{guid}")
    app.personas.invalidate()
    fake = FakeVoiceClient()
    fake.greeted = True
    app.voice.client = fake
    return VApp(app, fake, clock)


def status(*, ok: bool = True, ui: bool = True):
    from leeroy_mind.voice import wire
    return wire.Status(ok, "ok" if ok else "closed", "none", "parakeet", ui, 0, ())


class VRig:
    """make_rig plus a Comms over a RoomState, a VoiceStore and a FakeVoiceClient greeted with
    status.ok. What the rig has (`engine`, `world`, `advance`, `human`, `sent`, ...) reads
    through; `speaks()` are the Speaks the service would have received."""

    def __init__(self, rig):
        from leeroy_mind.voice import wire
        from leeroy_mind.voice.comms import Comms
        from leeroy_mind.voice.rooms import RoomState
        from leeroy_mind.voice.store import VoiceStore
        self.rig = rig
        self._speak = wire.Speak
        self.fake = FakeVoiceClient()
        self.fake.greeted, self.fake.status = True, status()
        self.rooms = RoomState(officers=lambda: set(OFFICERS), human=lambda: HUMAN)
        app = SimpleNamespace(world=rig.world, raid=rig.raid, engine=rig.engine, store=rig.store,
                              clock=rig.clock, personas=rig.engine.personas)
        self.ext = SimpleNamespace(app=app, client=self.fake, rooms=self.rooms,
                                   store=VoiceStore(rig.store), run_id="run")
        self.comms = Comms(self.ext)
        rig.engine.comms = self.comms
        rig.world.room_audience = self.comms.audience

    def __getattr__(self, name):
        return getattr(self.rig, name)

    def seat(self, guid: int, room: str) -> None:
        self.rooms.move(guid, room)

    def him(self, room: str) -> None:
        self.rooms.move(HUMAN, room)

    def says(self, reply) -> None:
        """What the model answers from now on: a text, or a script(model, messages)."""
        self.rig.llm._script = reply if callable(reply) else (lambda model, messages: reply)

    def speaks(self) -> list:
        return [m for m in self.fake.sent if isinstance(m, self._speak)]

    def rows(self, sql: str, params=()) -> list:
        return self.rig.store.conn.execute(sql, params).fetchall()

    def lines(self) -> list:
        return self.rows("SELECT * FROM chat_line WHERE origin = 'mind' ORDER BY id")


@pytest.fixture
def vrig(store, clock, tmp_path, voice_data):
    from engine_rig import make_rig
    return VRig(make_rig(store, clock, tmp_path))
