"""Whose "he" a recalled memory means, in a private turn with him (self-play audit,
2026-11-17: a memory of her ex-husband, "the app tracking his gym hours", reached her DM with
the owner; he is the only "he" in that prompt, so it became "your gym hours" and she told him
she tracks when he leaves).

`for_him(memories, ...)` goes through the memories recalled for a whisper or Discord DM with him
and, for one whose text uses his pronoun (his forms: he/him/his, or she/her) about somebody else:

- about him after all, shown as written: he is one of its actors, it is private with him
  (scope_with), or it names him;
- the text names who it is about before the pronoun ("my ex kept his key"): shown as written;
- another member is its actor: the pronoun becomes that member's name;
- the text names a role only after the pronoun ("his gym hours ... my ex"): the first pronoun
  becomes the role ("my ex's gym hours");
- nobody is named and no other actor is stored: the memory is left out of this prompt. The
  stored memory is never changed, and every other turn shows it as before.

A human whose forms are they/them is never matched (the pronoun is no evidence of anyone).
Pure: no I/O, no logging."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace

_ROLES = (r"ex[- ]husband|ex[- ]wife|ex[- ]boyfriend|ex[- ]girlfriend|ex|husband|wife|boyfriend"
          r"|girlfriend|partner|fianc[eé]e?|brother|sister|dad|father|mum|mom|mother|son"
          r"|daughter|boss|manager|coworker|colleague|friend|mate|flatmate|roommate|landlord"
          r"|neighbou?r|uncle|aunt|nephew|niece|cousin|grandad|grandpa|grandma|gran|nan|teacher"
          r"|client|customer|doctor|kid|toddler|baby")
_ROLE = re.compile(rf"\b(?:my|our|her|the) ((?:{_ROLES}))\b", re.IGNORECASE)
_PRONOUNS = {
    "he": (re.compile(r"\b(he|him|his|himself|he's|hes|he'd|he'll)\b", re.IGNORECASE),
           {"his": "'s", "himself": "", "he's": " is", "hes": " is", "he'd": " would",
            "he'll": " will"}),
    "she": (re.compile(r"\b(she|her|hers|herself|she's|shes|she'd|she'll)\b", re.IGNORECASE),
            {"her": "'s", "hers": "'s", "herself": "", "she's": " is", "shes": " is",
             "she'd": " would", "she'll": " will"}),
}


def _named(text: str, names: Sequence[str]) -> bool:
    low = text.casefold()
    return any(n and re.search(rf"\b{re.escape(n.casefold())}\b", low) for n in names)


def _put(text: str, rx: re.Pattern, tails: Mapping[str, str], who: str, *, count: int = 0) -> str:
    """`text` with the pronouns `rx` finds (the first `count`; 0: all) replaced by `who`, the
    possessive and contracted forms kept ("his" -> "Mara's", "he's" -> "Mara is")."""
    def swap(m: re.Match) -> str:
        return who + tails.get(m.group(1).casefold().replace("’", "'"), "")
    return rx.sub(swap, text, count=count)


def for_him(memories: Sequence, *, owner: int, hg: int, forms: tuple[str, str, str],
            his_names: Sequence[str], names: Mapping[int, str]) -> list:
    """`memories` (MemoryRow) as a private turn between `owner` and him (`hg`) may show them
    (the module docstring): unchanged, with a third party named, or left out."""
    found = _PRONOUNS.get((forms[0] or "").casefold())
    if found is None or not hg:
        return list(memories)
    rx, tails = found
    out = []
    for m in memories:
        first = rx.search(m.text)
        if first is None:
            out.append(m)
            continue
        actors = tuple(getattr(m, "actors", ()) or ())
        if hg in actors or getattr(m, "scope_with", 0) == hg or _named(m.text, his_names):
            out.append(m)  # about him
            continue
        role = _ROLE.search(m.text)
        if role is not None and role.start() < first.start():
            out.append(m)  # "my ex kept his key": it says whose
            continue
        others = [names[g] for g in actors if g not in (owner, hg) and names.get(g)]
        if others:
            out.append(replace(m, text=_put(m.text, rx, tails, others[0])))
        elif role is not None:
            out.append(replace(m, text=_put(m.text, rx, tails, "my " + role.group(1).lower(),
                                            count=1)))
        # else: nobody it could mean but him, and it is not about him: not in this prompt
    return out
