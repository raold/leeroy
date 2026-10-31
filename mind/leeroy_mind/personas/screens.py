"""Cheap checks on a persona record before it is accepted: the owner's real name must never
appear in it, and romance aimed at another member (rather than the owner's character) is
refused. A ValueError makes chat_json ask again."""
import re
from collections.abc import Iterable

from ..human import HumanIdentity

ROMANCE_RE = re.compile(
    r"(?<!\w)(?:crush(?:es|ed|ing)?\s+on|a\s+crush|flirt(?!\w*\s+with\s+(?:disaster|danger))\w*"
    r"|in\s+love|dating(?!\s+back)|girlfriend|boyfriend|ex-wife|ex-husband|slept\s+with"
    r"|kiss(?!\w*\s+up)(?!(?:es)?\s+of\s+death)\w*|hook(?:ed|ing|s)?\s+up|fling"
    r"|romanc\w*|(?:his|her|their|secret|ex)[\s-]+lover"
    r"|fancies(?!\s+(?:him|her|them)sel(?:f|ves))|fancy\s+(?:him|her)"
    r"|(?:has|have|had|got)\s+a\s+thing\s+for|snog\w*|married\s+to|a\s+date\s+with"
    r"|(?:her|his|their)\s+ex(?![\w-])"
    r"(?!\s+(?:officer|guild\w*|raid\w*|tank|healer|main|leader|member)))(?!\w)",
    re.IGNORECASE)


def has_word(text: str, name: str, case_sensitive: bool = False) -> bool:
    return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text,
                                    0 if case_sensitive else re.IGNORECASE) is not None


def name_keys(name: str) -> set[str]:
    """The full name and the first token, as the card shows it, case-folded."""
    full = " ".join(name.split()).casefold()
    return {k for k in (full, full.split(" ")[0] if full else "") if k}


NAME_FIELDS = r"identity\.real_name|relationships\[\d+\]\.other"  # names, not text
_NAME_FIELD = re.compile(NAME_FIELDS)


def touches_real_name(name: str, human: HumanIdentity) -> bool:
    """`name` shares a word with the configured real name, either way round and in any case,
    so logs and reports must not print it: with a two-word real name, a first name alone is
    already half of it."""
    real = human.real_name
    return bool(real and name) and (any(has_word(name, w) for w in real.split())
                                    or any(has_word(real, w) for w in name.split()))


def _strings(v, path=""):
    if isinstance(v, str):
        yield path, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _strings(k, f"{path}.{k}" if path else str(k))
            yield from _strings(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            yield from _strings(x, f"{path}[{i}]")


def _real_name_matches(text: str, human: HumanIdentity):
    """Whole-word matches of the configured real name that do not start with a lower-case
    letter, whatever casing the config uses (a name in a script without case always counts).
    The persona screens use this: a persona model is never given the name, so a lower-case hit
    there can only be the ordinary word it also is."""
    if not human.real_name:
        return []
    pat = re.compile(rf"(?<!\w){re.escape(human.real_name)}(?!\w)", re.IGNORECASE)
    return [m for m in pat.finditer(text) if not m.group(0)[:1].islower()]


def mentions_real_name(path: str, text: str, human: HumanIdentity) -> bool:
    """Free text: see _real_name_matches. Name fields: any case."""
    if _NAME_FIELD.fullmatch(path):
        return has_word(text, human.real_name)
    return bool(_real_name_matches(text, human))


def _real_name_use(human: HumanIdentity) -> re.Pattern | None:
    """The real name as redaction and the memory screen see it: its configured spelling, its
    Title form, its UPPER form and the configured spelling with only its first letter raised
    ("Name", "NAME"; "O'neil" for o'neil, which is what keep_case writes), as whole words; never
    the all-lowercase form, unless the name has no cased letters at all. None when no real name
    is configured.

    The real name may also be an ordinary word, and chat is typed in lower case, so matching
    lower case too would mangle ordinary text in prompts, output, memories and the seed
    context (controller ruling, Task 16 fix round 2). The engine keeps the name capitalised
    in a close member's styled line (style.apply_style keep_case), so a stored line shows the
    name in a form this catches. The residual case is accepted: a model that writes the name
    itself in lower case is not caught."""
    name = human.real_name
    forms = {name, name.title(), name.upper(), name[:1].upper() + name[1:]} - {""}
    if name.lower() != name.upper():  # it has case, so its lower-case form is the ordinary word
        forms.discard(name.lower())
    forms = sorted(forms, key=len, reverse=True)
    if not forms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(f) for f in forms) + r")(?!\w)")


def names_real_name(text: str, human: HumanIdentity) -> bool:
    """The text uses the real name in a form _real_name_use takes."""
    pat = _real_name_use(human)
    return pat is not None and pat.search(text) is not None


def redact_real_name(text: str, human: HumanIdentity, replacement: str) -> str:
    """Each use of the real name that _real_name_use finds becomes `replacement`; the
    all-lowercase word is left alone."""
    pat = _real_name_use(human)
    return text if pat is None else pat.sub(lambda m: replacement, text)


def real_name_error(path: str, human: HumanIdentity) -> str:
    segs = [("<key>" if has_word(s, human.real_name) else s) for s in path.split(".")]
    _, them, their = human.forms
    return (f"field {'.'.join(segs)} mentions the guild master's real name; refer to {them} by "
            f"{their} nickname")


def check_no_real_name(data: dict, human: HumanIdentity) -> None:
    if not human.real_name:
        return
    for path, text in _strings(data):
        if mentions_real_name(path, text, human):
            raise ValueError(real_name_error(path, human))


def human_aliases(human: HumanIdentity) -> set[str]:
    return {n.casefold() for n in (human.nickname, human.real_name, human.character_name) if n}


GM_ALIASES = frozenset({"gm", "the gm", "guild master", "the guild master"})


def refers_to_human(name, human: HumanIdentity) -> bool:
    """A relationship target that means the human: "@human", his nickname, character name or
    real name in any case, or, when he is the guild master, "GM", "the GM" or "guild master"."""
    n = " ".join(str(name).split()).casefold()
    return n == "@human" or n in human_aliases(human) or (human.is_guild_master
                                                          and n in GM_ALIASES)


OBJECT_WINDOW = 40  # a member named this close after a romance term is its object


def _dated(name: str) -> re.Pattern:
    return re.compile(rf"(?<!\w)(?:dated|married)\s+{re.escape(name)}(?!\w)", re.IGNORECASE)


def _alternation(names: Iterable[str], flags: int = 0) -> re.Pattern | None:
    names = sorted({n for n in names if n}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)",
                      flags) if names else None


class _Members:
    """The member names a romance screen watches for: `exact` as written (a real first name is
    often an ordinary word too) and `characters` in any case (a lower-case typist's "arkon" is
    still Arkon)."""

    def __init__(self, exact: Iterable[str], characters: Iterable[str] = ()):
        self.names = [n for n in (*exact, *characters) if n]
        self._pats = [p for p in (_alternation(exact), _alternation(characters, re.IGNORECASE))
                      if p is not None]

    def search(self, text: str, pos: int = 0) -> re.Match | None:
        """The first member named at or after pos."""
        hits = [m for p in self._pats if (m := p.search(text, pos))]
        return min(hits, key=lambda m: m.start(), default=None)

    def dated(self, text: str) -> bool:
        return any(_dated(n).search(text) for n in self.names)


def _human_at(text: str, human: HumanIdentity, pos: int = 0, any_case: bool = False
              ) -> re.Match | None:
    """Where the text first names the owner's character at or after pos (names_human), or
    None. any_case: typed chat, where his names come in lower case too."""
    exact = ["@human", human.nickname, human.character_name]
    loose = ["the GM", "guild master"] if human.is_guild_master else []
    if human.is_guild_master:
        (loose if any_case else exact).append("GM")
    pats = [_alternation(exact, re.IGNORECASE if any_case else 0),
            _alternation(loose, re.IGNORECASE)]
    hits = [m for p in pats if p is not None and (m := p.search(text, pos))]
    return min(hits, key=lambda m: m.start(), default=None)


def names_human(text: str, human: HumanIdentity, any_case: bool = False) -> bool:
    """The text names the owner's character: @human, or his nickname or character name as
    written (case-sensitive, so a lowercase word the nickname equals does not count). When he
    is the guild master, "GM" (as written), "the GM" and "guild master" (any case) count too.
    any_case: all of them in any case (typed chat)."""
    return _human_at(text, human, 0, any_case) is not None


# between his name and a member's: they are both objects ("with <him> and with <member>")
_COORDINATED = re.compile(r"\s*(?:,|and|or|&|plus)(?:\s+(?:also|even|then))?"
                          r"(?:\s+(?:with|on|at|to))?\s*", re.IGNORECASE)


def _aims_at_member(text: str, members: _Members, human: HumanIdentity,
                    any_case: bool = False) -> bool:
    """The object of a romance term is the first person named after it: a member named within
    OBJECT_WINDOW characters after it, unless the human is named before that member and the
    member is not joined to him as a second object ("flirts with <him> but <member> does too"
    is his; "flirts with <him> and with <member>" is not). A member right after
    "dated"/"married" is the object too."""
    for m in ROMANCE_RE.finditer(text):
        hit = members.search(text, m.end())
        if hit is None or hit.start() - m.end() > OBJECT_WINDOW:
            continue
        him = _human_at(text, human, m.end(), any_case)
        if (him is not None and him.start() < hit.start()
                and not _COORDINATED.fullmatch(text, him.end(), hit.start())):
            continue
        return True
    return members.dated(text)


def _member_romance(text: str, members: _Members, human: HumanIdentity,
                    any_case: bool = False) -> bool:
    """Romance in a text that names a member: refused unless the text also names the human
    (rivalry over him) and no member is the object of the romance."""
    return (not names_human(text, human, any_case)
            or _aims_at_member(text, members, human, any_case))


_NOT_FREE_TEXT = re.compile(rf"character_name|{NAME_FIELDS}|relationships\[\d+\]\..*"
                            r"|bits\[\d+\]\.id")
LONG_TEXT = 300  # longer than any field but the mock interview
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def _free_text(v, path=""):
    """(path, text) for every string value in a record, keys and names left out."""
    if isinstance(v, str):
        if not _NOT_FREE_TEXT.fullmatch(path):
            yield path, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _free_text(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            yield from _free_text(x, f"{path}[{i}]")


def check_romance(data: dict, human: HumanIdentity, member_names: Iterable[str], *,
                  characters: Iterable[str] = ()) -> None:
    """Romance may aim at the owner's character only. Relationship notes about anyone but
    @human may not use romance terms; no other text in the record may use them alongside a
    member's name. Either is allowed when it also names the human and no member is the object
    of the romance (_aims_at_member), so "jealous when <member> flirts with <him>" passes and
    "flirts with <member> to make <him> jealous" does not. member_names match as written,
    characters (the cast's character names) in any case, and in sample lines, typed chat, his
    names count in any case too."""
    aliases = human_aliases(human)
    names = [n for n in member_names if n and n.casefold() not in aliases]
    chars = [n for n in characters if n and n.casefold() not in aliases]
    rels = data.get("relationships")
    for r in (rels if isinstance(rels, list) else []):
        if (not isinstance(r, dict) or r.get("other") == "@human"
                or str(r.get("other", "")).casefold() in aliases):
            continue
        note = str(r.get("note", ""))
        members = _Members(names, [*chars, str(r.get("other", ""))])
        if (ROMANCE_RE.search(note) or members.dated(note)) and _member_romance(note, members,
                                                                                human):
            raise ValueError("relationship note: romance is allowed only toward the guild "
                             "master's character, not between members")
    members = _Members(names, chars)
    if not members.names:
        return
    for path, text in _free_text(data):
        typed = path.startswith("sample_lines")
        # a long text (the mock interview) is taken a sentence at a time: a member in one
        # answer and a romance word in another are not one romance
        for part in (_SENTENCES.split(text) if len(text) > LONG_TEXT else (text,)):
            if ((ROMANCE_RE.search(part) or members.dated(part)) and members.search(part)
                    and _member_romance(part, members, human, typed)):
                raise ValueError(f"{path}: romance involving another member is not allowed; "
                                 "romance may aim only at the guild master's character")
