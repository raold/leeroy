import math
import re

# One left-to-right pass: a bracketed link keeps its [Name]; textures, colour codes, bare link
# codes and stray |h go; "||" is an escaped pipe.
_MARKUP = re.compile(
    r"\|H[^|]*\|h(?P<name>\[[^\]]*\])\|h|\|T[^|]*\|t|\|c[0-9a-fA-F]{8}|\|r|\|H[^|]*\|h|\|h"
    r"|(?P<pipe>\|\|)")

META_RE = re.compile(
    r"\b(as an ai|language model|llm|chatgpt|openai|my programming|as a bot)\b", re.IGNORECASE)

_WORDS = re.compile(r"\w+")

# Pictographs, dingbats, flags, skin tones, keycaps, joiners and variation selectors. Plain
# text emoticons (":)", ";P", "<3") are not emoji.
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002300-\U000023FF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U000E0020-\U000E007F\uFE0F\u200D\u20E3]")

# A chat speaker tag as the prompt shows lines ("Arkon:", "Belra (raid):") or as the client
# does ("[Guild] [Arkon]:"). WoW names are one capitalised word of 2-12 letters, so "lol:",
# "LOL:" and "D:" are not tags. The group "name" holds the name.
NAME_TAG = (r"(?:\[[A-Za-z ]{1,12}\]\s*)?\[?(?P<name>[A-Z][a-z]{1,11})\]?"
            r"(?:\s*\([^)\n]{1,24}\))?\s*:")
NAME_PREFIX_RE = re.compile(r"^\s*" + NAME_TAG + r"\s*")


def strip_wow_markup(text: str) -> str:
    """Render any |H...|h[Name]|h link as [Name], drop colour, texture and link codes, and turn
    the escape || into |."""
    return _MARKUP.sub(lambda m: m.group("name") or ("|" if m.group("pipe") else ""), text)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def truncate_words(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if not text[max_chars].isspace():
        head, sep, _ = cut.rpartition(" ")
        if sep:
            cut = head
    return cut.rstrip()


def has_meta(text: str) -> bool:
    return META_RE.search(text) is not None


def normalize_words(text: str) -> str:
    """Casefolded words joined by single spaces, so punctuation and spacing don't count."""
    return " ".join(_WORDS.findall(text.casefold()))


def word_jaccard(a: str, b: str) -> float:
    x, y = set(_WORDS.findall(a.casefold())), set(_WORDS.findall(b.casefold()))
    return len(x & y) / len(x | y) if x | y else 0.0


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text)


def strip_name_prefix(text: str, name: str) -> str:
    """Drop a leading speaker tag that names `name`, the speaker writing their own line. A
    tag naming anyone else is an address ("Belra: you're up") and stays."""
    m = NAME_PREFIX_RE.match(text)
    if m and name and m.group("name").casefold() == name.casefold():
        return text[m.end():]
    return text
