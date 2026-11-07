"""The sample-line preview behind the console's dials: what a proposed value would change, before
it is set. Nothing here is stored, logged or broadcast; the answer goes only to the caller.

- A dial that changes how often rather than how (talkative, forward, jealousy, party, drama,
  opening up, reliability; chattiness and the world's jealousy) shows numbers, worked out with
  the functions the live path uses. No model is asked.
- A wording dial (argumentative, humour, theorycrafter, lore; the world's romance) shows a line
  before and after, through the mind's own prompt and model path (Pipeline.preview_line), with
  the same Ollama seed in both requests, at background priority so a reply to him goes first.

A line preview runs only while the live chat model is already resident and no night run is
under way or due (the GPU rules of nightly.gpu): it never loads, unloads or swaps a model. It
holds a short live lease of its own meanwhile, so no night can start under it, and gives it
back; a lease another holder took meanwhile gets the hand-over its own start skipped."""
import asyncio
import logging
import random

from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log as log_event
from leeroy_mind.nightly.gpu import norm
from leeroy_mind.talk.budget import GAP_REFERENCE_CHATTINESS, PHASE_MODS, DayPhase
from leeroy_mind.talk.prompt import pushback_help
from leeroy_mind.talk.romance import (MODE_WEIGHTS, RIVALRY_P, band, flirt_chance, forward,
                                      whisper_weight)
from leeroy_mind.talk.select import SpeakerContext, speaker_weight
from leeroy_mind.talk.turns import Turn, TurnKind

from . import changes as C
from . import groups as G
from .dials import MEMBER_DIALS, PARTY_SUBSTANCES, WORLD_DIALS, applies
from .effective import dials_of

log = logging.getLogger("leeroy_mind.tuning")

LEASE = "tuning_preview"
LINE_DEADLINE_S = 15.0  # each request's deadline at background priority
SUBJECTS = 3  # a group or world change is previewed on up to this many members it reaches
GAP_S = (240.0, 720.0)  # talk.budget.BurstBudget's default social gap at the reference

REFUSE = {
    "stopped": "The mind isn't running, so it can't preview or change dials. Start it from the "
               "Mind tab.",
    "night": "Tonight's reflection is using the GPU. Previews come back when it finishes or "
             "when you log in.",
    "unloaded": "The chat model isn't loaded (it unloads 10 minutes after the last session). "
                "Previews work while you're in game, during an offline raid, or within 10 "
                "minutes of logging off.",
    "silent": "She'd say nothing here.",
    "tells": "The model's line failed the checks twice.",
    "busy": "The guild is talking a lot right now and the preview timed out. Try again in a "
            "moment.",
    "nobody": "Nobody this change reaches can be previewed.",
}
# His line each wording dial's sample answers, and how (a reply in guild chat by default).
SCENARIO = {
    "argumentative": "what are you talking about? that's not what happened",
    "humour": "long day at work, glad to be on",
    "theorycraft": "what do you reckon about gear and stats for my spec?",
    "lore": "anyone know the story behind this place?",
    "romance": "hey you",
}


# -- subjects -----------------------------------------------------------------------------------

def _subjects(app, pic: C.Picture, tuning, scope: str, target: str, dial: str) -> list[int]:
    """The members to preview on: the member, or up to SUBJECTS that the change reaches, the
    core ones first, the rest drawn by a seed of their own."""
    if scope == "member":
        return [int(target)]
    members = sorted(g for g in pic.personas if g != pic.human and g in pic.cast)
    if scope == "group":
        grp = tuning.groups.get(target)
        if grp is None:
            return []
        members = [g for g in members if grp.builtin
                   or G.matches(grp.where, pic.personas[g], pic.cast.get(g))]
    if dial in MEMBER_DIALS:
        members = [g for g in members if applies(dial, pic.personas[g])]
    if dial == "romance":  # the world's romance dial reads in a woman's words to him
        members = [g for g in members if pic.personas[g].identity.player_gender == "female"
                   and pic.personas[g].romance.stance in ("fond", "drawn", "lust")]
    rng = random.Random(f"preview:{scope}:{target}:{dial}")
    rng.shuffle(members)
    members.sort(key=lambda g: not pic.personas[g].core)  # stable: the draw within each
    return members[:SUBJECTS]


# -- numbers ------------------------------------------------------------------------------------

def _share(app, pic, persona_of, guid: int) -> float:
    """Her share of the opener draw among the members on line now (the whole cast when
    nobody is), as speaker_weight works it out."""
    online = [g for g in sorted(getattr(app.world, "online", ()) or ()) if g in pic.personas
              and g != pic.human] or [g for g in pic.personas if g != pic.human]
    if guid not in online:
        online.append(guid)
    ctx = SpeakerContext(now=app.clock.now(), topic=set(), opener=True, phase=DayPhase.EVENING)
    weights = {g: speaker_weight(persona_of(g), app.store.get_state(g), None, ctx, None, 1.0)
               for g in online}
    total = sum(weights.values())
    return weights[guid] / total if total else 0.0


def _member_numbers(app, pic, dial: str, before, after, guid: int, cast) -> list[dict]:
    b, a = before(guid), after(guid)
    rows = []

    def row(what, x, y, unit=""):
        rows.append({"what": what, "before": x, "after": y, "unit": unit})
    if dial == "talkative":
        row("share of the guild's openers", round(100 * _share(app, pic, before, guid), 1),
            round(100 * _share(app, pic, after, guid), 1), "%")
    elif dial == "forward":
        row("chance a social turn becomes a flirt", round(100 * flirt_chance(b, 1.0), 1),
            round(100 * flirt_chance(a, 1.0), 1), "%")
        row("weight to whisper you unprompted",
            round(whisper_weight(b, None), 2), round(whisper_weight(a, None), 2))
        row("how often her turns to you carry her romance (x as written)",
            round(forward(b), 2), round(forward(a), 2), "x")
    elif dial == "jealousy":
        mode = b.romance.jealousy_mode
        if b.identity.player_gender == "female":
            row("weight to cut in on a rival's flirt", round(dials_of(b).mult("jealousy"), 2),
                round(dials_of(a).mult("jealousy"), 2), "x")
        elif mode in MODE_WEIGHTS:
            row("weight to be the one with an envy aside",
                round(MODE_WEIGHTS[mode] * dials_of(b).mult("jealousy"), 2),
                round(MODE_WEIGHTS[mode] * dials_of(a).mult("jealousy"), 2))
        row("how fast the night's jealousy grows (x as written)",
            round(dials_of(b).mult("jealousy"), 2), round(dials_of(a).mult("jealousy"), 2), "x")
    elif dial == "party":
        for hb, ha in zip(b.habits, a.habits):
            if hb.substance in PARTY_SUBSTANCES:
                row(f"nights their {hb.substance} habit is on ({hb.when.replace('_', ' ')})",
                    round(100 * hb.odds), round(100 * ha.odds), "%")
    elif dial == "drama":
        row("how much they want to retell what they may tell (x as written)",
            round(dials_of(b).mult("drama"), 2), round(dials_of(a).mult("drama"), 2), "x")
        row("weight of a confront, needle or gossip intention (x as written)",
            round(dials_of(b).mult("drama"), 2), round(dials_of(a).mult("drama"), 2), "x")
    elif dial == "openup":
        from leeroy_mind.nightly.storyteller import layer_trust, opens_up
        ext = float(b.big_five.extraversion.score)
        row("confides at night (at their own extraversion)", opens_up(b, ext), opens_up(a, ext))
        for depth in ("surface", "middle", "deep"):
            row(f"trust a {depth} disclosure needs", layer_trust(b, depth),
                layer_trust(a, depth))
    elif dial == "reliability":
        from leeroy_mind.guild.roles import reliability
        row("reliability", reliability(b), reliability(a))
        row("late for a summon", reliability(b) <= 3, reliability(a) <= 3)
    return rows


def _world_numbers(dial: str, before: int, after: int) -> list[dict]:
    def gap(c):
        if c <= 0:
            return None  # the guild never starts talking on its own
        mean = (GAP_S[0] + GAP_S[1]) / 2 * GAP_REFERENCE_CHATTINESS / c
        return round(mean * PHASE_MODS[DayPhase.EVENING].gap_mult / 60, 1)
    if dial == "chattiness":
        return [{"what": "minutes between the guild's bursts of talk, evening, you active",
                 "before": gap(before), "after": gap(after), "unit": "min"}]
    if dial == "selfies":
        from leeroy_mind.selfies.ration import per_day, per_week
        return [{"what": "photos made and stocked a week across the guild, at most", "before": per_week(before),
                 "after": per_week(after), "unit": ""},
                {"what": "photos made and stocked a day, at most", "before": per_day(before),
                 "after": per_day(after), "unit": ""}]
    if dial == "jealousy":
        return [{"what": "envy asides an hour, at most", "before": round(2 * before / 5),
                 "after": round(2 * after / 5), "unit": ""},
                {"what": "chance a flirt sets one off", "before": round(100 * min(0.9, before / 10)),
                 "after": round(100 * min(0.9, after / 10)), "unit": "%"},
                {"what": "chance another woman cuts in", "before": round(100 * RIVALRY_P * before / 10),
                 "after": round(100 * RIVALRY_P * after / 10), "unit": "%"}]
    return [{"what": "the women's wording band", "before": band(before), "after": band(after),
             "unit": ""}]


# -- lines --------------------------------------------------------------------------------------

async def admission(app) -> str | None:
    """None when a line preview may run now, else the REFUSE key that says why."""
    if getattr(app, "stopping", False):
        return "stopped"
    sched = getattr(app, "night_scheduler", None)
    if sched is not None and sched.running:
        return "night"
    arbiter = getattr(app, "arbiter", None)
    if arbiter is None:
        return "unloaded"  # nothing here can tell whether the model is in memory
    try:
        loaded = {norm(n) for n in await arbiter.admin.loaded_models()}
    except Exception as e:  # Ollama away: it can't be resident
        log_event("tune_preview_unchecked", level=logging.WARNING, error=type(e).__name__)
        return "unloaded"
    cfg = app.cfg
    if norm(cfg.models.reflection) in loaded and norm(cfg.models.reflection) != norm(cfg.models.live):
        return "night"
    if norm(cfg.models.live) not in loaded:
        return "unloaded"
    if not arbiter.holders and sched is not None and sched.due() is not None:
        return "night"
    return None


def _turn(app, dial: str, p, guid: int) -> Turn:
    hg = app.human_guid
    if dial == "romance":
        return Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel="whisper", to_guid=hg,
                    addressee=hg, priority=Priority.BACKGROUND,
                    deadline=app.clock.now() + LINE_DEADLINE_S)
    if dial == "humour":
        return Turn(kind=TurnKind.OPEN, speaker=guid, channel="guild", max_lines=1,
                    priority=Priority.BACKGROUND, deadline=app.clock.now() + LINE_DEADLINE_S)
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel="guild", addressee=hg,
                max_lines=1, priority=Priority.BACKGROUND,
                deadline=app.clock.now() + LINE_DEADLINE_S)
    if dial == "argumentative":
        turn.instruction = pushback_help(app.engine.human, "anger",
                                         p.big_five.agreeableness.score)
    return turn


async def _line(app, dial: str, p, guid: int, seed: int, romance: int | None) -> dict:
    turn = _turn(app, dial, p, guid)
    trigger = "" if turn.kind is TurnKind.OPEN else SCENARIO.get(dial, SCENARIO["humour"])
    try:
        text, why = await app.engine.preview_line(turn, p, trigger=trigger, seed=seed,
                                                  dial=romance)
    except ValueError:
        return {"text": "", "note": REFUSE["tells"]}
    if why in ("expired", "llm_error"):
        return {"text": "", "note": REFUSE["busy"]}
    if why == "silent":
        return {"text": "", "note": REFUSE["silent"]}
    if why:
        return {"text": "", "note": REFUSE["tells"]}
    return {"text": text, "note": ""}


async def _lines(app, dial: str, subjects, before, after, romance=(None, None)) -> dict | str:
    why = await admission(app)
    if why is not None:
        return why
    arbiter = app.arbiter
    first = arbiter.hold(LEASE)  # no night starts under the preview; nothing is loaded
    try:
        out = []
        for g in subjects:
            seed = random.Random(f"preview-seed:{g}:{dial}:{app.personas.tuning.version}"
                                 ).randrange(1, 1 << 31)
            b = await _line(app, dial, before(g), g, seed, romance[0])
            a = await _line(app, dial, after(g), g, seed, romance[1])
            out.append({"guid": g, "name": before(g).character_name, "before": b, "after": a})
        return {"lines": out}
    finally:
        await arbiter.release_live(LEASE)
        if first and arbiter.holders:  # a login meanwhile skipped the hand-over: do it now
            asyncio.ensure_future(arbiter.hand_over())


# -- the op -------------------------------------------------------------------------------------

async def preview(app, args: dict) -> dict:
    scope, target, dial, value = args["scope"], args["target"], args["dial"], args["value"]
    service = app.tuning
    pic = service.picture()
    C.check_set(C.Set(scope, target, dial, value), pic, C.live_groups(app.store.conn))
    app.store.conn.commit()
    current = app.personas.tuning
    candidate = current.with_value(scope, target, dial, None if value == 0 and scope == "group"
                                   else value)
    reach = service.reach(scope, target, dial, pic)
    out = {"ok": True, "scope": scope, "target": target, "dial": dial, "value": value,
           "reach": reach, "kind": "numbers", "subjects": [], "world": []}
    if scope == "world":
        d = WORLD_DIALS[dial]
        file_value = getattr(getattr(app.file_cfg, d.section), d.key)
        b = current.world.get(dial, file_value)
        a = candidate.world.get(dial, file_value)
        out["world"] = _world_numbers(dial, b, a)
        if dial != "romance":
            log_event("tune_previewed", dial=dial, kind="numbers")
            return out
    subjects = _subjects(app, pic, current, scope, target, dial)
    if not subjects:
        if out["world"]:
            out["note"] = REFUSE["nobody"]
            return out
        return {"ok": False, "error": REFUSE["nobody"]}

    def before(g):
        return current.apply(pic.personas[g], pic.cast.get(g))

    def after(g):
        return candidate.apply(pic.personas[g], pic.cast.get(g))
    wording = dial == "romance" or (dial in MEMBER_DIALS and MEMBER_DIALS[dial].wording)
    if not wording:
        out["subjects"] = [{"guid": g, "name": pic.personas[g].character_name,
                            "numbers": _member_numbers(app, pic, dial, before, after, g, pic.cast)}
                           for g in subjects]
        log_event("tune_previewed", dial=dial, kind="numbers")
        return out
    romance = (None, None)
    if dial == "romance":
        own = app.file_cfg.romance.dial
        romance = (current.world.get(dial, own), candidate.world.get(dial, own))
    lines = await _lines(app, dial, subjects, before, after, romance)
    if isinstance(lines, str):
        log_event("tune_previewed", dial=dial, kind="refused", reason=lines)
        if out["world"]:
            out["note"] = REFUSE[lines]
            return out
        return {"ok": False, "error": REFUSE[lines]}
    out["kind"] = "lines"
    out["subjects"] = lines["lines"]
    log_event("tune_previewed", dial=dial, kind="lines", subjects=len(lines["lines"]))
    return out
