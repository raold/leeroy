"""Each member's night of reflection (P1c Task 11; the owner's beyond-Smallville items 3, 4 and 5,
and the cognition memo's vector 4, appraisal): what bugged them and what they want tomorrow,
what they make of the others and of themselves, what others believe, who gets a nickname, how
they felt, and the diary.

For one member, stage "reflect" runs one unit, str(guid) (reflect_member):
1. The digest (build_digest): their memories made tonight, the most important first. With fewer
   than min_new_memories there is nothing to think about: the unit is skipped, with no call.
2. The questions the digest raises (salient_questions: one JSON call, skipped for a digest under
   questions_min_new, the budget's last degrade step), then the evidence (gather): the digest,
   what each question brings back, and up to WHO_KNOWS things the member knows that someone in
   the digest does not (the ledger, Task 6).
3. One reasoning pass (Reasoner.think) over the evidence, trimmed to EVIDENCE_CHARS, the
   relationship cards, who knows what, what is on their mind, the questions and the owner's
   GOAL_QUESTIONS; then one JSON pass that writes it down (REFLECTION_SCHEMA). No output moves
   attraction (Task 6 rule 11).
4. Grounding (Task 6): every output and the diary through the rules and the judge, the feelings
   and the nicknames through the rules alone; Task 9's romance rules (relations.screen_claim)
   stand in front, so romance is only ever aimed at him and only from a woman with an attitude.
5. The scope each output will be stored with (below), then supersession for each belief and
   self-insight in that scope (Task 10), and the core judge for a self-insight that may become a
   self-note (growth).
6. One commit writes everything (_write).

Privacy (controller rulings N4 and B2; pre-flight PR7). Every memory goes through
ctx.writer.put, so its scope is never weaker than what it cites. The model saw more than any one
output cites: `shown` is every item the think prompt rendered after the trim, and every digest
memory the questions call showed (the questions ride along in the think prompt), and
`shown_open` says whether all of it is open.
- A diary or plan the judge accepted only as "partly" also takes the scope of `shown`.
- The backstop (re-review M7): when not shown_open, a belief, a self-insight, a theory-of-mind
  line, or a diary or plan judged "yes" takes the scope of `shown` too when it shares
  PRIVATE_OVERLAP content words with a shown item that is not open, whether its own evidence is
  open or already scoped (review-task11 I3): a scope only rises, so an output that overlaps a
  stricter thread than its own is stored at least as strictly as it, and two partners give 0.
- A feeling keeps its why only when shown_open, and a nickname is coined only then.
- Only plans stored open become persona_state.goals, and a self-insight becomes a self-note only
  when it is growth.eligible and stored open.
- Supersession offers candidates of exactly the stored scope, so a belief resting on a whisper
  never merges into, or supersedes, an open one."""
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from statistics import mean
from typing import TYPE_CHECKING

from leeroy_mind.llm.presets import STRUCTURE
from leeroy_mind.logs import log
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly import growth
from leeroy_mind.nightly.budget import CORE_JUDGE, reflecting, register_counter
from leeroy_mind.nightly.evidence import (Evidence, Facts, content_words, load_facts, load_items,
                                          retrieve)
from leeroy_mind.nightly.ground import Claim, Verdict, ground, log_verdicts, screen_text
from leeroy_mind.nightly.knowledge import unknown_to
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.relations import apply_capped, screen_claim
from leeroy_mind.nightly.runner import ATTITUDE_WORDS
from leeroy_mind.nightly.settings import NightlySettings, iso_week, week_bounds
from leeroy_mind.nightly.store import stage_record
from leeroy_mind.nightly.supersede import candidates, decide, resolve
from leeroy_mind.personas.screens import has_word
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store, clean
from leeroy_mind.talk.prompt import relationship_card
from leeroy_mind.text import estimate_tokens, normalize_words, strip_wow_markup, truncate_words

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "reflect"
GOAL_QUESTIONS = "What bugged you today? What do you want tomorrow?"
OCC = ("joy", "distress", "pride", "shame", "admiration", "reproach", "gratitude", "anger", "hope",
       "fear", "happy_for", "resentment", "gloating", "pity")
OUTPUT_KINDS = ("belief", "self_insight", "rel_delta", "plan", "tom", "nickname")
DIMS = ("affinity", "trust", "respect", "familiarity")
EMOTIONS_MAX = 3
EVIDENCE_CHARS = 14000  # the think prompt's evidence, so the call fits num_ctx 8192 (review M16)
JSON_PREDICT = 1024     # Reasoner.json's num_predict: the JSON pass's room for its answer
CTX_SLACK = 320         # tokens the JSON pass keeps free: the suffixes chat_json adds (a retry's
#                         too) and the schema's JSON, which runs denser than 4 characters a token
PRIVATE_OVERLAP = 2     # content words an open output shares with a shown private item (M7)
PEOPLE = 6              # digest.people: the most frequent actors
WHO_KNOWS = 3           # who-knows-what pairs, at most
WHO_KNOWS_SCAN = 12     # roots read per person to find them
WHO_KNOWS_DAYS = 7      # how far before the window the ledger is read
DAY_S = 86400.0
IMPORTANCE = {"plan": 5, "tom": 5, "nickname": 4, "diary": 3}  # beliefs and self-insights: cited
PARTLY = ("diary", "plan")  # PARTLY_OK kinds here: "partly" takes the scope of what was shown
BACKSTOPPED = ("belief", "self_insight", "tom", "diary", "plan")
SUPERSEDED = ("belief", "self_insight")  # stored through supersession (Task 10)
NEEDS_ABOUT = ("rel_delta", "nickname")  # tom too, by grounding rule 12
OPEN = ("open", 0)

_EVIDENCE = {"type": "array", "items": {"type": "integer"}, "minItems": 1}


def questions_schema(n: int) -> dict:
    """{"questions": [str<=150]} with exactly `n` items (settings.questions)."""
    return schema("questions", {"questions": {"type": "array", "minItems": n, "maxItems": n,
                                              "items": {"type": "string", "maxLength": 150}}})


def reflection_schema(max_outputs: int) -> dict:
    """The JSON pass: at most `max_outputs` outputs (settings.max_outputs), at most EMOTIONS_MAX
    feelings and the diary, each citing items by alias."""
    output = schema("output", {
        "kind": {"type": "string", "enum": list(OUTPUT_KINDS)},
        "about": {"type": ["string", "null"]},
        "text": {"type": "string", "maxLength": 200},
        "dim": {"type": ["string", "null"], "enum": [*DIMS, None]},
        "delta": {"type": ["integer", "null"], "minimum": -10, "maximum": 10},
        "valence": {"type": "number", "minimum": -1, "maximum": 1},
        "evidence": _EVIDENCE})
    emotion = schema("emotion", {
        "type": {"type": "string", "enum": list(OCC)},
        "target": {"type": ["string", "null"]},
        "intensity": {"type": "integer", "enum": [1, 2, 3]},
        "why": {"type": "string", "maxLength": 80},
        "evidence": _EVIDENCE})
    diary = schema("diary", {"text": {"type": "string", "maxLength": 200}, "evidence": _EVIDENCE})
    for part in (output, emotion, diary):
        del part["title"]
    return schema("reflection", {
        "outputs": {"type": "array", "maxItems": max_outputs, "items": output},
        "emotions": {"type": "array", "maxItems": EMOTIONS_MAX, "items": emotion},
        "diary": diary})


QUESTIONS_SCHEMA = questions_schema(NightlySettings.questions)
REFLECTION_SCHEMA = reflection_schema(NightlySettings.max_outputs)

QUESTIONS_PROMPT = "These are {name}'s memories of tonight."
QUESTIONS_ASK = ("Given only these memories, what are the {n} most salient high-level questions "
                 "we can answer about the people in them?")
REFLECT_PROMPT = "These are {name}'s private thoughts after tonight.\n{card}\n{attitude}"
REFLECT_ASK = (
    "Now write down what {name} takes from tonight, in {name}'s own words. Cite the numbers of "
    "the items each answer rests on, and name people only as those items do.\n"
    "outputs: at most {n}, each one short sentence:\n"
    "- belief: what {name} now thinks of someone (about: who) or of how things are (about: "
    "null);\n"
    "- self_insight: what {name} has come to see in themselves (about: null);\n"
    "- rel_delta: how {name}'s view of someone moved tonight (about: who; dim: affinity, trust, "
    "respect or familiarity; delta: -10 to 10), the text saying why;\n"
    "- plan: something {name} wants to do tomorrow, from what bugged them or what they want "
    "(about: who it involves, or null);\n"
    "- tom: what someone else believes, or does not know (about: who);\n"
    "- nickname: what {name} would call someone (about: who; text: the nickname alone, at most "
    "24 characters).\n"
    "dim and delta are null except for a rel_delta; valence is how {name} feels about it, from "
    "-1 to 1.\n"
    "emotions: at most 3 feelings {name} had tonight, each one of: {occ}; target: who it was "
    "about, or null; intensity 1 to 3; why: in a few words.\n"
    "diary: a line or two of {name}'s diary for tonight.")


@dataclass
class Digest:
    """A member's memories made tonight, the most important first, and the people they involve
    most (never the member, never 0)."""
    owner: int
    memories: list[MemoryRow]
    people: list[int]


@dataclass
class _Memory:
    """A memory the unit will write: its claim, its row (with its vector once embedded), what
    its scope must also cover beyond its evidence (put's `shown`), the scope that gives it and,
    for a belief or self-insight, supersession's decisions."""
    claim: Claim
    row: MemoryRow
    shown: tuple[str, ...]
    stored: tuple[str, int]
    decisions: list = field(default_factory=list)
    twins: list[str] = field(default_factory=list)  # texts of the rows it duplicates


def build_digest(store: Store, owner: int, window: tuple[float, float], *, size: int = 60,
                 min_new: int = 3) -> Digest | None:
    """The owner's active memories created in the window, the top `size` by importance and then
    the newest event (ties by id, newest first); None when fewer than `min_new` were created.
    `people` are the PEOPLE most frequent actors among them, the owner and 0 left out, ties in
    the digest's order."""
    start, end = window
    with store.transaction():
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM memory WHERE owner = ? AND status = 'active' AND t_created >= ? "
            "AND t_created < ? ORDER BY importance DESC, t_event DESC, id DESC",
            (owner, start, end))]
    if len(ids) < min_new:
        return None
    memories = store.get_memories(ids[:size])
    counts: Counter[int] = Counter()
    first: dict[int, int] = {}
    for i, m in enumerate(memories):
        for g in m.actors:
            if g not in (owner, 0):
                counts[g] += 1
                first.setdefault(g, i)
    return Digest(owner, memories,
                  sorted(counts, key=lambda g: (-counts[g], first[g]))[:PEOPLE])


def _digest_items(ctx: "NightContext", digest: Digest) -> Evidence:
    """The digest as evidence (tier 1), in its order: his real name redacted (load_items)."""
    ev = Evidence()
    for item in load_items(ctx.store, [f"memory:{m.id}" for m in digest.memories],
                           ident=ctx.ident):
        ev.add(item)
    return ev


async def salient_questions(ctx: "NightContext", owner: int, digest: Digest) -> list[str]:
    """One JSON call over the digest alone: the settings.questions questions it raises."""
    n = ctx.settings.questions
    if n <= 0:
        return []
    system = system_prompt("questions", QUESTIONS_PROMPT, ctx.settings.data_dir,
                           human=ctx.ident, name=ctx.name(owner))
    user = _digest_items(ctx, digest).render(ctx.tz) + "\n\n" + QUESTIONS_ASK.format(n=n)
    reply = await ctx.reasoner.json(system, user, questions_schema(n))
    return [" ".join(strip_wow_markup(q).split()) for q in reply["questions"]]


def _nameable(ctx: "NightContext", guid: int) -> bool:
    """The night can name `guid`: the human, or someone in the stored roster (an actor may be
    neither, such as a character no longer stored)."""
    try:
        ctx.name(guid)
    except KeyError:
        return False
    return True


async def gather(ctx: "NightContext", owner: int, digest: Digest,
                 questions: list[str]) -> tuple[Evidence, list[tuple[int, int]]]:
    """The reflection's evidence and its who-knows-what pairs (person, alias). First the digest
    (tier 1); then, for each question, the owner's settings.retrieve_k memories that best answer
    it (retrieve: it touches nothing), each once (tier 2); then, for the people of the digest in
    order, the roots the owner learned since a week before the window that they do not know
    (knowledge.unknown_to), each loaded as an item (tier 0, never trimmed), until there are
    WHO_KNOWS pairs. unknown_to leaves out whoever could have seen a root, and so never names
    him, whom the ledger never holds: rule 12 needs someone who really does not know, and reads
    the same rule (knowledge.could_have_seen)."""
    ev = _digest_items(ctx, digest)
    for q in questions:
        rows = await retrieve(ctx, owner, q, ctx.settings.retrieve_k)
        for item in load_items(ctx.store, [f"memory:{m.id}" for m in rows], ident=ctx.ident):
            ev.add(replace(item, tier=2))
    pairs: list[tuple[int, int]] = []
    since = ctx.window[0] - WHO_KNOWS_DAYS * DAY_S
    for g in digest.people:
        if len(pairs) == WHO_KNOWS:
            break
        if not _nameable(ctx, g):
            continue
        for root in unknown_to(ctx.store, owner, g, since=since, limit=WHO_KNOWS_SCAN):
            if len(pairs) == WHO_KNOWS:
                break
            items = load_items(ctx.store, [root], ident=ctx.ident)
            if items:
                pairs.append((g, ev.add(replace(items[0], tier=0))))
    return ev, pairs


def _text(s: str) -> str:
    return " ".join(strip_wow_markup(s).split())


def to_claims(owner: int, out: dict, ev: Evidence, facts: Facts) -> list[Claim]:
    """The JSON pass as claims, in order: the outputs, the feelings (kind "emotion", the why as
    text, the target as about, the type and intensity in extra) and the diary. A name is a guid
    through facts.names (his character name and what the guild calls him are the human's), an
    unknown one -1; an alias is a ref through ev.ref. extra["importance"] is min(9, round(the
    mean importance cited) + 1) for a belief or self-insight, IMPORTANCE for the rest."""
    def refs(aliases) -> list[str]:
        return [ev.ref(a) for a in dict.fromkeys(aliases)]

    def who(name) -> int | None:
        return None if name is None else facts.names.get(" ".join(str(name).split()).lower(), -1)

    def importance(kind: str, cited: list[str]) -> dict:
        if kind in ("belief", "self_insight"):
            got = [ev[r].importance for r in cited if r in ev]
            return {"importance": min(9, round(mean(got)) + 1) if got else 1}
        return {"importance": IMPORTANCE[kind]} if kind in IMPORTANCE else {}
    claims = []
    for o in out["outputs"]:
        cited = refs(o["evidence"])
        claims.append(Claim(o["kind"], owner, _text(o["text"]), cited, about=who(o["about"]),
                            dim=o["dim"], delta=o["delta"], valence=float(o["valence"]),
                            extra=importance(o["kind"], cited)))
    for e in out["emotions"]:
        claims.append(Claim("emotion", owner, _text(e["why"]), refs(e["evidence"]),
                            about=who(e["target"]),
                            extra={"type": e["type"], "intensity": e["intensity"]}))
    d = out["diary"]
    claims.append(Claim("diary", owner, _text(d["text"]), refs(d["evidence"]),
                        extra=importance("diary", [])))
    return claims


def _screen(ctx: "NightContext", c: Claim, ev: Evidence, facts: Facts, attitude: bool) -> Verdict:
    """Task 6's rules and Task 9's romance rules (relations.screen_claim), then two of this
    stage's own: a rel_delta or nickname is about someone; and a nickname never holds his real
    name, in any case, since a term is the vocabulary (the one exception to P1b's matcher)."""
    member = (ctx.name(c.about) if c.about is not None and c.about > 0 and c.about != ctx.human
              else None)
    v = screen_claim(c, ev, facts, ctx.ident, member=member, attitude=attitude)
    if v.ok and c.kind in NEEDS_ABOUT and c.about is None:
        return Verdict(False, "deterministic", "about not in evidence")
    if (v.ok and c.kind == "nickname" and ctx.ident.real_name
            and has_word(c.text, ctx.ident.real_name)):
        return Verdict(False, "deterministic", "real name")
    return v


async def _grounded(ctx: "NightContext", claims: list[Claim], ev: Evidence, facts: Facts,
                    attitude: bool) -> list[tuple[Claim, Verdict]]:
    """Each claim with its verdict, in order: _screen first, then ground (the judge) for the
    claims that passed it."""
    first = [_screen(ctx, c, ev, facts, attitude) for c in claims]
    passed = [c for c, v in zip(claims, first) if v.ok]
    judged = iter(await ground(ctx, passed, ev, facts)) if passed else iter(())
    return [(c, next(judged)[1] if v.ok else v) for c, v in zip(claims, first)]


def _cards(ctx: "NightContext", owner: int, people: list[int]) -> list[str]:
    """P1b's relationship cards of the owner for `people`: his with his pronouns and the guild
    master's tag, and her own note about him kept as live talk keeps it (`speaker`)."""
    speaker = ctx.persona(owner).identity.player_gender
    return [relationship_card(ctx.name(g), ctx.store.get_relationship(owner, g),
                              is_human=g == ctx.human,
                              is_guild_master=ctx.ident.is_guild_master,
                              forms=ctx.ident.forms, speaker=speaker)
            for g in people if _nameable(ctx, g)]


def _think_user(ctx: "NightContext", owner: int, ev: Evidence, cards: list[str],
                pairs: list[tuple[int, int]], life: str, questions: list[str]) -> str:
    """The think pass's user message, in order: the evidence, the cards, who knows what, what is
    on the owner's mind, the questions and GOAL_QUESTIONS; an empty section is left out."""
    name = ctx.name(owner)
    parts = [f"What {name} remembers of tonight, and what it brings back, one numbered item "
             f"each:\n{ev.render(ctx.tz)}"]
    if cards:
        parts.append(f"How {name} feels about the people in it:\n" + "\n".join(cards))
    if pairs:
        parts.append("Who knows what:\n" + "\n".join(f"- {ctx.name(p)} doesn't know [{a}]"
                                                     for p, a in pairs))
    if life:
        parts.append(f"On {name}'s mind lately: {life}")
    if questions:
        parts.append(f"Questions {name} turns over tonight:\n"
                     + "\n".join(f"- {q}" for q in questions))
    parts.append(GOAL_QUESTIONS)
    return "\n\n".join(parts)


def _fit_notes(notes: str, system: str, user: str, schema_: dict) -> str:
    """The reasoning pass's notes as the JSON pass gets them: whole when the call fits num_ctx
    with JSON_PREDICT tokens of output and CTX_SLACK to spare, else only their end, where the
    conclusions are. With the evidence at EVIDENCE_CHARS, six cards, three questions, the output
    instruction and the schema, a full 2,048 tokens of notes would run about 160 tokens past
    num_ctx 8192, and Ollama would cut the prompt's head, where the system prompt and INVARIANTS
    are (review M16)."""
    fixed = sum(estimate_tokens(t) for t in (system, user, json.dumps(schema_)))
    room = max(0, (STRUCTURE.options["num_ctx"] - JSON_PREDICT - CTX_SLACK - fixed) * 4)
    return notes if len(notes) <= room else notes[len(notes) - room:]


def _overlaps(text: str, private: list[set[str]], facts: Facts) -> bool:
    words = content_words(text, facts.names)
    return any(len(words & p) >= PRIVATE_OVERLAP for p in private)


def _row(owner: int, kind: str, text: str, evidence, *, actors=(), importance: int,
         valence: float = 0.0, now: float, key: str | None = None) -> MemoryRow:
    """A memory the reflection writes: source reflection, made and set now (a goal's t_event is
    when it was set), its vector added once the unit's texts are embedded."""
    t = int(now)
    return MemoryRow(
        id=None, owner=owner, kind=kind, text=text, t_event=t, t_created=t, t_accessed=t,
        access_n=0, actors=tuple(sorted(set(actors))), place=None, boss_id=None, raid_id=None,
        importance=max(1, min(10, int(importance))), valence=max(-1.0, min(1.0, float(valence))),
        source="reflection", evidence=tuple(evidence), key=key)


def _coined_since(store: Store, owner: int, since: float) -> int:
    with store.transaction():
        return store.conn.execute(
            "SELECT count(*) FROM lexicon WHERE kind = 'nickname' AND originator = ? "
            "AND born >= ?", (owner, since)).fetchone()[0]


def _holds_nickname(store: Store, owner: int, target: int) -> bool:
    """The owner already uses a nickname for `target` (one not vetoed)."""
    with store.transaction():
        return store.conn.execute(
            "SELECT 1 FROM lexicon l JOIN lexicon_adopter a ON a.term = l.id WHERE "
            "l.kind = 'nickname' AND l.target = ? AND l.status != 'vetoed' AND a.guid = ? "
            "AND a.active = 1 LIMIT 1", (target, owner)).fetchone() is not None


def _vetoed(store: Store, term: str, target: int) -> bool:
    with store.transaction():
        return store.conn.execute(
            "SELECT 1 FROM lexicon WHERE kind = 'nickname' AND term = ? AND target = ? "
            "AND status = 'vetoed'", (clean(term), target)).fetchone() is not None


async def reflect_member(ctx: "NightContext", owner: int, *, facts: Facts | None = None) -> int:
    """Unit str(owner) of stage "reflect" (the module docstring). `facts` is the night's Facts,
    loaded here when not given. Every call is made before the one commit, so a cancel or a
    failure anywhere writes nothing. Returns how many times it asked the core judge, which the
    budget measures (CORE_JUDGE)."""
    unit, s, store = str(owner), ctx.settings, ctx.store
    if ctx.done(STAGE, unit):
        return 0
    digest = build_digest(store, owner, ctx.window, size=s.digest_size,
                          min_new=s.min_new_memories)
    if digest is None:
        ctx.skip(STAGE, unit)
        return 0
    asked = len(digest.memories) >= s.questions_min_new
    questions = await salient_questions(ctx, owner, digest) if asked else []
    ev, pairs = await gather(ctx, owner, digest, questions)
    texts = {i.ref: i.text for i in ev.items()}  # before the trim, for the backstop
    ev.trim(EVIDENCE_CHARS, ctx.tz)
    # What the prompt showed: the items left after the trim and, when there are questions, the
    # whole digest the questions call read, since the questions ride along in the think prompt.
    shown = list(dict.fromkeys([i.ref for i in ev.items()]
                               + ([f"memory:{m.id}" for m in digest.memories] if questions
                                  else [])))

    name, stance = ctx.name(owner), ctx.attitudes().get(owner)
    attitude = ATTITUDE_WORDS[stance].format(name=name, human=ctx.ident.called) if stance else ""
    system = system_prompt("reflect", REFLECT_PROMPT, s.data_dir, human=ctx.ident, name=name,
                           card=ctx.card(owner), attitude=attitude)
    state = store.get_state(owner)
    life = truncate_words(" ".join((state.life_thread
                                    or ctx.persona(owner).real_life.life_thread).split()), 200)
    user = _think_user(ctx, owner, ev, _cards(ctx, owner, digest.people), pairs, life, questions)
    notes = await ctx.reasoner.think(system, user)
    json_user = user + "\n\n" + REFLECT_ASK.format(name=name, n=s.max_outputs, occ=", ".join(OCC))
    shape = reflection_schema(s.max_outputs)
    fitted = _fit_notes(notes, system, json_user, shape)
    if fitted != notes:
        log("nightly_reflect_notes_cut", night=ctx.night, guid=owner, kept=len(fitted),
            of=len(notes))
    reply = await ctx.reasoner.json(system, json_user, shape, notes=fitted)
    facts = facts if facts is not None else load_facts(ctx)
    verdicts = await _grounded(ctx, to_claims(owner, reply, ev, facts), ev, facts,
                               stance is not None)
    accepted = [(c, v) for c, v in verdicts if v.ok]

    # The scope each memory will be stored with, before supersession sees it (PR7).
    shown_open = ctx.writer.scope_of(owner, shown) == OPEN
    private = [] if shown_open else [content_words(texts[r], facts.names) for r in shown
                                     if r in texts and ctx.writer.scope_of(owner, [r]) != OPEN]

    def shown_for(c: Claim, v: Verdict) -> tuple[str, ...]:
        """`shown` when the scope of what was shown must cover `c`, else (). The backstop holds
        an output already scoped by its own evidence too: its scope only rises, so one that
        overlaps a stricter shown thread is never left weaker than it (review-task11 I3)."""
        if c.kind in PARTLY and v.label == "partly":
            return tuple(shown)
        if (not shown_open and c.kind in BACKSTOPPED and v.label in (None, "yes")
                and _overlaps(c.text, private, facts)):
            return tuple(shown)
        return ()

    now = ctx.clock.now()
    week = week_bounds(iso_week(ctx.night), ctx.tz)[0]
    memories: list[_Memory] = []
    for c, v in accepted:
        if c.kind in BACKSTOPPED:
            extra = shown_for(c, v)
            key = (f"tom:{c.about}:{ctx.night}" if c.kind == "tom"
                   else f"diary:{ctx.night}" if c.kind == "diary" else None)
            row = _row(owner, "plan" if c.kind == "plan" else "reflection",
                       clean_memory_text(c.text, ctx.ident), c.evidence,
                       actors=() if c.about is None else (c.about,),
                       importance=c.extra["importance"], valence=c.valence, now=now, key=key)
            memories.append(_Memory(c, row, extra,
                                    ctx.writer.scope_of(owner, c.evidence, shown=extra)))
        elif c.kind == "nickname":
            coined = [m.claim for m in memories if m.claim.kind == "nickname"]
            why = ("not_open" if not shown_open
                   else "week" if (_coined_since(store, owner, week) + len(coined)
                                   >= s.nicknames_per_week)
                   else "held" if (_holds_nickname(store, owner, c.about)
                                   or any(n.about == c.about for n in coined))
                   else "vetoed" if _vetoed(store, c.text, c.about) else None)
            if why == "not_open":
                log("nickname_not_open", night=ctx.night, owner=owner)
            elif why is not None:
                log("nickname_not_coined", night=ctx.night, owner=owner, reason=why)
            else:  # the nickname's memory: all it rests on is open (shown_open)
                row = _row(owner, "reflection", clean_memory_text(
                    f"I call {ctx.name(c.about)} '{c.text}'.", ctx.ident), c.evidence,
                    actors=(c.about,), importance=c.extra["importance"], now=now,
                    key=f"nick:{c.about}")
                memories.append(_Memory(c, row, (), OPEN))
    vecs = await ctx.embed([m.row.text for m in memories]) if memories else []
    for m, vec in zip(memories, vecs):
        m.row = replace(m.row, embedding=vec)

    # Decisions are taken here, before the commit, against what is stored now: two near-identical
    # new beliefs of this unit never see each other, so both are stored.
    for m in memories:
        if m.claim.kind in SUPERSEDED:
            near = candidates(store, owner, m.claim.about, m.row.embedding, scope=m.stored)
            m.decisions = await decide(ctx, m.claim, near)
            twins = {d.memory_id for d in m.decisions if d.relation == "duplicates"}
            m.twins = [r.text for r in near if r.id in twins]
    has_attitude = stance is not None
    # A note is never added twice (review-task11 M1): an insight that restates a held note (in
    # other case or punctuation), that supersession folds into a reflection reading as one, or
    # that an earlier insight of this unit already put to the judge, is stored but not judged.
    held = {normalize_words(n.get("text", "")) for n in store.get_state(owner).self_notes}
    notes_due = []
    for m in memories:
        if (m.claim.kind == "self_insight" and m.stored == OPEN
                and growth.eligible(m.claim, ev, owner_has_attitude=has_attitude,
                                    human=ctx.human, store=store)):
            if {normalize_words(t) for t in (m.row.text, *m.twins)} & held:
                log("self_note_held", night=ctx.night, guid=owner)
                continue
            held.add(normalize_words(m.row.text))
            verdict, why = await growth.judge_core(ctx, owner, m.row.text)
            notes_due.append((replace(m.claim, text=m.row.text), verdict,
                              screen_text(why, ctx.ident)))

    def write(st: Store) -> None:
        _write(ctx, st, owner, now, verdicts, memories, notes_due, shown_open)
    if ctx.commit(STAGE, unit, write):
        log("nightly_reflect", night=ctx.night, guid=owner, questions=len(questions),
            evidence=len(ev), who_knows=len(pairs), accepted=len(accepted),
            rejected=len(verdicts) - len(accepted), memories=len(memories),
            self_notes=len(notes_due))
    return len(notes_due)


def _write(ctx: "NightContext", st: Store, owner: int, now: float,
           verdicts: list[tuple[Claim, Verdict]], memories: list["_Memory"],
           notes_due: list[tuple[Claim, str, str]], shown_open: bool) -> None:
    """The unit's one commit:
    - a rel_delta moves the owner's view through apply_capped (reason "reflection");
    - a belief or self-insight is a reflection memory through resolve, whose insert is a
      ctx.writer.put of the new row;
    - a plan is a plan memory, and the plans stored open replace persona_state.goals (at most 3;
      with none, the old goals stay);
    - a tom is a reflection memory keyed tom:{about}:{night}, the diary one keyed diary:{night};
    - a feeling is an emotion row, its why only when shown_open;
    - a nickname is a lexicon term with the owner its first adopter, and the memory
      "I call {name} '{term}'." keyed nick:{about}, citing the claim's refs (PR29);
    - self-notes through growth.apply_note; then log_verdicts."""
    speed = ctx.settings.relationship_speed
    for c, v in verdicts:
        if v.ok and c.kind == "rel_delta":
            apply_capped(st, night=ctx.night, a=owner, b=c.about, dim=c.dim, delta=c.delta,
                         speed=speed, reason="reflection", evidence=c.evidence, ts=now,
                         human=ctx.human)
    goals = []
    for m in memories:
        c = m.claim
        if c.kind in SUPERSEDED:
            resolve(st, m.decisions, c, lambda m=m: ctx.writer.put(m.row, shown=m.shown))
            continue
        if c.kind == "nickname":
            term = clean(c.text)
            st.conn.execute(
                "INSERT OR IGNORE INTO lexicon(kind, term, target, origin_ref, originator, born, "
                "last_used) VALUES ('nickname', ?, ?, ?, ?, ?, ?)",
                (term, c.about, c.evidence[0], owner, now, now))
            term_id = st.conn.execute("SELECT id FROM lexicon WHERE kind = 'nickname' "
                                      "AND term = ? AND target = ?",
                                      (term, c.about)).fetchone()["id"]
            st.conn.execute("INSERT OR IGNORE INTO lexicon_adopter(term, guid, first_use, via) "
                            "VALUES (?, ?, ?, ?)", (term_id, owner, now, owner))
        ctx.writer.put(m.row, shown=m.shown)
        if c.kind == "plan" and m.stored == OPEN:
            goals.append(m.row.text)
    if goals:
        state = st.get_state(owner)
        st.put_state(replace(state, goals=tuple(goals[:3]), updated=int(now)))
    for c, v in verdicts:
        if v.ok and c.kind == "emotion":
            st.conn.execute(
                "INSERT INTO emotion(guid, night, type, target, intensity, why, source, evidence, "
                "ts) VALUES (?, ?, ?, ?, ?, ?, 'reflect', ?, ?)",
                (owner, ctx.night, c.extra["type"], c.about or 0, c.extra["intensity"],
                 clean(screen_text(c.text, ctx.ident)) if shown_open else "",
                 json.dumps(list(c.evidence)), now))
    for c, verdict, why in notes_due:
        growth.apply_note(st, owner, c, verdict, why, now)
    log_verdicts(st, ctx.night, STAGE, verdicts, now, ctx.ident)


async def stage_reflect(ctx: "NightContext") -> None:
    """Stage "reflect" (the reflection model): reflect_member for every member of the cast, away
    ones too (a member on a break still has a day to think about), at most settings.parallel at
    once. The budget counts its units (budget.reflecting). The core judge's calls are recorded
    on their own, as the night's CORE_JUDGE row (night_stage), with their share of the pass's
    seconds: the runner's "reflect" row holds every call, and budget's measurements tell the
    core judge's apart from supersession's (review-task11 I1)."""
    members = ctx.members(include_away=True)
    if not members:
        return
    facts = load_facts(ctx)
    judged, started = 0, ctx.clock.now()
    calls, seconds = ctx.reasoner.calls, ctx.reasoner.seconds

    async def one(unit: str) -> None:
        nonlocal judged
        judged += await reflect_member(ctx, int(unit), facts=facts)
    await ctx.map_units(STAGE, [str(g) for g in members], one)
    if judged:
        calls, seconds = ctx.reasoner.calls - calls, ctx.reasoner.seconds - seconds
        stage_record(ctx.store, ctx.night, CORE_JUDGE, started=started, finished=ctx.clock.now(),
                     calls=judged, call_s=seconds * judged / max(calls, judged))


def _core_judge_count(ctx: "NightContext") -> int:
    """The budget's count for the core judge (review-task11 I1): the members who reflect
    tonight, each of whom may put a self-insight to it, whether or not anyone holds
    supersession's candidates; budget prices each at CORE_JUDGE_CALLS until measured."""
    return len(reflecting(ctx))


register_counter(CORE_JUDGE, _core_judge_count)
