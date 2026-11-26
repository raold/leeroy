"""The voice extension: attach_voice(app) builds it at the end of MindApp.__init__ and starts
nothing. Only an enabled voice (the default; [voice] enabled = false turns it off) registers a
background task (the client's reconnect loop and a 1 s tick) and gives the talk engine its Comms.

With `autostart`, the mind starts leeroy-voice when it starts and nothing listens, and a
supervisor starts it again whenever it has been unreachable for RELAUNCH_AFTER_S, then at
RELAUNCH_BACKOFF_S intervals while it stays down. A stop an operator asked for (the console's,
or scripts/ac-voice.sh's) leaves voice.stopped in its data dir: that holds it down until his
next login, when the mind starts it at once. Any start removes the hold, and a hold older than
the service's last greeting (started by hand since) no longer counts."""
import asyncio
import json
import logging
import os
import random
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Callable

from leeroy_mind.clock import night_of
from leeroy_mind.config import ConfigError
from leeroy_mind.lifecycle import release_voice_hold, voice_hold
from leeroy_mind.logs import log as event
from leeroy_mind.voice import wire
from leeroy_mind.voice.brief import SpecWatch, load_specs
from leeroy_mind.voice.client import VoiceClient, VoiceDown
from leeroy_mind.voice.comms import Comms, roll_on_comms
from leeroy_mind.voice.config import OFF as VOICE_OFF, VoiceCfg, load_voice_config
from leeroy_mind.voice.elsewhere import ElsewhereTalk
from leeroy_mind.voice.gpu import OllamaPins
from leeroy_mind.voice.heard import HeardHandler
from leeroy_mind.voice.hotmic import HotMic
from leeroy_mind.voice.movement import Movement
from leeroy_mind.voice.rooms import BY_ID, HOME, RoomState
from leeroy_mind.voice.store import VoiceStore

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]  # mind/leeroy_mind/voice/attach.py
PROBE_S = 1.0  # ensure_service: how long a probe of the port may take
SUPERVISE_S = 5.0  # how often the supervisor looks at a service that is down
RELAUNCH_AFTER_S = 60.0  # unreachable this long before the mind starts it again
START_GRACE_S = 90.0  # a launch loads its models before it listens: no second one sooner
RELAUNCH_BACKOFF_S = (120.0, 300.0, 600.0, 1800.0)  # between its own launches; the last repeats
STABLE_S = 600.0  # up this long, the next outage starts the backoff afresh (a crash loop doesn't)
TICK_S = 1.0
RESTORE_S = 1800.0  # seats saved at a clean stop are restored if he is back within this
STOP_KEY = "voice_stop_seats"  # meta: the seats at the last clean stop
VOICE_GPU = "voice_gpu"  # independent of the human's live-model lease
GPU_STOP_S = 2.0  # owned admin tasks must not hold up extension shutdown


def voice_data_dir() -> Path:
    env = os.environ.get("LEEROY_VOICE_DATA")
    return Path(env) if env else REPO_ROOT / "voice-data"


class VoiceExtension:
    def __init__(self, app, cfg: VoiceCfg):
        self.app = app
        self.cfg = cfg
        self.run_id = f"{int(app.clock.now()):x}"
        self.specs: dict[int, dict] = {}  # read at each session start (snapshot)
        self.rooms = RoomState(officers=self._officers, human=lambda: app.human_guid)
        self.on_comms: set[int] = set()  # tonight's roll, for the session in progress
        self.store: VoiceStore | None = VoiceStore(app.store) if cfg.enabled else None
        self._seated_sid: int | None = None  # the session the rooms were seated for
        self._raid_sent = ("none", 0)  # the (phase, lead) the service last heard
        self.comms: Comms | None = Comms(self) if cfg.enabled else None
        self.heard: HeardHandler | None = HeardHandler(self) if cfg.enabled else None
        self.dc: dict[int, bool] = {}  # tank -> the latest dc_status's `enabled`
        # Later tasks fill these in
        self.movement = None
        self.elsewhere = self.hotmic = self.pins = None
        self._policy_tasks: dict[str, asyncio.Task] = {}
        self.tickers: list[Callable[[], None]] = []  # called once per tick
        self.gpu_pins = OllamaPins(app.cfg.ollama.url)
        self._gpu_held = False  # the service's last status.gpu was not "none"
        self._gpu_tasks: set[asyncio.Task] = set()
        self._gpu_handbacks: set[asyncio.Task] = set()
        self._gpu_arbiter = None  # retained across disconnect: a lost socket proves no release
        self._gpu_generation = 0
        self._gpu_release_generation = -1
        self._gpu_closing = False
        self._pin_lock = asyncio.Lock()
        if self.comms is not None:
            self.tickers += [self._tick_raid, self.comms.age_out, SpecWatch(self)]
            self.elsewhere = ElsewhereTalk(self, lines_per_10min=cfg.elsewhere_lines_per_10min,
                                           rng=random.Random())
            self.hotmic = HotMic(self, per_month=cfg.hot_mic_per_month, rng=random.Random())
            self.movement = Movement(self, rng=random.Random())
            self.tickers.append(lambda: self.movement.tick(app.clock.now()))
        self._service_checked = False
        # The supervisor (autostart): clock times, except _greeted_wall (a hold file's mtime
        # is compared with it)
        self._down_since: float | None = app.clock.now()  # the mind starts unconnected
        self._launched_at: float | None = None  # the mind's last launch
        self._launches = 0  # the supervisor's, since the service last stayed up STABLE_S
        self._up_since: float | None = None
        self._greeted_wall = 0.0
        self._greeted_hold = None
        self._login_due = False  # his login: start a service that is down at once
        self._login_wall = 0.0  # time.time() of that login: a hold written after it still holds
        self._login_hold = None
        self._held_logged = self._skipped_logged = False
        self.client = VoiceClient(cfg.host, cfg.port, self.on_message, clock=app.clock,
                                  on_connection=self.on_connection)

    # -- the connection ------------------------------------------------------------------

    async def on_message(self, msg: wire.Message) -> None:
        """What the service sends. The client logs a failure."""
        notes = getattr(self.app,'voice_notes',None)
        if notes is not None and notes.receive(msg):
            return
        if isinstance(msg, wire.Status):
            await self._on_gpu(msg.gpu)
            if self.movement is not None:
                self._guard("room_status", lambda: self.movement.tick_rooms(self.app.clock.now()))
        if self.comms is None:
            return
        if isinstance(msg, wire.Played):
            self.comms.played(msg)
        elif isinstance(msg, wire.Ptt):
            self.heard.on_ptt(msg)
        elif isinstance(msg, wire.Heard):
            await self.heard.on_heard(msg)
        elif isinstance(msg, wire.Join):
            await self.heard.on_join(msg)
            if self.movement is not None:
                self._guard("room_join", lambda: self.movement.tick_rooms(self.app.clock.now()))

    async def _on_gpu(self, gpu: str) -> None:
        """Each time the service takes VRAM, pin the models beside it; when it lets go, unpin
        them. Run as a task, one at a time and in order, so a hung Ollama never stalls the
        client's receive loop (Ptt, Heard, Played)."""
        if self._gpu_closing:
            return
        if getattr(getattr(self.app, 'gpu', None), 'paused', False):
            if gpu == 'none':
                self._gpu_held = False
                self._gpu_arbiter = None
                await self.app.arbiter.release_live(VOICE_GPU)
            return
        was, self._gpu_held = self._gpu_held, gpu != "none"
        # Take the lease before yielding, so a scheduler tick cannot admit reflection
        # between this status and the admin task. CPU rendering reports gpu="none".
        arbiter = getattr(self.app, "arbiter", None)
        if self._gpu_held and arbiter is not None:
            self._gpu_arbiter = arbiter
            if arbiter.hold(VOICE_GPU):
                task = asyncio.create_task(arbiter.hand_over(), name="voice-gpu-handback")
                self._gpu_handbacks.add(task)
                task.add_done_callback(self._handback_done)
        if was != self._gpu_held or (not self._gpu_held and self._gpu_arbiter is not None
                                    and self._gpu_release_generation != self._gpu_generation):
            self._gpu_generation += 1
            if not self._gpu_held:
                self._gpu_release_generation = self._gpu_generation
            task = asyncio.create_task(self._pin_job(self._gpu_held, self._gpu_generation))
            self._gpu_tasks.add(task)
            task.add_done_callback(self._gpu_tasks.discard)

    def _handback_done(self, task: asyncio.Task) -> None:
        self._gpu_handbacks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            event("voice_handback_failed", level=logging.WARNING, reason=type(exc).__name__)

    async def handback(self) -> None:
        """Live generation waits for a voice-initiated GPU handback too."""
        while self._gpu_handbacks:
            await asyncio.wait(set(self._gpu_handbacks))

    async def _pin_job(self, pin: bool, generation: int) -> None:
        async with self._pin_lock:
            if self._gpu_closing or getattr(getattr(self.app, 'gpu', None), 'paused', False):
                return
            models = self.app.cfg.models
            try:
                if pin:
                    await self.gpu_pins.pin(models.live, models.embedding)
                else:
                    await self.gpu_pins.unpin(models.live, models.embedding)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                event("voice_pin_failed", level=logging.WARNING, reason=type(exc).__name__)
            if not pin and (server := getattr(self.app, 'server', None)) is not None and server.lease.held:
                # Even a partial unpin can shorten the live runner's lifetime.
                # Restore through the arbiter's serialized hand-over before
                # releasing voice; the server may have acquired during the I/O.
                try:
                    await self.app.arbiter.hand_over()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    event('voice_server_pin_failed', level=logging.WARNING, reason=type(exc).__name__)
            # Do not let queued pin work reload the live model after the night was admitted.
            # A disconnect or newer GPU status supersedes this proof that the GPU was freed.
            if not pin and generation == self._gpu_generation and not self._gpu_held:
                arbiter, self._gpu_arbiter = self._gpu_arbiter, None
                if arbiter is not None:
                    await arbiter.release_live(VOICE_GPU)

    async def _close_gpu(self) -> None:
        """Drain owned admin tasks and release the lease only at extension shutdown.

        Disconnect alone retains a held lease: the remote worker could still own VRAM.
        A reconnect must report gpu="none" to permit a night, or the app must shut down.
        """
        self._gpu_closing = True
        self._gpu_generation += 1
        tasks = set(self._gpu_tasks) | set(self._gpu_handbacks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=GPU_STOP_S)
            if pending:
                event("voice_gpu_cleanup_timeout", level=logging.WARNING, tasks=len(pending))
        arbiter, self._gpu_arbiter = self._gpu_arbiter, None
        if arbiter is not None:
            await arbiter.release_live(VOICE_GPU)

    async def on_connection(self, up: bool) -> None:
        now = self.app.clock.now()
        if up:
            self._down_since, self._up_since, self._login_due = None, now, False
            self._greeted_wall = time.time()
            self._greeted_hold = voice_hold(voice_data_dir())
            self._held_logged = self._skipped_logged = False
        else:
            if self._up_since is not None and now - self._up_since >= STABLE_S:
                self._launches = 0
            self._down_since, self._up_since = now, None
        if self.movement is not None:
            self._guard("room_connection", lambda: self.movement.on_connection(up))
        if not up:
            self._gpu_generation += 1
            self._gpu_held = False
            for task in list(self._gpu_tasks):
                task.cancel()
            if self.comms is not None:
                self.comms.release()  # a press the dead service can't end (NP5)
                self.comms.disconnected()
            return
        try:
            for msg in self.snapshot():
                self.send(msg)
        except Exception as exc:
            self._failed("snapshot", exc)

    def _failed(self, hook: str, exc: Exception) -> None:
        """A hook of ours failed: logged by name and exception type, never with chat text, and
        never allowed to reach the client's loop or the mind's."""
        event("voice_handler_failed", level=logging.WARNING, hook=hook,
              reason=type(exc).__name__)

    def _officers(self) -> set[int]:
        return {g for g, p in self.app.personas.all().items() if p.play.guild_role == "officer"}

    # -- his session: the rooms and who is on comms --------------------------------------

    async def _on_login(self, ev) -> None:
        if ev.human:
            # The supervisor starts a service his login finds down, held or not
            self._login_due, self._login_wall = not self.client.greeted, time.time()
            self._login_hold = voice_hold(voice_data_dir())
            self._guard("login", self.begin_session)
        elif self.movement is not None:
            self._guard("member_login", lambda: self.movement.on_login(ev.guid))

    async def _on_logout(self, ev) -> None:
        if self.movement is not None:
            self._guard("member_logout", lambda: self.movement.on_logout(ev.guid))
        if ev.human:
            self._guard("logout", self.end_session)
        else:
            self.send(wire.Cancel(guid=ev.guid))

    async def _on_hello(self, ev) -> None:
        self.dc.clear()  # mod-leeroy tells each new connection who is running, after the hello
        self._guard("hello", self.begin_session if ev.human_guid else self.end_session)
        if ev.human_guid and self.movement is not None:
            self._guard("room_hello", self.movement.on_hello)

    async def _on_chat(self, ev) -> None:
        self._guard("chat", lambda: self.heard.stored(ev))

    async def _on_dc_status(self, ev) -> None:
        self.dc[ev.tank_guid] = ev.enabled

    async def _on_group(self, ev) -> None:
        self._guard("room_group", lambda: self.movement.on_group(ev))

    async def _on_afk(self, ev) -> None:
        self._guard("room_afk", lambda: self.movement.on_afk(ev))

    def dc_run(self) -> bool:
        """A dungeon-clear run in his group: a tank in it whose latest status says enabled. No
        status, or a tank outside his group, is no run (I9). mod-leeroy publishes both edges of
        a run with an empty payload (LeeroyPollDcTanks), so `dc` mirrors its list of tanks."""
        world = self.app.world
        return any(on and world.in_group(tank) for tank, on in self.dc.items())

    def echoes_command(self, ev) -> bool:
        """`ev` is the game's report of a command said for him (HeardHandler.is_echo)."""
        return self.heard is not None and self.heard.is_echo(ev)

    def _guard(self, hook: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._failed(hook, exc)

    def begin_session(self) -> None:
        """His login, or a hello with him online: roll tonight's comms, restore the session's
        open seats (a mind restart mid-session) or seat everyone on comms and him in the guild
        hall, and send the snapshot. A repeat for the same session only re-sends it."""
        app, human = self.app, self.app.human_guid
        sid = app.session_id
        if sid is None or not app.world.human_online or self.store is None:
            return
        if self._seated_sid != sid:
            now = app.clock.now()
            if self.movement is not None:
                self.movement.reset(now)
            self.specs = load_specs(app.data_dir)
            in_cast = set(app.store.cast()) | {human}
            open_rows = self.store.open_presence(sid)  # a crash left these open
            saved = self._saved_seats(now) if not open_rows else {}  # a clean stop saved these
            seats = {g: (r, by) for g, (r, by) in (open_rows or saved).items()
                     if r in BY_ID and g in in_cast}
            if self.movement is not None:
                seats = self.movement.restored_seats(seats, crashed=bool(open_rows))
            for g in open_rows.keys() - seats.keys():
                self.store.leave(g, now)  # left the guild while the mind was down
            self.rooms.clear()
            self.store.close_all(now, keep_session=sid)
            if seats:
                for g, (r, by) in seats.items():
                    self.rooms.move(g, r, by)
                self.on_comms = set(seats) - {human}
            else:
                self.on_comms = self._roll()
                for g in (*sorted(self.on_comms), human):
                    self.rooms.move(g, HOME)
            if human not in self.rooms.where:
                self.rooms.move(human, HOME)
            for g, r in self.rooms.where.items():
                if g not in open_rows or open_rows[g][0] != r:
                    self.store.enter(g, r, self.rooms._by.get(g, 0), now, sid)
            self._seated_sid = sid
            if self.movement is not None:
                if app.world.group is not None:
                    self.movement.on_group(app.world.group)
                self.movement.tick_rooms(now)
            for msg in self.snapshot(reload=False):
                self.send(msg)
        else:
            for msg in self.snapshot():
                self.send(msg)

    def _saved_seats(self, now: float) -> dict[int, tuple[str, int]]:
        """The seats shutdown() saved, if the stop was recent; consumed either way."""
        raw = self.app.store.get_meta(STOP_KEY)
        if not raw:
            return {}
        self.app.store.set_meta(STOP_KEY, "")
        try:
            data = json.loads(raw)
            if now - float(data["at"]) > RESTORE_S:
                return {}
            return {int(g): (str(r), int(by)) for g, (r, by) in data["seats"].items()}
        except (ValueError, KeyError, TypeError):
            return {}

    def shutdown(self) -> None:
        """A clean stop: the seats are saved (so a start within the evening restores them) and
        every open stretch closes now, so the time the mind is down never counts as time in a
        room."""
        if self.store is None or self._seated_sid is None:
            return
        now = self.app.clock.now()
        if self.movement is not None:
            self.movement.prepare_shutdown(now)
        seats = {str(g): [r, self.rooms._by.get(g, 0)] for g, r in self.rooms.where.items()}
        self.app.store.set_meta(STOP_KEY, json.dumps({"at": now, "seats": seats}))
        self.store.close_all(now)
        self.rooms.clear()
        self.on_comms = set()
        self._seated_sid = None

    def _roll(self) -> set[int]:
        app, cfg = self.app, self.cfg
        people = {g: p for g, p in app.personas.all().items() if g != app.human_guid}
        missing = sum(1 for g in people if g not in self.specs)
        if missing:
            event("voice_spec_missing", count=missing)
        officers = self._officers()
        return roll_on_comms(night_of(app.clock.local()), people,
                             raider_share=cfg.on_comms_raider, social_share=cfg.on_comms_social,
                             is_officer=officers.__contains__,
                             has_voice=self.specs.__contains__)

    def end_session(self) -> None:
        """His logout (or a hello saying he is gone): every open stretch closes, the rooms
        empty, and the service hears `session online=false`."""
        if self.store is None:
            return
        for task in self._policy_tasks.values():
            task.cancel()
        if self.movement is not None:
            self.movement.reset(self.app.clock.now())
        self.store.close_all(self.app.clock.now())
        if self.app.store.get_meta(STOP_KEY):
            self.app.store.set_meta(STOP_KEY, "")
        seated = self._seated_sid is not None
        self.rooms.clear()
        self.on_comms = set()
        self._seated_sid = None
        if seated:
            app = self.app
            self.send(wire.Session(False, app.human_guid, app.cfg.models.live,
                                   app.cfg.models.embedding))

    def send(self, msg: wire.Message) -> None:
        try:
            self.client.send(msg)
        except VoiceDown:
            pass
        except wire.WireError as exc:
            event("voice_message_dropped", level=logging.WARNING, type=type(msg).__name__,
                  reply=exc.reply)

    def snapshot(self, reload: bool = True) -> list[wire.Message]:
        """What the service is told on every (re)connect, in the protocol's order: session,
        channels, one voice per spec, one moved per seat (members, then him), then raid."""
        app = self.app
        if reload:
            self.specs = load_specs(app.data_dir)
        if self.comms is not None:
            self.comms.release()  # no press outlives a snapshot (NP5)
        self._raid_sent = self._raid_now()
        msgs: list[wire.Message] = [wire.Session(
            app.world.human_online, app.human_guid, app.cfg.models.live,
            app.cfg.models.embedding), self.rooms.channels_message()]
        for guid, spec in sorted(self.specs.items()):
            msgs.append(wire.VoiceDef(guid, str(spec.get("name") or guid), spec))
        msgs.extend(self.rooms.moves())
        msgs.append(wire.Raid(*self._raid_sent))
        return msgs

    def _raid_now(self) -> tuple[str, int]:
        app = self.app
        return str(app.raid.phase(app.clock.now())), app.raid.lead() or 0

    def _tick_raid(self) -> None:
        """The service mirrors the raid's silence: it hears each change of phase or lead, and
        a pull cuts the banter off (Cancel below p3 drops p3 and p4)."""
        now = self._raid_now()
        if now == self._raid_sent:
            return
        pulled = now[0] == "combat" and self._raid_sent[0] != "combat"
        self._raid_sent = now
        self.send(wire.Raid(*now))
        if pulled:
            self.send(wire.Cancel(below="p3"))

    # -- the service -----------------------------------------------------------------------

    def ensure_service(self) -> None:
        """Once per mind start, with autostart on: when nothing accepts on the port within 1 s,
        start leeroy-voice detached (an operator's hold ends here, as it always has with a mind
        restart). Skipped, and logged, without the service's venv."""
        if not self.cfg.autostart or self._service_checked:
            return
        self._service_checked = True
        if self._port_open():
            return
        pythonw = self._pythonw()
        if pythonw is None:
            event("voice_autostart_skipped", level=logging.WARNING, reason="no_venv")
            return
        self._spawn(pythonw)
        event("voice_autostarted")

    def _port_open(self) -> bool:
        try:
            socket.create_connection((self.cfg.host, self.cfg.port), PROBE_S).close()
        except OSError:
            return False
        return True

    @staticmethod
    def _pythonw() -> Path | None:
        pythonw = REPO_ROOT / "voice" / ".venv" / "Scripts" / "pythonw.exe"
        return pythonw if pythonw.exists() else None

    def _spawn(self, pythonw: Path) -> None:
        """Start leeroy-voice detached. A start ends an operator's hold. A launch that fails
        still counts as one, for the supervisor's waits."""
        self._launched_at = self.app.clock.now()
        data = voice_data_dir()
        with suppress(OSError):  # one that stays is stale after the service's first greeting
            release_voice_hold(data)
        logs = data / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "voice-stdout.log", "ab") as out:
            subprocess.Popen(
                [str(pythonw), "-m", "leeroy_voice", "run"], cwd=str(REPO_ROOT / "voice"),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

    # -- the supervisor (autostart) ------------------------------------------------------------

    def _hold(self) -> dict | None:
        """An operator's stop that still holds the service down; None when none does. One older
        than its last greeting is stale only if it was already present at that greeting.
        A later stop wins even when the filesystem reuses an older timestamp."""
        data = voice_data_dir()
        hold = voice_hold(data)
        if (hold is not None and hold == self._greeted_hold
                and self._greeted_wall and hold["at"] < self._greeted_wall):
            with suppress(OSError):
                release_voice_hold(data)
            return None
        return hold

    def _relaunch_due(self, now: float) -> str | None:
        """Why the supervisor would start the service now: "login" (his login found it down)
        or "down" (unreachable RELAUNCH_AFTER_S, and its backoff is over); None to wait."""
        if not self.cfg.autostart:
            return None
        if self.client.greeted:  # a login that found it up: a later stop holds as any other
            self._login_due = False
            return None
        if self._down_since is None:
            self._down_since = now
        login, hold = self._login_due, self._hold()
        if login and hold is not None and (hold != self._login_hold or hold["at"] > self._login_wall):
            login = self._login_due = False  # stopped since his login: held until the next one
        if not login:
            if hold is not None:
                if not self._held_logged:
                    self._held_logged = True
                    event("voice_relaunch_held", by=hold["by"],
                          down_s=int(now - self._down_since))
                return None
            if now - self._down_since < RELAUNCH_AFTER_S:
                return None
        if self._launched_at is not None:
            wait = (START_GRACE_S if login or not self._launches else
                    RELAUNCH_BACKOFF_S[min(self._launches, len(RELAUNCH_BACKOFF_S)) - 1])
            if now - self._launched_at < wait:
                return None
        return "login" if login else "down"

    async def _supervise_once(self) -> None:
        now = self.app.clock.now()
        why = self._relaunch_due(now)
        if why is None:
            return
        pythonw = self._pythonw()
        if pythonw is None:
            if not self._skipped_logged:
                self._skipped_logged = True
                event("voice_relaunch_skipped", level=logging.WARNING, reason="no_venv")
            return
        if await asyncio.to_thread(self._port_open):
            return  # started by someone else: the client's next try finds it
        hold = self._hold()
        self._login_due = False
        self._launches += 1
        down_s = int(now - self._down_since)
        try:
            await asyncio.to_thread(self._spawn, pythonw)
        except OSError as exc:  # tried again after the next wait
            event("voice_relaunch_failed", level=logging.WARNING, why=why,
                  attempt=self._launches, reason=type(exc).__name__)
            return
        if hold is not None:
            event("voice_hold_lifted", by=hold["by"])
        event("voice_relaunched", why=why, attempt=self._launches, down_s=down_s)

    async def _supervise(self) -> None:
        while True:
            await self.app.clock.sleep(SUPERVISE_S)
            try:
                await self._supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed launch is tried again at the next look
                self._failed("supervise", exc)

    # -- the background task ---------------------------------------------------------------

    async def _policy_tick(self, name: str, policy, now: float) -> None:
        try:
            await (policy.invite_tick(now) if name == "invites" else policy.tick(now))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(name, exc)

    async def _tick_loop(self) -> None:
        while True:
            await self.app.clock.sleep(TICK_S)
            for tick in list(self.tickers):
                try:
                    tick()
                except Exception:
                    log.exception("voice_tick_failed")
            # Model calls must not stall raid updates or service acknowledgements.
            for name in ("elsewhere", "hotmic", "invites"):
                policy = self.movement if name == "invites" else getattr(self, name)
                task = self._policy_tasks.get(name)
                if policy is not None and (task is None or task.done()):
                    self._policy_tasks[name] = asyncio.create_task(
                        self._policy_tick(name, policy, self.app.clock.now()),
                        name=f"voice-{name}")

    async def _serve(self) -> None:
        try:
            await asyncio.to_thread(self.ensure_service)
        except Exception as exc:
            event("voice_autostart_failed", level=logging.WARNING, reason=type(exc).__name__)
        tasks = [asyncio.create_task(self.client.run()), asyncio.create_task(self._tick_loop())]
        if self.cfg.autostart:
            tasks.append(asyncio.create_task(self._supervise(), name="voice-supervisor"))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()  # stop the producer before waiting for its policy tasks
            for task in self._policy_tasks.values():
                task.cancel()
            await asyncio.gather(*tasks, *self._policy_tasks.values(), return_exceptions=True)
            self._policy_tasks.clear()
            await self._close_gpu()
            self._guard("shutdown", self.shutdown)
            await self.client.close()


def attach_voice(app) -> VoiceExtension:
    try:
        cfg = load_voice_config(app.data_dir / "mind.toml")
    except ConfigError as exc:
        event("voice_config_invalid", level=logging.WARNING, reason=str(exc))
        cfg = VOICE_OFF
    ext = VoiceExtension(app, cfg)
    if ext.cfg.enabled:
        app.engine.comms = ext.comms
        app.world.room_audience = ext.comms.audience
        app.register_event_handler("login", ext._on_login)
        app.register_event_handler("logout", ext._on_logout)
        app.register_event_handler("hello", ext._on_hello)
        app.register_event_handler("chat", ext._on_chat)
        app.register_event_handler("dc_status", ext._on_dc_status)
        app.register_event_handler("group", ext._on_group)
        app.register_event_handler("afk", ext._on_afk)
        app.register_background(ext._serve)
    return ext
