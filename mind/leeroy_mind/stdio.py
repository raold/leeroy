"""The detached mind owns its stdout log so Windows can rotate it while it runs."""
import atexit
import io
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .logs import KEEP_DAYS


class _OutputHandler(TimedRotatingFileHandler):
    terminator = ""  # write() supplies the newlines, including traceback fragments.

    def handleError(self, record):
        # Logging's default error path writes back to our stderr and would recurse.
        if sys.__stderr__ is not None:
            sys.__stderr__.write("mind stdout log write failed\n")


class RotatingOutput(io.TextIOBase):
    def __init__(self, directory: Path):
        super().__init__()
        directory.mkdir(parents=True, exist_ok=True)
        self.handler = _OutputHandler(directory / "mind-stdout.log", when="midnight",
                                      backupCount=KEEP_DAYS, encoding="utf-8", delay=True)

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if self.closed:
            raise ValueError("write to closed mind stdout")
        if text:
            self.handler.handle(logging.LogRecord("mind_stdio", logging.INFO, "", 0, text, (), None))
        return len(text)

    def flush(self):
        self.handler.acquire()
        try:
            self.handler.flush()
        finally:
            self.handler.release()

    def close(self):
        if not self.closed:
            super().close()
            self.handler.close()


def capture(directory: Path):
    """Capture prints, warnings and uncaught Python tracebacks; return an idempotent teardown."""
    before = sys.stdout, sys.stderr
    output = RotatingOutput(directory)
    sys.stdout = sys.stderr = output

    def restore():
        if sys.stdout is output:
            sys.stdout = before[0]
        if sys.stderr is output:
            sys.stderr = before[1]
        output.close()
    return restore


def capture_detached():
    if os.environ.pop("LEEROY_CAPTURE_STDIO", "") == "1":
        atexit.register(capture(Path(os.environ["LEEROY_MIND_DATA"]) / "logs"))
