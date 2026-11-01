"""Task 21: the GPU admission rule, the probes behind it, and GpuGuard on the whole service
(test_service.py's Rig), with Ollama, nvidia-smi and the process list invented."""
import asyncio
import dataclasses
import http.server
import json
import logging
import threading

import pytest

from conftest import eventually, run
from fakes import FakeProbes, FakeRecognizer
from leeroy_voice import __main__ as cli
from leeroy_voice import gpu
from leeroy_voice.config import load_config
from leeroy_voice.__main__ import main
from leeroy_voice.gpu import GpuGuard, OllamaPs, admit
from leeroy_voice.stt import Transcript
from leeroy_voice.wire import Heard, Status
from test_service import HEARD, Rig

GB = 1024 ** 3
OK_WORLD = {"orion": (16, 16), "embed": (1, 1)}
OK = {"orion": (16, 16), "emb": (1, 1)}


def test_admit():
    a = dict(ps=OK, free_gb=3.0, wow=True, live="orion", embed="emb", need_gb=1.5, margin_gb=1.0)
    assert admit(**a) is None
    assert admit(**{**a, "ps": {"orion": (16, 12), "emb": (1, 1)}}) == "live model split"
    assert admit(**{**a, "ps": {"orion": (16, 16)}}) == "embedding model not loaded"
    assert admit(**{**a, "free_gb": 2.0}) == "2.0 GB free, need 2.5"
    assert admit(**{**a, "wow": False}) == "WoW not running"
    assert admit(**{**a, "ps": None}) == "cannot ask Ollama"


def test_admit_the_other_refusals():
    a = dict(ps=OK, free_gb=3.0, wow=True, live="orion", embed="emb", need_gb=1.5, margin_gb=1.0)
    assert admit(**{**a, "ps": {"emb": (1, 1)}}) == "live model not loaded"
    assert admit(**{**a, "ps": {"orion": (16, 16), "emb": (1, 0)}}) == "embedding model split"
    assert admit(**{**a, "free_gb": None}) == "free VRAM unreadable"
    assert admit(**{**a, "free_gb": 2.5}) is None  # exactly need + margin
    # Ollama lists an untagged name with :latest
    assert admit(**{**a, "ps": {"orion": (16, 16), "emb:latest": (1, 1)}}) is None
    assert admit(**{**a, "live": "", "embed": ""}) == "live model not loaded"


class _Ps(http.server.BaseHTTPRequestHandler):
    body = b"{}"
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def ollama_server():
    handler = type("H", (_Ps,), {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_ollama_ps_reads_sizes(ollama_server):
    handler, url = ollama_server
    handler.body = json.dumps({"models": [
        {"name": "orion:q", "model": "orion:q", "size": 16 * GB, "size_vram": 16 * GB},
        {"name": "emb:latest", "size": GB, "size_vram": GB // 2}]}).encode()
    assert OllamaPs(url).loaded() == {"orion:q": (16 * GB, 16 * GB),
                                      "emb:latest": (GB, GB // 2)}


def test_ollama_ps_answers_none_when_it_cannot_be_asked(ollama_server):
    handler, url = ollama_server
    handler.body = b"not json"
    assert OllamaPs(url).loaded() is None
    handler.body, handler.status = b"{}", 500
    assert OllamaPs(url).loaded() is None
    handler.status, handler.body = 200, b'{"models": "x"}'
    assert OllamaPs(url).loaded() is None
    handler.body = b'{"models": [{"name": "a", "size": "big"}]}'
    assert OllamaPs(url).loaded() is None


def test_ollama_ps_with_nothing_loaded(ollama_server):
    handler, url = ollama_server
    handler.body = b'{"models": []}'
    assert OllamaPs(url).loaded() == {}


def test_free_vram_gb(monkeypatch):
    class Done:
        returncode, stdout = 0, "3072\n"
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **k: Done())
    assert gpu.free_vram_gb() == 3.0
    Done.stdout = "garbage"
    assert gpu.free_vram_gb() is None
    Done.stdout, Done.returncode = "3072\n", 1
    assert gpu.free_vram_gb() is None

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(gpu.subprocess, "run", missing)
    assert gpu.free_vram_gb() is None


def test_wow_running(monkeypatch):
    import psutil

    class P:
        def __init__(self, name):
            self.info = {"name": name}
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([P("a.exe"), P("WoW.exe")]))
    assert gpu.wow_running() is True
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([P("a.exe"), P(None)]))
    assert gpu.wow_running() is False


# -- the guard on the whole service -------------------------------------------------------------

class World:
    """Ollama, nvidia-smi and the process list, as the guard asks them."""

    def __init__(self):
        self.ps = {"orion": (16, 16), "embed": (1, 1)}
        self.free = 3.0
        self.wow = True
        self.asked = 0
        self.fail = False

    def loaded(self):
        self.asked += 1
        if self.fail:
            raise RuntimeError("boom")
        return None if self.ps is None else dict(self.ps)


class Whisper:
    def __init__(self):
        self.built = 0
        self.closed = 0
        self.rec = FakeRecognizer(default=Transcript("hello from the gpu", 0.95, 1100),
                                  name="whisper")
        self.rec.close = self._close
        self.error: Exception | None = None

    def _close(self):
        self.closed += 1

    def __call__(self):
        self.built += 1
        if self.error is not None:
            raise self.error
        return self.rec


def gpu_rig(tmp_path, bank, world, whisper, mode="auto"):
    cfg = dataclasses.replace(load_config(tmp_path / "no-such.toml"), input_device="Mic A",
                              recognizer=mode)
    rig = Rig(tmp_path, bank, cfg=cfg)
    guard = GpuGuard(rig.service, ps=world, free=lambda: world.free, wow=lambda: world.wow,
                     need_gb=1.5, margin_gb=1.0)
    rig.service.attach_gpu(guard, whisper)
    rig.guard = guard
    return rig


async def until(rig, predicate, steps=400):
    for _ in range(steps):
        if predicate():
            return
        await rig.clock.advance(0.5)
        await asyncio.sleep(0.01)
    raise AssertionError("never true")


def gpu_of(rig) -> str:
    return rig.service._status().gpu


def go(tmp_path, bank, world, whisper, body, **kw):
    async def main():
        async with gpu_rig(tmp_path, bank, world, whisper, **kw) as rig:
            await body(rig)
    run(main())


def test_loads_when_admitted(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        assert rig.service._stt_name() == "whisper" and whisper.built == 1
        await rig.tick(1.0)
        status = await rig.next_of(Status, lambda s: s.gpu == "whisper")
        assert status.stt == "whisper"
    go(tmp_path, fake_bank, w, whisper, body)


def test_a_press_uses_whisper_while_held(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        heard = await rig.next_of(Heard)
        assert heard.text == "hello from the gpu"
        assert whisper.rec.calls and not rig.recognizer.calls
    go(tmp_path, fake_bank, w, whisper, body)


def test_nothing_loads_in_parakeet_mode(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await rig.clock.advance(60)
        await asyncio.sleep(0.1)
        assert whisper.built == 0 and gpu_of(rig) == "none"
        assert w.asked > 0  # the budget is still watched and logged
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_auto_falls_back(tmp_path, fake_bank):
    """Refused (2 GB free, not 2.5): Parakeet, the fallback, hears the press."""
    w, whisper = World(), Whisper()
    w.free = 2.0

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: w.asked >= 1)
        assert whisper.built == 0 and gpu_of(rig) == "none"
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        heard = await rig.next_of(Heard)
        assert heard.text == HEARD.text and rig.recognizer.calls
    go(tmp_path, fake_bank, w, whisper, body)


def test_unload_on_split(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        w.ps = {"orion": (16, 12), "embed": (1, 1)}
        await until(rig, lambda: gpu_of(rig) == "none")
        assert rig.service._stt_name() == "parakeet"
        await eventually(lambda: whisper.closed == 1)
        await rig.clock.advance(60)  # not back while the model is still split
        await asyncio.sleep(0.1)
        assert whisper.built == 1
    go(tmp_path, fake_bank, w, whisper, body)


@pytest.mark.parametrize("ps", [{"orion": (16, 16)}, {"embed": (1, 1)}, None])
def test_unload_on_a_missing_model_or_a_silent_ollama(tmp_path, fake_bank, ps):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        w.ps = ps
        await until(rig, lambda: gpu_of(rig) == "none")
    go(tmp_path, fake_bank, w, whisper, body)


def test_unload_on_logout(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        await rig.snapshot(online=False)
        await until(rig, lambda: gpu_of(rig) == "none")
        await eventually(lambda: whisper.closed == 1)
        assert rig.service._stt_name() == "parakeet"
        await rig.tick(1.0)
        status = await rig.next_of(Status, lambda s: s.gpu == "none" and s.stt == "parakeet")
        assert status.gpu == "none"
    go(tmp_path, fake_bank, w, whisper, body)


def test_a_logout_during_the_load_keeps_nothing(tmp_path, fake_bank):
    w = World()
    release = threading.Event()
    inner = Whisper()

    def slow():
        assert release.wait(5)
        return inner()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: rig.guard.loading)
        await rig.snapshot(online=False)
        await eventually(lambda: not rig.service.online)
        release.set()
        await eventually(lambda: inner.built == 1)
        await until(rig, lambda: inner.closed == 1)
        assert gpu_of(rig) == "none"
    go(tmp_path, fake_bank, w, slow, body)


def test_a_failed_load_stays_on_parakeet_and_backs_off(tmp_path, fake_bank, caplog):
    w, whisper = World(), Whisper()
    whisper.error = RuntimeError("cuda out of memory: secret path")

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: whisper.built == 1)
        await rig.clock.advance(30)
        await asyncio.sleep(0.1)
        assert whisper.built == 1  # not again at the next check
        assert gpu_of(rig) == "none" and rig.service._stt_name() == "parakeet"
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, w, whisper, body)
    assert "gpu_load_failed: error=RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_a_probe_that_raises_never_kills_the_loop(tmp_path, fake_bank):
    w, whisper = World(), Whisper()
    w.fail = True

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: w.asked >= 2)
        w.fail = False
        await until(rig, lambda: gpu_of(rig) == "whisper")
        assert not rig.stopping()
    go(tmp_path, fake_bank, w, whisper, body)


def test_gpu_budget_is_logged_every_ten_minutes_while_online(tmp_path, fake_bank, caplog):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.clock.advance(1300, step=5)  # offline: nothing
        assert "gpu_budget:" not in caplog.text
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        await rig.clock.advance(1250, step=5)
        await asyncio.sleep(0.1)
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, w, whisper, body)
    lines = [ln for ln in caplog.text.splitlines() if "gpu_budget:" in ln]
    assert 2 <= len(lines) <= 4
    assert "free_gb=3.0" in lines[0] and "wow=true" in lines[0]
    assert "live=gpu" in lines[0] and "embed=gpu" in lines[0]


def test_the_guard_serves_another_holder(tmp_path, fake_bank):
    """Task 22's use: admits(need) asks the same rule, hold() registers an unload that a split
    model or a logout calls at once."""
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        g = rig.guard
        assert await g.admits(1.0) is None
        w.free = 1.2
        assert await g.admits(1.0) == "1.2 GB free, need 2.0"
        w.free = 3.0
        unloaded = []
        g.hold("chatterbox", lambda: unloaded.append(1))
        w.ps = {"orion": (16, 12), "embed": (1, 1)}
        await until(rig, lambda: unloaded)
        assert unloaded == [1] and "chatterbox" not in g.holders
        g.hold("chatterbox", lambda: unloaded.append(2))
        g.drop("chatterbox")
        assert g.holders == {}
        g.hold("chatterbox", lambda: unloaded.append(3))
        await rig.snapshot(online=False)
        await until(rig, lambda: unloaded == [1, 3])
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_the_hotwords_are_his_room_then_the_last_ten_speakers(tmp_path, fake_bank):
    rig = Rig(tmp_path, fake_bank)
    s = rig.service
    for i in range(12):
        s._note_speaker(f"Sp{i}")
    s._note_speaker("Sp5")  # again: most recent, not twice
    got = s._hotwords(["Arkon", "Sp11"])
    assert got[:2] == ["Arkon", "Sp11"]
    assert got[2:] == ["Sp5", "Sp10", "Sp9", "Sp8", "Sp7", "Sp6", "Sp4", "Sp3", "Sp2"]


# -- Task 22's contract: async unloads, atomic reservations, the engine beats Whisper -----------

def test_hold_accepts_a_coroutine_unload(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        done = []

        async def unload():
            await asyncio.sleep(0.01)
            done.append(1)
        rig.guard.hold("worker", unload)
        w.ps = {"orion": (16, 12), "embed": (1, 1)}
        await until(rig, lambda: done)
        assert "worker" not in rig.guard.holders
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_a_hung_async_unload_is_cut_off_and_the_others_still_run(tmp_path, fake_bank,
                                                                 monkeypatch, caplog):
    monkeypatch.setattr(gpu, "UNLOAD_TIMEOUT_S", 0.05)
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        done = []

        async def hangs():
            await asyncio.sleep(30)
        rig.guard.hold("a", hangs)
        rig.guard.hold("b", lambda: done.append("b"))
        rig.guard.release_all("test")
        await rig.guard.settle()
        assert done == ["b"] and rig.guard.holders == {}
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")
    assert "gpu_unload_failed: holder=a error=TimeoutError" in caplog.text


def test_two_loads_cannot_both_be_admitted_on_the_same_gigabytes(tmp_path, fake_bank):
    """Free 5.0, margin 1.0: one load of 3.0 fits, a second of 3.0 at the same moment does not,
    because the first has reserved its need though the card has not moved yet."""
    w, whisper = World(), Whisper()
    w.free = 5.0

    async def body(rig):
        await rig.snapshot()
        g = rig.guard
        first, second = await asyncio.gather(
            g.acquire("a", 3.0, lambda: None), g.acquire("b", 3.0, lambda: None))
        assert first is None and second is not None and "b" not in g.holders
        g.confirm("a")  # loaded: the card now shows it
        w.free = 2.0
        assert await g.acquire("b", 1.5, lambda: None) is not None  # 2.0 free < 1.5 + 1.0
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_a_reservation_that_is_never_confirmed_expires(tmp_path, fake_bank):
    w, whisper = World(), Whisper()
    w.free = 5.0

    async def body(rig):
        await rig.snapshot()
        g = rig.guard
        assert await g.acquire("a", 3.0, lambda: None) is None
        assert await g.acquire("b", 3.0, lambda: None) is not None
        await rig.clock.advance(gpu.RESERVE_S + 1)
        assert await g.acquire("b", 3.0, lambda: None) is None
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_drop_gives_the_reservation_back(tmp_path, fake_bank):
    w, whisper = World(), Whisper()
    w.free = 5.0

    async def body(rig):
        await rig.snapshot()
        g = rig.guard
        assert await g.acquire("a", 3.0, lambda: None) is None
        g.drop("a")
        assert await g.acquire("b", 3.0, lambda: None) is None
    go(tmp_path, fake_bank, w, whisper, body, mode="parakeet")


def test_the_engine_beats_whisper(tmp_path, fake_bank):
    """Whisper held, 1.4 GB free; an engine of 1.8 GB (2.8 with the margin) fits only once Whisper's 1.5 is back:
    Whisper is released (Parakeet takes over) and the engine is admitted. A request that would
    not fit even then leaves Whisper alone, and a peer of Whisper's rank never preempts it."""
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        g = rig.guard
        w.free = 1.4
        assert await g.acquire("huge", 20.0, lambda: None, priority=1) is not None
        assert gpu_of(rig) == "whisper"
        assert await g.acquire("peer", 3.0, lambda: None, priority=0) is not None
        assert gpu_of(rig) == "whisper"
        unloaded = []
        assert await g.acquire("engine", 1.8, lambda: unloaded.append(1), priority=1) is None
        assert gpu_of(rig) == "none" and rig.service._stt_name() == "parakeet"
        await eventually(lambda: whisper.closed == 1)
        assert "engine" in g.holders and "whisper" not in g.holders
    go(tmp_path, fake_bank, w, whisper, body)


def test_a_press_in_flight_survives_the_preemption(tmp_path, fake_bank):
    w, whisper = World(), Whisper()
    gate, started = threading.Event(), threading.Event()
    real = whisper.rec.transcribe

    def slow(audio, hotwords):
        started.set()
        assert gate.wait(5)
        return real(audio, hotwords)
    whisper.rec.transcribe = slow

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        rig.keys.key(True)
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        await eventually(started.is_set)
        w.free = 1.4
        assert await rig.guard.acquire("engine", 1.8, lambda: None, priority=1) is None
        assert rig.service._stt_name() == "parakeet" and whisper.closed == 0  # not before it ends
        gate.set()
        heard = await rig.next_of(Heard)
        assert heard.text == "hello from the gpu"
        await eventually(lambda: whisper.closed == 1)
        rig.keys.key(True)  # and the next press goes to Parakeet
        await rig.talk(0.5)
        rig.keys.key(False)
        await rig.talk(0.3)
        assert (await rig.next_of(Heard)).text == HEARD.text
    go(tmp_path, fake_bank, w, whisper, body)


def test_whisper_returns_only_if_it_still_fits(tmp_path, fake_bank):
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        g = rig.guard
        w.free = 1.4
        assert await g.acquire("engine", 1.8, lambda: None, priority=1) is None
        g.confirm("engine")
        assert gpu_of(rig) == "none"
        w.free = 2.0  # 2.0 < 1.5 + 1.0: it does not fit beside the engine
        await rig.clock.advance(gpu.RETRY_S + 30)
        await asyncio.sleep(0.1)
        assert whisper.built == 1 and gpu_of(rig) == "none"
        w.free = 4.0
        await until(rig, lambda: gpu_of(rig) == "whisper")
        assert whisper.built == 2 and "engine" in g.holders
    go(tmp_path, fake_bank, w, whisper, body)


def test_a_split_does_not_reload_whisper_every_check(tmp_path, fake_bank):
    """Minor 4: after a release, Whisper waits RETRY_S even if the models look healthy again."""
    w, whisper = World(), Whisper()

    async def body(rig):
        await rig.snapshot()
        await until(rig, lambda: gpu_of(rig) == "whisper")
        w.ps = {"orion": (16, 12), "embed": (1, 1)}
        await until(rig, lambda: gpu_of(rig) == "none")
        w.ps = dict(OK_WORLD)
        await rig.clock.advance(20)
        await asyncio.sleep(0.1)
        assert whisper.built == 1
        await until(rig, lambda: gpu_of(rig) == "whisper")
    go(tmp_path, fake_bank, w, whisper, body)


# -- the doctor ---------------------------------------------------------------------------------

def test_doctor_reports_ollama(monkeypatch, live_guard, capsys):
    monkeypatch.setattr(cli, "PROBES", FakeProbes(ollama={"orion": (16, 16), "emb": (1, 1)}))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "ok   Ollama: orion 100% GPU, emb 100% GPU" in out


def test_doctor_notes_a_split_model_and_a_silent_ollama(monkeypatch, live_guard, capsys):
    monkeypatch.setattr(cli, "PROBES", FakeProbes(ollama={"orion": (16, 12)}))
    assert main(["doctor"]) == 0
    assert "note Ollama: orion 75% GPU" in capsys.readouterr().out.splitlines()
    monkeypatch.setattr(cli, "PROBES", FakeProbes(ollama=None))
    assert main(["doctor"]) == 0
    assert any(ln.startswith("note Ollama: not reachable")
               for ln in capsys.readouterr().out.splitlines())
    monkeypatch.setattr(cli, "PROBES", FakeProbes(ollama={}))
    assert main(["doctor"]) == 0
    assert any(ln.startswith("note Ollama: no model loaded")
               for ln in capsys.readouterr().out.splitlines())


@pytest.mark.gpu
@pytest.mark.ollama
def test_the_real_probes_answer():
    """Skips unless Ollama has a model loaded now: the real OllamaPs, nvidia-smi and process
    list answer."""
    loaded = OllamaPs().loaded()
    if not loaded:
        pytest.skip("Ollama has nothing loaded")
    free = gpu.free_vram_gb()
    assert free is not None and 0 <= free <= 24.0
    assert isinstance(gpu.wow_running(), bool)
