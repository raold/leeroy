"""The worker protocol (Task 22): an expressive engine, or a design engine, runs in a process of
its own under its own venv, and talks to the service over its stdin and stdout.

A request is one JSON line, {"id", "text", "ref", "params"}. An answer is one JSON line,
{"id", "ok", "sr", "frames", "error"}, followed by `frames` float32 samples, little-endian. The
first answer of a worker's life has the id READY and no samples: its model has loaded, or with
ok false it could not. `error` is the failure's type alone, since an exception's message may
quote the line, and the service's log never holds one. A failed line leaves the worker running.

A library that prints would corrupt the pipe, so serve() keeps stdout for itself and points
everything else at stderr, which the service throws away. The worker's loop ends at EOF, so a
service that dies leaves nothing on the card.

Both sides import this file, and a worker's venv does not have leeroy_voice: it needs the
standard library and numpy, and nothing else of this package."""
import json
import os
import sys
from dataclasses import dataclass
from typing import BinaryIO, Callable

import numpy as np

READY = "ready"
MAX_HEAD = 1 << 16  # an answer's JSON line
SAMPLE = "<f4"
SAMPLE_BYTES = 4


@dataclass(frozen=True, slots=True)
class Answer:
    id: str
    ok: bool
    sr: int
    audio: np.ndarray
    error: str


def request_line(id_: str, text: str, ref: str, params: dict) -> bytes:
    return json.dumps({"id": id_, "text": text, "ref": ref, "params": params}).encode() + b"\n"


def read_answer(stream: BinaryIO) -> Answer | None:
    """The next answer, or None at the end of the stream or on anything that is not one."""
    line = stream.readline(MAX_HEAD)
    try:
        head = json.loads(line)
        frames = int(head["frames"])
        size = frames * SAMPLE_BYTES
        body = stream.read(size) if size > 0 else b""
        if len(body) != size:
            return None
        audio = np.frombuffer(body, SAMPLE).astype(np.float32)
        return Answer(str(head["id"]), bool(head["ok"]), int(head["sr"]), audio,
                      str(head["error"]))
    except (ValueError, KeyError, TypeError):
        return None


def _send(out: BinaryIO, id_: str, ok: bool, sr: int, audio, error: str) -> None:
    body = b"" if audio is None else np.asarray(audio, dtype=SAMPLE).reshape(-1).tobytes()
    head = {"id": id_, "ok": ok, "sr": sr, "frames": len(body) // SAMPLE_BYTES, "error": error}
    out.write(json.dumps(head).encode() + b"\n" + body)
    out.flush()


def _claim_stdout() -> BinaryIO:
    """The pipe for the protocol alone: fd 1, and sys.stdout, now lead to stderr."""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return out


def serve(load: Callable[[], tuple[Callable, int]]) -> int:
    """A worker's whole life. `load()` loads the model and returns (speak, rate), where
    speak(text, ref, params) -> mono audio at `rate`."""
    out = _claim_stdout()
    try:
        speak, rate = load()
    except Exception as e:
        _send(out, READY, False, 0, None, type(e).__name__)
        return 1
    try:
        _send(out, READY, True, rate, None, "")
        for line in sys.stdin.buffer:
            id_ = ""
            try:
                request = json.loads(line)
                id_ = str(request["id"])
                audio = speak(request["text"], request["ref"], request["params"])
            except Exception as e:
                _send(out, id_, False, 0, None, type(e).__name__)
            else:
                _send(out, id_, True, rate, audio, "")
    except OSError:  # the service has gone
        return 0
    return 0
