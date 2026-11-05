"""Model-free tests for the optional Turbo measurement tools."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def adapter():
    return module("turbo_measurement_worker")


@pytest.fixture
def controller():
    return module("measure_turbo")


def test_admission_is_conservative_and_requires_a_query(adapter):
    assert adapter.admission({"device_status": "unavailable"}, "cuda") == "refused/device_unavailable"
    assert adapter.admission({"device_status": "available"}, "cuda") == "refused/memory_query_failed"
    low = {"device_status": "available", "cuda_memory": {"free_bytes": adapter.MIN_FREE_BYTES - 1},
           "physical_memory": {"free_bytes": 2 * adapter.MIN_FREE_BYTES}}
    assert adapter.admission(low, "cuda", 1) == "refused/insufficient_free_memory"
    low["cuda_memory"]["free_bytes"] = adapter.MIN_FREE_BYTES
    assert adapter.admission(low, "cuda") == "available"
    assert adapter.admission({"device_status": "available"}, "cpu") == "available"


def test_physical_memory_disagreement_and_multiple_gpus_refuse(adapter, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(stdout="19500\n1887\n")
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(
        mem_get_info=lambda: (19 * 1024**3, 24 * 1024**3)))
    readings = adapter.query_cuda_memory(torch)
    assert readings["physical_memory"] == {"free_bytes": 1887 * 1024**2, "device_count": 2}
    assert calls[0][1]["timeout"] == adapter.PHYSICAL_QUERY_TIMEOUT_S
    assert adapter.admission({"device_status": "available", **readings}, "cuda") == "refused/insufficient_free_memory"


@pytest.mark.parametrize("response", ["", "nan\n", "-1\n", "123 extra\n", "1\n" * 65])
def test_malformed_physical_memory_refuses(adapter, monkeypatch, response):
    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(stdout=response))
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(
        mem_get_info=lambda: (20 * 1024**3, 24 * 1024**3)))
    readings = adapter.query_cuda_memory(torch)
    assert readings["physical_memory"] == {"status": "failed", "error_type": "ValueError"}
    assert adapter.admission({"device_status": "available", **readings}, "cuda") == "refused/physical_memory_query_failed"


@pytest.mark.parametrize("error", [OSError("private sentinel"), subprocess.TimeoutExpired("private sentinel", 5)])
def test_physical_query_failure_refuses_without_error_message(adapter, monkeypatch, error):
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr(adapter.subprocess, "run", fail)
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(
        mem_get_info=lambda: (20 * 1024**3, 24 * 1024**3)))
    readings = adapter.query_cuda_memory(torch)
    assert "private sentinel" not in json.dumps(readings)
    assert adapter.admission({"device_status": "available", **readings}, "cuda") == "refused/physical_memory_query_failed"


def test_cuda_recheck_refuses_before_load(adapter, monkeypatch, tmp_path):
    enough = 20 * 1024**3
    initial = {"device_status": "available", "cuda_memory": {"free_bytes": enough},
               "physical_memory": {"free_bytes": enough}}
    monkeypatch.setattr(adapter, "probe_capabilities", lambda device: initial.copy())
    monkeypatch.setattr(adapter, "query_cuda_memory", lambda torch: {
        "cuda_memory": {"free_bytes": enough}, "physical_memory": {"free_bytes": 1887 * 1024**2}})
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    monkeypatch.setattr(adapter, "load_measured", lambda *a, **kw: pytest.fail("model must not load"))
    def serve(load):
        with pytest.raises(RuntimeError): load()
        return 1
    monkeypatch.setattr(adapter.worker, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["worker", "--mode", "baseline", "--device", "cuda",
                                     "--run-dir", str(tmp_path), "--threads", "1", "--seed", "0",
                                     "--max-calls", "1"])
    assert adapter.main() == 1
    receipt = json.loads((tmp_path / "capabilities.json").read_text(encoding="utf-8"))
    assert receipt["admission"] == "refused/insufficient_free_memory"


def test_observer_forwards_identity_exceptions_and_restores(adapter):
    class Target:
        def operation(self, *args, **kwargs):
            assert args == (marker,) and kwargs == {"key": marker}
            return marker

        def failure(self):
            raise RuntimeError("private sentinel")

    marker = object()
    target = Target()
    syncs = []
    recorder = adapter.Recorder(lambda: syncs.append(1))
    recorder.wrap(target, "operation", "op")
    recorder.wrap(target, "failure", "failed")
    recorder.wrap(target, "missing", "missing")
    assert target.operation(marker, key=marker) is marker
    with pytest.raises(RuntimeError, match="private sentinel"):
        target.failure()
    assert len(syncs) == 4
    assert recorder.phases["op"]["calls"] == 1
    assert recorder.phases["failed"]["calls"] == 1
    assert recorder.phases["missing"] == {"status": "unavailable"}
    recorder.reset()
    assert recorder.phases["op"] == {"status": "not_called"}
    recorder.close()
    assert "operation" not in vars(target) and "failure" not in vars(target)


def test_production_load_keeps_cache_tag_and_sampling_behavior(adapter, monkeypatch, tmp_path):
    class Inference:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda n: None
    torch.inference_mode = Inference
    monkeypatch.setitem(sys.modules, "torch", torch)
    chatterbox = types.ModuleType("chatterbox")
    chatterbox.__path__ = []
    tts_module = types.ModuleType("chatterbox.tts_turbo")

    class Wave:
        def squeeze(self, axis): return self
        def cpu(self): return self
        def numpy(self): return np.array([0.25], dtype=np.float32)

    class TTS:
        sr = 24000
        def __init__(self):
            self.conds = None
            self.prepared = 0
            self.calls = []
        @classmethod
        def from_local(cls, model, device):
            cls.instance = cls()
            return cls.instance
        def prepare_conditionals(self, ref):
            self.prepared += 1
            self.conds = object()
        def generate(self, text, **kwargs):
            self.calls.append((text, kwargs))
            return Wave()

    tts_module.ChatterboxTurboTTS = TTS
    monkeypatch.setitem(sys.modules, "chatterbox", chatterbox)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", tts_module)
    (tmp_path / "added_tokens.json").write_text(json.dumps({"[laugh]": 1}), encoding="utf-8")
    ref = tmp_path / "synthetic.wav"
    ref.write_bytes(b"abcd")
    stat = ref.stat()
    speak, rate = adapter.turbo.load(tmp_path, "cpu", 2)
    speak("Hello [laugh] [unknown]", str(ref), {"temperature": 0.5, "ignored": 7})
    speak("Hello", str(ref), {})
    assert TTS.instance.prepared == 1 and rate == 24000
    assert TTS.instance.calls[0] == ("Hello [laugh]", {"temperature": 0.5})
    ref.write_bytes(b"wxyz")
    os.utime(ref, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    speak("Hello", str(ref), {})
    assert TTS.instance.prepared == 2


def test_factory_restored_on_load_failure(adapter, monkeypatch, tmp_path):
    torch = types.ModuleType("torch")
    monkeypatch.setitem(sys.modules, "torch", torch)
    chatterbox = types.ModuleType("chatterbox")
    chatterbox.__path__ = []
    tts_module = types.ModuleType("chatterbox.tts_turbo")
    class TTS:
        @classmethod
        def from_local(cls, *args): return cls()
    tts_module.ChatterboxTurboTTS = TTS
    monkeypatch.setitem(sys.modules, "chatterbox", chatterbox)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", tts_module)
    original = vars(TTS)["from_local"]
    def fail(*args): raise RuntimeError("private sentinel")
    monkeypatch.setattr(adapter.turbo, "load", fail)
    with pytest.raises(RuntimeError):
        adapter.load_measured(tmp_path, "cpu", 1, "phases", tmp_path, 3)
    assert vars(TTS)["from_local"] is original


def test_observers_restored_when_load_receipt_write_fails(adapter, monkeypatch, tmp_path):
    torch = types.ModuleType("torch")
    monkeypatch.setitem(sys.modules, "torch", torch)
    chatterbox = types.ModuleType("chatterbox")
    chatterbox.__path__ = []
    tts_module = types.ModuleType("chatterbox.tts_turbo")
    class TTS:
        @classmethod
        def from_local(cls, *args):
            cls.instance = cls()
            return cls.instance
        def generate(self): return None
    tts_module.ChatterboxTurboTTS = TTS
    monkeypatch.setitem(sys.modules, "chatterbox", chatterbox)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", tts_module)
    original = vars(TTS)["from_local"]
    def load(*args):
        TTS.from_local()
        return lambda *unused: None, 24000
    def fail_write(*args): raise OSError("private sentinel")
    monkeypatch.setattr(adapter.turbo, "load", load)
    monkeypatch.setattr(adapter, "write_json", fail_write)
    with pytest.raises(OSError):
        adapter.load_measured(tmp_path, "cpu", 1, "phases", tmp_path, 3)
    assert vars(TTS)["from_local"] is original
    assert "generate" not in vars(TTS.instance)


def test_job_validation_and_statistics(controller, tmp_path):
    args = types.SimpleNamespace(iterations=1, warmups=0, threads=1, seed=1, timeout=5,
                                 python=tmp_path / "python", output=tmp_path / "new",
                                 mode="capabilities", model=None, reference=None)
    args.python.touch()
    controller.validate_job(args)
    for field, invalid in (("iterations", 21), ("warmups", 3), ("timeout", float("nan")), ("threads", 0)):
        previous = getattr(args, field)
        setattr(args, field, invalid)
        with pytest.raises(ValueError): controller.validate_job(args)
        setattr(args, field, previous)
    args.output.mkdir()
    with pytest.raises(ValueError): controller.validate_job(args)
    assert controller.summarize_samples([1.0, 3.0])["median"] == 2.0
    assert controller.summarize_samples([float("inf")])["status"] == "unavailable"


def test_failure_receipt_has_no_exception_message_and_cleanup_is_checked(controller, monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs): pass
        def start(self): raise RuntimeError("private sentinel")
        def stop(self): raise RuntimeError("private sentinel")
    monkeypatch.setattr(controller, "WorkerProcess", FakeWorker)
    python = tmp_path / "python"
    python.touch()
    args = types.SimpleNamespace(iterations=1, warmups=0, threads=1, seed=1, timeout=5,
                                 python=python, output=tmp_path / "new", mode="capabilities",
                                 model=None, reference=None, device="cpu", fixture="short")
    assert controller.run(args) == 1
    raw = (args.output / "report.json").read_text(encoding="utf-8")
    report = json.loads(raw)
    assert "private sentinel" not in raw
    assert report["status"] == "failed" and report["cleanup"] == "failed"


@pytest.mark.parametrize("script, expected, timeout", [
    ("import time\ntime.sleep(30)\n", "timed_out", 0.5),
    ("import sys, time\nsys.stdout.write('malformed\\n')\nsys.stdout.flush()\ntime.sleep(30)\n", "failed", 3.0),
])
def test_controller_cleans_owned_child_on_bad_start(controller, monkeypatch, tmp_path, script, expected, timeout):
    child_script = tmp_path / "fake_worker.py"
    child_script.write_text(script, encoding="utf-8")
    monkeypatch.setattr(controller, "ADAPTER", child_script)
    workers = []
    real_worker = controller.WorkerProcess
    class TrackedWorker(real_worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)
    monkeypatch.setattr(controller, "WorkerProcess", TrackedWorker)
    args = types.SimpleNamespace(iterations=1, warmups=0, threads=1, seed=1, timeout=timeout,
                                 python=Path(sys.executable), output=tmp_path / "run", mode="capabilities",
                                 model=None, reference=None, device="cpu", fixture="short")
    assert controller.run(args) == 1
    report = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == expected and report["cleanup"] == "complete"
    assert workers[0].process.poll() is not None
    assert not workers[0]._tree._alive()
