"""The worker's loop: a ready line, then one answer per request line until quit or the end of
stdin. Logs (stderr) carry ids, counts, scores and timings, never a prompt."""
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

from . import protocol
from .fit import PromptTooLong
from .protocol import IdentityUnavailable, Quit, Render, RenderFailed
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
    during the load or a render too."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(stdin, lines, on_eof), daemon=True).start()
    try:
        renderer = load()
    except ReadyError as e:
        _send(out, {"id": "ready", "ok": False, "error": e.reason})
        return 1
    except Exception as e:
        _send(out, {"id": "ready", "ok": False, "error": type(e).__name__})
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
        _send(out, _answer(renderer, req))


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
