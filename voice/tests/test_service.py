"""The whole service on ephemeral ports (Task 11): FakeEngine and the real chain, FakeDeviceApi,
FakeSessions, FakeKeys, FakeRecognizer and a FakeMind, on a FakeClock. The clock moves only when
a test moves it; the synthesis and recognizer threads are real, so a test waits for what they
post with `eventually`. Nothing here opens a device or hooks a key. The test plays the devices:
out().pump() asks the output for audio, talk() feeds the mic while the clock moves."""
import asyncio
import dataclasses
import json
import logging
import os
import threading
from datetime import datetime, timezone

import numpy as np
import pytest

from conftest import BANK_SHA, eventually, run
from fakes import (FakeClock, FakeDeviceApi, FakeKeys, FakeMind, FakePage, FakeRecognizer,
                   FakeSessions)
from leeroy_voice import logs
from leeroy_voice.attitude import RAID_CALL_SPEED
from leeroy_voice.config import load_config
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.schedule import MOVE_FADE_MS, P0_GAIN_DB
from leeroy_voice.service import STATUS_S, ServiceLock, ServiceLocked, VoiceService
from leeroy_voice.state import Occupancy, Tree
from leeroy_voice.stock import STOCK
from leeroy_voice.stt import Transcript
from leeroy_voice.stt.names import RosterCorrector
from leeroy_voice.voices import spec_from_dict
from leeroy_voice.wire import (Cancel, Channels, Error, Heard, Hello, Invite, Join, Moved, Played,
                               Ptt, Raid, Refused, Room, Session, Speak, Status, VoiceDef)

START = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)
HUMAN = 1
ROOM, RAID_ROOM = "guild_hall", "raid"
ROOMS = (Room(ROOM, "Guild Hall", "", False, (), 0), Room(RAID_ROOM, "Raid", "", False, (), 0))
BLOCK = np.full((480, 1), 0.1, np.float32)
BLOCK_S = 0.01
HEARD = Transcript("hello team", 0.9, 1100)


def spec(guid=101, *, speed=1.0, mic="desk_mic", gain_db=0.0, sha=BANK_SHA) -> dict:
    return {"version": 1, "guid": guid, "name": "Arkon", "engine": "kokoro", "lang": "a",
            "timbre": {"am_michael": 1.0}, "prosody": {}, "speed": speed, "pitch_st": 0.0,
            "mic": mic, "gain_db": gain_db, "locked": False, "voices_sha256": sha, "ref": ""}


class Engine(FakeEngine):
    """FakeEngine, or `real` in its place, that keeps what reached it, in order; `gate` holds
    it, and a line holding a word of `fail` raises EngineError."""

    def __init__(self, real=None):
        super().__init__()
        self.real = real
        if real is not None:
            self.name, self.sample_rate = real.name, real.sample_rate
        self.gate = threading.Event()
        self.gate.set()
        self.said: list[str] = []
        self.spoke: dict[str, float] = {}  # each line's speed
        self.fail: set[str] = set()

    def synth(self, text, spec, style, speed):
        assert self.gate.wait(5)
        self.said.append(text)
        self.spoke[text] = speed
        if any(word in text for word in self.fail):
            raise EngineError("fake: ValueError on a line")
        return (self.real or super()).synth(text, spec, style, speed)

    def live(self) -> list[str]:
        """What it said that is not a stock render."""
        return [s for s in self.said if s not in STOCK.values()]


class Rig:
    """The service with every fake, on a tmp data dir. The context starts run(), connects a
    FakeMind (unless connect=False) and stops the service on the way out."""

    def __init__(self, tmp_path, bank, *, connect=True, cfg=None, recognizer=None, sessions=None,
                 devices=None, elevated=False, keys_fail=False, engine=None, decision=None,
                 **config):
        self.clock = FakeClock(START, yields=4)
        self.data = tmp_path / "data"
        self.data.mkdir(exist_ok=True)
        self.engine = engine or Engine()
        self.devices = devices or FakeDeviceApi(outputs=["Speakers A"], inputs=["Mic A"],
                                                default="Speakers A")
        self.sessions = sessions or FakeSessions({"Wow.exe": 0.8})
        self.recognizer = recognizer or FakeRecognizer(default=HEARD)
        self.keys: FakeKeys | None = None
        self._keys_fail = keys_fail
        self.elevated = elevated
        self.connect = connect
        self.cfg = cfg or dataclasses.replace(load_config(tmp_path / "no-such.toml"),
                                              **{"input_device": "Mic A", **config})
        self.service = VoiceService(
            self.cfg, decision or Decision(), self.data, clock=self.clock, engine=self.engine,
            bank=bank,
            devices=self.devices, sessions=self.sessions, keys_factory=self.make_keys,
            recognizer=self.recognizer,
            corrector=RosterCorrector((), ("Kel'Thuzad",), common=frozenset({"chorus"})),
            ui_port=0, mind_port=0, wow_elevated=lambda: self.elevated,
            foreground=lambda: "Wow.exe")
        self.mind: FakeMind | None = None
        self.task: asyncio.Task | None = None
        self.pages: list[FakePage] = []

    def make_keys(self, vk, on_key, *, on_close):
        self.keys = FakeKeys(vk, on_key, on_close=on_close)
        if self._keys_fail:
            def refuse():
                raise OSError("no window")
            self.keys.start = refuse
        return self.keys

    async def __aenter__(self) -> "Rig":
        self.task = asyncio.create_task(self.service.run())
        await eventually(lambda: self.service.server.port and self.service.ui.port
                         or self.task.done())
        if self.task.done():
            self.task.result()
        if self.connect:
            self.mind = await FakeMind.connect(self.service.server.port)
            await self.next_of(Hello)
        return self

    async def __aexit__(self, *exc) -> None:
        self.engine.gate.set()
        for page in self.pages:
            await page.close()
        if self.mind is not None:
            await self.mind.close()
        await self.service.stop()
        await asyncio.wait({self.task}, timeout=5)

    def stopping(self) -> bool:
        return self.service._stop.is_set()

    async def page(self) -> FakePage:
        page = await FakePage.connect(self.service.ui.port)
        self.pages.append(page)
        await eventually(lambda: page.received)
        return page

    async def next_of(self, kind, predicate=lambda m: True, mind=None):
        """The next message of this kind the mind has not yet looked at."""
        mind = mind or self.mind
        while True:
            msg = await mind.next(3.0)
            if isinstance(msg, kind) and predicate(msg):
                return msg

    def got(self, kind, mind=None) -> list:
        return [m for m in (mind or self.mind).received if isinstance(m, kind)]

    def played(self, mind=None) -> list[tuple[str, str, str]]:
        return [(p.id, p.state, p.reason) for p in self.got(Played, mind)]

    async def sees(self, line_id, state, reason="", mind=None) -> None:
        await eventually(lambda: (line_id, state, reason) in self.played(mind), 3.0)

    async def snapshot(self, mind=None, *, online=True) -> None:
        mind = mind or self.mind
        await mind.send(Session(online, HUMAN, "orion", "embed"))
        await mind.send(Channels(ROOMS))
        await mind.send(VoiceDef(101, "Arkon", spec()))
        await mind.send(Moved(101, ROOM))
        await mind.send(Moved(HUMAN, ROOM))
        await eventually(lambda: self.service.occupancy.human_room == ROOM)

    def speak(self, id, guid=101, text="hello there everyone", *, priority="p4", kind="open",
              channel=ROOM, attitude="none", state="sober", ttl=15.0, after="", template=False,
              to_human=False) -> Speak:
        return Speak(id, guid, text, channel, to_human, priority, kind, attitude, state,
                     int((self.clock.now() + ttl) * 1000), after, template)

    def stream(self, kind: str):
        return next((s for s in reversed(self.devices.streams)
                     if s.kind == kind and not s.closed), None)

    def out(self):
        return self.stream("output")

    async def tick(self, seconds=0.04) -> None:
        await self.clock.advance(seconds, step=0.02)

    async def talk(self, seconds: float) -> None:
        """The clock moves, and the mic delivers its blocks as it does."""
        for _ in range(round(seconds / BLOCK_S)):
            mic = self.stream("input")
            if mic is not None:
                mic.feed(BLOCK)
            await self.clock.advance(BLOCK_S, step=BLOCK_S)

    async def play_out(self) -> None:
        """The output plays until every voice has ended."""
        for _ in range(200):
            if not self.service.scheduler.playing():
                return
            self.out().pump(4800)
            await self.tick(0.1)
        raise AssertionError("a voice never ended")

    def wow(self):
        return self.sessions.handle("Wow.exe")

    async def stock_rendered(self, guid=101) -> None:
        folder = self.data / "cache" / "stock" / str(guid)
        await eventually(lambda: len(list(folder.glob("*/*.wav"))) == len(STOCK), 3.0)


def go(tmp_path, bank, body, **kw):
    async def main():
        async with Rig(tmp_path, bank, **kw) as rig:
            await body(rig)
    run(main())


# -- the brief's --------------------------------------------------------------------------------

def test_speak_to_played(tmp_path, fake_bank):
    """A p4 speak from 101: played started then done, and the mixer had its audio."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        assert np.abs(rig.out().pump(4800)).max() > 0.01
        await rig.play_out()
        await rig.sees("s1", "done")
        assert rig.played() == [("s1", "started", ""), ("s1", "done", "")]
        assert rig.engine.live() == ["hello there everyone"]
    go(tmp_path, fake_bank, body)


def test_speak_while_he_is_offline(tmp_path, fake_bank):
    """Offline comes first of the refusals, a voice held or not."""
    async def body(rig):
        await rig.mind.send(rig.speak("s1", 104))
        await rig.sees("s1", "dropped", "offline")
    go(tmp_path, fake_bank, body)


def test_speak_before_its_voice(tmp_path, fake_bank):
    """A member whose spec has not come: dropped no_voice."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1", 104))
        await rig.sees("s1", "dropped", "no_voice")
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, body)


def test_muted_member(tmp_path, fake_bank):
    """His mute in the UI: played dropped muted, and nothing in the transcript."""
    async def body(rig):
        await rig.snapshot()
        page = await rig.page()
        await page.send({"type": "mute", "guid": 101, "on": True})
        await eventually(lambda: 101 in rig.service.state()["mutes"])
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "dropped", "muted")
        assert rig.service.state()["transcript"] == []
        assert not [m for m in page.received if m.get("type") == "transcript"]
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, body)


def test_empty_text_dropped(tmp_path, fake_bank):
    """Review Focus 4: nothing speakable is dropped, never played as silence."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1", text="lol"))
        await rig.sees("s1", "dropped", "empty")
        await rig.mind.send(rig.speak("s2", text="<3"))
        await rig.sees("s2", "dropped", "empty")
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, body)


def test_stock_line_starts_fast(tmp_path, fake_bank):
    """A cached callout starts within 50 ms of the clock, however busy the synthesis thread."""
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        rig.engine.gate.clear()  # a live line would now wait
        await rig.mind.send(rig.speak("s1", text="oom", kind="callout", priority="p1"))
        t0 = rig.clock.now()
        await rig.sees("s1", "started")
        assert rig.clock.now() - t0 <= 0.05
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, body)


def test_ptt_to_heard(tmp_path, fake_bank):
    """Key down, 1.2 s, key up: ptt down 150 ms after the key-down, ptt up, then heard, with
    latency_ms from the key-up."""
    async def body(rig):
        await rig.snapshot()
        await rig.talk(0.3)  # the ring fills
        rig.keys.key(True)
        t0 = rig.clock.now()
        await rig.talk(0.13)
        assert not rig.got(Ptt)
        await rig.talk(0.05)
        down = await rig.next_of(Ptt)
        assert down == Ptt(True, ROOM)
        await rig.talk(1.02)
        rig.keys.key(False)
        up = await rig.next_of(Ptt)
        assert up == Ptt(False, ROOM)
        assert not rig.got(Heard)
        await rig.talk(0.25)
        heard = await rig.next_of(Heard)
        assert heard.channel == ROOM and heard.text == "hello team" and heard.raw == "hello team"
        assert heard.speech_ms == 1100 and heard.confidence == pytest.approx(0.9)
        assert 200 <= heard.latency_ms <= 300
        clip, hotwords = rig.recognizer.calls[0]
        assert abs(len(clip) / 16000 - (0.3 + 1.05 + 0.2)) < 0.1  # the ring, the hold, the tail
        assert hotwords == ["Arkon"]
        assert rig.clock.now() - t0 >= 1.2
    go(tmp_path, fake_bank, body)


def test_tap_sends_nothing(tmp_path, fake_bank):
    """I2, Review Focus 1: a 120 ms press: no ptt, set_ptt never called, no duck, no capture,
    and the recognizer never called."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        calls = []
        real = rig.service.scheduler.set_ptt
        rig.service.scheduler.set_ptt = lambda down, now=None: (calls.append(down),
                                                                real(down, now))[1]
        rig.keys.key(True)
        await rig.talk(0.12)
        rig.keys.key(False)
        await rig.talk(0.4)
        assert not rig.got(Ptt) and calls == [] and rig.recognizer.calls == []
        assert not rig.service.capture.ready
        assert rig.wow().sets == []
    go(tmp_path, fake_bank, body)


def test_changed_spec_drops_stale_stock(tmp_path, fake_bank):
    """I6: another speed: the old folder goes, and "oom" next plays the new render."""
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        folders = list((rig.data / "cache" / "stock" / "101").iterdir())
        assert len(folders) == 1
        await rig.mind.send(VoiceDef(101, "Arkon", spec(speed=1.1)))
        await eventually(lambda: not folders[0].exists(), 3.0)
        await rig.stock_rendered()
        starts = []
        real = rig.service.mixer.apply

        def spy(plan, lines, own_db):
            starts.extend(audio for _, audio, _ in plan.starts)
            real(plan, lines, own_db)
        rig.service.mixer.apply = spy
        await rig.mind.send(rig.speak("s1", text="oom", kind="callout", priority="p1"))
        await rig.sees("s1", "started")
        dry = FakeEngine().synth(STOCK["oom"], spec_from_dict(spec(speed=1.1)), None,
                                 1.1 * RAID_CALL_SPEED)
        assert len(starts[0]) == len(dry) * 2  # at 48 kHz
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, body)


def test_a_spec_held_already_renders_nothing(tmp_path, fake_bank):
    """A mind's reconnect sends every spec again: the same one starts no work."""
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        count = len(rig.engine.said)
        drops = []
        rig.service.stock.drop_stale = drops.append
        await rig.mind.send(VoiceDef(101, "Arkon", spec()))
        await rig.mind.send(rig.speak("s1", 104))
        await rig.sees("s1", "dropped", "no_voice")  # the voice message has been handled
        assert drops == [] and len(rig.engine.said) == count
    go(tmp_path, fake_bank, body)


def test_close_restores(tmp_path, fake_bank):
    """M7: on_close (a plain taskkill's WM_CLOSE) stops the service, with WoW back at its
    baseline and the lock released."""
    async def main():
        rig = Rig(tmp_path, fake_bank)
        async with rig:
            await rig.snapshot()
            await rig.mind.send(rig.speak("s1"))
            await rig.sees("s1", "started")
            await eventually(lambda: rig.wow().volume < 0.5, 3.0)
            assert (rig.data / "voice.pid").read_text().strip() == str(os.getpid())
            rig.keys.close()
            await asyncio.wait_for(rig.task, 5)
            assert rig.task.exception() is None
            assert rig.wow().volume == pytest.approx(0.8)
            assert not (rig.data / "state" / "wow-volume.json").exists()
            assert not (rig.data / "voice.pid").exists()
            assert rig.keys.stopped and not rig.devices.streams[0].active
            ServiceLock(rig.data).acquire().release()
    run(main())


def test_exits_when_mind_gone(tmp_path, fake_bank):
    """M24: no mind and no session for 10 minutes of the clock: run() returns."""
    async def body(rig):
        await rig.clock.advance(599.0, step=50.0)
        assert not rig.stopping()
        await rig.clock.advance(2.0, step=1.0)
        await asyncio.wait_for(rig.task, 5)
        assert rig.task.exception() is None
    go(tmp_path, fake_bank, body, connect=False)


def test_the_ten_minutes_run_from_the_minds_leaving(tmp_path, fake_bank):
    async def body(rig):
        await rig.clock.advance(599.0, step=50.0)
        await rig.snapshot(online=False)
        await rig.clock.advance(599.0, step=50.0)
        assert not rig.stopping()
        await rig.mind.close()
        rig.mind = None
        await eventually(lambda: not rig.service.server.connected)
        await rig.tick()  # the first tick without it starts the count
        await rig.clock.advance(599.0, step=50.0)
        assert not rig.stopping()
        await rig.clock.advance(2.0, step=1.0)
        await asyncio.wait_for(rig.task, 5)
    go(tmp_path, fake_bank, body, connect=True)


async def mind_gone(rig) -> None:
    """The mind drops with his session online, and a tick sees it."""
    await rig.mind.close()
    rig.mind = None
    await eventually(lambda: not rig.service.server.connected)
    await rig.tick()


async def mind_back(rig) -> None:
    rig.mind = await FakeMind.connect(rig.service.server.port)
    await rig.next_of(Hello)
    await rig.snapshot()
    await rig.tick()


def test_a_mind_gone_ten_minutes_ends_the_session(tmp_path, fake_bank, caplog):
    """The ruling on I1: no mind is left to say offline, so 10 minutes end the session as a
    logout does, the mic closed and WoW back; then M24's 10 minutes stop the service."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await eventually(lambda: rig.wow().volume < 0.5, 3.0)
        await mind_gone(rig)
        await rig.clock.advance(599.0, step=50.0)
        assert rig.service.mic.state == "ok" and rig.keys.armed and not rig.stopping()
        await rig.clock.advance(2.0, step=1.0)
        assert rig.service.mic.state == "closed" and rig.stream("input") is None
        assert not rig.keys.armed
        await eventually(lambda: rig.wow().volume == pytest.approx(0.8), 3.0)
        assert rig.service.scheduler.playing() == set()
        await rig.tick(0.4)
        assert rig.stream("output") is None
        assert not rig.stopping()  # M24 counts from here
        await rig.clock.advance(598.0, step=50.0)
        assert not rig.stopping()
        await rig.clock.advance(3.0, step=1.0)
        await asyncio.wait_for(rig.task, 5)
        assert rig.task.exception() is None
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "session_abandoned" in caplog.text and "idle_exit" in caplog.text


def test_a_mind_back_within_the_ten_minutes_keeps_the_session(tmp_path, fake_bank):
    """A restart: the mic and the key never closed, and the count starts over."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        mic = rig.stream("input")
        await mind_gone(rig)
        await rig.clock.advance(590.0, step=50.0)
        await mind_back(rig)
        await rig.clock.advance(590.0, step=50.0)
        assert rig.stream("input") is mic and rig.keys.armed and rig.service.mic.state == "ok"
        await mind_gone(rig)
        await rig.clock.advance(590.0, step=50.0)
        assert rig.stream("input") is mic and rig.keys.armed
    go(tmp_path, fake_bank, body)


def test_a_mind_back_after_the_session_ended_brings_it_back(tmp_path, fake_bank):
    """Its snapshot says session online, and everything opens again; M24's count is off."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        await mind_gone(rig)
        await rig.clock.advance(601.0, step=50.0)
        await rig.tick(0.4)
        assert rig.service.mic.state == "closed" and rig.stream("output") is None
        await mind_back(rig)
        assert rig.service.mic.state == "ok" and rig.stream("output") and rig.keys.armed
        await rig.clock.advance(1300.0, step=50.0)
        assert not rig.stopping() and rig.service.mic.state == "ok"
    go(tmp_path, fake_bank, body)


def test_debug_record(tmp_path, fake_bank):
    """M25: the flag on: one wav per capture, and a 25 h old file removed at start."""
    async def main():
        rig = Rig(tmp_path, fake_bank, debug_record=True)
        debug = rig.data / "debug"
        debug.mkdir()
        old, fresh = debug / "ptt-1.wav", debug / "ptt-2.wav"
        old.write_bytes(b"old")
        fresh.write_bytes(b"fresh")
        now = rig.clock.now()
        os.utime(old, (now - 25 * 3600, now - 25 * 3600))
        os.utime(fresh, (now - 23 * 3600, now - 23 * 3600))
        async with rig:
            await eventually(lambda: not old.exists())
            await rig.snapshot()
            await rig.tick()
            for _ in range(2):
                rig.keys.key(True)
                await rig.talk(0.5)
                rig.keys.key(False)
                await rig.talk(0.3)
                await rig.next_of(Heard)
            written = sorted(p.name for p in debug.glob("*.wav"))
            assert len(written) == 3 and fresh.name in written
            await rig.clock.advance(3700.0, step=100.0)  # the hourly pass: it is over 24 h now
            await eventually(lambda: not fresh.exists())
    run(main())


def test_debug_record_off_writes_nothing(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        await rig.next_of(Heard)
        assert not (rig.data / "debug").exists()
    go(tmp_path, fake_bank, body)


def test_ptt_while_mind_down_is_discarded(tmp_path, fake_bank):
    """Review Focus 5: no mind: the press is dropped at once, the recognizer never called, and
    the UI shows the mind offline."""
    async def body(rig):
        await rig.snapshot()
        page = await rig.page()
        await rig.mind.close()
        rig.mind = None
        await eventually(lambda: rig.service.state()["mind"] is False)
        await eventually(lambda: {"type": "mind", "up": False} in page.received)
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.4)
        assert rig.recognizer.calls == [] and not rig.service.capture.ready
        assert rig.service.state()["ptt"] is False
    go(tmp_path, fake_bank, body)


def test_mind_gone_mid_press_never_delivers_later(tmp_path, fake_bank):
    """Review Focus 5: a press under way when the mind drops is dropped with it, and a mind that
    reconnects hears nothing of it."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        await rig.next_of(Ptt)
        await rig.mind.close()
        await eventually(lambda: not rig.service.server.connected)
        await rig.talk(0.1)
        rig.mind = await FakeMind.connect(rig.service.server.port)
        rig.keys.key(False)
        await rig.talk(0.5)
        assert rig.recognizer.calls == [] and not rig.got(Heard) and not rig.got(Ptt)
    go(tmp_path, fake_bank, body)


def test_logout_mid_press(tmp_path, fake_bank):
    """Review Focus 2: offline during a press and a line: no heard, the line fades, the mic
    closes and WoW comes back."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await eventually(lambda: rig.wow().volume < 0.5, 3.0)
        rig.keys.key(True)
        await rig.talk(0.3)
        await rig.next_of(Ptt)
        await rig.mind.send(Session(False, HUMAN, "orion", "embed"))
        await rig.sees("s1", "cut", "offline")
        assert rig.service.mic.state == "closed" and rig.stream("input") is None
        assert not rig.keys.armed
        await rig.next_of(Ptt, lambda m: not m.down)
        await eventually(lambda: rig.wow().volume == pytest.approx(0.8), 3.0)
        rig.keys.key(False)
        await rig.tick(0.5)
        assert rig.recognizer.calls == [] and not rig.got(Heard)
        assert rig.service.state()["ptt"] is False
    go(tmp_path, fake_bank, body)


def test_a_hold_ended_by_logout_gives_no_press_after_it(tmp_path, fake_bank, caplog):
    """The key went down inside the gate and the session ended: its Press never comes, and a
    fresh hold after the next login is a press of its own."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.mind.send(Session(False, HUMAN, "orion", "embed"))
        await eventually(lambda: not rig.keys.armed)
        await rig.tick(0.3)
        assert not rig.service.machine.held
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "ptt_press" not in caplog.text


def test_ducking_follows_voices(tmp_path, fake_bank):
    """duck(True) at the first start; duck(False) 400 ms after the last end."""
    async def body(rig):
        await rig.snapshot()
        calls = []
        real = rig.service.ducker.duck
        rig.service.ducker.duck = lambda on: (calls.append(on), real(on))[1]
        await rig.mind.send(rig.speak("s1", text="one"))
        await rig.sees("s1", "started")
        assert calls == [True]
        await rig.play_out()
        await rig.sees("s1", "done")
        await rig.mind.send(rig.speak("s2", text="two"))  # another comes within the 400 ms
        await rig.sees("s2", "started")
        await rig.tick(0.5)
        assert calls == [True]
        rig.out().pump(48000)
        await rig.tick(0.02)  # the tick that takes its end
        await rig.sees("s2", "done")
        await rig.tick(0.36)
        assert calls == [True]
        await rig.tick(0.06)
        assert calls == [True, False]
    go(tmp_path, fake_bank, body)


def test_status(tmp_path, fake_bank):
    """On a change and every 10 s: ok, mic, gpu, stt, ui, queue."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        assert rig.got(Status)[-1] == Status(True, "ok", "none", "parakeet", False, 0, ())
        count = len(rig.got(Status))
        await rig.clock.advance(STATUS_S + 0.1, step=0.5)
        await eventually(lambda: len(rig.got(Status)) == count + 1)
    go(tmp_path, fake_bank, body)


def test_status_with_no_microphone(tmp_path, fake_bank):
    """M22, PR15: no input configured: mic "unset", and ok still True."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        assert rig.got(Status)[-1].mic == "unset" and rig.got(Status)[-1].ok
    go(tmp_path, fake_bank, body, input_device="")


def test_status_follows_a_change(tmp_path, fake_bank):
    """A page opens: status.ui turns True at the next tick, to the mind and the UI alike."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        page = await rig.page()
        await rig.tick()
        await eventually(lambda: rig.got(Status)[-1].ui)
        await eventually(lambda: any(m.get("type") == "status" and m["ui"]
                                     for m in page.received))
    go(tmp_path, fake_bank, body)


def test_status_without_recognizer_or_output(tmp_path, fake_bank):
    """No recognizer: stt "off"; before the session the output is shut, so ok is False."""
    async def main():
        rig = Rig(tmp_path, fake_bank)
        rig.service._recognizer = None
        async with rig:
            await rig.tick()
            first = rig.got(Status)[-1]
            assert first.stt == "off" and not first.ok and first.mic == "closed"
    run(main())


def test_mind_restart(tmp_path, fake_bank):
    """The mind drops mid-line: it plays to its end; a queued one is dropped mind_gone in the
    log; the new mind's snapshot replaces the tree and who is where."""
    async def main():
        rig = Rig(tmp_path, fake_bank)
        async with rig:
            await rig.snapshot()
            await rig.mind.send(rig.speak("s1"))
            await rig.sees("s1", "started")
            await rig.mind.send(rig.speak("s2"))
            await eventually(lambda: rig.service.scheduler.waiting() == 1)
            await rig.mind.close()
            await eventually(lambda: rig.service.scheduler.waiting() == 0)
            assert rig.service.scheduler.playing() == {"s1"}
            assert rig.service.state()["mind"] is False
            await rig.play_out()
            mind = rig.mind = await FakeMind.connect(rig.service.server.port)
            await mind.send(Session(True, HUMAN, "orion", "embed"))
            await mind.send(Channels((Room("new_hall", "New Hall", "", False, (), 0),)))
            await mind.send(Moved(HUMAN, "new_hall"))
            await eventually(lambda: rig.service.occupancy.human_room == "new_hall")
            state = rig.service.state()
            assert [r["id"] for r in state["tree"]] == ["new_hall"]
            assert state["where"] == {HUMAN: "new_hall"} and state["mind"] is True
    run(main())


def test_mind_gone_reaches_the_log(tmp_path, fake_bank, caplog):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await rig.mind.send(rig.speak("s2"))
        await eventually(lambda: rig.service.scheduler.waiting() == 1)
        await rig.mind.close()
        rig.mind = None
        await eventually(lambda: rig.service.scheduler.waiting() == 0)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "line_dropped" in caplog.text and "reason=mind_gone" in caplog.text


def test_wow_elevated_is_reported(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        assert rig.got(Status)[-1].errors == ()
        rig.elevated = True
        await rig.clock.advance(STATUS_S + 0.1, step=0.5)
        await eventually(lambda: "wow_elevated" in rig.got(Status)[-1].errors)
    go(tmp_path, fake_bank, body)


def test_a_failed_elevation_check_is_no_error(tmp_path, fake_bank):
    """wow_elevated() may raise OSError: it costs nothing but that reading."""
    async def main():
        rig = Rig(tmp_path, fake_bank)

        def refuse():
            raise OSError("access denied")
        rig.service._wow_elevated = refuse
        async with rig:
            await rig.snapshot()
            await rig.clock.advance(STATUS_S + 0.1, step=0.5)
            await eventually(lambda: rig.got(Status))
            assert rig.got(Status)[-1].errors == () and rig.got(Status)[-1].ok
    run(main())


# -- the other mind messages --------------------------------------------------------------------

def test_a_bad_id_is_refused_and_the_loop_lives(tmp_path, fake_bank):
    """Task 6's Minor 5: an empty or reused id passes the wire and would raise in submit."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak(""))
        assert (await rig.next_of(Error)).message == "bad field: id"
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await rig.mind.send(rig.speak("s1"))
        assert (await rig.next_of(Error)).message == "bad field: id"
        await rig.play_out()
        await rig.sees("s1", "done")
        await rig.mind.send(rig.speak("s1"))  # remembered for `after`
        assert (await rig.next_of(Error)).message == "bad field: id"
        await rig.mind.send(rig.speak("s2"))
        await rig.sees("s2", "started")
        assert rig.played().count(("s1", "started", "")) == 1
    go(tmp_path, fake_bank, body)


@pytest.mark.parametrize("bad", [spec(102, mic="carrier_pigeon"), spec(102, sha="0" * 64),
                                 spec(103), {"version": 1}, {**spec(102), "name": "   "}])
def test_a_bad_spec_is_logged_and_refused(tmp_path, fake_bank, caplog, bad):
    """A mic the chain has no profile for (it would raise at play time), a voices file the bank
    does not hold, another member's spec: refused when the message comes, not at the line."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(VoiceDef(102, "Belra", bad))
        assert (await rig.next_of(Error)).message == "bad field: spec"
        await rig.mind.send(rig.speak("s1", 102))
        await rig.sees("s1", "dropped", "no_voice")
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "voice_refused" in caplog.text


def test_a_bad_update_keeps_the_voice_held(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(VoiceDef(101, "Arkon", spec(mic="carrier_pigeon")))
        await rig.next_of(Error)
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
    go(tmp_path, fake_bank, body)


def test_a_start_is_at_the_spec_his_volume_and_the_levers(tmp_path, fake_bank):
    """spec.gain_db + his UI volume + Levers.gain_db (hungover -2 dB); the mixer adds the tier
    offset, and a p0 ducked by the key comes back to that level."""
    async def body(rig):
        await rig.mind.send(Session(True, HUMAN, "orion", "embed"))
        await rig.mind.send(Channels(ROOMS))
        await rig.mind.send(VoiceDef(101, "Arkon", spec(gain_db=2.0)))
        await rig.mind.send(Moved(HUMAN, ROOM))
        await eventually(lambda: rig.service.occupancy.human_room == ROOM)
        rig.service.volume(101, -5.0)
        await rig.mind.send(rig.speak("s1", state="hungover"))
        await rig.sees("s1", "started")
        assert rig.service.mixer._voices["s1"].base_db == pytest.approx(2.0 - 5.0 - 2.0)
        await rig.play_out()
        await rig.mind.send(rig.speak("s2", priority="p0", kind="callout", state="hungover"))
        await rig.sees("s2", "started")
        want = 2.0 - 5.0 + 0.0 + P0_GAIN_DB  # a raid call takes no state's gain
        assert rig.service.mixer._voices["s2"].base_db == pytest.approx(want)
    go(tmp_path, fake_bank, body)


def test_his_volume_reaches_a_line_already_waiting(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await rig.mind.send(rig.speak("s2"))
        await eventually(lambda: rig.service.scheduler.waiting() == 1)
        rig.service.volume(101, -6.0)
        own = {}
        real = rig.service.mixer.apply

        def spy(plan, lines, own_db):
            own.update(own_db)
            real(plan, lines, own_db)
        rig.service.mixer.apply = spy
        await rig.play_out()
        await rig.sees("s2", "started")
        assert own["s2"] == pytest.approx(-6.0)
    go(tmp_path, fake_bank, body)


def test_a_mute_cuts_what_the_member_is_saying(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        rig.service.mute(101, True)
        await rig.sees("s1", "cut", "muted")
        rig.service.mute(101, False)
        assert json.loads((rig.data / "ui-state.json").read_text()) == {"volumes": {}, "mutes": []}
        await rig.mind.send(rig.speak("s2"))
        await rig.sees("s2", "started")
    go(tmp_path, fake_bank, body)


def test_ui_settings_are_kept(tmp_path, fake_bank):
    async def main():
        async with Rig(tmp_path, fake_bank) as rig:
            rig.service.volume(101, -3.0)
            rig.service.mute(102, True)
        rig2 = Rig(tmp_path, fake_bank)
        state = rig2.service.state()
        assert state["volumes"] == {101: -3.0} and state["mutes"] == [102]
    run(main())


def test_synthesis_goes_in_tier_order(tmp_path, fake_bank):
    """One synthesis thread, the most urgent first: a p1 overtakes the sentences still queued
    behind a long p4, which gives up the thread between its sentences."""
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        rig.engine.gate.clear()
        await rig.mind.send(rig.speak("s1", text="First one here. Second one here. Third here."))
        await eventually(lambda: rig.service.pending_synthesis() == 2)
        await rig.mind.send(rig.speak("s2", text="heal the tank", priority="p1", kind="urgent"))
        await eventually(lambda: rig.service.pending_synthesis() == 3)
        rig.engine.gate.set()
        await eventually(lambda: len(rig.engine.live()) == 4, 3.0)
        assert rig.engine.live() == ["First one here.", "heal the tank", "Second one here.",
                                     "Third here."]
    go(tmp_path, fake_bank, body)


def test_a_line_that_ended_leaves_the_synthesis_queue(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        rig.engine.gate.clear()
        await rig.mind.send(rig.speak("s1", text="First one here."))
        await rig.mind.send(rig.speak("s2", text="Never said."))
        await eventually(lambda: rig.service.scheduler.waiting() == 2)
        await rig.mind.send(Cancel(id="s2"))
        await rig.sees("s2", "dropped", "cancelled")
        rig.engine.gate.set()
        await rig.sees("s1", "started")
        await eventually(lambda: rig.service.pending_synthesis() == 0)
        assert "Never said." not in rig.engine.said
    go(tmp_path, fake_bank, body)


def test_a_stock_clip_goes_to_raid_calls_and_plain_lines(tmp_path, fake_bank):
    """Task 5's carry: a stock clip is the neutral raid-call reading. A callout takes it
    whatever the night, a plain line too; a line with an attitude or a state is spoken live."""
    async def body(rig):
        await rig.snapshot()
        await rig.stock_rendered()
        await rig.mind.send(rig.speak("s1", text="ready", kind="open"))
        await rig.sees("s1", "started")
        await rig.mind.send(rig.speak("s2", 101, text="ready", kind="callout", attitude="lust",
                                      state="drunk", priority="p2"))
        await rig.mind.send(rig.speak("s3", 101, text="ready", kind="flirt", attitude="lust"))
        await eventually(lambda: rig.service.pending_synthesis() == 0 and rig.engine.live())
        assert rig.engine.live() == ["ready"]  # the flirt alone
    go(tmp_path, fake_bank, body)


def test_a_failed_synthesis_is_dropped_engine_error(tmp_path, fake_bank, caplog):
    """Task 4's carry: the type and the reason in the log, never the line."""
    async def body(rig):
        await rig.snapshot()
        rig.engine.fail = {"cursed"}
        await rig.mind.send(rig.speak("s1", text="a cursed line"))
        await rig.sees("s1", "dropped", "engine_error")
        await rig.mind.send(rig.speak("s2"))
        await rig.sees("s2", "started")
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "synthesis_failed" in caplog.text and "cursed" not in caplog.text


def test_any_other_failure_is_logged_by_its_type_alone(tmp_path, fake_bank, caplog):
    """An exception that is no EngineError may quote the line."""
    async def body(rig):
        await rig.snapshot()

        def boom(*args, **kw):
            raise ValueError("a cursed line")
        rig.service._fx = boom
        await rig.mind.send(rig.speak("s1", text="a cursed line"))
        await rig.sees("s1", "dropped", "engine_error")
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "error=ValueError" in caplog.text and "cursed" not in caplog.text


def test_audio_that_is_not_finite_never_reaches_the_mixer(tmp_path, fake_bank):
    """The mixer refuses it on the event loop, mid-Plan: it fails on the worker instead, as any
    other failed line does."""
    async def body(rig):
        await rig.snapshot()
        rig.service._fx = lambda *args, **kw: np.full(4800, np.nan, np.float32)
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "dropped", "engine_error")
        assert rig.service.scheduler.playing() == set()
    go(tmp_path, fake_bank, body)


def test_a_failed_second_sentence_ends_the_line_with_its_first(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        rig.engine.fail = {"cursed"}
        await rig.mind.send(rig.speak("s1", text="Fine words here. A cursed line. Never spoken."))
        await rig.sees("s1", "started")
        await rig.play_out()
        await rig.sees("s1", "cut", "engine_error")
        assert "Never spoken." not in rig.engine.said
    go(tmp_path, fake_bank, body)


def test_sentences_become_segments_in_one_voice(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1", text="One sentence. And another one."))
        await rig.sees("s1", "started")
        line = rig.service._jobs["s1"].line
        await eventually(lambda: all(s is not None for s in line.segments))  # real threads
        assert rig.engine.live() == ["One sentence.", "And another one."]
        await rig.play_out()
        await rig.sees("s1", "done")
        assert rig.played().count(("s1", "started", "")) == 1
    go(tmp_path, fake_bank, body)


def test_levers_shape_the_text_and_the_speed(tmp_path, fake_bank):
    """Ear text, then the levers: the spec's speed times the attitude's, and misaki's stress on
    the word aimed at him."""
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1", text="i see u", attitude="lust", to_human=True))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["I see [you](+2)"]
        assert rig.engine.spoke["I see [you](+2)"] == pytest.approx(0.86)
    go(tmp_path, fake_bank, body)


def test_cancel_below_is_that_tier_and_less_urgent(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(rig.speak("s1", priority="p4"))
        await rig.sees("s1", "started")
        await rig.mind.send(rig.speak("s2", priority="p3"))
        await rig.mind.send(rig.speak("s3", priority="p2", kind="callout"))
        await eventually(lambda: rig.service.scheduler.waiting() == 2)
        await rig.mind.send(Cancel(below="p3"))
        await rig.sees("s1", "cut", "cancelled")
        await rig.sees("s2", "dropped", "cancelled")
        assert ("s3", "dropped", "cancelled") not in rig.played()
    go(tmp_path, fake_bank, body)


def test_raid_silence(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        await rig.mind.send(Raid("combat", 101))
        await rig.mind.send(rig.speak("s1", priority="p4"))
        await rig.sees("s1", "dropped", "raid")
    go(tmp_path, fake_bank, body)


def test_his_move_fades_the_room_he_left(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        page = await rig.page()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await rig.mind.send(Moved(HUMAN, RAID_ROOM, 101))
        await rig.sees("s1", "cut", "moved")
        await eventually(lambda: {"type": "moved", "guid": HUMAN, "channel": RAID_ROOM}
                         in page.received)
        await rig.mind.send(rig.speak("s2", channel=ROOM))
        await rig.sees("s2", "dropped", "not_his_channel")
        assert MOVE_FADE_MS == 150
    go(tmp_path, fake_bank, body)


def test_a_whisper_is_his_wherever_he_is(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        chains = []
        real = rig.service._fx

        def spy(audio, sr, spec, *, grit, whisper):
            chains.append((grit, whisper))
            return real(audio, sr, spec, grit=grit, whisper=whisper)
        rig.service._fx = spy
        await rig.mind.send(rig.speak("s1", channel="whisper", priority="p3", kind="whisper"))
        await rig.sees("s1", "started")
        entry = rig.service.state()["transcript"][-1]
        assert entry["kind"] == "whisper" and entry["who"] == "Arkon"
        await rig.mind.send(rig.speak("s2"))
        await eventually(lambda: len(chains) == 2)
        assert chains == [(3, True), (3, False)]  # dry and close, then the room's chain
    go(tmp_path, fake_bank, body)


def test_invite_refused_and_join(tmp_path, fake_bank):
    """invite and refused reach the UI's state, and a join from the UI goes to the mind."""
    async def body(rig):
        await rig.snapshot()
        page = await rig.page()
        await rig.mind.send(Invite(101, RAID_ROOM))
        await eventually(lambda: {"type": "invite", "from": 101, "channel": RAID_ROOM}
                         in page.received)
        assert rig.service.state()["invites"] == [{"from": 101, "channel": RAID_ROOM}]
        await page.send({"type": "join", "channel": RAID_ROOM})
        assert await rig.next_of(Join) == Join(RAID_ROOM)
        await rig.mind.send(Refused(RAID_ROOM, "locked"))
        await eventually(lambda: {"type": "refused", "channel": RAID_ROOM, "reason": "locked"}
                         in page.received)
        state = rig.service.state()
        assert state["invites"] == []
        assert state["transcript"][-1]["kind"] == "system"
        await rig.mind.send(Invite(101, RAID_ROOM))
        await eventually(lambda: rig.service.state()["invites"])
        await rig.mind.send(Moved(HUMAN, RAID_ROOM))
        await eventually(lambda: rig.service.state()["invites"] == [])
    go(tmp_path, fake_bank, body)


def test_the_transcript_comes_with_the_voice(tmp_path, fake_bank):
    async def body(rig):
        await rig.snapshot()
        page = await rig.page()
        await rig.mind.send(rig.speak("s1", text="adds on the left"))
        await rig.sees("s1", "started")
        await eventually(lambda: [m for m in page.received if m.get("type") == "transcript"])
        entry = [m for m in page.received if m.get("type") == "transcript"][0]
        assert (entry["who"], entry["text"], entry["kind"]) == ("Arkon", "adds on the left", "say")
        await eventually(lambda: {"type": "speaking", "guid": 101, "on": True} in page.received)
        await rig.play_out()
        await eventually(lambda: {"type": "speaking", "guid": 101, "on": False} in page.received)
        assert rig.service.state()["speaking"] == []
    go(tmp_path, fake_bank, body)


def test_the_state_follows_server_pys_contract(tmp_path, fake_bank):
    """Every key, the `human` one included, in its shapes; a page gets it first."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        state = (await rig.page()).received[0]
        assert state["type"] == "state"
        assert state["tree"][0] == {"id": ROOM, "name": "Guild Hall", "parent": "",
                                    "locked": False, "capacity": 0}
        assert state["where"] == {str(HUMAN): ROOM, "101": ROOM}
        assert state["names"] == {"101": "Arkon"}
        assert state["room"] == ROOM and state["human"] == HUMAN and state["speaking"] == [101]
        assert state["ptt"] is False and state["volumes"] == {} and state["mutes"] == []
        assert state["invites"] == [] and state["mind"] is True
        assert state["transcript"][0]["kind"] == "say"
        assert set(state["status"]) == {"ok", "mic", "gpu", "stt", "ui", "queue", "errors"}
        assert state["status"]["errors"] == []
    go(tmp_path, fake_bank, body)


def test_a_snapshot_reaches_an_open_page(tmp_path, fake_bank):
    """`channels` has no event of its own: a fresh state replaces the page's."""
    async def body(rig):
        page = await rig.page()
        await rig.mind.send(Channels(ROOMS))
        await eventually(lambda: rig.service.tree.rooms)
        await rig.tick()
        await eventually(lambda: any(m["type"] == "state" and m["tree"]
                                     for m in page.received))
    go(tmp_path, fake_bank, body)


def test_hello_names_the_service(tmp_path, fake_bank):
    async def body(rig):
        hello = next(m for m in rig.mind.received if isinstance(m, Hello))
        assert hello.server == "leeroy-voice" and hello.engines == ("fake",)
        assert hello.input_device == "Mic A" and hello.ptt_key == "\\"
    go(tmp_path, fake_bank, body)


def test_no_line_text_in_the_log(tmp_path, fake_bank):
    """What members and he said reaches the UI and the mind, never the log."""
    async def main():
        teardown = logs.setup(tmp_path / "log")
        try:
            recognizer = FakeRecognizer(default=Transcript("my secret plan", 0.9, 1100))
            async with Rig(tmp_path, fake_bank, recognizer=recognizer) as rig:
                await rig.snapshot()
                await rig.tick()
                await rig.mind.send(rig.speak("s1", text="a private confession"))
                await rig.sees("s1", "started")
                rig.keys.key(True)
                await rig.talk(0.5)
                rig.keys.key(False)
                await rig.talk(0.3)
                await rig.next_of(Heard)
        finally:
            teardown()
        text = (tmp_path / "log" / "logs" / "voice.log").read_text(encoding="utf-8")
        assert "ptt_press" in text
        assert "confession" not in text and "secret" not in text
    run(main())


# -- the key ------------------------------------------------------------------------------------

def test_a_press_is_logged_with_the_foreground_process(tmp_path, fake_bank, caplog):
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.2)
        await rig.next_of(Ptt)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "ptt_press: foreground=Wow.exe" in caplog.text


def test_a_press_holds_the_models_lines_and_ducks_the_rest(tmp_path, fake_bank):
    """set_ptt reaches the scheduler: a p4 from the model waits, a template plays ducked."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.2)
        await rig.next_of(Ptt)
        assert rig.service.state()["ptt"] is True
        await rig.mind.send(rig.speak("s1"))
        await eventually(lambda: rig.service.pending_synthesis() == 0
                         and rig.service.scheduler.waiting() == 1)
        await rig.talk(0.1)
        assert ("s1", "started", "") not in rig.played()
        await rig.mind.send(rig.speak("s2", 101, template=True))
        await rig.sees("s2", "started")
        rig.keys.key(False)
        await rig.talk(0.5)
    go(tmp_path, fake_bank, body)


def test_a_missed_key_up_closes_the_press(tmp_path, fake_bank):
    """The watchdog: GetAsyncKeyState says up, twice: the Release is dated at the first read."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        await rig.next_of(Ptt)
        rig.keys.key(False, delivered=False)
        await rig.talk(0.1)
        assert await rig.next_of(Ptt) == Ptt(False, ROOM)
        await rig.talk(0.3)
        await rig.next_of(Heard)
    go(tmp_path, fake_bank, body)


def test_a_capped_hold_ends_the_press(tmp_path, fake_bank):
    """max_hold_s: the press ends with the key still down, and nothing more comes of it."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(1.2)
        assert await rig.next_of(Ptt, lambda m: not m.down) == Ptt(False, ROOM)
        await rig.talk(0.3)
        await rig.next_of(Heard)
        await rig.talk(0.5)
        assert len(rig.got(Ptt)) == 2
    go(tmp_path, fake_bank, body, max_hold_s=1.0)


def test_no_microphone_no_press(tmp_path, fake_bank, caplog):
    """A press with no mic open would promise a line that cannot come."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.talk(0.3)
        assert not rig.got(Ptt) and rig.recognizer.calls == []
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body, input_device="")
    assert "ptt_discarded: reason=mic" in caplog.text


def test_a_press_lost_to_a_stream_break_is_dropped_and_said(tmp_path, fake_bank, caplog):
    """M2: the break drops the press; the service stops waiting at once and logs it."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        await rig.next_of(Ptt)
        rig.devices.refresh()  # the output's refresh closes the mic's stream
        await rig.talk(0.2)
        rig.keys.key(False)
        assert await rig.next_of(Ptt) == Ptt(False, ROOM)
        await rig.talk(0.4)
        assert rig.recognizer.calls == [] and not rig.got(Heard)
        assert not rig.service._clips
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "ptt_dropped" in caplog.text


def test_a_clip_that_never_finishes_is_given_up(tmp_path, fake_bank, caplog):
    """A stream that stops delivering in the tail: the wait ends at CLIP_WAIT_S, and a stale
    clip is never taken for a later press."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        await rig.next_of(Ptt)
        rig.keys.key(False)
        await rig.tick(1.2)  # no blocks: the tail never comes
        assert not rig.service._clips and rig.recognizer.calls == []
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.talk(0.3)
        await rig.next_of(Heard)
        assert len(rig.recognizer.calls) == 1
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "ptt_dropped" in caplog.text


def test_a_quick_second_press_keeps_each_channel(tmp_path, fake_bank):
    """The first clip is finished by the second press, and each heard keeps its own room."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.mind.send(Moved(HUMAN, RAID_ROOM))
        await eventually(lambda: rig.service.occupancy.human_room == RAID_ROOM)
        await rig.talk(0.05)
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.talk(0.3)
        await eventually(lambda: len(rig.got(Heard)) == 2, 3.0)
        assert [h.channel for h in rig.got(Heard)] == [ROOM, RAID_ROOM]
    go(tmp_path, fake_bank, body)


def test_the_address_is_matched_against_his_room(tmp_path, fake_bank):
    """Task 9's carry: "Chorus," is Coros only while Coros is in his room."""
    async def main():
        recognizer = FakeRecognizer(default=Transcript("Chorus, you're on adds", 0.9, 1100))

        async def press(rig):
            rig.keys.key(True)
            await rig.talk(0.3)
            rig.keys.key(False)
            await rig.talk(0.3)
            return await rig.next_of(Heard)

        async with Rig(tmp_path, fake_bank, recognizer=recognizer) as rig:
            await rig.snapshot()
            await rig.mind.send(VoiceDef(103, "Coros", spec(103)))
            await rig.mind.send(Moved(103, RAID_ROOM))
            await eventually(lambda: rig.service.occupancy.room_of(103) == RAID_ROOM)
            await rig.tick()
            heard = await press(rig)
            assert heard.text == heard.raw == "Chorus, you're on adds"
            await rig.mind.send(Moved(103, ROOM))
            await eventually(lambda: rig.service.occupancy.room_of(103) == ROOM)
            heard = await press(rig)
            assert heard.text == "Coros, you're on adds" and heard.raw == "Chorus, you're on adds"
            assert rig.service.state()["transcript"][-1]["kind"] == "you"
    run(main())


def test_nothing_heard_sends_nothing(tmp_path, fake_bank, caplog):
    """No speech, a phantom, a confidence under the floor, a recognizer that fails: no heard."""
    recognizer = FakeRecognizer([None, Transcript("thank you", 0.9, 500),
                                 Transcript("mumbling", 0.1, 2000)])

    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        for _ in range(3):
            rig.keys.key(True)
            await rig.talk(0.3)
            rig.keys.key(False)
            await rig.talk(0.3)
        await eventually(lambda: len(recognizer.calls) == 3, 3.0)
        recognizer.error = RuntimeError("onnx said something")
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.talk(0.3)
        await eventually(lambda: len(recognizer.calls) == 4, 3.0)
        await rig.tick()
        assert not rig.got(Heard)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body, recognizer=recognizer)
    assert "stt_no_speech" in caplog.text and "stt_phantom" in caplog.text
    assert "stt_failed" in caplog.text and "onnx said" not in caplog.text


def test_his_words_stay_out_of_the_log_when_a_callback_fails(tmp_path, fake_bank):
    """Review I2, on the real path: _on_heard fails on the loop, with his words among the
    arguments asyncio writes into its record."""
    async def main():
        teardown = logs.setup(tmp_path / "log")
        try:
            recognizer = FakeRecognizer(default=Transcript("my secret plan", 0.9, 1100))
            async with Rig(tmp_path, fake_bank, recognizer=recognizer) as rig:
                await rig.snapshot()
                await rig.tick()

                def broken(*args):
                    raise ValueError("the transcript failed")
                rig.service._say = broken
                rig.keys.key(True)
                await rig.talk(0.5)
                rig.keys.key(False)
                await rig.talk(0.3)
                await rig.next_of(Heard)
        finally:
            teardown()
        text = (tmp_path / "log" / "logs" / "voice.log").read_text(encoding="utf-8")
        assert "_on_heard" in text and "ValueError" in text
        assert "secret" not in text
    run(main())


def test_a_clip_recognized_after_logout_is_never_sent(tmp_path, fake_bank):
    """Review Focus 2: what the recognizer finishes after he went offline goes nowhere."""
    recognizer = FakeRecognizer(default=HEARD)
    release = threading.Event()
    real = recognizer.transcribe

    def slow(audio, hotwords):
        assert release.wait(5)
        return real(audio, hotwords)
    recognizer.transcribe = slow

    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.keys.key(True)
        await rig.talk(0.3)
        rig.keys.key(False)
        await rig.talk(0.3)
        await eventually(lambda: not rig.service._clips)
        await rig.mind.send(Session(False, HUMAN, "orion", "embed"))
        await eventually(lambda: rig.service.mic.state == "closed")
        release.set()
        await eventually(lambda: recognizer.called.is_set())
        await rig.tick()
        await asyncio.sleep(0.05)
        assert not rig.got(Heard)
    go(tmp_path, fake_bank, body, recognizer=recognizer)


def test_a_key_that_will_not_start_is_reported(tmp_path, fake_bank):
    """Windows refused the window: the voices still play, and the status says so."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        assert "ptt_key" in rig.got(Status)[-1].errors and rig.got(Status)[-1].ok
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
    go(tmp_path, fake_bank, body, keys_fail=True)


# -- the devices --------------------------------------------------------------------------------

def test_a_session_opens_and_closes_the_devices(tmp_path, fake_bank):
    """Online: the output, the mic and the key. Offline: the output closes once the fade has
    played."""
    async def body(rig):
        assert not rig.devices.streams and not rig.keys.armed
        await rig.snapshot()
        assert rig.stream("output") and rig.stream("input") and rig.keys.armed
        await rig.mind.send(Session(False, HUMAN, "orion", "embed"))
        await eventually(lambda: rig.service.mic.state == "closed")
        await rig.tick(0.2)  # the 150 ms fade plays out on it
        assert rig.stream("output")
        await rig.tick(0.2)
        assert rig.stream("output") is None and rig.stream("input") is None
        await rig.mind.send(Session(True, HUMAN, "orion", "embed"))
        await eventually(lambda: rig.stream("output") and rig.stream("input"))
    go(tmp_path, fake_bank, body)


def test_a_mic_refresh_leaves_the_output_down_for_one_tick_at_most(tmp_path, fake_bank):
    """Both are polled every tick: a missing mic's refresh closes the output's stream, and the
    next tick brings it back (M22: a missing mic never silences the members)."""
    async def body(rig):
        await rig.snapshot()
        await rig.tick()
        rig.devices.refresh()
        assert rig.stream("output") is None
        await rig.tick(0.02)
        assert rig.stream("output") is not None
    go(tmp_path, fake_bank, body)


def test_a_crash_is_put_right_at_the_start(tmp_path, fake_bank):
    """The level a crash left is restored before anything plays, again at his login."""
    async def main():
        sessions = FakeSessions({"Wow.exe": 0.3})
        state = tmp_path / "data" / "state"
        state.mkdir(parents=True)
        (state / "wow-volume.json").write_text('{"volume": 0.8}\n', encoding="utf-8")
        async with Rig(tmp_path, fake_bank, sessions=sessions):
            await eventually(lambda: sessions.handle("Wow.exe").volume == pytest.approx(0.8))
    run(main())


def test_a_crash_is_put_right_at_his_login_too(tmp_path, fake_bank):
    """WoW was not running when the service started: its level waits for his login."""
    async def main():
        sessions = FakeSessions()
        state = tmp_path / "data" / "state"
        state.mkdir(parents=True)
        (state / "wow-volume.json").write_text('{"volume": 0.8}\n', encoding="utf-8")
        async with Rig(tmp_path, fake_bank, sessions=sessions) as rig:
            handle = sessions.launch("Wow.exe", 0.3)
            await rig.mind.send(Session(True, HUMAN, "orion", "embed"))
            await eventually(lambda: handle.volume == pytest.approx(0.8))
    run(main())


def test_wows_level_is_watched_only_with_the_mic_open(tmp_path, fake_bank, caplog):
    """M30 reads WoW's level over COM: before his session there is nothing to watch for."""
    async def body(rig):
        await rig.clock.advance(3.0, step=0.5)
        assert rig.service._wow_seen is None and not rig.service._wow_reading
        finds = rig.sessions.finds
        await rig.clock.advance(5.0, step=0.5)
        assert rig.sessions.finds == finds
    go(tmp_path, fake_bank, body)


def test_a_change_to_wows_level_is_logged(tmp_path, fake_bank, caplog):
    """M30: Windows' communications ducking: WoW's level moves with the mic open and nothing
    ducking."""
    async def body(rig):
        await rig.snapshot()
        await rig.clock.advance(3.0, step=0.5)
        await eventually(lambda: rig.service._wow_seen == 0.8)
        rig.wow().volume = 0.16
        await rig.clock.advance(3.0, step=0.5)
        await eventually(lambda: rig.service._wow_seen == 0.16)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "wow_volume_changed_externally" in caplog.text


def test_our_own_ducking_is_no_outside_change(tmp_path, fake_bank, caplog):
    async def body(rig):
        await rig.snapshot()
        await rig.clock.advance(3.0, step=0.5)
        await eventually(lambda: rig.service._wow_seen == 0.8)
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await eventually(lambda: rig.wow().volume < 0.5, 3.0)
        await rig.clock.advance(3.0, step=0.5)
        await rig.play_out()
        await rig.clock.advance(3.0, step=0.5)
        await eventually(lambda: rig.wow().volume == pytest.approx(0.8), 3.0)
        await rig.clock.advance(3.0, step=0.5)
        await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, body)
    assert "wow_volume_changed_externally" not in caplog.text


# -- one at a time ------------------------------------------------------------------------------

def test_a_second_service_is_refused(tmp_path, fake_bank):
    async def main():
        async with Rig(tmp_path, fake_bank, connect=False):
            second = Rig(tmp_path, fake_bank, connect=False)
            with pytest.raises(ServiceLocked):
                await second.service.run()
    run(main())


def test_a_port_in_use_stops_the_start_cleanly(tmp_path, fake_bank):
    """The error reaches run()'s caller, and the lock is free again."""
    async def main():
        taken = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        rig = Rig(tmp_path, fake_bank, connect=False)
        rig.service.server._port = taken.sockets[0].getsockname()[1]
        with pytest.raises(OSError):
            await rig.service.run()
        taken.close()
        await taken.wait_closed()
        ServiceLock(rig.data).acquire().release()
    run(main())


# -- state.py -----------------------------------------------------------------------------------

def test_tree_holds_the_rooms_by_id():
    tree = Tree()
    assert tree.rooms == {}
    tree.apply(Channels(ROOMS))
    assert list(tree.rooms) == [ROOM, RAID_ROOM] and tree.rooms[ROOM].name == "Guild Hall"
    tree.apply(Channels(ROOMS[1:]))
    assert list(tree.rooms) == [RAID_ROOM]


def test_occupancy_knows_who_is_where():
    who = Occupancy()
    who.human = HUMAN
    who.apply(Moved(101, ROOM))
    who.apply(Moved(HUMAN, ROOM))
    who.apply(Moved(102, RAID_ROOM))
    assert who.room_of(101) == ROOM and who.room_of(999) == "" and who.human_room == ROOM
    assert who.in_room(ROOM) == {101, HUMAN} and who.in_room("nowhere") == set()
    who.apply(Moved(101, ""))
    assert who.room_of(101) == "" and who.in_room(ROOM) == {HUMAN}
    who.names[101] = "Arkon"
    who.clear()
    assert who.human_room == "" and who.in_room(RAID_ROOM) == set()
    assert who.names == {101: "Arkon"}  # a new mind sends the rooms; the names stay


# -- on the real models (marked `engines`, deselected by default) ---------------------------------

@pytest.mark.engines
def test_a_line_and_his_answer_on_the_real_models(tmp_path, kokoro, parakeet):
    """Kokoro speaks a line of two sentences through the chain on the CPU; what the mixer played
    goes back in at the mic, and Parakeet hears it through a press."""
    from leeroy_voice.voices import StyleBank

    async def main():
        bank = StyleBank(kokoro.voices)
        rig = Rig(tmp_path, bank, recognizer=parakeet, engine=Engine(kokoro))

        async def voice(guid=101):
            await rig.mind.send(Session(True, HUMAN, "orion", "embed"))
            await rig.mind.send(Channels(ROOMS))
            await rig.mind.send(VoiceDef(guid, "Arkon", spec(guid, sha=bank.sha256)))
            await rig.mind.send(Moved(HUMAN, ROOM))
            await rig.mind.send(Moved(guid, ROOM))
            await eventually(lambda: rig.service.occupancy.human_room == ROOM)

        async with rig:
            await voice()
            await rig.mind.send(rig.speak("s1", text="Pull the boss now. Everyone stack up."))
            await rig.sees("s1", "started")
            line = rig.service._jobs["s1"].line
            await eventually(lambda: all(s is not None for s in line.segments), 30.0)
            heard = []
            for _ in range(600):  # up to a minute of audio
                if not rig.service.scheduler.playing():
                    break
                heard.append(rig.out().pump(4800)[:, 0].copy())
                await rig.tick(0.1)
            await rig.sees("s1", "done")
            audio = np.concatenate(heard)
            assert np.abs(audio).max() > 0.05
            rig.keys.key(True)
            await rig.talk(0.2)
            await rig.next_of(Ptt)
            mic = rig.stream("input")
            for at in range(0, len(audio) - 480, 480):
                mic.feed(audio[at:at + 480, None])
                await rig.clock.advance(BLOCK_S, step=BLOCK_S)
            rig.keys.key(False)
            await rig.talk(0.3)
            got = await rig.next_of(Heard, mind=None)
            assert "boss" in got.text.lower() and got.confidence > 0.4
    run(main(), timeout=120.0)
