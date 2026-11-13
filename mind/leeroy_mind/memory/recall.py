import logging
import math
import re
import struct
from datetime import date, datetime, tzinfo
from typing import Sequence

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.discord.channels import (OFFICER_GAME, is_discord, is_officer_channel,
                                          is_private, is_restricted)
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import Priority, SchedulerError
from leeroy_mind.memory.privacy import OFFICERS, restricted_channels
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.retrieval import mmr, vector_or_word_similarity  # mmr remains a public import
from leeroy_mind.storage.rows import MemoryRow, cosine, unpack_vec
from leeroy_mind.storage.store import VEC_DIM, Store
from leeroy_mind.storage.store import fts_query as _safe_fts_query
from leeroy_mind.text import strip_wow_markup

log = logging.getLogger("leeroy_mind")

_WORD = re.compile(r"\w+", re.UNICODE)
_CHAT_REF = re.compile(r"chat:([0-9]+)")
OWNER_REF = "owner:"  # owner-authored evidence (the guild's history): public, not a chat line
# What a private or secret memory's prompt line ends with (memory.privacy).
PRIVATE_TAIL = " (keep it between you two)"
OFFICERS_TAIL = " (officers only)"  # one the officers may know (privacy.OFFICERS)
# A memory a live prompt brought up rests this long: it comes back only when nothing else
# fills the prompt (final fix wave, prompts I3: the same five memories won every query).
REST_S = 1800.0
REST_PENALTY = 10.0
HEARD = 0.8  # a memory held below this confidence is hearsay (format_memory: "(heard)")
HOLDER_HITS = 40  # how many matches Recall.holders reads, best first
_MAX_TERMS = 8
_MIN_LEN = 3
_STOPWORDS = frozenset(
    "the and for you your are was were but not with have has had this that these those from they "
    "them then than there their what when where who whom why how can could would should will "
    "just about into over out all any one our its it's his her she him let get got did does "
    "dont don't yes yeah lol".split())


def fts_query(text: str, terms: int = _MAX_TERMS) -> str | None:
    """A safe FTS5 query from chat text: markup stripped, stopwords and words under three
    characters dropped, repeated words once, at most `terms` terms (eight for a line),
    OR-joined and quoted by Store's fts_query. None when nothing searchable is left."""
    seen: list[str] = []
    for w in _WORD.findall(strip_wow_markup(text).casefold()):
        if len(w) >= _MIN_LEN and w not in _STOPWORDS and w not in seen:
            seen.append(w)
            if len(seen) == terms:
                break
    return _safe_fts_query(" ".join(seen)) or None


def chat_ids(evidence) -> list[int]:
    """The chat line ids among evidence refs ("chat:<id>")."""
    hits = (_CHAT_REF.fullmatch(e) for e in evidence if isinstance(e, str))
    return [int(h.group(1)) for h in hits if h]


def whisper_ok(channel: str, frm: int, to: int, *, speaker: int, turn_channel: str,
               to_guid: int) -> bool:
    """Whether one chat line may reach a turn by `speaker` on `turn_channel` (going to
    `to_guid`), as the conversation she is in (one mind per member, 2026-11-12):
    - a private line (a whisper or a Discord DM) reaches a private turn between the same two in
      either medium: a whisper and a DM between her and him are one conversation;
    - any other Discord line reaches only a turn on that same channel, and a Discord turn no
      other game line: a public conversation keeps to its own place (R11);
    - the officers' game lines (officer chat, their voice room) reach only the officers' game
      turns;
    - every other game line reaches every game turn, as before.
    Memories are kept to their audience through their scope (scope_ok), and their evidence
    still goes through this under the scope as a second check (Recall._shareable; controller
    ruling B1)."""
    if is_private(channel):
        return is_private(turn_channel) and {frm, to} == {speaker, to_guid}
    if is_discord(channel) or is_discord(turn_channel):
        return channel == turn_channel
    if channel in OFFICER_GAME:
        return turn_channel in OFFICER_GAME
    return True


def scope_ok(m: MemoryRow, *, turn_channel: str, to_guid: int) -> bool:
    """Whether a memory's scope lets it into a turn's prompt (controller rulings N4 and B1). An
    open memory reaches every turn. A private or secret one counts as a whisper between its
    owner and its partner, so it reaches only a private turn (a whisper, or a Discord DM: one
    conversation) from her to that partner, and one with no partner (scope_with 0) reaches
    none. A private one the officers may know (privacy.OFFICERS) reaches the officers' turns in
    either medium. A bond moment is a pair's: a whisper or a DM to her partner only."""
    if (getattr(m, 'key', None) or '').startswith('bond:') and not is_private(turn_channel):
        return False
    if m.scope == "open":
        return True
    if m.scope_with == OFFICERS:
        return m.scope == "private" and is_officer_channel(turn_channel)
    if (peer := _dm_peer(turn_channel)) is not None and peer not in (m.owner, m.scope_with):
        return False  # a DM is its member's own: never another member's
    return (m.scope_with != 0 and is_private(turn_channel)
            and {m.owner, m.scope_with} == {m.owner, to_guid})


def _dm_peer(channel) -> int | None:
    """The member of a Discord DM turn channel (`dm:<guid>`); None for any other channel, and -1
    for a malformed one (it matches nobody)."""
    if not isinstance(channel, str) or not channel.startswith("dm:"):
        return None
    tail = channel[3:]
    return int(tail) if tail.isdigit() else -1


def hidden_ok(channels, turn_channel: str) -> bool:
    """Whether a memory resting on lines of these restricted channels may reach a turn on
    `turn_channel`: the officers' in either medium reach every officers' turn; any other (a
    Discord channel the layout lacks) only a turn on that one channel (rulings R7, R10)."""
    channels = frozenset(channels)
    if channels == {turn_channel}:
        return True
    return is_officer_channel(turn_channel) and all(is_officer_channel(c) for c in channels)


def recency(t_accessed: float, now: float, tau_days: float) -> float:
    return math.exp(-max(0.0, now - t_accessed) / 86400 / tau_days)


def resting(m: MemoryRow, now: float) -> bool:
    """A memory a live prompt used within REST_S."""
    return m.access_n > 0 and now - m.t_accessed < REST_S


def format_memory(m: MemoryRow, now: float, *, tz: tzinfo) -> str:
    """One prompt line: how long ago it happened (memory_age), then the text. `tz` is the
    mind clock's zone (clock.local().tzinfo), required so the host's zone never leaks in;
    Recall.format passes it. A game memory (events.py's templates, whose stored text stays as
    written) reads "3 times" for "3\u00d7", and one from before tonight loses the "tonight" the
    template baked in, since the age says when (prompts lens, minor 4). A private or secret
    memory ends with PRIVATE_TAIL."""
    when = memory_age(m.t_event, now, tz)
    text = m.text
    if m.kind == "event":
        text = text.replace("\u00d7", " times")
        if night_of(datetime.fromtimestamp(m.t_event, tz)) != night_of(
                datetime.fromtimestamp(now, tz)):
            text = _BAKED_TONIGHT.sub("", text).strip()
            text = text[:1].upper() + text[1:]
    tail = " (heard)" if m.confidence < HEARD else ""
    if m.scope != "open":
        tail += OFFICERS_TAIL if m.scope_with == OFFICERS else PRIVATE_TAIL
    return f"- {when}: {text}{tail}"


_BAKED_TONIGHT = re.compile(r"^Tonight: |(?<=\w) tonight(?=[.!]?$)")
_NUMBERS = ("no", "a", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
            "eleven", "twelve")


def _count(n: int, unit: str) -> str:
    """ "an hour", "two days", "13 days"."""
    if n == 1:
        return f"{'an' if unit[0] in 'aeiou' or unit == 'hour' else 'a'} {unit}"
    return f"{_NUMBERS[n] if n < len(_NUMBERS) else n} {unit}s"


def memory_age(t_event: float, now: float, tz: tzinfo) -> str:
    """When something happened, as people say it, at every scale: "just now", "five minutes
    ago", "two hours ago" (the same night), "yesterday" (by day) or "last night", "three days
    ago", "a week ago", "two weeks ago", "a month ago", "six months ago", "about a year ago",
    "about two years ago". Nights run 06:00 to 06:00 (clock.night_of) in the clock's zone."""
    age = max(0.0, now - t_event)
    if age < 90:
        return "just now"
    if age < 3600:
        return f"{_count(int(age // 60), 'minute')} ago"
    then, here = datetime.fromtimestamp(t_event, tz), datetime.fromtimestamp(now, tz)
    nights = (date.fromisoformat(night_of(here)) - date.fromisoformat(night_of(then))).days
    if nights <= 0:
        return f"{_count(int(age // 3600), 'hour')} ago"
    if nights == 1:
        return "yesterday" if 6 <= then.hour < 17 else "last night"
    if nights < 7:
        return f"{_count(nights, 'day')} ago"
    if nights < 14:
        return "a week ago"
    if nights < 30:
        return f"{_count(nights // 7, 'week')} ago"
    if nights < 335:
        return f"{_count(max(1, round(nights / 30.44)), 'month')} ago"
    years = nights / 365.25
    return "about a year ago" if years < 1.5 else f"about {_count(round(years), 'year')} ago"


def _spread(values: dict[int, float]) -> dict[int, float]:
    """Min-max to 0..1; 0.5 for everyone when all values are equal."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {i: 0.5 for i in values}
    return {i: (v - lo) / (hi - lo) for i, v in values.items()}


class Recall:
    """Picks the memories worth saying out loud: vector, full-text and who-is-here candidates,
    scored on recency, importance, relevance and presence, then diversified. Times are
    mind-clock seconds. If the query cannot be embedded it falls back to text and recency."""

    def __init__(self, store: Store, embedder: Embedder, clock: Clock, *, tau_days: float):
        self._store = store
        self._embedder = embedder
        self._clock = clock
        self.tau_days = tau_days

    def set_tau(self, tau_days: float) -> None:
        """A reloaded mind.toml's memory depth (TalkEngine.apply_config)."""
        if not tau_days > 0:
            raise ValueError(f"memory depth must be > 0 days, got {tau_days!r}")
        self.tau_days = tau_days

    async def _embed_query(self, text: str, priority: Priority) -> bytes | None:
        if not text.strip():
            return None
        try:
            vec = (await self._embedder.embed([text], priority))[0]
            if len(unpack_vec(vec)) != VEC_DIM:
                raise ValueError(f"query vector has {len(vec)} bytes")
            return vec
        except (LLMError, SchedulerError, ValueError, IndexError, TypeError, struct.error) as e:
            log.warning("recall query embed failed, using text and recency: %s", e)
            return None

    def format(self, m: MemoryRow) -> str:
        return format_memory(m, self._clock.now(), tz=self._clock.local().tzinfo)

    def _shareable(self, rows: list[MemoryRow], owner: int, channel: str,
                   to_guid: int, *, include_bond: bool = False) -> list[MemoryRow]:
        """P1b's interim whisper rule, kept under the memory scope as a second check
        (controller ruling B1). A memory whose stored chat lines are all private (whispers or
        Discord DMs) stays out of a turn on any other channel, and out of a private turn unless
        every one of them may reach it (whisper_ok: between `owner` and `to_guid`, in a whisper
        or a DM alike). A memory resting on a line of a restricted channel, directly or through
        memory: refs (privacy.restricted_channels), comes up only where hidden_ok allows: an
        officers' one in every officers' turn of either medium, any other only in a turn on that
        channel, owner refs or not (rulings R7, R10). A memory with a
        public line, an owner-authored ref (OWNER_REF: public, non-chat evidence) or no stored
        chat line (a game event) stays. One store query for all of `rows`, and one walk more
        only for rows that cite other memories."""
        refs = {m.id: chat_ids(m.evidence) for m in rows}
        lines = self._store.chat_line_parties(i for ids in refs.values() for i in ids)
        hidden = {m.id: frozenset(ch for i in refs[m.id] if i in lines
                                  and is_restricted(ch := lines[i][0])) for m in rows}
        nested = {m.id: [e for e in m.evidence if isinstance(e, str) and e.startswith("memory:")]
                  for m in rows}
        if nested := {k: v for k, v in nested.items() if v}:  # only these need the walk
            with self._store.transaction():
                for k, found in restricted_channels(self._store.conn, nested).items():
                    hidden[k] |= found
        hidden = {k: v for k, v in hidden.items() if v}
        keep = []
        for m in rows:
            if not scope_ok(m, turn_channel=channel, to_guid=to_guid):
                continue
            if (m.key or '').startswith('bond:') and (not include_bond or m.owner != owner):
                continue
            cited = [lines[i] for i in refs[m.id] if i in lines]
            if m.id in hidden and not hidden_ok(hidden[m.id], channel):
                continue
            if any(isinstance(e, str) and e.startswith(OWNER_REF) for e in m.evidence):
                keep.append(m)
                continue
            if cited and all(is_private(ch) for ch, _, _ in cited) and not all(
                    whisper_ok(ch, a, b, speaker=owner, turn_channel=channel, to_guid=to_guid)
                    for ch, a, b in cited):
                continue
            keep.append(m)
        if len(keep) < len(rows):
            log.debug("recall_private_excluded: n=%d", len(rows) - len(keep))
        return keep

    def _not_own_lines(self, rows: list[MemoryRow], owner: int) -> list[MemoryRow]:
        """A memory of the speaker's own lines from the session now open stays out of her
        recall: her prompt already shows them under "Your last lines", and a memory of them
        had her say them again (dress rehearsal 2, judge A fix 4). Only an extracted memory
        whose every cited line is hers and from this session; afterwards it is history and
        comes back. Her Discord lines carry no session (they never touch his game session), so
        one of hers in Discord since the session opened counts as this session's. One store
        query for all of `rows`, none outside a session."""
        sid = self._store.open_session()
        if sid is None:
            return rows
        started = self._store.session_started(sid)
        refs = {m.id: ids for m in rows if m.source == "extract" and (ids := chat_ids(m.evidence))}
        if not refs:
            return rows
        lines = {ln.id: ln for ln in self._store.chat_lines(
            dict.fromkeys(i for ids in refs.values() for i in ids))}
        def now(ln) -> bool:
            return ln.session_id == sid or (ln.session_id is None and is_discord(ln.channel)
                                            and started is not None and ln.ts >= started)
        own = {mid for mid, ids in refs.items()
               if all(i in lines and lines[i].from_guid == owner and now(lines[i]) for i in ids)}
        if own:
            log.debug("recall_own_lines_excluded: n=%d", len(own))
        return [m for m in rows if m.id not in own]

    def holders(self, text: str, *, k: int = HOLDER_HITS) -> list[MemoryRow]:
        """The memories anyone holds on `text`, best match first: a full-text search over every
        member's active memories (fix wave G3, rehearsal 3: the members who held the guild's
        history on his question were never drawn). Hearsay (under HEARD) and a memory resting
        only on whispers are no one's to tell in public and stay out (_shareable's rule for a
        public channel); the caller decides which match its line."""
        q = fts_query(text)
        if not q:
            return []
        rows = self._store.get_memories([i for i, _ in self._store.fts_all(q, k)])
        rows = [m for m in rows if m.status == "active" and m.confidence >= HEARD]
        return self._shareable(rows, 0, "guild", 0)

    async def recall(self, owner: int, query: str, *, present: Sequence[int],
                     addressee: int | None, k: int = 5, priority: Priority = Priority.SOCIAL,
                     touch: bool = True, channel: str | None = None,
                     to_guid: int = 0, include_bond: bool = False) -> list[MemoryRow]:
        """The k memories to bring up. `touch` marks them accessed (recency); a look that
        doesn't use them (an evaluation, a quiz) passes False. `channel` is that of the turn
        they are for and `to_guid` whom its whisper goes to. With a channel, a private or
        secret memory comes up only in a whisper to its partner (scope_ok), then a memory
        drawn only from whispers only in a whisper between the same two (_shareable, the
        second check), and one of the owner's own lines of the open session not at all
        (_not_own_lines). Without one nothing is left out: only callers that build no prompt
        omit it.

        Scores add recency (from the last access, with the tau_days half-life, not stretched
        over the candidates), importance and relevance (both spread 0..1) and 1.0 for one the
        addressee is in. A memory a live prompt used within REST_S rests: it comes after every
        other candidate, so one member's lines don't circle the same few stories."""
        store = self._store
        text = " ".join(strip_wow_markup(query).split())
        qbytes = await self._embed_query(text, priority)
        qvec = unpack_vec(qbytes) if qbytes is not None else None
        now = int(self._clock.now())

        guids = list(dict.fromkeys([*present, *([addressee] if addressee is not None else [])]))
        by_id: dict[int, MemoryRow] = {m.id: m for m in store.memories_with_actors(owner, guids, 200)}
        knn_ids: list[int] = []
        if qbytes is not None:
            try:
                knn_ids = [i for i, _ in store.knn(owner, qbytes, 50)]
            except ValueError as e:
                log.warning("recall vector search failed, using text and recency: %s", e)
                qvec = None
        q = fts_query(text)
        hits = dict(store.fts(owner, q, 20)) if q else {}
        for m in store.get_memories([i for i in (*knn_ids, *hits) if i not in by_id]):
            by_id[m.id] = m
        rows = [m for m in by_id.values() if m.status == "active" and m.owner == owner]
        if channel is not None:
            if not include_bond:
                rows = [m for m in rows if not (m.key or '').startswith('bond:')]
            scoped = [m for m in rows if scope_ok(m, turn_channel=channel, to_guid=to_guid)]
            if len(scoped) < len(rows):
                log.debug("recall_scope_excluded: n=%d", len(rows) - len(scoped))
            rows = self._not_own_lines(self._shareable(scoped, owner, channel, to_guid, include_bond=include_bond), owner)
        if not rows:
            return []

        bm25 = _spread({i: -s for i, s in hits.items()})
        relevance: dict[int, float] = {}
        vecs: dict[int, list[float]] = {}
        for m in rows:
            best = bm25.get(m.id, 0.0)
            if m.embedding is not None:
                vecs[m.id] = unpack_vec(m.embedding)
                if qvec is not None:
                    best = max(best, cosine(qvec, vecs[m.id]))
            relevance[m.id] = best
        imp = _spread({m.id: float(m.importance) for m in rows})
        rel = _spread(relevance)
        total = {m.id: recency(m.t_accessed, now, self.tau_days) + imp[m.id] + rel[m.id]
                 + (1.0 if addressee is not None and addressee in m.actors else 0.0)
                 - (REST_PENALTY if resting(m, now) else 0.0)
                 for m in rows}

        sim = vector_or_word_similarity(vecs, {m.id: m.text for m in rows})

        order = sorted(rows, key=lambda m: (-total[m.id], -m.t_created, m.id))
        picked = mmr([m.id for m in order], total, sim, k)
        if touch:
            store.touch_memories(picked, now)
        return store.get_memories(picked)
