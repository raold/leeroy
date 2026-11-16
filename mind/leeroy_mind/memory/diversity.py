"""Recall diversity (`[talk] recall_diversity`, 2026-11-16): one member must not circle the same
memory, or the same subject, turn after turn and day after day.

The owner's DM audit (PM log 2026-11-16): a week-old "finish the story draft so I can hear him
say it tomorrow" plan came back as current in five replies; four near-copies of it were active,
and her most used memories had been shown 20 to 25 times. Recall's recency counts from the last
access, so every use made a memory likelier to be used again. Three rules, all after the scope
and audience filters (recall.scope_ok, Recall._shareable), so they only ever drop or lower a
candidate those filters already let into the turn, never add one:

1. A time-bound intention ("tonight", "tomorrow", "this weekend", "in the morning", "I need to X
   by Friday"; a plan memory, or a first-person "I need to / I'll / I want to") whose time has
   passed is shown as past (`lead`: "(I was going to, but that time has passed)" before its
   words; the stored text is never rewritten), and lowered by STALE_PENALTY once older than
   STALE_S.
2. In one conversation (CONVERSATION_S: her pair with him in either medium, a whisper's pair,
   else the channel) a memory her prompt already showed for a line she sent there is left out,
   unless his line this turn is close to it (cosine at least REPEAT_COSINE between his line's
   embedding and the memory's). And a prompt holds at most one memory per subject: content-word
   Jaccard at least CLUSTER_JACCARD keeps only the best-scored one (`one_per_subject`).
3. Talking with him privately, a subject she raised in SUBJECT_RAISED or more of her own lines to
   him in the last FATIGUE_S (at least SUBJECT_SHARED content words in common with the memory)
   is lowered by SUBJECT_PENALTY, unless he raised it himself in this conversation (his line, or
   his lines to her within CONVERSATION_S, sharing a content word with it).

Reads only: the utterance log (memory ids shown for her sent lines) and chat_line. No model call,
no write, no log text. REPEAT_COSINE was measured read-only on the live guild (2026-11-16): of
2,419 pairs of his line and a memory recall showed for an answer to it, 0.7% reach 0.6 (each one
read as his line being about that memory), 2.6% reach 0.55 (where spurious matches begin)."""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Callable, Iterable, Sequence

from leeroy_mind.clock import night_of
from leeroy_mind.discord.channels import is_private
from leeroy_mind.retrieval import cosine
from leeroy_mind.storage.rows import MemoryRow

VERSION = "diversity-1"  # learn.recipe.recall_used: lines whose memories these rules picked
CONVERSATION_S = 3 * 3600.0
STALE_S = 48 * 3600.0
FATIGUE_S = 7 * 86400.0
REPEAT_COSINE = 0.6
CLUSTER_JACCARD = 0.5
SUBJECT_RAISED = 3
SUBJECT_SHARED = 2
STALE_PENALTY = 1.5  # a score is recency + importance + relevance (each 0..1) + 1 for the addressee
SUBJECT_PENALTY = 1.5
PAST_SOON = "(I was going to, but that time has passed) "
PAST_LONG = "(I meant to, but that time has passed) "  # a week or more ago

_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
_STOP = frozenset("""
the and for you your are was were but not with have has had this that these those from they them
then than there their what when where who whom why how can could would should will just about into
over out all any one our its his her hers she him let get got did does dont don't yes yeah lol
also really very still much been like some more here only even again today tomorrow tonight
yesterday okay nope well sure maybe thing things stuff gonna wanna gotta kinda sorta something
anything nothing everything someone anyone because while after before until being doing going make
made know think thought feel feels felt good great nice come came back want wants wanted need needs
needed said says tell told right guys babe baby i'm i've i'd i'll me my mine we us im ive youre
you're actually honestly literally god love time times day days night nights week weeks morning
evening later now never ever every always little bit way lot lots keep kept try tried take took
give gave look looked looking see saw seen mean meant hear heard talk talked talking say saying
first last next other another same new old big real proper properly though cause cuz too off
away without absolutely already enough around through
""".split())

# The time an intention is bound to, and how many nights (clock.night_of) until it has passed:
# the latest one a memory names counts.
_HORIZONS = (
    (re.compile(r"\b(?:tonight|today|later today|this (?:morning|afternoon|evening)|right now)\b"), 1),
    (re.compile(r"\b(?:tomorrow|tmrw|tmr|in the morning|first thing)\b"), 2),
    (re.compile(r"\b(?:this (?:weekend|week)|the weekend|end of the week|next (?:raid|reset)"
                r"|before (?:the )?(?:next )?raid|raid night"
                r"|(?:on |by )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b(?!s))"), 7),
    (re.compile(r"\bnext (?:week|weekend)\b"), 14),
)
_INTENT = re.compile(
    r"\b(?:i|we)\s+(?:really\s+|still\s+|just\s+)?(?:need|have|want|plan|mean|intend|hope|promised?"
    r"|swore|got)\s+to\b"
    r"|\b(?:i|we)(?:'ll|'m going to|'m gonna|'re going to|'re gonna| will| am going to"
    r"| are going to| am gonna| are gonna"
    r"| shall| should| must)\b"
    r"|\bi'?m\s+(?:going|gonna)\b|\blet'?s\b")


@dataclass(frozen=True)
class Diversity:
    """What the rules need from the turn: `human`, his guid (0: no rule 3); `his_line`, the text of
    his line this turn answers ("" when it answers none of his); `names`, names that are never a
    subject word (the cast's, his)."""
    human: int = 0
    his_line: str = ""
    names: frozenset[str] = field(default_factory=frozenset)


def _fold(text: str) -> str:
    return text.casefold().replace("’", "'")


def _stem(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[:-len(suffix)]
            if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":  # sitting, stopped
                w = w[:-1]
            break
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


_STOP_STEMS = _STOP | {_stem(w) for w in _STOP}  # "making" is "make"


def name_words(names: Iterable[str]) -> frozenset[str]:
    """Every word of `names`, folded, as `words` leaves them out."""
    return frozenset(t for n in names if n for t in _TOKEN.findall(_fold(n)))


def words(text: str, names: frozenset[str] = frozenset()) -> frozenset[str]:
    """A text's content words, stemmed: three letters or more, no stopword (_STOP, in any of its
    forms), contraction ("you've"), number or word of a name in `names` (name_words)."""
    out = set()
    for w in _TOKEN.findall(_fold(text or "")):
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) < 3 or "'" in w or w.isdigit() or w in _STOP or w in names:
            continue
        s = _stem(w)
        if s not in _STOP_STEMS and len(s) >= 3:
            out.add(s)
    return frozenset(out)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def horizon(m: MemoryRow) -> int | None:
    """How many nights after it was written a time-bound intention's time has passed, None for a
    memory that is not one. An intention: a plan, or a first-person "I need to", "I'll", "I want
    to", "we're going to", "let's". Never a game event or the owner's words, shown as written."""
    if m.source in ("owner", "game") or m.kind == "story":
        return None
    text = _fold(m.text)
    if m.kind != "plan" and not _INTENT.search(text):
        return None
    found = [nights for rx, nights in _HORIZONS if rx.search(text)]
    return max(found) if found else None


def _nights(t_event: float, now: float, tz: tzinfo) -> int:
    return (date.fromisoformat(night_of(datetime.fromtimestamp(now, tz)))
            - date.fromisoformat(night_of(datetime.fromtimestamp(t_event, tz)))).days


def lead(m: MemoryRow, now: float, tz: tzinfo) -> str:
    """What a memory's prompt line says before its words: PAST_SOON or PAST_LONG for a time-bound
    intention whose time has passed (horizon), else ""."""
    h = horizon(m)
    if h is None:
        return ""
    nights = _nights(m.t_event, now, tz)
    if nights < h:
        return ""
    return PAST_LONG if nights >= 7 else PAST_SOON


def stale(m: MemoryRow, now: float, tz: tzinfo) -> bool:
    """A past intention (lead) older than STALE_S."""
    return now - m.t_event > STALE_S and bool(lead(m, now, tz))


def _ids(raw) -> list[int]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [i for i in value if type(i) is int] if isinstance(value, list) else []


def shown_here(conn, speaker: int, channel: str, to_guid: int, now: float) -> set[int]:
    """The memories her prompts showed for the lines she sent in this conversation within
    CONVERSATION_S (the utterance log's memory_ids): a private turn's pair in either medium
    (a whisper to `to_guid`, or her DM with him, `dm:<speaker>`), else this channel."""
    since = now - CONVERSATION_S
    if is_private(channel):
        rows = conn.execute(
            "SELECT memory_ids FROM utterance WHERE guid=? AND dropped IS NULL AND ts>=? "
            "AND to_guid=? AND channel IN ('whisper', ?)", (speaker, since, to_guid, f"dm:{speaker}"))
    else:
        rows = conn.execute(
            "SELECT memory_ids FROM utterance WHERE guid=? AND dropped IS NULL AND ts>=? "
            "AND channel=?", (speaker, since, channel))
    return {i for (raw,) in rows for i in _ids(raw)}


def _pair_lines(conn, frm: int, to: int, member: int, since: float) -> list[str]:
    """`frm`'s lines to `to` since `since`: whispers to them, and lines in the member's DM with
    him (`dm:<member>`)."""
    return [t for (t,) in conn.execute(
        "SELECT text FROM chat_line WHERE from_guid=? AND ts>=? AND "
        "((channel='whisper' AND to_guid=?) OR channel=?)", (frm, since, to, f"dm:{member}"))]


def tired(conn, speaker: int, ctx: Diversity, now: float
          ) -> tuple[list[frozenset[str]], list[frozenset[str]]]:
    """Rule 3's evidence: the words of her lines to him within FATIGUE_S, and of his to her in
    this conversation (within CONVERSATION_S) with his line this turn."""
    hers = [words(t, ctx.names) for t in _pair_lines(conn, speaker, ctx.human, speaker,
                                                       now - FATIGUE_S)]
    his = [words(t, ctx.names) for t in [*_pair_lines(conn, ctx.human, speaker, speaker,
                                                       now - CONVERSATION_S), ctx.his_line]]
    return [w for w in hers if w], [w for w in his if w]


def raised(subject: frozenset[str], hers: Sequence[frozenset[str]],
           his: Sequence[frozenset[str]]) -> bool:
    """Her subject is tired: SUBJECT_RAISED or more of her lines share SUBJECT_SHARED of its
    words, and none of his shares one."""
    if len(subject) < SUBJECT_SHARED or any(subject & w for w in his):
        return False
    return sum(1 for w in hers if len(subject & w) >= SUBJECT_SHARED) >= SUBJECT_RAISED


def rerank(conn, rows: list[MemoryRow], total: dict[int, float], ctx: Diversity, *,
           owner: int, channel: str | None, to_guid: int, now: float, tz: tzinfo,
           qvec: Sequence[float] | None, vecs: dict[int, list[float]]
           ) -> tuple[list[MemoryRow], dict[int, float]]:
    """Rules 1-3 over recall's scored candidates (already within the turn's audience): the
    candidates left and their scores, lowered where a rule says. Without a channel (no turn)
    only rule 1 applies."""
    total = dict(total)
    for m in rows:
        if stale(m, now, tz):
            total[m.id] -= STALE_PENALTY
    if channel is None:
        return rows, total
    shown = shown_here(conn, owner, channel, to_guid, now)
    if shown:
        def asked(m):  # his line this turn is about this memory
            return (bool(ctx.his_line) and qvec is not None and m.id in vecs
                    and cosine(qvec, vecs[m.id]) >= REPEAT_COSINE)
        rows = [m for m in rows if m.id not in shown or asked(m)]
    if ctx.human and to_guid == ctx.human and is_private(channel):
        hers, his = tired(conn, owner, ctx, now)
        if len(hers) >= SUBJECT_RAISED:
            for m in rows:
                if raised(words(m.text, ctx.names), hers, his):
                    total[m.id] -= SUBJECT_PENALTY
    return rows, total


def one_per_subject(order: list[MemoryRow], names: frozenset[str] = frozenset(),
                    text: Callable[[MemoryRow], str] = lambda m: m.text) -> list[MemoryRow]:
    """`order` (best first) with every memory whose words (`text`) are CLUSTER_JACCARD or more
    like a better one's left out."""
    kept: list[tuple[MemoryRow, frozenset[str]]] = []
    for m in order:
        w = words(text(m), names)
        if not any(jaccard(w, k) >= CLUSTER_JACCARD for _, k in kept):
            kept.append((m, w))
    return [m for m, _ in kept]
