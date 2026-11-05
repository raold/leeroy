"""Bounded, content-free measurement of the checkout's production Turbo worker."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

import numpy as np

VOICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VOICE))
from leeroy_voice.engines.expressive import _TURBO, WorkerProcess  # noqa: E402

ADAPTER = Path(__file__).with_name("turbo_measurement_worker.py")
FIXTURES = {"short": "Ready for the next step.",
            "pause": "First, take a breath. Then, continue."}
PARAMS = dict(_TURBO)


def validate_job(args):
    if not 1 <= args.iterations <= 20 or not 0 <= args.warmups <= 2:
        raise ValueError("invalid call bounds")
    if not 1 <= args.threads <= 32 or not 0 <= args.seed <= 2**32 - 23:
        raise ValueError("invalid threads or seed")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
        raise ValueError("invalid timeout")
    if not args.python.is_file():
        raise ValueError("interpreter unavailable")
    if args.output.exists() or not args.output.parent.is_dir():
        raise ValueError("output must be a new directory in an existing parent")
    if args.mode != "capabilities":
        if args.model is None or not args.model.is_dir() or args.reference is None or not args.reference.is_file():
            raise ValueError("model directory and synthetic reference are required")


def summarize_samples(samples):
    values = [x for x in samples if isinstance(x, (int, float)) and math.isfinite(x) and x > 0]
    if len(values) != len(samples) or not values:
        return {"status": "unavailable", "count": len(values)}
    return {"status": "available", "count": len(values), "min": min(values),
            "median": statistics.median(values), "max": max(values), "mean": statistics.mean(values)}


def write_report(path, report):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_receipt(path):
    try:
        with path.open(encoding="utf-8") as stream:
            result = json.load(stream)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def valid_call(receipt, rate, audio):
    if not isinstance(receipt, dict) or receipt.get("status") != "ok" or receipt.get("finite_mono") is not True:
        return False
    seconds = receipt.get("seconds")
    digest = receipt.get("waveform_sha256")
    return (isinstance(seconds, (float, int)) and math.isfinite(seconds) and seconds > 0
            and isinstance(digest, str) and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest)
            and rate > 0 and receipt.get("sample_rate") == rate and audio.ndim == 1
            and len(audio) > 0 and receipt.get("frames") == len(audio) and bool(np.isfinite(audio).all()))


def run(args):
    validate_job(args)
    args.output.mkdir()  # exclusive: preserve previous receipts
    report_path = args.output / "report.json"
    report = {"schema": 1, "mode": args.mode, "device": args.device, "fixture_id": args.fixture,
              "warmups": args.warmups, "iterations": args.iterations, "threads": args.threads,
              "timing": "instrumented_synchronized_phase_wall" if args.mode == "phases" else "call_boundary_wall",
              "status": "running", "cleanup": "pending", "calls": []}
    write_report(report_path, report)
    child_args = ["--mode", args.mode, "--device", args.device, "--run-dir", args.output.resolve(),
                  "--threads", args.threads, "--seed", args.seed,
                  "--max-calls", args.warmups + args.iterations if args.mode != "capabilities" else 0]
    if args.model is not None:
        child_args += ["--model", args.model.resolve()]
    cache = args.output.resolve() / "cache"
    env = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HOME": str(cache / "huggingface"), "XDG_CACHE_HOME": str(cache),
           "HF_HUB_CACHE": str(cache / "huggingface" / "hub"),
           "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
           "TRANSFORMERS_CACHE": str(cache / "transformers"),
           "TORCH_HOME": str(cache / "torch"), "TRITON_CACHE_DIR": str(cache / "triton"),
           "TORCHINDUCTOR_CACHE_DIR": str(cache / "inductor"),
           "TORCH_EXTENSIONS_DIR": str(cache / "extensions"), "NUMBA_CACHE_DIR": str(cache / "numba"),
           "MPLCONFIGDIR": str(cache / "matplotlib"),
           "CUDA_CACHE_PATH": str(cache / "cuda")}
    if args.device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    child = WorkerProcess(args.python.resolve(), ADAPTER, args=child_args, start_s=args.timeout, env=env)
    try:
        started = time.perf_counter()
        child.start()
        report["startup_round_trip_seconds"] = time.perf_counter() - started
        report["capabilities"] = read_receipt(args.output / "capabilities.json")
        report["load"] = read_receipt(args.output / "load.json")
        write_report(report_path, report)
        if args.mode != "capabilities":
            for index in range(args.warmups + args.iterations):
                started = time.perf_counter()
                audio, rate = child.ask(FIXTURES[args.fixture], str(args.reference.resolve()), PARAMS,
                                        timeout=args.timeout)
                round_trip = time.perf_counter() - started
                receipt = read_receipt(args.output / f"call-{index + 1:03d}.json")
                if not valid_call(receipt, rate, audio):
                    raise ValueError("invalid receipt")
                report["calls"].append({**receipt, "kind": "warmup" if index < args.warmups else "measured",
                                        "round_trip_seconds": round_trip})
                write_report(report_path, report)
        report["status"] = "ok"
    except Exception as exc:
        # Exception messages can contain user paths or speech; never include them, and never
        # parse them: a start or call that reached its deadline is the timeout.
        report["status"] = "timed_out" if time.perf_counter() - started >= args.timeout else "failed"
        report["error_type"] = type(exc).__name__
    finally:
        try:
            child.stop()
            report["cleanup"] = "complete"
        except Exception as exc:
            report["cleanup"] = "failed"
            report["cleanup_error_type"] = type(exc).__name__
            report["status"] = "failed"
        report["capabilities"] = read_receipt(args.output / "capabilities.json")
        report["load"] = read_receipt(args.output / "load.json")
        measured = [call for call in report["calls"] if call["kind"] == "measured"]
        report["generation_seconds"] = summarize_samples([call["seconds"] for call in measured])
        report["round_trip_seconds"] = summarize_samples([call["round_trip_seconds"] for call in measured])
        write_report(report_path, report)
    return 0 if report["status"] == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("capabilities", "baseline", "phases"), default="capabilities")
    parser.add_argument("--python", type=Path, required=True, help="isolated Turbo interpreter")
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--reference", type=Path, help="existing synthetic reference audio")
    parser.add_argument("--fixture", choices=tuple(FIXTURES), default="short")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, OSError) as exc:
        print(f"measurement could not start: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
