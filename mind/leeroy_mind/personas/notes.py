"""The women's notes toward the human against their attitude (fix wave G2; dress rehearsal 2,
judge C). The notes were written before the owner's attitude mix of 2026-10-30, so 19 of them
still said "no romantic interest" for women whose attitude is fond, drawn or lust, and her
prompt printed her attitude next to a note that denied it. A note that denies the attitude is
a doctor FAIL for a lust or drawn woman and a note for a fond one, whose warmth need not be
romantic."""
import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..human import HumanIdentity
from .schema import Persona
from .seed import human_ref
from .stances import is_woman

ATTITUDES = ("lust", "drawn", "fond")
FAILS = ("lust", "drawn")  # a fond woman's denial is a note
# "nothing more" only as a limit ("Nothing more.", "nothing more than friends"), never as the
# intensifier "wants nothing more than to get him alone"; a distance kept from the others and
# "not interested in anyone else" are no denial of him.
_COLD = (r"no(?:t|thing)?\s+romantic\w*", r"non-?romantic\w*", r"no\s+romance",
         r"nothing\s+more(?=\s*(?:[,.;!]|$)|\s+than\s+(?:a\s+|just\s+)?(?:friend|guildmate"
         r"|colleague|raid\s+buddies|buddies|respect|professional|that))",
         r"professional\s+distance", r"(?:strictly|purely|just)\s+professional",
         r"keeps?\s+(?:it|things|interactions|everything)\s+professional",
         r"relationship\s+as\s+professional", r"emotional\s+distance",
         r"keeps?\s+(?:her|a|some)\s+distance(?!\s+from\s+(?:the\s+others|others|everyone"
         r"|the\s+rest|the\s+guild|them))",
         r"(?<!not\s)(?<!than\s)(?:just|only)\s+friends",
         r"not\s+(?:interested|attracted)(?=\s*(?:[,.;!]|$)|\s+(?:in|to)\s+(?:him|romance"
         r"|dating|anything\s+more)(?!\w))",
         r"no\s+(?:interest|feelings|attraction)\s+(?:in|for)\s+him")
COLD_NOTE = re.compile(r"(?<!\w)(?:" + "|".join(_COLD) + r")(?!\w)", re.IGNORECASE)


def denies(note: str) -> str | None:
    """The words in a note toward him that deny a woman's interest ("no romantic", "nothing
    more", "professional distance" and the like), lower case; None when there are none."""
    m = COLD_NOTE.search(" ".join(note.split()))
    return m.group(0).casefold() if m else None


@dataclass(frozen=True)
class ColdNote:
    guid: int
    name: str
    stance: str
    where: str  # "row": her relationship row's notes; "persona": her declaration toward him
    phrase: str


def cold_notes(store, personas: Iterable[Persona], human: HumanIdentity) -> list[ColdNote]:
    """Every denial in the notes of the women with an attitude toward the human, by guid: her
    relationship row's notes, then each of her persona's declarations toward him (the one
    `personas relink-human` copies to the row). Reads only."""
    toward = human_ref(human, {m.name: m.guid for m in store.cast().values()})
    out = []
    for p in sorted(personas, key=lambda p: p.guid):
        if not is_woman(p) or p.romance.stance not in ATTITUDES:
            continue
        if phrase := denies(store.get_relationship(p.guid, human.guid).notes):
            out.append(ColdNote(p.guid, p.character_name, p.romance.stance, "row", phrase))
        for r in p.relationships:
            if toward(r.other) and (phrase := denies(r.note)):
                out.append(ColdNote(p.guid, p.character_name, p.romance.stance, "persona",
                                    phrase))
                break
    return out


def doctor_lines(found: list[ColdNote], called: str, women: int) -> list[tuple[bool | None, str]]:
    """The doctor's lines: one FAIL listing the lust and drawn women whose notes deny their
    attitude and one note for the fond women, or one ok. Each woman once, with the first
    denial found and where it is; never the note itself."""
    by: dict[int, list[ColdNote]] = {}
    for c in found:
        by.setdefault(c.guid, []).append(c)

    def who(cs: list[ColdNote]) -> str:
        c = cs[0]
        return (f"{c.stance} {c.name} ({c.guid}): \"{c.phrase}\" "
                f"({', '.join(dict.fromkeys(x.where for x in cs))})")
    fails = [who(cs) for cs in by.values() if cs[0].stance in FAILS]
    fond = [who(cs) for cs in by.values() if cs[0].stance not in FAILS]
    out: list[tuple[bool | None, str]] = []
    if fails:
        out.append((False, f"notes toward {called} deny the attitude of {len(fails)} women "
                           f"(fix them in both places): {'; '.join(fails)}"))
    if fond:
        out.append((None, f"notes toward {called} keep {len(fond)} fond women at a distance: "
                          f"{'; '.join(fond)}"))
    if not out:
        out.append((True, f"notes toward {called} fit every woman's attitude ({women} with "
                          "one)"))
    return out
