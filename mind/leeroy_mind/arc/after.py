"""What follows the weekend, and how a romance can end (Task S6).

Step 5 of the arc stage (arc.night.stage_arc): one unit "after:<guid>" for each full-arc woman
(stance in FULL_ARC), not away, who is at "after" (her milestones) or who has a partner at home
and is at late_nights or past (the affair roll). Neither makes a model call.

Milestones: "after" is a sequence, not an open stage. Each meetup (the weekend or a visit) arms
one cycle (arc.weekend sets milestone 0): MILESTONES in order, one every MILESTONE_GAP play days
of his since the last one (drawn once for each milestone), the first counted from that meetup;
"month" also waits for MONTH_S on the calendar since it, so it comes once a meetup. "next_visit"
ends the cycle (milestone CYCLE_DONE): it floats a new plan, a hope (state "hoped", its kind
picked again, never the last plan's), which he shapes as he did the first (arc.night's read),
and his yes makes a visit in a later gap of his play (arc.weekend), which arms the next cycle.
Until then nothing more is written. A plan still open at next_visit is left as it is and nothing
is floated (logged skipped "plan_open"). Each milestone writes one private memory with him and
an arc event (its name): the first cycle in MILESTONE_TEXT's words, a later one in
MILESTONE_AGAIN's with the meetup's ordinal ("our third time together"), so her words never
repeat (built-in, non-explicit, filled from her card).

The affair: a woman with a partner at home (plan.has_partner, her arc's partner first) at
late_nights or past is found out at home with AFFAIR_RISK_NIGHT a night, or AFFAIR_RISK_TRIP on
the night her meetup was written (met_tonight). FOUND_OUT_ENDS_ROMANCE_P then picks the ending
(ctx.rng("arc", "found", guid)): the romance (state.end_romance, by "found_out", event
"found_out") or her marriage (event "separated"; the arc carries on). Her marriage ending
rewrites her card's household (HOUSEHOLD_SEPARATED, or HOUSEHOLD_SEPARATED_KIDS when it named
children) as a new persona version with a persona_ledger row of kind "household", sets her
arc's partner to "separated" and drops her plan's cover story; nightly.growth.undo puts back the
household, the partner and the cover story, and takes back the separation's private memory and
its arc event (restore_household). Either ending leaves her an open memory that names no one
(FOUND_TEXT["open"]) and a private one with him.

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
CYCLE_DONE = len(MILESTONES)             # the cycle is spent: nothing until the next meetup
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
# A later cycle's words ({nth}: the meetup's ordinal, "second", "third", ...), never "first".
MILESTONE_AGAIN = {
    "missing": "Seeing {called} for the {nth} time only made me miss {him} more, and I keep "
               "wanting to tell {him} about {like}.",
    "inside_joke": "{called} and I came back from our {nth} time together with a new joke "
                   "nobody else would get.",
    "month": "It's a month since our {nth} time together, and I still go back over it, smiling.",
    "gift": "After our {nth} time together I sent {called} another little something by post, "
            "something to do with {like}.",
    "friends_on_voice": "My friends had {called} on voice again after our {nth} time together, "
                        "and by now they ask after {him}.",
    "fight_and_make_up": "{called} and I squabbled over nothing much after our {nth} time "
                         "together, and making up came easier than it used to.",
    "next_visit": "Our {nth} time together only made me want the next one, and I've floated "
                  "another trip with {him}.",
}
_ORDINALS = {2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
             8: "eighth", 9: "ninth", 10: "tenth"}
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
_CHILD = re.compile(r"\b(kids?|children|child|sons?|daughters?|bab(y|ies)|toddlers?|twins"
                    r"|step-?(kids|children|sons?|daughters?))\b(?![-\u2010]free)", re.IGNORECASE)
# A child word that is not hers, by what her clause says just before it ("no kids", "doesn't
# have kids", "wants kids", "his son", "her sister's kids") or just after it ("kids someday").
_NOT_HERS_BEFORE = re.compile(
    r"\b(no|without|wants?|wanting|would like|hoping for|hopes? for|his)\b"
    r"|(n[\u2019']t|\b(not|never|doesnt|dont|didnt))\s+(have|had|got)\b"
    r"|\b(?!(she|he|it|that|there|who|what|one)[\u2019']s\b)\w+[\u2019']s\b", re.IGNORECASE)
_NOT_HERS_AFTER = re.compile(r"\s*(someday|some day|one day|eventually|in the future)\b",
                             re.IGNORECASE)
_OPEN_PLANS = ("hoped", "talked", "set")


class _Memory(NamedTuple):
    text: str
    kind: str
    scope: str                           # "open" or "private" (with him)
    importance: int
    valence: float
    evidence: tuple[str, ...]


def has_children(household: str) -> bool:
    """Whether her household names children of her own: a child word whose clause does not say
    she has none, wants some, or that they are someone else's."""
    for m in _CHILD.finditer(household):
        clause = household[:m.start()]
        for mark in ",;.:":
            clause = clause.rsplit(mark, 1)[-1]
        if (_NOT_HERS_BEFORE.search(" ".join(clause.split()[-3:]))
                or _NOT_HERS_AFTER.match(household, m.end())):
            continue
        return True
    return False


def separated_household(household: str) -> str:
    """Her card's household after the affair ends her marriage: her children stay in it."""
    return HOUSEHOLD_SEPARATED_KIDS if has_children(household) else HOUSEHOLD_SEPARATED


def ordinal(n: int) -> str:
    """Her n-th meetup in words (second to tenth), then in figures (11th, 21st, ...)."""
    if n in _ORDINALS:
        return _ORDINALS[n]
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def milestone_text(name: str, meetups: int, *, called: str, forms, like: str) -> str:
    """Her memory for milestone `name` of her cycle after meetup number `meetups`: the first
    cycle's words, or a later cycle's with its ordinal."""
    template = MILESTONE_TEXT[name] if meetups <= 1 else MILESTONE_AGAIN[name]
    return template.format(called=called, him=forms[1], his=forms[2], like=like,
                           nth=ordinal(meetups))


def _gap(guid: int, index: int, since: float) -> int:
    """The whole play days from `since` to milestone `index`, drawn once for that milestone
    (ctx.rng would draw again each night, so the span would be the first of several draws)."""
    return random.Random(f"arc|milestone|{guid}|{index}|{since!r}").randint(*MILESTONE_GAP)


def due_milestone(store: Store, arc: Arc, *, now: float, tz: "tzinfo") -> str | None:
    """Her next milestone when it is due at `now`: MILESTONE_GAP play days of his since the last
    one (the first since her last meetup), and for "month" MONTH_S since her last meetup. None
    once her cycle is spent (CYCLE_DONE) until a meetup arms the next."""
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
    """The women with an "after" unit tonight: full-arc, not away, at "after" with her cycle not
    spent or, with a partner at home, at late_nights or past."""
    store, human, away = ctx.store, ctx.human, active_away(ctx.store)
    out = []
    for guid, stance in sorted(ctx.attitudes().items()):
        if stance not in FULL_ARC or guid in away:
            continue
        stage, arc = store.get_relationship(guid, human).romance_stage, get_arc(store, guid, human)
        if ((stage == "after" and arc.milestone < CYCLE_DONE)
                or (stage in AFFAIR_FROM and has_partner(ctx.persona(guid), arc.partner))):
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
    called, forms = ctx.ident.called, ctx.ident.forms
    _, him, his = forms
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
    skipped = "plan_open" if name == "next_visit" and arc.plan_state in _OPEN_PLANS else None
    if name is not None and skipped is None:
        rng = ctx.rng(STAGE, "milestone", guid, name)
        like = rng.choice(p.likes) if p.likes else LIKE_FALLBACK
        memories.append(_Memory(
            milestone_text(name, arc.weekends, called=called, forms=forms, like=like),
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
            _separate(s, guid, arc.partner, cover=a.plan.get("cover") or "", memory=ids[1],
                      evidence=[f"memory:{i}" for i in ids[:2]], now=now)
            a = replace(a, partner="separated",
                        plan=a.plan | {"cover": ""} if a.plan.get("cover") else a.plan)
        if found is not None:
            a = add_event(a, FOUND_EVENT[found], now)
        if name is not None:
            a = replace(a, milestone=MILESTONES.index(name) + 1, milestone_at=now)
            if marked:
                a = add_event(a, name, now)
            if floated is not None:
                a = replace(a, plan=floated, plan_state="hoped", plan_at=None,
                            planning_since=now)
        put_arc(s, replace(a, updated=now))
    if ctx.commit(STAGE, unit, write):
        if found == "separated":  # the night's later reads see her new household
            ctx.personas.invalidate(guid)
            ctx._memo.pop(("card", guid), None)
        log("arc_after", night=ctx.night, guid=guid, milestone=name, found=found, trip=trip,
            skipped=skipped)


def _separate(store: Store, guid: int, partner: str, *, cover: str, memory: int,
              evidence: list[str], now: float) -> int:
    """Her stored card's household rewritten (separated_household) as a new persona version,
    and its "household" ledger row, which holds what restore_household needs: the household
    before and after, her arc's partner and plan's cover story before, the separation's private
    memory and its arc event. Returns the ledger row's id."""
    stored = load_persona(store, guid)
    if stored is None:
        raise LookupError(f"guid {guid} has no stored persona")
    before = stored.real_life.household
    after = separated_household(before)
    race, class_name = _race_class(store, store.cast(), guid)
    save_persona(store, replace(stored, real_life=replace(stored.real_life, household=after)),
                 source="generated", ts=now, race=race, class_name=class_name)
    return ledger_add(store, guid=guid, kind="household",
                      text=json.dumps({"household": [before, after], "partner": partner,
                                       "cover": cover, "memory": memory,
                                       "event": [FOUND_EVENT["separated"], now]},
                                      ensure_ascii=False),
                      evidence=evidence, now=now)


def restore_household(store: Store, row, *, now: float) -> bool:
    """nightly.growth.undo of a "household" ledger row, inside its transaction: her card's
    household back to what it was, while it still reads as the row wrote it (a later hand edit
    stands); her arc's partner back while it is still "separated", and with it the cover story
    on a plan still open that has none; the separation's arc event taken out of her events and
    its private memory rejected, while it is active. False when nothing is left to reverse."""
    try:
        entry = json.loads(row["text"])
        before, after = entry["household"]
        partner = entry["partner"]
    except (ValueError, KeyError, TypeError):
        return False
    cover, memory, event = entry.get("cover") or "", entry.get("memory"), entry.get("event")
    guid, done = row["guid"], False
    p = load_persona(store, guid)
    if p is not None and p.real_life.household == after:
        race, class_name = _race_class(store, store.cast(), guid)
        save_persona(store, replace(p, real_life=replace(p.real_life, household=before)),
                     source="generated", ts=now, race=race, class_name=class_name)
        done = True
    arc = get_arc(store, guid, int(store.get_meta("human_guid") or 0))
    back = arc
    if back.partner == "separated":
        back = replace(back, partner=partner)
        if cover and back.plan_state in _OPEN_PLANS and not back.plan.get("cover"):
            back = replace(back, plan=back.plan | {"cover": cover})
    if event is not None:
        back = replace(back, events=tuple(e for e in back.events if list(e) != list(event)))
    if back != arc:
        put_arc(store, replace(back, updated=now))
        done = True
    if memory is not None and store.conn.execute(
            "UPDATE memory SET status = 'rejected' WHERE id = ? AND owner = ? "
            "AND status = 'active'", (memory, guid)).rowcount:
        done = True
    return done
