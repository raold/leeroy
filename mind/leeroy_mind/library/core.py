import asyncio
import json
import logging
import re
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from leeroy_mind.clock import Clock
from leeroy_mind.library.chunks import KINDS, SOURCES, Chunk, ChunkDraft, SourceResult
from leeroy_mind.library.db import VEC_DIM, checkpoint, open_library_db
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import Priority, SchedulerError
from leeroy_mind.memory.recall import fts_query, mmr
from leeroy_mind.storage.db import EmbeddingMismatch
from leeroy_mind.storage.rows import cosine, pack_vec, unpack_vec
from leeroy_mind.storage.store import clean
from leeroy_mind.storage.store import fts_query as quote_words
from leeroy_mind.text import word_jaccard

log = logging.getLogger("leeroy_mind")

DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"
QUERY_PREFIX = ("Instruct: Given a question about World of Warcraft, retrieve passages that "
                "answer it\nQuery: ")
_CANDIDATES = 50
_LEADS = 10          # page leads that join the candidates, so "Arthas" can reach his page
_LEAD_BONUS = 1.25   # a page's first chunk or a curated summary, its title has every query word
_TERM = re.compile(r'"([^"]+)"')
_TITLE_WORD = re.compile(r"\w+")
_LINK_ID = re.compile(r"Hitem:(\d+)")
_SOURCE_ORDER = {"world": 0, "atlasloot": 1, "wiki": 2, "curated": 3}
# "what drops from X", "what does X drop", "loot from X", "X loot table", "X drops"
_LOOT_QUESTIONS = (
    re.compile(r"^(?:what|which)\s+(?:\w+\s+)?(?:does|do|did|can|will)\s+(?P<who>.+?)\s+drop\b",
               re.IGNORECASE),
    re.compile(r"\b(?:drops?|loot(?:\s+table)?)\s+(?:from|of|off|for)\s+(?P<who>.+)$",
               re.IGNORECASE),
    re.compile(r"^(?P<who>.+?)\s+(?:loot(?:\s+table)?|drops)$", re.IGNORECASE),
)
_WHERE_TAIL = re.compile(r"\s+(?:in|on|at)\s+(?:the\s+)?(?:\d+|heroic|normal|icc|toc|naxx|"
                         r"[A-Z][\w' -]*)$", re.IGNORECASE)
# The qualifiers a page title adds to a name (titled): the wiki's and the faction versions.
TITLE_QUALIFIERS = ("instance", "tactics", "Alliance", "Horde", "achievement", "subzone",
                    "kingdom", "lore", "quest")


def loot_target(query: str) -> str | None:
    """The boss or chest a loot question is about ("what drops from Sindragosa" -> "Sindragosa"),
    or None when the query is not one."""
    q = " ".join(query.split()).strip(" ?!.")
    for pat in _LOOT_QUESTIONS:
        m = pat.search(q)
        if m:
            who = _WHERE_TAIL.sub("", m.group("who").strip(" ?!.,\"")).strip()
            who = re.sub(r"'s?$", "", who)
            return who if len(who) >= 3 else None
    return None


class Source(Protocol):
    name: str

    def load(self, library: "Library") -> SourceResult: ...


@dataclass
class BuildReport:
    source: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def doc_text(title: str, section: str, text: str) -> str:
    """What gets embedded for a chunk: its heading, then its text."""
    return f"{title} - {section}\n{text}" if section else f"{title}\n{text}"


async def _embed(llm, model: str, texts: list[str], priority: Priority) -> list[list[float]]:
    """Through an LLMScheduler (anything with submit) at `priority`, or straight to an LLMClient."""
    if hasattr(llm, "submit"):
        return await llm.submit(priority, lambda c: c.embed(model, texts, dimensions=VEC_DIM))
    return await llm.embed(model, texts, dimensions=VEC_DIM)


def _scale(values: dict[int, float]) -> dict[int, float]:
    """BM25 strengths (positive) as fractions of the best. Not min-max: that pins the weakest
    candidate to 0 whatever its match, so with few candidates a chunk weight could never lift
    a passage over a lighter chunk that names the query once."""
    hi = max(values.values(), default=0.0)
    if hi <= 0:
        return {i: 1.0 for i in values}
    return {i: max(0.0, v) / hi for i, v in values.items()}


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Library:
    """The guild's reference shelf in library.db: build it from sources, embed it in the
    background, search it (hybrid vector + BM25, plain BM25 when vectors or the embedder are
    missing) and answer item, loot and quest-chain lookups. Times are clock unix seconds."""

    def __init__(self, db_path: Path, *, clock: Clock, llm=None,
                 embed_model: str = DEFAULT_EMBED_MODEL, readonly: bool = False):
        """`readonly`: search and look up only, as the mind does at runtime (db.open_library_db);
        building, embedding and set_meta then fail."""
        self.conn = open_library_db(Path(db_path), readonly=readonly)
        self.readonly = readonly
        self.clock = clock
        self.llm = llm
        self.embed_model = embed_model
        self._depth = 0

    # -- plumbing ----------------------------------------------------------------------

    @contextmanager
    def _tx(self):
        if self._depth:
            yield
            return
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._depth = 0

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        rows = self.conn.execute(sql, params).fetchall()
        if not self._depth:
            self.conn.commit()  # never pin a WAL snapshot between calls
        return rows

    def get_meta(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx():
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) "
                              "DO UPDATE SET value = excluded.value", (key, value))

    def close(self) -> None:
        try:
            if not self.readonly:
                checkpoint(self.conn)
        finally:
            self.conn.close()

    @staticmethod
    def _chunk(r: sqlite3.Row, score: float = 0.0) -> Chunk:
        return Chunk(id=r["id"], source=r["source"], kind=r["kind"], title=r["title"],
                     section=r["section"], text=r["text"], url=r["url"], license=r["license"],
                     map_id=r["map_id"], instance=r["instance"],
                     ref_ids=tuple(json.loads(r["ref_ids"])), t_created=r["t_created"],
                     score=score)

    def chunks_by_id(self, ids: Sequence[int]) -> list[Chunk]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        by = {r["id"]: r for r in self._all(f"SELECT * FROM chunks WHERE id IN ({marks})", ids)}
        return [self._chunk(by[i]) for i in ids if i in by]

    # -- build -------------------------------------------------------------------------

    def build(self, sources: Iterable[Source], *,
              progress: Callable[[str], None] | None = None) -> list[BuildReport]:
        """Load each source and replace its chunks. The world source runs first so AtlasLoot can
        cross-reference its items. A chunk whose key, heading and text are unchanged keeps its
        embedding."""
        reports = []
        for src in sorted(sources, key=lambda s: _SOURCE_ORDER.get(s.name, 9)):
            if progress:
                progress(f"{src.name}: loading")
            res = src.load(self)
            rep = self._apply(res)
            if progress:
                progress(f"{src.name}: {rep.total} chunks ({rep.inserted} new, {rep.updated} "
                         f"changed, {rep.unchanged} unchanged, {rep.deleted} removed)")
            reports.append(rep)
        return reports

    def _apply(self, res: SourceResult) -> BuildReport:
        if res.name not in SOURCES:
            raise ValueError(f"unknown source {res.name!r}")
        rep = BuildReport(res.name, log=list(res.log))
        now = int(self.clock.now())
        drafts: dict[str, ChunkDraft] = {}
        for d in res.chunks:
            if d.key in drafts:
                rep.log.append(f"duplicate key {d.key} ({d.title}): kept the first")
                continue
            drafts[d.key] = d
        with self._tx():
            c = self.conn
            if res.items is not None:
                c.execute("DELETE FROM item")
                c.executemany("INSERT INTO item(id, name, quality, ilvl, req_level, slot) "
                              "VALUES (?,?,?,?,?,?)",
                              [(i.id, clean(i.name), i.quality, i.ilvl, i.req_level, i.slot)
                               for i in res.items])
            if res.quests is not None:
                c.execute("DELETE FROM quest")
                c.executemany("INSERT INTO quest(id, title, level, zone, prev_id, next_id, "
                              "exclusive_group) VALUES (?,?,?,?,?,?,?)",
                              [(q.id, clean(q.title), q.level, clean(q.zone), q.prev_id,
                                q.next_id, q.exclusive_group) for q in res.quests])
            old = {r["key"]: r for r in c.execute(
                "SELECT id, key, kind, title, section, text, url, license, map_id, instance, "
                "ref_ids, weight FROM chunks WHERE source = ?", (res.name,))}
            for key, d in drafts.items():
                vals = (d.kind, clean(d.title), clean(d.section), clean(d.text),
                        d.url, d.license, d.map_id, None if d.instance is None else clean(d.instance),
                        json.dumps(list(d.ref_ids), ensure_ascii=False), float(d.weight))
                r = old.get(key)
                if r is None:
                    cid = c.execute(
                        "INSERT INTO chunks(source, key, kind, title, section, text, url, license, "
                        "map_id, instance, ref_ids, weight, t_created) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (res.name, key, *vals, now)).lastrowid
                    self._index_refs(cid, d.ref_ids)
                    rep.inserted += 1
                    continue
                have = (r["kind"], r["title"], r["section"], r["text"], r["url"], r["license"],
                        r["map_id"], r["instance"], r["ref_ids"], r["weight"])
                if have == vals:
                    rep.unchanged += 1
                    continue
                reembed = have[:4] != vals[:4]
                c.execute("UPDATE chunks SET kind=?, title=?, section=?, text=?, url=?, license=?, "
                          "map_id=?, instance=?, ref_ids=?, weight=?, t_created=?"
                          + (", embedding=NULL" if reembed else "") + " WHERE id=?",
                          (*vals, now, r["id"]))
                if reembed:
                    c.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (r["id"],))
                self._index_refs(r["id"], d.ref_ids)
                rep.updated += 1
            stale = [r["id"] for k, r in old.items() if k not in drafts]
            for i in range(0, len(stale), 500):
                part = stale[i:i + 500]
                marks = ",".join("?" * len(part))
                c.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({marks})", part)
                c.execute(f"DELETE FROM chunks WHERE id IN ({marks})", part)
            rep.deleted = len(stale)
            for k, v in ((f"built:{res.name}", str(now)), (f"count:{res.name}", str(rep.total))):
                c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) "
                          "DO UPDATE SET value = excluded.value", (k, v))
        return rep

    def _index_refs(self, cid: int, refs) -> None:
        self.conn.execute("DELETE FROM chunk_ref WHERE chunk_id = ?", (cid,))
        self.conn.executemany("INSERT OR IGNORE INTO chunk_ref(chunk_id, ref) VALUES (?, ?)",
                              [(cid, r) for r in refs])

    def known_refs(self, refs: Iterable[str]) -> set[str]:
        """The "item:<id>" and "quest:<id>" refs whose ids are in the item and quest tables (the
        world source's); refs of other kinds are never known."""
        out: set[str] = set()
        for table in ("item", "quest"):
            ids = sorted({int(r.split(":", 1)[1]) for r in refs if r.startswith(table + ":")})
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                marks = ",".join("?" * len(part))
                out |= {f"{table}:{r['id']}" for r in self._all(
                    f"SELECT id FROM {table} WHERE id IN ({marks})", part)}
        return out

    def item_info(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        """Rows of the item table (from the world source) by id; {} before a world build."""
        ids = list(dict.fromkeys(ids))
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            out.update({r["id"]: r for r in self._all(
                f"SELECT * FROM item WHERE id IN ({marks})", part)})
        return out

    # -- embedding ---------------------------------------------------------------------

    def _check_embed_meta(self, model: str) -> None:
        have_model, have_dim = self.get_meta("embed_model"), self.get_meta("embed_dim")
        if have_model is None and have_dim is None:
            self.set_meta("embed_model", model)
            self.set_meta("embed_dim", str(VEC_DIM))
        elif have_model != model or have_dim != str(VEC_DIM):
            raise EmbeddingMismatch(f"library.db was embedded with {have_model} ({have_dim}d), "
                                    f"asked to use {model} ({VEC_DIM}d)")

    def pending_embeddings(self) -> int:
        return self._all("SELECT count(*) AS n FROM chunks WHERE embedding IS NULL")[0]["n"]

    async def embed_pending(self, llm=None, *, model: str | None = None, batch: int = 32,
                            priority: Priority = Priority.BACKGROUND, limit: int | None = None,
                            progress: Callable[[str], None] | None = None) -> int:
        """Embed chunks that have no vector yet, `batch` texts per call, committing and yielding
        to the loop after each batch, so it can run in the background and resume where it
        stopped. `llm` is an LLMScheduler (the call queues at `priority`) or an LLMClient.
        Returns how many chunks were embedded; an embed error propagates after the batches
        already done are saved."""
        llm = llm if llm is not None else self.llm
        if llm is None:
            raise ValueError("embed_pending needs an LLM client or scheduler")
        model = model or self.embed_model
        self._check_embed_meta(model)
        done = 0
        total = self.pending_embeddings()
        while limit is None or done < limit:
            n = batch if limit is None else min(batch, limit - done)
            rows = self._all("SELECT id, kind, title, section, text FROM chunks "
                             "WHERE embedding IS NULL ORDER BY id LIMIT ?", (n,))
            if not rows:
                break
            vecs = await _embed(llm, model, [doc_text(r["title"], r["section"], r["text"])
                                             for r in rows], priority)
            if len(vecs) != len(rows) or any(len(v) != VEC_DIM for v in vecs):
                raise LLMError(f"embed returned {len(vecs)} vectors for {len(rows)} texts "
                               f"or a vector that is not {VEC_DIM}d")
            with self._tx():
                for r, v in zip(rows, vecs):
                    blob = pack_vec(v)
                    self.conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?",
                                      (blob, r["id"]))
                    self.conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (r["id"],))
                    self.conn.execute("INSERT INTO chunks_vec(chunk_id, kind, embedding) "
                                      "VALUES (?, ?, ?)", (r["id"], r["kind"], blob))
            done += len(rows)
            if progress:
                progress(f"embedded {done}/{total}")
            await asyncio.sleep(0)
        return done

    # -- search ------------------------------------------------------------------------

    def _kinds(self, kinds) -> list[str] | None:
        if kinds is None:
            return None
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            raise ValueError(f"unknown chunk kinds {bad}")
        return kinds

    def _has_vectors(self) -> bool:
        return bool(self._all("SELECT 1 FROM chunks WHERE embedding IS NOT NULL LIMIT 1"))

    def _fts(self, query: str, kinds: list[str] | None, n: int, *,
             leads: bool = False) -> list[tuple[int, float]]:
        """(id, bm25) of the n best matches by weighted bm25 (bm25 is negative: lower is
        better, so a weight under 1 moves a chunk down); with `leads`, only wiki pages' first
        chunks and the curated summaries that stand in for pages."""
        if not query:
            return []
        sql = ("SELECT c.id AS id, bm25(chunks_fts, 4.0, 2.0, 1.0) AS score, c.weight AS weight "
               "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
        params: list = [query]
        if kinds:
            sql += f" AND c.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if leads:
            sql += (" AND ((c.source = 'wiki' AND c.key GLOB 'wiki:*:0') "
                    "OR c.source = 'curated')")
        try:
            rows = self._all(sql + " ORDER BY score * weight LIMIT ?", (*params, n))
        except sqlite3.OperationalError as e:
            log.debug("library fts query rejected: %s", e)  # the query is someone's words
            self.conn.rollback()
            return []
        return [(r["id"], r["score"]) for r in rows]

    def _knn(self, qblob: bytes, kinds: list[str] | None, n: int) -> list[tuple[int, float]]:
        hits: list[tuple[int, float]] = []
        for kind in (kinds or [None]):
            sql = "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
            params: list = [qblob, n]
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            hits += [(r["chunk_id"], r["distance"]) for r in self._all(sql, params)]
        return sorted(hits, key=lambda h: h[1])[:n]

    def search_text(self, query: str, k: int = 5, kinds=None) -> list[Chunk]:
        """BM25 only; never calls the embedder. Loot questions go to drops_from() first, as in
        search()."""
        kinds = self._kinds(kinds)
        text = " ".join(query.split())
        routed = self._routed(text, kinds, k)
        return self._fill(routed, self._rank(text, None, k, kinds), k)

    def _routed(self, text: str, kinds: list[str] | None, k: int) -> list[Chunk]:
        """A loot question ("what drops from Sindragosa") answered by drops_from(): the boss's
        loot tables, the world database's first. [] for other queries or no such boss."""
        if not text or (kinds is not None and "loot" not in kinds):
            return []
        who = loot_target(text)
        return [replace(c, score=1.0) for c in self.drops_from(who)[:k]] if who else []

    @staticmethod
    def _fill(first: list[Chunk], rest: list[Chunk], k: int) -> list[Chunk]:
        seen = {c.id for c in first}
        return (first + [c for c in rest if c.id not in seen])[:k]

    async def search(self, query: str, k: int = 5, kinds=None, *, llm=None,
                     priority: Priority = Priority.SOCIAL) -> list[Chunk]:
        """The k best chunks for a question: vector and BM25 candidates scored together, then
        diversified. Falls back to BM25 alone when the library has no vectors, the stored
        vectors come from another model, there is no embedder or the embed call fails.

        Pass `kinds` whenever the question has a type: lore and history ("lore", "zone",
        "event"), fights ("strategy"), gear ("item", "loot"), quests ("quest"), classes
        ("class"). Without it, a name matches every item, quest and passage that carries it.
        Each chunk's weight scales its score, so trade goods, recipes, grey items and quotes
        rank below the passages and gear the guild asks about. A loot question ("what drops
        from Sindragosa", "Sindragosa loot") is answered by drops_from() first."""
        kinds = self._kinds(kinds)
        text = " ".join(query.split())
        if not text:
            return []
        routed = self._routed(text, kinds, k)
        if len(routed) >= k:
            return routed
        llm = llm if llm is not None else self.llm
        qvec = None
        if (llm is not None and self._has_vectors()
                and self.get_meta("embed_model") in (None, self.embed_model)):
            try:
                v = (await _embed(llm, self.embed_model, [QUERY_PREFIX + text], priority))[0]
                if len(v) != VEC_DIM:
                    raise ValueError(f"query vector has {len(v)} dims")
                qvec = [float(x) for x in v]
            except (LLMError, SchedulerError, ValueError, IndexError, TypeError,
                    struct.error) as e:
                log.warning("library query embed failed, using text search: %s", e)
        return self._fill(routed, self._rank(text, qvec, k, kinds), k)

    def _rank(self, text: str, qvec: list[float] | None, k: int,
              kinds: list[str] | None) -> list[Chunk]:
        q = fts_query(text)
        fts = dict(self._fts(q, kinds, _CANDIDATES)) if q else {}
        for i, s in (self._fts(q, kinds, _LEADS, leads=True) if q else []):
            fts.setdefault(i, s)
        terms = set(_TERM.findall(q or ""))
        knn: dict[int, float] = {}
        if qvec is not None:
            try:
                knn = dict(self._knn(pack_vec(qvec), kinds, _CANDIDATES))
            except (sqlite3.OperationalError, ValueError) as e:
                log.warning("library vector search failed, using text search: %s", e)
                qvec = None
        ids = list(dict.fromkeys([*knn, *fts]))
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in self._all(f"SELECT * FROM chunks WHERE id IN ({marks})", ids)}
        bm = _scale({i: -s for i, s in fts.items()})
        vecs: dict[int, list[float]] = {}
        score: dict[int, float] = {}
        for i, r in rows.items():
            if r["embedding"] is not None:
                vecs[i] = unpack_vec(r["embedding"])
            if qvec is not None:
                cos = max(0.0, cosine(qvec, vecs[i])) if i in vecs else 0.0
                score[i] = (0.5 * bm.get(i, 0.0) + 0.5 * cos) * r["weight"]
            else:
                score[i] = bm.get(i, 0.0) * r["weight"]
            lead = r["source"] == "curated" or (r["source"] == "wiki" and r["key"].endswith(":0"))
            if (terms and lead
                    and terms <= set(_TITLE_WORD.findall(r["title"].casefold()))):
                score[i] *= _LEAD_BONUS  # the page about it: "Arthas" -> Arthas Menethil

        def sim(a: int, b: int) -> float:
            if a in vecs and b in vecs:
                return cosine(vecs[a], vecs[b])
            return word_jaccard(rows[a]["text"], rows[b]["text"])

        order = sorted(rows, key=lambda i: (-score[i], i))
        picked = mmr(order, score, sim, k, lam=0.75)
        return [self._chunk(rows[i], score[i]) for i in picked]

    # -- lookups -----------------------------------------------------------------------

    def _title_words_query(self, text: str) -> str:
        words = quote_words(text)
        return "{title} : (" + words.replace(" OR ", " AND ") + ")" if words else ""

    def _title_matches(self, kind: str, name: str, limit: int = 50) -> list[int]:
        q = self._title_words_query(name)
        if not q:
            return []
        try:
            rows = self._all(
                "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? AND c.kind = ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (q, kind, limit))
        except sqlite3.OperationalError:
            self.conn.rollback()
            return []
        return [r["id"] for r in rows]

    def lookup_item(self, name_or_id) -> list[Chunk]:
        """Item chunks by id, item link (|Hitem:<id>|), exact name (any case, every item that
        shares it, highest item level first) or, failing that, all words of the name."""
        s = str(name_or_id).strip()
        m = _LINK_ID.search(s)
        if isinstance(name_or_id, int) or s.isdigit() or m:
            ids = [int(m.group(1)) if m else int(s)]
        else:
            name = " ".join(s.strip("[]").split())
            ids = [r["id"] for r in self._all(
                "SELECT id FROM item WHERE name = ? COLLATE NOCASE ORDER BY ilvl DESC, id",
                (name,))]
            if not ids:
                hits = self._title_matches("item", name)
                return self.chunks_by_id(hits)
        keys = [f"item:{i}" for i in ids]
        marks = ",".join("?" * len(keys))
        by = {r["key"]: r for r in self._all(
            f"SELECT * FROM chunks WHERE kind = 'item' AND key IN ({marks})", keys)}
        return [self._chunk(by[k]) for k in keys if k in by]

    def drops_from(self, boss_name: str) -> list[Chunk]:
        """Loot chunks for a boss or chest: exact title (any case), else titles containing the
        name, else all of its words; the world database's first, then AtlasLoot's."""
        name = " ".join(boss_name.split())
        if not name:
            return []
        order = (" ORDER BY CASE source WHEN 'world' THEN 0 WHEN 'atlasloot' THEN 1 ELSE 2 END, "
                 "map_id, key")
        rows = self._all("SELECT * FROM chunks WHERE kind = 'loot' AND title = ? COLLATE NOCASE"
                         + order, (name,))
        if not rows:
            rows = self._all("SELECT * FROM chunks WHERE kind = 'loot' AND title LIKE ? "
                             "ESCAPE '\\'" + order, (f"%{_like_escape(name)}%",))
        if not rows:
            return self.chunks_by_id(self._title_matches("loot", name))
        return [self._chunk(r) for r in rows]

    def titled(self, names: Iterable[str], kinds=None, *, limit: int = 60) -> list[Chunk]:
        """The chunks one of `names` titles (any case), also as "The <name>" or with a
        qualifier (TITLE_QUALIFIERS: "Icecrown Citadel (instance)", "The Argent Tournament
        (Alliance)"), in `kinds`, in id order (page order), at most `limit`. Exact titles only:
        "Emblem of Frost" never brings "Emblem of Frost Rewards"."""
        kinds = self._kinds(kinds) or list(KINDS)  # the kind leads the (kind, title) index
        base = list(dict.fromkeys(" ".join(n.split()) for n in names if n and n.strip()))
        forms = list(dict.fromkeys(
            form for n in base for t in (n, f"The {n}")
            for form in (t, *(f"{t} ({q})" for q in TITLE_QUALIFIERS))))
        rows: dict[int, sqlite3.Row] = {}
        for i in range(0, len(forms), 400):
            part = forms[i:i + 400]
            sql = (f"SELECT * FROM chunks WHERE kind IN ({','.join('?' * len(kinds))}) AND title "
                   f"COLLATE NOCASE IN ({','.join('?' * len(part))}) ORDER BY id LIMIT ?")
            rows.update((r["id"], r) for r in self._all(sql, (*kinds, *part, limit)))
        return [self._chunk(rows[i]) for i in sorted(rows)[:limit]]

    def strategy_for(self, boss_name: str, instance: str | None = None) -> list[Chunk]:
        """A boss's tactics page, its strategy chunks in page order: "<name> (tactics)" or
        "<name> (... tactics)" (any case, with or without a leading "The"), else the page
        titled with the name (an NPC page may carry an ability list), else a title ending in
        the name ("Icecrown Gunship Battle"). With `instance`, a page of that instance (or of
        none) wins over another's: Anub'arak in Trial of the Crusader, not in Azjol-Nerub. []
        when there is none."""
        name = re.sub(r"^the\s+", "", " ".join(boss_name.split()), flags=re.IGNORECASE)
        if not name:
            return []
        esc = _like_escape(name)
        rows = self._all("SELECT * FROM chunks WHERE kind = 'strategy' AND (title COLLATE NOCASE "
                         "IN (?, ?) OR title LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' "
                         "OR title LIKE ? ESCAPE '\\')",
                         (name, f"The {name}", f"{esc} (%tactics)", f"The {esc} (%tactics)",
                          f"% {esc}"))
        pages: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            pages.setdefault(r["key"].rsplit(":", 1)[0], []).append(r)
        if not pages:
            return []
        forms = (name.casefold(), f"the {name.casefold()}")

        def rank(page: list[sqlite3.Row]):
            title, where = page[0]["title"].casefold(), page[0]["instance"]
            tier = (0 if title.startswith(tuple(f"{f} (" for f in forms))
                    else 1 if title in forms else 2)
            elsewhere = instance is not None and where is not None and where != instance
            return elsewhere, tier, instance is None or where != instance, page[0]["id"]

        def part(r: sqlite3.Row) -> int:
            tail = r["key"].rsplit(":", 1)[-1]
            return int(tail) if tail.isdigit() else 0
        return [self._chunk(r) for r in sorted(min(pages.values(), key=rank), key=part)]

    def quest_chain(self, title: str, limit: int = 40) -> list[Chunk]:
        """The quest chain through the quest with this title (any case): back along the
        required previous quests to the start, then forward through the quests offered next
        (or, without one, the first quest that requires it)."""
        name = " ".join(title.split())
        rows = self._all("SELECT * FROM quest WHERE title = ? COLLATE NOCASE ORDER BY id", (name,))
        if not rows:
            rows = self._all("SELECT * FROM quest WHERE title LIKE ? ESCAPE '\\' ORDER BY id",
                             (f"%{_like_escape(name)}%",))
        if not rows:
            return []
        quests = {r["id"]: r for r in self._all("SELECT * FROM quest")}
        start = rows[0]["id"]
        back = [start]
        while (p := quests[back[0]]["prev_id"]) and p in quests and p not in back \
                and len(back) < limit:
            back.insert(0, p)
        chain = list(back)
        followers: dict[int, list[int]] = {}
        for q in quests.values():
            if q["prev_id"]:
                followers.setdefault(q["prev_id"], []).append(q["id"])
        cur = start
        while len(chain) < limit:
            nxt = quests[cur]["next_id"]
            if not nxt or nxt not in quests:
                nxt = min(followers.get(cur, []), default=0)
            if not nxt or nxt in chain:
                break
            chain.append(nxt)
            cur = nxt
        keys = [f"quest:{i}" for i in chain]
        marks = ",".join("?" * len(keys))
        by = {r["key"]: r for r in self._all(
            f"SELECT * FROM chunks WHERE kind = 'quest' AND key IN ({marks})", keys)}
        return [self._chunk(by[k]) for k in keys if k in by]

    def stats(self) -> dict:
        out: dict = {"chunks": {}, "embedded": 0, "pending": 0}
        for r in self._all("SELECT source, kind, count(*) AS n, sum(embedding IS NOT NULL) AS e "
                           "FROM chunks GROUP BY source, kind ORDER BY source, kind"):
            out["chunks"][f"{r['source']}/{r['kind']}"] = r["n"]
            out["embedded"] += r["e"]
            out["pending"] += r["n"] - r["e"]
        out["items"] = self._all("SELECT count(*) AS n FROM item")[0]["n"]
        out["quests"] = self._all("SELECT count(*) AS n FROM quest")[0]["n"]
        out["meta"] = {r["key"]: r["value"] for r in self._all("SELECT * FROM meta ORDER BY key")}
        return out
