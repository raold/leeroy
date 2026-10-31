"""Prompt text. Built-in defaults are non-explicit at every dial setting; private or explicit
wording lives only in mind-data/prompts/<name>.txt, which is never in the repository."""
import codecs
import logging
import re
from pathlib import Path
from typing import NamedTuple

from .logs import log as log_event

_NAME = re.compile(r"[A-Za-z0-9_-]+")
_REPLACED = chr(0xFFFD)  # what errors="replace" puts for a byte that is not UTF-8
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_warned: set[str] = set()  # the overlays already warned about, by path


class Overlay(NamedTuple):
    """An overlay file as read: its text (stripped), the encoding it was read in ("utf-8", or
    "utf-16" for a file with a UTF-16 byte order mark) and how many characters could not be
    read (each became U+FFFD)."""
    text: str
    encoding: str
    replaced: int


def read_overlay(name: str, data_dir: Path) -> Overlay | None:
    """mind-data/prompts/<name>.txt, or None when there is none. UTF-8 with or without a byte
    order mark; UTF-16 when it starts with one of UTF-16's (Notepad's "Unicode" save); a file
    in another encoding (an ANSI code page) is still read, each byte that is not UTF-8
    replaced (privacy minor 11, part F review minor 7)."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"bad prompt name {name!r}")
    path = Path(data_dir) / "prompts" / f"{name}.txt"
    if not path.is_file():
        return None
    data = path.read_bytes()
    # utf-16 reads the byte order from its mark and drops it, as utf-8-sig drops UTF-8's
    encoding = "utf-16" if data.startswith(_UTF16_BOMS) else "utf-8"
    codec = "utf-16" if encoding == "utf-16" else "utf-8-sig"
    try:
        text, replaced = data.decode(codec), 0
    except UnicodeDecodeError:
        text = data.decode(codec, errors="replace")
        replaced = text.count(_REPLACED)
    return Overlay(text.strip(), encoding, replaced)


def load_prompt(name: str, data_dir: Path, default: str) -> str:
    """mind-data/prompts/<name>.txt when it exists and is not blank, else `default`, read as
    read_overlay reads it. One with characters that could not be read is warned about once
    per file, with its name and the count, never its text: a turn never fails on it."""
    found = read_overlay(name, data_dir)
    if found is None:
        return default
    if found.replaced:
        path = Path(data_dir) / "prompts" / f"{name}.txt"
        key = str(path.resolve())
        if key not in _warned:
            _warned.add(key)
            log_event("prompt_overlay_not_utf8", level=logging.WARNING, file=str(path),
                      replaced=found.replaced, fix="save it as UTF-8")
    return found.text or default
