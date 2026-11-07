"""The Library tab's read-only question to library.db: BM25 over the guild's reference shelf,
opened and closed inside the worker thread that asks. Also the one hit shape the mind's `library`
op answers in, and the rules both share for a question."""
import re
import sqlite3
from pathlib import Path

from leeroy_mind.library.chunks import KINDS
from leeroy_mind.library.core import Library

K = 12
EXACT_KINDS = ("command", "teleport")
TEXT_MAX = 1200
QUERY_MAX = 200
NO_LIBRARY = "there is no library.db yet: build it with the library CLI while the mind is stopped."
OTHER_VERSION = "library.db is from another version: migrate it with the library CLI."
HIT_KEYS = ("id", "kind", "title", "section", "text", "source", "cite", "score")


def clean_query(raw) -> str | None:
    """The question with its whitespace collapsed, or None when it is not 1-QUERY_MAX characters."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    return text if 0 < len(text) <= QUERY_MAX else None


def cut(text: str) -> str:
    """text, or its first TEXT_MAX characters cut at the last line break before that."""
    if len(text) <= TEXT_MAX:
        return text
    head = text[:TEXT_MAX]
    at = head.rfind("\n")
    return head[:at] if at > 0 else head


def hit(chunk) -> dict:
    return {"id": chunk.id, "kind": chunk.kind, "title": chunk.title, "section": chunk.section,
            "text": cut(chunk.text), "source": chunk.source, "cite": chunk.cite(),
            "score": round(float(chunk.score), 4)}


def clean_hits(raw, kind: str | None = None) -> list[dict]:
    """The hits another process sent, kept only when well formed (and of `kind`, if given)."""
    out = []
    for h in raw if isinstance(raw, list) else ():
        if not isinstance(h, dict) or set(h) != set(HIT_KEYS):
            continue
        if (type(h["id"]) is not int or h["kind"] not in KINDS
                or not all(isinstance(h[k], str) for k in ("title", "section", "text", "source", "cite"))
                or isinstance(h["score"], bool) or not isinstance(h["score"], (int, float))):
            continue
        if kind and h["kind"] != kind:
            continue
        out.append({**h, "text": cut(h["text"]), "score": float(h["score"])})
        if len(out) == K:
            break
    return out


def _stats(lib: Library) -> dict:
    kinds: dict[str, int] = {}
    s = lib.stats()
    for key, n in s["chunks"].items():
        kind = key.split("/", 1)[1]
        kinds[kind] = kinds.get(kind, 0) + n
    return {"total": sum(kinds.values()), "kinds": kinds, "embedded": s["embedded"]}


def _ask(lib: Library, query: str, kind: str | None) -> list:
    """The exact command and teleport records for the question first (only when no kind is
    chosen: a chosen kind already gets them from search_text), then the BM25 hits."""
    kinds = [kind] if kind else None
    exact = lib.reference_matches(query, list(EXACT_KINDS), K) if kind is None else []
    seen = {c.id for c in exact}
    return exact + [c for c in lib.search_text(query, K, kinds) if c.id not in seen][:K - len(exact)]


def _search(lib: Library, query: str, kind: str | None):
    try:
        return _ask(lib, query, kind)
    except sqlite3.OperationalError:
        lib.conn.rollback()
        words = " ".join(re.findall(r"\w+", query))
        return _ask(lib, words, kind) if words else []


def library_view(path: Path, query: str | None, kind: str | None, *, clock) -> dict:
    """The tab's answer. Run in a worker thread: the library is opened and closed here."""
    out = {"setup": False, "why": NO_LIBRARY, "stats": None, "hits": []}
    if not Path(path).exists():
        return out
    try:
        lib = Library(path, clock=clock, readonly=True)
    except RuntimeError:
        return {**out, "why": OTHER_VERSION}
    try:
        out = {"setup": True, "why": None, "stats": None, "hits": []}
        if query:
            out["hits"] = [hit(c) for c in _search(lib, query, kind)]
        else:
            out["stats"] = _stats(lib)
        return out
    finally:
        lib.close()
