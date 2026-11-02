"""The voice service: Tasks 2-10 made one running program (draft sections 2-6).

Everything meets on the event loop. The mind's messages, the keys, the page's hooks and the tick
every TICK_S run there, and only there is the scheduler called; each Plan is applied right after
it is computed, and every call first takes the ends the mixer has reported (Task 7's contract).
What takes time runs off it, and posts its result back:

- one synthesis thread: every line's sentences, most urgent tier first, so a p1 overtakes what is
  left of a long p4. It runs the engine and fx.process, and each sentence is one segment.
- a thread for the fx chain of stock clips, so a cached callout never waits behind a live line;
- a thread for the voices' stock renders (StockCache.render_missing and drop_stale, about 6 s a
  member), one member at a time, so neither runs under the other (Task 5's review). It runs
  beside the synthesis thread: as one job it would hold a p0 past its 3 s;
- one recognizer thread, a press at a time.
- optional CPU Turbo has one separate synthesis slot, with no waiting CPU backlog;
  urgent and overflow work continues through the ordinary synthesis thread.

A line: `speak` is refused with `played dropped` when he is offline, she has no voice, he muted
her, or nothing is left to say; the scheduler refuses the rest (not_his_channel, raid, expired,
after_dropped). A line the scheduler takes waits in it while its sentences are synthesized. A
stock clip is the neutral raid-call reading, so a line takes one when it is a raid call, or a
plain line of a sober speaker with no attitude; any other is spoken live (Task 5's carry). It
starts at spec.gain_db + his UI volume + Levers.gain_db, computed as it starts; the mixer adds
the tier offset, and the scheduler's Plan the ducks. Its transcript and speaking light go to the
UI at `started`, so a line he never heard is never in the transcript. Text reaches the UI and
the mind, never the log.

His key (I2): nothing at key-down. A Press with the mind connected and the mic open begins the
capture, sends `ptt down` and holds the scheduler; a Tap reaches nothing; a Press with neither is
discarded at once, and so is a press under way when the mind goes. A Release gives the clip to
the recognizer thread, and `heard` carries latency_ms from the key-up. A clip that has not come
CLIP_WAIT_S after its Release is given up, and each press's room goes with its clip. A press from
before the mind left or the session ended never reaches anyone (`_epoch`).

`session online` opens the output and the mic and arms the key. `offline` disarms, drops the
press, fades what plays, closes the mic, restores WoW, and closes the output once the fade has
played. WoW ducks while any voice plays, and comes back DUCK_HOLD_S after the last one ends.

The expressive engine (Task 22): a member whose spec names it speaks through it, every line,
while it is up. It is kept up beside his session whenever GpuGuard admits it, and it gives way
first: logout, a split model and a dead worker all stop it, and Whisper gives way to it. A line
is never lost to any of that. Down at the speak, the line is that member's Kokoro blend; failing
on a sentence, that sentence and the rest of the line are, with the sounds gone, since Kokoro
reads no tags. Either way `engine_fallback` is logged once a line. An expressive member's stock
clips are rendered offline (cast.render_designed_references) and play with the engine down; the
service never renders them.

The service holds <data dir>/voice.lock for its whole run and writes voice.pid. The key's window
turns a plain taskkill into on_close: a clean stop, WoW's volume restored (M7). A mind gone for
IDLE_EXIT_S ends the session as a logout does, since no one is left to say offline and the mic
must not stay open; a mind back sooner finds everything as it was, and one back later brings the
session back with its snapshot. With no mind and no session for IDLE_EXIT_S the service stops by
itself (M24). Every part of a tick and of the stop is guarded: one failure costs only that part."""
import asyncio
import concurrent.futures
import itertools
import logging
import os
import queue
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from . import __version__, fx, hotkey
from .attitude import RAID_CALLS, kokoro_markup, levers_for
from .audio_io import AudioOut
from .capture import STT_RATE, Capture, Mic
from .config import UiState, VoiceConfig, load_ui_state, save_ui_state
from .decision import Decision
from .duck import WowDucker
from .engines import EngineError
from .hotkey import Press, PttMachine, Release
from .hotmic import render_hotmic
from .lexicon import load_lexicon
from .lock import PID_FILE, ServiceLock, ServiceLocked  # noqa: F401
from .gpu import CHECK_S, RETRY_S, GpuGuard, close_quietly
from .logs import log
from .mixer import SAMPLE_RATE, Mixer
from .schedule import WHISPER, Line, Plan, PlaybackScheduler
from .server import MindServer
from .state import Occupancy, Tree
from .stock import StockCache, stock_key
from .stt import Transcript
from .stt.names import RosterCorrector
from .stt.phantoms import is_phantom
from .textear import ear, sentences
from .ui.server import TRANSCRIPT_ENTRIES, UiServer
from .voices import VoiceSpec, spec_from_dict
from .wire import (BAD_FIELD, SERVER_NAME, TIERS, Cancel, Channels, Error, Heard, Hello, Invite,
                   Join, Message, Moved, Played, Ptt, Raid, Refused, Session, Speak, Status,
                   VoiceDef, WireError, as_fields)

TICK_S = 0.02  # the machine's gate and the capture's ring want 50 ms or less
STATUS_S = 10.0
DUCK_HOLD_S = 0.4  # WoW comes back this long after the last voice ends
IDLE_EXIT_S = 600.0  # no mind this long ends a session; with no session too, the service (M24)
OUTPUT_LINGER_S = 0.3  # the output stays open this long after logout: the fade plays out
CLIP_WAIT_S = 1.0  # a clip not whole this long after its Release is given up
CPU_START_RAM_GIB = 12.0
CPU_MIN_RAM_GIB = 4.0
CPU_FALLBACK_RESERVE_S = 7.0  # bounded worker cleanup (5 s), then Kokoro (2 s)
CPU_MIN_BUDGET_S = 2.0
CPU_STARTUP_BUDGET_S = 60.0  # model loading and one discarded warmup together
CPU_WARMUP_BUDGET_S = 30.0
WOW_WATCH_S = 2.0  # WoW's level is read this often while the mic is open and nothing ducks
WOW_MOVED = 0.005  # a change this large is no rounding
DEBUG_KEEP_S = 24 * 3600.0  # M25
DEBUG_SWEEP_S = 3600.0
MAX_VOICES = 2
MAX_INVITES = 16
FAILURE_LOG_S = 60.0  # a part of the tick that keeps failing is logged this often
SPEAKERS_KEPT = 10  # the recognizer's hotwords: the last members who spoke
JOIN_S = 1.0  # what the stop waits for each worker
RESTART_S = 5.0  # an expressive worker that died comes back after this
WOW_STATE = "wow-volume.json"  # in <data dir>/state


class SystemClock:
    """Unix seconds, speak.expires_ms's time base: the scheduler has one clock for both."""

    @staticmethod
    def now() -> float:
        return time.time()

    @staticmethod
    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(slots=True, eq=False)
class _Job:
    """A line from its speak to its end."""
    speak: Speak
    spec: VoiceSpec
    line: Line
    lever_db: float
    speed: float
    parts: tuple[str, ...]  # its sentences for Kokoro; none for a stock clip
    spoken: tuple[str, ...] = ()  # the same sentences for the expressive engine, when it speaks
    fell_back: bool = False  # Kokoro has the rest of this line
    ended: bool = False  # nothing more of it needs synthesizing
    expressive: object | None = None  # the worker selected when this line arrived
    cpu: bool = False


@dataclass(slots=True, eq=False)
class _Clip:
    """A released press whose clip is on its way."""
    channel: str
    key_up: float
    deadline: float
    epoch: int


def _why(e: Exception) -> str:
    """A failure as the log may keep it: an EngineError's message names no text, and anything
    else may quote the line."""
    return str(e) if isinstance(e, EngineError) else type(e).__name__


def available_ram_gib() -> float | None:
    """CPU fallback fails closed if its optional host-memory probe is unavailable."""
    try:
        import psutil
        return psutil.virtual_memory().available / 2**30
    except Exception:
        return None


class VoiceService:
    """See the module's docstring. `recognizer` None is stt "off". `ui_port` and `mind_port`
    override the config's (tests bind 0). `wow_elevated` and `foreground` default to hotkey's.
    `corrector` is made without names: it gets the roster as the voices come. run() is the
    whole life of the service; stop(), or the key's on_close, ends it."""

    def __init__(self, cfg: VoiceConfig, decision: Decision, data_dir: Path, *, clock, engine,
                 bank, devices, sessions, keys_factory, recognizer, corrector: RosterCorrector,
                 ui_port: int | None = None, mind_port: int | None = None, wow_elevated=None,
                 foreground=None):
        self._cfg = cfg
        self._decision = decision
        self._data = Path(data_dir)
        self._clock = clock
        self._engine = engine
        self._bank = bank
        self._recognizer = recognizer  # the CPU one, always the fallback
        self._whisper = None  # the GPU one, while GpuGuard holds it (Task 21)
        self._gpu: GpuGuard | None = None
        self._whisper_factory = None
        self._gpu_task: asyncio.Task | None = None
        self._expressive = None  # the engine the owner chose, while attached (Task 22)
        self._expressive_cpu = None
        self._cpu_active = False
        self._cpu_check_at = 0.0
        self._cpu_release_task: asyncio.Task | None = None
        self._expressive_stock: StockCache | None = None
        self._expressive_task: asyncio.Task | None = None
        self._expressive_at = 0.0  # no start before this
        self._expressive_why = "not started"  # why it is down, for the log
        self._live_model = self._embed_model = ""
        self._speakers: deque[str] = deque(maxlen=SPEAKERS_KEPT)
        self._corrector = corrector
        self._wow_elevated = wow_elevated or hotkey.wow_elevated
        self._foreground = foreground or hotkey.foreground_process
        self._lexicon = load_lexicon(self._data)
        self._fx = fx.process
        self._hotmic = render_hotmic
        self._hotmic_rng = np.random.default_rng()  # used only on the single TTS thread
        self._ui_path = self._data / "ui-state.json"
        self._ui_state: UiState = load_ui_state(self._ui_path)

        self.tree = Tree()
        self.occupancy = Occupancy()
        self.scheduler = PlaybackScheduler(clock, random.Random(), max_voices=MAX_VOICES)
        self.mixer = Mixer(stereo_spread=cfg.stereo_spread)
        self.output = AudioOut(self.mixer, devices, pinned=cfg.output_device, clock=clock)
        self.capture = Capture(SAMPLE_RATE, 1, max_s=cfg.max_hold_s)
        self.mic = Mic(devices, cfg.input_device, self.capture, clock=clock)
        self.machine = PttMachine(min_press_ms=cfg.min_press_ms, max_hold_s=cfg.max_hold_s)
        self.ducker = WowDucker(sessions, self._data / "state" / WOW_STATE, duck_db=cfg.duck_db)
        self.stock = StockCache(self._data / "cache" / "stock", engine, bank, decision)
        self.server = MindServer(cfg.mind_host, cfg.mind_port if mind_port is None else mind_port,
                                 self._on_message, on_connect=self._on_connect,
                                 on_disconnect=self._on_disconnect, hello=self._hello)
        self.ui = UiServer(cfg.ui_port if ui_port is None else ui_port, self)
        self.keys = keys_factory(cfg.ptt_vk, self._on_key, on_close=self._on_close)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        self._running = False
        self._out: asyncio.Queue[Message] = asyncio.Queue()  # to the mind, never waited on

        self._specs: dict[int, VoiceSpec] = {}
        self._jobs: dict[str, _Job] = {}
        self._speaking: dict[str, int] = {}  # line id -> guid, while it plays
        self._invites: list[dict] = []
        self._transcript: deque[dict] = deque(maxlen=TRANSCRIPT_ENTRIES)
        self._online = False
        self._fresh_mind = False  # the next `channels` starts a new mind's snapshot
        self._roster_stale = True
        self._state_stale = False  # a page's tree or names changed
        self._pressing = False
        self._press_channel = ""
        self._clips: deque[_Clip] = deque()
        self._epoch = 0  # moves on when the mind or the session ends: no press outlives either
        self._ducked = False
        self._unduck_at: float | None = None
        self._output_off_at: float | None = None
        self._idle_since: float | None = None
        self._status_sent: Status | None = None
        self._status_at = 0.0
        self._elevated = False
        self._elevation_at = 0.0
        self._key_failed = False
        self._wow_at = 0.0
        self._wow_seen: float | None = None
        self._wow_reading = False
        self._sweep_at = 0.0
        self._failing: dict[str, float] = {}

        self._tts_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._tts_seq = itertools.count()
        self._tts_thread = threading.Thread(target=self._synth_loop, name="leeroy-voice-tts",
                                            daemon=True)
        self._cpu_job: _Job | None = None  # one admitted CPU job, including handoff to its thread
        self._cpu_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._cpu_thread = threading.Thread(target=self._cpu_synth_loop,
                                            name="leeroy-voice-cpu-tts", daemon=True)
        self._stock_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._stock_lock = threading.Lock()
        self._stock_wanted: dict[int, VoiceSpec] = {}
        self._stock_thread = threading.Thread(target=self._stock_loop, name="leeroy-voice-stock",
                                              daemon=True)
        self._fx_pool = concurrent.futures.ThreadPoolExecutor(1, "leeroy-voice-fx")
        self._stt_pool = concurrent.futures.ThreadPoolExecutor(1, "leeroy-voice-stt")

    # the service's life

    async def run(self) -> None:
        """Until stop(), on_close or the idle exit. Raises ServiceLocked when another service
        holds the data dir, and OSError when a port is taken."""
        lock = ServiceLock(self._data).acquire()
        self._loop = asyncio.get_running_loop()
        self._running = True
        pid = self._data / PID_FILE
        ticker = sender = None
        try:
            pid.write_text(f"{os.getpid()}\n", encoding="ascii")
            await self._begin()
            sender = asyncio.create_task(self._sender())
            ticker = asyncio.create_task(self._ticks())
            await self._stop.wait()
        finally:
            try:
                await self._end(ticker, sender)
            finally:
                pid.unlink(missing_ok=True)
                lock.release()
                self._running = False
                self._finished.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._running:
            await self._finished.wait()

    def pending_synthesis(self) -> int:
        """Sentences waiting for the synthesis thread."""
        return self._tts_queue.qsize()

    async def _begin(self) -> None:
        self._idle_since = self._clock.now()
        (self._data / "state").mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.ducker.restore_after_crash)
        self._sweep(self._clock.now())
        self._tts_thread.start()
        if self._expressive_cpu is not None:
            self._cpu_thread.start()
        self._stock_thread.start()
        await self.ui.start()
        await self.server.start()
        try:
            self.keys.start()
        except OSError as e:  # the voices play on; the status tells the mind
            self._key_failed = True
            log("ptt_key_failed", level=logging.ERROR, error=type(e).__name__)
        if self._gpu is not None:
            self._gpu_task = asyncio.create_task(self._gpu.run())
        log("service_started", engine=self._engine.name, stt=self._stt_name())

    async def _end(self, ticker, sender) -> None:
        for task in (ticker, self._gpu_task, self._expressive_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        now = self._clock.now()
        for step in (lambda: self._session_off(now) if self._online else None, self.mic.close,
                     self.output.stop):
            self._guarded_step(step)
        if self._gpu is not None:
            await self._gpu.settle()
        if self._cpu_release_task is not None:
            await asyncio.gather(self._cpu_release_task, return_exceptions=True)
        if sender is not None:
            try:
                await asyncio.wait_for(self._out.join(), JOIN_S)
            except TimeoutError:
                log("mind_flush_timed_out", level=logging.WARNING)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        for closing in (self.server.close, self.ui.close,
                        lambda: asyncio.to_thread(self.ducker.close)):
            try:
                await closing()
            except Exception as e:
                log("stop_failed", level=logging.ERROR, error=type(e).__name__, exc_info=True)
        self._guarded_step(self.keys.stop)
        self._stop_workers()
        log("service_stopped")

    def _guarded_step(self, step) -> None:
        try:
            step()
        except Exception as e:
            log("stop_failed", level=logging.ERROR, error=type(e).__name__, exc_info=True)

    def _stop_workers(self) -> None:
        if self._expressive is not None:  # first: the synthesis thread may be waiting on it
            self._guarded_step(self._expressive.stop)
        if self._expressive_cpu is not None:
            self._guarded_step(self._expressive_cpu.stop)
        self._tts_queue.put((-1, 0, 0, None))
        self._cpu_queue.put(None)
        self._stock_queue.put(None)
        for thread in (self._tts_thread, self._cpu_thread, self._stock_thread):
            if thread.ident is not None:
                thread.join(JOIN_S)
        self._fx_pool.shutdown(wait=False, cancel_futures=True)
        self._stt_pool.shutdown(wait=False, cancel_futures=True)

    def _post(self, fn, *args) -> None:
        """From a worker to the event loop."""
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop has closed: the service is gone
            pass

    async def _sender(self) -> None:
        while True:
            msg = await self._out.get()
            try:
                await self.server.send(msg)
            except WireError as e:  # a bug here, not a dead mind: the message is lost alone
                log("mind_send_refused", level=logging.ERROR, reply=e.reply)
            finally:
                self._out.task_done()

    def _send(self, msg: Message) -> None:
        self._out.put_nowait(msg)

    async def _ticks(self) -> None:
        parts = (self._tick_scheduler, self._tick_key, self._tick_clips, self._tick_devices,
                 self._tick_duck, self._tick_elevation, self._tick_expressive, self._tick_status,
                 self._tick_page, self._tick_wow, self._tick_sweep, self._tick_idle)
        while True:
            now = self._clock.now()
            for part in parts:
                try:
                    part(now)
                except Exception as e:
                    if now >= self._failing.get(part.__name__, 0.0):
                        self._failing[part.__name__] = now + FAILURE_LOG_S
                        log("tick_failed", level=logging.ERROR, part=part.__name__,
                            error=type(e).__name__, exc_info=True)
            await self._clock.sleep(TICK_S)

    # the scheduler: every call here, on the loop

    def _plan(self, call, *args, **kw) -> None:
        """One scheduler call, after the ends the mixer has reported, each Plan applied as it
        comes."""
        now = self._clock.now()
        for line_id in self.mixer.ended():
            self._apply(self.scheduler.finished(line_id, now), now)
        self._apply(call(*args, now=now, **kw), now)

    def _apply(self, plan: Plan, now: float) -> None:
        jobs = self._jobs
        lines = {line_id: jobs[line_id].line for line_id, _, _ in plan.starts}
        own = {line_id: self._own_db(jobs[line_id]) for line_id, _, _ in plan.starts}
        self.mixer.apply(plan, lines, own)
        for played in plan.played:
            self._note(played)
        self._follow_voices(now)

    def _own_db(self, job: _Job) -> float:
        return (job.spec.gain_db + self._ui_state.volumes.get(job.speak.guid, 0.0)
                + job.lever_db)

    def _note(self, played: Played) -> None:
        job = self._jobs.get(played.id)
        if played.state == "started":
            guid = job.speak.guid
            self._speaking[played.id] = guid
            self.ui.broadcast({"type": "speaking", "guid": guid, "on": True})
            self._note_speaker(self.occupancy.names.get(guid, ""))
            self._say(self.occupancy.names.get(guid, ""), job.speak.text,
                      "whisper" if job.line.whisper else "say")
        else:
            guid = self._speaking.pop(played.id, None)
            if guid is not None:
                self.ui.broadcast({"type": "speaking", "guid": guid, "on": False})
            if job is not None:
                job.ended = True
                del self._jobs[played.id]
            if played.state != "done":
                log(f"line_{played.state}", id=played.id, reason=played.reason)
        self._send(played)

    def _follow_voices(self, now: float) -> None:
        if self.scheduler.playing():
            self._unduck_at = None
            if not self._ducked:
                self._ducked = True
                self.ducker.duck(True)
        elif self._ducked and self._unduck_at is None:
            self._unduck_at = now + DUCK_HOLD_S

    def _unduck(self) -> None:
        self._unduck_at = None
        if self._ducked:
            self._ducked = False
            self.ducker.duck(False)

    # the mind

    def _hello(self) -> Hello:
        x = self._expressive
        engines = (self._engine.name, *(() if x is None else (x.name,)))
        return Hello(SERVER_NAME, __version__, engines, self._cfg.input_device,
                     self.output.device, self._cfg.ptt_key)

    def _on_connect(self) -> None:
        self._fresh_mind = True
        self._status_sent = None  # it goes out at the next tick
        self.ui.broadcast({"type": "mind", "up": True})

    def _on_disconnect(self) -> None:
        self._epoch += 1
        self._release_cpu("mind disconnected")
        self._drop_press()
        self._plan(self.scheduler.drop_waiting, "mind_gone")
        self.ui.broadcast({"type": "mind", "up": False})

    def _on_message(self, msg: Message) -> None:
        match msg:
            case Speak():
                self._on_speak(msg)
            case Session():
                self._on_session(msg)
            case Channels():
                self._on_channels(msg)
            case Moved():
                self._on_moved(msg)
            case VoiceDef():
                self._on_voice(msg)
            case Raid():
                self._plan(self.scheduler.set_raid, msg.phase, msg.lead)
            case Cancel():
                below = TIERS.index(msg.below) if msg.below else -1
                self._plan(self.scheduler.cancel, id=msg.id, guid=msg.guid, below=below)
            case Invite():
                self._on_invite(msg)
            case Refused():
                self._on_refused(msg)

    def _on_session(self, msg: Session) -> None:
        self.occupancy.human = msg.human_guid
        self._live_model, self._embed_model = msg.live_model, msg.embed_model
        if msg.online == self._online:
            return
        if msg.online:
            self._session_on(self._clock.now())
        else:
            self._session_off(self._clock.now())

    def _session_on(self, now: float) -> None:
        self._online = True
        self._expressive_at = 0.0
        self._output_off_at = None
        self._plan(self.scheduler.set_session, True)
        self.output.start(now)
        self.mic.open(now)
        self.keys.armed = True
        self._elevation_at = 0.0
        self._loop.run_in_executor(None, self.ducker.restore_after_crash)

    def _session_off(self, now: float) -> None:
        self._online = False
        self.keys.armed = False
        self._epoch += 1
        if self.machine.held:
            self.machine.key(False, now)  # its Release goes with the press
        if self._pressing:
            self._send(Ptt(False, self._press_channel))
        self._drop_press()
        self._plan(self.scheduler.set_session, False)
        self.mic.close()
        self._unduck()
        self._output_off_at = now + OUTPUT_LINGER_S
        if self._gpu is not None:
            self._gpu.release_all("logout")  # the night model gets the card
        self._release_cpu("logout")

    def _drop_press(self) -> None:
        """A press under way, and every clip on its way, go no further."""
        self.capture.discard()
        self._clips.clear()
        if self._pressing:
            self._pressing = False
            self._plan(self.scheduler.set_ptt, False)
            self.ui.broadcast({"type": "ptt", "down": False})

    def _on_channels(self, msg: Channels) -> None:
        if self._fresh_mind:
            self._fresh_mind = False
            self.occupancy.clear()
        self.tree.apply(msg)
        self._state_stale = True

    def _on_moved(self, msg: Moved) -> None:
        before = self.occupancy.room_of(msg.guid)
        self.occupancy.apply(msg)
        if msg.guid == self.occupancy.human:
            self._plan(self.scheduler.set_human_room, msg.channel)
            self._invites = [i for i in self._invites if i["channel"] != msg.channel]
        elif before != msg.channel:  # a member who left the room they asked him into
            self._invites = [i for i in self._invites
                             if i["from"] != msg.guid or i["channel"] != before]
        self.ui.broadcast({"type": "moved", "guid": msg.guid, "channel": msg.channel})

    def _on_voice(self, msg: VoiceDef) -> None:
        try:
            spec = spec_from_dict(msg.spec)
            if spec.guid != msg.guid:
                raise ValueError("another member's spec")
            if spec.mic not in fx.MIC_PROFILES:
                raise ValueError("no such mic profile")
            self._bank.style(spec, "none", self._decision)
        except (ValueError, TypeError) as e:  # SpecError is a ValueError; it names no text
            log("voice_refused", level=logging.WARNING, guid=msg.guid, reason=str(e))
            self._send(Error(BAD_FIELD + "spec"))
            return
        self.occupancy.names[msg.guid] = msg.name
        self._roster_stale = True
        self._state_stale = True
        if spec != self._specs.get(msg.guid):
            self._specs[msg.guid] = spec
            self._want_stock(spec)

    def _on_invite(self, msg: Invite) -> None:
        invite = {"from": msg.from_, "channel": msg.channel}
        if invite not in self._invites:
            self._invites = [*self._invites, invite][-MAX_INVITES:]
        self.ui.broadcast({"type": "invite", **invite})

    def _on_refused(self, msg: Refused) -> None:
        self._invites = [i for i in self._invites if i["channel"] != msg.channel]
        room = self.tree.rooms.get(msg.channel)
        self._remember("", f"{room.name if room else msg.channel}: {msg.reason}", "system")
        self.ui.broadcast({"type": "refused", "channel": msg.channel, "reason": msg.reason})

    # a line

    def _on_speak(self, msg: Speak) -> None:
        if not msg.id or self.scheduler.knows(msg.id):
            log("speak_refused", level=logging.WARNING, reason="id")
            self._send(Error(BAD_FIELD + "id"))
            return
        reason = self._refusal(msg)
        if reason:
            log("line_dropped", id=msg.id, reason=reason)
            self._send(Played(msg.id, "dropped", reason))
            return
        job, clip = self._job(msg)
        self._jobs[msg.id] = job
        self._plan(self.scheduler.submit, job.line)
        if job.ended:  # refused: not_his_channel, raid, expired, after_dropped
            return
        if clip is not None:
            self._fx_pool.submit(self._stock_ready, job, clip)
        if job.cpu:
            self._cpu_job = job
            self._cpu_queue.put(job)
            return
        for index in range(len(job.parts)):
            self._tts_queue.put((job.line.tier, next(self._tts_seq), index, job))

    def _refusal(self, msg: Speak) -> str:
        if not self._online:
            return "offline"
        if msg.guid not in self._specs:
            return "no_voice"
        if msg.guid in self._ui_state.mutes:
            return "muted"
        return "" if self._ear(msg).text else "empty"

    def _ear(self, msg: Speak):
        return ear(msg.text, lexicon=self._lexicon, engine=self._engine.name)

    def _job(self, msg: Speak) -> tuple[_Job, np.ndarray | None]:
        spec = self._specs[msg.guid]
        levers = levers_for(msg.attitude, msg.state, msg.kind)
        clip = self._stock_clip(msg, spec)
        parts = spoken = ()
        expressive = self._live_expressive() if self._speaks_expressive(spec) else None
        cpu = expressive is not None and expressive is self._expressive_cpu
        fallback_why = self._expressive_why
        if cpu and self._cpu_job is not None:
            expressive, cpu, fallback_why = None, False, "CPU synthesis busy"
        if cpu and (msg.priority != "p3" or msg.kind == "hotmic" or
                    msg.expires_ms / 1000 - self._clock.now() <
                    CPU_FALLBACK_RESERVE_S + CPU_MIN_BUDGET_S):
            expressive, cpu, fallback_why = None, False, "CPU deadline too short"
        if clip is not None:
            expressive, cpu = None, False
        if clip is None:
            if self._speaks_expressive(spec) and expressive is not None:
                spoken, parts = self._expressive_text(msg, levers)
                if cpu:
                    # CPU synthesis can exceed the scheduler's five-second segment gap.
                    # Hand it one bounded clip, preserving every sentence and its voice.
                    spoken, parts = (" ".join(spoken),), (" ".join(parts),)
            else:
                if self._speaks_expressive(spec):
                    log("engine_fallback", id=msg.id, guid=msg.guid, why=fallback_why)
                marked = kokoro_markup(self._ear(msg).text, levers, to_human=msg.to_human)
                parts = tuple(sentences(marked))
        if msg.kind == "hotmic":
            # One scene and one key-up click for the whole utterance, bounded to six
            # seconds even when the model returns more than one sentence.
            parts = (" ".join(parts),)
            spoken = (" ".join(spoken),) if spoken else ()
        line = Line(id=msg.id, guid=msg.guid, tier=TIERS.index(msg.priority), kind=msg.kind,
                    channel=msg.channel, whisper=msg.channel == WHISPER,
                    expires_at=msg.expires_ms / 1000, after=msg.after,
                    segments=[None] * (len(parts) or 1), template=msg.template)
        return (_Job(msg, spec, line, levers.gain_db, spec.speed * levers.speed_mult, parts, spoken,
                     expressive=expressive, cpu=cpu),
                clip)

    def _live_expressive(self):
        if self._expressive is not None and self._expressive.up:
            return self._expressive
        if self._cpu_active and self._expressive_cpu is not None and self._expressive_cpu.up:
            return self._expressive_cpu
        return None

    def _speaks_expressive(self, spec: VoiceSpec) -> bool:
        return self._expressive is not None and spec.engine == self._expressive.name

    def _expressive_text(self, msg: Speak, levers) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """What the engine says, sentence by sentence, and what Kokoro would say of each one if
        the engine fails on it: the same words, one markup for the whole line."""
        said = ear(msg.text, lexicon=self._lexicon, engine=self._expressive.name)
        marked = sentences(kokoro_markup(self._lexicon.apply(" ".join(said.plain)), levers,
                                         to_human=msg.to_human))
        if len(marked) != len(said.plain):  # a lexicon word with a sentence end in it
            marked = [self._lexicon.apply(s) for s in said.plain]
        return said.sentences, tuple(marked)

    def _stock_of(self, spec: VoiceSpec) -> StockCache:
        return self._expressive_stock if self._speaks_expressive(spec) else self.stock

    def _stock_clip(self, msg: Speak, spec: VoiceSpec) -> np.ndarray | None:
        if msg.kind == "hotmic":
            return None  # even a stock phrase needs the TTS-thread-only household scene
        if msg.kind not in RAID_CALLS and (msg.attitude != "none" or msg.state != "sober"):
            return None
        key = stock_key(msg.text)
        return None if key is None else self._stock_of(spec).get(spec, key)

    def _on_segment(self, job: _Job, index: int, clip: np.ndarray) -> None:
        self.scheduler.ready(job.speak.id, index, clip)
        self._plan(self.scheduler.tick)

    def _on_failed(self, job: _Job, why: str) -> None:
        log("synthesis_failed", level=logging.WARNING, id=job.speak.id, guid=job.speak.guid,
            error=why)
        self._plan(self.scheduler.engine_failed, job.speak.id)

    # on the workers

    def _process(self, job: _Job, dry: np.ndarray) -> np.ndarray:
        if job.speak.kind == "hotmic":
            clip = self._hotmic(dry, self._engine.sample_rate, job.spec, rng=self._hotmic_rng,
                                engine=self._engine, bank=self._bank)
        else:
            clip = self._fx(dry, self._engine.sample_rate, job.spec, grit=self._cfg.comms_grit,
                            whisper=job.line.whisper)
        if not np.isfinite(clip).all():  # the mixer would refuse it on the loop, mid-Plan
            raise ValueError("audio that is not finite")
        return clip

    def _dry(self, job: _Job, index: int) -> np.ndarray:
        """The sentence from the member's engine; Kokoro's reading of it, and of the rest of the
        line, once that engine has failed on it."""
        x = job.expressive or self._expressive
        if job.spoken and not job.fell_back:
            try:
                if job.cpu:
                    budget = min(self._cfg.tts_cpu_timeout_s,
                                 job.line.expires_at - self._clock.now() - CPU_FALLBACK_RESERVE_S)
                    if budget < CPU_MIN_BUDGET_S:
                        raise EngineError("CPU voice: insufficient time before expiry")
                    return x.synth(job.spoken[index], job.spec, x.levers(job.speak.attitude),
                                   job.speed, timeout_s=budget)
                return x.synth(job.spoken[index], job.spec, x.levers(job.speak.attitude), job.speed)
            except EngineError as e:
                job.fell_back = True
                log("engine_fallback", level=logging.WARNING, id=job.speak.id,
                    guid=job.speak.guid, why=str(e))
        style = self._bank.style(job.spec, job.speak.attitude, self._decision)
        return self._engine.synth(job.parts[index], job.spec, style, job.speed)

    def _synth_loop(self) -> None:
        while True:
            _, _, index, job = self._tts_queue.get()
            if job is None:
                return
            if job.ended:
                continue
            try:
                clip = self._process(job, self._dry(job, index))
            except Exception as e:
                job.ended = True  # here, so its next sentence is not synthesized meanwhile
                self._post(self._on_failed, job, _why(e))
            else:
                self._post(self._on_segment, job, index, clip)

    def _cpu_synth_loop(self) -> None:
        while (job := self._cpu_queue.get()) is not None:
            try:
                if job.ended:
                    continue
                clip = self._process(job, self._dry(job, 0))
            except Exception as exc:
                job.ended = True
                self._post(self._on_failed, job, _why(exc))
            else:
                if not job.ended:
                    self._post(self._on_segment, job, 0, clip)
            finally:
                self._post(self._cpu_job_finished, job)

    def _cpu_job_finished(self, job: _Job) -> None:
        if self._cpu_job is job:
            self._cpu_job = None

    def _stock_ready(self, job: _Job, dry: np.ndarray) -> None:
        try:
            clip = self._process(job, dry)
        except Exception as e:
            self._post(self._on_failed, job, _why(e))
        else:
            self._post(self._on_segment, job, 0, clip)

    def _want_stock(self, spec: VoiceSpec) -> None:
        with self._stock_lock:
            queued = spec.guid in self._stock_wanted
            self._stock_wanted[spec.guid] = spec  # the latest spec is the one rendered
        if not queued:
            self._stock_queue.put(spec.guid)

    def _stock_loop(self) -> None:
        while (guid := self._stock_queue.get()) is not None:
            with self._stock_lock:
                spec = self._stock_wanted.pop(guid)
            try:
                stock = self._stock_of(spec)
                stock.drop_stale(spec)
                if stock is self.stock:  # an expressive voice's are rendered offline
                    stock.render_missing(spec)
            except Exception as e:
                log("stock_failed", level=logging.WARNING, guid=guid, error=_why(e))

    # his key

    def _on_key(self, down: bool) -> None:
        now = self._clock.now()
        self._key_event(self.machine.key(down, now), now)

    def _key_event(self, event, now: float) -> None:
        if isinstance(event, Press):
            self._press(now)
        elif isinstance(event, Release):
            self._release(event, now)

    def _press(self, now: float) -> None:
        log("ptt_press", foreground=self._foreground_name())
        if not self.server.connected:
            log("ptt_discarded", reason="mind")
        elif not self.mic.ok:
            log("ptt_discarded", reason="mic")
        else:
            self.capture.begin(now)  # the tick's time: the ring is anchored at this call
            self._pressing = True
            self._press_channel = self.occupancy.human_room
            self._send(Ptt(True, self._press_channel))
            self._plan(self.scheduler.set_ptt, True)
            self.ui.broadcast({"type": "ptt", "down": True})

    def _foreground_name(self) -> str:
        try:
            return self._foreground()
        except Exception as e:  # for Task 23's reading only: a press never waits on it
            log("foreground_unread", level=logging.DEBUG, error=type(e).__name__)
            return ""

    def _release(self, event: Release, now: float) -> None:
        if not self._pressing:  # discarded at its Press
            return
        self._pressing = False
        coming = self.capture.end(event.at)
        self._send(Ptt(False, self._press_channel))
        self._plan(self.scheduler.set_ptt, False)
        self.ui.broadcast({"type": "ptt", "down": False})
        if coming:
            self._clips.append(_Clip(self._press_channel, event.at, now + CLIP_WAIT_S,
                                     self._epoch))
        else:
            log("ptt_dropped", reason="stream")

    def _hear(self, pending: _Clip, audio: np.ndarray, now: float) -> None:
        """The clip to the recognizer thread, with what it needs of this moment."""
        if self._roster_stale:
            self._roster_stale = False
            self._corrector = self._corrector.with_names(self.occupancy.names.values())
        who = self.occupancy
        present = sorted(who.names[guid] for guid in who.in_room(pending.channel)
                         if guid != who.human and guid in who.names)
        self._stt_pool.submit(self._recognize, pending, audio, self._hotwords(present),
                              present, self._corrector, round(now * 1000))

    def _recognize(self, pending: _Clip, audio: np.ndarray, hotwords: list[str],
                   present: list[str], corrector: RosterCorrector, stamp: int) -> None:
        if self._cfg.debug_record:
            self._record(audio, stamp)
        whisper = self._whisper
        recognizer = whisper or self._recognizer
        if recognizer is None:
            return
        try:
            heard = recognizer.transcribe(audio, hotwords)
        except Exception as e:  # RuntimeError, ValueError, onnxruntime's own: may quote nothing
            log("stt_failed", level=logging.WARNING, error=type(e).__name__)
            if whisper is None:
                return
            self._post(self._lose_whisper, whisper)  # this press falls back to the CPU one
            try:
                heard = self._recognizer.transcribe(audio, present) if self._recognizer else None
            except Exception as e2:
                log("stt_failed", level=logging.WARNING, error=type(e2).__name__)
                return
        if heard is None:
            log("stt_no_speech", clip_ms=round(len(audio) * 1000 / STT_RATE))
        elif is_phantom(heard, min_confidence=self._cfg.min_confidence):
            log("stt_phantom", speech_ms=heard.speech_ms, confidence=round(heard.confidence, 3))
        else:
            self._post(self._on_heard, pending, heard, corrector.correct(heard.text,
                                                                         present=present))

    def _record(self, audio: np.ndarray, stamp: int) -> None:
        folder = self._data / "debug"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            sf.write(folder / f"ptt-{stamp}.wav", audio, STT_RATE, subtype="FLOAT")
        except (OSError, sf.SoundFileError) as e:
            log("debug_record_failed", level=logging.WARNING, error=type(e).__name__)

    def _on_heard(self, pending: _Clip, heard: Transcript, text: str) -> None:
        if pending.epoch != self._epoch or not self.server.connected:
            log("stt_discarded")
            return
        latency_ms = round((self._clock.now() - pending.key_up) * 1000)
        self._send(Heard(text, heard.text, heard.confidence, pending.channel, heard.speech_ms,
                         latency_ms))
        self._say(self.occupancy.names.get(self.occupancy.human, "You"), text, "you")
        log("heard", speech_ms=heard.speech_ms, latency_ms=latency_ms)

    def _on_close(self) -> None:
        self._stop.set()

    # the page: UiHooks

    def state(self) -> dict:
        who = self.occupancy
        return {
            "tree": [{"id": r.id, "name": r.name, "parent": r.parent, "locked": r.locked,
                      "capacity": r.capacity} for r in self.tree.rooms.values()],
            "where": dict(who.where),
            "names": dict(who.names),
            "room": who.human_room,
            "human": who.human,
            "speaking": sorted(set(self._speaking.values())),
            "ptt": self._pressing,
            "volumes": dict(self._ui_state.volumes),
            "mutes": sorted(self._ui_state.mutes),
            "invites": [dict(i) for i in self._invites],
            "transcript": list(self._transcript),
            "mind": self.server.connected,
            "expressive_device": self._expressive_device(),
            "status": as_fields(self._status()),
        }

    def join(self, channel: str) -> None:
        self._send(Join(channel))

    def volume(self, guid: int, db: float) -> None:
        state = self._ui_state
        self._set_ui(UiState({**state.volumes, guid: db}, state.mutes))

    def mute(self, guid: int, on: bool) -> None:
        state = self._ui_state
        mutes = state.mutes | {guid} if on else state.mutes - {guid}
        self._set_ui(UiState(state.volumes, mutes))
        if on:
            self._plan(self.scheduler.cancel, guid=guid, reason="muted")

    def _set_ui(self, state: UiState) -> None:
        self._ui_state = state
        try:
            save_ui_state(self._ui_path, state)
        except OSError as e:
            log("ui_state_unsaved", level=logging.WARNING, error=type(e).__name__)

    def _remember(self, who: str, text: str, kind: str) -> dict:
        entry = {"who": who, "text": text, "ts": round(self._clock.now() * 1000), "kind": kind}
        self._transcript.append(entry)
        return entry

    def _say(self, who: str, text: str, kind: str) -> None:
        self.ui.broadcast({"type": "transcript", **self._remember(who, text, kind)})

    # the tick

    def _tick_scheduler(self, now: float) -> None:
        self._plan(self.scheduler.tick)

    def _tick_key(self, now: float) -> None:
        self._key_event(self.machine.tick(now), now)
        if self.machine.held:  # a missed key-up never leaves the mic open
            self._key_event(self.machine.watch(self.keys.pressed(), now), now)

    def _tick_clips(self, now: float) -> None:
        while self._clips:
            pending = self._clips[0]
            audio = self.capture.take()
            if audio is not None:
                self._clips.popleft()
                self._hear(pending, audio, now)
            elif now >= pending.deadline:  # the stream stalled in its tail
                self._clips.popleft()
                if not self._pressing:  # a new press would have finished this clip
                    self.capture.discard()
                log("ptt_dropped", reason="clip")
            else:
                return

    def _tick_devices(self, now: float) -> None:
        """Each tick: a refresh by one closes the other's stream, and it comes back here."""
        if self._output_off_at is not None and now >= self._output_off_at:
            self._output_off_at = None
            self.output.stop()
        self.output.poll(now)
        self.mic.poll(now)

    def _tick_duck(self, now: float) -> None:
        if self._unduck_at is not None and now >= self._unduck_at:
            self._unduck()

    def _tick_elevation(self, now: float) -> None:
        if self._online and now >= self._elevation_at:
            self._elevation_at = now + STATUS_S
            try:
                self._elevated = bool(self._wow_elevated())
            except Exception as e:  # OSError from Windows, psutil's own: this reading is lost
                log("wow_elevation_unread", level=logging.DEBUG, error=type(e).__name__)

    def _stt_name(self) -> str:
        active = self._whisper or self._recognizer
        return "off" if active is None else active.name

    # the GPU (Task 21)

    @property
    def online(self) -> bool:
        return self._online

    @property
    def live_model(self) -> str:
        return self._live_model

    @property
    def embed_model(self) -> str:
        return self._embed_model

    @property
    def clock(self):
        return self._clock

    @property
    def wants_whisper(self) -> bool:
        return (self._cfg.recognizer == "auto" and self._whisper_factory is not None
                and self._whisper is None)

    def attach_gpu(self, guard: GpuGuard, whisper_factory) -> None:
        """Before run(). `whisper_factory()` builds the GPU recognizer (on a thread); with
        stt.recognizer "parakeet" it is never called."""
        self._gpu, self._whisper_factory = guard, whisper_factory

    def attach_expressive(self, engine, *, cpu_fallback=None) -> None:
        """Before run(), with attach_gpu: without the guard it never starts."""
        self._expressive = engine
        self._expressive_cpu = (cpu_fallback if self._cfg.tts_cpu_fallback and
                                engine.name == "chatterbox-turbo" else None)
        self._expressive_stock = StockCache(self._data / "cache" / "stock", engine, self._bank,
                                            self._decision)

    def _tick_expressive(self, now: float) -> None:
        x, guard = self._expressive, self._gpu
        if x is None or guard is None or self._expressive_task is not None:
            return
        if self._cpu_release_task is not None and not self._cpu_release_task.done():
            return
        if self._cpu_active:
            if not self._expressive_cpu.up:
                self._cpu_active = False
                self._expressive_why, self._expressive_at = "CPU worker stopped", now + RESTART_S
                self._state_stale = True
                log("engine_lost", level=logging.WARNING, engine=x.name, device="cpu")
            elif now >= self._cpu_check_at:
                self._cpu_check_at = now + CHECK_S
                available = available_ram_gib()
                if available is None or available < CPU_MIN_RAM_GIB:
                    self._release_cpu("host memory unavailable")
            return  # keep one device for this session; do not flap back to CUDA
        if x.name in guard.holders and not x.up:  # it died: nothing is left to unload
            guard.drop(x.name)
            self._expressive_why, self._expressive_at = "the worker stopped", now + RESTART_S
            log("engine_lost", level=logging.WARNING, engine=x.name)
        elif (self._online and not x.up and now >= self._expressive_at
              and any(s.engine == x.name for s in self._specs.values())):
            self._expressive_task = asyncio.create_task(self._start_expressive(x, guard))
            self._expressive_task.add_done_callback(self._expressive_settled)

    async def _start_expressive(self, x, guard: GpuGuard) -> None:
        reason = await guard.acquire(x.name, x.need_gb, self._let_expressive_go)
        if reason is not None:
            self._expressive_at = self._clock.now() + CHECK_S
            if reason != self._expressive_why:
                log("engine_refused", engine=x.name, why=reason)
            self._expressive_why = reason
            if self._expressive_cpu is not None:
                await self._start_cpu_expressive()
            return
        self._expressive_why = "loading"
        try:
            await asyncio.to_thread(x.start)
        except Exception as e:  # EngineError; anything else may name a path
            guard.drop(x.name)
            self._expressive_why, self._expressive_at = _why(e), self._clock.now() + RETRY_S
            log("engine_start_failed", level=logging.WARNING, engine=x.name, error=_why(e))
            return
        if x.name in guard.holders and self._online:
            guard.confirm(x.name)
            self._state_stale = True
            log("gpu_loaded", holder=x.name)
        else:  # released while it loaded (logout, a split): keep nothing
            guard.drop(x.name)
            await asyncio.to_thread(x.stop)

    def _expressive_settled(self, task: asyncio.Task) -> None:
        self._expressive_task = None
        if not task.cancelled() and task.exception() is not None:
            self._expressive_at = self._clock.now() + RETRY_S
            log("engine_start_failed", level=logging.WARNING, engine=self._expressive.name,
                error=type(task.exception()).__name__)

    async def _let_expressive_go(self) -> None:
        """GpuGuard's unload: he logged out, a model split or went missing."""
        self._expressive_why, self._expressive_at = "released", self._clock.now() + RETRY_S
        await asyncio.to_thread(self._expressive.stop)
        self._state_stale = True

    async def _start_cpu_expressive(self) -> None:
        cpu, epoch = self._expressive_cpu, self._epoch
        spec = next((s for s in sorted(self._specs.values(), key=lambda s: s.guid)
                     if s.engine == cpu.name), None)
        available = available_ram_gib()
        if (spec is None or not self._online or not self.server.connected or available is None
                or available < CPU_START_RAM_GIB):
            self._expressive_at = self._clock.now() + RETRY_S
            return
        self._expressive_why = "loading CPU"
        cancelled = threading.Event()
        deadline = time.monotonic() + CPU_STARTUP_BUDGET_S

        def load():
            if (not cancelled.is_set() and self._online and self.server.connected
                    and self._epoch == epoch):
                cpu.start()
                if (not cancelled.is_set() and self._online and self._epoch == epoch):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise EngineError("CPU voice: startup deadline reached")
                    cpu.warmup(spec, timeout_s=min(CPU_WARMUP_BUDGET_S, remaining))
                # The thread still reaches this check if its awaiting task is cancelled.
                if cancelled.is_set() or not self._online or self._epoch != epoch:
                    cpu.stop()

        loading = asyncio.create_task(asyncio.to_thread(load))
        try:
            while not loading.done():
                await asyncio.wait({loading}, timeout=.25)
                available = available_ram_gib()
                if (time.monotonic() >= deadline or not self._online
                        or self._epoch != epoch or available is None
                        or available < CPU_MIN_RAM_GIB):
                    cancelled.set()
                    await asyncio.to_thread(cpu.stop)
            await loading
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.to_thread(cpu.stop)
            loading.cancel()
            raise
        except Exception as exc:
            await asyncio.to_thread(cpu.stop)
            self._expressive_why, self._expressive_at = _why(exc), self._clock.now() + RETRY_S
            log("engine_start_failed", level=logging.WARNING, engine=cpu.name,
                device="cpu", error=_why(exc))
            return
        available = available_ram_gib()
        if (not cancelled.is_set() and self._online and self._epoch == epoch and cpu.up
                and available is not None and available >= CPU_MIN_RAM_GIB):
            self._cpu_active = True
            self._cpu_check_at = self._clock.now() + CHECK_S
            self._state_stale = True
            log("engine_loaded", engine=cpu.name, device="cpu")
        else:
            await asyncio.to_thread(cpu.stop)

    def _release_cpu(self, why: str) -> None:
        if self._expressive_cpu is None:
            return
        self._cpu_active = False
        self._state_stale = True
        self._expressive_why, self._expressive_at = why, self._clock.now() + RETRY_S
        if self._cpu_release_task is None or self._cpu_release_task.done():
            self._cpu_release_task = asyncio.create_task(asyncio.to_thread(self._expressive_cpu.stop))

    def _expressive_device(self) -> str:
        active = self._live_expressive()
        return "none" if active is None else "cpu" if active is self._expressive_cpu else "cuda"

    def build_whisper(self):
        return self._whisper_factory()

    def hold_whisper(self, recognizer) -> None:
        self._whisper = recognizer
        self._status_at = 0.0  # status.gpu and stt go out at the next tick

    def drop_whisper(self) -> None:
        """The CPU recognizer takes over at once; the model is freed on the recognizer thread,
        after any press it is hearing."""
        recognizer, self._whisper = self._whisper, None
        self._status_at = 0.0
        if recognizer is not None:
            try:
                self._stt_pool.submit(close_quietly, recognizer)
            except RuntimeError:  # the pool has shut down with the service
                close_quietly(recognizer)

    def _lose_whisper(self, recognizer) -> None:
        if recognizer is self._whisper:
            if self._gpu is not None:
                self._gpu.drop("whisper")
            self.drop_whisper()

    def _note_speaker(self, name: str) -> None:
        if not name:
            return
        if name in self._speakers:
            self._speakers.remove(name)
        self._speakers.append(name)

    def _hotwords(self, present: list[str]) -> list[str]:
        """His room's occupants, then the last members who spoke (most recent first)."""
        return [*present, *(n for n in reversed(self._speakers) if n not in present)]

    def _status(self) -> Status:
        errors = (*(("ptt_key",) if self._key_failed else ()),
                  *(("wow_elevated",) if self._elevated else ()))
        return Status(self.output.ok and self._engine is not None, self.mic.state,
                      self._gpu_holder(), self._stt_name(), self.ui.clients > 0,
                      self.scheduler.waiting(), errors)

    def _gpu_holder(self) -> str:
        """What the mind pins Ollama's models beside (Task 21)."""
        x = self._expressive
        if x is not None and x.up:
            return x.name
        return "none" if self._whisper is None else self._whisper.name

    def _tick_status(self, now: float) -> None:
        status = self._status()
        changed = status != self._status_sent
        if changed or now >= self._status_at:
            self._status_sent, self._status_at = status, now + STATUS_S
            self._send(status)
            if changed:
                self.ui.broadcast({"type": "status", **as_fields(status)})

    def _tick_page(self, now: float) -> None:
        if self._state_stale:
            self._state_stale = False
            self.ui.broadcast({"type": "state", **self.state()})

    def _tick_wow(self, now: float) -> None:
        """Windows' communications ducking would show here (M30): WoW's level moving with the
        mic open and nothing of ours ducking it."""
        if self._wow_reading or now < self._wow_at or self._ducked or not self.mic.ok:
            return
        self._wow_at = now + WOW_WATCH_S
        self._wow_reading = True
        read = self._loop.run_in_executor(None, self.ducker.level)
        read.add_done_callback(self._wow_read)

    def _wow_read(self, read: asyncio.Future) -> None:
        self._wow_reading = False
        level = None if read.cancelled() or read.exception() is not None else read.result()
        if level is None or self._ducked:
            return
        seen, self._wow_seen = self._wow_seen, level
        if seen is not None and abs(level - seen) > WOW_MOVED:
            log("wow_volume_changed_externally", level=logging.WARNING, before=round(seen, 4),
                after=round(level, 4))

    def _tick_sweep(self, now: float) -> None:
        if now >= self._sweep_at:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        """M25: what debug.record kept goes after 24 hours, whatever the flag says now."""
        self._sweep_at = now + DEBUG_SWEEP_S
        folder = self._data / "debug"
        if not folder.is_dir():
            return
        for path in folder.glob("*.wav"):
            try:
                if now - path.stat().st_mtime > DEBUG_KEEP_S:
                    path.unlink()
            except OSError as e:
                log("debug_sweep_failed", level=logging.WARNING, error=type(e).__name__)

    def _tick_idle(self, now: float) -> None:
        if self.server.connected:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= IDLE_EXIT_S:
            if self._online:
                log("session_abandoned")
                self._session_off(now)
                self._idle_since = now  # M24's count starts here
            else:
                log("idle_exit")
                self._stop.set()
