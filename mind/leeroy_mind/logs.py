"""The mind service's log: JSON lines in mind-data/logs/mind.log, one object per record:
{"ts", "level", "event", ...fields}. It rotates at midnight and keeps 14 days; WARNING and above
also reach the console.

Code logs either with log(event, **fields) or with ordinary logging calls written as
"event: key=value ..." (the engine's "say_sent: guid=101 kind=greet ..."), which the formatter
splits into the event and its fields: whole-number values become numbers, quoted values lose
their quotes, and any other text after the event goes into "detail". A message that does not
start with an event name is kept whole as {"event": "log", "msg": ...}."""
import json
import logging
import logging.handlers
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

LOGGER = "leeroy_mind"
LOG_FILE = "mind.log"
KEEP_DAYS = 14
_EVENT_ATTR, _FIELDS_ATTR = "leeroy_event", "leeroy_fields"
_RESERVED = frozenset({"ts", "level", "event"})
_EVENT = re.compile(r"([a-z][a-z0-9_]*):(?:\s+(.*))?\Z", re.DOTALL)
_PAIR = re.compile(r"""(\w+)=('[^']*'|"[^"]*"|\S+)""")
_INT = re.compile(r"-?\d+\Z")


def _shown(v) -> str:
    if isinstance(v, str) and v and not any(c.isspace() for c in v):
        return v
    return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))


def log(event: str, *, level: int = logging.INFO, **fields) -> None:
    """One structured event, e.g. log("ollama_unavailable", level=logging.WARNING,
    missing=["model"]). The JSON log keeps the fields' types; other handlers (the console,
    pytest's caplog) see them in the message, as "event: key=value"."""
    text = event + (": " + " ".join(f"{k}={_shown(v)}" for k, v in fields.items())
                    if fields else "")
    logging.getLogger(LOGGER).log(level, "%s", text,
                                  extra={_EVENT_ATTR: event, _FIELDS_ATTR: fields})


def _value(raw: str):
    if raw[:1] in "'\"" and raw[-1:] == raw[:1] and len(raw) >= 2:
        return raw[1:-1]
    raw = raw.rstrip(",;:")
    return int(raw) if _INT.match(raw) else raw


def parse_message(msg: str) -> dict:
    """{"event": ..., key: value, ..., "detail": leftover text} for an "event: k=v" message."""
    m = _EVENT.match(msg)
    if m is None:
        return {"event": "log", "msg": msg}
    out: dict = {"event": m.group(1)}
    rest = m.group(2) or ""
    leftover, pos = [], 0
    for p in _PAIR.finditer(rest):
        leftover.append(rest[pos:p.start()])
        key = p.group(1)
        out[f"{key}_" if key in _RESERVED else key] = _value(p.group(2))
        pos = p.end()
    leftover.append(rest[pos:])
    detail = " ".join("".join(leftover).split()).strip(" :,;")
    if detail:
        out["detail"] = detail
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created).astimezone().isoformat(
                   timespec="milliseconds"),
               "level": record.levelname}
        event = getattr(record, _EVENT_ATTR, None)
        if event is None:
            out.update(parse_message(record.getMessage()))
        else:
            out["event"] = event
            out.update({f"{k}_" if k in _RESERVED else k: v
                        for k, v in getattr(record, _FIELDS_ATTR, {}).items()})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = f"{record.levelname.lower()} {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def setup_logging(log_dir: Path, *, level: str | int = "INFO") -> Callable[[], None]:
    """Send the leeroy_mind loggers to log_dir/mind.log and WARNING+ to the console. Records
    still propagate to the root logger (pytest's caplog sees them). Returns the teardown, which
    removes and closes both handlers and restores the logger's level."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER)
    to_file = logging.handlers.TimedRotatingFileHandler(
        log_dir / LOG_FILE, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8", delay=True)
    to_file.setLevel(level)
    to_file.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(ConsoleFormatter())
    old_level = logger.level
    logger.addHandler(to_file)
    logger.addHandler(console)
    logger.setLevel(level)

    def teardown() -> None:
        for handler in (to_file, console):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
    return teardown
