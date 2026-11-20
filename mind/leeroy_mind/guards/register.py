"""Chat register on WoW turns ([talk] wow_topic; talk/wowtopic.py). The blind A/B of 2026-11-20
found the new live model writing raid and guild chat like a letter: capitalised full sentences
with no contraction anywhere, lines opening "It is ...", "dear" and "honey" in raid chat, and her
sheet's written stage notes ("[objection: ...]", "(Tidy.)") said verbatim. One soft code,
`chat_register`: the draft gets one screened rewrite whose hint says what chat sounds like; an
answer owed to him is never dropped for it (the owed rules keep the best soft-only draft).

`reasons(text)` names what it found, never the text:
- `formal`: the first sentence opens with a capital letter, has FORMAL_WORDS words or more, ends
  in a full stop or "!", holds an uncontracted pair people contract in chat ("it is", "do not",
  "I am", "cannot"), and the draft has no contraction anywhere;
- `it_is`: the draft opens "It is" (any case), never "it is what it is";
- `pet_name`: a pet name, "honey", "hun", "sweetie", "sweetheart", "darling", "babe" anywhere,
  "dear", "love", "sugar" and "hon" only as an address ("thanks, dear", "my dear", "Dear, ...");
- `aside`: a written stage note, a bracketed label ("[objection: ...]", never a recipe's
  "[Pattern: ...]") or one word alone in parentheses written as a note ("(Tidy.)", never "(lol)").

Read only for a draft marked `wow_topic` (Draft.wow_topic). Pure: no clock, no I/O, no logging."""
from __future__ import annotations

import re

CODE = "chat_register"
FORMAL_WORDS = 6
HINT = ("it reads like written prose, not WoW chat: type it the way people type in raid and guild "
        "chat, with contractions, no pet names like dear or honey, no bracketed or parenthesised "
        "stage notes")

_CONTRACTED = re.compile(r"\b\w+['’](?:s|t|re|ve|ll|d|m)\b|\b(?:dont|cant|wont|didnt|doesnt|"
                         r"isnt|arent|wasnt|werent|im|ive|youre|theyre|thats|whats|theres|"
                         r"couldnt|wouldnt|shouldnt|hasnt|havent|lets|gonna|wanna|gotta)\b",
                         re.IGNORECASE)
_UNCONTRACTED = re.compile(r"\b(?:(?:it|that|there|what|who|he|she) is|i am|(?:you|we|they) are|"
                           r"(?:do|does|did|is|are|was|were|could|would|should|will|have|has|"
                           r"had) not|cannot|(?:i|we|you|they) will|let us)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_IT_IS = re.compile(r"^\W*it\s+is\b", re.IGNORECASE)
_IT_IS_WHAT = re.compile(r"^\W*it\s+is\s+what\s+it\s+is\b", re.IGNORECASE)
_PET_ANY = re.compile(r"\b(?:honey|hun|sweetie|sweetheart|darling|sweetpea|babe|babes)\b",
                      re.IGNORECASE)
_PET_ADDRESS = re.compile(r"(?:^|[,.!?;:]\s*|\bmy\s+)(?:dear|love|sugar|hon)\b(?=\s*(?:[,.!?;:]|$))",
                          re.IGNORECASE)
# a bracketed label is a stage note; a recipe's or a pattern's name is an item
_RECIPES = frozenset({"pattern", "formula", "design", "plans", "schematic", "recipe", "manual",
                      "technique"})
_LABEL = re.compile(r"\[\s*([A-Za-z][A-Za-z ]{0,24}?)\s*:[^\[\]\n]*\]")
# one word alone in parentheses, written as a note ("(Tidy.)", "(Sigh)"): never chat's "(lol)"
_WORD_ALONE = re.compile(r"\(\s*(?:[A-Z][a-z]+[.!]?|[a-z]+[.!])\s*\)")


def _formal(text: str) -> bool:
    s = text.lstrip(" \"'“(")
    if not s[:1].isupper():
        return False
    end = _SENTENCE_END.search(s)
    if end is None or s[end.start()] == "?":
        return False
    first = s[:end.start()]
    return (len(first.split()) >= FORMAL_WORDS and bool(_UNCONTRACTED.search(first))
            and not _CONTRACTED.search(text))


def reasons(text: str) -> tuple[str, ...]:
    """What reads as written prose in `text` (the module docstring); () for chat."""
    text = " ".join((text or "").split())
    if not text:
        return ()
    out = []
    if _formal(text):
        out.append("formal")
    if _IT_IS.search(text) and not _IT_IS_WHAT.search(text):
        out.append("it_is")
    if _PET_ANY.search(text) or _PET_ADDRESS.search(text):
        out.append("pet_name")
    if (any(m.group(1).split()[0].casefold() not in _RECIPES for m in _LABEL.finditer(text))
            or _WORD_ALONE.search(text)):
        out.append("aside")
    return tuple(out)


__all__ = ["CODE", "FORMAL_WORDS", "HINT", "reasons"]
