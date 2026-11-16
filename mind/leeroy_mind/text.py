import math
import re

# One left-to-right pass: a bracketed link keeps its [Name]; textures, colour codes, bare link
# codes and stray |h go; "||" is an escaped pipe.
_MARKUP = re.compile(
    r"\|H[^|]*\|h(?P<name>\[[^\]]*\])\|h|\|T[^|]*\|t|\|c[0-9a-fA-F]{8}|\|r|\|H[^|]*\|h|\|h"
    r"|(?P<pipe>\|\|)")

# Talk about the model or its prompt (identity audit, 2026-11-15: a member told officer chat what
# "the prompt" showed her and that she had "no chat history"). Each phrase is one a player in
# 2009 never types about their own chat: "follow my instructions", "accept the summon prompt",
# "i'm an assistant manager" and "i was instructed to bring snacks" stay.
_I_AM = r"(?:i'm|i’m|i am|im)"
META_RE = re.compile(
    r"\b(as an ai|language model|llm|chatgpt|openai|my programming|as a bot"
    rf"|{_I_AM} (?:just )?an? (?:ai|chatbot|ai assistant)|an ai (?:assistant|model)"
    r"|system prompt|system message|my prompt"
    r"|the prompt (?:shows|showed|tells|told|gives|gave|only (?:has|shows)) (?:me|us)"
    r"|the prompt (?:doesn't|does not|didn't|did not) (?:show|give|tell) (?:me|us)"
    r"|my (?:system )?instructions (?:say|said|are not|aren't|don't|do not|won't|tell me|told me"
    r"|forbid|prevent|only)|against my (?:instructions|guidelines|programming)|my guidelines"
    r"|(?:no|the|full|whole|my) chat history|no chat shown|chat (?:shown|provided|given)"
    r"|(?:my|your) (?:previous|last) lines)\b",
    re.IGNORECASE)

_WORDS = re.compile(r"\w+")

# Pictographs, dingbats, flags, skin tones, keycaps, joiners and variation selectors. Plain
# text emoticons (":)", ";P", "<3") are not emoji.
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002300-\U000023FF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U000E0020-\U000E007F\uFE0F\u200D\u20E3]")

# A chat speaker tag as the prompt shows lines ("Arkon:", "Belra (raid):", "Belra (she, whisper
# to Arkon):") or as the client does ("[Guild] [Arkon]:"). WoW names are one capitalised word of
# 2-12 letters, so "lol:", "LOL:" and "D:" are not tags. The group "name" holds the name.
NAME_TAG = (r"(?:\[[A-Za-z ]{1,12}\]\s*)?\[?(?P<name>[A-Z][a-z]{1,11})\]?"
            r"(?:\s*\([^)\n]{1,32}\))?\s*:")
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
