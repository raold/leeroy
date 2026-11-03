"""The voice service's log, like the mind's: JSON lines in voice-data/logs/voice.log, one object
per record ({"ts", "level", "event", ...fields}), rotated at midnight and kept 14 days; WARNING
and above also reach the console.

It holds ids, counts and timings, never line text: log() keeps a `text` or `raw` field only as
its length (`text_chars`), and a traceback keeps its frames and exception types with every
message "<redacted>", as the mind prints a withheld name (an exception's message can quote a
line). A plain logging call is kept as {"event": "log", "msg": ...}. asyncio's own records reach
the same handlers, and asyncio writes its data into the message itself (a failed callback's
arguments, a task's exception), so of those only what comes before the first ( or newline stays:
where it failed, never what it held. The type is in the redacted traceback."""
import json
import logging
import logging.handlers
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

LOGGER = "leeroy_voice"
ASYNCIO_LOGGER = "asyncio"
LOG_FILE = "voice.log"
KEEP_DAYS = 14
PRIVATE_FIELDS = frozenset({"text", "raw"})  # what members and the owner said
REDACTED = "<redacted>"
_EVENT_ATTR, _FIELDS_ATTR = "leeroy_event", "leeroy_fields"
_RESERVED = frozenset({"ts", "level", "event"})
_ASYNCIO_DATA = re.compile(r"[(\n]")  # where asyncio's data starts in its message
_CAUSE = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT = "\nDuring handling of the above exception, another exception occurred:\n\n"


def redacted_traceback(exc: BaseException | None) -> str:
    """The traceback Python would print, cause first, with each exception's frames and type
    but its message (and any notes) replaced by REDACTED."""
    parts: list[str] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        kind = type(exc).__qualname__
        if type(exc).__module__ not in ("builtins", "__main__"):
            kind = f"{type(exc).__module__}.{kind}"
        frames = "".join(traceback.format_tb(exc.__traceback__))
        head = "Traceback (most recent call last):\n" + frames if frames else ""
        parts.append(f"{head}{kind}: {REDACTED}\n")
        if exc.__cause__ is not None:
            parts.append(_CAUSE)
            exc = exc.__cause__
        elif exc.__context__ is not None and not exc.__suppress_context__:
            parts.append(_CONTEXT)
            exc = exc.__context__
        else:
            exc = None
    if parts and parts[-1] in (_CAUSE, _CONTEXT):  # a chain that loops back on itself
        parts.pop()
    return "".join(reversed(parts)).rstrip("\n")


def _shown(v) -> str:
    if isinstance(v, str) and v and not any(c.isspace() for c in v):
        return v
    return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))


def log(event: str, *, level: int = logging.INFO, exc_info: bool = False, **fields) -> None:
    """One structured event, e.g. log("mind_refused", peer="127.0.0.1:50211"). Other handlers
    (the console, pytest's caplog) see it as "event: key=value ..."."""
    for key in PRIVATE_FIELDS & fields.keys():
        v = fields.pop(key)
        fields[f"{key}_chars"] = len(v) if isinstance(v, str) else None
    text = event + (": " + " ".join(f"{k}={_shown(v)}" for k, v in fields.items())
                    if fields else "")
    logging.getLogger(LOGGER).log(level, "%s", text, exc_info=exc_info,
                                  extra={_EVENT_ATTR: event, _FIELDS_ATTR: fields})


def _message(record: logging.LogRecord) -> str:
    """A plain record's message, as the log may keep it."""
    text = record.getMessage()
    if record.name != ASYNCIO_LOGGER and not record.name.startswith(ASYNCIO_LOGGER + "."):
        return text
    head = _ASYNCIO_DATA.split(text, maxsplit=1)[0].rstrip()
    return head if head == text else f"{head} {REDACTED}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created).astimezone().isoformat(
                   timespec="milliseconds"),
               "level": record.levelname}
        event = getattr(record, _EVENT_ATTR, None)
        if event is None:
            out.update(event="log", msg=_message(record))
        else:
            out["event"] = event
            out.update({f"{k}_" if k in _RESERVED else k: v
                        for k, v in getattr(record, _FIELDS_ATTR, {}).items()})
        if record.exc_info and record.exc_info[1] is not None:
            out["exc"] = redacted_traceback(record.exc_info[1])
        return json.dumps(out, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = f"{record.levelname.lower()} {_message(record)}"
        if record.exc_info and record.exc_info[1] is not None:
            text += "\n" + redacted_traceback(record.exc_info[1])
        return text


def utf8_stdio(*streams) -> None:
    """Make text streams UTF-8 that never raise on a character, as a device name on a cp1252
    console would. A stream may be None (pythonw has none)."""
    for stream in streams:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def setup(data_dir: Path, *, level: str | int = "INFO") -> Callable[[], None]:
    """Send the leeroy_voice loggers to <data_dir>/logs/voice.log, and WARNING and above to the
    console when there is one (pythonw has none). Records still propagate to the root logger.
    Returns the teardown, which removes and closes the handlers and restores the level."""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER)
    tasks = logging.getLogger(ASYNCIO_LOGGER)
    handlers: list[logging.Handler] = []
    to_file = logging.handlers.TimedRotatingFileHandler(
        log_dir / LOG_FILE, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8", delay=True)
    to_file.setLevel(level)
    to_file.setFormatter(JsonFormatter())
    handlers.append(to_file)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(ConsoleFormatter())
        handlers.append(console)
    old_level = logger.level
    for handler in handlers:
        logger.addHandler(handler)
        tasks.addHandler(handler)
    logger.setLevel(level)

    def teardown() -> None:
        for handler in handlers:
            logger.removeHandler(handler)
            tasks.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
    return teardown
