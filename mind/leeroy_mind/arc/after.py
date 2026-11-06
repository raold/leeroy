"""What follows the weekend, and how a romance can end (Task S6).

Step 5 of the arc stage (arc.night.stage_arc): one unit "after:<guid>" for each full-arc woman
(stance in FULL_ARC), not away, who is at "after" (her milestones) or who has a partner at home
and is at late_nights or past (the affair roll). Neither makes a model call.

Milestones: "after" is a sequence, not an open stage. MILESTONES come in order, one every
MILESTONE_GAP play days of his since the last one (drawn once for each milestone), the first
counted from her last meetup; "month" also waits for MONTH_S on the calendar since that meetup.
"next_visit" floats a new plan, a hope (state "hoped", its kind picked again, never the last
plan's), which he shapes as he did the first (arc.night's read); his yes makes a visit in a
later gap of his play (arc.weekend). A plan still open by then is left as it is and nothing is
floated. After next_visit the cycle starts again at "inside_joke". Each milestone writes one
private memory with him from MILESTONE_TEXT (built-in, non-explicit, filled from her card) and
an arc event (its name).

The affair: a woman with a partner at home (plan.has_partner, her arc's partner first) at
late_nights or past is found out at home with AFFAIR_RISK_NIGHT a night, or AFFAIR_RISK_TRIP on
the night her meetup was written (met_tonight). FOUND_OUT_ENDS_ROMANCE_P then picks the ending
(ctx.rng("arc", "found", guid)): the romance (state.end_romance, by "found_out", event
"found_out") or her marriage (event "separated"; the arc carries on). Her marriage ending
rewrites her card's household (HOUSEHOLD_SEPARATED, or HOUSEHOLD_SEPARATED_KIDS when it named
children) as a new persona version with a persona_ledger row of kind "household", sets her
arc's partner to "separated" and drops her plan's cover story; nightly.growth.undo reverses the
household and the partner (restore_household), not the cover story. Either ending leaves her an
open memory that names no one (FOUND_TEXT["open"]) and a private one with him.

Nothing else here ends a romance: neglect, a cooling score and a bad weekend never do. Logs
carry her guid and the kinds, never a text or a name."""
import json
import random
import re
from dataclasses import replace
from typing import TYPE_CHECKING, NamedTuple

from leeroy_mind.arc.ladder import FULL_ARC
from leeroy_mind.arc.plan import has_partner, propose_plan
from leeroy_mind.arc.state import (Arc, add_event, end_romance, get_arc, play_days_after,
                                   put_arc)
from leeroy_mind.logs import log
from leeroy_mind.nightly.store import active_away, ledger_add
from leeroy_mind.personas.io import _race_class, load_persona, save_persona
from leeroy_mind.storage.rows import ROMANCE_STAGES, MemoryRow
from leeroy_mind.storage.store import Store

if TYPE_CHECKING:
    from datetime import tzinfo

    from leeroy_mind.nightly.runner import NightContext

STAGE = "arc"
DAY_S = 86400.0
MILESTONES = ("missing", "inside_joke", "month", "gift", "friends_on_voice",
              "fight_and_make_up", "next_visit")
MILESTONE_GAP = (4, 8)                   # his play days from one milestone to the next
MONTH_S = 30 * DAY_S                     # "month" waits this long after her last meetup
RESTART = MILESTONES.index("inside_joke")  # where the cycle starts again after next_visit
AFFAIR_FROM = ROMANCE_STAGES[ROMANCE_STAGES.index("late_nights"):ROMANCE_STAGES.index("ex")]
AFFAIR_RISK_NIGHT = 0.005                # found out at home, a night (owner, 2026-11-06)
AFFAIR_RISK_TRIP = 0.04                  # on the night her meetup with him was written
FOUND_OUT_ENDS_ROMANCE_P = 0.5           # else it ends her marriage and the arc carries on

# Her own words, first person, about him ({called}, {him}, {his}); {like} is from her card.
MILESTONE_TEXT = {
    "missing": "Since we met in person I miss {called} more than I did before, and I keep "
               "wanting to tell {him} about {like}.",
    "inside_joke": "{called} and I have a joke from our time together that nobody else would "
                   "get, and it still makes me grin at the oddest moments.",
    "month": "It's a month since I last saw {called} in person, and I still go back over it, "
             "smiling.",
    "gift": "I sent {called} a little something by post, something to do with {like}, just "
            "because I wanted to.",
    "friends_on_voice": "{called} met a couple of my friends on voice this week; they liked "
                        "{him}, and I couldn't stop smiling.",
    "fight_and_make_up": "{called} and I had our first silly argument over nothing much, and "
                         "making up afterwards felt closer than before.",
    "next_visit": "I've started daydreaming about seeing {called} in person again, and I've "
                  "floated another trip with {him}.",
}
LIKE_FALLBACK = "the little things I love"
MILESTONE_KIND = {"next_visit": "plan"}  # every other milestone is an "event"
MILESTONE_IMPORTANCE = 6
MILESTONE_VALENCE = {"fight_and_make_up": 0.4}  # every other milestone 0.6

# "open" is hers and names no one; the ending is private with him. Her guilt and the trouble at
# home are hers: she never turns them on him.
FOUND_TEXT = {
    "open": "Things blew up at home this week, and I'm still finding my feet.",
    "ended": "It all came out at home about {called} and me, and I chose to end things with "
             "{him} to put my home first. It's history now; I'm sad, not angry.",
    "separated": "It all came out at home about {called} and me, and that relationship at home "
                 "is over now. That part is mine to carry, and I'm still with {called}.",
}
FOUND_IMPORTANCE = {"open": 7, "ended": 8, "separated": 8}
FOUND_VALENCE = {"open": -0.6, "ended": -0.5, "separated": -0.3}
FOUND_EVENT = {"ended": "found_out", "separated": "separated"}

# Her card's household once the affair ends her marriage: no partner word, so has_partner reads
# it as none even without her arc.
HOUSEHOLD_SEPARATED = ("recently separated after things blew up at home, living on her own "
                       "for now")
HOUSEHOLD_SEPARATED_KIDS = ("recently separated after things blew up at home, sharing the kids "
                            "with her ex")
_KIDS = re.compile(r"(?<!no )\b(kids?|children|child|sons?|daughters?|bab(y|ies)|toddlers?"
                   r"|twins|step-?(kids|children))\b", re.IGNORECASE)
_OPEN_PLANS = ("hoped", "talked", "set")


class _Memory(NamedTuple):
    text: str
    kind: str
    scope: str                           # "open" or "private" (with him)
    importance: int
    valence: float
    evidence: tuple[str, ...]


def separated_household(household: str) -> str:
    """Her card's household after the affair ends her marriage: her children stay in it."""
    return HOUSEHOLD_SEPARATED_KIDS if _KIDS.search(household) else HOUSEHOLD_SEPARATED


def _gap(guid: int, index: int, since: float) -> float:
    """The play days from `since` to milestone `index`, drawn once for that milestone (ctx.rng
    would draw again each night, so the span would be the first of several draws to fit)."""
    return random.Random(f"arc|milestone|{guid}|{index}|{since!r}").uniform(*MILESTONE_GAP)


def due_milestone(store: Store, arc: Arc, *, now: float, tz: "tzinfo") -> str | None:
    """Her next milestone when it is due at `now`: MILESTONE_GAP play days of his since the last
    one (the first since her last meetup), and for "month" MONTH_S since her last meetup."""
    since = arc.last_meetup if arc.milestone_at is None else arc.milestone_at
    if since is None or not 0 <= arc.milestone < len(MILESTONES):
        return None
    name = MILESTONES[arc.milestone]
    if play_days_after(store, since, now, tz) < _gap(arc.guid, arc.milestone, since):
        return None
    if name == "month" and (arc.last_meetup is None or now - arc.last_meetup < MONTH_S):
        return None
    return name


def met_tonight(store: Store, night: str, guid: int) -> bool:
    """Whether tonight's meetup step wrote her meetup. A night's settle commits before its
    meetup runs (arc.weekend.meetup waits for it), so a meetup is tonight's when it was written
    once tonight's settle had committed; every earlier night's was written before."""
    with store.transaction():
        return store.conn.execute(
            "SELECT 1 FROM arc_meetup m JOIN night_step s ON s.night = ? AND s.stage = ? "
            "AND s.unit = 'settle' WHERE m.guid = ? AND m.written >= s.finished LIMIT 1",
            (night, STAGE, guid)).fetchone() is not None


def _last_trip(store: Store, guid: int) -> str | None:
    """away:<id> of her newest meetup with him, which her milestones rest on."""
    with store.transaction():
        r = store.conn.execute("SELECT away_id FROM arc_meetup WHERE guid = ? "
                               "ORDER BY written DESC, absence DESC LIMIT 1", (guid,)).fetchone()
    return None if r is None else f"away:{r[0]}"


def _candidates(ctx: "NightContext") -> list[int]:
    """The women with an "after" unit tonight: full-arc, not away, at "after" or, with a partner
    at home, at late_nights or past."""
    store, human, away = ctx.store, ctx.human, active_away(ctx.store)
    out = []
    for guid, stance in sorted(ctx.attitudes().items()):
        if stance not in FULL_ARC or guid in away:
            continue
        stage = store.get_relationship(guid, human).romance_stage
        if stage == "after" or (stage in AFFAIR_FROM and has_partner(
                ctx.persona(guid), get_arc(store, guid, human).partner)):
            out.append(guid)
    return out


async def milestones(ctx: "NightContext") -> None:
    """Units "after:<guid>", at most settings.parallel at once."""
    await ctx.map_units(STAGE, [f"after:{g}" for g in _candidates(ctx)],
                        lambda unit: _after(ctx, int(unit.split(":", 1)[1])))


async def _after(ctx: "NightContext", guid: int) -> None:
    """Unit "after:<guid>": the affair roll, then her milestone if one is due and her romance
    goes on."""
    unit, store, human, now = f"after:{guid}", ctx.store, ctx.human, ctx.clock.now()
    stage = store.get_relationship(guid, human).romance_stage
    arc, p = get_arc(store, guid, human), ctx.persona(guid)
    trip = met_tonight(store, ctx.night, guid)
    found = None
    if stage in AFFAIR_FROM and has_partner(p, arc.partner):
        risk = AFFAIR_RISK_TRIP if trip else AFFAIR_RISK_NIGHT
        if ctx.rng(STAGE, "affair", guid).random() < risk:
            found = ("ended" if ctx.rng(STAGE, "found", guid).random() < FOUND_OUT_ENDS_ROMANCE_P
                     else "separated")
    name = (due_milestone(store, arc, now=now, tz=ctx.tz)
            if stage == "after" and found != "ended" else None)
    if found is None and name is None:
        ctx.skip(STAGE, unit)
        return
    called, (_, him, his) = ctx.ident.called, ctx.ident.forms
    partner = "separated" if found == "separated" else arc.partner
    memories: list[_Memory] = []
    if found is not None:
        ref = (f"found:{guid}:{ctx.night}",)
        memories += [
            _Memory(FOUND_TEXT["open"], "event", "open", FOUND_IMPORTANCE["open"],
                    FOUND_VALENCE["open"], ref),
            _Memory(FOUND_TEXT[found].format(called=called, him=him, his=his), "event",
                    "private", FOUND_IMPORTANCE[found], FOUND_VALENCE[found], ref)]
    floated, marked = None, False
    if name is not None and not (name == "next_visit" and arc.plan_state in _OPEN_PLANS):
        rng = ctx.rng(STAGE, "milestone", guid, name)
        like = rng.choice(p.likes) if p.likes else LIKE_FALLBACK
        memories.append(_Memory(
            MILESTONE_TEXT[name].format(called=called, him=him, his=his, like=like),
            MILESTONE_KIND.get(name, "event"), "private", MILESTONE_IMPORTANCE,
            MILESTONE_VALENCE.get(name, 0.6),
            (_last_trip(store, guid) or f"milestone:{guid}:{ctx.night}",)))
        marked = True
        if name == "next_visit":
            floated = propose_plan(p, rng, now=now, exclude=arc.plan.get("kind", ""),
                                   partner=partner)
    vectors = await ctx.embed([m.text for m in memories]) if memories else []

    def write(s: Store) -> None:
        if (s.get_relationship(guid, human).romance_stage != stage
                or get_arc(s, guid, human) != arc):
            raise ValueError("the arc changed while it was written")
        ids = [ctx.writer.put(MemoryRow(
            id=None, owner=guid, kind=m.kind, text=m.text, t_event=int(now), t_created=int(now),
            t_accessed=int(now), access_n=0, actors=(human,) if m.scope == "private" else (),
            place=None, boss_id=None, raid_id=None, importance=m.importance, valence=m.valence,
            source="offscreen", evidence=m.evidence, embedding=vector, scope=m.scope,
            scope_with=human if m.scope == "private" else 0))
            for m, vector in zip(memories, vectors)]
        a = arc
        if found == "ended":
            end_romance(s, guid, human, by="found_out", now=now)
            a = get_arc(s, guid, human)
        elif found == "separated":
            _separate(s, guid, arc.partner, evidence=[f"memory:{ids[0]}"], now=now)
            a = replace(a, partner="separated",
                        plan=a.plan | {"cover": ""} if a.plan.get("cover") else a.plan)
        if found is not None:
            a = add_event(a, FOUND_EVENT[found], now)
        if name is not None:
            a = replace(a, milestone=RESTART if name == "next_visit"
                        else MILESTONES.index(name) + 1, milestone_at=now)
            if marked:
                a = add_event(a, name, now)
            if floated is not None:
                a = replace(a, plan=floated, plan_state="hoped", plan_at=None,
                            planning_since=now)
        put_arc(s, replace(a, updated=now))
    if ctx.commit(STAGE, unit, write):
        if found == "separated":
            ctx.personas.invalidate(guid)
        log("arc_after", night=ctx.night, guid=guid, milestone=name, found=found, trip=trip)


def _separate(store: Store, guid: int, partner: str, *, evidence: list[str],
              now: float) -> int:
    """Her stored card's household rewritten (separated_household) as a new persona version,
    and its "household" ledger row, which holds what restore_household needs: the household
    before and after, and her arc's partner before. Returns the ledger row's id."""
    stored = load_persona(store, guid)
    if stored is None:
        raise LookupError(f"guid {guid} has no stored persona")
    before = stored.real_life.household
    after = separated_household(before)
    race, class_name = _race_class(store, store.cast(), guid)
    save_persona(store, replace(stored, real_life=replace(stored.real_life, household=after)),
                 source="generated", ts=now, race=race, class_name=class_name)
    return ledger_add(store, guid=guid, kind="household",
                      text=json.dumps({"household": [before, after], "partner": partner},
                                      ensure_ascii=False),
                      evidence=evidence, now=now)


def restore_household(store: Store, row, *, now: float) -> bool:
    """nightly.growth.undo of a "household" ledger row, inside its transaction: her card's
    household back to what it was, while it still reads as the row wrote it (a later hand edit
    stands), and her arc's partner back, while it is still "separated". False when neither is
    left to reverse."""
    try:
        entry = json.loads(row["text"])
        before, after = entry["household"]
        partner = entry["partner"]
    except (ValueError, KeyError, TypeError):
        return False
    guid, done = row["guid"], False
    p = load_persona(store, guid)
    if p is not None and p.real_life.household == after:
        race, class_name = _race_class(store, store.cast(), guid)
        save_persona(store, replace(p, real_life=replace(p.real_life, household=before)),
                     source="generated", ts=now, race=race, class_name=class_name)
        done = True
    arc = get_arc(store, guid, int(store.get_meta("human_guid") or 0))
    if arc.partner == "separated":
        put_arc(store, replace(arc, partner=partner, updated=now))
        done = True
    return done
