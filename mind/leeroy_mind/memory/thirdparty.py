"""Whose "he" a recalled memory means, in a private turn with him (self-play audit,
2026-11-17: a memory of her ex-husband, "the app tracking his gym hours", reached her DM with
the owner; he is the only "he" in that prompt, so it became "your gym hours").

`for_him(memories, ...)` goes through the memories recalled for a whisper or Discord DM with him
and names the other man where there is positive evidence of one. It never leaves a memory out
(review of #368, P2-3: most memories that say "he" with no actor stored are her night's
reflections about him, and leaving them out took the romance's own material from the turns
where it matters). For a memory whose text uses his pronoun (his forms: he/him/his, or she/her):

- a game memory (source `game`: an achievement, a kill) is shown as written, always;
- about him, shown as written: he is one of its actors, it is private with him (scope_with), or
  it names him;
- the text names who it is about before the pronoun ("my ex kept his key"): as written;
- another member is its actor: the pronoun becomes that member's name;
- the text names a role only after the pronoun ("his gym hours ... my ex"): the first pronoun
  becomes the role ("my ex's gym hours");
- a memory of her own day away from the game (source `offscreen`), where he never is, when her
  persona's household has a man in it (`household_man`: an ex, a husband, a partner, a son, a
  brother, her dad): the first pronoun becomes him ("my ex-husband's gym hours");
- anything else, a night reflection or a plan that says "he" and names nobody above all, is
  shown as written: with no evidence of another man, "he" is him.

The stored memory is never changed. A human whose forms are they/them is never matched (the
pronoun is no evidence of anyone). Pure: no I/O, no logging."""
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
# the men of a household, the likeliest "he" of her own day first; and the women, for a human
# whose forms are she/her
_MEN = ("ex-husband", "ex husband", "ex-boyfriend", "ex boyfriend", "ex", "husband", "boyfriend",
        "fiance", "fiancé", "partner", "son", "brother", "dad", "father")
_WOMEN = ("ex-wife", "ex wife", "ex-girlfriend", "ex girlfriend", "wife", "girlfriend",
          "fiancee", "fiancée", "daughter", "sister", "mum", "mom", "mother")
_PRONOUNS = {
    "he": (re.compile(r"\b(he|him|his|himself|he's|hes|he'd|he'll)\b", re.IGNORECASE),
           {"his": "'s", "himself": "", "he's": " is", "hes": " is", "he'd": " would",
            "he'll": " will"}, _MEN),
    "she": (re.compile(r"\b(she|her|hers|herself|she's|shes|she'd|she'll)\b", re.IGNORECASE),
            {"her": "'s", "hers": "'s", "herself": "", "she's": " is", "shes": " is",
             "she'd": " would", "she'll": " will"}, _WOMEN),
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


def household_man(persona, people: Sequence[str] = _MEN) -> str:
    """The man her persona's household or life names ("ex-husband", "husband", "son"), the
    first of `people` her household or life thread holds; "" for none."""
    rl = getattr(persona, "real_life", None)
    if rl is None:
        return ""
    text = " ".join(str(getattr(rl, f, "") or "") for f in ("household", "life_thread")).casefold()
    for who in people:
        if re.search(rf"\b{re.escape(who)}\b", text):
            return who.replace(" ", "-") if who.startswith("ex ") else who
    return ""


def for_him(memories: Sequence, *, owner: int, hg: int, forms: tuple[str, str, str],
            his_names: Sequence[str], names: Mapping[int, str], persona=None) -> list:
    """`memories` (MemoryRow) as a private turn between `owner` and him (`hg`) may show them
    (the module docstring): each as written or with a third party named, never left out.
    `persona`: hers, for the man of her household."""
    found = _PRONOUNS.get((forms[0] or "").casefold())
    if found is None or not hg:
        return list(memories)
    rx, tails, people = found
    out = []
    for m in memories:
        first = rx.search(m.text)
        source = getattr(m, "source", "")
        actors = tuple(getattr(m, "actors", ()) or ())
        if (first is None or source == "game" or hg in actors
                or getattr(m, "scope_with", 0) == hg or _named(m.text, his_names)):
            out.append(m)  # no pronoun, the game's own words, or about him
            continue
        role = _ROLE.search(m.text)
        if role is not None and role.start() < first.start():
            out.append(m)  # "my ex kept his key": it says whose
            continue
        others = [names[g] for g in actors if g not in (owner, hg) and names.get(g)]
        man = household_man(persona, people) if source == "offscreen" else ""
        if others:
            out.append(replace(m, text=_put(m.text, rx, tails, others[0])))
        elif role is not None:
            out.append(replace(m, text=_put(m.text, rx, tails, "my " + role.group(1).lower(),
                                            count=1)))
        elif man:
            out.append(replace(m, text=_put(m.text, rx, tails, "my " + man, count=1)))
        else:
            out.append(m)  # no evidence of another man: "he" is him
    return out
