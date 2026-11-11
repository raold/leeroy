"""Rotate detached output before launch, keeping the child's OS stderr visible."""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .logs import KEEP_DAYS


def rotate_before_launch(path: Path):
    """Roll a prior day's output, then let the child inherit an append handle.

    No stream is redirected inside Python. Import errors, venv launcher failures
    and writes to OS fd 2 therefore reach the same operator-visible file.
    Windows handles stay open until the child exits, so rotation is at launch.
    """
    handler=TimedRotatingFileHandler(path,when='midnight',backupCount=KEEP_DAYS,
                                    encoding='utf-8',delay=True)
    try:
        if path.exists() and handler.shouldRollover(logging.makeLogRecord({})):
            handler.doRollover()
    finally:
        handler.close()
