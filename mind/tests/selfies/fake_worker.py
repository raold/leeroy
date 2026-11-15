"""A stand-in for `python -m leeroy_selfie worker` (selfie/leeroy_selfie/worker.py) that speaks
the same JSON lines without torch or a model. Run as a script under `python -I`, so it imports
only the standard library. Its behaviour comes from the environment:

FAKE_SELFIE_READY_ERROR  refuse ready with this error and exit 1
FAKE_SELFIE_VRAM         how many ready answers refuse for vram with retry true; each waits for
                         the mind's retry line (the real worker keeps its pipeline on the CPU)
FAKE_SELFIE_STARTS       a file it appends "spawn <pid>" to when it starts and "retry" to for
                         each retry line
FAKE_SELFIE_FLAG_SEEDS   a comma list of seeds whose renders come back flagged (nothing written)
FAKE_SELFIE_OOM          how many renders answer `oom` before the rest succeed
FAKE_SELFIE_HANG         renders never answer
FAKE_SELFIE_HANG_AFTER   renders after the first n never answer
FAKE_SELFIE_DIE          the worker exits (3) when a render arrives, without an answer
FAKE_SELFIE_CHILD        start a sleeping child process before ready
FAKE_SELFIE_PIDS         a file it writes {"pid", "child"} to before ready
FAKE_SELFIE_LOG          a file it appends "<id> <seed>" (plus " identity <ref>" for an identity render) to for each
                         render request and "<id> grid <tile count>" for each grid
FAKE_SELFIE_TOO_LONG    a comma list of request id prefixes that answer prompt_too_long
FAKE_SELFIE_BAD_SIMILARITY  grids answer a malformed similarity diagnostic
FAKE_SELFIE_FLAG_CAPTION  captions come back flagged (nothing written)
FAKE_SELFIE_IDENTITY     ready says identity true, and a render without an identity answers
                         bad_request (a plain worker answers identity_unavailable to one with)

A grid writes JPEG to `out` and `thumb` and answers the canvas size from the real worker's layout
(selfie/leeroy_selfie/grid.py); a tile that is not a file answers missing_tile.

A caption (the culture memes) writes JPEG to `out_path` when `base_path` is a file (else
missing_base) and answers a 1024 x 1024 image; FAKE_SELFIE_LOG gets "<id> caption".

A render it answers writes JPEG to `out` and `thumb`. Like the real worker, the end of stdin
ends it at once, mid-render too, and quit ends it with 0."""
import json
import os
import queue
import subprocess
import sys
import threading

JPEG = bytes.fromhex(  # an 8 x 8 JPEG, the smallest file every decoder reads
    "ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09"
    "090a140e0f0c1017141818171416161a1d251f1a1b231c1616202c20232627292a29191f2d302d2830252829"
    "28ffdb0043010707070a080a130a0a13281a161a28282828282828282828282828282828282828282828282828"
    "282828282828282828282828282828282828282828ffc00011080008000803012200021101031101ffc4001f00"
    "00010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504"
    "040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a"
    "161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a7374"
    "75767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4"
    "c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101"
    "010101010101010000000000000102030405060708090a0bffc400b511000201020404030407050404000102"
    "77000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f1"
    "1718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a7374757677"
    "78797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6"
    "c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f"
    "00dda28a2bcc3d93ffd9")
READY = {"id": "ready", "ok": True, "device": "cuda", "free_gib": 20.0,
         "versions": {"fake": "1"}, "error": "", "check_ms": 12, "load_ms": 345}
VRAM = {"id": "ready", "ok": False, "error": "vram", "retry": True, "check_ms": 12,
        "load_ms": 345, "free_gib": 9.5}


LAYOUTS = ((1, 1, 1), (2, 2, 1), (4, 2, 2), (6, 3, 2), (9, 3, 3), (12, 4, 3), (16, 4, 4))


def _canvas(n: int, tile: int, gutter: int) -> tuple[int, int]:
    cols, rows = next((c, r) for most, c, r in LAYOUTS if n <= most)
    return gutter + cols * (tile + gutter), gutter + rows * (tile + gutter)


def _grid(req: dict) -> None:
    if os.environ.get("FAKE_SELFIE_LOG"):
        with open(os.environ["FAKE_SELFIE_LOG"], "a", encoding="utf-8") as f:
            f.write(f"{req['id']} grid {len(req['tiles'])}\n")
    if not all(os.path.isfile(t) for t in req["tiles"]):
        _send({"id": req["id"], "ok": False, "error": "missing_tile"})
        return
    width, height = _canvas(len(req["tiles"]), req["tile"], req["gutter"])
    _write(req["out"], JPEG)
    _write(req["thumb"], JPEG)
    extra = {"similarity": {"code": "ok", "pairs": 99}}         if os.environ.get("FAKE_SELFIE_BAD_SIMILARITY") else {}
    _send({"id": req["id"], "ok": True, "error": "", "width": width, "height": height,
           "seconds": 0.01, **extra})


def _caption(req: dict) -> None:
    if os.environ.get("FAKE_SELFIE_LOG"):
        with open(os.environ["FAKE_SELFIE_LOG"], "a", encoding="utf-8") as f:
            f.write(f"{req['id']} caption\n")
    if not os.path.isfile(req["base_path"]):
        _send({"id": req["id"], "ok": False, "error": "missing_base"})
        return
    flagged = bool(os.environ.get("FAKE_SELFIE_FLAG_CAPTION"))
    if not flagged:
        _write(req["out_path"], JPEG)
    _send({"id": req["id"], "ok": True, "error": "", "flagged": flagged,
           "nsfw": 0.95 if flagged else 0.01, "width": 1024, "height": 1024, "lines": 2,
           "seconds": 0.01})


def _send(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _child() -> int:
    python = getattr(sys, "_base_executable", None) or sys.executable
    return subprocess.Popen(
        [python, "-I", "-c", "import time; time.sleep(600)"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).pid


def _reader(lines: queue.Queue) -> None:
    for line in sys.stdin.buffer:
        lines.put(line)
    os._exit(0)


def _note(text: str) -> None:
    if os.environ.get("FAKE_SELFIE_STARTS"):
        with open(os.environ["FAKE_SELFIE_STARTS"], "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main() -> None:
    env = os.environ
    _note(f"spawn {os.getpid()}")
    child = _child() if env.get("FAKE_SELFIE_CHILD") else None
    if env.get("FAKE_SELFIE_PIDS"):
        _write(env["FAKE_SELFIE_PIDS"], json.dumps({"pid": os.getpid(), "child": child}).encode())
    if env.get("FAKE_SELFIE_READY_ERROR"):
        _send({"id": "ready", "ok": False, "error": env["FAKE_SELFIE_READY_ERROR"]})
        os._exit(1)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(lines,), daemon=True).start()
    for _ in range(int(env.get("FAKE_SELFIE_VRAM") or 0)):
        _send(VRAM)
        req = json.loads(lines.get())
        if req.get("op") != "retry":
            os._exit(1)
        _note("retry")
    identity = bool(env.get("FAKE_SELFIE_IDENTITY"))
    _send({**(READY if env.get("FAKE_SELFIE_VRAM") is None else
              {**READY, "check_ms": 0, "load_ms": 0}), **({"identity": True} if identity else {})})
    flagged = {int(s) for s in env.get("FAKE_SELFIE_FLAG_SEEDS", "").split(",") if s.strip()}
    oom = int(env.get("FAKE_SELFIE_OOM") or 0)
    hang_after = int(env["FAKE_SELFIE_HANG_AFTER"]) if env.get("FAKE_SELFIE_HANG_AFTER") else None
    renders = 0
    while True:
        req = json.loads(lines.get())
        if req.get("op") == "quit":
            os._exit(0)
        if req.get("op") == "grid":
            _grid(req)
            continue
        if req.get("op") == "caption":
            _caption(req)
            continue
        if req.get("op") == "embed":
            if env.get("FAKE_SELFIE_HANG"):
                threading.Event().wait()
            vector = [1.0] + [0.0]*1023
            if env.get("FAKE_SELFIE_BAD_EMBED"):
                vector = [1.0]
            _send({"id":req["id"],"ok":True,"error":"","model":"ipadapter-vith-head-v1",
                   "vector":vector,"device":"cpu","seconds":.01})
            continue
        if env.get("FAKE_SELFIE_LOG"):
            ref = (req.get("identity") or {}).get("ref")
            with open(env["FAKE_SELFIE_LOG"], "a", encoding="utf-8") as f:
                f.write(f"{req['id']} {req['seed']}" + (f" identity {ref}" if ref else "") + "\n")
        renders += 1
        if any(req["id"].startswith(x) for x in env.get("FAKE_SELFIE_TOO_LONG", "").split(",") if x):
            _send({"id": req["id"], "ok": False, "error": "prompt_too_long"})
            continue
        if env.get("FAKE_SELFIE_HANG") or (hang_after is not None and renders > hang_after):
            threading.Event().wait()
        if env.get("FAKE_SELFIE_DIE"):
            os._exit(3)
        if (req.get("identity") is not None) != identity:
            _send({"id": req["id"], "ok": False,
                   "error": "bad_request" if identity else "identity_unavailable"})
            continue
        if oom > 0:
            oom -= 1
            _send({"id": req["id"], "ok": False, "error": "oom"})
            continue
        prompt = ", ".join(text for text, _ in req["phrases"])
        bad = req["seed"] in flagged
        if not bad:
            _write(req["out"], JPEG)
            _write(req["thumb"], JPEG)
        width, height = (832, 1216) if req.get("shape") == "portrait" else (req["size"],) * 2
        _send({"id": req["id"], "ok": True, "error": "", "flagged": bad,
               "width": width, "height": height,
               "nsfw": 0.95 if bad else 0.01, "prompt": prompt, "dropped": [],
               "tokens": len(prompt.split()), "seconds": 0.01, "peak_gib": 10.5})


if __name__ == "__main__":
    main()
