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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.schema import Persona
from leeroy_mind.prompts import load_prompt
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.select import pick_speakers
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens, truncate_words

log = logging.getLogger("leeroy_mind.romance")

# A drawn or lust woman's OPEN or CONTINUE turn becomes a FLIRT with p = min(0.9, base x
# candour) while the human is active (controller ruling, Task 16; no flirt budget, no stages).
FLIRT_BASE = {"drawn": 0.3, "lust": 0.45}
FLIRT_CAP = 0.9


def flirt_chance(p: Persona, candour: float) -> float:
    """How likely a member's social turn turns into a FLIRT at the human. Fond and none women
    never flirt like this (a fond woman's warmth rides her turns to him); men are friends."""
    if p.identity.player_gender != "female" or p.romance.stance not in FLIRT_BASE:
        return 0.0
    return min(FLIRT_CAP, FLIRT_BASE[p.romance.stance] * candour)


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
        pool = {g: MODE_WEIGHTS[m.romance.jealousy_mode] for g, m in sorted(men.items())
                if g != woman.guid and m.romance.jealousy_mode in MODE_WEIGHTS}
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
    rivals = {g: 2.0 if p.romance.style == "competitive" else 1.0
              for g, p in sorted(drawn.items())
              if g != flirter and _is_woman(p) and p.romance.stance in RIVAL_STANCES}
    if not rivals or rng.random() >= RIVALRY_P * jealousy / 10:
        return None
    return pick_speakers(rivals, 1, rng)[0]


def rivalry_text(flirter: Persona, human: HumanIdentity) -> str:
    return RIVALRY_TEXT.format(flirter=flirter.character_name, name=human.called,
                               their=human.forms[2])


# -- unprompted whispers ---------------------------------------------------------------------

WhisperTrigger = Literal["after_kill", "idle", "late", "evening"]
WHISPER_TRIGGERS = frozenset({"after_kill", "idle", "late", "evening"})
WHISPER_P = 0.3
WHISPER_GAP_S = 600.0
WHISPER_WEIGHT = {"lust": 3.0, "drawn": 2.0, "fond": 1.0}
WHISPER_AFFINITY_SCALE = 50.0


class WhisperPlanner:
    """Who whispers the human unprompted. The only rule is pacing: one member's unprompted
    whispers are at least WHISPER_GAP_S apart, across sessions too, so a relog never resets
    it. There is no per-session cap and no stage or affinity threshold: a woman's attitude
    (WHISPER_WEIGHT) or anyone's warmth toward him (affinity / 50) gives weight, and someone
    with neither has no reason to whisper him. Replies to his own whispers never come
    through here and are never paced (select.plan_replies). Call maybe() once per event (a
    kill, an idle stretch, the late hour), not on every tick."""

    def __init__(self, rng: random.Random):
        self._rng = rng
        self._last: dict[int, float] = {}

    def start_session(self) -> None:
        """A new session keeps the pacing (a relog is not a reason to whisper again)."""

    def maybe(self, now: float, trigger: WhisperTrigger,
              candidates: dict[int, tuple[Persona, Relationship]]) -> int | None:
        """candidates: the members who could whisper now (online, not AFK), with their
        relationship to the human. Fires with p WHISPER_P; returns the whisperer's guid and
        starts her pacing."""
        if trigger not in WHISPER_TRIGGERS:
            raise ValueError(f"unknown whisper trigger {trigger!r}")
        pool = {}
        for g, (p, r) in sorted(candidates.items()):
            if now - self._last.get(g, -math.inf) < WHISPER_GAP_S:
                continue
            affinity = r.affinity if r is not None else 0.0
            attitude = WHISPER_WEIGHT.get(p.romance.stance, 0.0) if _is_woman(p) else 0.0
            w = attitude + max(0.0, affinity) / WHISPER_AFFINITY_SCALE
            if w > 0:
                pool[g] = w
        if not pool or self._rng.random() >= WHISPER_P:
            return None
        g = pick_speakers(pool, 1, self._rng)[0]
        self._last[g] = now
        return g
