"""Task 22: the expressive engine as a worker process, its fallback to Kokoro under GpuGuard, the
levers, the designed references and the member's selftest. The workers here are small real
processes that echo a tone; nothing needs a model, an audio device or the GPU. The invented cast
only."""
import asyncio
import dataclasses
import json
import logging
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from conftest import BANK_SHA, eventually, run
from fakes import FakeRecognizer
from leeroy_voice import __main__ as cli
from leeroy_voice import cast, logs
from leeroy_voice import fx
from leeroy_voice.__main__ import main
from leeroy_voice.config import load_config
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines import expressive as x
from leeroy_voice.engines import worker as wire
from leeroy_voice.engines.expressive import ExpressiveEngine, WorkerProcess, load_levers
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.gpu import GpuGuard
from leeroy_voice.stock import STOCK, StockCache
from leeroy_voice.stt import Transcript
from leeroy_voice.voices import StyleBank, spec_from_dict, spec_to_dict
from leeroy_voice.wire import Moved, Played, Status, VoiceDef
from test_cast import entry
from test_gpu import Whisper, World, until
from test_service import HEARD, ROOM, Engine, Rig, spec

TURBO = "chatterbox-turbo"
ENGINES_DIR = str(Path(x.__file__).parent)
RATE = 24000
FRAMES = 2400

FAKE_WORKER = textwrap.dedent("""\
    import json
    import os
    import sys
    import time

    sys.path.append(sys.argv[1])
    import numpy as np
    from worker import serve

    LOG = sys.argv[2]


    def load():
        print("a library talking on stdout while it loads")
        if os.path.exists(LOG + ".refuse"):
            raise RuntimeError("no such model: " + LOG)

        def speak(text, ref, params):
            print("a library talking on stdout: " + text)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"text": text, "ref": ref, "params": params}) + "\\n")
            if "crash" in text:
                os._exit(3)
            if "boom" in text:
                raise ValueError("boom on: " + text)
            if "hang" in text:
                time.sleep(60)
            if "nothing" in text:
                return np.zeros(0)
            n = params.get("frames", %d)
            return 0.2 * np.sin(2 * np.pi * 220.0 * np.arange(n) / %d)
        return speak, int(os.environ.get("FAKE_RATE", "%d"))


    sys.exit(serve(load))
    """ % (FRAMES, RATE, RATE))


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "worker_fake.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    return path


def calls_of(tmp_path) -> list[dict]:
    path = tmp_path / "said.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def process(tmp_path, script, **kw) -> WorkerProcess:
    return WorkerProcess(sys.executable, script, args=(ENGINES_DIR, str(tmp_path / "said.jsonl")),
                         **kw)


def refs(tmp_path, *guids):
    folder = tmp_path / "voices"
    folder.mkdir(exist_ok=True)
    for guid in guids:
        (folder / f"{guid}.ref.wav").write_bytes(b"RIFF")
    return folder


def engine(tmp_path, script, **kw) -> ExpressiveEngine:
    if "refs_dir" not in kw:
        kw["refs_dir"] = refs(tmp_path, 101)
    kw.setdefault("need_gb", 3.0)
    return ExpressiveEngine(TURBO, sys.executable, script=script,
                            args=(ENGINES_DIR, str(tmp_path / "said.jsonl")), **kw)


def turbo_spec(guid=101, **kw) -> dict:
    return {**spec(guid), "engine": TURBO, "ref": f"{guid}.ref.wav", **kw}


def spec_of(guid=101):
    return spec_from_dict(turbo_spec(guid))


# -- the brief's --------------------------------------------------------------------------------

def test_worker_round_trip(tmp_path, script):
    """One request -> audio of the requested length, the rate reported."""
    w = process(tmp_path, script)
    w.start()
    try:
        audio, sr = w.ask("hello there", "ref.wav", {"frames": 4800}, timeout=10)
    finally:
        w.stop()
    assert sr == RATE and audio.dtype == np.float32 and audio.shape == (4800,)
    assert np.abs(audio).max() == pytest.approx(0.2, abs=0.01)
    assert calls_of(tmp_path) == [{"text": "hello there", "ref": "ref.wav",
                                   "params": {"frames": 4800}}]


def test_the_wire_is_a_json_line_then_little_endian_floats():
    head = json.dumps({"id": "7", "ok": True, "sr": RATE, "frames": 3, "error": ""}).encode()
    body = np.array([0.0, 0.5, -0.5], "<f4").tobytes()

    class Stream:
        chunks = [head + b"\n" + body[:5], body[5:]]

        def readline(self, limit):
            return self.chunks[0].partition(b"\n")[0] + b"\n"

        def read(self, n):
            return body[:n]

    answer = wire.read_answer(Stream())
    assert (answer.id, answer.ok, answer.sr, answer.error) == ("7", True, RATE, "")
    assert answer.audio.tolist() == [0.0, 0.5, -0.5]
    request = json.loads(wire.request_line("9", "hi", "r.wav", {"a": 1}))
    assert request == {"id": "9", "text": "hi", "ref": "r.wav", "params": {"a": 1}}


class Stand:
    """What the service needs of an expressive engine, without a process: `up` as the test
    says, what reached it in `said`, and `fail` raised."""

    name = TURBO
    sample_rate = RATE
    need_gb = 3.0

    def __init__(self, *, up=True):
        self.running = up
        self.loads = self.stops = 0
        self.said: list[tuple[str, dict]] = []
        self.fail: EngineError | None = None

    @property
    def up(self):
        return self.running

    def start(self):
        self.loads += 1
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def levers(self, attitude):
        return {"attitude": attitude}

    def synth(self, text, spec, style, speed):
        if self.fail is not None:
            raise self.fail
        self.said.append((text, style))
        return FakeEngine().synth(text, spec, None, speed)


def roomy() -> World:
    """Room for the engine beside Ollama's models."""
    world = World()
    world.free = 8.0
    return world


class Rig22(Rig):
    """test_service's Rig with the guard and an expressive engine attached."""

    def __init__(self, tmp_path, bank, expressive, world=None, whisper=None, *, mode="parakeet",
                 decision=None, **kw):
        cfg = dataclasses.replace(load_config(tmp_path / "no-such.toml"), input_device="Mic A",
                                  recognizer=mode)
        super().__init__(tmp_path, bank, cfg=cfg, decision=decision or Decision(), **kw)
        self.world = world or roomy()
        self.guard = GpuGuard(self.service, ps=self.world, free=lambda: self.world.free,
                              wow=lambda: self.world.wow, need_gb=1.5, margin_gb=1.0)
        self.service.attach_gpu(self.guard, whisper or Whisper())
        self.service.attach_expressive(expressive)
        self.expressive = expressive

    async def seat(self, *guids, room=ROOM, **fields):
        """His session, and these members in an expressive voice in his room."""
        await self.snapshot()
        for guid in guids:
            await self.mind.send(VoiceDef(guid, f"Member{guid}", turbo_spec(guid, **fields)))
            await self.mind.send(Moved(guid, room))
        await eventually(lambda: all(g in self.service._specs for g in guids))


def go(tmp_path, bank, expressive, body, **kw):
    async def main_():
        async with Rig22(tmp_path, bank, expressive, **kw) as rig:
            await body(rig)
    run(main_(), 20.0)


def events(caplog, name):
    return [r for r in caplog.records if getattr(r, logs._EVENT_ATTR, "") == name]


def test_refused_gpu_falls_back(tmp_path, fake_bank, script, caplog):
    """GpuGuard refuses: Kokoro renders the line, engine_fallback logged, no worker started and
    no line lost."""
    w = World()
    w.free = 3.0  # 3.0 needed + the 1.0 margin
    caplog.set_level(logging.INFO, "leeroy_voice")
    made = engine(tmp_path, script)

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: w.asked >= 2)
        assert not made.up
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["hello there everyone"]
        assert calls_of(tmp_path) == []
    go(tmp_path, fake_bank, made, body, world=w)
    fell = events(caplog, "engine_fallback")
    assert [getattr(r, logs._FIELDS_ATTR)["id"] for r in fell] == ["s1"]
    assert "GB free" in getattr(fell[0], logs._FIELDS_ATTR)["why"]


def test_worker_crash_falls_back(tmp_path, fake_bank, script, caplog):
    """The worker exits mid-line: Kokoro speaks that line, nothing is lost, and the worker is
    back for the next one."""
    caplog.set_level(logging.INFO, "leeroy_voice")
    made = engine(tmp_path, script)

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: made.up)
        await rig.mind.send(rig.speak("s1", text="this one will crash it"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["this one will crash it"]
        await until(rig, lambda: not made.up or len(calls_of(tmp_path)) > 1)
        await until(rig, lambda: made.up)
        await rig.play_out()
        await rig.mind.send(rig.speak("s2", text="and the next one is fine"))
        await rig.sees("s2", "started")
        assert [c["text"] for c in calls_of(tmp_path)] == ["this one will crash it",
                                                           "and the next one is fine"]
        assert rig.engine.live() == ["this one will crash it"]
    go(tmp_path, fake_bank, made, body)
    assert "this one" not in caplog.text


def test_one_engine_per_member(tmp_path, fake_bank):
    """A member with an expressive spec always uses the engine while it is up, every line of
    theirs; a Kokoro member never does."""
    stand = Stand()

    async def body(rig):
        await rig.seat(102)
        for n, guid in enumerate((102, 101, 102)):
            await rig.mind.send(rig.speak(f"s{n}", guid, f"line number {n} now"))
            await rig.sees(f"s{n}", "started")
            await rig.play_out()
            await rig.tick(0.5)  # let the gap between different speakers pass
        assert [t for t, _ in stand.said] == ["line number 0 now", "line number 2 now"]
        assert rig.engine.live() == ["line number 1 now"]
    go(tmp_path, fake_bank, stand, body)


def test_builtin_levers_are_neutral(tmp_path):
    """M28: the code holds numbers only; a local attitudes.toml adds wording, over the defaults,
    and is the only place wording comes from."""
    for name, attitudes in x.LEVERS.items():
        assert set(attitudes) == {"none", "fond", "drawn", "lust"}
        for params in attitudes.values():
            assert "emo_text" not in params
            assert all(type(v) in (int, float) or isinstance(v, list) for v in params.values())
    assert load_levers(tmp_path, "indextts2") == x.LEVERS["indextts2"]
    (tmp_path / "attitudes.toml").write_text(
        '[indextts2.fond]\nuse_emo_text = true\nemo_text = "warm words"\nemo_alpha = 0.7\n'
        '[chatterbox-turbo.lust]\ntemperature = 0.9\n', encoding="utf-8")
    levers = load_levers(tmp_path, "indextts2")
    assert levers["fond"]["emo_text"] == "warm words" and levers["fond"]["emo_alpha"] == 0.7
    assert levers["none"] == x.LEVERS["indextts2"]["none"]
    turbo = load_levers(tmp_path, TURBO)
    assert turbo["lust"]["temperature"] == 0.9
    assert turbo["lust"]["top_k"] == x.LEVERS[TURBO]["lust"]["top_k"]
    assert x.LEVERS[TURBO]["lust"]["temperature"] != 0.9


@pytest.mark.parametrize("text", ['[chatterbox-turbo.sulk]\ntemperature = 0.9\n',
                                  '[chatterbox-turbo]\nlust = 3\n', 'not toml ['])
def test_a_bad_attitudes_file_is_refused(tmp_path, text):
    (tmp_path / "attitudes.toml").write_text(text, encoding="utf-8")
    with pytest.raises(x.LeverError):
        load_levers(tmp_path, TURBO)


def write_specs(folder, *specs):
    for s in specs:
        cast.write_spec(folder, spec_from_dict(s))


TEN_S = RATE * 10


def design_worker(tmp_path, script) -> cast.DesignWorker:
    return cast.DesignWorker("qwen3-voicedesign", sys.executable, script=script,
                             args=(ENGINES_DIR, str(tmp_path / "said.jsonl")), timeout_s=10,
                             params={"frames": TEN_S})


@pytest.fixture
def designing(tmp_path, script, fake_bank):
    """The specs of two members in a voices folder, and what a run needs around them."""
    out = tmp_path / "voices"
    write_specs(out, spec(101), {**spec(102), "name": "Belra"})
    decision = Decision(engine=TURBO, voice_source="qwen3-voicedesign", vram_trade="slots")

    class Run:
        pass
    run_ = Run()
    run_.out = out
    run_.decision = decision
    run_.source = design_worker(tmp_path, script)
    run_.stock = StockCache(tmp_path / "stock", FakeEngine(), fake_bank, decision)
    yield run_
    run_.source.close()


BRIEF = [entry(101, "Arkon", "male", descriptors=("low", "calm")),
         entry(102, "Belra", "female", "en-gb", age=41)]


def test_designed_reference_written(tmp_path, designing):
    """I11: a fake design worker -> <guid>.ref.wav and the spec's ref and engine set; the
    description holds only brief fields, and the voice reads REF_TEXT."""
    report = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                             decision=designing.decision,
                                             stock=lambda: designing.stock)
    assert report["written"] == [101, 102] and report["locked"] == []
    arkon = cast.load_existing(designing.out)[101]
    assert (arkon.ref, arkon.engine) == ("101.ref.wav", TURBO)
    audio, rate = sf.read(designing.out / "101.ref.wav", dtype="float32")
    assert rate == RATE and len(audio) == TEN_S and audio.ndim == 1
    asked = calls_of(tmp_path)
    assert [c["text"] for c in asked] == [cast.REF_TEXT, cast.REF_TEXT]
    assert asked[0]["params"]["instruct"] == "A 30-year-old American man, low, calm."
    assert asked[1]["params"]["instruct"] == "A 41-year-old British woman."
    assert asked[0]["params"]["gender"] == "male" and asked[1]["params"]["gender"] == "female"
    assert asked[0]["params"]["seed"] != asked[1]["params"]["seed"]


def test_designed_references_keep_locks(tmp_path, designing):
    """N9: a locked spec untouched and listed; a changed spec's old stock folder gone and its
    stock re-rendered, so no recast follows."""
    write_specs(designing.out, {**spec(102), "locked": True})
    before = (designing.out / "102.json").read_bytes()
    old_arkon = cast.load_existing(designing.out)[101]
    stale = tmp_path / "stock" / "101" / designing.stock.voice_hash(old_arkon)
    stale.mkdir(parents=True)
    (stale / "pulling-00000000.wav").write_bytes(b"x")
    report = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                             decision=designing.decision,
                                             stock=lambda: designing.stock)
    assert report["locked"] == [102] and report["written"] == [101]
    assert (designing.out / "102.json").read_bytes() == before
    assert not (designing.out / "102.ref.wav").exists()
    assert not stale.exists()
    now = cast.load_existing(designing.out)[101]
    folder = tmp_path / "stock" / "101" / designing.stock.voice_hash(now)
    assert len(list(folder.glob("*.wav"))) == len(STOCK)
    assert not (tmp_path / "stock" / "102").exists()


# -- the worker, beyond the brief ---------------------------------------------------------------

def test_a_failed_line_names_its_type_and_never_the_text(tmp_path, script):
    w = process(tmp_path, script)
    w.start()
    try:
        with pytest.raises(EngineError) as e:
            w.ask("boom a private line", "", {}, timeout=10)
        assert "ValueError" in str(e.value) and "private" not in str(e.value)
        assert w.ask("still here", "", {"frames": 10}, timeout=10)[0].shape == (10,)  # it lives
    finally:
        w.stop()


def test_a_worker_that_cannot_load_is_an_error_with_its_type(tmp_path, script):
    (tmp_path / "said.jsonl.refuse").write_text("", encoding="utf-8")
    w = process(tmp_path, script)
    with pytest.raises(EngineError) as e:
        w.start()
    assert "RuntimeError" in str(e.value) and "no such model" not in str(e.value)
    assert not w.up


def test_a_stuck_worker_times_out_and_is_stopped(tmp_path, script):
    w = process(tmp_path, script)
    w.start()
    t0 = time.monotonic()
    with pytest.raises(EngineError):
        w.ask("hang here", "", {}, timeout=0.3)
    assert time.monotonic() - t0 < 5 and not w.up


def test_a_worker_gone_mid_line_is_an_error(tmp_path, script):
    w = process(tmp_path, script)
    w.start()
    with pytest.raises(EngineError):
        w.ask("crash now", "", {}, timeout=10)
    assert not w.up
    with pytest.raises(EngineError):
        w.ask("and again", "", {}, timeout=10)


def test_stop_is_idempotent_and_ends_the_process(tmp_path, script):
    w = process(tmp_path, script)
    w.start()
    proc = w.process
    w.stop()
    w.stop()
    assert proc.poll() is not None and not w.up


def test_a_worker_ends_when_its_stdin_closes(tmp_path, script):
    """A dead service leaves no orphan on the card: the worker's loop ends at EOF."""
    w = process(tmp_path, script)
    w.start()
    w.process.stdin.close()
    assert w.process.wait(10) == 0
    w.stop()


def test_expressive_engine_sends_the_reference_and_the_levers(tmp_path, script):
    made = engine(tmp_path, script)
    made.start()
    try:
        audio = made.synth("hello", spec_of(), {"frames": 480, "temperature": 0.7}, 1.0)
    finally:
        made.stop()
    assert audio.shape == (480,)
    call = calls_of(tmp_path)[0]
    assert call["ref"] == str((tmp_path / "voices" / "101.ref.wav").resolve())
    assert call["params"] == {"frames": 480, "temperature": 0.7}


def test_expressive_speed_changes_audio_without_changing_worker_params(tmp_path, script):
    made = engine(tmp_path, script)
    made.start()
    try:
        params = {"frames": RATE * 2, "temperature": 0.7}
        slow = made.synth("hello", spec_of(), params, 0.9)
        fast = made.synth("hello", spec_of(), params, 1.15)
        with pytest.raises(EngineError, match="invalid speed"):
            made.synth("hello", spec_of(), params, float("nan"))
    finally:
        made.stop()
    assert len(slow) == pytest.approx(RATE * 2 / 0.9, abs=2)
    assert len(fast) == pytest.approx(RATE * 2 / 1.15, abs=2)
    assert [c["params"] for c in calls_of(tmp_path)] == [params, params]


def test_tempo_failure_is_an_engine_error_without_private_details(tmp_path, script, monkeypatch):
    from leeroy_voice.engines import expressive

    def fail(*args):
        raise RuntimeError("private line or path")

    monkeypatch.setattr(expressive, "change_tempo", fail)
    made = engine(tmp_path, script)
    made.start()
    try:
        with pytest.raises(EngineError, match="tempo: RuntimeError") as error:
            made.synth("hello", spec_of(), None, 0.9)
        assert "private" not in str(error.value)
    finally:
        made.stop()


def test_a_stock_render_takes_the_neutral_levers(tmp_path, script, fake_bank):
    """StockCache hands the bank's style; a cloning engine reads it as attitude none."""
    made = engine(tmp_path, script, levers={a: {"frames": 240, "mood": a} for a in
                                            ("none", "fond", "drawn", "lust")})
    made.start()
    try:
        made.synth("Pulling.", spec_of(), np.zeros(3), 1.0)
    finally:
        made.stop()
    assert calls_of(tmp_path)[0]["params"]["mood"] == "none"


def test_a_missing_reference_is_an_engine_error(tmp_path, script):
    made = engine(tmp_path, script, refs_dir=refs(tmp_path))
    made.start()
    try:
        with pytest.raises(EngineError):
            made.synth("hello", spec_of(), None, 1.0)
    finally:
        made.stop()
    assert calls_of(tmp_path) == []


def test_down_means_an_engine_error_at_once(tmp_path, script):
    made = engine(tmp_path, script)
    with pytest.raises(EngineError):
        made.synth("hello", spec_of(), None, 1.0)


def test_no_audio_and_a_wrong_rate_are_engine_errors(tmp_path, script, monkeypatch):
    made = engine(tmp_path, script)
    made.start()
    try:
        with pytest.raises(EngineError):
            made.synth("nothing to say", spec_of(), None, 1.0)
    finally:
        made.stop()
    monkeypatch.setenv("FAKE_RATE", "22050")
    made = engine(tmp_path, script, env={"FAKE_RATE": "22050"})
    made.start()
    try:
        with pytest.raises(EngineError):
            made.synth("hello", spec_of(), None, 1.0)
    finally:
        made.stop()


# -- the service ---------------------------------------------------------------------------------

def test_the_engine_comes_up_with_his_session_and_reports_on_status(tmp_path, fake_bank, script):
    made = engine(tmp_path, script)

    async def body(rig):
        assert not made.up
        await rig.seat(101)
        await until(rig, lambda: made.up and TURBO in rig.guard.holders
                    and rig.guard.holders[TURBO].confirmed)
        assert rig.guard.holders[TURBO].confirmed
        await rig.tick(0.1)
        status = await rig.next_of(Status, lambda s: s.gpu == TURBO)
        assert status.stt == "parakeet"  # the mind pins Ollama's models on this
    go(tmp_path, fake_bank, made, body)
    assert not made.up


def test_no_expressive_member_loads_nothing(tmp_path, fake_bank):
    stand = Stand(up=False)

    async def body(rig):
        await rig.snapshot()
        await rig.clock.advance(30)
        await asyncio.sleep(0.05)
        assert stand.loads == 0 and rig.guard.holders == {}
    go(tmp_path, fake_bank, stand, body)


def test_logout_unloads_the_worker_and_login_brings_it_back(tmp_path, fake_bank, script):
    made = engine(tmp_path, script)

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: made.up)
        await rig.snapshot(online=False)
        await until(rig, lambda: not made.up)
        assert TURBO not in rig.guard.holders
        await rig.tick(0.1)
        await rig.next_of(Status, lambda s: s.gpu == "none")
        await rig.seat(101)  # re-send the member's expressive spec for the new session
        await until(rig, lambda: made.up)
    go(tmp_path, fake_bank, made, body)


def test_a_split_model_stops_the_engine_at_once(tmp_path, fake_bank):
    """Live chat always wins: a split Orion unloads the engine, the line goes to Kokoro."""
    stand, w = Stand(up=False), World()
    w.free = 8.0

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: stand.up)
        w.ps = {"orion": (16, 12), "embed": (1, 1)}
        await until(rig, lambda: not stand.up)
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["hello there everyone"] and stand.said == []
        await rig.clock.advance(30)
        await asyncio.sleep(0.05)
        assert stand.loads == 1  # not back while the model is split
    go(tmp_path, fake_bank, stand, body, world=w)


def test_the_engine_preempts_a_held_whisper(tmp_path, fake_bank):
    """The priority of Task 21: Whisper holds the card, the expressive engine takes it, and
    Parakeet hears him at once."""
    stand, w, whisper = Stand(up=False), World(), Whisper()
    w.free = 2.6  # Whisper fits; the engine (3.0 + 1.0) only with Whisper's 1.5 given back

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: rig.service._stt_name() == "whisper")
        await rig.seat(101)
        await until(rig, lambda: stand.up)
        assert rig.service._stt_name() == "parakeet"
        assert "whisper" not in rig.guard.holders and rig.guard.holders[TURBO].confirmed
    go(tmp_path, fake_bank, stand, body, world=w, whisper=whisper, mode="auto")


def test_a_load_that_outlives_the_session_keeps_nothing(tmp_path, fake_bank):
    stand = Stand(up=False)
    release = threading.Event()
    real_start = stand.start

    def slow():
        assert release.wait(5)
        real_start()
    stand.start = slow

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: TURBO in rig.guard.holders)
        await rig.snapshot(online=False)
        await eventually(lambda: not rig.service.online)
        release.set()
        await until(rig, lambda: stand.stops >= 2 and not stand.up)
        assert TURBO not in rig.guard.holders
    go(tmp_path, fake_bank, stand, body)


def test_a_failed_start_backs_off_on_kokoro(tmp_path, fake_bank, caplog):
    caplog.set_level(logging.INFO, "leeroy_voice")
    stand = Stand(up=False)
    tries = []

    def refuse():
        tries.append(1)
        raise EngineError(f"{TURBO}: ImportError")
    stand.start = refuse

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: tries)
        await rig.clock.advance(20)
        await asyncio.sleep(0.05)
        assert len(tries) == 1 and TURBO not in rig.guard.holders
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["hello there everyone"]
    go(tmp_path, fake_bank, stand, body)
    assert events(caplog, "engine_start_failed")


def test_the_mind_stopping_leaves_no_worker(tmp_path, fake_bank, script):
    made = engine(tmp_path, script)
    procs = []

    async def body(rig):
        await rig.seat(101)
        await until(rig, lambda: made.up)
        procs.append(made.worker.process)
    go(tmp_path, fake_bank, made, body)
    assert procs[0].poll() is not None


def test_an_expressive_line_gets_its_attitude_levers_and_sounds(tmp_path, fake_bank):
    stand = Stand()

    async def body(rig):
        await rig.seat(101)
        await rig.mind.send(rig.speak("s1", text="haha see you later", attitude="fond"))
        await rig.sees("s1", "started")
        assert stand.said == [("[laugh] see you later", {"attitude": "fond"})]
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, stand, body)


def test_kokoro_never_gets_tags_on_a_fall_back(tmp_path, fake_bank):
    """The engine fails on the first sentence: Kokoro speaks both, with no tag, and his chat
    text reaches the transcript as the mind sent it."""
    stand = Stand()
    stand.fail = EngineError(f"{TURBO}: worker gone")

    async def body(rig):
        await rig.seat(101)
        page = await rig.page()
        await rig.mind.send(rig.speak("s1", text="lol what a pull. ugh no way"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["what a pull.", "no way"]
        said = [m["text"] for m in page.received if m.get("type") == "transcript"]
        assert said == ["lol what a pull. ugh no way"]
    go(tmp_path, fake_bank, stand, body)


def test_a_refused_line_is_plain_kokoro_with_its_own_reading(tmp_path, fake_bank):
    """Engine down at the speak: the line is what a Kokoro member would say, cue words too."""
    stand = Stand(up=False)
    stand.start = lambda: None

    async def body(rig):
        await rig.seat(101)
        await rig.mind.send(rig.speak("s1", text="omg no way"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["oh my god no way"]
    go(tmp_path, fake_bank, stand, body)


def test_the_kokoro_fall_back_keeps_its_levers_once_a_line(tmp_path, fake_bank):
    """Stress and pauses are for the line, not for each sentence."""
    stand = Stand()
    stand.fail = EngineError(f"{TURBO}: worker gone")

    async def body(rig):
        await rig.seat(101)
        await rig.mind.send(rig.speak("s1", text="well, you know. so, you came", attitude="fond",
                                      to_human=True))
        await rig.sees("s1", "started")
        assert rig.engine.live() == ["well, [you](+1) know.", "so, you came"]
    go(tmp_path, fake_bank, stand, body)


def test_a_designed_members_stock_clip_plays_with_the_engine_down(tmp_path, fake_bank):
    """A rendered clip needs no GPU: he hears her real voice on a callout."""
    stand = Stand(up=False)
    stand.start = lambda: None

    async def body(rig):
        await rig.seat(101)
        s = spec_from_dict(turbo_spec(101))
        cache = StockCache(rig.data / "cache" / "stock", Stand(), fake_bank, Decision())
        cache.render_missing(s)
        await rig.mind.send(rig.speak("s1", text="oom", kind="callout", priority="p1"))
        await rig.sees("s1", "started")
        assert rig.engine.live() == []
    go(tmp_path, fake_bank, stand, body)


def test_the_service_renders_no_stock_under_the_expressive_name(tmp_path, fake_bank):
    """Stock is rendered offline by the references step (Task 5's carry): the service neither
    stores Kokoro's fallback under the engine's name nor spends the worker on it."""
    stand = Stand()

    async def body(rig):
        await rig.seat(101)
        await rig.clock.advance(1.0)
        await asyncio.sleep(0.1)
        assert stand.said == [] and rig.engine.live() == []
        s = spec_from_dict(turbo_spec(101))
        cache = StockCache(rig.data / "cache" / "stock", stand, fake_bank, Decision())
        assert cache.get(s, "oom") is None
    go(tmp_path, fake_bank, stand, body)


def test_hello_names_the_engine(tmp_path, fake_bank):
    stand = Stand()

    async def main_():
        rig = Rig22(tmp_path, fake_bank, stand)
        assert rig.service._hello().engines == (rig.engine.name, TURBO)
    run(main_())


# -- references, beyond the brief ----------------------------------------------------------------

def test_a_rerun_resumes(tmp_path, designing):
    """Interrupted, a run goes on where it stopped: what is designed is not designed again."""
    done = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                           decision=designing.decision,
                                           stock=lambda: designing.stock)
    n = len(calls_of(tmp_path))
    again = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                            decision=designing.decision,
                                            stock=lambda: designing.stock)
    assert done["written"] == [101, 102] and again["written"] == []
    assert again["kept"] == [101, 102] and len(calls_of(tmp_path)) == n


def test_a_member_without_a_spec_is_listed(tmp_path, designing):
    report = cast.render_designed_references(
        [*BRIEF, entry(103, "Coros", "male")], designing.source, designing.out,
        decision=designing.decision, stock=lambda: designing.stock)
    assert report["uncast"] == [103] and not (designing.out / "103.ref.wav").exists()


def test_a_failed_member_stays_as_they_were(tmp_path, designing):
    real = designing.source.clip

    def spoiled(e, spec_):
        if e.guid == 101:
            raise EngineError("qwen3-voicedesign: ValueError")
        return real(e, spec_)
    designing.source.clip = spoiled
    report = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                             decision=designing.decision,
                                             stock=lambda: designing.stock)
    assert report["failed"] == [101] and report["written"] == [102]
    assert cast.load_existing(designing.out)[101].engine == "kokoro"
    assert not (designing.out / "101.ref.wav").exists()


def test_too_short_a_reference_is_refused(tmp_path, designing):
    """Chatterbox refuses a reference of 5 s or less, at every line, so it is caught here."""
    designing.source.params["frames"] = RATE * 4
    report = cast.render_designed_references(BRIEF[:1], designing.source, designing.out,
                                             decision=designing.decision,
                                             stock=lambda: designing.stock)
    assert report["failed"] == [101] and not (designing.out / "101.ref.wav").exists()


def test_core_members_are_designed_first(tmp_path, designing):
    brief = [entry(101, "Arkon", "male"), entry(102, "Belra", core=True)]
    cast.render_designed_references(brief, designing.source, designing.out,
                                    decision=designing.decision, stock=lambda: designing.stock)
    voices = [c["params"]["seed"] for c in calls_of(tmp_path)]
    assert voices == [cast.seed(102, 0), cast.seed(101, 0)]


def test_a_featured_decision_changes_only_the_featured(tmp_path, designing):
    decision = Decision(engine="kokoro", featured={102: TURBO}, vram_trade="slots")
    report = cast.render_designed_references(BRIEF, designing.source, designing.out,
                                             decision=decision, stock=lambda: designing.stock)
    assert report["written"] == [102]
    specs = cast.load_existing(designing.out)
    assert specs[101].engine == "kokoro" and specs[102].engine == TURBO


def test_kokoro_as_the_source_reads_the_reference_in_the_blend(tmp_path, fake_bank):
    """With voice_source kokoro the reference is the member's own blend reading REF_TEXT."""
    out = tmp_path / "voices"
    write_specs(out, spec(101))
    decision = Decision(engine=TURBO, vram_trade="slots")
    report = cast.render_designed_references(
        BRIEF[:1], cast.KokoroSource(FakeEngine(), fake_bank, decision), out, decision=decision,
        stock=lambda: StockCache(tmp_path / "stock", FakeEngine(), fake_bank, decision))
    assert report["written"] == [101]
    audio, rate = sf.read(out / "101.ref.wav", dtype="float32")
    assert rate == RATE and len(audio) == pytest.approx(len(cast.REF_TEXT) * 0.06 * RATE, abs=2)


def test_the_references_command(tmp_path, monkeypatch, capsys, fake_voices_bin, script):
    """The CLI end to end on fakes: the brief, the specs, the design worker and a stock engine."""
    root = tmp_path / "voice-data"
    root.mkdir()
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(root))
    (root / "decision.toml").write_text(
        f'engine = "{TURBO}"\nvoice_source = "qwen3-voicedesign"\nvram_trade = "slots"\n',
        encoding="utf-8")
    out = tmp_path / "voices"
    write_specs(out, spec(101), {**spec(102), "locked": True})
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps([
        {"guid": e.guid, "name": e.name, "gender": e.gender, "accent": e.accent, "age": e.age,
         "descriptors": list(e.descriptors), "speed": e.speed, "guild_role": e.guild_role,
         "raid_role": e.raid_role, "core": e.core} for e in BRIEF]), encoding="utf-8")
    source = design_worker(tmp_path, script)
    made = Stand(up=False)
    monkeypatch.setattr(cli, "load_engine",
                        lambda root_, cfg: (FakeEngine(), StyleBank(fake_voices_bin)))
    monkeypatch.setattr(cli, "design_source", lambda *a: source)
    monkeypatch.setattr(cli, "load_expressive", lambda *a, **k: made)
    assert main(["references", "--brief", str(brief), "--specs", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 written" in printed and "locked, unchanged: 102" in printed
    assert made.loads == 1 and made.stops >= 1
    report = json.loads((root / "cast" / "references.json").read_text(encoding="utf-8"))
    assert report["written"] == [101] and report["locked"] == [102]


# -- the selftest of one member ------------------------------------------------------------------

@pytest.fixture
def one_member(tmp_path, monkeypatch, fake_voices_bin):
    root = tmp_path / "voice-data"
    root.mkdir()
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(root))
    out = tmp_path / "voices"
    write_specs(out, turbo_spec(101), spec(102))
    monkeypatch.setattr(cli, "load_engine",
                        lambda root_, cfg: (Engine(), StyleBank(fake_voices_bin)))
    monkeypatch.setattr(cli, "_admission", lambda root_, cfg, need: None)
    return root, out


def test_selftest_guid_speaks_through_the_members_engine(one_member, monkeypatch, capsys):
    root, out = one_member
    made = Stand(up=False)
    monkeypatch.setattr(cli, "load_expressive", lambda *a, **k: made)
    assert main(["selftest", "--guid", "101", "--specs", str(out)]) == 0
    assert made.said[0][0] == STOCK["ready"] and made.stops >= 1
    audio, rate = sf.read(root / "debug" / "selftest.wav", dtype="float32")
    assert rate == fx.OUT_RATE and np.abs(audio).max() > 0.01
    assert TURBO in capsys.readouterr().out


def test_selftest_guid_on_a_kokoro_member(one_member, monkeypatch):
    root, out = one_member
    monkeypatch.setattr(cli, "load_expressive",
                        lambda *a, **k: pytest.fail("a Kokoro member starts no worker"))
    assert main(["selftest", "--guid", "102", "--specs", str(out)]) == 0
    assert (root / "debug" / "selftest.wav").exists()


def test_selftest_guid_refused_says_why_and_speaks_kokoro(one_member, monkeypatch, capsys):
    root, out = one_member
    monkeypatch.setattr(cli, "_admission", lambda root_, cfg, need: "2.8 GB free, need 4.0")
    monkeypatch.setattr(cli, "load_expressive",
                        lambda *a, **k: pytest.fail("refused: no worker"))
    assert main(["selftest", "--guid", "101", "--specs", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "2.8 GB free" in printed and "kokoro" in printed.lower()
    assert (root / "debug" / "selftest.wav").exists()


def test_selftest_guid_cpu_skips_the_admission(one_member, monkeypatch):
    root, out = one_member
    made = Stand(up=False)
    seen = {}

    def load(*a, **k):
        seen.update(k)
        return made
    monkeypatch.setattr(cli, "_admission", lambda root_, cfg, need: pytest.fail("no admission"))
    monkeypatch.setattr(cli, "load_expressive", load)
    assert main(["selftest", "--guid", "101", "--specs", str(out), "--cpu"]) == 0
    assert seen["device"] == "cpu" and made.said


def test_selftest_guid_without_a_spec_is_one_line(one_member, capsys):
    root, out = one_member
    assert main(["selftest", "--guid", "999", "--specs", str(out)]) == 1
    assert "999" in capsys.readouterr().err


def test_real_worker_scripts_read_only_the_standard_library_and_their_venv():
    """The worker scripts run in another venv: worker.py must not need this package."""
    source = Path(wire.__file__).read_text(encoding="utf-8")
    assert "from ." not in source and "import leeroy_voice" not in source
    assert "from leeroy_voice" not in source


def test_no_wording_reaches_the_code():
    """M28: emotion wording lives only in the local overlay."""
    source = Path(x.__file__).read_text(encoding="utf-8")
    for word in ("sultry", "affectionate", "attracted"):
        assert word not in source


def test_the_design_prompt_is_the_briefs_alone():
    e = entry(104, "Dunna", "female", "en-gb", age=27, descriptors=("warm", "a bit husky"))
    assert cast.describe(e) == "A 27-year-old British woman, warm, a bit husky."
    assert "Dunna" not in cast.describe(e)


def test_a_transcribed_press_still_goes_to_parakeet_with_the_engine_up(tmp_path, fake_bank):
    """His side of comms is untouched by the engine."""
    stand = Stand()

    async def body(rig):
        await rig.seat(101)
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        await until(rig, lambda: rig.recognizer.calls)
    go(tmp_path, fake_bank, stand, body, recognizer=FakeRecognizer(
        default=Transcript(HEARD.text, 0.9, 1100)))


def test_played_reports_a_fall_back_as_a_normal_line(tmp_path, fake_bank):
    stand = Stand()
    stand.fail = EngineError(f"{TURBO}: worker gone")

    async def body(rig):
        await rig.seat(101)
        await rig.mind.send(rig.speak("s1"))
        await rig.sees("s1", "started")
        await rig.play_out()
        await rig.sees("s1", "done")
        assert not [m for m in rig.got(Played) if m.state == "dropped"]
    go(tmp_path, fake_bank, stand, body)


def test_the_bank_hash_is_the_specs(fake_bank):
    assert fake_bank.sha256 == BANK_SHA
    assert spec_to_dict(spec_of())["engine"] == TURBO
