"""Item links in the guild's lines: the owner clicks a member's [Shadowmourne] and sees the item,
as he would a player's link.

canonical() runs on a turn's words before the member's typing styles them, and rewrites each
item mention as the item's own name in brackets; style keeps a bracketed name whole (no typo,
case or abbreviation inside it). That plain form is what the filters, the chat log, the
utterance log, the voice service and every later prompt see. Only the say command to the bridge
carries a mention as {{item:<id>}} (wire), and native turns that into the client's link from its
own item template: its name and quality colour, at most two a line, within 255 bytes
(modules/mod-leeroy/src/Lib/LeeroyItemLinks.h). Its echo comes back as that link, which
strip_wow_markup reads as the same [Name].

A mention is a bracketed name ("[shadowmourne]", any case), or unbracketed the exact whole name
of an item of two words or more besides little words ("the", "of"), at least one of them no
everyday word (COMMON_WORDS, CHAT_WORDS), and at least MIN_FREE_CHARS long: "deathbringer's
will" and "frozen orb" link; "the key", "gold bar", "mana potion", "heavy boots", "dark desire"
and a one-word name such as "hearthstone" only in brackets. Bracketed mentions take the MAX_LINKS slots first, then unbracketed ones, left to
right. A bracketed span that names no item, or one past the slots, loses its brackets: in WoW
chat a bracket reads as a link, and a dead one would mislead. A junk name (Deprecated, Monster -,
[PH]: the library's own rule), a boss's, a raid's or a dungeon's name, a class or a race is no
item here, bracketed or not.

Several items of one name (a heroic copy, a quest copy): one that dropped or was won within
SEEN_S (seen(), from the engine's loot events: the copy the raid is talking about) wins, else
one the speaker's level can use, then the highest item level, then the highest quality, then
the lowest id. With no item table (no library.db, or no world build in it) nothing is linked
and the words keep their brackets. An id is only ever one the item table holds. {{ the words
carried themselves is defused (defuse), so no placeholder ever comes from the model.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from leeroy_mind.library.world import _JUNK_NAME
from leeroy_mind.protocol import MAX_TEXT_BYTES
from leeroy_mind.talk.words import COMMON_WORDS
from leeroy_mind.wow import (BOSS_NAMES, CLASSES, DUNGEON_BOSS_NAMES, DUNGEONS, ENCOUNTER_MEMBERS,
                             RACES, RAID_INSTANCES)

log = logging.getLogger(__name__)

MAX_LINKS = 2  # native's kMaxItemLinks
SEEN_S = 6 * 3600.0
SEEN_KEPT = 256
MIN_FREE_CHARS = 8
# A link's bytes besides its id, level and name: "|c" + 8 hex + "|Hitem:" + ":0" x 7 + ":" +
# "|h[" + "]|h|r" (LeeroyItemLinks.cpp BuildItemLink).
LINK_OVERHEAD = 40

_BRACKET = re.compile(r"\[([^\[\]\n]{1,100})\]")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
_OPEN = re.compile(r"\{\{+")
_LITTLE = frozenset("a an the of and or to in on at for with from by".split())
# Words players type as words that also make item names: the game's own ("arena points",
# "heroic armor"), gear and its stuff ("heavy boots", "leather gloves"), and the plain and
# fond words a line may hold ("dark desire", "sweet surprise", "eternal life"). A name of only
# these and COMMON_WORDS links in brackets only.
CHAT_WORDS = frozenset("""
arena heroic raid raids guild gold elite rare epic pet pets mount mounts gear loot drop drops
points tank heal heals dps boss bosses quest daily mana dungeon run runs farm trade bag bags
boots gloves bracers leggings cloak gauntlets girdle mantle helm breastplate pants pauldrons
robe robes legguards spaulders tunic shield greaves cape legplates armor shoulders shoulderpads
buckler vest dagger wand sash hammer chestguard bindings helmet wristguards pendant
shoulderguards sword sabatons handguards crystal crown essence fang bands mace chestpiece cord
claw claws signet necklace letter cowl cuffs crossbow trousers handwraps totem skull tome
circlet tabard scepter treads talisman slippers sandals shard elixir amulet idol shroud torch
armguards rune vambraces drape cleaver talon scroll waistband wraps medallion hauberk scale
collar bracelets grips choker headpiece journal relic headdress parchment potion longbow charm
shortsword maul kilt ore tablet jerkin rifle shoes sigil faceguard waistguard insignia feather
mask crate cinch fragment banner chestplate vestments footwraps stave stompers ink arrow tooth
spear crest fists harness bundle satchel powder sphere mitts footpads coif vial mushroom
glaive shackles leather cloth plate mail silk wool iron steel bronze copper silver
simple heavy broken shadow arcane shimmering primal ancient royal bright engraved ornate rigid
feathered elemental eternal twilight righteous regal desire surprise sweet delight passion
pleasure embrace secret darling beauty kisses console wrath vengeance faith judgement
redemption absolution ritual tempest
""".split())
_NOT_ITEMS = frozenset(" ".join(n.replace("’", "'").casefold().split()) for n in (
    *BOSS_NAMES.values(), *DUNGEON_BOSS_NAMES.values(), *RAID_INSTANCES.values(),
    *DUNGEONS.values(), *ENCOUNTER_MEMBERS, *(m for ms in ENCOUNTER_MEMBERS.values() for m in ms),
    *CLASSES.values(), *RACES.values()))


def key(name: str) -> str:
    """A name as names are compared: casefolded, ’ as ', single spaces, none at either end."""
    return " ".join(name.replace("’", "'").casefold().split())


def defuse(text: str) -> str:
    """No placeholder from the words themselves: every run of "{" is one "{"."""
    return _OPEN.sub("{", text)


@dataclass(frozen=True, slots=True)
class ItemRef:
    id: int
    name: str
    quality: int
    ilvl: int
    req_level: int


def free_name(k: str) -> bool:
    """A name that links unbracketed (the module docstring): two words or more besides little
    words, one of them no everyday chat word, at least MIN_FREE_CHARS, a word at either end."""
    if len(k) < MIN_FREE_CHARS or not (k[0].isalnum() and k[-1].isalnum()):
        return False
    words = [w for w in _WORD.findall(k) if w not in _LITTLE]
    return len(words) >= 2 and any(w not in COMMON_WORDS and w not in CHAT_WORDS for w in words)


def link_bytes(ref: ItemRef, level: int) -> int:
    """The bytes native's link for `ref` takes in the line."""
    return LINK_OVERHEAD + len(str(ref.id)) + len(str(level)) + len(ref.name.encode("utf-8"))


class ItemLinker:
    """The item table's names, read once on first use from `load` (rows with id, name, quality,
    ilvl and req_level, as Library.item_rows gives them). `now` is the engine's clock."""

    def __init__(self, load: Callable[[], Iterable], now: Callable[[], float]):
        self._load, self._now = load, now
        self._names: dict[str, list[ItemRef]] | None = None
        self._free: dict[str, tuple[int, ...]] = {}  # a free name's first word -> its word counts
        self._seen: dict[int, float] = {}

    @classmethod
    def for_library(cls, library, now: Callable[[], float]) -> ItemLinker | None:
        rows = getattr(library, "item_rows", None)
        return cls(rows, now) if callable(rows) else None

    # -- the names -------------------------------------------------------------------------------

    def _index(self) -> dict[str, list[ItemRef]]:
        if self._names is not None:
            return self._names
        names: dict[str, list[ItemRef]] = {}
        try:
            rows = list(self._load())
        except Exception as e:  # a library without the table, or a broken one: no links
            log.warning("item_links_unavailable: %s", type(e).__name__)
            rows = []
        for r in rows:
            name = " ".join(str(r["name"]).split())
            k = key(name)
            if not k or "[" in k or "]" in k or "|" in k or k in _NOT_ITEMS or _JUNK_NAME.search(name):
                continue
            names.setdefault(k, []).append(ItemRef(int(r["id"]), name, int(r["quality"]),
                                                   int(r["ilvl"]), int(r["req_level"])))
        free: dict[str, set[int]] = {}
        for k in names:
            if free_name(k):
                words = _WORD.findall(k)
                free.setdefault(words[0], set()).add(len(words))
        self._names = names
        self._free = {w: tuple(sorted(n, reverse=True)) for w, n in free.items()}
        log.info("item_links_ready: names=%d free=%d", len(names), sum(map(len, free.values())))
        return names

    def ready(self) -> bool:
        return bool(self._index())

    def seen(self, item_ids: Iterable[int], now: float | None = None) -> None:
        """Items that just dropped or were won: their copy of a shared name wins for SEEN_S."""
        at = self._now() if now is None else now
        for i in item_ids:
            self._seen.pop(int(i), None)
            self._seen[int(i)] = at
        while len(self._seen) > SEEN_KEPT:
            del self._seen[next(iter(self._seen))]

    def resolve(self, name: str, level: int) -> ItemRef | None:
        """The item `name` names (any case), by the module docstring's rule; None for none."""
        refs = self._index().get(key(name))
        if not refs:
            return None
        if len(refs) == 1:
            return refs[0]
        now = self._now()
        pool = ([r for r in refs if now - self._seen.get(r.id, -math.inf) <= SEEN_S]
                or [r for r in refs if r.req_level <= level] or refs)
        return max(pool, key=lambda r: (r.ilvl, r.quality, -r.id))

    # -- the line --------------------------------------------------------------------------------

    def canonical(self, text: str, level: int) -> str:
        """`text` with its item mentions as "[Name]" (the item table's name), at most MAX_LINKS;
        other bracketed spans without their brackets. Unchanged but defused with no item table."""
        text = defuse(text)
        if not self.ready():
            return text
        edits: list[tuple[int, int, str]] = []
        links = 0
        for m in _BRACKET.finditer(text):
            ref = self.resolve(m.group(1), level) if links < MAX_LINKS else None
            if ref is not None:
                links += 1
            edits.append((m.start(), m.end(), f"[{ref.name}]" if ref else m.group(1).strip()))
        if links < MAX_LINKS and self._free:
            edits += self._free_mentions(text, [(a, b) for a, b, _ in edits], level,
                                         MAX_LINKS - links)
        out, last = [], 0
        for a, b, rep in sorted(edits):
            out += [text[last:a], rep]
            last = b
        out.append(text[last:])
        return "".join(out)

    def _free_mentions(self, text: str, taken: list[tuple[int, int]], level: int,
                       slots: int) -> list[tuple[int, int, str]]:
        """Unbracketed whole names outside `taken`, longest first at each word, left to right."""
        words = [m for m in _WORD.finditer(text)
                 if not any(a <= m.start() < b for a, b in taken)]
        found: list[tuple[int, int, str]] = []
        i = 0
        while i < len(words) and len(found) < slots:
            for n in self._free.get(key(words[i].group(0)), ()):
                j = i + n - 1
                if j >= len(words):
                    continue
                a, b = words[i].start(), words[j].end()
                if any(a < y and x < b for x, y in taken):
                    continue
                k = key(text[a:b])
                if free_name(k) and (ref := self.resolve(k, level)) is not None:
                    found.append((a, b, f"[{ref.name}]"))
                    i = j
                    break
            i += 1
        return found

    def wire(self, line: str, level: int) -> str:
        """The say command's text: each bracketed item name as {{item:<id>}}, at most MAX_LINKS,
        and only as many as native's links leave the line within MAX_TEXT_BYTES; any other
        bracket stays as it is."""
        line = defuse(line)
        if not self.ready():
            return line
        chosen: list[tuple[re.Match, ItemRef]] = []
        for m in _BRACKET.finditer(line):
            if len(chosen) == MAX_LINKS:
                break
            if (ref := self.resolve(m.group(1), level)) is not None:
                chosen.append((m, ref))
        size = len(line.encode("utf-8"))
        while chosen and size + sum(link_bytes(ref, level) - len(m.group(0).encode("utf-8"))
                                    for m, ref in chosen) > MAX_TEXT_BYTES:
            chosen.pop()
        out, last = [], 0
        for m, ref in chosen:
            out += [line[last:m.start()], "{{item:%d}}" % ref.id]
            last = m.end()
        out.append(line[last:])
        return "".join(out)
