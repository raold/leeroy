"""The nightly re-pass over whispers (P1c Task 8; the cognition memo's vectors 1 and 3). P1b's
extractor has already turned each closed thread into memories on the live model; this pass
re-reads the confidential ones with the reflection model and answers three questions: what was
said in confidence (the ledger's facts, vector 1), how far the member opened up to him (surface,
middle or deep), and how he answered (vector 3). The model only labels; rules decide what the
labels change.

A thread is re-read when it has a whisper between him and a cast member, or a whisper between
two members that asked for secrecy (DONT_TELL_RE). A line on any other channel that asks for
secrecy is public: everyone in its audience saw it, so its thread is not re-read and nothing in
it becomes secret (controller ruling B2).

- A fact is grounded like any claim (Task 6: the twelve rules, then the judge). It is secret when
  the model calls it confidential or its thread asked for secrecy (privacy.secret_lines: an
  earlier thread of the same two that night counts), and otherwise private (ruling I8): only the
  member knows it, and only her own gossip can pass it on (Task 16).
- The labels are checked by rule, never judged: a disclosure cites the member's own lines, and
  his answer his lines after the disclosure's first line. A label that fails counts as none.
  Disclosures are to him only: two members' labels are not read.
- A disclosure to him is a `disclosure` row. His answer moves her trust in him through a pending
  rel_delta, which tonight's nudges stage applies under the night cap (Task 9). A responsive
  answer to a middle or deep disclosure is also a memory of hers that says nothing of what she
  told, private to him through its whisper evidence (rulings N4 and B2); it is never a self-note
  (Task 11's `eligible` skips the disclose: key)."""
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from leeroy_mind.logs import log
from leeroy_mind.memory.privacy import DONT_TELL_RE, secret_lines
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly.audience import cast_names
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.evidence import Evidence, Facts, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, Verdict, ground, log_verdicts
from leeroy_mind.nightly.knowledge import add_fact, believe, learn
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.settings import NightlySettings
from leeroy_mind.storage.rows import ChatLineRow, MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "confide"
DEPTHS = {"surface": 1, "middle": 2, "deep": 3}
HEARD_TRUST = {1: 2.0, 2: 3.0, 3: 4.0}  # trust a responsive answer adds, by depth
DISMISSED_TRUST = -2.0
DISMISSED_DAYS = 14  # a dismissed disclosure holds her back a layer this long
DAY_S = 86400.0

CONFIDE_PROMPT = (
    "Read this conversation the way {name} remembers it. List only what was said in confidence "
    "or is personal news, who it is about, how far {name} opened up to {called}, and how "
    "{called} answered.")
CONFIDE_ASK = (
    "The conversation, one numbered line each:\n{lines}\n\n"
    "Give the numbers of the lines each answer rests on.\n"
    "facts: at most three things said in confidence or that are personal news, each one short "
    "sentence. kind is \"confidential\" for what someone was asked to keep to themselves, "
    "\"said_about\" for what was said about someone and \"life_news\" for news of someone's own "
    "life; about is the name of who it is about, or null.\n{labels}")
LABELS_HIS = (
    "disclosure: how far {name} opened up to {called}: \"surface\" (work, music, daily life), "
    "\"middle\" (money, family, a partner who minds the hours), \"deep\" (an old loss, a bad "
    "year), or \"none\"; cite {name}'s own lines.\n"
    "response: how {called} answered it: \"responsive\" (with understanding), \"neutral\", "
    "\"dismissive\" (brushing it off), or \"none\" (no answer); cite {called}'s lines.")
LABELS_MEMBERS = ("disclosure and response: \"none\", citing no lines, since {called} is not in "
                  "this conversation.")
HEARD = "Told {called} something real. {he} listened."

_LINES = {"type": "array", "items": {"type": "integer"}}
CONFIDE_SCHEMA = schema("confide", {
    "facts": {"type": "array", "maxItems": 3, "items": {
        "type": "object",
        "properties": {"kind": {"type": "string",
                                "enum": ["said_about", "life_news", "confidential"]},
                       "about": {"type": ["string", "null"]},
                       "text": {"type": "string", "maxLength": 160},
                       "evidence": {**_LINES, "minItems": 1}},
        "required": ["kind", "about", "text", "evidence"], "additionalProperties": False}},
    "disclosure": {"type": "object",
                   "properties": {"depth": {"type": "string",
                                            "enum": ["none", "surface", "middle", "deep"]},
                                  "evidence": _LINES},
                   "required": ["depth", "evidence"], "additionalProperties": False},
    "response": {"type": "object",
                 "properties": {"kind": {"type": "string",
                                         "enum": ["responsive", "neutral", "dismissive", "none"]},
                                "evidence": _LINES},
                 "required": ["kind", "evidence"], "additionalProperties": False},
})


# -- which threads -------------------------------------------------------------------------------

def _his_end(frm: int, to: int, human: int, cast: set[int]) -> int | None:
    """The cast end of a whisper between him and a cast member, either way; else None."""
    if frm == human and to in cast:
        return to
    if to == human and frm in cast:
        return frm
    return None


def _asks_secrecy(frm: int, to: int, text: str, cast: set[int]) -> bool:
    """Two members' whisper that asked for secrecy."""
    return frm in cast and to in cast and frm != to and DONT_TELL_RE.search(text) is not None


def confidential_threads(store: Store, window: tuple[float, float], human: int, *,
                         limit: int = NightlySettings.confide_per_night) -> list[int]:
    """The threads closed in the window (status extracted or skipped) with a whisper line between
    the human and a cast member, or a whisper line between two members that matches DONT_TELL_RE;
    newest first, at most `limit` (settings.confide_per_night). Only whisper lines count: a
    line on any other channel that asks for secrecy is public, so its thread is not re-read
    (ruling B2). The cast is the night's (audience.cast_names)."""
    start, end = window
    cast = set(cast_names(store)) - {human}
    with store.transaction():
        conn = store.conn
        threads = [r["id"] for r in conn.execute(
            "SELECT id FROM chat_thread WHERE status IN ('extracted', 'skipped') AND closed >= ? "
            "AND closed < ? ORDER BY closed DESC, id DESC", (start, end))]
        lines = conn.execute(
            "SELECT thread_id, from_guid, to_guid, text FROM chat_line WHERE channel = 'whisper' "
            "AND thread_id IN (SELECT value FROM json_each(?))",
            (json.dumps(threads),)).fetchall() if threads else []
    keep = {r["thread_id"] for r in lines
            if _his_end(r["from_guid"], r["to_guid"], human, cast) is not None
            or _asks_secrecy(r["from_guid"], r["to_guid"], r["text"], cast)}
    return [t for t in threads if t in keep][:max(0, limit)]


def _thread_lines(store: Store, tid: int) -> list[ChatLineRow]:
    with store.transaction():
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM chat_line WHERE thread_id = ?", (tid,))]
    return sorted(store.chat_lines(ids), key=lambda ln: (ln.ts, ln.id))


def _who(lines: list[ChatLineRow], human: int, cast: set[int]) -> tuple[int, int] | None:
    """(the member, the other end). In a whisper thread with him the member is its cast end and
    the other end is him; in two members' thread that asked for secrecy the member is the
    speaker of its first DONT_TELL_RE line, and the other end that line's listener."""
    whispers = [ln for ln in lines if ln.channel == "whisper"]
    for ln in whispers:
        end = _his_end(ln.from_guid, ln.to_guid, human, cast)
        if end is not None:
            return end, human
    for ln in whispers:
        if _asks_secrecy(ln.from_guid, ln.to_guid, ln.text, cast):
            return ln.from_guid, ln.to_guid
    return None


# -- the labels ----------------------------------------------------------------------------------

@dataclass
class _Labels:
    """What the labels change: `depth` 0 is no disclosure to him; `disclosed` and `answered` are
    the lines each passing label cites, in the thread's order; `log` is every label read, with
    its verdict, for the grounding log."""
    depth: int = 0
    disclosed: list[str] = field(default_factory=list)
    response: str = "none"
    answered: list[str] = field(default_factory=list)
    log: list[tuple[Claim, Verdict]] = field(default_factory=list)


def _label(owner: int, kind: str, value: str, refs: list[str], ok: bool,
           reason: str) -> tuple[Claim, Verdict]:
    return (Claim(kind=kind, owner=owner, text=value, evidence=refs),
            Verdict(ok, "deterministic", "" if ok else reason))


def _check_labels(reply: dict, ev: Evidence, lines: dict[str, ChatLineRow],
                  order: dict[str, int], m: int, human: int) -> _Labels:
    """A disclosure passes when it cites lines, all of them `m`'s own ("disclosure not
    theirs"); his answer to a passing one when it cites lines, all of them his and after the
    disclosure's first line ("response not his"). With no disclosure there is nothing he
    answered, so the response is not read."""
    out = _Labels()
    d, r = reply["disclosure"], reply["response"]
    if d["depth"] == "none":
        return out
    refs = [ev.ref(a) for a in dict.fromkeys(d["evidence"])]
    ok = bool(refs) and all(x in lines and lines[x].from_guid == m for x in refs)
    out.log.append(_label(m, "disclosure", d["depth"], refs, ok, "disclosure not theirs"))
    if not ok:
        return out
    out.depth, out.disclosed = DEPTHS[d["depth"]], sorted(refs, key=order.__getitem__)
    if r["kind"] == "none":
        return out
    first = order[out.disclosed[0]]
    refs = [ev.ref(a) for a in dict.fromkeys(r["evidence"])]
    ok = bool(refs) and all(x in lines and lines[x].from_guid == human and order[x] > first
                            for x in refs)
    out.log.append(_label(m, "response", r["kind"], refs, ok, "response not his"))
    if ok:
        out.response, out.answered = r["kind"], sorted(refs, key=order.__getitem__)
    return out


# -- one thread ----------------------------------------------------------------------------------

def _fact_claim(f: dict, m: int, ev: Evidence, facts: Facts) -> Claim:
    """A fact of the reply as a claim of kind "fact" of `m`, its text as it will be stored
    (markup stripped, spaces collapsed) and `about` resolved by facts.names (None when it names
    nobody known). A fact about `m` is checked with no `about`: the rule that a claim is not
    about its owner is for what members think of others, and what she said of herself is the
    secret most worth keeping."""
    text = " ".join(strip_wow_markup(f["text"]).split())
    about = facts.names.get((f["about"] or "").strip().lower())
    return Claim(kind="fact", owner=m, text=text,
                 evidence=[ev.ref(a) for a in dict.fromkeys(f["evidence"])],
                 about=None if about == m else about,
                 extra={"kind": f["kind"], "about": about})


def _keep_fact(store: Store, claim: Claim, lines: dict[str, ChatLineRow], order: dict[str, int],
               m: int, other: int, human: int, secret_thread: bool, now: float) -> None:
    """An accepted fact: the fact row on its first cited line (in the thread's order); `m`
    learns it, as witnessed when every line it cites is hers, else confided by the other end;
    in two members' thread the listener learns it as confided too, and each believes the other
    knows it. A secret is never passed on: may_tell 0."""
    cited = sorted(claim.evidence, key=order.__getitem__)
    root, kind = cited[0], claim.extra["kind"]
    secret = kind == "confidential" or secret_thread
    add_fact(store, kind="whisper_scene" if kind == "confidential" else kind, root=root,
             about=claim.extra["about"], text=claim.text, secret=secret, now=now)
    learned, tell = max(lines[x].ts for x in cited), 0 if secret else 1
    if all(lines[x].from_guid == m for x in cited):
        learn(store, root, m, "witnessed", ts=learned, may_tell=tell)
    else:
        learn(store, root, m, "confided", via=other, ts=learned, may_tell=tell)
    if other != human:
        learn(store, root, other, "confided", via=m, ts=learned, may_tell=tell)
        believe(store, m, other, root, "yes", now=now)
        believe(store, other, m, root, "yes", now=now)


async def confide_thread(ctx: "NightContext", tid: int, *, facts: Facts | None = None) -> None:
    """Unit str(tid) of stage "confide": one JSON call (CONFIDE_SCHEMA, temperature 0.2) over
    the thread's lines as chat: items (load_items: his real name redacted, his lines under
    what the guild calls him), the facts through `ground`, the labels by rule, then one commit
    (see the module docstring). `facts` is the night's Facts, loaded here when not given."""
    store, human, unit = ctx.store, ctx.human, str(tid)
    rows = _thread_lines(store, tid)
    who = _who(rows, human, set(cast_names(store)) - {human})
    if who is None:
        ctx.skip(STAGE, unit)
        return
    m, other = who
    lines = {f"chat:{ln.id}": ln for ln in rows}
    order = {ref: i for i, ref in enumerate(lines)}
    ev = Evidence()
    for item in load_items(store, list(lines), ident=ctx.ident):
        ev.add(item)
    name, called = ctx.name(m), ctx.ident.called
    system = system_prompt(STAGE, CONFIDE_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=name, called=called)
    labels = (LABELS_HIS if other == human else LABELS_MEMBERS).format(name=name, called=called)
    reply = await ctx.reasoner.json(system, CONFIDE_ASK.format(lines=ev.render(ctx.tz),
                                                               labels=labels),
                                    CONFIDE_SCHEMA, temperature=0.2)
    facts = facts if facts is not None else load_facts(ctx)
    pairs = await ground(ctx, [_fact_claim(f, m, ev, facts) for f in reply["facts"]], ev, facts)
    said = (_check_labels(reply, ev, lines, order, m, human) if other == human else _Labels())
    refs = said.disclosed + [x for x in said.answered if x not in said.disclosed]
    listened = clean_memory_text(
        HEARD.format(called=called, he=ctx.ident.forms[0].capitalize()), ctx.ident)
    vec = None
    if said.response == "responsive" and said.depth >= DEPTHS["middle"]:
        (vec,) = await ctx.embed([listened])  # before the commit: nothing awaits inside it
    with store.transaction():
        secret_thread = bool(secret_lines(store.conn, [ln.id for ln in rows]))
    now = ctx.clock.now()
    heard = None if vec is None else MemoryRow(
        id=None, owner=m, kind="conversation", text=listened,
        t_event=int(max(lines[x].ts for x in refs)), t_created=int(now), t_accessed=int(now),
        access_n=0, actors=(human,), place=None, boss_id=None, raid_id=None, importance=6,
        valence=0.5, source="reflection", evidence=tuple(refs), key=f"disclose:{tid}",
        embedding=vec)

    def write(s: Store) -> None:
        for claim, v in pairs:
            if v.ok:
                _keep_fact(s, claim, lines, order, m, other, human, secret_thread, now)
        if said.depth:
            s.conn.execute(
                "INSERT INTO disclosure(guid, other, depth, response, layer, ts, ref) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (m, human, said.depth, said.response, now, said.disclosed[0]))
            if said.response == "responsive":
                s.add_rel_delta(m, human, "trust", HEARD_TRUST[said.depth], "disclosure heard",
                                refs, now, applied=False)
                if heard is not None:
                    ctx.writer.put(heard)
            elif said.response == "dismissive":
                s.add_rel_delta(m, human, "trust", DISMISSED_TRUST, "disclosure dismissed", refs,
                                now, applied=False)
        log_verdicts(s, ctx.night, STAGE, pairs + said.log, now, ctx.ident)
    if ctx.commit(STAGE, unit, write):
        log("nightly_confide", night=ctx.night, thread=tid, guid=m,
            facts=sum(v.ok for _, v in pairs), rejected=sum(not v.ok for _, v in pairs),
            depth=said.depth, response=said.response)


# -- what the night reads back -------------------------------------------------------------------

def depth_with(store: Store, guid: int, other: int, *, now: float) -> int:
    """How far `guid` has opened up to `other`, 0-3: the deepest responsive disclosure, less 1
    (not below 0) while a dismissive one is younger than DISMISSED_DAYS. Task 12's stage gates
    read it."""
    with store.transaction():
        r = store.conn.execute(
            "SELECT max(CASE WHEN response = 'responsive' THEN depth END) AS deepest, "
            "max(response = 'dismissive' AND ? - ts < ?) AS held FROM disclosure "
            "WHERE guid = ? AND other = ?", (now, DISMISSED_DAYS * DAY_S, guid, other)).fetchone()
    deepest = r["deepest"] or 0
    return max(0, deepest - 1) if r["held"] else deepest


async def stage_confide(ctx: "NightContext") -> None:
    """Stage "confide", units str(thread id): confide_thread over tonight's confidential threads,
    at most settings.parallel at once. It runs before "nudges", which applies the trust it
    leaves pending."""
    threads = confidential_threads(ctx.store, ctx.window, ctx.human,
                                   limit=ctx.settings.confide_per_night)
    if not threads:
        return
    facts = load_facts(ctx)
    await ctx.map_units(STAGE, [str(t) for t in threads],
                        lambda unit: confide_thread(ctx, int(unit), facts=facts))


def _count(ctx: "NightContext") -> int:
    """The budget's count: tonight's confidential threads."""
    return len(confidential_threads(ctx.store, ctx.window, ctx.human,
                                    limit=ctx.settings.confide_per_night))


register_counter(STAGE, _count)
