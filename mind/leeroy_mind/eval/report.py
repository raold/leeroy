"""Private weekly evaluation artifacts, with explicit missing measurements and trends."""
import html
import json
import math
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from leeroy_mind.eval.quiz import _week

validate_week = _week


def _path(reports_dir, week, suffix):
    validate_week(week)
    root = Path(reports_dir).resolve()
    target = root / f"{week}.{suffix}"
    # An existing symlink must not turn a private report write/read into another path.
    if target.resolve().parent != root or target.is_symlink():
        raise ValueError("report path is not a regular file inside reports_dir")
    return target


def load_prev(reports_dir, week):
    previous = validate_week(week) - timedelta(days=7)
    year, number, _ = previous.isocalendar()
    path = _path(reports_dir, f"{year:04d}-W{number:02d}", "json")
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _escape(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _display(value):
    if value is None:
        return "not measured"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (dict, list, tuple)):
        return _escape(json.dumps(value, ensure_ascii=False))
    return _escape(value)


def _flat(value, prefix=""):
    for key, item in value.items():
        if key in ("rate_these", "skipped"):
            continue
        label = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            yield from _flat(item, label)
        else:
            yield label, item


def _markdown(week, sections, prev):
    lines = [f"# Weekly evaluation — {week}", ""]
    for key, section in sections.items():
        lines.extend(["## " + _escape(key.replace("_", " ").capitalize()), ""])
        if section.get("skipped"):
            lines.extend(["_skipped_", ""])
            continue
        old = (prev or {}).get(key, {})
        old_flat = dict(_flat(old)) if isinstance(old, dict) and not old.get("skipped") else {}
        metrics = list(_flat(section))
        if metrics:
            lines.extend(["| Measure | Value | vs last week |", "| --- | --- | --- |"])
            for label, value in metrics:
                before = old_flat.get(label)
                trend = "—"
                if _number(value) and _number(before):
                    trend = "▲" if value > before else "▼" if value < before else "="
                lines.append(f"| {_escape(label)} | {_display(value)} | {trend} |")
            lines.append("")
        if section.get("rate_these"):
            lines.extend(["### Owner ratings", ""])
            for row in section["rate_these"]:
                lines.append(f"- [ ] {_escape(row['guid'])}: {_escape(row['text'])}")
                if row.get("evidence"):
                    lines.append("  Evidence: " + _display(row["evidence"]))
            lines.append("")
    return "\n".join(lines)


def _atomic(path, text):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.resolve().parent != path.parent:
            raise ValueError("report destination changed")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_report(reports_dir, week, sections, prev):
    """Return (Markdown, JSON); preserve raw sections and never invent a zero sample rate."""
    md, js = _path(reports_dir, week, "md"), _path(reports_dir, week, "json")
    if not isinstance(sections, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                             for k, v in sections.items()):
        raise ValueError("report sections must be named objects")
    # Validate both serializations before making either artifact visible.
    raw = json.dumps(sections, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    rendered = _markdown(week, sections, prev)
    md.parent.mkdir(parents=True, exist_ok=True)
    _atomic(js, raw)
    _atomic(md, rendered)
    return md, js
