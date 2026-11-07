"""The weekend in a gap of his play (Task S5). Once he has said yes to a woman's plan (plan state
"set"), the first time he is away from the game for WEEKEND_GAP_S or more the night writes their
time together into her memory, from her point of view. The night that writes it is the first to
run after the gap opens, so by then he has been gone 48 to 72 hours. The trip is placed inside
his absence and has ended by the time it is written (controller ruling, fix round 1): her `away`
row records it already closed, so she is home, at "after", whenever he comes back. From "after",
a later plan he says yes to makes a visit: a shorter trip, written the same way. A meetup never
happens without his yes.

Two steps of the arc stage (arc.night.stage_arc), each one unit:
- settle (unit "settle", first): the safety net for rows an older build left open. An away past
  its returns_at is closed (returned_at = returns_at); a woman back from a weekend moves to
  "after" (a rel_delta "weekend->after"), and after a weekend or a visit her plan is done, her
  weekends count one more, last_meetup is when she came back and her after-weekend milestones
  start a new cycle (arc.after).
- meetup (unit "meetup", after the reads): absence() finds his gap, pick_meetup() the woman who
  takes it (one meetup per absence, at most one woman away), and the night writes her finished
  trip (the away row, closed), the arc_meetup row, her moments and what settle would do for it:
  a weekend moves her "planning->weekend" and "weekend->after" (evidence away:<id>), and her plan
  is done, weekends goes up by one, last_meetup is when she came back and a new milestone cycle
  starts (milestone 0, arc.after).

Her moments are grounded (kind "life": fiction, so no number or boss rule) and stored as private
memories with him (scope_with him): they reach only her prompts for a whisper to him, never a
guild prompt or another member's. A refusal, an invalid reply or fewer than two grounded
moments leaves one plain, non-explicit memory instead (PLAIN_WEEKEND, or PLAIN_VISIT for a
visit), and the unit commits: the night never fails on it. A softened reply is kept as written.
A request that fails (Ollama, not the model's answer) fails the unit, which a later run retries.

His home stays vague: no text here names his city or town or where he lives; she says where they
met only in general terms. The built-in prompt is non-explicit; the owner's local overlay
(mind-data/prompts/nightly_weekend.txt, never in the repo) replaces it. Logs carry guids, counts
and reasons, never a moment, a name or the plan."""
import math
import re
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from leeroy_mind.arc.ladder import FULL_ARC, PACE_ATTITUDE, PACE_STYLE
from leeroy_mind.arc.plan import plan_planned, plan_summary
from leeroy_mind.arc.state import get_arc, play_days, put_arc
from leeroy_mind.llm.presets import JSONJobFailed
from leeroy_mind.logs import log
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
from leeroy_mind.nightly.ground import Claim, ground, log_verdicts
from leeroy_mind.nightly.llm import ReflectionError, schema, system_prompt
from leeroy_mind.nightly.store import active_away
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "arc"
# His time away from the game that makes room for a meetup: the night writes it at its first run
# after the gap opens, 48 to 72 hours after his last logout.
WEEKEND_GAP_S = 2 * 86400.0
PLAN_MIN_PLAY_DAYS = 5               # play days from her plan to the weekend, times her pace
WEEKEND_S = 2 * 86400.0
VISIT_S = (86400.0, 2 * 86400.0)
TRAVEL_START_H = (6, 18)             # hours after his last logout that she sets off, at most
PROMPT = "weekend"                   # system_prompt's name for the weekend writer
WEEKEND_OVERLAY = f"nightly_{PROMPT}"  # the owner's overlay: mind-data/prompts/nightly_weekend.txt
KINDS = {"planning": "weekend", "after": "visit"}  # the stage she meets him from: the kind
MOMENTS = {"weekend": (4, 6), "visit": (2, 4)}
MOMENTS_TEXT = {"weekend": "four to six moments from their first weekend together",
                "visit": "two to four moments from this visit, their time together again"}
IMPORTANCE = {"weekend": 8, "visit": 7}
TONES = ("warm", "bittersweet or awkward")  # asked; the second when her attraction is cooling
VALENCE = {"warm": 0.8, "bittersweet": 0.3, "awkward": -0.2}
PLAIN_VALENCE = 0.5
COOLING_NIGHTS = 7

WEEKEND_SCHEMA = schema("arc_weekend", {
    "moments": {"type": "array", "minItems": 2, "maxItems": 6,
                "items": {"type": "string", "maxLength": 200}},
    "tone": {"type": "string", "enum": ["warm", "bittersweet", "awkward"]}})
WEEKEND_PROMPT = (
    "{card}\n\n"
    "{name} and {called} know each other through the game and live far apart. They have just "
    "spent time together in person. {plan}\n"
    "Write {moments}, from {name}'s point of view and in her own words (\"I\", \"we\", "
    "\"{called}\"), each a sentence or two: where they met, a few concrete moments, how she "
    "felt, and one awkward or funny moment so it feels real. The tone is {tone}.\n"
    "Leave the small details open: no address, no street or hotel name, nothing {called} would "
    "have to confirm. Never name {called}'s city or town, or where {called} lives; say where "
    "they met only in general terms, like a hotel halfway or her city.\n"
    "{affair}")
AFFAIR = ("{name} has a partner at home, who believes she was away on {cover}. That is part of "
          "her story too: the cover story she gave at home, her nerves about being found out and "
          "the risk she knows she took. Her guilt and nerves are hers alone; she never turns "
          "them on {called}.\n")
WEEKEND_ASK = "Write what {name} remembers of their time together."
PLAIN_WEEKEND = "I finally met {called} in person, {where}, and I'm still thinking about it."
PLAIN_VISIT = "I got to see {called} in person again, {where}, and I'm still smiling about it."
_WHERE = {"halfway": "at a hotel halfway between us", "event": "at the convention",
          "neutral": "somewhere quiet and neutral"}
# What a refusal looks like inside a schema-shaped reply: a moment that is one is dropped. A
# refusing verb needs its object ("write that", "continue with this request"), so a moment such
# as "I can't write down how that felt" stays; assist and comply stand alone. An apology that
# leads into a refusal ("I apologize, but", "sorry, I can't") counts whatever follows; "sorry,
# but I was starving" and "not sorry I won" are moments.
_CANNOT = (r"(I (cannot|can[’']?t|won[’']?t|will not|am unable to|am not able to)"
           r"|I[’']?m (unable|not able) to)")
_OBJECT = r"(with )?(this|that|it|your|such|the (request|scene|story|content))\b"
_VERB = r"(write|continue|create|produce|generate|provide|help with|fulfill|engage( in| with)?)"
REFUSAL_RE = re.compile(
    rf"\b({_CANNOT} ({_VERB} {_OBJECT}|(assist|comply)\b)"
    r"|I[’']?m sorry,? but I (cannot|can[’']?t|won[’']?t|will not|am unable|am not able)\b"
    r"|apologi[sz]e,? but\b|sorry,? I (can|won)(no|[’'])?t\b"
    rf"|(I[’']?m|I am) not comfortable (writing|with) {_OBJECT}"
    r"|I (must |have to |need to |will )?decline (to|this|that|your|the request)\b"
    r"|as an AI\b|content polic(y|ies)\b|(against|violates?) (my|the) (guidelines|policies)\b)",
    re.I)


def plain_text(kind: str, plan_kind: str, region: str, called: str) -> str:
    """The plain memory a weekend (or a visit) leaves when the model's moments cannot be used:
    where they met in general terms, never a place's name."""
    if plan_kind == "she_travels":
        where = "after my flight over" if region == "UK" else "after my trip out there"
    else:
        where = _WHERE[plan_kind]
    return (PLAIN_WEEKEND if kind == "weekend" else PLAIN_VISIT).format(called=called,
                                                                        where=where)


# -- who and when ------------------------------------------------------------------------------

def absence(store: Store, now: float) -> tuple[int, float] | None:
    """(his last ended session's id, its end) when no session of his is open and it ended
    WEEKEND_GAP_S or more before `now`; else None."""
    with store.transaction():
        if store.conn.execute("SELECT 1 FROM session WHERE ended IS NULL LIMIT 1").fetchone():
            return None
        r = store.conn.execute("SELECT id, ended FROM session WHERE ended IS NOT NULL "
                               "ORDER BY ended DESC, id DESC LIMIT 1").fetchone()
    if r is None or now - r["ended"] < WEEKEND_GAP_S:
        return None
    return r["id"], r["ended"]


def pick_meetup(store: Store, ctx: "NightContext", gap: tuple[int, float], *,
                fresh: bool = True) -> tuple[int, str] | None:
    """(guid, "weekend" or "visit") for the woman who takes his gap: none when arc_meetup
    already holds this absence or someone is away. Else, of the full-arc women whose plan is
    set, the one set earliest (plan_at; ties by guid): at "planning" (a weekend) once he has
    played PLAN_MIN_PLAY_DAYS times her pace since she floated it, or at "after" (a visit). The
    others keep their set plans for a later gap. `fresh` re-reads the stored personas."""
    with store.transaction():
        held = store.conn.execute("SELECT 1 FROM arc_meetup WHERE absence = ?",
                                  (gap[0],)).fetchone()
    if held is not None or active_away(store):
        return None
    now, human = ctx.clock.now(), ctx.human
    ready = []
    for guid, stance in sorted(ctx.attitudes(fresh=fresh).items()):
        if stance not in FULL_ARC:
            continue
        arc = get_arc(store, guid, human)
        kind = KINDS.get(store.get_relationship(guid, human).romance_stage)
        if arc.plan_state != "set" or kind is None:
            continue
        if kind == "weekend":
            wait = (PLAN_MIN_PLAY_DAYS * PACE_ATTITUDE[stance]
                    * PACE_STYLE.get(ctx.persona(guid).romance.style, 1.0))
            if play_days(store, arc.planning_since or 0.0, now, ctx.tz) < wait:
                continue
        ready.append((math.inf if arc.plan_at is None else arc.plan_at, guid, kind))
    return min(ready)[1:] if ready else None


def due(ctx: "NightContext") -> int:
    """1 when tonight's meetup step would write a meetup, else 0: for the budget's count."""
    gap = absence(ctx.store, ctx.clock.now())
    return int(gap is not None and pick_meetup(ctx.store, ctx, gap, fresh=False) is not None)


def cooling(store: Store, guid: int, night: str) -> bool:
    """Whether her attraction decayed more than it gained over the last COOLING_NIGHTS nights
    of attraction_log, up to `night`."""
    first = (date.fromisoformat(night) - timedelta(days=COOLING_NIGHTS - 1)).isoformat()
    with store.transaction():
        r = store.conn.execute(
            "SELECT coalesce(sum(gained), 0), coalesce(sum(decayed), 0), count(*) "
            "FROM attraction_log WHERE guid = ? AND night >= ? AND night <= ?",
            (guid, first, night)).fetchone()
    return r[2] > 0 and r[1] > r[0]


def _refused(e: ReflectionError) -> bool:
    """The model's answer was the trouble (two invalid replies), not the request."""
    return not e.fatal and isinstance(e.__cause__, JSONJobFailed)


# -- step 1: settle ----------------------------------------------------------------------------

def settle(ctx: "NightContext") -> None:
    """Unit "settle": aways past their returns_at close (returned_at = returns_at). Each meetup
    whose away is closed and that her arc has not counted (last_meetup before its returns_at)
    settles: a weekend moves her from "weekend" to "after" (rel_delta "weekend->after",
    evidence away:<id>); after a weekend or a visit her plan is done, weekends goes up by one,
    last_meetup is returns_at and her milestone cycle starts again (milestone 0, arc.after). A
    woman no longer on that stage (her romance ended) is left."""
    if ctx.done(STAGE, "settle"):
        return
    now, human = ctx.clock.now(), ctx.human
    closed, back = 0, []

    def write(store: Store) -> None:
        nonlocal closed
        closed = store.conn.execute(
            "UPDATE away SET returned_at = returns_at WHERE returned_at IS NULL "
            "AND returns_at <= ?", (now,)).rowcount
        rows = store.conn.execute(
            "SELECT m.guid, m.kind, a.id, a.returns_at FROM arc_meetup m "
            "JOIN away a ON a.id = m.away_id WHERE a.returned_at IS NOT NULL "
            "ORDER BY a.returns_at, m.absence").fetchall()
        for guid, kind, away_id, returns_at in rows:
            arc = get_arc(store, guid, human)
            if arc.last_meetup is not None and arc.last_meetup >= returns_at:
                continue
            rel = store.get_relationship(guid, human)
            if rel.romance_stage != ("weekend" if kind == "weekend" else "after"):
                continue
            if kind == "weekend":
                store.put_relationship(replace(rel, romance_stage="after"))
                store.add_rel_delta(guid, human, "romance_stage", 1, "weekend->after",
                                    [f"away:{away_id}"], now, applied=True)
            put_arc(store, replace(arc, plan_state="done", weekends=arc.weekends + 1,
                                   last_meetup=returns_at, milestone=0, milestone_at=None,
                                   updated=now))
            back.append(guid)
    if ctx.commit(STAGE, "settle", write) and (closed or back):
        log("arc_settle", night=ctx.night, closed=closed, guids=back)


# -- step 4: the meetup ------------------------------------------------------------------------

async def meetup(ctx: "NightContext") -> None:
    """Unit "meetup", once tonight's settle has committed."""
    if not ctx.done(STAGE, "settle"):
        return
    await ctx.map_units(STAGE, ["meetup"], lambda unit: _meetup(ctx))


async def _meetup(ctx: "NightContext") -> None:
    store, human, now = ctx.store, ctx.human, ctx.clock.now()
    gap = absence(store, now)
    picked = None if gap is None else pick_meetup(store, ctx, gap)
    if picked is None:
        ctx.skip(STAGE, "meetup")
        return
    (absence_id, end), (guid, kind) = gap, picked
    arc, p = get_arc(store, guid, human), ctx.persona(guid)
    region, name, called = p.identity.region, ctx.name(guid), ctx.ident.called
    rng = ctx.rng(STAGE, "meetup", absence_id, guid)
    span = WEEKEND_S if kind == "weekend" else rng.uniform(*VISIT_S)
    # She sets off a few hours after his logout, but never so late that the trip outlasts the
    # night that writes it: it has ended by `now` (the gap is at least WEEKEND_GAP_S >= span).
    left_at = max(end, min(end + rng.uniform(*TRAVEL_START_H) * 3600, now - span))
    returns_at = left_at + span
    with store.transaction():
        away_id = store.conn.execute("SELECT coalesce(max(id), 0) + 1 FROM away").fetchone()[0]
    ref = f"away:{away_id}"
    summary = plan_summary(arc.plan, region)
    cover = arc.plan.get("cover") or ""
    system = system_prompt(
        PROMPT, WEEKEND_PROMPT, ctx.settings.data_dir, human=ctx.ident, name=name,
        called=called, card=ctx.card(guid), plan=plan_planned(arc.plan, region),
        tone=TONES[1] if cooling(store, guid, ctx.night) else TONES[0],
        affair=AFFAIR.format(name=name, cover=cover, called=called) if cover else "",
        moments=MOMENTS_TEXT[kind])
    pairs, kept, tone, why = [], [], None, None
    try:
        reply = await ctx.reasoner.json(system, WEEKEND_ASK.format(name=name), WEEKEND_SCHEMA,
                                        temperature=0.8)
    except ReflectionError as e:
        if not _refused(e):
            raise
        reply, why = None, "invalid"
    if reply is not None:
        tone = reply["tone"]
        texts = [" ".join(strip_wow_markup(t).split())
                 for t in reply["moments"]][:MOMENTS[kind][1]]
        refusals = [t for t in texts if REFUSAL_RE.search(t)]
        judge_refused = False
        ev = Evidence()
        ev.add(Item(ref, summary, frozenset({guid, human}), left_at))
        claims = [Claim("life", guid, t, [ref]) for t in texts if t not in refusals]
        try:
            pairs = await ground(ctx, claims, ev, load_facts(ctx)) if claims else []
        except ReflectionError as e:
            if not _refused(e):
                raise
            judge_refused = True
        kept = [c.text for c, v in pairs if v.ok]
        if len(kept) < 2:
            why = "refusal" if refusals or judge_refused else "ungrounded"
    texts = kept if why is None else [plain_text(kind, arc.plan["kind"], region, called)]
    valence = VALENCE[tone] if why is None else PLAIN_VALENCE
    vectors = await ctx.embed(texts)
    times = [int(left_at + (i + 1) * span / (len(texts) + 1)) for i in range(len(texts))]

    def write(s: Store) -> None:
        arc_now = get_arc(s, guid, human)
        if (s.conn.execute("SELECT 1 FROM away WHERE id = ? OR returned_at IS NULL",
                           (away_id,)).fetchone()
                or s.conn.execute("SELECT 1 FROM arc_meetup WHERE absence = ?",
                                  (absence_id,)).fetchone()
                or arc_now.plan_state != "set"
                or KINDS.get(s.get_relationship(guid, human).romance_stage) != kind):
            raise ValueError("the meetup changed while it was written")
        s.conn.execute("INSERT INTO away(id, guid, reason, left_at, returns_at, returned_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (away_id, guid, kind, left_at, returns_at, returns_at))
        s.conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                       "VALUES (?, ?, ?, ?, ?)", (absence_id, guid, away_id, kind, now))
        if kind == "weekend":  # the weekend, and her coming home from it, as settle would
            rel = s.get_relationship(guid, human)
            s.add_rel_delta(guid, human, "romance_stage", 1, f"{rel.romance_stage}->weekend",
                            [ref], now, applied=True)
            s.put_relationship(replace(rel, romance_stage="after"))
            s.add_rel_delta(guid, human, "romance_stage", 1, "weekend->after", [ref], now,
                            applied=True)
        put_arc(s, replace(arc_now, plan_state="done", weekends=arc_now.weekends + 1,
                           last_meetup=returns_at, milestone=0, milestone_at=None, updated=now))
        for text, t, vector in zip(texts, times, vectors):
            ctx.writer.put(MemoryRow(
                id=None, owner=guid, kind="event", text=text, t_event=t, t_created=int(now),
                t_accessed=int(now), access_n=0, actors=(human,), place=None, boss_id=None,
                raid_id=None, importance=IMPORTANCE[kind], valence=valence, source="offscreen",
                evidence=(ref,), embedding=vector, scope="private", scope_with=human))
        log_verdicts(s, ctx.night, STAGE, pairs, now, ctx.ident)
    if ctx.commit(STAGE, "meetup", write):
        log("arc_meetup", night=ctx.night, guid=guid, kind=kind, away=away_id,
            moments=len(kept) if why is None else 0, tone=tone if why is None else None)
        if why is not None:
            log("arc_weekend_plain", night=ctx.night, guid=guid, reason=why)
