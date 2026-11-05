"""Private benchmark adapter around the production Turbo worker protocol."""
import argparse
import functools
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import re
import shutil
import subprocess
import sys
import time

ENGINE_DIR = Path(__file__).resolve().parents[1] / "leeroy_voice" / "engines"
sys.path.insert(0, str(ENGINE_DIR))
import worker  # noqa: E402
import worker_chatterbox_turbo as turbo  # noqa: E402

# The floor is intentionally not adjustable downward by the caller. Admission is not a
# reservation: other applications can allocate after the query.
MIN_FREE_BYTES = 8 * 1024**3
PHYSICAL_QUERY_TIMEOUT_S = 5


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def query_cuda_memory(torch):
    """Independent CUDA and physical readings; never expose command output or errors."""
    result = {}
    try:
        free, total = torch.cuda.mem_get_info()
        if type(free) is not int or type(total) is not int or not 0 <= free <= total or total <= 0:
            raise ValueError("invalid CUDA memory")
        result["cuda_memory"] = {"free_bytes": free, "total_bytes": total}
    except Exception as exc:
        result["cuda_memory"] = {"status": "failed", "error_type": type(exc).__name__}
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=PHYSICAL_QUERY_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = completed.stdout.splitlines()
        if not 1 <= len(rows) <= 64 or any(not re.fullmatch(r"[0-9]{1,12}", row.strip()) for row in rows):
            raise ValueError("invalid physical memory")
        free_values = [int(row.strip()) * 1024**2 for row in rows]
        result["physical_memory"] = {"free_bytes": min(free_values), "device_count": len(free_values)}
    except Exception as exc:
        result["physical_memory"] = {"status": "failed", "error_type": type(exc).__name__}
    return result


def probe_capabilities(device):
    result = {"python": platform.python_version(), "platform": platform.system(),
              "device": device, "packages": {}, "compiler": {}}
    for name in ("torch", "chatterbox-tts", "transformers", "triton"):
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = None
    try:
        import torch
        result["torch_cuda"] = torch.version.cuda
        result["device_status"] = "available" if device == "cpu" or torch.cuda.is_available() else "unavailable"
        if device == "cuda" and result["device_status"] == "available":
            result.update(query_cuda_memory(torch))
        try:
            result["compiler"]["backends"] = sorted(torch._dynamo.list_backends())
        except Exception as exc:
            result["compiler"]["backend_listing"] = {"status": "failed", "error_type": type(exc).__name__}
        try:
            import triton  # noqa: F401
            result["compiler"]["triton"] = "available"
        except Exception:
            result["compiler"]["triton"] = "unavailable"
        result["compiler"]["cuda_inductor"] = ("not_attempted" if result["compiler"]["triton"] == "available"
                                                else "unsupported/no_triton")
        result["compiler"]["cpu_compiler_executable"] = ("detected" if shutil.which("cl" if os.name == "nt" else "c++")
                                                           else "not_detected")
        result["compiler"]["execution"] = "not_attempted"
    except Exception as exc:
        result["device_status"] = "failed"
        result["error_type"] = type(exc).__name__
    return result


def admission(capabilities, device, required_bytes=MIN_FREE_BYTES):
    if capabilities.get("device_status") != "available":
        return "refused/device_unavailable"
    if device == "cuda":
        memory = capabilities.get("cuda_memory", {})
        free = memory.get("free_bytes")
        if type(free) is not int or free < 0:
            return "refused/memory_query_failed"
        physical = capabilities.get("physical_memory", {}).get("free_bytes")
        if type(physical) is not int or physical < 0:
            return "refused/physical_memory_query_failed"
        if min(free, physical) < max(MIN_FREE_BYTES, required_bytes):
            return "refused/insufficient_free_memory"
    return "available"


class Recorder:
    """A fixed set of phase aggregates, reset at each bounded worker call."""
    def __init__(self, sync):
        self.sync = sync
        self.phases = {}
        self.restores = []

    def wrap(self, obj, name, label):
        if obj is None or not callable(getattr(obj, name, None)):
            self.phases[label] = {"status": "unavailable"}
            return
        self.phases[label] = {"status": "not_called"}
        original = getattr(obj, name)
        had_own = name in vars(obj)
        old_own = vars(obj).get(name)

        @functools.wraps(original)
        def observed(*args, **kwargs):
            self.sync()
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                self.sync()
                entry = self.phases[label]
                if entry["status"] == "not_called":
                    entry = self.phases[label] = {"status": "observed", "calls": 0, "seconds": 0.0}
                entry["calls"] += 1
                entry["seconds"] += time.perf_counter() - started
        setattr(obj, name, observed)
        self.restores.append((obj, name, had_own, old_own))

    def reset(self):
        self.phases = {key: {"status": "unavailable" if value["status"] == "unavailable" else "not_called"}
                       for key, value in self.phases.items()}

    def close(self):
        for obj, name, had_own, old in reversed(self.restores):
            if had_own:
                setattr(obj, name, old)
            else:
                delattr(obj, name)
        self.restores.clear()


def install_phase_observers(tts, recorder):
    for obj, name, label in (
        (tts, "prepare_conditionals", "conditioning"), (tts, "generate", "generation"),
        (getattr(tts, "t3", None), "inference_turbo", "autoregressive"),
        (getattr(tts, "s3gen", None), "flow_inference", "flow"),
        (getattr(tts, "s3gen", None), "hift_inference", "vocoder"),
        (getattr(tts, "watermarker", None), "apply_watermark", "watermark"),
    ):
        recorder.wrap(obj, name, label)


def load_measured(model, device, threads, mode, run_dir, seed, max_calls=22):
    import numpy as np
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    sync = torch.cuda.synchronize if device == "cuda" else lambda: None
    recorder = Recorder(sync)
    # Save the descriptor, not a bound method, so restoration preserves class semantics.
    original_descriptor = vars(ChatterboxTurboTTS)["from_local"]
    original_factory = ChatterboxTurboTTS.from_local
    observed_model = {}

    def capture(cls, *args, **kwargs):
        tts = original_factory(*args, **kwargs)
        transformer = getattr(getattr(tts, "t3", None), "tfmr", None)
        try:
            dtype = str(next(transformer.parameters()).dtype)
            observed_model["dtype"] = dtype if dtype in {
                "torch.float16", "torch.float32", "torch.float64", "torch.bfloat16"
            } else "other"
        except (AttributeError, StopIteration, TypeError):
            observed_model["dtype"] = "unavailable"
        attention = getattr(getattr(transformer, "config", None), "_attn_implementation", None)
        observed_model["attention_implementation"] = (attention if attention in {
            "eager", "sdpa", "flash_attention_2", "flash_attention_3", "flex_attention"
        } else "unavailable")
        if mode == "phases":
            install_phase_observers(tts, recorder)
        return tts

    started = time.perf_counter()
    try:
        ChatterboxTurboTTS.from_local = classmethod(capture)
        speak, rate = turbo.load(model, device, threads)
    except BaseException:
        recorder.close()
        raise
    finally:
        ChatterboxTurboTTS.from_local = original_descriptor
    try:
        write_json(run_dir / "load.json", {"seconds": time.perf_counter() - started,
                                           "sample_rate": rate, **observed_model})
    except BaseException:
        recorder.close()
        raise
    sequence = 0

    def measured(text, ref, params):
        nonlocal sequence
        if sequence >= max_calls:
            raise ValueError("call limit reached")
        sequence += 1
        call_seed = seed + sequence - 1
        random.seed(call_seed)
        np.random.seed(call_seed)
        torch.manual_seed(call_seed)
        if device == "cuda":
            torch.cuda.manual_seed_all(call_seed)
        recorder.reset()
        sync()
        started = time.perf_counter()
        try:
            audio = speak(text, ref, params)
            sync()
            elapsed = time.perf_counter() - started
            valid = audio.ndim == 1 and audio.size > 0 and bool(np.isfinite(audio).all()) and rate > 0
            record = {"status": "ok" if valid else "invalid_audio", "seed": call_seed,
                      "seconds": elapsed, "frames": int(audio.size), "sample_rate": rate,
                      "finite_mono": valid, "phases": recorder.phases if mode == "phases" else None}
            if valid:
                record["audio_seconds"] = audio.size / rate
                record["real_time_factor"] = elapsed / record["audio_seconds"]
                record["waveform_sha256"] = hashlib.sha256(audio.tobytes(order="C")).hexdigest()
                record["waveform_dtype"] = str(audio.dtype)
            write_json(run_dir / f"call-{sequence:03d}.json", record)
            if not valid:
                raise ValueError("invalid audio")
            return audio
        except Exception as exc:
            path = run_dir / f"call-{sequence:03d}.json"
            if not path.exists():
                write_json(path, {"status": "failed", "error_type": type(exc).__name__, "seed": call_seed})
            raise
    return measured, rate, recorder


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("capabilities", "baseline", "phases"), required=True)
    ap.add_argument("--device", choices=("cpu", "cuda"), required=True)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--model", type=Path)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--max-calls", type=int, required=True, choices=range(23))
    args = ap.parse_args()
    recorder = None

    def load():
        nonlocal recorder
        capabilities = probe_capabilities(args.device)
        capabilities["admission"] = admission(capabilities, args.device)
        write_json(args.run_dir / "capabilities.json", capabilities)
        if args.mode == "capabilities":
            return lambda *_: None, 1
        if capabilities["admission"] != "available":
            raise RuntimeError("admission refused")
        # Query again immediately before the model allocation.
        if args.device == "cuda":
            import torch
            capabilities.update(query_cuda_memory(torch))
            status = admission(capabilities, "cuda")
            capabilities["admission"] = status
            write_json(args.run_dir / "capabilities.json", capabilities)
            if status != "available":
                raise RuntimeError("admission refused")
        speak, rate, recorder = load_measured(args.model, args.device, args.threads, args.mode, args.run_dir,
                                              args.seed, args.max_calls)
        return speak, rate

    try:
        return worker.serve(load)
    finally:
        if recorder is not None:
            recorder.close()


if __name__ == "__main__":
    sys.exit(main())
