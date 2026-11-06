"""python -m leeroy_selfie where | fetch-models | doctor | worker | smoke. Only worker and smoke
load torch (inside render.load); the rest import nothing heavy."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import models
from .paths import data_dir

MARK = {True: "ok", False: "FAIL", None: "note"}


def _fail(what: str, e: Exception) -> int:
    print(f"{what}: {' '.join(str(e).split())}", file=sys.stderr)
    return 1


def cmd_where(args) -> int:
    print(f"data_dir={data_dir()}")
    return 0


def cmd_fetch_models(args) -> int:
    only = None if args.only is None else {n.strip() for n in args.only.split(",") if n.strip()}
    try:
        lines = models.fetch_models(data_dir(), only)
    except (models.ModelError, OSError) as e:
        return _fail("fetch-models", e)
    for ok, text in lines:
        print(f"{MARK[ok]} {text}")
    return 0


def cmd_doctor(args) -> int:
    root = Path(args.data) if args.data else data_dir()
    lines: list[tuple[bool | None, str]] = [(True, f"data dir {root}")]
    if sys.version_info[:2] != (3, 12):
        lines.append((False, f"python {sys.version_info.major}.{sys.version_info.minor}, "
                             "not the 3.12 this package needs"))
    for name in models.MODEL_PINS:
        lines += models.check(root, name, quick=args.quick)
    if not args.quick:
        lines += _gpu_lines()
    for ok, text in lines:
        print(f"{MARK[ok]} {text}")
    return 0 if all(ok is not False for ok, _ in lines) else 1


def _gpu_lines() -> list[tuple[bool | None, str]]:
    """The full doctor's only import of torch; a missing gpu extra is a note."""
    try:
        import torch
    except ImportError:
        return [(None, "torch is not installed (pip install -e \".[gpu]\")")]
    if not torch.cuda.is_available():
        return [(False, f"torch {torch.__version__} sees no CUDA device")]
    props = torch.cuda.get_device_properties(0)
    return [(True, f"torch {torch.__version__}, {props.name}, "
                   f"{props.total_memory / 2**30:.1f} GiB")]


SMOKE_PHRASES = [["photo of a woman in her thirties", 0], ["short auburn hair", 1],
                 ["green eyes", 1], ["denim jacket", 2], ["smiling", 2],
                 ["standing in a sunlit kitchen", 3], ["natural light", 4]]
SMOKE_NEGATIVE = [["child, teenager", 0], ["blurry", 1], ["deformed hands", 2]]


def cmd_worker(args) -> int:
    from . import render, worker
    out = worker.claim_stdout()
    code = worker.serve(
        lambda: render.load(Path(args.data) if args.data else data_dir(),
                            min_free_gib=args.min_free_gib),
        stdin=sys.stdin.buffer, out=out, on_eof=lambda: os._exit(0))
    # the reader thread still holds stdin's lock; a normal return would crash in finalization
    out.flush()
    sys.stderr.flush()
    os._exit(code)


def cmd_smoke(args) -> int:
    from . import render
    from .protocol import Render
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        renderer = render.load(data_dir(), min_free_gib=args.min_free_gib)
    except render.ReadyError as e:
        return _fail("smoke", e)
    load_s = time.perf_counter() - t0
    req = Render("smoke", tuple(map(tuple, SMOKE_PHRASES)), tuple(map(tuple, SMOKE_NEGATIVE)),
                 1234, 30, 5.0, 1024, str(out_dir.resolve() / "smoke.jpg"),
                 str(out_dir.resolve() / "smoke-thumb.jpg"), 256, 0.2)
    r = renderer.render(req)
    print(json.dumps({"load_s": round(load_s, 1), "seconds": r["seconds"],
                      "peak_gib": r["peak_gib"], "nsfw": r["nsfw"], "tokens": r["tokens"]}))
    return 1 if r["flagged"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_selfie")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_where = sub.add_parser("where", help="print the data dir")
    p_where.set_defaults(func=cmd_where)
    p_fetch = sub.add_parser("fetch-models", help="fetch the pinned models into "
                                                  "<data dir>/models and check them")
    p_fetch.add_argument("--only", metavar="NAMES",
                         help="comma-separated, of " + ", ".join(models.MODEL_PINS) +
                              " (default: all)")
    p_fetch.set_defaults(func=cmd_fetch_models)
    p_doc = sub.add_parser("doctor", help="check Python, the models and the GPU; one ok, note "
                                          "or FAIL line each")
    p_doc.add_argument("--quick", action="store_true",
                       help="never import torch or hash a file: files, records, revisions")
    p_doc.add_argument("--data", metavar="DIR", help="the data dir to check")
    p_doc.set_defaults(func=cmd_doctor)
    p_worker = sub.add_parser("worker", help="the night worker: JSON lines on stdin and stdout")
    p_worker.add_argument("--data", metavar="DIR", help="the data dir with the models")
    p_worker.add_argument("--min-free-gib", type=float, required=True,
                          help="refuse to load with less free VRAM")
    p_worker.set_defaults(func=cmd_worker)
    p_smoke = sub.add_parser("smoke", help="render one invented person into --out")
    p_smoke.add_argument("--out", metavar="DIR", required=True)
    p_smoke.add_argument("--min-free-gib", type=float, default=8.0)
    p_smoke.set_defaults(func=cmd_smoke)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
