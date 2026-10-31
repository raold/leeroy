"""Facts from the guild's library (library.db) for live talk (fix wave part E, owner approved
2026-10-30): the rehearsal's guild answered his game questions wrong or not at all, and its
raid lead made boss mechanics up. His question is read in talk/questions.py.

- fact_lines: the chunks found as a compact block, one fact a line, plain ASCII with no URLs,
  wiki markup or macros, FACTS_TOKENS at most;
- relevant: the gate (fix round 1, from the controller's live probe: better no facts than
  wrong facts). A question that names something (a raid, a boss, an item, a person, a
  faction) only gets facts that name it too, the most specific of its names (a boss over the
  raid it is in, either over the zone that only locates it); one that names nothing, facts
  that share its words as whole words (two of them when he gives three or more), never only
  a generic one ("entrance", "best", "fastest", "drop");
- the guild's faction (the human's race, wow.faction_known): a quest, a place, a section or a
  loot table of the other faction is no fact (chunk_faction);
- boss_mechanics: two or three lines of a boss's real mechanics from its tactics page (for an
  encounter of several bosses without a page of its own, its members' pages), for the raid
  lead's call after a wipe or a kill;
- LibraryFacts: one lookup per human line, shared by every reply to it. The chunks his line
  names by title lead (Library.titled), then the search's hits. The search gets FACTS_CAP_S -
  TEXT_RESERVE_S of wall time to come back (its embed call is its only await), then the text
  search runs while the FACTS_CAP_S last, else there are no facts; the ranking that follows
  the embed, the title lookup and the formatting are not interrupted, so a lookup can run past
  the cap by their time.

Facts are not memories: they come only from library.db, which is public reference text, and
none of it is logged."""
import asyncio
import functools
import logging
import re
import time
from collections import OrderedDict
from typing import Callable, Sequence

from leeroy_mind.library.chunks import Chunk, split_sentences
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.names import (NEUTRAL_FACTIONS, name_level, name_pattern, sides_named,
                                    words_re)
from leeroy_mind.talk.questions import GameQuestion, game_question
from leeroy_mind.talk.style import to_ascii
from leeroy_mind.text import truncate_words
from leeroy_mind.wow import ENCOUNTER_MEMBERS

__all__ = ["FACTS_CAP_S", "FACTS_TOKENS", "GameQuestion", "LibraryFacts", "boss_mechanics",
           "chunk_faction", "clean_text", "fact_lines", "for_side", "game_question",
           "most_specific", "relevant", "salient", "title_names"]

log = logging.getLogger("leeroy_mind.talk")

FACTS_TOKENS = 150  # the fact lines of one prompt, at most (text.estimate_tokens)
FACTS_CAP_S = 0.3  # wall time for a line's lookup (see the module docstring)
TEXT_RESERVE_S = 0.1  # of the cap, kept for the text search after a slow hybrid one
SEARCH_K = 8  # candidates the search brings for the gate to choose the facts from
LINE_CHARS = 260  # one fact
MIN_LINE_CHARS = 60  # a fact shorter than this is not worth its room
FILL_CHARS = 100  # a fact with little that matches his words still says this much
KEEP_LINES = 32  # the lines whose lookups are kept for their replies
TITLE_NAMES = 120  # the names and word runs of his line looked up as titles, at most
STRICT_TOPICS = frozenset({"item", "class"})  # what's good is an opinion: relevant(strict=)
MECHANICS_LINES = 3
MECHANIC_WORDS = (6, 28)
# Words of his that never tie a fact to his question on their own (the probe: "entrance"
# brought the Nexus Vault and Dalaran's mailboxes): question and chat words, game words every
# page has, the continents, and the adjectives that only grade a thing (the re-review:
# "fastest" brought a mount to a fishing question).
GENERIC = frozenset("""entrance best good great nice need tip trick drop loot way worth start
deal work thing stuff kind sort help want wanna gonna gotta keep losing lose lost buy sell farm
learn find turn reward fight boss raid quest item gear faction reputation rep level people guy
again still really pretty much many lot tonight today now first last next new old easy hard fast
quick quickly long big mean everyone anything something usually actually going trying nah yeah
lol heroic normal player mode exactly guild time place spot area make made use using used take
give put try run running northrend azeroth outland kalimdor draenor
better worse worst bad fastest faster quickest quicker easiest easier cheapest cheaper cheap
hardest harder biggest bigger strongest stronger strong highest higher high lowest lower low
safest safer safe longest longer shortest shorter short simplest simpler simple nearest near
closest closer close newest newer oldest older latest earliest early late greatest coolest
cool top optimal ideal efficient efficiently decent solid viable proper right real main usual
common awesome amazing""".split())


# -- the facts block ---------------------------------------------------------------------------

# A clause with a URL in it goes whole ("see https://... for the map"), with its comma.
_URL_CLAUSE = re.compile(r"[,;]?[^,;.()\n]*(?:https?://|www\.)\S+[^,;.()\n]*", re.IGNORECASE)
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_WIKILINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
_TAG = re.compile(r"</?[A-Za-z][^<>]{0,80}>")
_NOTE_MARK = re.compile(r"\[(?:edit|citation needed|\d+|note \d+)\]", re.IGNORECASE)
_EMPHASIS = re.compile(r"'{2,}")
_LIST_ITEM = re.compile(r"^\s*-\s+")  # "- " at a line's start; a dash inside one is punctuation
_BOILERPLATE = re.compile(
    r"^(?:n\.?\s?b\b|note:|notes:|see (?:also|above|below|this|the|that)\b|main article\b|"
    r"for other uses\b|needs? clarification\b|this (?:section|article|page)\b|patch changes\b|"
    r"external links\b|references\b|sources?:)", re.IGNORECASE)
# A quoted joke is no fact (the probe: a raid recording's swearing, on a boss's page).
_SWEARING = re.compile(r"\b(?:fuck(?:ing|ed|er|ers|s)?|shit(?:ty|s)?|cunts?|bitch(?:es)?|"
                       r"assholes?|whores?|sluts?)\b", re.IGNORECASE)
_PARENTHESES = re.compile(r"\([^()]*\)")
# A macro or a slash command is no fact (rehearsal 2: the historian read "/tar Blood /cast (Name
# of ability) /tar Death" out as "targets blood, drops slows, targets death"). A piece is cut
# where a macro's command, "#showtooltip" or a run of slash words starts; a whole sentence before
# it stays. The lead-in before it goes ("use this macro:"), and so does the piece after it that
# only explains it ("This immediately uses ..."). A quest's single emote stays ("/cheer at it"),
# and so does a slash between words ("Revivify (3)/Life Burst (4)").
_SLASH = r"(?<![^\s\"'(\[])/"  # a command starts the line or follows a space, a quote or a bracket
_MACRO = re.compile(
    _SLASH + r"(?:cast|castsequence|castrandom|use|userandom|target|tar|targetexact|targetenemy|"
    r"targetfriend|targetlasttarget|targetparty|targetraid|cleartarget|clearfocus|focus|assist|"
    r"stopmacro|stopcasting|stopattack|startattack|petattack|petfollow|petpassive|petstay|"
    r"cancelaura|cancelform|equip|equipslot|dismount|run|script|console|click|dump|usetalents)\b"
    r"|#show(?:tooltip)?\b|\bshowtooltip\b|(?:" + _SLASH + r"[a-z]+\b.*?){2}", re.IGNORECASE)
_MACRO_WORD = re.compile(r"\bmacros?\b", re.IGNORECASE)
_EXPLAINS = re.compile(r"^(?:this|it|that|these|those|which)\b", re.IGNORECASE)
_TACTICS_TITLE = re.compile(r"\s*\((?:[^()]*\s)?tactics\)$", re.IGNORECASE)
_QUALIFIER = re.compile(r"\s*\([^()]*\)$")
# A whole sentence that states something: not a question ("The Black Knight of Westfall?").
_SENTENCE_END = re.compile(r"[.!][\"')\]]*$")
_TABLE_CELLS = 4  # a piece with this many " - " is a table row, not a fact
SAME_TITLE = 2  # facts from one page or boss: a boss's 10- and 25-player tables, not all four
# A world chunk keeps its head, then these, then the rest; a quest's story is flavour.
_WORLD_FIRST = ("Drops from", "Sold by", "Reward from", "Starts with", "Ends with", "Objective")
_STORY = " Text: "
_TERM = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = frozenset("""the and for are was were with that this from have has had you your what
when where which who whom whose why how does did can could should would will just like know
anyone anybody someone any into onto about there their them they then than also only its not
but get got does doing done never ever one any all some""".split())


def _terms(text: str) -> set[str]:
    """Words as the facts match them: lower case, no little words, plurals singular."""
    out = set()
    for w in _TERM.findall(text.casefold()):
        w = w.removesuffix("'s")
        if len(w) < 3 or w in _STOP:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def clean_text(text: str) -> str:
    """Library text as plain ASCII (style.to_ascii, like every outgoing line) with no URLs,
    wiki templates, links, tags, emphasis or note marks."""
    t = to_ascii(text)
    low = t.casefold()
    if "http" in low or "www." in low:  # the clause pattern is slow on long text with no stops
        t = _URL_CLAUSE.sub("", t)
    t = _TEMPLATE.sub("", _TEMPLATE.sub("", t))
    t = _WIKILINK.sub(r"\1", t)
    t = _TAG.sub("", t)
    t = _NOTE_MARK.sub("", t)
    return _EMPHASIS.sub("", t)


def _pieces(text: str, *, prose: bool = False) -> list[str]:
    """The sentences and list items of a text, cleaned, without boilerplate, swearing or a
    macro (_MACRO). `prose` (a wiki or curated text) keeps only whole statements: no scraps of
    a list ("Squire Pell (quest) -or-Squire Dun", "3211 Reputation # To the Rise ...!", "The
    Story Thus Far..."), no lead-in to one ("The following quests are given here:"), no aside
    in brackets, no question, no table row."""
    out = []
    macro = False
    for line in clean_text(text).split("\n"):
        for s in split_sentences(_LIST_ITEM.sub("", line)):
            s = " ".join(s.split()).strip(" |;-")
            if m := _MACRO.search(s):
                if out and (out[-1].endswith(":") or _MACRO_WORD.search(out[-1])):
                    out.pop()  # its lead-in
                macro, s = True, s[:m.start()].strip()
                if not _SENTENCE_END.search(s) or _MACRO_WORD.search(s):
                    continue
            elif macro and _EXPLAINS.match(s):
                continue  # what the macro does
            else:
                macro = False
            if not s or _BOILERPLATE.match(s) or _SWEARING.search(s):
                continue
            if prose and (len(_PARENTHESES.sub("", s).split()) < 4
                          or not _SENTENCE_END.search(s) or s.endswith("...") or "#" in s
                          or s.count(" - ") >= _TABLE_CELLS):
                continue
            out.append(s)
    return out


def _label(c: Chunk, body: str) -> str:
    """The page a wiki or curated fact is from, when its words don't already say it."""
    if c.source == "world" or c.source == "atlasloot":
        return ""
    title = " ".join(_TACTICS_TITLE.sub("", clean_text(c.title)).split())
    head = " ".join(body.split()[:8]).casefold()
    return "" if not title or title.casefold() in head else f"{title}: "


def _cut(text: str, room: int) -> str:
    """`text` in `room` characters: cut at a word, and back to the last comma when that keeps
    half of it (a loot list ends on a whole item, a sentence on a whole clause)."""
    t = truncate_words(text, room)
    if len(t) < len(text) and (at := t.rfind(", ")) >= len(t) // 2:
        t = t[:at]
    return t


# A loot record's list, cut, says how many it holds (judge B): "2 items from 11, among them:
# ...", "What X drops (... in AtlasLoot): 12 items, among them: ...", so a cut list never reads
# as the whole table. The library writes a list in parts of LIST_PART (world.py's and
# atlasloot.py's per_chunk), so a part that size may go on in the next: "30 or more".
_LIST_LEAD = re.compile(r"(?:(?P<pool>\d+ items from|One item from)|(?P<lead>Always|Sometimes|"
                        r"Quest items|.*?\bin AtlasLoot\)?)):\s+")
LIST_PART = 30


def _entries(items: str) -> list[str]:
    """A list's entries: its parts between the commas outside brackets, a part that starts in
    lower case going on with the name before it ("Apolyon, the Soul-Render")."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(items):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and items.startswith(", ", i):
            parts.append(items[start:i])
            start = i + 2
    out: list[str] = []
    for p in [*parts, items[start:]]:
        if out and p[:1].islower():
            out[-1] += ", " + p
        else:
            out.append(p)
    return out


def _cut_list(piece: str, room: int) -> str | None:
    """A loot record's list in `room` characters, whole entries only, led by its full count ("" when
    not one fits); None when the piece is no list."""
    m = _LIST_LEAD.match(piece)
    entries = _entries(piece[m.end():].rstrip(" .")) if m else []
    if len(entries) < 2:
        return None
    count = f"{len(entries)}{' or more' if len(entries) >= LIST_PART else ''}"
    lead = (f"{m.group('pool')} {count}, among them: " if m.group("pool")
            else f"{m.group('lead')}: {count} items, among them: ")
    kept: list[str] = []
    for e in entries:
        if len(lead) + sum(len(x) + 2 for x in kept) + len(e) > room:
            break
        kept.append(e)
    return lead + ", ".join(kept) if kept else ""


def _cut_piece(piece: str, room: int, loot: bool) -> str:
    if loot and (listed := _cut_list(piece, room)) is not None:
        return listed
    return _cut(piece, room)


def _fit(pieces: list[str], order: list[int], room: int, *, cut: bool = False,
         loot: bool = False) -> str:
    """The pieces in `order` that fit `room` characters, said in their own order. The best is
    cut (_cut) when it alone is too long; with `cut` (a record read in order, such as a loot
    table) so is the first that no longer fits, and nothing comes after it. A `loot` record's
    list is cut to whole entries and says its full count (_cut_list)."""
    chosen, used, tail = [], 0, ""
    for i in order:
        n = len(pieces[i]) + (1 if chosen else 0)
        if used + n <= room:
            chosen.append(i)
            used += n
        elif not chosen:
            return _cut_piece(pieces[i], room, loot)
        elif cut:
            if room - used - 1 >= MIN_LINE_CHARS // 2:
                tail = _cut_piece(pieces[i], room - used - 1, loot)
            break
    return " ".join([*(pieces[i] for i in sorted(chosen)), *([tail] if tail else [])])


def _fact_line(c: Chunk, terms: set[str], room: int, named: frozenset[str] = frozenset(),
               extra: frozenset[str] = frozenset()) -> str:
    """One chunk as one fact of at most `room` characters ("" when nothing in it is one). A
    world or AtlasLoot record is read in order, its sources first and cut where the room ends;
    prose gives the sentences that share his words, the opening one first among equals. A
    word of a name he gave (`named`: every sentence of its page has it) counts half, and a
    WHERE_WORDS one he did not type (`extra`) a quarter: the sentence with "entrance" wins for
    "where is the entrance to Icecrown Citadel"."""
    if c.source in ("world", "atlasloot"):
        pieces = _pieces(c.text.split(_STORY)[0])
        lead = [i for i, p in enumerate(pieces) if i and p.startswith(_WORLD_FIRST)]
        order = [0, *lead, *(i for i in range(1, len(pieces)) if i not in lead)]
        return _fit(pieces, order, room, cut=True, loot=c.kind == "loot") if pieces else ""
    pieces = _pieces(c.text, prose=True)
    if not pieces:
        return ""
    hits = [2 * len((shared := _terms(p) & terms) - named - extra) + len(shared & named)
            + 0.5 * len((shared & extra) - named) for p in pieces]
    order = sorted((i for i, h in enumerate(hits) if h or i == 0),
                   key=lambda i: (-(hits[i] + (0.5 if i == 0 else 0.0)), i))
    if not any(hits[1:]):  # nothing past its opening matched: what it says first
        filled = sum(len(pieces[i]) for i in order)
        for i in range(1, len(pieces)):
            if filled >= FILL_CHARS:
                break
            order.append(i)
            filled += len(pieces[i])
    probe = _fit(pieces, order, room)
    label = _label(c, probe)
    body = _fit(pieces, order, room - len(label)) if label else probe
    return f"{label}{body}" if body else ""


def fact_lines(chunks: Sequence[Chunk], query: str, *, budget: int = FACTS_TOKENS,
               keep: Callable[[Chunk, str], bool] | None = None,
               names: Sequence[str] = ()) -> list[str]:
    """The chunks, best first, as facts for a prompt: one line each, at most LINE_CHARS, and
    `budget` estimated tokens in all (the later ones go first). A world chunk keeps its own
    head and its sources (who drops it, sells it, starts the quest), never a quest's story; a
    wiki or curated one the sentences that match his words (`names`, the ones he named, count
    less), labelled with its page when they don't name it. `keep(chunk, line)` (the gate,
    relevant) leaves a line out."""
    terms, room = his_words(query), budget * 4
    named, extra = frozenset(_terms(" ".join(names))), frozenset(terms - _terms(query))
    out: list[str] = []
    seen: set[str] = set()
    titles: dict[str, int] = {}
    for c in chunks:
        left = room - sum(len(x) + 1 for x in out)
        if left < MIN_LINE_CHARS:
            break
        title = c.title.casefold()
        if titles.get(title, 0) >= SAME_TITLE or (c.source == "wiki" and "/" in title):
            continue  # a wiki subpage is someone's notes
        line = _fact_line(c, terms, min(LINE_CHARS, left), named, extra)
        body = " ".join(line.casefold().replace(title, " ").split())  # three ranks of one item
        if (len(line) >= min(MIN_LINE_CHARS, len(c.text)) and body not in seen
                and (keep is None or keep(c, line))):
            seen.add(body)
            titles[title] = titles.get(title, 0) + 1
            out.append(line)
    return out


# -- relevance -----------------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _name_re(name: str) -> re.Pattern:
    return re.compile(name_pattern(name), re.IGNORECASE)


# What a superlative or a comparative grades: "toughest", "rarest", "biggest", "easiest" are
# generic like "fastest" (fix round 3), and so are most, least, more and less.
_GRADES = frozenset("""tough rare weak large small high low strong fast quick easy hard cheap
big nice fine cute safe late early great brave pure hot cold old new long short simple cool
deep slow smart dumb sad mad wild crazy funny heavy scary ugly pretty lucky busy tiny dirty
angry deadly weird lame close near clean poor tall wide thin thick dark bright loud quiet
calm soft sweet fair tanky squishy rough""".split())


def _graded(w: str) -> bool:
    if w in ("most", "least", "more", "less"):
        return True
    for end in ("est", "er"):
        if w.endswith("i" + end) and w[:-len(end) - 1] + "y" in _GRADES:
            return True  # easiest, funnier
        if w.endswith(end):
            stem = w[:-len(end)]
            if stem in _GRADES or stem + "e" in _GRADES or (
                    len(stem) > 2 and stem[-1] == stem[-2] and stem[:-1] in _GRADES):
                return True  # toughest, rarest, biggest
    return False


def salient(query: str) -> set[str]:
    """His words that can tie a fact to the question: _terms without GENERIC and without
    grading words (_graded)."""
    return {t for t in _terms(query) if t not in GENERIC and not _graded(t)}


def most_specific(names: Sequence[str]) -> list[str]:
    """Of the names his line gives, the ones a fact must hold one of: the most specific
    (names.name_level), so a boss over the raid it is in and either over the zone or city that
    only locates the question ("where in dalaran is the cold weather flying trainer")."""
    top = max((name_level(n) for n in names), default=0)
    return [n for n in names if name_level(n) == top]


# What a fact about training says (re-review 4, minor N5: "where can i learn to fish?" got a pet
# fish for its one word).
_TRAINING = re.compile(r"\b(?:trainers?|trains?|trained|training|taught|teach(?:es)?|"
                       r"learn(?:s|ed|t)?)\b", re.IGNORECASE)


def relevant(c: Chunk, line: str, names: Sequence[str], words: set[str], *,
             strict: bool = False, trainer: bool = False) -> bool:
    """The gate: whether `line` (made from chunk `c`) is about his question. With `names`
    (the question's, and the titles his line names), the line or the chunk's title holds one
    of the most specific of them (in any spelling, possessive too: names.name_pattern);
    without, it shares his `words` (salient) as whole words, a plural or a possessive the
    same word ("fire" is never "firework"): one of one or two, two of three or more, or with
    `strict` (a question about gear or a class: "what's a good gem for fire mage") all of
    them, since the library has no opinion of what is good. Without names, a `trainer`
    question's fact also speaks of training (_TRAINING)."""
    hay = f"{c.title} {line}"
    if names:
        return any(_name_re(n).search(hay) for n in most_specific(names))
    if not words or (trainer and not _TRAINING.search(hay)):
        return False
    shared = words & _terms(hay)
    if strict:
        return shared == words
    return len(shared) >= (2 if len(words) >= 3 else 1)


_GRAM_WORD = re.compile(r"[A-Za-z0-9][\w'-]*")
_EDGE = frozenset("""the a an of to in on at for and or is are was were my your his her i you
we he she it do does did how where what who whos why when which any anyone know get got from
with this that there here me us them up out about just still really""".split())


def _singular(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w


def _plural(name: str) -> str:
    """A name with its last word plural: "oracle" -> "oracles", "faction champion" ->
    "faction champions"."""
    head, _, last = name.rpartition(" ")
    if last.endswith("s"):
        return name
    last = last[:-1] + "ies" if last.endswith("y") and last[-2:-1] not in "aeiou" else last + "s"
    return f"{head} {last}".strip()


def title_names(q: GameQuestion) -> list[str]:
    """What his line may name by a page's title, for Library.titled: its names, also plural
    (names.py reads "the oracles" as "oracle"; the page is "The Oracles"), and its runs of two
    to six words that neither start nor end on a little word, each also with its words
    singular ("aspects" -> "aspect", "emblems of frost" -> "emblem of frost")."""
    words = _GRAM_WORD.findall(q.query)
    out = [x for n in q.names
           for x in (n, " ".join(_singular(w) for w in n.split()), _plural(n))]
    for n in range(2, 7):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0].casefold() in _EDGE or gram[-1].casefold() in _EDGE:
                continue
            out.append(" ".join(gram))
            out.append(" ".join(_singular(w.casefold()) for w in gram))
    return list(dict.fromkeys(x.casefold() for x in out))[:TITLE_NAMES]


def _proper(title: str) -> bool:
    """A page about something with a name ("Tier 10", "Deathbringer's Will"), not about a kind
    of thing ("Frost wyrm"): every word of four letters or more starts with a capital."""
    words = [w for w in _GRAM_WORD.findall(_QUALIFIER.sub("", title)) if w[:1].isalpha()]
    long = [w for w in words if len(w) >= 4]
    return bool(words) and all(w[0].isupper() for w in long)


def _title_name(title: str) -> str:
    t = _QUALIFIER.sub("", title).strip().casefold()
    return t[4:] if t.startswith("the ") else t


_WHERE_ASK = re.compile(r"\bwhere\b|\bhow (?:do|does|can|would) (?:i|you|we|u) get (?:to|into|in)"
                        r"\b", re.IGNORECASE)
# What tells where something is: a "where" question counts these as his words.
WHERE_WORDS = frozenset("""located location found sold vendor quartermaster trainer near inside
outside entrance entry portal beneath north south east west northeast northwest southeast
southwest""".split())


def his_words(query: str) -> set[str]:
    """His words (_terms) as the facts are chosen and led by: with WHERE_WORDS when he asks
    where something is."""
    return _terms(query) | (WHERE_WORDS if _WHERE_ASK.search(query) else set())


def lead_with(named: Sequence[Chunk], hits: Sequence[Chunk], query: str,
              names: Sequence[str] = ()) -> list[Chunk]:
    """The chunks his line names by title first (those the search also found, in its order,
    then the rest by how many of his words they share, then page order), then the search's
    other hits. A titled chunk that shares none of his words besides the names he gave goes
    after the hits ("how do i start the onyxia quest" is not about the dragon's biography),
    unless he gave nothing but the name ("where is the ruby sanctum")."""
    terms = his_words(query)
    other = terms - _terms(" ".join(names))
    if not _terms(query) - _terms(" ".join(names)):  # the name alone: its pages lead
        other = set()
    rank = {c.id: i for i, c in enumerate(hits)}
    ordered = sorted(named, key=lambda c: (c.id not in rank, rank.get(c.id, 0),
                                           -len(_terms(c.text) & terms), c.id))
    first = [c for c in ordered if not other or _terms(c.text) & other]
    last = [c for c in ordered if c not in first]
    seen = {c.id for c in ordered}
    return [*first, *(c for c in hits if c.id not in seen), *last]


# -- the guild's faction -------------------------------------------------------------------------

ICON_KINDS = frozenset({"quest", "zone"})  # the kinds whose faction icons tag them
_SIDES = frozenset({"alliance", "horde"})
_ICON = re.compile(r"\((Alliance|Horde)\)")  # wikitext's faction icons: "(Alliance) Valgarde"
_QUALIFIED = re.compile(r"\((?:[^()]*\s)?(Alliance|Horde)\)$")  # "Koralon (Horde)"
# A place a faction of neither side holds is everyone's, whatever its lists' icons say (fix
# round 3: the Violet Hold's Kirin Tor and its one "(Alliance)" inmate group, Light's Breach of
# the Argent Crusade and its Horde flights).
_NEUTRAL = re.compile(r"(?<![\w'])(?:" + words_re(*NEUTRAL_FACTIONS) + r")(?![\w'])",
                      re.IGNORECASE)


def _quest_tags(text: str) -> list[str]:
    """A world quest's head tags: "Quest X (Old) (level 80, raid (10), Alliance)." -> ["level
    80", "raid (10)", "Alliance"]: the bracket the head's sentence ends on, read with the
    brackets inside it."""
    depth, start = 0, -1
    for i, ch in enumerate(text[:400]):
        if ch == "(":
            start = i if depth == 0 else start
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0 and text[i + 1:i + 2] in (".", ""):
                tags, level, cut = [], 0, start + 1
                for j in range(start + 1, i):
                    level += {"(": 1, ")": -1}.get(text[j], 0)
                    if text[j] == "," and level == 0:
                        tags.append(text[cut:j].strip())
                        cut = j + 1
                return [*tags, text[cut:i].strip()]
    return []


def chunk_faction(c: Chunk) -> str | None:
    """The faction a chunk is for, "alliance" or "horde", when everything that tags it says
    the same: a world quest's tag ("Quest X (level 80, ..., Alliance)."); a page's or a loot
    table's title qualifier ("Koralon the Flame Watcher (Horde)"); a section of one faction
    ("NPCs > Quest NPCs > Alliance", "Obtaining the Drakefire Amulet (Alliance)"); and for a
    quest or a place that no neutral faction holds, its faction icons. None when it is
    neither side's or both's ("(Alliance) Jaina or (Horde) Sylvanas"); a faction its story
    names is no tag."""
    if c.source == "world":
        tags = _quest_tags(c.text) if c.kind == "quest" else []
        sides = {t.casefold() for t in tags} & _SIDES
    else:
        sides = ({m.group(1).casefold() for m in _ICON.finditer(c.text)}
                 if c.kind in ICON_KINDS and not _NEUTRAL.search(c.text) else set())
        if m := _QUALIFIED.search(c.title.strip()):
            sides.add(m.group(1).casefold())
        for part in re.split(r"[>;]", c.section):
            if m := _QUALIFIED.search(part.strip()):
                sides.add(m.group(1).casefold())
            elif part.strip().casefold() in _SIDES:
                sides.add(part.strip().casefold())
    return next(iter(sides)) if len(sides) == 1 else None


def by_place(chunks: Sequence[Chunk], names: Sequence[str]) -> list[Chunk]:
    """The chunks that also hold the place that locates his question (a zone, a city or a camp
    less specific than the gate's names: "where in dalaran is the cold weather flying trainer")
    first, in order. The raid a boss is in locates nothing ("how do we beat marrowgar in icc?":
    the boss's own tactics come first)."""
    rest = [n for n in names if n not in most_specific(names) and name_level(n) == 1]
    if not rest:
        return list(chunks)
    return sorted(chunks, key=lambda c: not any(_name_re(n).search(f"{c.title} {c.text}")
                                                for n in rest))


def for_side(chunks: Sequence[Chunk], side: str | None) -> list[Chunk]:
    """The chunks without those of the faction that is not `side` (chunk_faction); all of
    them when `side` is None (his race unknown)."""
    return [c for c in chunks if side is None or chunk_faction(c) in (None, side)]


# -- a boss's mechanics ------------------------------------------------------------------------

_ABILITY = re.compile(r"(?:^|\n)\s*-\s*([A-Z][\w'-]*(?: [\w'-]+){0,4}?)\s*(?:\([^)]*\))?\s*:")
_NOT_ABILITY = re.compile(r"^(?:phase|stage|heroic|normal|note|tip|\d|enrage|berserk|frenzy)",
                          re.IGNORECASE)
_ADVICE = re.compile(
    r"\b(?:should|need to|needs to|must|avoid|move|moves|stack|spread|free|soak|interrupt|"
    r"dispel|stay|run|stand|out of|away from|switch|taunt|kite|don't|do not|have to|has to|"
    r"make sure|watch|get out|range)\b", re.IGNORECASE)
_NOT_TACTICS = re.compile(r"abilit|quote|achievement|solo|bug|patch|video|loot|trivia|lore|"
                          r"background|alternat|heroic|hard mode|notes", re.IGNORECASE)
# Sentences that lean on the one before ("They are slow"), or a raider's own story.
_NO_CONTEXT = re.compile(r"^(?:they|it|this|these|those|he|she|his|her|its|their|there|then|"
                         r"also|however|but|and|so|that|because|can|may|will)\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:we|our|us|i|my|me)\b", re.IGNORECASE)


def _abilities(chunks: Sequence[Chunk]) -> list[str]:
    out, seen = [], set()
    for c in chunks:
        if "abilit" not in c.section.casefold():
            continue
        text = "\n" + clean_text(c.text).replace(" - ", "\n- ")
        for m in _ABILITY.finditer(text):
            name = m.group(1).strip()
            if len(name) >= 4 and not _NOT_ABILITY.match(name) and name.casefold() not in seen:
                seen.add(name.casefold())
                out.append(name)
    return out


def _advice(chunks: Sequence[Chunk]) -> list[str]:
    lo, hi = MECHANIC_WORDS
    out = []
    for c in chunks:
        if _NOT_TACTICS.search(c.section):
            continue
        for s in _pieces(c.text):
            if (lo <= len(s.split()) <= hi and _ADVICE.search(s) and not _NO_CONTEXT.match(s)
                    and not _FIRST_PERSON.search(s)):
                out.append(s)
    return out


def boss_mechanics(chunks: Sequence[Chunk], *, n: int = MECHANICS_LINES) -> list[str]:
    """Up to n short lines of a boss's real mechanics from its tactics page (Library
    .strategy_for), in page order: the advice sentences that name one of its abilities (the
    page's ability list) not named yet, then, short of n, the other advice. Plain ASCII, one
    sentence a line, MECHANIC_WORDS long."""
    names = _abilities(chunks)
    tips = _advice(chunks)
    picked, covered = [], set()
    for tip in tips:
        named = {a.casefold() for a in names
                 if re.search(rf"\b{re.escape(a)}(?:e?s)?\b", tip, re.IGNORECASE)}
        if named - covered:
            picked.append(tip)
            covered |= named
    for tip in tips:
        if len(picked) >= n:
            break
        if tip not in picked:
            picked.append(tip)
    order = {tip: i for i, tip in enumerate(tips)}
    return sorted(picked[:n], key=order.__getitem__)


# -- the lookup --------------------------------------------------------------------------------

class LibraryFacts:
    """The library for live talk: one lookup per human line, shared by every reply to it (a
    task per line; KEEP_LINES are kept), and the mechanics per boss, kept for the session.

    A line's lookup (the module docstring has its time cap): Library.search(his written-out
    query, k=SEARCH_K, the question's kinds) at REPLY_HUMAN through `llm` (the app's
    LLMScheduler), or Library.search_text; the chunks his line names by title lead
    (Library.titled, only pages about something with a name); what is the other faction's
    goes (for_side; `faction` gives the guild's, "horde", "alliance" or None); then the gate
    (relevant) and fact_lines. Each lookup is logged once, at debug, with counts only."""

    def __init__(self, library, llm, *, cap_s: float = FACTS_CAP_S,
                 timer: Callable[[], float] = time.monotonic,
                 faction: Callable[[], str | None] = lambda: None):
        self.library, self.llm, self.cap_s, self.timer = library, llm, cap_s, timer
        self.faction = faction
        self._lines: OrderedDict[int, asyncio.Task] = OrderedDict()
        self._mechanics: dict[tuple[str, str | None], list[str]] = {}

    def start(self, line_id: int, q: GameQuestion) -> None:
        """Start the lookup for his line `line_id` (once: a second call does nothing)."""
        if line_id in self._lines:
            return
        self._lines[line_id] = asyncio.get_running_loop().create_task(
            self._lookup(line_id, q), name=f"mind-facts-{line_id}")
        while len(self._lines) > KEEP_LINES:
            self._lines.popitem(last=False)[1].cancel()

    async def for_line(self, line_id: int) -> list[str]:
        """The fact lines for his line: [] when it had no lookup or the lookup was cleared. A
        reply cancelled while it waits leaves the lookup to the others, and stays cancelled
        even when the lookup was cancelled with it."""
        task = self._lines.get(line_id)
        if task is None:
            return []
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if task.cancelled() and not (me is not None and me.cancelling()):
                return []
            raise

    async def _lookup(self, line_id: int, q: GameQuestion) -> list[str]:
        t0 = self.timer()
        kinds = list(q.kinds)
        hits, outcome = [], "search"
        try:
            hits = await asyncio.wait_for(
                self.library.search(q.query, k=SEARCH_K, kinds=kinds, llm=self.llm,
                                    priority=Priority.REPLY_HUMAN),
                max(0.0, self.cap_s - TEXT_RESERVE_S))
        except TimeoutError:
            outcome = "timeout"
        except Exception as e:  # a broken or locked library.db: the reply goes on without it
            outcome = f"error:{type(e).__name__}"
        if outcome != "search":
            if self.timer() - t0 < self.cap_s:
                try:
                    hits = self.library.search_text(q.query, k=SEARCH_K, kinds=kinds)
                    outcome += "+text"
                except Exception as e:
                    outcome += f"+text_error:{type(e).__name__}"
            else:
                outcome += "+out_of_time"
        named: list[Chunk] = []
        lines: list[str] = []
        if not outcome.endswith("+out_of_time"):
            try:
                named = [c for c in self.library.titled(title_names(q), kinds)
                         if _proper(c.title)]
            except Exception as e:
                outcome += f"+titled_error:{type(e).__name__}"
        found = len(hits) + len(named)
        try:
            said = sides_named(q.query)
            side = (next(iter(said)) if len(said) == 1 else None) if said else self.faction()
            hits, named = for_side(hits, side), for_side(named, side)
            names = list(dict.fromkeys([*q.names, *(_title_name(c.title) for c in named)]))
            words = salient(q.query)
            strict, trainer = q.topic in STRICT_TOPICS, q.topic == "trainer"
            lines = fact_lines(by_place(lead_with(named, hits, q.query, names), names),
                               q.query, names=names,
                               keep=lambda c, line: relevant(c, line, names, words,
                                                             strict=strict, trainer=trainer))
        except Exception:  # a chunk the formatter chokes on: no facts, never a lost reply
            log.exception("facts_format_failed: line=%d", line_id)
            lines = []
        log.debug("facts_lookup: line=%d topic=%s outcome=%s hits=%d facts=%d named=%d "
                  "other_side=%d ms=%d", line_id, q.topic, outcome, len(hits), len(lines),
                  len(named), found - len(hits) - len(named), round((self.timer() - t0) * 1000))
        return lines

    def mechanics(self, boss: str, instance: str | None = None) -> list[str]:
        """boss_mechanics of the boss's tactics page (Library.strategy_for), looked up once a
        session; for an encounter of several bosses with no page of its own
        (wow.ENCOUNTER_MEMBERS), a line from each member's tactics page. Never from a lore
        page or a search: [] when there is no tactics page or the library fails."""
        key = (boss.casefold(), instance)
        if key not in self._mechanics:
            try:
                lines = boss_mechanics(self.library.strategy_for(boss, instance))
                members = next((m for name, m in ENCOUNTER_MEMBERS.items()
                                if name.casefold() == boss.casefold()), ())
                if not lines and members:
                    lines = [x for member in members
                             for x in boss_mechanics(self.library.strategy_for(member, instance),
                                                     n=1)][:MECHANICS_LINES]
                self._mechanics[key] = lines
            except Exception as e:
                log.warning("boss_mechanics_failed: error=%s", type(e).__name__)
                self._mechanics[key] = []
        return self._mechanics[key]

    def clear(self) -> None:
        """The session ended: pending lookups are cancelled and nothing is kept."""
        for task in self._lines.values():
            task.cancel()
        self._lines.clear()
        self._mechanics.clear()
