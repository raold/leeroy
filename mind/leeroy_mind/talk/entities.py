"""The mind's own instruction text names only what the game has ([talk] blind A/B, 2026-11-20:
an entity invented once in a line got carried into the next turn's instruction, and the next
member repeated it as fact).

`known_only` drops, from a text the mind builds for a turn (a seed, an outreach item, an aside, an
opener's topic), every sentence that names a game entity the library does not know: a boss or a
battleground by the "game name" tell's own reading (talk/gamenames.py, passed in as `invented`),
or a bracketed item the item table has no entry for (`item`). Members' chat is never read here: it
is what the chat section shows, and a member may say what she likes there (the tells judge her
own line). Quoted material inside the instruction (a "..." quote, a <tag>...</tag> block such as
banter's observation) is evidence, never the mind's own words: it is left whole, and so is a
sentence that runs into it. Both checks fail open: with neither given the text comes back as it
was.

Pure: no clock, no I/O, no logging."""
from __future__ import annotations

import re
from collections.abc import Callable

_SEPARATOR = re.compile(r"((?<=[.!?])\s+)")
_BRACKET = re.compile(r"\[([^\[\]\n]{2,80})\]")
_QUOTED = re.compile(r"<(\w+)>.*?</\1>|\"[^\"\n]*\"|“[^”\n]*”", re.S)


def _unknown_item(sentence: str, item: Callable[[str], bool] | None) -> bool:
    if item is None:
        return False
    return any(not item(" ".join(m.group(1).split())) for m in _BRACKET.finditer(sentence))


def known_only(text: str, *, invented: Callable[[str], bool] | None = None,
               item: Callable[[str], bool] | None = None) -> tuple[str, int]:
    """(`text` without its sentences that name an unknown game entity, how many were dropped).
    `invented(sentence)`: the sentence names a boss or a battleground nobody could know;
    `item(name)`: the item table knows the bracketed name. A text whose every sentence goes comes
    back as "" (the turn's template then stands on its own)."""
    if not text or (invented is None and item is None):
        return text, 0
    pieces, last = [], 0  # (quoted, text)
    for m in _QUOTED.finditer(text):
        pieces += [(False, text[last:m.start()]), (True, m.group(0))]
        last = m.end()
    pieces.append((False, text[last:]))
    out, dropped = [], 0
    for i, (quoted, piece) in enumerate(pieces):
        if quoted or not piece:
            out.append(piece)
            continue
        parts = _SEPARATOR.split(piece)  # sentence, separator, sentence, ...
        kept = []
        for j in range(0, len(parts), 2):
            sentence, sep = parts[j], parts[j + 1] if j + 1 < len(parts) else ""
            touches = (j == 0 and i > 0) or (j == len(parts) - 1 and i < len(pieces) - 1)
            if (sentence.strip() and not touches
                    and ((invented is not None and invented(sentence))
                         or _unknown_item(sentence, item))):
                dropped += 1
                continue
            kept.append(sentence + sep)
        out.append("".join(kept))
    if not dropped:
        return text, 0
    return " ".join("".join(out).split()), dropped


__all__ = ["known_only"]
