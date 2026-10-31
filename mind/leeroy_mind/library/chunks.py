"""What sources produce (ChunkDraft), what the library returns (Chunk), and the prose chunker that
cuts wiki sections into pieces of about 120-250 words."""
import re
from dataclasses import dataclass, field

KINDS = ("item", "loot", "quest", "lore", "strategy", "class", "zone", "event")
SOURCES = ("world", "atlasloot", "wiki", "curated")
MIN_WORDS = 120
MAX_WORDS = 250

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[\x00])")


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """One chunk as a source builds it. `key` is stable within the source across rebuilds, so an
    unchanged chunk keeps its embedding. `weight` (0-1] scales its search score: 1 for the
    material the guild asks about, less for trade goods, recipes, grey items and quotes."""
    key: str
    kind: str
    title: str
    text: str
    section: str = ""
    url: str | None = None
    license: str | None = None
    map_id: int | None = None
    instance: str | None = None
    ref_ids: tuple[str, ...] = ()
    weight: float = 1.0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown chunk kind {self.kind!r}")
        if not self.text.strip() or not self.title.strip():
            raise ValueError(f"chunk {self.key!r} has no title or text")
        if not 0 < self.weight <= 1:
            raise ValueError(f"chunk {self.key!r} has weight {self.weight}, outside (0, 1]")


@dataclass(frozen=True, slots=True)
class Chunk:
    id: int
    source: str
    kind: str
    title: str
    section: str
    text: str
    url: str | None
    license: str | None
    map_id: int | None
    instance: str | None
    ref_ids: tuple[str, ...]
    t_created: int
    score: float = 0.0

    def heading(self) -> str:
        return f"{self.title} - {self.section}" if self.section else self.title

    def cite(self) -> str:
        """Where it came from, for an attribution line: the wiki page and its license, or the
        local source."""
        if self.source == "wiki":
            return f"warcraft.wiki.gg: {self.title} ({self.license}) {self.url}"
        return {"world": "the server's world database",
                "atlasloot": "AtlasLoot (WotLK tables)",
                "curated": "a hand-written 3.3.5 summary of the server's world database"
                }.get(self.source, self.source)

    def format(self) -> str:
        """One prompt block: the heading, then the text."""
        return f"[{self.heading()}] {self.text}"


@dataclass
class ItemRow:
    id: int
    name: str
    quality: int
    ilvl: int
    req_level: int
    slot: str = ""


@dataclass
class QuestRow:
    id: int
    title: str
    level: int
    zone: str = ""
    prev_id: int = 0
    next_id: int = 0
    exclusive_group: int = 0


@dataclass
class SourceResult:
    name: str
    chunks: list[ChunkDraft] = field(default_factory=list)
    items: list[ItemRow] | None = None    # None: leave the item table alone
    quests: list[QuestRow] | None = None  # None: leave the quest table alone
    log: list[str] = field(default_factory=list)


def word_count(text: str) -> int:
    return len(text.split())


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def _pieces(paragraph: str, max_words: int) -> list[str]:
    """A paragraph as it is, or cut at sentence ends into runs of at most max_words (a single
    sentence longer than that is cut at word boundaries)."""
    if word_count(paragraph) <= max_words:
        return [paragraph]
    out: list[str] = []
    cur: list[str] = []
    n = 0
    for s in split_sentences(paragraph):
        words = s.split()
        while len(words) > max_words:
            if cur:
                out.append(" ".join(cur))
                cur, n = [], 0
            out.append(" ".join(words[:max_words]))
            words = words[max_words:]
        if n + len(words) > max_words and cur:
            out.append(" ".join(cur))
            cur, n = [], 0
        if words:
            cur.append(" ".join(words))
            n += len(words)
    if cur:
        out.append(" ".join(cur))
    return out


def _label(names: list[str]) -> str:
    uniq = list(dict.fromkeys(n for n in names if n and n != "..."))
    return "; ".join(uniq[:3]) + ("; ..." if len(uniq) > 3 else "")


def join_labels(a: str, b: str) -> str:
    """Two chunk labels as one, the way chunk_sections joins merged sections."""
    return _label(a.split("; ") + b.split("; "))


def chunk_sections(sections: list[tuple[str, list[str]]], *, min_words: int = MIN_WORDS,
                   max_words: int = MAX_WORDS) -> list[tuple[str, str]]:
    """Pack (section label, paragraphs) into (label, text) chunks of about min..max words.
    Paragraphs stay whole when they fit; a chunk never spans a new section once it has
    min_words; a short tail is folded into the chunk before it when the result stays near
    max_words. Labels of merged sections are joined with '; '."""
    out: list[tuple[list[str], list[str]]] = []
    names: list[str] = []
    paras: list[str] = []
    n = 0

    def flush():
        nonlocal names, paras, n
        if paras:
            out.append((names, paras))
        names, paras, n = [], [], 0

    for section, paragraphs in sections:
        for para in paragraphs:
            for piece in _pieces(para, max_words):
                w = word_count(piece)
                if paras and (n + w > max_words or (section not in names and n >= min_words)):
                    flush()
                if section not in names:
                    names.append(section)
                paras.append(piece)
                n += w
    flush()
    if len(out) >= 2:
        tail_names, tail = out[-1]
        prev_names, prev = out[-2]
        if (sum(map(word_count, tail)) < min_words // 2
                and sum(map(word_count, prev + tail)) <= max_words * 6 // 5):
            out[-2:] = [(prev_names + [x for x in tail_names if x not in prev_names], prev + tail)]
    return [(_label(ns), "\n".join(ps)) for ns, ps in out]
