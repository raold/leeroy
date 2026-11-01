import logging
import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Literal

from leeroy_mind.clock import Clock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.client import LLMClient, LLMError
from leeroy_mind.llm.scheduler import LLMScheduler, Priority, SchedulerError
from leeroy_mind.memory.privacy import derive_scope, with_request
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.rows import MemoryRow, cosine, pack_vec, unpack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.text import normalize_words, strip_wow_markup, truncate_words

log = logging.getLogger("leeroy_mind")

MAX_TEXT = 200
OWNER_REF = "owner:"  # evidence of the owner's own memories: "owner:<moment>"
_CHAT_REF = re.compile(r"chat:([0-9]+)")


@dataclass(frozen=True)
class MemoryCandidate:
    owner: int
    kind: str
    text: str
    t_event: int
    actors: tuple[int, ...]
    importance: int
    valence: float
    source: str
    evidence: tuple[str, ...]
    confidence: float = 1.0
    place: str | None = None
    boss_id: int | None = None
    raid_id: int | None = None
    key: str | None = None
    t_created: int | None = None  # when it was made (and last touched); None: the clock's now
    scope: str = "open"  # the caller's request: the writer may make it stricter, never weaker
    scope_with: int = 0


@dataclass(frozen=True)
class WriteResult:
    id: int | None
    action: Literal["inserted", "merged", "updated", "rate_limited", "rejected"]


class Embedder:
    def __init__(self, scheduler: LLMScheduler, model: str, dim: int = 512):
        self._scheduler = scheduler
        self._model = model
        self._dim = dim

    async def embed(self, texts: list[str], priority: Priority) -> list[bytes]:
        async def job(llm: LLMClient):
            return await llm.embed(self._model, list(texts), dimensions=self._dim)
        vecs = await self._scheduler.submit(priority, job)
        return [pack_vec(v) for v in vecs]


def clean_memory_text(text: str, human: HumanIdentity | None) -> str:
    """A memory's text as the writer stores it: markup stripped, whitespace collapsed, the
    owner's real name replaced by what the guild calls him, at most MAX_TEXT characters."""
    text = " ".join(strip_wow_markup(text).split())
    if human is not None:
        text = redact_real_name(text, human, human.called)
    return truncate_words(text, MAX_TEXT)


class MemoryWriter:
    """Validates, limits, embeds, dedups and stores memories, each with the scope its evidence
    derives (scope_of). Every time here is mind-clock seconds. `human` (optional) screens the
    owner's real name out of the text."""

    PARK_S = 3600.0  # a memory Ollama cannot embed is left alone this long before another try
    _PROBE = "guild"  # a text any embedder can embed: tells an outage from one bad text

    def __init__(self, store: Store, embedder: Embedder, clock: Clock, *, per_hour: int = 20,
                 per_day: int = 120, dedup_cos: float = 0.92, dedup_window_h: float = 48,
                 human: Callable[[], HumanIdentity | None] | None = None):
        self._store = store
        self._embedder = embedder
        self._clock = clock
        self._per_hour = per_hour
        self._per_day = per_day
        self._dedup_cos = dedup_cos
        self._dedup_window = dedup_window_h * 3600
        self._human = human
        self._parked: dict[int, float] = {}  # memory id -> when to try it again
        self._backfill_failing = False

    def _clean_text(self, text: str) -> str:
        return clean_memory_text(text, self._human() if self._human is not None else None)

    def _limited(self, owner: int, now: int) -> bool:
        # "within the last hour/day" is exclusive of the boundary second
        s = self._store
        return (s.count_inserted_since(owner, now - 3600 + 1) >= self._per_hour
                or s.count_inserted_since(owner, now - 86400 + 1) >= self._per_day)

    def scope_of(self, owner: int, evidence: Iterable[str], *, shown: Iterable[str] = (),
                 scope: str = "open", scope_with: int = 0) -> tuple[str, int]:
        """The (scope, scope_with) a memory of `owner` citing `evidence` is stored with
        (controller ruling B2): what its evidence, and what its prompt showed beyond it
        (`shown`), derive (privacy.derive_scope), raised by the caller's request but never
        lowered (privacy.with_request). It reads inside a store transaction, which nests, so
        any caller may use it."""
        with self._store.transaction():
            derived = derive_scope(self._store.conn, owner, evidence, shown=shown)
        if scope == "secret" and derived.public:
            log.info("scope_secret_refused: owner=%d", owner)
        return with_request(derived, scope, scope_with)

    def _scoped(self, row: MemoryRow, scope: str, scope_with: int,
                shown: Iterable[str] = ()) -> MemoryRow:
        scope, scope_with = self.scope_of(row.owner, row.evidence, shown=shown, scope=scope,
                                          scope_with=scope_with)
        return replace(row, scope=scope, scope_with=scope_with)

    def put(self, row: MemoryRow, *, shown: Iterable[str] = ()) -> int:
        """Store `row` with the scope scope_of gives it, the row's own scope and scope_with
        being the request and `shown` what its prompt showed beyond its evidence; through
        upsert_keyed_memory when it has a key. Returns the id. The night's write path (P1c):
        synchronous, joining the caller's transaction, with no rate limit, dedup or text screen
        (the caller screens the text)."""
        store = self._store
        with store.transaction():
            row = self._scoped(row, row.scope, row.scope_with, shown)
            if row.key is not None:
                return store.upsert_keyed_memory(row)
            return store.insert_memory(row)

    async def write(self, c: MemoryCandidate, *, enforce_limits: bool = True) -> WriteResult:
        store = self._store
        text = self._clean_text(c.text)
        if not text:
            return WriteResult(None, "rejected")
        if c.owner != 0 and store.get_persona_json(c.owner) is None:
            return WriteResult(None, "rejected")
        if c.source != "game" and not c.evidence:
            return WriteResult(None, "rejected")
        if c.source != "owner" and any(str(e).startswith(OWNER_REF) for e in c.evidence):
            return WriteResult(None, "rejected")  # only the owner's own history cites "owner:"
        now = int(self._clock.now())
        made = now if c.t_created is None else int(c.t_created)
        row = MemoryRow(
            id=None, owner=c.owner, kind=c.kind, text=text, t_event=int(c.t_event), t_created=made,
            t_accessed=made, access_n=0, actors=tuple(sorted(set(c.actors))), place=c.place,
            boss_id=c.boss_id, raid_id=c.raid_id, importance=max(1, min(10, int(c.importance))),
            valence=max(-1.0, min(1.0, float(c.valence))), source=c.source,
            evidence=tuple(c.evidence), confidence=c.confidence, key=c.key)
        if c.key is not None:
            with store.transaction():
                row = self._scoped(row, c.scope, c.scope_with)
                return WriteResult(store.upsert_keyed_memory(row), "updated")
        if enforce_limits and self._limited(c.owner, now):
            return WriteResult(None, "rate_limited")
        vec: bytes | None = None
        try:
            vec = (await self._embedder.embed([text], Priority.EXTRACTION))[0]
        except (LLMError, SchedulerError) as e:
            log.warning("memory embed failed, storing without a vector: %s", e)
        # No await below: the checks, the scope and the insert are one transaction and one step
        # for the event loop.
        with store.transaction():
            if enforce_limits and self._limited(c.owner, int(self._clock.now())):
                return WriteResult(None, "rate_limited")
            row = self._scoped(row, c.scope, c.scope_with)
            twin = self._find_twin(row, vec)
            if twin is not None:
                store.merge_memory(twin, row.evidence, row.importance)
                return WriteResult(twin, "merged")
            return WriteResult(store.insert_memory(replace(row, embedding=vec)), "inserted")

    def _find_twin(self, row: MemoryRow, vec: bytes | None) -> int | None:
        """The earlier memory `row` duplicates, if any. Only one of the same privacy class
        (_privacy_class) and the same scope counts, so a merge never makes whispered text
        public or hands it to another partner."""
        since = row.t_created - self._dedup_window + 1
        best_id, best = None, 0.0
        mine = unpack_vec(vec) if vec is not None else None
        cands = [c for c in self._store.recent_similar_candidates(row.owner, row.kind, row.actors,
                                                                  since) if c.key is None]
        parties = self._store.chat_line_parties(
            i for ev in (row.evidence, *(c.evidence for c in cands)) for i in _chat_ids(ev))
        mine_class = _privacy_class(row.evidence, parties)
        for cand in cands:
            if (_privacy_class(cand.evidence, parties) != mine_class
                    or (cand.scope, cand.scope_with) != (row.scope, row.scope_with)):
                continue
            if row.source != "game" and mine is not None and cand.embedding is not None:
                score = cosine(mine, unpack_vec(cand.embedding))
                if score >= self._dedup_cos and score > best:
                    best_id, best = cand.id, score
            elif normalize_words(cand.text) == normalize_words(row.text) and best < 1.0:
                best_id, best = cand.id, 1.0
        return best_id

    async def _embed_one(self, text: str) -> bytes | None:
        try:
            return (await self._embedder.embed([text], Priority.BACKGROUND))[0]
        except (LLMError, SchedulerError):
            return None

    async def embed_pending(self, limit: int = 32) -> int:
        """Vectors for memories stored without one (keyed rows, an embed that failed), oldest
        first. When a batch fails and Ollama still embeds a probe text, the rows are tried one
        at a time and one that fails alone is parked for PARK_S, so no text Ollama cannot embed
        holds up the rest. When the probe fails too it is an outage: nothing is parked, and it
        is logged once until the backfill works again."""
        now = self._clock.now()
        self._parked = {i: t for i, t in self._parked.items() if t > now}
        rows = self._store.memories_missing_embedding(limit, exclude=self._parked)
        if not rows:
            return 0
        try:
            vecs = await self._embedder.embed([r.text for r in rows], Priority.BACKGROUND)
            pairs = list(zip(rows, vecs))
        except (LLMError, SchedulerError) as e:
            if await self._embed_one(self._PROBE) is None:
                if not self._backfill_failing:
                    self._backfill_failing = True
                    log.warning("embedding backfill failed: %s", e)
                return 0
            pairs = []
            for r in rows:  # one at a time: a passing failure gets its second try here
                v = await self._embed_one(r.text)
                if v is None:
                    self._parked[r.id] = now + self.PARK_S
                    log.warning("embedding_parked: memory=%d retry_s=%d", r.id, self.PARK_S)
                else:
                    pairs.append((r, v))
        if self._backfill_failing:
            self._backfill_failing = False
            log.info("embedding backfill works again")
        # A keyed row may have been rewritten during the await; skip any whose text changed or
        # that got a vector meanwhile, so it stays pending. No await below.
        fresh = {m.id: m for m in self._store.get_memories([r.id for r, _ in pairs])}
        done = 0
        for r, v in pairs:
            now = fresh.get(r.id)
            if now is None or now.embedding is not None or now.text != r.text:
                continue
            self._store.set_embedding(r.id, v)
            done += 1
        return done


def _chat_ids(evidence) -> list[int]:
    hits = (_CHAT_REF.fullmatch(e) for e in evidence if isinstance(e, str))
    return [int(h.group(1)) for h in hits if h]


def _privacy_class(evidence, parties: dict[int, tuple[str, int, int]]):
    """"public" when the evidence cites no stored chat line or any line that is not a whisper,
    else the whisper pairs it cites: the classes recall's interim whisper rule tells apart."""
    cited = [parties[i] for i in _chat_ids(evidence) if i in parties]
    if not cited or any(ch != "whisper" for ch, _, _ in cited):
        return "public"
    return frozenset(frozenset((a, b)) for _, a, b in cited)
