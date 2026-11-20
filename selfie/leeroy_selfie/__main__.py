"""python -m leeroy_selfie where | fetch-models | doctor | worker | smoke | detail. Only worker,
smoke and detail (without --boxes) load torch (inside render.load); the rest import nothing
heavy."""
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
    if only is None and args.backend != "realvisxl":
        base, identity = models.backend_pins(args.backend)
        only = set(base + identity + models.FACE_PINS + models.EMBEDDING_PINS)
    try:
        extra = {"authenticated": True} if args.auth else {}
        lines = models.fetch_models(data_dir(), only, **extra)
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
    base, identity = models.backend_pins(args.backend)
    for name in base:
        lines += models.check(root, name, quick=args.quick)
    optional = tuple(dict.fromkeys(identity + models.FACE_PINS + models.CRITIC_PINS
                                   + models.EMBEDDING_PINS))
    for name in optional:  # not fetched: a note
        lines += models.check(root, name, quick=args.quick)
    from .caption import FONT  # the memes' caption font and its licence, bundled with the package
    lines.append((FONT.is_file() and FONT.with_name("OFL.txt").is_file(), "caption font " + FONT.name))
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
SMOKE_SCALE = 0.6  # the mind's identity_scale default
DETAIL_FACE_PROMPT = "detailed face, natural eyes looking at the camera"
DETAIL_HAND_PROMPT = "a natural hand, five fingers"
DETAIL_NEGATIVE = ("child, teenager, blurry, deformed, crossed eyes, lazy eye, extra fingers, "
                   "fused fingers, missing fingers")


def _backend(args) -> dict:
    options = {"backend": args.backend} if args.backend != "realvisxl" else {}
    if getattr(args, "lora", None) is not None:
        options.update(lora=Path(args.lora),
                       lora_scale=1.0 if args.lora_scale is None else args.lora_scale)
    return options


def cmd_worker(args) -> int:
    from . import render, worker
    from . import embedding
    out = worker.claim_stdout()
    stdin = worker.claim_stdin()
    root = Path(args.data) if args.data else data_dir()
    load = (lambda: embedding.load(root, min_free_gib=args.min_free_gib)) if args.embed_only else (
        lambda: render.load(root, min_free_gib=args.min_free_gib, identity=args.identity,
                            **_backend(args)))
    code = worker.serve(load,
        stdin=stdin, out=out, on_eof=lambda: os._exit(0), identity=args.identity)
    # the reader thread still holds stdin's lock; a normal return would crash in finalization
    out.flush()
    sys.stderr.flush()
    os._exit(code)


def cmd_smoke(args) -> int:
    """One invented person; with --identity a portrait conditioned on --ref's whole image, so
    the release can measure the identity worker's VRAM (peak_gib)."""
    from . import render
    from .detail import Detail
    from .protocol import Identity, Render, RenderFailed
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        renderer = render.load(data_dir(), min_free_gib=args.min_free_gib,
                               identity=args.identity, **_backend(args))
    except render.ReadyError as e:
        return _fail("smoke", e)
    load_s = time.perf_counter() - t0
    identity = (Identity(str(Path(args.ref).resolve()), (0.0, 0.0, 1.0, 1.0), SMOKE_SCALE)
                if args.identity else None)
    req = Render("smoke", tuple(map(tuple, SMOKE_PHRASES)), tuple(map(tuple, SMOKE_NEGATIVE)),
                 1234, 30, 3.5 if args.backend == "flux1-dev" else 5.0,
                 1024, str(out_dir.resolve() / "smoke.jpg"),
                 str(out_dir.resolve() / "smoke-thumb.jpg"), 256, 0.2, identity,
                 "portrait" if identity else "square",
                 Detail(True, True) if args.detail else None)
    try:
        r = renderer.render(req)
    except RenderFailed as e:
        return _fail("smoke", e)
    print(json.dumps({"load_s": round(load_s, 1), "seconds": r["seconds"],
                      "peak_gib": r["peak_gib"], "nsfw": r["nsfw"], "tokens": r["tokens"],
                      **{k: v for k, v in r.items() if k.startswith("detail_")}}))
    return 1 if r["flagged"] else 0


def cmd_detail(args) -> int:
    """The detailer's pass over an existing JPEG, for experiments without the mind: --out gets
    the image after the pass (never written when it scores --nsfw-max or more), stdout one JSON
    line of counts, codes and timings. --boxes only finds and prints the crops, on the CPU."""
    from . import critic, jpeg
    from .detail import DENOISE_RANGE, SIZES, Detail, plan
    from .protocol import DETAIL_FACE_MAX_RANGE, DETAIL_IDENTITY_MARGIN_RANGE
    src, out = Path(args.src).resolve(), Path(args.out).resolve()
    lo, hi = DENOISE_RANGE
    if not args.faces and not args.hands:
        return _fail("detail", ValueError("nothing to do: --no-faces without --hands"))
    if src == out:
        return _fail("detail", ValueError("--out is --in: the pass never writes over its input"))
    if not (lo <= args.denoise <= hi and lo <= args.hand_denoise <= hi):
        return _fail("detail", ValueError(f"a denoise is not in {lo}-{hi}"))
    if args.size not in SIZES:
        return _fail("detail", ValueError("--size is not 768, 896 or 1024"))
    if not (0.3 <= args.scale <= 0.9 and 10 <= args.steps <= 60 and 1.0 <= args.cfg <= 12.0
            and 0 <= args.seed < 2**32):
        return _fail("detail", ValueError("--scale is 0.3-0.9, --steps 10-60, --cfg 1-12, "
                                          "--seed 0 to 4294967295"))
    if not (DETAIL_FACE_MAX_RANGE[0] <= args.face_max <= DETAIL_FACE_MAX_RANGE[1]
            and DETAIL_IDENTITY_MARGIN_RANGE[0] <= args.identity_margin
            <= DETAIL_IDENTITY_MARGIN_RANGE[1]):
        return _fail("detail", ValueError("--face-max is 0.05-0.5, --identity-margin 0-1"))
    if args.ref is not None and not Path(args.ref).is_file():
        return _fail("detail", ValueError("--ref is not a file"))
    try:
        image = jpeg.open_rgb(str(src))
    except Exception as e:  # Pillow's errors share no narrower base
        return _fail("detail", ValueError(f"--in is not a readable JPEG ({type(e).__name__})"))
    opts = Detail(args.faces, args.hands, args.denoise, args.hand_denoise, args.size,
                  args.face_max, args.identity_margin)
    root = Path(args.data) if args.data else data_dir()
    if args.boxes:
        faces, hands, skipped, code = plan(image, critic.load_detectors(root), opts)
        print(json.dumps({"width": image.size[0], "height": image.size[1],
                          "faces": [list(c) for _, c in faces],
                          "face_ranks": [r for r, _ in faces],
                          "hands": [list(c) for _, c in hands], "skipped": skipped,
                          "code": code}))
        return 0
    from . import render
    from .protocol import Identity
    t0 = time.perf_counter()
    try:
        renderer = render.load(root, min_free_gib=args.min_free_gib,
                               identity=args.ref is not None, **_backend(args))
    except render.ReadyError as e:
        return _fail("detail", e)
    load_s = time.perf_counter() - t0
    identity = (Identity(str(Path(args.ref).resolve()), (0.0, 0.0, 1.0, 1.0), args.scale)
                if args.ref is not None else None)
    torch = renderer.torch
    torch.cuda.reset_peak_memory_stats()
    nsfw_in = renderer.score(image)
    if nsfw_in >= args.nsfw_max:  # as a render: a flagged image gets no pass
        print(json.dumps({"detail_code": "nsfw_base", "flagged": True,
                          "nsfw_in": round(nsfw_in, 4)}))
        return 1
    image, fields = renderer.detail_pass(
        image, opts, seed=args.seed, steps=args.steps, cfg=args.cfg, face_prompt=args.prompt,
        hand_prompt=args.hand_prompt, negative=args.negative, identity=identity)
    nsfw = renderer.score(image)
    flagged = nsfw >= args.nsfw_max
    if not flagged:
        out.parent.mkdir(parents=True, exist_ok=True)
        jpeg.write(image, str(out), 95)
    print(json.dumps({**fields, "flagged": flagged, "nsfw": round(nsfw, 4),
                      "nsfw_in": round(nsfw_in, 4),
                      "load_s": round(load_s, 1),
                      "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                      "seed": args.seed, "identity": identity is not None}))
    return 1 if flagged else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_selfie")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_where = sub.add_parser("where", help="print the data dir")
    p_where.set_defaults(func=cmd_where)
    p_fetch = sub.add_parser("fetch-models", help="fetch the pinned models into "
                                                  "<data dir>/models and check them")
    p_fetch.add_argument("--only", metavar="NAMES",
                         help="comma-separated, of " + ", ".join(models.MODEL_PINS) +
                              " (default: the selected backend's models)")
    p_fetch.add_argument("--backend", choices=models.BACKENDS, default="realvisxl")
    p_fetch.add_argument("--auth", action="store_true",
                         help="use your local Hugging Face login for gated model access")
    p_fetch.set_defaults(func=cmd_fetch_models)
    p_doc = sub.add_parser("doctor", help="check Python, the models and the GPU; one ok, note "
                                          "or FAIL line each")
    p_doc.add_argument("--quick", action="store_true",
                       help="never import torch or hash a file: files, records, revisions")
    p_doc.add_argument("--data", metavar="DIR", help="the data dir to check")
    p_doc.add_argument("--backend", choices=models.BACKENDS, default="realvisxl")
    p_doc.set_defaults(func=cmd_doctor)
    p_worker = sub.add_parser("worker", help="the night worker: JSON lines on stdin and stdout")
    p_worker.add_argument("--data", metavar="DIR", help="the data dir with the models")
    p_worker.add_argument("--min-free-gib", type=float, required=True,
                          help="refuse to load with less free VRAM")
    p_worker.add_argument("--identity", action="store_true",
                          help="load the identity adapter and serve renders that carry one")
    p_worker.add_argument("--embed-only", action="store_true",
                          help="load only the fetched image encoder; use CPU if VRAM is occupied")
    p_worker.add_argument("--backend", choices=models.BACKENDS, default="realvisxl")
    p_worker.set_defaults(func=cmd_worker)
    p_smoke = sub.add_parser("smoke", help="render one invented person into --out")
    p_smoke.add_argument("--out", metavar="DIR", required=True)
    p_smoke.add_argument("--min-free-gib", type=float, default=8.0)
    p_smoke.add_argument("--backend", choices=models.BACKENDS, default="realvisxl")
    p_smoke.add_argument("--identity", action="store_true",
                         help="load the identity adapter and render a portrait with --ref's face")
    p_smoke.add_argument("--ref", metavar="FILE", help="the reference .jpg for --identity")
    p_smoke.add_argument("--detail", action="store_true",
                         help="run the detailer's face and hand pass over the render")
    p_smoke.set_defaults(func=cmd_smoke)
    p_detail = sub.add_parser("detail", help="the detailer's pass over an existing JPEG; needs "
                                             "the GPU (without --boxes)")
    p_detail.add_argument("--in", dest="src", metavar="FILE", required=True, help="the JPEG")
    p_detail.add_argument("--out", metavar="FILE", required=True, help="the JPEG to write")
    p_detail.add_argument("--faces", action=argparse.BooleanOptionalAction, default=True,
                          help="draw the faces again (default)")
    p_detail.add_argument("--hands", action="store_true",
                          help="draw the hands again (needs fetch-models --only palm,handpose)")
    p_detail.add_argument("--denoise", type=float, default=0.35, help="the faces' (0.35)")
    p_detail.add_argument("--hand-denoise", type=float, default=0.40, help="the hands' (0.40)")
    p_detail.add_argument("--face-max", type=float, default=0.18,
                          help="a face box over this share of the frame's shorter side is "
                               "left alone (0.18)")
    p_detail.add_argument("--identity-margin", type=float, default=0.03,
                          help="with --ref: her face stays as it was when its cosine to the "
                               "reference falls by more than this in the pass (0.03)")
    p_detail.add_argument("--size", type=int, default=1024, help="the pass's square (1024)")
    p_detail.add_argument("--ref", metavar="FILE",
                          help="her reference .jpg: loads the identity adapter, and the largest "
                               "face keeps her face")
    p_detail.add_argument("--scale", type=float, default=SMOKE_SCALE,
                          help="the reference's scale (0.6)")
    p_detail.add_argument("--seed", type=int, default=1234)
    p_detail.add_argument("--steps", type=int, default=30)
    p_detail.add_argument("--cfg", type=float, default=None,
                          help="guidance (default: FLUX 3.5, RealVisXL 5.0)")
    p_detail.add_argument("--prompt", default=DETAIL_FACE_PROMPT, help="the face crops' prompt")
    p_detail.add_argument("--hand-prompt", default=DETAIL_HAND_PROMPT)
    p_detail.add_argument("--negative", default=DETAIL_NEGATIVE)
    p_detail.add_argument("--nsfw-max", type=float, default=0.2)
    p_detail.add_argument("--min-free-gib", type=float, default=8.0)
    p_detail.add_argument("--data", metavar="DIR", help="the data dir with the models")
    p_detail.add_argument("--backend", choices=models.BACKENDS, default="realvisxl")
    p_detail.add_argument("--boxes", action="store_true",
                          help="only find and print the crops: the CPU, no model but the "
                               "detectors, nothing written")
    p_detail.set_defaults(func=cmd_detail)
    for command in (p_worker, p_smoke, p_detail):
        command.add_argument("--lora", metavar="FILE",
                             help="explicit local FLUX SafeTensors LoRA; no automatic discovery")
        command.add_argument("--lora-scale", type=float, default=None,
                             help="LoRA strength, 0-2 (default: 1); requires --lora")
    args = parser.parse_args(argv)
    if getattr(args, "lora_scale", None) is not None:
        if getattr(args, "lora", None) is None or not 0 <= args.lora_scale <= 2:
            parser.error("--lora-scale requires --lora and a finite strength from 0 to 2")
    if getattr(args, "lora", None) is not None:
        if args.backend != "flux1-dev" or getattr(args, "embed_only", False):
            parser.error("--lora requires the flux1-dev rendering backend")
    if args.cmd == "detail" and args.cfg is None:
        args.cfg = 3.5 if args.backend == "flux1-dev" else 5.0
    if args.cmd == "smoke" and args.identity != (args.ref is not None):
        p_smoke.error("--identity and --ref go together")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
