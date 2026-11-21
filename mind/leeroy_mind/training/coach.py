"""Bounded overnight proposals grounded in eligible observed pulls.

The model proposes tactics, never combat facts. Durable lesson prose is assembled
from observed fact texts and validated, explicitly untested directives. The current
telemetry cannot prove mechanic death causes or tactic-caused improvements, so this
stage deliberately writes no learning memories until a trial producer supports them.
Training storage's public writers own transactions; this stage uses SQL only inside
NightContext.commit, sharing its rollback and persistent night_step checkpoint.
"""
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
import json
import logging
import math
import re

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.llm import (CTX_SLACK, JSON_PREDICT, THINK_OPTIONS, THINK_PREDICT_MAX,
                                     THINK_PREDICT_MIN, THINK_SLACK, schema, system_prompt)
from leeroy_mind.nightly.runner import NightContext
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.storage.store import clean
from leeroy_mind.wow import boss_name, is_raid_fight

from .score import strategy_id
from . import store
from .cards import (Card, Directive, LEVERS, Targets, describe, nearest, stock_card,
                    validate_card, validate_directive)
from .score import SCORE_BASES, is_farm
from .vocab import Vocab


COACH_DEFAULT = """Review this boss's observed killed and wiped pulls. Compare what is
known, respect metric coverage, and propose at most two separate one-lever experiments.
Cards are candidates, never applied lessons or proven improvements. Use only the
supplied vocabulary, without naming people. Calls are prospective instructions of at
most 60 characters. Lesson is at most 200 characters. Points are supplied fact IDs only.
Requested pre-AI damage is not effective damage or DPS. Raw minus effective healing
is not exact overheal or efficiency. Thread pairing cannot prove a death cause.
Incomplete checks are lower bounds, never proof of success. Do not invent coverage,
causes, causal improvement, interruptions landed, or unsupported comparisons.
Library mechanics and other cards are background, not evidence about these pulls.
Use short tactical calls such as Spread out, Stack on the boss, Stay behind the boss,
Focus adds, Hold position, Save mana, Wait 5 seconds, or Use lust at 30 percent.
Do not add explanations or claimed results to calls.
A lever option with whole_numbers_from and to admits every whole number between them, ends
included. Parts marked not_shown were left out for length; never guess their contents."""

COACH_SCHEMA = schema("coach", {
    "lesson": {"type": "string"},
    "cards": {"type": "array", "maxItems": 2, "items": schema("coach_card", {
        "lever": {"type": "string"}, "value": {"type": ["string", "integer"]},
        "roles": {"type": "array", "items": {"type": "string"}},
        "call": {"type": "string"}, "why": {"type": "string"}})},
    "call": {"type": "string"},
    "points": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
})


# The prompt is bounded here, by construction: past num_ctx Ollama keeps only the prompt's tail
# (on 2026-11-17 six prompts of about 10,000 tokens were cut to 4,098), so nothing may rely on
# its truncation. Both passes must fit: the reasoning pass with its smallest answer room, and the
# answer pass with the schema, a repair line and at least NOTES_MIN tokens of the notes.
NUM_CTX = THINK_OPTIONS["num_ctx"]
NOTES_MIN = 600        # the reasoning notes always get this much room in the answer pass
REPAIR_ROOM = 160      # "Repair these validation failures: <codes>"
NOTES_CHARS = 3        # characters a token of prose, on the safe side
PULLS_MAX = 40         # the newest pulls shown; older new pulls stay in the lesson's evidence
FAILURES_MAX = 12      # a pull's failed checks shown, the most frequent first


def prompt_tokens(text: str) -> int:
    """A safe token count for this prompt. It is JSON with identifiers and numbers, which run
    far denser than prose's four characters a token: the 2026-11-17 prompts measured about 2.2
    characters a token outside their number lists and about one inside. So every digit and
    punctuation mark counts as a token, letters three to a token, blanks two."""
    letters = sum(c.isalpha() for c in text)
    blanks = sum(c.isspace() for c in text)
    return (len(text) - letters - blanks) + math.ceil(letters / 3) + math.ceil(blanks / 2)


def user_budget(system: str) -> int:
    """The tokens the question may take beside `system`, which is never cut."""
    fixed = prompt_tokens(system)
    think = NUM_CTX - THINK_PREDICT_MIN - THINK_SLACK - fixed
    answer = (NUM_CTX - JSON_PREDICT - CTX_SLACK - fixed - prompt_tokens(json.dumps(COACH_SCHEMA))
              - REPAIR_ROOM - NOTES_MIN)
    return min(think, answer)


def think_room(system: str, user: str) -> int:
    """The reasoning pass's num_predict: what the two messages leave, by this module's count."""
    room = NUM_CTX - prompt_tokens(system) - prompt_tokens(user) - THINK_SLACK
    return max(THINK_PREDICT_MIN, min(THINK_PREDICT_MAX, room))


def fit_notes(notes: str, system: str, user: str) -> str:
    """The notes' end (where the conclusions are) that fits the answer pass beside `user`."""
    room = (NUM_CTX - JSON_PREDICT - CTX_SLACK - prompt_tokens(system) - prompt_tokens(user)
            - prompt_tokens(json.dumps(COACH_SCHEMA)) - REPAIR_ROOM)
    room = max(0, room) * NOTES_CHARS
    return notes if len(notes) <= room else notes[len(notes) - room:]


@dataclass(frozen=True)
class CoachSources:
    """Task20 binds these synchronous current-snapshot providers in a stage closure.

    mechanics(context) adapts TrainingPorts.boss_mechanics to the context key.
    No provider is allowed to issue a bridge/model request inside the commit.
    Missing sources admit no directives and leave existing cards untouched.
    """
    vocab: Callable[[], Vocab]
    mechanics: Callable[[str], Sequence[str]] = lambda context: ()


@dataclass(frozen=True)
class CoachFact:
    id: str
    text: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CoachOutput:
    lesson: str
    cards: tuple[Card, ...]
    call: str
    points: tuple[str, ...]
    reasons: tuple[str, ...]


def _short(text, limit=200):
    text = clean(text).strip()
    if len(text) <= limit:
        return text
    ends = [match for match in re.finditer(r"[.!?](?:\s|$)", text[:limit + 1])
            if match.start() < limit]
    if ends:
        return text[:ends[-1].start() + 1]
    cut = text[:limit]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def _names(text, names, human):
    return (any(name and has_word(text, name) for name in
                (*names, human.called, human.character_name)) or names_real_name(text, human))


# Deliberately restricted first-version command grammar. Full-string matching
# prevents an imperative prefix from smuggling a causal or retrospective clause.
# These words only change the spoken call, never a bot directive or timing value.
_CALL = re.compile(r"(?:"
    r"spread(?: out| apart)?|stack(?: up| together| on (?:the )?(?:boss|tank))?"
    r"|stay (?:behind (?:the )?boss|spread|together|in range|out of (?:the )?fire)"
    r"|move (?:out|apart|behind (?:the )?boss|away from (?:the )?boss|out of (?:the )?fire)"
    r"|(?:focus|kill|switch to) (?:the )?(?:adds|boss)"
    r"|hold (?:position|the line|damage|fire)|(?:save|conserve) mana"
    r"|wait (?:[0-9]|10) seconds?(?: before attacking)?"
    r"|(?:use|save|hold) (?:lust|bloodlust|heroism)(?: now| at (?:[2-9][05]|100)(?: percent|%))?"
    r"|keep (?:your distance|distance|spread|together|behind (?:the )?boss)"
    r"|avoid (?:the )?(?:fire|ground effects|front|cleave)"
    r"|watch (?:the )?(?:adds|boss|ground|mana)|heal (?:the )?(?:tanks|raid)"
    r"|interrupt (?:the )?(?:casts|boss)"
    r")", re.IGNORECASE)


def _prospective_call(text):
    return not text or all(_CALL.fullmatch(part.strip()) for part in
                           re.split(r"[;.!]", text.rstrip(".!")))


def validate_output(out, vocab, *, names, human, facts) -> CoachOutput:
    """Return safe fragments plus repair reasons; model wording is never a fact."""
    reasons, cards, points = [], [], []
    lesson = clean(out.get("lesson", "")).strip()
    if _names(lesson, names, human):
        reasons.append("lesson_names_someone")
        lesson = ""
    lesson = _short(lesson)
    for raw in out.get("cards", ())[:2]:
        card = Card(None, "", 1, "candidate", "coach",
                    (Directive(raw["lever"], raw["value"], Targets(tuple(raw["roles"]))),),
                    call=clean(raw["call"]).strip())
        bad = validate_card(card, vocab, names=names, human=human)
        # Screen every proposed public field, including fields we do not persist.
        if _names(str(raw["value"]) + " " + raw["lever"] + " " + raw["why"], names, human):
            bad.append("card_names_someone")
        if bad:
            reasons.extend(bad)
        else:
            if not _prospective_call(card.call):
                reasons.append("call_not_prospective")
                card = replace(card, call="")
            cards.append(replace(card, reason="Untested coach proposal."))
    call = clean(out.get("call", "")).strip()
    bad = validate_card(Card(None, "", 1, "candidate", "stock", (), call=call),
                        vocab, names=names, human=human)
    if bad:
        reasons.extend(bad)
        call = ""
    elif not _prospective_call(call):
        reasons.append("call_not_prospective")
        call = ""
    for ident in out.get("points", ())[:3]:
        fact = facts.get(ident)
        if not isinstance(fact, CoachFact) or fact.id != ident:
            reasons.append("unknown_fact")
        elif _names(fact.text, names, human):
            reasons.append("fact_names_someone")
        elif ident not in points:
            points.append(ident)
    return CoachOutput(lesson, tuple(cards), call, tuple(points), tuple(dict.fromkeys(reasons)))


def _eligible(row):
    return (strategy_id(row) == 0 and row["complete"] == 1 and row["scoreable"] == 1
            and row["outcome"] in {"killed", "wiped"}
            and row["end_ms"] > row["start_ms"] >= 0
            and row["score"] is not None and row["score_basis"] in SCORE_BASES
            and is_raid_fight(row["map"], row["boss_id"]))


def _history(ctx):
    """All covered IDs are receipts, so late-arriving earlier fights remain new."""
    conn = ctx.store.conn
    histories, covered, done = defaultdict(list), defaultdict(set), set()
    for row in conn.execute("SELECT context,night,evidence FROM tg_lesson ORDER BY night"):
        covered[row["context"]].update(json.loads(row["evidence"]))
        if row["night"] == ctx.night:
            done.add(row["context"])
    cutoff = min(ctx.window[1], ctx.clock.now())
    for row in conn.execute("SELECT * FROM tg_pull WHERE received<=? ORDER BY start_ms,id", (cutoff,)):
        if _eligible(row):
            histories[row["context"]].append(dict(row))
    return histories, covered, done


def _new(rows, covered):
    return [row for row in rows if f"pull:{row['id']}" not in covered]


def coach_units(ctx: NightContext) -> list[str]:
    histories, covered, done = _history(ctx)
    ranked = []
    for context, rows in histories.items():
        new = _new(rows, covered[context])
        if not new or context in done or ctx.done("coach", context):
            continue
        # A concluded trial must be newer than the last committed lesson receipt.
        last = ctx.store.conn.execute(
            "SELECT max(s.finished) FROM night_step s JOIN tg_lesson l ON l.night=s.night "
            "AND l.context=s.unit WHERE s.stage='coach' AND l.context=?", (context,)).fetchone()[0]
        trial = ctx.store.conn.execute("SELECT 1 FROM tg_trial WHERE scope=? AND status!='open' "
                                      "AND closed>? LIMIT 1", (context, last or 0)).fetchone()
        previous = [r["outcome"] for r in rows if f"pull:{r['id']}" in covered[context]]
        farm = is_farm([r["outcome"] for r in rows]) and not is_farm(previous)
        rank = 0 if any(r["outcome"] == "wiped" for r in new) else 1 if trial else 2 if farm else 3
        ranked.append((rank, context))
    # Completed units consume the night's cap even after restart/reselection.
    remaining = max(0, ctx.settings.coach_units - len(done))
    return [context for _, context in sorted(ranked)[:remaining]]


def _facts(rows):
    facts = {}
    for row in rows:
        seconds = (row["end_ms"] - row["start_ms"]) / 1000
        ident = f"pull:{row['id']}:result"
        text = f"Observed {row['outcome']} pull: {row['deaths']} deaths over {seconds:g} seconds."
        facts[ident] = CoachFact(ident, text, (f"pull:{row['id']}",))
    return facts


def _vocab(sources):
    return sources.vocab() if sources is not None else Vocab({}, {}, {}, frozenset(), 0)


def _allowed_menu(vocab):
    """Only combinations the current snapshot admits, using the validator itself.

    Empty roles mean the lever's default targets, not a wildcard bypass. Rotation
    requires a class/spec selector unavailable in this model-output schema.
    """
    menu = {}
    if not vocab.complete:
        return menu
    for name, lever in LEVERS.items():
        if lever.kind == "rotation":
            continue
        options = []
        for size in range(min(2, len(lever.roles)) + 1):
            for roles in combinations(lever.roles, size):
                values = [value for value in lever.values if validate_directive(
                    Directive(name, value, Targets(roles)), vocab) is None]
                if values:
                    options.append(dict(roles=list(roles)) | _values(values))
        if options:
            menu[name] = options
    return menu


def _values(values):
    """A run of whole numbers as its two ends: one hundred listed integers cost the model about
    four hundred tokens a lever, and seventeen dungeon-clear levers have such a run."""
    if (len(values) >= 3 and all(type(v) is int for v in values)
            and values == list(range(values[0], values[0] + len(values)))):
        return dict(whole_numbers_from=values[0], to=values[-1])
    return dict(values=values)


def _render(parts):
    return json.dumps(parts, ensure_ascii=True)


def _shrink(parts, budget):
    """Cut evidence, least needed first, until the question fits `budget`; what was cut, by
    count. The context, the facts' ids of the pulls still shown and the vocabulary a card must
    use go last. A question that cannot fit is refused: it is never sent to be truncated."""
    dropped = defaultdict(int)
    # One full count, then each cut subtracts no more than it freed, so the running size never
    # falls below the true one; the result is counted in full once more at the end.
    size = prompt_tokens(_render(parts))

    def cost(value):
        return prompt_tokens(_render(value))

    def note(holder, key):
        """Count one more thing left out of `holder`; the tokens its marker may add."""
        first = key not in holder
        holder[key] = holder.get(key, 0) + 1
        return cost({key: holder[key]}) + 2 if first else 1

    def pop(key, index=-1):
        def step():
            if not parts[key]:
                return None
            dropped[key] += 1
            return cost(parts[key].pop(index))
        return step

    def failure():
        pull = max(parts["pulls"], key=lambda p: len(p["observed_failures_lower_bound"]), default=None)
        if pull is None or not pull["observed_failures_lower_bound"]:
            return None
        dropped["failures"] += 1
        # the least frequent: they are sorted
        return cost(pull["observed_failures_lower_bound"].pop()) - note(pull, "failures_not_shown")

    def coverage():
        pull = next((p for p in parts["pulls"] if p["coverage"]), None)  # the oldest first
        if pull is None:
            return None
        freed = cost(pull["coverage"]) - cost([])
        pull["coverage"] = []
        dropped["coverage"] += 1
        return freed - note(pull, "coverage_not_shown")

    def pull():
        if len(parts["pulls"]) <= 1:
            return None
        gone = parts["pulls"].pop(0)  # the oldest
        prefix = f"pull:{gone['pull']}:"
        freed = cost(gone) + sum(cost(f) for f in parts["facts"] if f["id"].startswith(prefix))
        parts["facts"] = [f for f in parts["facts"] if not f["id"].startswith(prefix)]
        dropped["pulls"] += 1
        return freed - note(parts, "older_pulls_not_shown")

    def lever():
        levers = parts["vocabulary"]["levers"]
        if not levers:
            return None
        name = next(reversed(levers))
        dropped["levers"] += 1
        return cost(name) + cost(levers.pop(name)) - note(parts["vocabulary"], "levers_not_shown")

    # lessons are newest first, trials and cards oldest first
    for step in (pop("neighbors"), pop("mechanics"), pop("lessons"), pop("trials", 0),
                 pop("cards", 0), failure, coverage, pull, lever):
        while size > budget and (freed := step()) is not None:
            size -= freed
    if prompt_tokens(_render(parts)) > budget:
        raise ValueError("coach prompt cannot fit the model context")
    return dict(dropped)


def _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors, *, budget):
    """The question as JSON within `budget` tokens (prompt_tokens), and what was left out."""
    cards = [Card.from_row(row) for row in store.card_rows(ctx.store.conn, context)]
    trials = [dict(row) for row in store.trial_rows(ctx.store.conn, context)]
    checks = []
    shown = rows[-PULLS_MAX:]
    for row in shown:
        # Exclude requested damage, misleading death labels and unverified spec metrics.
        failures = [dict(f) for f in ctx.store.conn.execute(
            "SELECT kind,key,count FROM tg_failure WHERE pull=? AND count>0 "
            "ORDER BY count DESC,id", (row["id"],))]
        check = dict(pull=row["id"], outcome=row["outcome"],
                     score_basis=row["score_basis"], complete=bool(row["checks_complete"]),
                     coverage=json.loads(row["checks_coverage"]),
                     observed_failures_lower_bound=failures[:FAILURES_MAX])
        if len(failures) > FAILURES_MAX:
            check["failures_not_shown"] = len(failures) - FAILURES_MAX
        checks.append(check)
    ids = {f"pull:{row['id']}" for row in shown}
    parts = dict(context=context, boss=boss_name(rows[0]["map"], rows[0]["boss_id"], "boss"),
        metric_coverage=dict(damage="requested_pre_ai, not verified damage or DPS",
            causes="unknown", healing_efficiency="unknown", interruptions_landed="unknown",
            improvement_attribution="unknown; proposals have not been applied"),
        pulls=checks, facts=[dict(id=f.id, text=f.text) for f in facts.values()
                             if set(f.evidence) <= ids],
        cards=[dict(id=c.id, status=c.status, proposal=describe(c)) for c in cards],
        trials=trials, lessons=[dict(row) for row in store.lessons(ctx.store.conn, context)],
        mechanics=list(mechanics), vocabulary=dict(complete=vocab.complete,
            levers=_allowed_menu(vocab)),
        neighbors=[dict(context=c.context, proposal=describe(c)) for c in neighbors])
    if len(rows) > len(shown):
        parts["older_pulls_not_shown"] = len(rows) - len(shown)
    dropped = _shrink(parts, budget)
    if len(rows) > len(shown):
        dropped["pulls"] = dropped.get("pulls", 0) + len(rows) - len(shown)
    return _render(parts), dropped


def _insert_card(conn, card):
    row = card.to_row()
    row.pop("id")
    for key in ("directives", "assignment", "evidence", "vetoes"):
        row[key] = json.dumps(row[key], ensure_ascii=True)
    conn.execute("INSERT INTO tg_card (" + ",".join(row) + ") VALUES (" +
                 ",".join("?" for _ in row) + ")", tuple(row.values()))


def _durable_lesson(out, facts):
    # Free-form model prose cannot launder an unsupported causal/DPS claim into memory.
    parts = [facts[ident].text for ident in out.points]
    for card in out.cards:
        directive = card.directives[0]
        parts.append(f"Untested proposal: {directive.lever} {directive.value}.")
    return _short(" ".join(parts) or "Review recorded outcomes; no verified improvement established.")


async def stage_coach(ctx: NightContext, *, sources: CoachSources | None = None) -> None:
    async def unit(context):
        histories, covered, _ = _history(ctx)
        rows = _new(histories.get(context, []), covered[context])
        if not rows:
            ctx.skip("coach", context)
            return
        names = {member.name for member in ctx.store.cast().values()}
        facts = _facts(rows)
        facts = {ident: fact for ident, fact in facts.items() if not _names(fact.text, names, ctx.ident)}
        vocab = _vocab(sources)
        current = store.champion(ctx.store.conn, context)
        query = Card.from_row(current) if current is not None else stock_card(context)
        vecs = await ctx.embed([describe(query)])
        neighbors = nearest(ctx.store.conn, vecs[0], exclude=context)
        mechanics = sources.mechanics(context) if sources is not None else ()
        system = system_prompt("coach", COACH_DEFAULT, ctx.settings.data_dir, human=ctx.ident)
        budget = user_budget(system)
        user, dropped = _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors, budget=budget)
        if dropped:
            log("coach_prompt_trimmed", level=logging.WARNING, context=context,
                tokens=prompt_tokens(user), budget=budget, dropped=dropped)
        notes = await ctx.reasoner.think(system, user, num_predict=think_room(system, user))
        raw = await ctx.reasoner.json(system, user, COACH_SCHEMA, notes=fit_notes(notes, system, user))
        out = validate_output(raw, vocab, names=names, human=ctx.ident, facts=facts)
        if out.reasons:
            vocab = _vocab(sources)
            user, _ = _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors, budget=budget)
            repair = "\nRepair these validation failures: " + ", ".join(out.reasons)
            if prompt_tokens(repair) > REPAIR_ROOM:
                raise ValueError("coach repair line cannot fit the model context")
            raw = await ctx.reasoner.json(system, user + repair, COACH_SCHEMA,
                                          notes=fit_notes(notes, system, user))
            out = validate_output(raw, vocab, names=names, human=ctx.ident, facts=facts)
        for reason in out.reasons:
            log("coach_dropped", reason=reason)
        evidence = tuple(f"pull:{r['id']}" for r in rows)
        now = ctx.clock.now()
        candidates = [replace(card, context=context, evidence=evidence, created=now, changed=now)
                      for card in out.cards]
        vectors = await ctx.embed([describe(card) for card in candidates]) if candidates else []
        if len(vectors) != len(candidates):
            raise ValueError("coach embeddings missing")
        candidates = [replace(card, embedding=vec) for card, vec in zip(candidates, vectors)]
        # A replacement stock call is embedded too, before the transaction.
        call_vec = (await ctx.embed([out.call]))[0] if out.call else None
        lesson = _durable_lesson(out, facts)

        def write(db):
            conn = db.conn
            if conn.execute("SELECT 1 FROM tg_lesson WHERE context=? AND night=?",
                            (context, ctx.night)).fetchone():
                return
            # Awaiting models/embeddings can allow rechecks or eligibility vetoes.
            for old in rows:
                row = conn.execute("SELECT * FROM tg_pull WHERE id=?", (old["id"],)).fetchone()
                if row is None or not _eligible(row) or any(row[key] != old[key] for key in
                        ("checks_version", "score_basis", "score", "deaths", "start_ms", "end_ms")):
                    raise ValueError("coach evidence changed")
            fresh_vocab = _vocab(sources)
            for card in candidates:
                if validate_card(card, fresh_vocab, names=names, human=ctx.ident):
                    raise ValueError("coach vocabulary changed")
            version = conn.execute("SELECT coalesce(max(version),0) FROM tg_card WHERE context=?",
                                   (context,)).fetchone()[0]
            for card in candidates:
                version += 1
                _insert_card(conn, replace(card, version=version))
            champion = store.champion(conn, context)
            if out.call and champion is not None:
                prior = Card.from_row(champion)
                if prior.origin == "stock" and not prior.directives and prior.call != out.call:
                    conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE id=?",
                                 (now, prior.id))
                    version += 1
                    _insert_card(conn, replace(prior, id=None, version=version, call=out.call,
                        lineage=prior.lineage or prior.id, prev_champion=prior.id, evidence=evidence,
                        embedding=call_vec, created=now, changed=now))
            conn.execute("INSERT INTO tg_lesson(context,night,text,evidence) VALUES (?,?,?,?)",
                         (context, ctx.night, lesson, json.dumps(evidence)))
            for ident in out.points:
                conn.execute("INSERT INTO tg_point(context,night,fact,text) VALUES (?,?,?,?)",
                             (context, ctx.night, ident, facts[ident].text))
        ctx.commit("coach", context, write)

    await ctx.map_units("coach", coach_units(ctx), unit)


register_counter("coach", lambda ctx: len(coach_units(ctx)))
