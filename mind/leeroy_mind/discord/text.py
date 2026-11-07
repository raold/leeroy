"""The text rules of the guild's Discord: what a message may hold and what counts as one emoji.
The page mirrors `is_one_emoji` in JS, so keep the grammar here small and exact."""
import re
import unicodedata

from ..storage.store import clean

MAX_TEXT = 2000
MAX_THREAD_NAME = 60


class TextRefused(ValueError):
    """A text the rules refuse; `code` is `empty` or `too_long`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_NEWLINES = re.compile(r"\n{4,}")


def clean_text(s: str) -> str:
    """Lone surrogates replaced, control characters except newline dropped (CRLF is a newline),
    runs of newlines cut to three, the ends trimmed. Empty or over MAX_TEXT code points is
    refused, never cut."""
    s = clean(s).replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(c for c in s if c == "\n" or unicodedata.category(c) != "Cc")
    s = _NEWLINES.sub("\n\n\n", s).strip()
    if not s:
        raise TextRefused("empty")
    if len(s) > MAX_TEXT:
        raise TextRefused("too_long")
    return s


# One grapheme: a flag, a tag sequence, or units joined by ZWJ. A unit is a keycap or an emoji
# code point with an optional variation selector and skin tone.
_BASE = ("[\U0001F000-\U0001F1E5\U0001F200-\U0001F3FA\U0001F400-\U0001FAFF"
         "\U00002300-\U000023FF\U00002600-\U000027BF\U00002B00-\U00002BFF]")
_UNIT = rf"(?:[#*0-9]️⃣|{_BASE}️?[\U0001F3FB-\U0001F3FF]?)"
_EMOJI = re.compile(
    rf"(?:[\U0001F1E6-\U0001F1FF]{{2}}|\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F"
    rf"|{_UNIT}(?:‍{_UNIT})*)")


def is_one_emoji(s: str) -> bool:
    return _EMOJI.fullmatch(s) is not None
