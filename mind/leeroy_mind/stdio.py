"""Rotate detached output before launch, keeping the child's OS stderr visible."""
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

from .logs import KEEP_DAYS

_ROTATED = re.compile(r"\.(\d{4}-\d{2}-\d{2})(?:\.(\d+))?$")


def _since(path: Path) -> Path:
    return path.with_name(path.name + "-since")


def _began(path: Path) -> date:
    """The day the current file was begun: what the launcher noted when it began it, else (a
    file from before the note) the earlier of its creation and its last write."""
    try:
        return date.fromisoformat(_since(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    stat = path.stat()
    born = getattr(stat, "st_birthtime", stat.st_ctime if os.name == "nt" else stat.st_mtime)
    return datetime.fromtimestamp(min(born, stat.st_mtime)).date()


def rotate_before_launch(path: Path, *, now: float | None = None, keep: int = KEEP_DAYS):
    """Roll output begun on a prior day, then let the child inherit an append handle.

    No stream is redirected inside Python. Import errors, venv launcher failures
    and writes to OS fd 2 therefore reach the same operator-visible file.
    Windows handles stay open until the child exits, so rotation is at launch.

    The day is the one the file was begun on, kept in a note beside it. The file's last write
    cannot say: a mind that is restarted writes its last line as it stops, so at the next launch
    the file always looked like today's and never rolled."""
    path = Path(path)
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    try:
        if path.exists() and path.stat().st_size:
            began = _began(path)
            if began < today:
                target, n = path.with_name(f"{path.name}.{began.isoformat()}"), 0
                while target.exists():  # never over an earlier day's file
                    n += 1
                    target = path.with_name(f"{path.name}.{began.isoformat()}.{n}")
                os.replace(path, target)
                _since(path).unlink(missing_ok=True)
        if not _since(path).exists():
            # a file kept from an earlier launch today (or an old one that could not be
            # rolled) keeps the day it began
            began = _began(path) if path.exists() and path.stat().st_size else today
            _since(path).write_bytes(began.isoformat().encode("ascii"))
        rolled = sorted((m.group(1), int(m.group(2) or 0), p) for p in path.parent.iterdir()
                        if p.name.startswith(path.name + ".")
                        and (m := _ROTATED.search(p.name[len(path.name):])))
        for _, _, old in rolled[:max(0, len(rolled) - keep)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # the launch matters more than the rotation
