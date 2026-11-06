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
FAKE_SELFIE_DIE          the worker exits (3) when a render arrives, without an answer
FAKE_SELFIE_CHILD        start a sleeping child process before ready
FAKE_SELFIE_PIDS         a file it writes {"pid", "child"} to before ready
FAKE_SELFIE_LOG          a file it appends "<id> <seed>" to for each render request

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
    _send(READY if env.get("FAKE_SELFIE_VRAM") is None else
          {**READY, "check_ms": 0, "load_ms": 0})
    flagged = {int(s) for s in env.get("FAKE_SELFIE_FLAG_SEEDS", "").split(",") if s.strip()}
    oom = int(env.get("FAKE_SELFIE_OOM") or 0)
    while True:
        req = json.loads(lines.get())
        if req.get("op") == "quit":
            os._exit(0)
        if env.get("FAKE_SELFIE_LOG"):
            with open(env["FAKE_SELFIE_LOG"], "a", encoding="utf-8") as f:
                f.write(f"{req['id']} {req['seed']}\n")
        if env.get("FAKE_SELFIE_HANG"):
            threading.Event().wait()
        if env.get("FAKE_SELFIE_DIE"):
            os._exit(3)
        if oom > 0:
            oom -= 1
            _send({"id": req["id"], "ok": False, "error": "oom"})
            continue
        prompt = ", ".join(text for text, _ in req["phrases"])
        bad = req["seed"] in flagged
        if not bad:
            _write(req["out"], JPEG)
            _write(req["thumb"], JPEG)
        _send({"id": req["id"], "ok": True, "error": "", "flagged": bad,
               "nsfw": 0.95 if bad else 0.01, "prompt": prompt, "dropped": [],
               "tokens": len(prompt.split()), "seconds": 0.01, "peak_gib": 10.5})


if __name__ == "__main__":
    main()
