"""Romance toward the human, the men's jealousy asides, the women's rivalry and unprompted
whispers.

Owner, 2026-10-30 ("romance: no brakes"): every persona is an adult, everything is consensual,
and romance is aimed only at the human's character, never between members. Nothing else is a
rule. How far a woman goes follows her attitude (persona.romance.stance: fond, drawn or lust)
and the owner's dial, or her own when he has set one for her (persona.romance.dial), on
whatever channel the conversation is on. Men read as friends: a man marked fond (a seed may
be) never gets the romance frame or a whisper weight for it. The romance stage, which P1c
moves, is narrative: it adds history and, deeper in, more intimacy, and never caps or forbids
anything. The loop guards (guards.py) still brake the volume of every line; they are not
romance rules.

Built-in text is non-explicit at every setting. Explicit wording comes only from the owner's
local overlays (mind-data/prompts/romance_<band>.txt) through prompts.load_prompt; a missing
romance_explicit overlay falls back to the suggestive built-in and is warned about once per
data dir."""
import itertools
import logging
import math
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.schema import Persona
from leeroy_mind.prompts import load_prompt
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.budget import GAP_SPREAD, HOLD_PUSH_S, DayPhase
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.select import pick_speakers
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens, truncate_words
from leeroy_mind.tuning.effective import dials_of

log = logging.getLogger("leeroy_mind.romance")

# A drawn or lust woman's OPEN or CONTINUE turn becomes a FLIRT with p = min(0.9, base x
# candour) while the human is active (controller ruling, Task 16; no flirt budget, no stages).
FLIRT_BASE = {"drawn": 0.3, "lust": 0.45}
FLIRT_CAP = 0.9


def forward(p: Persona) -> float:
    """Her forward / flirty dial (tuning) as a multiplier: 1.0 as written, 0 to 2."""
    return dials_of(p).mult("forward")


# Her forward dial (tuning) at these levels and past them makes her attitude's wording the
# boldest, or the quietest, of its variants (attitude_text); in between, extraversion picks.
FORWARD_BOLD, FORWARD_QUIET = 8, 2


def flirt_chance(p: Persona, candour: float) -> float:
    """How likely a member's social turn turns into a FLIRT at the human. Fond and none women
    never flirt like this (a fond woman's warmth rides her turns to him); men are friends. Her
    forward dial (tuning) scales it, never past FLIRT_CAP."""
    if p.identity.player_gender != "female" or p.romance.stance not in FLIRT_BASE:
        return 0.0
    return min(FLIRT_CAP, FLIRT_BASE[p.romance.stance] * candour * forward(p))


Band = Literal["warm", "flirty", "suggestive", "explicit"]
BANDS: tuple[Band, ...] = ("warm", "flirty", "suggestive", "explicit")

# Where each attitude's wording starts, and how much further a deeper stage takes it. The
# dial's band is the only ceiling: nothing here lowers anything.
ATTITUDE_REACH = {"fond": 0, "drawn": 1, "lust": 3}
# Controller ruling, rehearsal 2 (the owner's "they all want him", "no brakes", dial 9): at
# this dial and above a drawn woman reaches one band further, to suggestive.
NO_BRAKES_DIAL = 9
# A woman's own dial (persona.romance.dial, the owner's setting for her alone) stands in for his
# in everything she reads and does toward him. At this one every turn of hers to him carries her
# romance (pipeline._romance) and a lust woman reads as the boldest (attitude_text).
FULL_DIAL = 10
STAGE_DEPTH = {"none": 0, "notices": 0, "ex": 0, "banter": 1, "whispers": 1,
               "vulnerable": 2, "invitation": 2, "acknowledged": 2}

# Templates take {name} (HumanIdentity.called) and {them}/{their} (HumanIdentity.forms).
# How forward she is within her attitude follows her extraversion (attitude_text): fifteen
# lust women were all "the most forward" (prompts I5).
ATTITUDE_TEXT = {
    "fond": {"open": ("You're fond of {name}: warm and attentive with {them}, glad when "
                      "{their} name pops up, quick to remember what was said last time.")},
    "drawn": {
        "bold": ("You're into {name} and it shows: you tease {them}, angle for {their} "
                 "attention and flirt whenever there's an opening."),
        "quiet": ("You're into {name}, a little more than you let on: you angle for {their} "
                  "attention and flirt when the moment's right."),
    },
    "lust": {
        "bold": ("You want {name} and you don't hide it: you're one of the boldest with "
                 "{them}, and you say what you'd like."),
        "open": ("You want {name}, and it shows: you flirt openly and let {them} know what "
                 "you'd like."),
        "quiet": "You want {name} more than you let on, and it slips out around {them}.",
    },
}
STAGE_TEXT = {
    "none": "",
    "notices": "Lately you've caught yourself noticing {name} more than you let on.",
    "banter": "You and {name} have a running back-and-forth going.",
    "whispers": "You and {name} whisper each other privately now and then.",
    "vulnerable": "You've told {name} things you don't tell the guild.",
    "invitation": "Something more has been offered between you two, and it's still in the air.",
    "acknowledged": "You and {name} are a thing now, and the guild knows it.",
    "ex": "You and {name} had something once and it ended. There's history.",
}
# Drawn and lust women start at "notices" (seed_relationships) but are past just noticing him.
INTO_HIM_ALREADY = "You've had your eye on {name} for a while."
STYLE_TEXT = {
    "bold": "Your way: you make the first move and say what you mean.",
    "shy": "Your way: it slips out sideways, then you laugh it off or change the subject.",
    "earnest": "Your way: plain and sincere, no games.",
    "theatrical": ("Your way: big and dramatic, played up like there's an audience, half joke "
                   "and all meant."),
    "guarded": "Your way: dry and understated, a remark that lands a beat too warm.",
    "competitive": ("Your way: {their} attention is something to win, especially when other "
                    "women are after it."),
}
# A woman with no style of her own gets one from her humour, the same every time (prompts I5).
HUMOUR_STYLE = {
    "dry": STYLE_TEXT["guarded"],
    "teasing": "Your way: you tease {them} and watch for a bite.",
    "absurd": STYLE_TEXT["theatrical"],
    "self-deprecating": "Your way: you flirt by putting yourself down and hoping {name} argues.",
    "puns": "Your way: puns and double meanings, the worse the better.",
    "dark": "Your way: deadpan and a little dark, and you mean it.",
}
OVERLAY_CAP_TOKENS = 200  # an overlay's band text, capped on its own (prompts lens, minor 6)
# At the explicit band in a whisper to him the owner's whisper overlay is read too, and goes
# after the turn's own instruction, the last thing she reads (rehearsal 2, judge C 2: the
# overlay only permitted, mid-paragraph, and "one short line" came last).
WHISPER_OVERLAY = "romance_explicit_whisper"
# Every overlay a romance prompt reads, with the cap it is cut to (the doctor reports them).
OVERLAY_CAPS = {**{f"romance_{b}": OVERLAY_CAP_TOKENS for b in BANDS},
                WHISPER_OVERLAY: OVERLAY_CAP_TOKENS}
BUILTIN_ROMANCE: dict[str, str] = {
    "warm": "Keep the wording warm: a kind word, an in-joke, real interest.",
    "flirty": ("Flirty wording is fine: playful teasing, a compliment with a wink, a little "
               "tension."),
    "suggestive": ("Suggestive wording is fine: innuendo, double meanings, a hint of what you'd "
                   "like, nothing graphic."),
}
BUILTIN_ROMANCE["explicit"] = BUILTIN_ROMANCE["suggestive"]  # the real thing is an overlay
SPECIFIC = ("Hook it on what's actually happening (what {name} just said, the pull, the loot, "
            "the hour), never a canned line.")
# The frame, worded as one and read mid-block, not as a line to say back (prompts I5).
INVARIANTS = "It's only ever with {name}, two adults, and only what you both want."
_AUDIENCE = {"guild": "whole guild", "officer": "officers", "party": "party", "raid": "raid",
             "say": "people standing nearby"}
_LATE = frozenset({DayPhase.LATE, DayPhase.DEAD})

_warned: set[str] = set()


def band(dial: int) -> Band:
    """The owner's dial (1 milquetoast .. 10 filth) as the ceiling of the wording."""
    if dial <= 3:
        return "warm"
    if dial <= 6:
        return "flirty"
    if dial <= 8:
        return "suggestive"
    return "explicit"


def own_dial(p: Persona) -> int | None:
    """The dial she has of her own (persona.romance.dial), if it counts: only a woman with an
    attitude toward him (fond, drawn or lust) has one."""
    if p.romance.dial is None or not _is_woman(p) or p.romance.stance not in ATTITUDE_REACH:
        return None
    return p.romance.dial


def dial_for(p: Persona, dial: int) -> int:
    """The dial that governs her turns toward him: her own when she has one, else `dial`, the
    owner's (mind.toml)."""
    own = own_dial(p)
    return dial if own is None else own


def effective_band(stance: str, stage: str, dial: int) -> Band | None:
    """How far the wording goes: the attitude's reach (a drawn woman's one band further from
    NO_BRAKES_DIAL) plus the stage's depth, up to the dial's band. None for stance "none". A
    deeper stage only ever raises it."""
    reach = ATTITUDE_REACH.get(stance)
    if reach is None:
        return None
    if stance == "drawn" and dial >= NO_BRAKES_DIAL:
        reach += 1
    return BANDS[min(BANDS.index(band(dial)), reach + STAGE_DEPTH.get(stage, 0))]


def explicit_whisper(p: Persona, rel: Relationship | None, *, dial: int, channel: str) -> bool:
    """Her romance in a whisper to him is at the explicit band: the whisper overlay is read,
    and her reply may run to a line or two. `dial` is the owner's; her own replaces it."""
    if channel != "whisper" or not _is_woman(p):
        return False
    stage = rel.romance_stage if rel is not None else "none"
    return effective_band(p.romance.stance, stage, dial_for(p, dial)) == "explicit"


def whisper_tail(p: Persona, rel: Relationship | None, *, dial: int, channel: str,
                 data_dir: Path) -> str:
    """The owner's whisper overlay (mind-data/prompts/romance_explicit_whisper.txt), capped
    like the band overlays, for a whisper to him at the explicit band; "" otherwise or when
    there is none (the band as before). The pipeline puts it after the turn's instruction."""
    if not explicit_whisper(p, rel, dial=dial, channel=channel):
        return ""
    text = load_prompt(WHISPER_OVERLAY, data_dir, "")
    return _capped(text, WHISPER_OVERLAY, data_dir) if text else ""


def stage_text(stance: str, stage: str) -> str:
    """The stage's line for this attitude (a template taking {name})."""
    if stage == "notices" and stance in ("drawn", "lust"):
        return INTO_HIM_ALREADY
    return STAGE_TEXT.get(stage, "")


def _is_woman(p: Persona) -> bool:
    return p.identity.player_gender == "female"


def _capped(text: str, name: str, data_dir: Path) -> str:
    """An overlay over OVERLAY_CAP_TOKENS keeps its start; the cut is warned about once per
    overlay and data dir, without the text."""
    if estimate_tokens(text) <= OVERLAY_CAP_TOKENS:
        return text
    key = f"cap:{name}:{Path(data_dir).resolve()}"
    if key not in _warned:
        _warned.add(key)
        log.warning("the %s overlay is capped at %d estimated tokens (it has %d)", name,
                    OVERLAY_CAP_TOKENS, estimate_tokens(text))
    return truncate_words(text, OVERLAY_CAP_TOKENS * 4)


def _band_text(b: Band, data_dir: Path) -> str:
    if b != "explicit":
        return _capped(load_prompt(f"romance_{b}", data_dir, BUILTIN_ROMANCE[b]),
                       f"romance_{b}", data_dir)
    text = load_prompt("romance_explicit", data_dir, "")
    if text:
        return _capped(text, "romance_explicit", data_dir)
    key = str(Path(data_dir).resolve())
    if key not in _warned:
        _warned.add(key)
        log.warning("no romance_explicit overlay in %s: the explicit band uses the built-in "
                    "suggestive wording", Path(data_dir) / "prompts")
    return BUILTIN_ROMANCE["explicit"]


def attitude_text(p: Persona) -> str:
    """Her attitude's line, as forward as her extraversion: lust bold from 7 (or at her own
    dial 10), quiet to 3, open between; drawn bold from 6."""
    variants = ATTITUDE_TEXT[p.romance.stance]
    ext = p.big_five.extraversion.score
    level = dials_of(p).values.get("forward")
    if level is not None and p.romance.stance in ("drawn", "lust") and (
            level >= FORWARD_BOLD or level <= FORWARD_QUIET):
        return variants["bold" if level >= FORWARD_BOLD else "quiet"]
    if p.romance.stance == "lust":
        key = ("bold" if ext >= 7 or own_dial(p) == FULL_DIAL
               else "quiet" if ext <= 3 else "open")
    elif p.romance.stance == "drawn":
        key = "bold" if ext >= 6 else "quiet"
    else:
        key = "open"
    return variants[key]


def style_text(p: Persona) -> str:
    """Her way: her own style, or one her humour gives her (HUMOUR_STYLE)."""
    if p.romance.style:
        return STYLE_TEXT.get(p.romance.style, "")
    return HUMOUR_STYLE.get(p.humour, "")


def romance_instruction(p: Persona, rel: Relationship | None, *, dial: int, channel: str,
                        addressee_is_human: bool, phase: DayPhase, human: HumanIdentity,
                        data_dir: Path) -> str | None:
    """The romance text for a turn by `p` (rel is p's relationship to the human), or None
    when p is a man, has no attitude toward him or is not talking to him. The human is named
    by human.called with his pronouns from human.forms; his real name never appears here. In
    order: her attitude, her way, the stage, the band's wording, the frame (INVARIANTS), the
    channel and the hour, and the hook on what's happening last. The pipeline puts it before
    the turn's own instruction, so the model reads that last. `dial` is the owner's; the band
    follows her own when she has one."""
    stance = p.romance.stance
    if not addressee_is_human or stance not in ATTITUDE_REACH or not _is_woman(p):
        return None
    dial = dial_for(p, dial)
    stage = rel.romance_stage if rel is not None else "none"
    _, them, their = human.forms
    fill = dict(name=human.called, them=them, their=their)
    audience = _AUDIENCE.get(channel, "everyone around")
    parts = [
        attitude_text(p),
        style_text(p),
        stage_text(stance, stage),
    ]
    rest = [
        INVARIANTS,
        ("It's a private whisper, just the two of you." if channel == "whisper"
         else "The people in the room can hear you, so play to the room."
         if channel.startswith("voice:")
         else f"The {audience} can read it, so play to the room."),
        "It's late, and late-night talk runs warmer." if phase in _LATE else "",
        SPECIFIC,
    ]
    head = " ".join(x.format(**fill) for x in parts if x)
    tail = " ".join(x.format(**fill) for x in rest if x)
    return f"{head} {_band_text(effective_band(stance, stage, dial), data_dir)} {tail}"


# -- the men's jealousy asides ---------------------------------------------------------------

Trigger = Literal["public_flirt", "loot_cheer"]  # what reactions.py sets off
MODE_WEIGHTS = {"play_by_play": 1.2, "teaser": 1.0, "grumbler": 1.0, "sulker": 1.0,
                "good_sport": 0.5}
DEFUSE_P = 0.5
HOUR_S = 3600.0
_SOUR = frozenset({"sulker", "grumbler"})
_HOLD = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})

# Templates take {woman}, {name}, {they}, {them}, {their} and, for "defuse", {sulker}.
TRIGGER_TEXT = {
    "public_flirt": "{woman} is flirting with {name} in chat.",
    "loot_cheer": "{woman} is cheering {name}'s loot like it dropped for her.",
}
ASIDE_INSTRUCTIONS = {
    "play_by_play": "Call it like a sports commentator, one dry line of play-by-play.",
    "teaser": "Tease {woman} about it, or rib {name} for being the guild's heartthrob.",
    "grumbler": "Grumble, half joking, that nobody ever carries on like that over you.",
    "sulker": "Sulk a little about it, one short sigh of a line, then let it go.",
    "good_sport": "Laugh it off like a good sport, maybe with a wink at {name}.",
    "defuse": ("{sulker} just got sour about it. Lighten things up with a quick joke that's "
               "friendly to everyone."),
}
# Envy is the sour and teasing modes' (prompts lens, minor 2); every aside is banter, and the
# man never flirts himself (privacy lens, minor 6).
ENVIOUS = frozenset({"sulker", "grumbler", "teaser"})
ASIDE_RULE = {"envious": "You're a bit envious of the attention {name} gets, but it's banter: "
                         "never cruel.",
              "plain": "It's banter: never cruel."}
NO_FLIRT = "You never flirt with her or anyone else yourself."


@dataclass(frozen=True, slots=True)
class AsidePlan:
    """counted: it holds a place in the hour (a defuse doesn't); n identifies it there."""
    speaker: int
    instruction: str
    kind: TurnKind = TurnKind.ASIDE
    counted: bool = True
    n: int = 0


class JealousyAsides:
    """The men's envy of the attention the human gets, as short asides. At jealousy j (the
    owner's [romance] jealousy, 0-10) at most round(2j/5) asides start in any rolling hour,
    each trigger starts one with p = min(0.9, j/10), never in a fight or a wipe, and the
    speaker is drawn by jealousy mode (MODE_WEIGHTS). After a sour one (sulker, grumbler) a
    good sport may defuse it (DEFUSE_P); a defuse is not counted against the hour. An aside
    holds its place in the hour from its plan; one that is never said is release()d (final
    fix wave), so only the asides said count."""

    def __init__(self, rng: random.Random, *, jealousy: int, human: HumanIdentity | None = None):
        self._rng = rng
        self._human = human or HumanIdentity(0, "", "", "", False)
        self._started: dict[int, float] = {}  # AsidePlan.n -> when it was planned
        self._n = itertools.count(1)
        self.set_jealousy(jealousy)

    def set_jealousy(self, j: int) -> None:
        self.jealousy = j
        self.per_hour = round(2 * j / 5)
        self.p = min(0.9, 0.5 * j / 5)

    def set_human(self, human: HumanIdentity) -> None:
        """A changed name or pronouns: later asides use them; the hour's count stays."""
        self._human = human

    def _text(self, key: str, trigger: str, woman: Persona, sulker: str = "") -> str:
        they, them, their = self._human.forms
        fill = dict(woman=woman.character_name, name=self._human.called, they=they, them=them,
                    their=their, sulker=sulker)
        rule = ASIDE_RULE["envious" if key in ENVIOUS else "plain"]
        return " ".join(t.format(**fill) for t in (TRIGGER_TEXT[trigger],
                                                   ASIDE_INSTRUCTIONS[key], rule, NO_FLIRT))

    def on_trigger(self, trigger: Trigger, woman: Persona, now: float, raid: RaidPhase,
                   men: dict[int, Persona]) -> list[AsidePlan]:
        """`woman` is the one who flirted, cheered or asked after him; `men` are the members
        who may answer (online, not AFK), keyed by guid. She is never one of the speakers."""
        if trigger not in TRIGGER_TEXT:
            raise ValueError(f"unknown aside trigger {trigger!r}")
        if self.per_hour <= 0 or raid in _HOLD:
            return []
        self._started = {n: t for n, t in self._started.items() if now - t < HOUR_S}
        if len(self._started) >= self.per_hour:
            return []
        pool = {g: MODE_WEIGHTS[m.romance.jealousy_mode] * dials_of(m).mult("jealousy")
                for g, m in sorted(men.items())
                if g != woman.guid and m.romance.jealousy_mode in MODE_WEIGHTS}
        pool = {g: w for g, w in pool.items() if w > 0}  # a man at jealousy 0 never reacts
        if not pool or self._rng.random() >= self.p:
            return []
        speaker = pick_speakers(pool, 1, self._rng)[0]
        mode = men[speaker].romance.jealousy_mode
        n = next(self._n)
        self._started[n] = now
        plans = [AsidePlan(speaker, self._text(mode, trigger, woman), n=n)]
        if mode in _SOUR:
            sports = [g for g, m in sorted(men.items()) if g not in (speaker, woman.guid)
                      and m.romance.jealousy_mode == "good_sport"]
            if sports and self._rng.random() < DEFUSE_P:
                plans.append(AsidePlan(self._rng.choice(sports), self._text(
                    "defuse", trigger, woman, men[speaker].character_name), counted=False))
        return plans

    def release(self, plan: AsidePlan) -> None:
        """The aside was never said (dropped, silent, cancelled): it gives its place in the
        hour back. Releasing it again, or an uncounted one, does nothing."""
        if plan.counted:
            self._started.pop(plan.n, None)


# -- the women's rivalry ---------------------------------------------------------------------

RIVALRY_P = 0.3  # at jealousy 10
RIVAL_STANCES = frozenset({"drawn", "lust"})
RIVALRY_TEXT = ("{flirter} is flirting with {name} in front of everyone. Cut in and compete for "
                "{their} attention, playful, and never nasty to {flirter}.")


def rivalry_reply(flirter: int, drawn: dict[int, Persona], jealousy: int,
                  rng: random.Random) -> int | None:
    """Another woman who wants the human answers a public flirt with one of her own, with
    p = 0.3 * jealousy / 10. Candidates are the drawn and lust women other than the flirter;
    a competitive one is twice as likely. Use rivalry_text for her turn's extra."""
    rivals = {g: (2.0 if p.romance.style == "competitive" else 1.0) * dials_of(p).mult("jealousy")
              for g, p in sorted(drawn.items())
              if g != flirter and _is_woman(p) and p.romance.stance in RIVAL_STANCES}
    rivals = {g: w for g, w in rivals.items() if w > 0}  # her jealousy dial (tuning) at 0
    if not rivals or rng.random() >= RIVALRY_P * jealousy / 10:
        return None
    return pick_speakers(rivals, 1, rng)[0]


def rivalry_text(flirter: Persona, human: HumanIdentity) -> str:
    return RIVALRY_TEXT.format(flirter=flirter.character_name, name=human.called,
                               their=human.forms[2])


# -- unprompted whispers ---------------------------------------------------------------------

WhisperTrigger = Literal["after_kill", "idle", "late", "evening"]
WHISPER_TRIGGERS = frozenset({"after_kill", "idle", "late", "evening"})
# The planner's own clock (owner, 2026-11-07: "they only respond, never actually initiate"):
# its whispers are why "whisper:start". The event triggers above still draw with WHISPER_P.
START = "start"
WHISPER_P = 0.3
WHISPER_GAP_S = 600.0
# Lust and drawn women whisper him most; a fond woman at a quarter of a lust woman's weight at
# equal affinity (FOND_SHARE, controller ruling 2026-11-07); men and women with no attitude
# only from warmth (affinity / 50). Only drawn and lust women's whispers are romantic.
WHISPER_WEIGHT = {"lust": 3.0, "drawn": 2.0}
FOND_SHARE = 0.25
ROMANTIC_STANCES = frozenset({"drawn", "lust"})
WHISPER_AFFINITY_SCALE = 50.0
# How far the member dials can move the clock's rate (forward, talkative, the romance lift).
RATE_MULT_MAX = 2.0
# Whatever the rate and the dials: at most this many unprompted whispers in any hour, and two
# of them never closer than this (an event's draw right after the clock's, his AFK return).
WHISPERS_HOUR_MAX = 8
WHISPERS_APART_S = 120.0
WhisperKind = Literal["romantic", "casual"]


def romance_lift(dial: int) -> float:
    """The owner's romance dial (the world's, mind.toml or its override) on the whisper weight
    of the women who want him (drawn, lust): 1.0 at 5, 0.6 at 1, 1.5 at 10. Nobody else's."""
    return 0.5 + dial / 10


def whisper_weight(p: Persona, r: Relationship | None, *, lift: float = 1.0,
                   neutral: bool = False) -> float:
    """How strongly a member is drawn to whisper him first: a drawn or lust woman's attitude
    (WHISPER_WEIGHT) times her forward dial and `lift`, plus anyone's warmth toward him
    (affinity / WHISPER_AFFINITY_SCALE); a fond woman FOND_SHARE of what a lust woman's would
    be at her affinity (forward, no lift); all times the talkative dial. 0: no reason to
    whisper him. `neutral`: as written, with no dial and no lift (the planner's rate scale)."""
    stance = p.romance.stance if _is_woman(p) else "none"
    fwd = 1.0 if neutral else forward(p)
    warmth = max(0.0, r.affinity if r is not None else 0.0) / WHISPER_AFFINITY_SCALE
    if stance == "fond":
        w = FOND_SHARE * (WHISPER_WEIGHT["lust"] * fwd + warmth)
    else:
        attitude = WHISPER_WEIGHT.get(stance, 0.0)
        w = attitude * (1.0 if neutral else fwd * lift) + warmth
    return w if neutral else w * dials_of(p).mult("talkative")


def whisper_kind(p: Persona) -> WhisperKind:
    """What a member's whisper to him starts: romantic from a drawn or lust woman, casual from
    everyone else, a fond woman included (controller ruling 2026-11-07); men are friends."""
    if _is_woman(p) and p.romance.stance in ROMANTIC_STANCES:
        return "romantic"
    return "casual"


class WhisperPlanner:
    """Who whispers the human unprompted, and when. Two ways in:
    - its own clock (due, start): about `per_hour` whispers an hour of his play (mind.toml's
      [talk] whispers_per_hour, which TalkEngine scales with chattiness), each gap GAP_SPREAD
      of the mean, scaled by the member dials of those who could whisper (forward, talkative
      and the romance lift, against the same members as written; at most RATE_MULT_MAX).
      Every due slot draws a whisperer by whisper_weight;
    - an event (maybe: a kill, an idle stretch, the late hour, the evening), which draws with
      p WHISPER_P and, when it lands, takes the clock's slot (the next one is a whole gap
      later), so events never add a second rate on top. With `events` off (TalkEngine: at a
      rate of 0, chattiness 0 included) no event draws either.
    Either way at most WHISPERS_HOUR_MAX in any hour and never two within WHISPERS_APART_S.
    One member's unprompted whispers are at least WHISPER_GAP_S apart, across sessions too,
    so a relog never resets it. There is no per-session cap and no stage or affinity
    threshold; someone with neither an attitude nor warmth has no reason to whisper him.
    Replies to his own whispers never come through here and are never paced
    (select.plan_replies). Call maybe() once per event, not on every tick; due() every tick."""

    def __init__(self, rng: random.Random, *, per_hour: float = 0.0, events: bool = True):
        self._rng = rng
        self._last: dict[int, float] = {}
        self._per_hour = max(0.0, per_hour)
        self.events = events
        self._mult = 1.0  # the member dials' pull on the rate, from the last draw
        self._next_at: float | None = None
        self._seen = 0.0  # the last due() time, for a rescale
        self._said: deque[float] = deque()  # every unprompted whisper in the last hour

    @property
    def next_at(self) -> float | None:
        """When the clock's next whisper is due (None: not drawn yet, or off)."""
        return self._next_at

    def start_session(self) -> None:
        """A new session keeps the pacing (a relog is not a reason to whisper again); its
        clock starts over at the first due()."""
        self._next_at = None

    def set_rate(self, per_hour: float) -> None:
        """A new rate (a reloaded mind.toml or world dial): the pending gap is rescaled, as
        BurstBudget.set_chattiness does; 0 stops the clock."""
        old, self._per_hour = self._per_hour, max(0.0, per_hour)
        if self._per_hour <= 0:
            self._next_at = None
        elif self._next_at is not None and old > 0 and old != self._per_hour:
            left = max(0.0, self._next_at - self._seen)
            self._next_at = self._seen + left * old / self._per_hour

    def _gap(self) -> float | None:
        """A gap of the clock, its rate capped at WHISPERS_HOUR_MAX; a pull of 0 (nobody would
        whisper) keeps the written rate, whose draws then find nobody, so a later dial change
        is still read."""
        rate = min(float(WHISPERS_HOUR_MAX), self._per_hour * (self._mult or 1.0))
        return 3600.0 / rate * self._rng.uniform(*GAP_SPREAD) if rate > 0 else None

    def _reschedule(self, now: float) -> None:
        gap = self._gap()
        self._next_at = None if gap is None else now + gap

    def due(self, now: float, *, hold: bool = False) -> bool:
        """Whether the clock's next whisper is due now. The first call (each session) draws
        the first gap; while `hold` (his raid's or group's fight, its wipe or debrief, his
        AFK, his own talk just now) a due whisper waits, until HOLD_PUSH_S after it ends at
        the soonest."""
        self._seen = now
        if self._per_hour <= 0:
            self._next_at = None
            return False
        if self._next_at is None:
            self._reschedule(now)
            return False
        if hold:
            self._next_at = max(self._next_at, now + HOLD_PUSH_S)
            return False
        return now >= self._next_at

    def refusal(self, now: float) -> str | None:
        """Why one more unprompted whisper does not fit now: "ceiling" (WHISPERS_HOUR_MAX in
        the last hour) or "spacing" (within WHISPERS_APART_S of the last one); None when it
        fits."""
        while self._said and now - self._said[0] >= HOUR_S:
            self._said.popleft()
        if len(self._said) >= WHISPERS_HOUR_MAX:
            return "ceiling"
        if self._said and now - self._said[-1] < WHISPERS_APART_S:
            return "spacing"
        return None

    def _room(self, now: float) -> bool:
        """Whether one more unprompted whisper fits (refusal)."""
        return self.refusal(now) is None

    def _pool(self, now: float, candidates: dict[int, tuple[Persona, Relationship]],
              lift: float) -> dict[int, float]:
        pool = {}
        for g, (p, r) in sorted(candidates.items()):
            if now - self._last.get(g, -math.inf) < WHISPER_GAP_S:
                continue
            w = whisper_weight(p, r, lift=lift)
            if w > 0:
                pool[g] = w
        return pool

    def _drawn(self, now: float, pool: dict[int, float]) -> int:
        g = pick_speakers(pool, 1, self._rng)[0]
        self._last[g] = now
        self._said.append(now)
        return g

    def start(self, now: float, candidates: dict[int, tuple[Persona, Relationship]], *,
              lift: float = 1.0) -> int | None:
        """The due whisper's whisperer (no WHISPER_P roll), drawn by whisper_weight over
        `candidates` (who could whisper now, with their relationship to him); `lift` is
        romance_lift of the world's dial. The member dials' pull on the rate is read from the
        same candidates and the next gap drawn. None when nobody may whisper now, or the hour
        or the last whisper leaves no room: the slot passes."""
        live = sum(whisper_weight(p, r, lift=lift) for p, r in candidates.values())
        written = sum(whisper_weight(p, r, neutral=True) for p, r in candidates.values())
        if written > 0:
            self._mult = min(RATE_MULT_MAX, live / written)
        self._reschedule(now)
        pool = self._pool(now, candidates, lift)
        if not pool or not self._room(now):
            return None
        return self._drawn(now, pool)

    def maybe(self, now: float, trigger: WhisperTrigger,
              candidates: dict[int, tuple[Persona, Relationship]]) -> int | None:
        """An event's draw. candidates: the members who could whisper now (online, not AFK),
        with their relationship to the human. Fires with p WHISPER_P while `events` is on and
        the hour and the last whisper leave room; returns the whisperer's guid, starts her
        pacing and, with the clock running, takes its slot."""
        if trigger not in WHISPER_TRIGGERS:
            raise ValueError(f"unknown whisper trigger {trigger!r}")
        if not self.events:
            return None
        pool = self._pool(now, candidates, 1.0)
        if not pool or not self._room(now) or self._rng.random() >= WHISPER_P:
            return None
        g = self._drawn(now, pool)
        if self._per_hour > 0:
            self._reschedule(now)
        return g
