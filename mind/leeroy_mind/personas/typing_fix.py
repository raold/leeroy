"""Typing data the wrong way round, and old seed names (rehearsal 1, 2026-10-30).

`typing.abbreviations` maps each full form to the shorter chat form ({"you": "u", "laughing
out loud": "lol"}: schema.ABBREVIATIONS_DOC), but most stored personas have it backwards
({"lol": "laughing out loud"}), so the styler spelled "lol" out in full. fix_abbreviations
turns each entry the right way round, or drops it when neither way round is an abbreviation;
fix_emoticons keeps plain-text emoticons only (the 3.3.5 client shows no emoji). Generation
and seeding run the same repair (repair_typing) on every new record.

seed_names reads the old sketch names from the seed logs, and plan_fix_typing replaces any
left in a persona's free text with the member's current character name: the seed's own rename
matched case-sensitively, so a lower-case typist's "quillo" stayed. Name fields and bit ids are
never touched."""
import functools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from ..human import HumanIdentity
from ..wow import RACES
from .schema import Persona, PersonaError, persona_from_dict, persona_to_dict, validate_persona
from .screens import touches_real_name

# Chat forms that spell a sound rather than letters of the words, by full form.
SOUND_FORMS = {
    "you": {"u", "ya"}, "are": {"r"}, "your": {"ur", "yr"}, "you're": {"ur"}, "you are": {"ur"},
    "why": {"y"}, "ok": {"k", "kk"}, "okay": {"k", "kk"}, "thanks": {"thx", "thnx"},
    "thank you": {"thx", "ty"}, "please": {"plz", "pls"}, "see you": {"cya"}, "see ya": {"cya"},
    "because": {"cuz", "coz", "bc"}, "isn't it": {"innit"}, "later": {"l8r"}, "great": {"gr8"},
    "before": {"b4"}, "tonight": {"2nite", "tonite"}, "today": {"2day"},
    "tomorrow": {"2moro", "tmrw"}, "i am": {"im"}, "for": {"4"}, "to": {"2"}, "too": {"2"},
}
_WORD = re.compile(r"[a-z0-9]+")


class Change(NamedTuple):
    kind: str   # "abbreviation turned round" / "dropped" / "cleaned", "emoticon dropped" /
    text: str   # "cleaned", "seed name"; text is ASCII, for the console


def _plain(s: str) -> str:
    """s with everything but printable ASCII removed and its spaces collapsed."""
    return " ".join("".join(c for c in s if c.isascii() and c.isprintable()).split())


def _subsequence(part: str, word: str) -> bool:
    it = iter(word)
    return all(c in it for c in part)


def _pieces(letters: str, words: tuple[str, ...]) -> bool:
    """letters splits into one piece per word it uses, in order: each piece starts with its
    word's first letter and takes the rest from that word in order. The first word is always
    used; later ones may be left out."""
    @functools.cache
    def match(i: int, w: int) -> bool:
        if i == len(letters):
            return w > 0
        if w == len(words):
            return False
        if w > 0 and match(i, w + 1):
            return True
        word = words[w]
        if letters[i] != word[0]:
            return False
        return any(_subsequence(letters[i + 1:j], word[1:]) and match(j, w + 1)
                   for j in range(i + 1, len(letters) + 1))
    return match(0, 0)


def is_chat_form(full: str, chat: str) -> bool:
    """`chat` is a shorter way to type `full`: a sound-alike (SOUND_FORMS) or an abbreviation
    that takes a piece of each word it uses, every piece starting where its word starts
    ("laughing out loud" -> "lol", "nevermind" -> "nvm", "critical strike" -> "crit"). One
    whole word of a phrase is not an abbreviation of it ("fire of doom" -> "fire")."""
    f, c = " ".join(full.casefold().split()), " ".join(chat.casefold().split())
    if not c or len(c) >= len(f):
        return False
    if c in SOUND_FORMS.get(f, ()):
        return True
    words, letters = tuple(_WORD.findall(f)), "".join(_WORD.findall(c))
    if not words or not letters or (len(words) > 1 and letters in words):
        return False
    return _pieces(letters, words)


def fix_abbreviations(abbreviations: Mapping[str, str]) -> tuple[dict[str, str], list[Change]]:
    """Each entry as full form -> chat form: kept, turned round, cleaned of anything but plain
    ASCII, or dropped when neither way round is an abbreviation, when nothing plain is left, or
    when an earlier entry already has its full form. One Change per entry that is not kept as
    it was."""
    out: dict[str, str] = {}
    changes: list[Change] = []
    for key, value in abbreviations.items():
        was = f"abbreviation {ascii(key)} -> {ascii(value)}"
        a, b = _plain(key), _plain(value)
        if not a or not b:
            changes.append(Change("abbreviation dropped", f"{was}: dropped, not plain text"))
            continue
        if is_chat_form(a, b):
            full, chat, turned = a, b, False
        elif is_chat_form(b, a):
            full, chat, turned = b, a, True
        else:
            why = "no change" if a.casefold() == b.casefold() else "not an abbreviation"
            changes.append(Change("abbreviation dropped", f"{was}: dropped, {why}"))
            continue
        if any(full.casefold() == k.casefold() for k in out):
            changes.append(Change("abbreviation dropped",
                                  f"{was}: dropped, {ascii(full)} already has one"))
            continue
        out[full] = chat
        if turned:
            changes.append(Change("abbreviation turned round",
                                  f"{was}: turned round to {ascii(full)} -> {ascii(chat)}"))
        elif (full, chat) != (key, value):
            changes.append(Change("abbreviation cleaned",
                                  f"{was}: now {ascii(full)} -> {ascii(chat)}"))
    return out, changes


def fix_emoticons(emoticons: Iterable[str]) -> tuple[list[str], list[Change]]:
    """Plain-text emoticons only, each once."""
    out: list[str] = []
    changes: list[Change] = []
    for e in emoticons:
        p = _plain(e)
        if not p:
            changes.append(Change("emoticon dropped", f"emoticon {ascii(e)}: dropped, not plain "
                                                      "text"))
        elif p in out:
            changes.append(Change("emoticon dropped", f"emoticon {ascii(e)}: dropped, a repeat"))
        else:
            if p != e:
                changes.append(Change("emoticon cleaned", f"emoticon {ascii(e)}: now {ascii(p)}"))
            out.append(p)
    return out, changes


def repair_typing(typing) -> list[Change]:
    """Fix a raw record's typing in place: abbreviations and emoticons as above. Anything not
    shaped like the schema is left for persona_from_dict to report."""
    if not isinstance(typing, dict):
        return []
    changes: list[Change] = []
    abbr = typing.get("abbreviations")
    if isinstance(abbr, dict) and all(isinstance(k, str) and isinstance(v, str)
                                      for k, v in abbr.items()):
        typing["abbreviations"], more = fix_abbreviations(abbr)
        changes += more
    emo = typing.get("emoticons")
    if isinstance(emo, list) and all(isinstance(e, str) for e in emo):
        typing["emoticons"], more = fix_emoticons(emo)
        changes += more
    return changes


# -- old seed names ------------------------------------------------------------------------------

_RACE_ALT = "|".join(re.escape(r) for r in sorted(RACES.values(), key=len, reverse=True))
# a row of the assignment table `personas seed` prints (seed._table)
_SEED_ROW = re.compile(rf"^\s*\d+ (?P<old>.+?)\s+(?:{_RACE_ALT}) .*?  ->  (?P<new>\S+) "
                       r"\(guid (?P<guid>\d+), level \d+")


def seed_names(log_texts: Iterable[str], cast: Mapping[int, object], *,
               human: HumanIdentity | None = None, personas: Iterable[Persona] = ()
               ) -> tuple[dict[str, str], list[str]]:
    """{old sketch name: the member's current character name} from the seed logs' tables, and a
    note for each name left out: one the logs map to more than one member, one that is also the
    name of someone in the guild (a character, the human, a member's real first name), or one
    that touches the owner's real name (never printed)."""
    found: dict[str, dict[int, str]] = {}
    for text in log_texts:
        for line in text.splitlines():
            m = _SEED_ROW.match(line)
            if m:
                found.setdefault(m["old"], {})[int(m["guid"])] = m["new"]
    taken = {getattr(m, "name", "").casefold() for m in cast.values()}
    for p in personas:
        taken |= {p.identity.real_name.casefold(), *p.identity.real_name.casefold().split()[:1]}
    if human:
        taken |= {n.casefold() for n in (human.nickname, human.character_name) if n}
    out: dict[str, str] = {}
    notes: list[str] = []
    for old, guids in sorted(found.items()):
        if human and touches_real_name(old, human):
            notes.append("a seed name was skipped: it touches the owner's real name")
            continue
        if len(guids) > 1:
            notes.append(f"seed name {ascii(old)}: skipped, the logs map it to {len(guids)} "
                         "members")
            continue
        (guid, logged), = guids.items()
        new = getattr(cast.get(guid), "name", "") or logged
        if old.casefold() == new.casefold():
            continue
        if old.casefold() in taken:
            notes.append(f"seed name {ascii(old)}: skipped, it is also the name of someone in "
                         "the guild")
            continue
        out[old] = new
    return out, notes


# never renamed: names and ids, not text about the person
_KEEP = re.compile(r"character_name|identity\.real_name|relationships\[\d+\]\.other"
                   r"|bits\[\d+\]\.id")


def cased_like(found: str, name: str) -> str:
    """name in the case the replaced word was typed in: all lower, all upper, or as spelled."""
    if found.islower():
        return name.lower()
    if found.isupper() and len(found) > 1:
        return name.upper()
    return name


def _rename_seeds(d: dict, names: Mapping[str, str]) -> tuple[dict, list[Change]]:
    if not names:
        return d, []
    lookup = {k.casefold(): v for k, v in names.items()}
    pat = re.compile(r"(?<!\w)(?:" + "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)
    changes: list[Change] = []

    def swap(v, path: str):
        if isinstance(v, str):
            if _KEEP.fullmatch(path):
                return v

            def one(m) -> str:
                new = cased_like(m.group(0), lookup[m.group(0).casefold()])
                changes.append(Change("seed name", f"seed name {ascii(m.group(0))} -> "
                                                   f"{ascii(new)} in {path}"))
                return new
            return pat.sub(one, v)
        if isinstance(v, dict):
            return {k: swap(x, f"{path}.{k}" if path else k) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x, f"{path}[{i}]") for i, x in enumerate(v)]
        return v
    return swap(d, ""), changes


@dataclass
class TypingFix:
    guid: int
    name: str
    changes: list[Change]
    persona: Persona | None  # the repaired persona, None when it cannot be built
    errors: list[str]        # why the repaired persona cannot be saved


def plan_fix_typing(personas: Iterable[Persona], names: Mapping[str, str]) -> list[TypingFix]:
    """A TypingFix for each persona with something to fix, by guid: typing (repair_typing) and
    old seed names in free text (`names`: seed_names). Writes nothing."""
    out = []
    for p in sorted(personas, key=lambda p: p.guid):
        d = persona_to_dict(p)
        changes = repair_typing(d["typing"])
        d, renamed = _rename_seeds(d, names)
        changes += renamed
        if not changes:
            continue
        try:
            fixed = persona_from_dict(d)
            errors = validate_persona(fixed)
        except PersonaError as e:
            fixed, errors = None, e.errors
        out.append(TypingFix(p.guid, p.character_name, changes, fixed, errors))
    return out
