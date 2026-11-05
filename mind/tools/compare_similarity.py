"""Compare an actual baseline retrieval.py with this checkout on synthetic data."""

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import random
import statistics
import sys
import time


MIND = Path(__file__).resolve().parents[1]
CANDIDATE = MIND / "leeroy_mind" / "retrieval.py"
sys.path.insert(0, str(MIND))
from leeroy_mind.storage.db import VEC_DIM  # noqa: E402  (memories and library alike)


def load_retrieval(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load retrieval module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workload(size, dimensions, k, text_only):
    rng = random.Random(20261104 + size * 17 + dimensions)
    ids = list(range(size))
    vectors = {} if text_only else {
        i: tuple(rng.uniform(-1.0, 1.0) for _ in range(dimensions)) for i in ids
    }
    texts = {i: f"synthetic group {i % 17} item {i % 31}" for i in ids}
    scores = {i: rng.random() for i in ids}
    return ids, scores, vectors, texts, k


def operation(module, data, trace=None):
    ids, scores, vectors, texts, k = data
    sim = module.vector_or_word_similarity(vectors, texts)
    if trace is not None:
        original = sim

        def sim(a, b):
            value = original(a, b)
            trace.append((a, b, value))
            return value

    return module.mmr(ids, scores, sim, k)


def exact_trace(left, right):
    return len(left) == len(right) and all(
        a[:2] == b[:2] and math.isfinite(a[2]) and math.isfinite(b[2])
        and a[2].hex() == b[2].hex() for a, b in zip(left, right)
    )


def compare_case(baseline, candidate, data, iterations, result, checkpoint):
    pairs = result["pairs"]
    for iteration in range(iterations):
        order = ("baseline", "candidate") if iteration % 2 == 0 else ("candidate", "baseline")
        timings, results = {}, {}
        for name in order:
            module = baseline if name == "baseline" else candidate
            start = time.perf_counter_ns()
            results[name] = operation(module, data)
            timings[name] = time.perf_counter_ns() - start
        pairs.append({"iteration": iteration + 1, "order": list(order),
                      "baseline_ns": timings["baseline"], "candidate_ns": timings["candidate"],
                      "ordered_equal": results["baseline"] == results["candidate"]})
        checkpoint()

    baseline_trace, candidate_trace = [], []
    old = operation(baseline, data, baseline_trace)
    new = operation(candidate, data, candidate_trace)
    # Include a direct pair check even when k is zero or one and MMR makes no calls.
    ids, _, vectors, texts, _ = data
    old_sim = baseline.vector_or_word_similarity(vectors, texts)
    new_sim = candidate.vector_or_word_similarity(vectors, texts)
    direct_old = [(ids[0], ids[-1], old_sim(ids[0], ids[-1]))]
    direct_new = [(ids[0], ids[-1], new_sim(ids[0], ids[-1]))]
    result.update({"ordered_equal": old == new and all(p["ordered_equal"] for p in pairs),
                   "pair_numeric_equal": exact_trace(baseline_trace, candidate_trace)
                                         and exact_trace(direct_old, direct_new),
                   "checked_mmr_pair_calls": len(baseline_trace),
                   "baseline_median_ns": statistics.median(p["baseline_ns"] for p in pairs),
                   "candidate_median_ns": statistics.median(p["candidate_ns"] for p in pairs)})
    checkpoint()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="path to the actual baseline retrieval.py")
    parser.add_argument("output", type=Path, help="new JSON report; existing files are never overwritten")
    parser.add_argument("--iterations", type=int, default=4, choices=range(1, 21),
                        metavar="1..20", help="paired iterations per case (default: 4)")
    args = parser.parse_args()
    # Reserve the destination before running, and retain partial or failed receipts.
    with args.output.open("x", encoding="utf-8") as report_file:
        report = {"schema": 1, "status": "running", "cases": {},
                  "python": platform.python_version(), "iterations": args.iterations,
                  "timing": "factory snapshot plus MMR; first measured and subsequent pairs; no OS cache purge"}

        def checkpoint():
            report_file.seek(0)
            json.dump(report, report_file, indent=2, allow_nan=False)
            report_file.write("\n")
            report_file.truncate()
            report_file.flush()

        checkpoint()
        try:
            baseline_path = args.baseline.resolve(strict=True)
            if baseline_path == CANDIDATE.resolve():
                raise ValueError("baseline must be distinct from candidate")
            report["baseline_sha256"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
            report["candidate_sha256"] = hashlib.sha256(CANDIDATE.read_bytes()).hexdigest()
            baseline = load_retrieval(baseline_path, "comparison_baseline_retrieval")
            candidate = load_retrieval(CANDIDATE, "comparison_candidate_retrieval")
            dim = VEC_DIM
            cases = [(f"vector_{n}_{dim}_k5", n, dim, 5, False) for n in (50, 100, 270)]
            cases += [(f"vector_270_{dim}_k0", 270, dim, 0, False),
                      (f"vector_270_{dim}_k1", 270, dim, 1, False),
                      ("text_100_k5", 100, 0, 5, True)]
            for name, size, dimensions, k, text_only in cases:
                result = report["cases"][name] = {"pairs": []}
                checkpoint()
                compare_case(baseline, candidate, workload(size, dimensions, k, text_only),
                             args.iterations, result, checkpoint)
            report["status"] = ("ok" if all(c["ordered_equal"] and c["pair_numeric_equal"]
                                             for c in report["cases"].values()) else "mismatch")
        except Exception as exc:
            # Exception messages can contain local paths; never put them in the report.
            report["status"] = "error"
            report["error_type"] = type(exc).__name__
        finally:
            checkpoint()
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
