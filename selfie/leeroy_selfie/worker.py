"""The worker's loop: a ready line, then one answer per request line until quit or the end of
stdin. A ready refusal that says `retry: true` (vram, the pipeline loaded on the CPU) waits for
the mind's retry line and answers ready again, loading nothing twice. Every ready line carries
the start's check_ms and load_ms once it got that far. Logs (stderr) carry ids, counts, scores
and timings, never a prompt."""
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

from . import protocol
from .fit import PromptTooLong
from .protocol import IdentityUnavailable, Quit, Render, RenderFailed, Retry
from .render import ReadyError, Renderer

_EOF = object()
_TOO_LONG = object()


def claim_stdout() -> BinaryIO:
    """The protocol's output: a private copy of fd 1, with fd 1 itself pointed at stderr, so
    nothing a library prints can land in the protocol."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    return out


def claim_stdin() -> BinaryIO:
    """The protocol's input: a private copy of fd 0, with fd 0 itself pointed at the null device.
    On Windows, while the reader waits in a read on the mind's pipe, importing numpy (torch's
    first import) waits for that read to end whenever the standard input still names the pipe,
    and only the mind can end it: the worker hung before its ready line. With fd 0 on the null
    device the reader's copy is the pipe's only handle, and the load goes on."""
    pipe = os.dup(0)
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, 0)
    finally:
        os.close(null)
    return os.fdopen(pipe, "rb")


def _log(text: str) -> None:
    print(f"selfie-worker: {text}", file=sys.stderr, flush=True)


def _send(out: BinaryIO, obj: dict) -> None:
    out.write(json.dumps(obj).encode("utf-8") + b"\n")
    out.flush()


def _reader(stdin: BinaryIO, lines: queue.Queue, on_eof: Callable[[], None]) -> None:
    """Feeds `lines`; however it ends (EOF or a read that fails) it calls on_eof and queues
    _EOF, so the loop never waits on a dead reader."""
    limit = protocol.MAX_LINE
    try:
        while True:
            line = stdin.readline(limit + 2)
            if not line:
                break
            if len(line.rstrip(b"\r\n")) > limit:
                while line and not line.endswith(b"\n"):  # drop the rest of the oversize line
                    line = stdin.readline(limit)
                lines.put(_TOO_LONG)
                continue
            lines.put(line.rstrip(b"\r\n"))
    except Exception as e:
        _log(f"stdin reader failed: {type(e).__name__}")
    finally:
        try:
            on_eof()
        finally:
            lines.put(_EOF)


def _id_of(line: bytes) -> str:
    try:
        rid = json.loads(line).get("id")
    except (ValueError, AttributeError, RecursionError):
        return ""
    if not isinstance(rid, str) or not 1 <= len(rid) <= 32:
        return ""
    try:
        rid.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return rid


def serve(load: Callable[[], Renderer], *, stdin: BinaryIO, out: BinaryIO,
          on_eof: Callable[[], None]) -> int:
    """0 after a quit or the end of stdin, 1 when the renderer could not be loaded. The reader
    runs from the start, so the end of stdin calls on_eof (the worker's: os._exit) at once,
    during the load, a wait for a retry or a render too."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(stdin, lines, on_eof), daemon=True).start()
    renderer = _ready(load, lines, out)
    if renderer is None:
        return 1
    _send(out, {"id": "ready", "ok": True, **renderer.info, "error": ""})
    while True:
        line = lines.get()
        if line is _EOF:
            return 0
        if line is _TOO_LONG:
            _send(out, {"id": "", "ok": False, "error": "bad_request"})
            continue
        try:
            req = protocol.parse(line)
        except IdentityUnavailable:
            _send(out, {"id": _id_of(line), "ok": False, "error": "identity_unavailable"})
            continue
        except ValueError:
            _send(out, {"id": _id_of(line), "ok": False, "error": "bad_request"})
            continue
        if isinstance(req, Quit):
            return 0
        if isinstance(req, Retry):  # it is ready: nothing to try again
            _send(out, {"id": req.id, "ok": False, "error": "bad_request"})
            continue
        _send(out, _answer(renderer, req))


def _ready(load: Callable[[], Renderer], lines: queue.Queue, out: BinaryIO) -> Renderer | None:
    """The renderer, or None once a refusal is sent that is final (or the mind quit or closed
    stdin while a retryable one waited)."""
    attempt = load
    while True:
        try:
            return attempt()
        except ReadyError as e:
            _send(out, {"id": "ready", "ok": False, "error": e.reason, **e.fields,
                        **({"retry": True} if e.retry is not None else {})})
            if e.retry is None or not _retry_asked(lines, out):
                return None
            attempt = e.retry
        except Exception as e:
            _send(out, {"id": "ready", "ok": False, "error": type(e).__name__})
            return None


def _retry_asked(lines: queue.Queue, out: BinaryIO) -> bool:
    """The mind's next line after a retryable refusal: True for a retry, False for a quit or the
    end of stdin; anything else is a bad request, answered, and the wait goes on."""
    while True:
        line = lines.get()
        if line is _EOF:
            return False
        if line is _TOO_LONG:
            _send(out, {"id": "", "ok": False, "error": "bad_request"})
            continue
        try:
            req = protocol.parse(line)
        except ValueError:
            _send(out, {"id": _id_of(line), "ok": False, "error": "bad_request"})
            continue
        if isinstance(req, Retry):
            return True
        if isinstance(req, Quit):
            return False
        _send(out, {"id": req.id, "ok": False, "error": "bad_request"})


def _answer(renderer: Renderer, req: Render) -> dict:
    try:
        fields = renderer.render(req)
    except PromptTooLong:
        error = "prompt_too_long"
    except RenderFailed as e:
        error = str(e)
    except Exception as e:
        error = type(e).__name__
    else:
        _log(f"{req.id} ok flagged={fields.get('flagged')} nsfw={fields.get('nsfw')} "
             f"tokens={fields.get('tokens')} seconds={fields.get('seconds')}")
        return {"id": req.id, "ok": True, "error": "", **fields}
    _log(f"{req.id} failed {error}")
    return {"id": req.id, "ok": False, "error": error}
