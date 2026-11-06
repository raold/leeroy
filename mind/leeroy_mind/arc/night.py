"""The night stage `arc`: the romance once she has reached planning. stage_romance climbs her
to planning; from there the arc floats a plan, reads what he said to her about it, takes her to
the weekend and what comes after, and rolls the risk of an affair found out at home.

stage_arc runs six steps in this order; each unit commits through ctx.commit("arc", unit, ...):
1. settle (unit "settle"): her time away ends, and a weekend becomes what comes after
   (arc.weekend.settle).
2. propose (unit "propose"): each full-arc woman (a woman whose stance is in FULL_ARC) at
   planning with no plan floats one (plan.propose_plan), a hope from now. No model call; a fond
   woman never gets a plan.
3. read (units "read:<guid>"): each full-arc woman on the arc (READ_STAGES) with lines from him
   in the window gets one JSON call (READ_SCHEMA) on what he said. His answer moves the plan
   (moved); a plan he never engages stays a hope for good. A breakup ends her romance
   (state.end_romance, by "he_ended") only when the model reads one and one of his lines says
   so (ENDING_RE).
4. meetup (unit "meetup"): the weekend, or a later visit, in a gap of his play once he has
   said yes to her plan (arc.weekend.meetup).
5. milestones (units "after:<guid>"): the milestones after the weekend, her next visit
   floated as a new plan, and the affair roll for a woman with a partner at home
   (arc.after.milestones). No model call.
6. noticed (unit "noticed"): the stages the guild can tell, written as shared facts that every
   other member learns (see noticed).

His lines are his own lines in the window: his whispers to her and his lines she answered (an
utterance of hers, said, whose trigger_line is the line). Hers are her said whispers to him in
the window and those answers. Nothing else reaches the read: no other member's line and no guild
line she did not answer (whisper scope). His real name is redacted to what the guild calls him.
The read's log carries her guid and its labels, never a line, a name or the plan."""
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from leeroy_mind.arc.after import milestones
from leeroy_mind.arc.ladder import FULL_ARC, NOTICED
from leeroy_mind.arc.plan import plan_summary, propose_plan
from leeroy_mind.arc.state import Arc, end_romance, get_arc, put_arc
from leeroy_mind.arc.weekend import due, meetup, settle
from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.knowledge import add_fact, learn
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup

if TYPE_CHECKING:
    import random

    from leeroy_mind.nightly.runner import NightContext

STAGE = "arc"
DAY_S = 86400.0
READ_STAGES = ("planning", "weekend", "after")  # the stages the arc moves her through
LIVE_PLANS = ("hoped", "talked", "set")
PUSH_S = {"not_yet": 7 * DAY_S, "later": 14 * DAY_S}  # how far each answer moves the target
# The newest lines of theirs that fit, so a long night of whispers stays well inside the
# reflection model's num_ctx with the prompt, the schema and the answer.
READ_CHARS = 12000
ENDING_RE = re.compile(r"\b(break(ing)? up|it'?s over|we'?re done|end (this|it|us)"
                       r"|not doing this anymore|see other people)\b", re.IGNORECASE)

MARK = "arc_noticed_mark"
HANDLED = "arc_noticed:"
# What the guild can tell, generic: her name and his called name, no word from any whisper.
NOTICED_TEXT = {
    "voice": "{name} vents to {called} late at night a lot these days.",
    "planning": "{name} keeps talking about meeting {called} for real.",
    "weekend": "{name} went away to meet {called} for a weekend.",
    "after": "{name} and {called} are together now.",
}
NOTICED_CONFIDENCE = {"voice": 0.6, "planning": 0.5, "weekend": 1.0, "after": 1.0}

READ_SCHEMA = schema("arc_read", {
    "plan": {"type": "string", "enum": ["yes", "not_yet", "elsewhere", "later", "none"]},
    "breakup": {"type": "boolean"}})
# What the read is told when no plan of hers is on the table: none yet, a plan floated tonight
# (she has not brought it up with him), or one already done.
NO_PLAN = "No plan to meet is on the table yet."
READ_PROMPT = (
    "{name} and {called} know each other through the game and live apart. {plan}\n"
    "Read what {called} and {name} said to each other and answer two questions about "
    "{called}'s own words.\n"
    "plan: what {called} said about that plan. \"yes\" when {called} agreed to it, \"not_yet\" "
    "when {called} wants it but not so soon, \"later\" when {called} wants it put off for a "
    "good while, \"elsewhere\" when {called} wants to meet some other way or somewhere else, "
    "and \"none\" when {called} said nothing clear about it or there is no plan.\n"
    "breakup: true only when {called} plainly ended things with {name}. Teasing, a bad mood, "
    "a joke or a quiet night is not a breakup.")
READ_ASK = "What {called} and {name} said to each other, oldest first:\n{lines}"


# -- who ---------------------------------------------------------------------------------------

def _women(ctx: "NightContext", *, fresh: bool) -> list[int]:
    """The full-arc women of the cast, away ones included, sorted."""
    return sorted(g for g, stance in ctx.attitudes(fresh=fresh).items() if stance in FULL_ARC)


def _his_lines(store: Store, guid: int, human: int,
               window: tuple[float, float]) -> list[tuple[float, int, str]]:
    """(ts, id, text) of his lines in the window that were to her: his whispers to her and his
    lines she answered with a said line; oldest first."""
    with store.transaction():
        return [(r["ts"], r["id"], r["text"]) for r in store.conn.execute(
            "SELECT id, ts, text FROM chat_line c WHERE from_guid = ? AND origin = 'human' "
            "AND ts >= ? AND ts < ? AND ((channel = 'whisper' AND to_guid = ?) OR EXISTS ("
            "SELECT 1 FROM utterance u WHERE u.trigger_line = c.id AND u.guid = ? "
            "AND u.dropped IS NULL)) ORDER BY ts, id", (human, *window, guid, guid))]


def _her_lines(store: Store, guid: int, human: int, window: tuple[float, float],
               his: list[int]) -> list[tuple[float, int, str]]:
    """(ts, id, text) of her said lines to him: her whispers to him in the window and her
    answers to his lines `his`; oldest first."""
    with store.transaction():
        return [(r["ts"], r["id"], r["text"]) for r in store.conn.execute(
            "SELECT id, ts, text FROM utterance WHERE guid = ? AND dropped IS NULL AND ("
            "(channel = 'whisper' AND to_guid = ? AND ts >= ? AND ts < ?) "
            "OR trigger_line IN (SELECT value FROM json_each(?))) ORDER BY ts, id",
            (guid, human, *window, json.dumps(his)))]


def _readers(ctx: "NightContext", *, fresh: bool) -> list[int]:
    """The women the read reads tonight: full-arc, on the arc, with lines from him."""
    store, human = ctx.store, ctx.human
    return [g for g in _women(ctx, fresh=fresh)
            if store.get_relationship(g, human).romance_stage in READ_STAGES
            and _his_lines(store, g, human, ctx.window)]


# -- step 2: propose ---------------------------------------------------------------------------

def propose(ctx: "NightContext") -> None:
    """Unit "propose": each full-arc woman at planning whose arc has no plan floats one
    (propose_plan with the night's generator for her), state "hoped", planning since now."""
    if ctx.done(STAGE, "propose"):
        return
    now = ctx.clock.now()
    women = _women(ctx, fresh=True)
    floated: list[int] = []

    def write(store: Store) -> None:
        for guid in women:
            if store.get_relationship(guid, ctx.human).romance_stage != "planning":
                continue
            arc = get_arc(store, guid, ctx.human)
            if arc.plan_state != "none":
                continue
            plan = propose_plan(ctx.persona(guid), ctx.rng(STAGE, "propose", guid), now=now,
                                partner=arc.partner)
            put_arc(store, replace(arc, plan=plan, plan_state="hoped", planning_since=now,
                                   updated=now))
            floated.append(guid)
    if ctx.commit(STAGE, "propose", write) and floated:
        log("arc_propose", night=ctx.night, guids=floated)


# -- step 3: read ------------------------------------------------------------------------------

def on_the_table(arc: Arc, window: tuple[float, float]) -> bool:
    """Whether her plan is one he could have talked about in the window: hoped, talked or set,
    and floated before the window closed (one floated tonight waits for the next night)."""
    return (arc.plan_state in LIVE_PLANS and bool(arc.plan)
            and (arc.planning_since is None or arc.planning_since < window[1]))


def moved(arc: Arc, answer: str, p: Persona, rng: "random.Random", *, now: float) -> Arc:
    """The arc after his answer about its plan. yes: set, from now (a set plan keeps its place);
    not_yet and later: talked, the target PUSH_S later; elsewhere: talked, a plan of another
    kind (propose_plan); none: unchanged. A plan no longer set loses plan_at."""
    if answer == "yes":
        if arc.plan_state == "set":
            return arc
        return replace(arc, plan_state="set", plan_at=now, updated=now)
    if answer in PUSH_S:
        plan = arc.plan | {"target": arc.plan["target"] + PUSH_S[answer]}
    elif answer == "elsewhere":
        plan = propose_plan(p, rng, now=now, exclude=arc.plan["kind"], partner=arc.partner)
    else:
        return arc
    return replace(arc, plan=plan, plan_state="talked", plan_at=None, updated=now)


def _conversation(ctx: "NightContext", guid: int) -> tuple[list[str], list[str]]:
    """(the lines the read shows, "<who>: <text>" oldest first, the newest that fit in
    READ_CHARS; his texts among them)."""
    store, human, called = ctx.store, ctx.human, ctx.ident.called
    his = _his_lines(store, guid, human, ctx.window)
    hers = _her_lines(store, guid, human, ctx.window, [i for _, i, _ in his])
    rows = sorted([(ts, 0, i, called, text) for ts, i, text in his]
                  + [(ts, 1, i, ctx.name(guid), text) for ts, i, text in hers])
    shown: list[tuple[int, str, str]] = []
    size = 0
    for _, side, _, who, text in reversed(rows):
        said = redact_real_name(strip_wow_markup(text), ctx.ident, called)
        line = f"{who}: {said}"
        if shown and size + len(line) + 1 > READ_CHARS:
            break
        shown.append((side, said, line))
        size += len(line) + 1
    shown.reverse()
    return [line for _, _, line in shown], [said for side, said, _ in shown if side == 0]


async def _read(ctx: "NightContext", guid: int) -> None:
    """Unit "read:<guid>": one JSON call over their lines, then the plan move or the ending."""
    unit, store, human = f"read:{guid}", ctx.store, ctx.human
    lines, his = _conversation(ctx, guid)
    if not his:
        ctx.skip(STAGE, unit)
        return
    p = ctx.persona(guid)
    name, called = ctx.name(guid), ctx.ident.called
    arc = get_arc(store, guid, human)
    live = on_the_table(arc, ctx.window)
    system = system_prompt("arc_read", READ_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=name, called=called,
                           plan=plan_summary(arc.plan, p.identity.region) if live else NO_PLAN)
    reply = await ctx.reasoner.json(
        system, READ_ASK.format(called=called, name=name, lines="\n".join(lines)), READ_SCHEMA)
    answer = reply["plan"] if live else "none"
    ending = reply["breakup"] and any(ENDING_RE.search(said) for said in his)
    now = ctx.clock.now()
    rng = ctx.rng(STAGE, "elsewhere", guid)
    ended = False

    def write(s: Store) -> None:
        nonlocal ended
        if ending:
            ended = end_romance(s, guid, human, by="he_ended", now=now)
        elif answer != "none":
            put_arc(s, moved(get_arc(s, guid, human), answer, p, rng, now=now))
    if ctx.commit(STAGE, unit, write):
        log("arc_read", night=ctx.night, guid=guid, plan=answer, breakup=reply["breakup"],
            ended=ended)


async def read(ctx: "NightContext") -> None:
    """Units "read:<guid>", at most settings.parallel at once."""
    await ctx.map_units(STAGE, [f"read:{g}" for g in _readers(ctx, fresh=True)],
                        lambda unit: _read(ctx, int(unit.split(":", 1)[1])))


# -- step 6: what the guild notices (S7) -------------------------------------------------------

def noticed(ctx: "NightContext") -> None:
    """Unit "noticed": a woman who entered a stage in NOTICED tonight is a fact the guild can
    tell. The source of truth is the stage's own rel_delta (romance_stage, applied, up), so a
    weekend, which the meetup records as planning->weekend and weekend->after together, writes
    both its facts here and nowhere else. Each is a shared fact (holder 0, not secret) whose
    root arc:<guid>:<stage>:<night> is added to the delta's evidence, and every other member
    learns it as "inferred" with the stage's confidence. The text is NOTICED_TEXT, never a word
    from a whisper. Women with a full-arc stance only. The ids it handled go to meta
    arc_noticed:<night> for the jealousy stage, and arc_noticed_mark keeps a later night from
    reading them again."""
    if ctx.done(STAGE, "noticed"):
        return
    now, human, night = ctx.clock.now(), ctx.human, ctx.night
    women = set(_women(ctx, fresh=True))
    cast = ctx.members(include_away=True)
    called = ctx.ident.called
    # Ruling S7: the night runs after its window closes, so the clock is past window[1]. The
    # facts below are written, and learned, with a ts just inside the window, because the
    # jealousy stage counts a holder's knowledge only when learned < window[1].
    learned_at = ctx.window[1] - 1.0
    handled: list[int] = []

    def write(store: Store) -> None:
        mark = store.get_meta(MARK)
        rows = store.conn.execute(
            "SELECT id, a, reason, evidence FROM rel_delta WHERE b = ? AND dim = 'romance_stage' "
            "AND applied = 1 AND delta > 0 AND " + ("id > ?" if mark is not None else "ts >= ?")
            + " ORDER BY id", (human, int(mark) if mark is not None else ctx.window[0])).fetchall()
        for row in rows:
            stage = row["reason"].split("->")[-1]
            if stage not in NOTICED or row["a"] not in women:
                continue
            guid = row["a"]
            root = f"arc:{guid}:{stage}:{night}"
            # The only place noticed facts are written: shared (holder 0), not secret, generic.
            add_fact(store, kind="noticed", root=root, about=guid,
                     text=NOTICED_TEXT[stage].format(name=ctx.name(guid), called=called),
                     secret=False, now=learned_at)
            for g in cast:
                if g != guid:
                    learn(store, root, g, "inferred", ts=learned_at,
                          confidence=NOTICED_CONFIDENCE[stage])
            evidence = json.loads(row["evidence"])
            if root not in evidence:
                store.conn.execute("UPDATE rel_delta SET evidence = ? WHERE id = ?",
                                   (json.dumps(evidence + [root]), row["id"]))
            handled.append(row["id"])
        top = store.conn.execute("SELECT max(id) FROM rel_delta").fetchone()[0] or 0
        store.set_meta(MARK, str(max(top, int(mark or 0))))
        store.set_meta(f"{HANDLED}{night}", json.dumps(handled))
    ctx.commit(STAGE, "noticed", write)


async def stage_arc(ctx: "NightContext") -> None:
    """The night stage `arc`: the six steps in order (see the module docstring)."""
    settle(ctx)
    propose(ctx)
    await read(ctx)
    await meetup(ctx)
    await milestones(ctx)
    noticed(ctx)


def _count(ctx: "NightContext") -> int:
    """The budget's count: tonight's read units, and the meetup when one is due."""
    return len(_readers(ctx, fresh=False)) + due(ctx)


register_counter(STAGE, _count)
