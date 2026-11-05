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
import re

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.runner import NightContext
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.storage.store import clean
from leeroy_mind.wow import boss_name, is_raid_fight

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
Do not add explanations or claimed results to calls."""

COACH_SCHEMA = schema("coach", {
    "lesson": {"type": "string"},
    "cards": {"type": "array", "maxItems": 2, "items": schema("coach_card", {
        "lever": {"type": "string"}, "value": {"type": ["string", "integer"]},
        "roles": {"type": "array", "items": {"type": "string"}},
        "call": {"type": "string"}, "why": {"type": "string"}})},
    "call": {"type": "string"},
    "points": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
})


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
    return (row["complete"] == 1 and row["scoreable"] == 1
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
                    options.append(dict(roles=list(roles), values=values))
        if options:
            menu[name] = options
    return menu


def _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors):
    cards = [Card.from_row(row) for row in store.card_rows(ctx.store.conn, context)]
    trials = [dict(row) for row in store.trial_rows(ctx.store.conn, context)]
    checks = []
    for row in rows:
        # Exclude requested damage, misleading death labels and unverified spec metrics.
        failures = [dict(f) for f in ctx.store.conn.execute(
            "SELECT kind,key,count FROM tg_failure WHERE pull=? AND count>0", (row["id"],))]
        checks.append(dict(pull=row["id"], outcome=row["outcome"],
                           score_basis=row["score_basis"], complete=bool(row["checks_complete"]),
                           coverage=json.loads(row["checks_coverage"]),
                           observed_failures_lower_bound=failures))
    return json.dumps(dict(context=context, boss=boss_name(rows[0]["map"], rows[0]["boss_id"], "boss"),
        metric_coverage=dict(damage="requested_pre_ai, not verified damage or DPS",
            causes="unknown", healing_efficiency="unknown", interruptions_landed="unknown",
            improvement_attribution="unknown; proposals have not been applied"),
        pulls=checks, facts=[dict(id=f.id, text=f.text) for f in facts.values()],
        cards=[dict(id=c.id, status=c.status, proposal=describe(c)) for c in cards],
        trials=trials, lessons=[dict(row) for row in store.lessons(ctx.store.conn, context)],
        mechanics=list(mechanics), vocabulary=dict(complete=vocab.complete,
            levers=_allowed_menu(vocab)),
        neighbors=[dict(context=c.context, proposal=describe(c)) for c in neighbors]), ensure_ascii=True)


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
        user = _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors)
        system = system_prompt("coach", COACH_DEFAULT, ctx.settings.data_dir, human=ctx.ident)
        notes = await ctx.reasoner.think(system, user)
        raw = await ctx.reasoner.json(system, user, COACH_SCHEMA, notes=notes)
        out = validate_output(raw, vocab, names=names, human=ctx.ident, facts=facts)
        if out.reasons:
            vocab = _vocab(sources)
            user = _prompt(ctx, context, rows, facts, vocab, mechanics, neighbors)
            raw = await ctx.reasoner.json(system, user + "\nRepair these validation failures: " +
                                          ", ".join(out.reasons), COACH_SCHEMA, notes=notes)
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
