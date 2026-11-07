"""The profile-photo prompt: a persona's identity, looks and photo kind as prioritised phrases.

The selfie worker drops the lowest priorities until the prompt fits the tokenizers (priority 0
is always kept). The shape maps one to one onto the worker's render request (`phrases` and
`negative`: lists of [text, priority 0-9]). Adults only, clothed, present-day phone look; no
name is ever put in a phrase.

The lust prompt is built only for a kind the lust rules chose (the caller passes that style,
kind.chosen_style) while she is still of the lust style (style.suggestive), at each render: her
outfit and setting go in as one priority-0 phrase, so the worker's fit to the tokenizers never
keeps one without the other, and the negative lets cleavage and swimwear through and names what
stays out instead. When her looks use a word of the age guard (style.AGE_RE) the render falls
back to the plain prompt, logged by guid only. Every other render gets the phrases and NEGATIVE
as they were. The worker's NSFW gate is the same for both."""
import re
from dataclasses import dataclass

from ..logs import log
from ..personas.schema import MIN_AGE, Persona
from .style import AGE_RE, LUST, PLAIN, suggestive

NEGATIVE: tuple[tuple[str, int], ...] = (
    ("cartoon, anime, painting, illustration, 3d render, cgi", 0),
    ("nsfw, nude, cleavage, lingerie, swimwear", 0),
    ("child, teenager", 0),
    ("celebrity, famous person", 0),
    ("plastic skin, airbrushed, doll, deformed face, extra fingers, bad hands", 1),
    ("vintage, retro, film grain, sepia, old photo", 1),
    ("watermark, text, logo", 2),
)
_COVERED = "nsfw, nude, cleavage, lingerie, swimwear"
_COVERED_LUST = "nsfw, nude, topless, lingerie, underwear, see-through, sheer"
LUST_NEGATIVE: tuple[tuple[str, int], ...] = tuple(
    (_COVERED_LUST, pr) if text == _COVERED else (text, pr) for text, pr in NEGATIVE)

_KINDS = {"selfie": "phone selfie", "mirror_selfie": "mirror selfie",
          "headshot": "professional headshot", "snapshot": "candid photo taken by a friend"}
_FRAMINGS = {"face": "close-up of the face", "head_and_shoulders": "head and shoulders",
             "upper_body": "upper body"}
_LENGTHS = {"short": "short", "chin": "chin-length", "shoulder": "shoulder-length",
            "long": "long", "very_long": "very long"}
_HEADSHOT_TAIL = "recent photo, natural skin texture, unretouched"
_PHONE_TAIL = "recent smartphone photo, natural light, natural skin texture, unretouched"


@dataclass(frozen=True)
class PromptSpec:
    phrases: tuple[tuple[str, int], ...]
    negative: tuple[tuple[str, int], ...]


def renderable(p: Persona | None) -> str | None:
    """None when a prompt can be built, else what is missing. Adults only, here too: the
    schema's MIN_AGE is checked again, not only when the persona was saved."""
    if p is None:
        return "no persona"
    if p.identity.age < MIN_AGE:
        return "under age"
    if p.appearance is None:
        return "no looks yet"
    if p.photo is None:
        return "no photo kind yet"
    return None


def _clean(text: str) -> str:
    text = text.encode("utf-8", "ignore").decode("utf-8")
    return " ".join(text.split())


def build_prompt(p: Persona, *, kind_style: str = PLAIN) -> PromptSpec:
    """The prompt for p's photo kind. `kind_style` is the style whose rules chose the kind
    (kind.chosen_style; PLAIN for a kind set by hand or with no record): the lust prompt only
    for a kind the lust rules chose, while she is of the lust style and her looks code nobody
    as young."""
    missing = renderable(p)
    if missing:
        raise ValueError(missing)
    a, ph, ident = p.appearance, p.photo, p.identity
    lust = kind_style == LUST and suggestive(p)
    if lust and any(AGE_RE.search(_clean(t)) for t in (
            a.hair_colour, a.hair_style, a.skin_tone, a.eye_colour, a.build, *a.face)):
        log("selfies_prompt_plain", guid=p.guid, reason="age")
        lust = False
    names = [n for n in (p.character_name, ident.real_name) if n and n.strip()]
    name_re = re.compile(r"\b(?:" + "|".join(re.escape(n.strip()) for n in names) + r")\b",
                         re.IGNORECASE) if names else None

    out: list[tuple[str, int]] = []

    def add(text: str, priority: int, free: bool = False) -> None:
        text = _clean(text)
        if not text or (free and name_re and name_re.search(text)):
            return
        out.append((text, priority))

    headshot = ph.kind == "headshot"
    adult = "adult " if ident.age < 25 else ""
    nation = "British" if ident.region == "UK" else "American"
    sex = "woman" if ident.player_gender == "female" else "man"
    add(_KINDS[ph.kind], 0)
    add(_FRAMINGS[ph.framing], 0)
    add(f"{ident.age}-year-old {adult}{nation} {sex}", 0)
    if ph.kind != "snapshot":
        add("looking at the camera", 2)
    if a.hair_length == "shaved":
        add("shaved head", 1)
    else:
        hair = f"{_LENGTHS[a.hair_length]} {_clean(a.hair_colour)} hair"
        style = _clean(a.hair_style)
        add(f"{hair}, {style}" if style else hair, 1, True)
    if _clean(a.skin_tone):
        add(f"{_clean(a.skin_tone)} skin", 1, True)
    if _clean(a.eye_colour):
        add(f"{_clean(a.eye_colour)} eyes", 2, True)
    for detail in a.face:
        add(detail, 2, True)
    add(f"{a.build} build", 3)
    if lust:  # one phrase: the fit keeps both or neither, and priority 0 keeps both
        add(f"wearing {_clean(ph.outfit)}, {_clean(ph.setting)}", 0, True)
    else:
        add(ph.outfit, 2, True)
        add(ph.setting, 2, True)
    add(ph.expression, 3, True)
    add(_HEADSHOT_TAIL if headshot else _PHONE_TAIL, 1)
    return PromptSpec(tuple(out), LUST_NEGATIVE if lust else NEGATIVE)
