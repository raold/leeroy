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

The distance is the frame (variety plan, Track B): she and he know each other through the game
and live far apart, so desire reads as longing, frustration she can't act on, specific wants and
teasing that builds on what he said, never a summons or "I'm waiting". Each stage of the
long-distance arc has a passage with variants, one picked per turn, and above HEAT_FROM her
attraction shows in three steps (warm, heated, aching). None of it reproaches him.

Built-in text is non-explicit at every setting. Explicit wording comes only from the owner's
local overlays (mind-data/prompts/romance_<band>.txt) through prompts.load_prompt; a missing
romance_explicit overlay falls back to the suggestive built-in and is warned about once per
data dir."""
import itertools
import logging
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.schema import Persona
from leeroy_mind.prompts import load_prompt
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.budget import DayPhase
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
# The long-distance arc. S1 puts the same tuple in storage/rows.ROMANCE_STAGES; until S8 makes
# this that one, a row still at a stage of the old ladder (before S's migration has run) reads
# its new stage's passage, by S1's own mapping (an invitation is planning).
ARC_STAGES = ("none", "notices", "banter", "whispers", "late_nights", "voice", "picture",
              "confession", "planning", "weekend", "after", "ex")
LEGACY_STAGE = {"vulnerable": "late_nights", "invitation": "planning",
                "acknowledged": "confession"}
STAGE_DEPTH = {"none": 0, "notices": 0, "ex": 0, "banter": 1, "whispers": 1,
               "late_nights": 2, "voice": 2, "picture": 2, "confession": 2, "planning": 2,
               "weekend": 2, "after": 2, "vulnerable": 2, "invitation": 2, "acknowledged": 2}

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
# What the meeting was like is left open, so she never contradicts what he says about it.
DEFER = "If {name} remembers something about the weekend differently, go with {their} version."
# One passage per stage, a variant picked per turn (stage_text), so the same text never opens
# every whisper. Each takes only {name}, {them} and {their} and is at most 40 estimated tokens.
# They say what has happened between them, never that she waits or that he should come.
STAGE_PASSAGES: dict[str, tuple[str, ...]] = {
    "none": ("",),
    "notices": (
        "Lately you've caught yourself noticing {name} more than you let on.",
        "You've started reading {name}'s lines in guild chat a little more closely than anyone "
        "else's.",
        "You perk up a little when {name} logs on, and you haven't told anyone why.",
    ),
    "banter": (
        "You and {name} have a running back-and-forth going in guild chat, and you like showing "
        "off for {them}.",
        "You and {name} tease each other in front of everyone, and you both play to the "
        "audience.",
        "Flirting with {name} in guild chat has become a habit, and you play it up when the "
        "guild is watching.",
    ),
    "whispers": (
        "You and {name} whisper each other privately now and then, things you'd never type in "
        "guild.",
        "Your whispers with {name} have their own tone, looser and more honest than guild chat.",
        "A lot of what you say to {name} goes by whisper now, where nobody else is reading.",
    ),
    "late_nights": (
        "You and {name} have started staying up after raids, talking about life outside the "
        "game.",
        "You've told {name} things you don't tell the guild, on late nights after the raid ends.",
        "Your late talks with {name} have gone past the game: work, home, what keeps you up at "
        "night.",
    ),
    "voice": (
        "You love hearing {name}'s voice on Vent late at night, just the two of you.",
        "You've gotten a little hooked on {name}'s voice on Vent, and you hear it when you read "
        "{their} lines.",
        "A quiet Vent channel late at night with {name} has become your favourite place to be.",
    ),
    "picture": (
        "You sent {name} a picture of yourself not long ago, just for {them}, and you're still a "
        "little giddy about it.",
        "Now {name} knows what you look like: you sent {them} a picture, and you liked sending "
        "it.",
    ),
    "confession": (
        "You've told {name} what this is for you, more than a guild flirt, and you meant every "
        "word.",
        "It's out now: {name} knows you have real feelings for {them}, and saying it was a "
        "relief.",
        "You've stopped pretending with {name}: this is real for you, and you've said so.",
    ),
    "planning": (
        "Meeting {name} for real has stopped being a joke to you: the money, the time off, the "
        "nerves.",
        "You keep daydreaming about meeting {name} in person: what you'd wear, what you'd say "
        "first.",
        "A real trip to meet {name} is on your mind, half excited and half nervous, full of "
        "what-ifs.",
    ),
    "weekend": (
        f"You're just back from meeting {{name}} in person and keep replaying it. {DEFER}",
        f"The weekend with {{name}} really happened, and you're still floating. {DEFER}",
    ),
    "after": (
        f"You've met {{name}} in person now, and you miss {{them}} more than before. {DEFER}",
        f"That weekend left you and {{name}} with inside jokes nobody else gets. {DEFER}",
        f"You and {{name}} are together now, and the guild knows it. {DEFER}",
    ),
    "ex": (
        "You and {name} had something once and it ended. There's history, and no hard feelings.",
        "What you and {name} had is over now. You keep it friendly, with a soft spot you don't "
        "make a fuss about.",
    ),
}
# Whisper scope: these stages' passages are said only in a whisper to him (stage_text's
# `private`). On every other channel she reads one of these, which keeps them there.
PRIVATE_GUARD = ("What you share in whispers stays there: in public it shows only in how you "
                 "tease {them}.")
PUBLIC_PASSAGES: dict[str, tuple[str, ...]] = {
    "late_nights": (
        f"You and {{name}} have gotten easy with each other. {PRIVATE_GUARD}",
        f"You and {{name}} have a lot going on these days. {PRIVATE_GUARD}",
    ),
    "picture": (
        f"There's a new spark in how you flirt with {{name}}. {PRIVATE_GUARD}",
        f"You've got a little grin saved up for {{name}} lately. {PRIVATE_GUARD}",
    ),
    "confession": (
        f"{{name}} matters to you more than you'd say in public. {PRIVATE_GUARD}",
        f"You and {{name}} are closer than you let on. {PRIVATE_GUARD}",
    ),
}
# Drawn and lust women start at "notices" (seed_relationships) but are past just noticing him.
INTO_HIM_ALREADY = "You've had your eye on {name} for a while."
# The distance is the frame (spec call 3: it comes after his overlay), at most 45 estimated
# tokens each. The planning stage's frame names the trip as the real meeting and leaves where
# and when to the plan. DISTANCE takes {them} and {they}, DISTANCE_PLAN {them}.
DISTANCE = ("You two live far apart and meet only in game: don't ask {them} to come over, don't "
            "say you're waiting. React to what {they} just said or to what's going on: want, "
            "longing, a tease.")
DISTANCE_PLAN = ("You two live far apart and meet only in game. A trip is the real meeting: "
                 "don't ask {them} to come over, don't say you're waiting or invent where or when. "
                 "Bring want, longing.")
# What you two did lately, for a whisper to him only (romance_instruction's `scene`, a few
# notes, newest first). Second person; {they}/{them}/{their} are his forms. Keys are
# his_<move> and her_<move> for A1's move names; a summons has no note, so it is never said back.
SCENE_HEAD = "Between you two lately:"
SCENE_MAX = 3
NOTE_TEXT: dict[str, str] = {
    "his_question": "{they} asked about you",
    "his_declare": "{they} said {they} wanted you",
    "his_compliment": "{they} complimented you",
    "his_gift": "{they} sent you something",
    "his_petname": "{they} gave you a pet name",
    "her_declare": "you told {them} how you feel",
    "her_tease": "you teased {them}",
    "her_question": "you asked {them} something personal",
    "her_callback": "you brought back something {they} said before",
    "her_compliment": "you complimented {them}",
    "her_plan": "you talked about meeting someday",
}
# Longing that has gone quiet, wistful or a little hurt, never nagging; time offline is never
# held against him. For a whisper to him only.
COOLING_TEXT: tuple[str, ...] = (
    "Things between you and {name} have gone a little quiet lately; it makes you wistful, "
    "and you keep it light.",
    "The spark with {name} has cooled a bit, and it stings softly; you're easy about it and "
    "glad for any line from {them}.",
    "You miss how easy it was with {name}, and you don't make a thing of it.",
)
# A scan over every built-in romance string keeps reproach and summons out of them.
REPROACH_RE = re.compile(
    r"\b(you never|you always|where (were|are) you|why (didn'?t|don'?t|won'?t) you|you forgot"
    r"|ignor(e|es|ing) me|you don'?t care|left me|stood me up|i'?m (still )?waiting"
    r"|waiting for you|come over|get over here|hurry( up)?|find me)\b", re.IGNORECASE)
# Her attraction inside the top band (60 to 100 used to read the same): longing, never reproach.
HEAT_FROM = 60
HEAT_HEATED, HEAT_ACHING = 75, 90
HEAT_TEXT: dict[str, tuple[str, ...]] = {
    "warm": (
        "{name} has been on your mind a lot lately, and it shows in how you answer {them}.",
        "You light up a little whenever {name} types something, and you don't mind if it shows.",
    ),
    "heated": (
        "It's getting harder to keep it light with {name}, and you've stopped trying very hard.",
        "Wanting {name} from this far away is its own kind of frustrating, and it makes you "
        "bolder.",
    ),
    "aching": (
        "You're aching for {name} now: every line from {them} lands hard, and the miles between "
        "you feel long.",
        "You miss {name} like it's a physical thing, and you'd give a lot just to be in the same "
        "room as {them}.",
    ),
}
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

VARIANT_SEP = "---"  # a line of just this splits an overlay into variants
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
                 data_dir: Path, forms: tuple[str, str, str],
                 rng: random.Random | None = None) -> str:
    """The owner's whisper overlay (mind-data/prompts/romance_explicit_whisper.txt; one of
    its variants, the first without `rng`), capped like the band overlays, then the stage's
    distance frame filled with his `forms` (HumanIdentity.forms), for a whisper to him at the
    explicit band; "" otherwise or when there is no overlay (the band as before). The pipeline
    puts it after the turn's instruction; romance_instruction then leaves the frame out
    (`tail_follows`), so it is said once, after his overlay."""
    if not explicit_whisper(p, rel, dial=dial, channel=channel):
        return ""
    variant = _pick(overlay_variants(WHISPER_OVERLAY, data_dir), rng)
    if not variant:
        return ""
    they, them, their = forms
    stage = rel.romance_stage if rel is not None else "none"
    return f"{variant} {distance_text(stage).format(they=they, them=them, their=their)}"


def passage_key(stage: str) -> str:
    """The arc stage whose passage a stored stage reads: an old ladder's name maps to its new
    one (LEGACY_STAGE), any other is itself."""
    return LEGACY_STAGE.get(stage, stage)


def stage_text(stance: str, stage: str, rng: random.Random | None = None, *,
               private: bool = False) -> str:
    """The stage's passage for this attitude, one of its variants (the first without `rng`), a
    template taking {name}, {them} and {their}; "" for none or an unknown stage. `private`: a
    whisper to him. Without it a stage whose passage is for whispers only (late nights, the
    picture, the confession) reads its PUBLIC_PASSAGES one."""
    if stage == "notices" and stance in ("drawn", "lust"):
        return INTO_HIM_ALREADY
    key = passage_key(stage)
    variants = (PUBLIC_PASSAGES[key] if not private and key in PUBLIC_PASSAGES
                else STAGE_PASSAGES.get(key, ("",)))
    return _pick(variants, rng) or ""


def distance_text(stage: str) -> str:
    """The distance frame for this stage (a template taking {them} and {they}): the planning
    stage's frame names the trip as the one real meeting."""
    return DISTANCE_PLAN if passage_key(stage) == "planning" else DISTANCE


def heat_step(attraction: float) -> str | None:
    """How her attraction shows inside the top band: None under HEAT_FROM, then warm, heated
    and aching."""
    if attraction < HEAT_FROM:
        return None
    if attraction < HEAT_HEATED:
        return "warm"
    return "heated" if attraction < HEAT_ACHING else "aching"


def heat_text(stance: str, stage: str, rel: Relationship | None,
              rng: random.Random | None = None) -> str:
    """Her heat step's line (a variant), or "" under the band, without a row, for a stance
    other than drawn or lust (a fond woman's warmth is her attitude's), or for an ex: an ex is
    history, so a high score never reads as pining there."""
    step = heat_step(rel.attraction) if rel is not None else None
    if step is None or stance not in ("drawn", "lust") or passage_key(stage) == "ex":
        return ""
    return _pick(HEAT_TEXT[step], rng) or ""


def scene_notes(her: frozenset[str], his: frozenset[str]) -> list[tuple[str, str]]:
    """(kind, note) for the moves of the lately line: his moves, then hers, in NOTE_TEXT's
    order; a move without a note (a summons) gives none. The note is a template over
    {they}/{them}/{their}."""
    return [(k, t) for k, t in NOTE_TEXT.items()
            if (k[4:] in his if k.startswith("his_") else k[4:] in her)]


def rival_text(rival: str, forms: tuple[str, str, str]) -> str:
    """The rival line (another guild member, a woman): it stings and aims at her, never at him.
    A template over {name}; the rival's own name is escaped for format()."""
    her_name = rival.replace("{", "{{").replace("}", "}}")
    return (f"{her_name} has been getting closer to {{name}}, and it stings: you'd like to "
            f"outshine her. It's her you're up against, never {forms[1]}.")


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


def split_variants(text: str) -> tuple[str, ...]:
    """An overlay's text split on the lines that are exactly `---`; blank blocks dropped."""
    blocks, cur = [], []
    for line in text.splitlines():
        if line.strip() == VARIANT_SEP:
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    blocks.append("\n".join(cur))
    return tuple(b.strip() for b in blocks if b.strip())


def overlay_variants(name: str, data_dir: Path) -> tuple[str, ...]:
    """mind-data/prompts/<name>.txt as its variants, each capped on its own; () when there is
    no file or it is blank. One block reads as the whole overlay always did."""
    text = load_prompt(name, data_dir, "")
    return tuple(_capped(v, name, data_dir) for v in split_variants(text)) if text else ()


def _pick(variants: tuple[str, ...], rng: random.Random | None) -> str | None:
    if not variants:
        return None
    return variants[0] if rng is None or len(variants) == 1 else rng.choice(variants)


def _band_text(b: Band, data_dir: Path, rng: random.Random | None = None) -> str:
    if b != "explicit":
        return _pick(overlay_variants(f"romance_{b}", data_dir), rng) or BUILTIN_ROMANCE[b]
    text = _pick(overlay_variants("romance_explicit", data_dir), rng)
    if text:
        return text
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


def _fill(template: str, fill: dict[str, str]) -> str:
    """A stored note or arc line with only its pronoun and name slots filled, so a brace in
    someone's words never breaks the format."""
    for k, v in fill.items():
        template = template.replace("{" + k + "}", v)
    return template


def _slots(scene: Sequence[str], arc_lines: Sequence[str], rival: str, cooling: bool,
           fill: dict[str, str], human: HumanIdentity, rng: random.Random | None) -> list[str]:
    out = []
    notes = [_fill(n, fill) for n in scene if n][:SCENE_MAX]
    if notes:
        out.append(f"{SCENE_HEAD} {'; '.join(notes)}.")
    out += [_fill(a, fill) for a in arc_lines if a]
    if rival:
        out.append(rival_text(rival, human.forms).format(**fill))
    if cooling:
        out.append(_pick(COOLING_TEXT, rng).format(**fill))
    return out


def romance_instruction(p: Persona, rel: Relationship | None, *, dial: int, channel: str,
                        addressee_is_human: bool, phase: DayPhase, human: HumanIdentity,
                        data_dir: Path, rng: random.Random | None = None,
                        tail_follows: bool = False, scene: Sequence[str] = (),
                        arc_lines: Sequence[str] = (), rival: str = "",
                        cooling: bool = False) -> str | None:
    """The romance text for a turn by `p` (rel is p's relationship to the human), or None
    when p is a man, has no attitude toward him or is not talking to him. The human is named
    by human.called with his pronouns from human.forms; his real name never appears here. In
    order: her attitude, her way, the stage's passage (a whisper-only stage's public one on any
    channel but a whisper), her heat (rel.attraction, drawn and lust only), the band's wording,
    the distance frame, the frame (INVARIANTS), the channel and the hour, and the hook on
    what's happening last. `rng` picks the variants (the first of each without it);
    `tail_follows`: the whisper tail says the distance frame after his overlay, so it is left
    out here. The pipeline puts it before the turn's own instruction, so the model reads that
    last. After the heat line, for a whisper to him only (whisper scope; any other channel
    leaves them out even if passed): SCENE_HEAD and at most SCENE_MAX `scene` notes, the
    `arc_lines`, the rival's line and a cooling variant. `dial` is the owner's; the band
    follows her own when she has one."""
    stance = p.romance.stance
    if not addressee_is_human or stance not in ATTITUDE_REACH or not _is_woman(p):
        return None
    dial = dial_for(p, dial)
    stage = rel.romance_stage if rel is not None else "none"
    they, them, their = human.forms
    fill = dict(name=human.called, they=they, them=them, their=their)
    audience = _AUDIENCE.get(channel, "everyone around")
    parts = [
        attitude_text(p),
        style_text(p),
        stage_text(stance, stage, rng, private=channel == "whisper"),
        heat_text(stance, stage, rel, rng),
    ]
    rest = [
        "" if tail_follows else distance_text(stage),
        INVARIANTS,
        ("It's a private whisper, just the two of you." if channel == "whisper"
         else "The people in the room can hear you, so play to the room."
         if channel.startswith("voice:")
         else f"The {audience} can read it, so play to the room."),
        "It's late, and late-night talk runs warmer." if phase in _LATE else "",
        SPECIFIC,
    ]
    head = " ".join(x.format(**fill) for x in parts if x)
    if channel == "whisper":
        head = " ".join(x for x in (head, *_slots(scene, arc_lines, rival, cooling, fill, human,
                                                   rng)) if x)
    tail = " ".join(x.format(**fill) for x in rest if x)
    return f"{head} {_band_text(effective_band(stance, stage, dial), data_dir, rng)} {tail}"


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
            attitude = (WHISPER_WEIGHT.get(p.romance.stance, 0.0) * forward(p) if _is_woman(p)
                        else 0.0)
            w = attitude + max(0.0, affinity) / WHISPER_AFFINITY_SCALE
            if w > 0:
                pool[g] = w
        if not pool or self._rng.random() >= WHISPER_P:
            return None
        g = pick_speakers(pool, 1, self._rng)[0]
        self._last[g] = now
        return g
