"""The profile-photo prompt: a persona's identity, looks and photo kind as prioritised phrases.

The selfie worker drops the lowest priorities until the prompt fits the tokenizers (priority 0
is always kept). The shape maps one to one onto the worker's render request (`phrases` and
`negative`: lists of [text, priority 0-9]). Adults only, clothed, present-day phone look; no
name is ever put in a phrase.

The lust prompt is built for a kind the lust rules chose (the caller passes that style,
kind.chosen_style), and such a kind renders only while she still has the lust style
(style.suggestive: lust, 25 or more, looks that code nobody as young); otherwise renderable
holds it until the photo pass has chosen her kind again, so its text is never rendered plainly.
Her outfit and setting go in as one priority-0 phrase, so the worker's fit to the tokenizers
never keeps one without the other, and the negative lets cleavage and swimwear through and names
what stays out instead. Every other render gets the phrases and NEGATIVE as they were. The
worker's NSFW gate is the same for both.

The subject (subject_phrase) and the face (looks_phrases: hair_skin, then eyes_face) are shared
with the situational photos' prompts (scene.py)."""
import re
from dataclasses import dataclass

from ..personas.schema import MIN_AGE, Persona
from .style import LUST, PLAIN, suggestive

LUST_HELD = "lust kind awaits a re-choice"
LUST_NAMED = "lust kind shares her name"  # her name in its joined phrase: held, chosen again
HELD = (LUST_HELD, LUST_NAMED)

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
             "upper_body": "waist-up shot, upper body",
             # face, bust, waist and hips in frame (owner, 2026-11-08)
             "three_quarter": "three-quarter shot from mid-thigh up, waist and hips in frame"}  # the waist in frame, so the figure shows
_LENGTHS = {"short": "short", "chin": "chin-length", "shoulder": "shoulder-length",
            "long": "long", "very_long": "very long"}
_HEADSHOT_TAIL = "recent photo, natural skin texture, unretouched"
_PHONE_TAIL = "recent smartphone photo, natural light, natural skin texture, unretouched"


@dataclass(frozen=True)
class PromptSpec:
    phrases: tuple[tuple[str, int], ...]
    negative: tuple[tuple[str, int], ...]


def renderable(p: Persona | None, kind_style: str = PLAIN) -> str | None:
    """None when a prompt can be built, else what is missing. Adults only, here too: the
    schema's MIN_AGE is checked again, not only when the persona was saved. `kind_style` is the
    style whose rules chose the kind (kind.chosen_style): a kind the lust rules composed waits
    (LUST_HELD) while she does not have the lust style, and (LUST_NAMED) while her name filter
    would drop its joined phrase, until the pass chooses again."""
    if p is None:
        return "no persona"
    if p.identity.age < MIN_AGE:
        return "under age"
    if p.appearance is None:
        return "no looks yet"
    if p.photo is None:
        return "no photo kind yet"
    if kind_style == LUST and not suggestive(p):
        return LUST_HELD
    if kind_style == LUST and lust_named(p):
        return LUST_NAMED
    return None


def lust_phrase(photo) -> str:
    """The one priority-0 phrase a lust kind renders as."""
    return f"wearing {_clean(photo.outfit)}, {_clean(photo.setting)}"


def lust_named(p: Persona) -> bool:
    """Her name filter would drop her lust kind's joined phrase (a rename since it was composed,
    or a name such as Wearing), so it must not render without it."""
    named = names_re(p)
    return p.photo is not None and named is not None and named.search(lust_phrase(p.photo)) \
        is not None


def names_re(p: Persona) -> re.Pattern | None:
    """Her character and real names, whole and in any case: no phrase holding one reaches a
    prompt (build_prompt drops it), and no lust kind is composed with one (kind.screen_lust)."""
    names = [n for n in (p.character_name, p.identity.real_name) if n and n.strip()]
    return re.compile(r"\b(?:" + "|".join(re.escape(n.strip()) for n in names) + r")\b",
                      re.IGNORECASE) if names else None


def _clean(text: str) -> str:
    text = text.encode("utf-8", "ignore").decode("utf-8")
    return " ".join(text.split())


# The build said as a figure, at priority 1, so the model shapes it (owner 2026-11-07: a plain "<build>
# build" at priority 3 left every body the same). Voluptuous is an hourglass fuller than curvy.
BUILD_WORDS = {"slim": "slim figure", "athletic": "athletic toned figure", "average": "average build",
               "curvy": "curvy hourglass figure, full bust, wide hips, narrow waist",
               "voluptuous": "voluptuous full-figured body, large full bust, wide hips, soft curves",
               "stocky": "stocky sturdy build", "heavy": "plus-size full figure"}


def phrase_adder(out: list[tuple[str, int]], name_re: re.Pattern | None):
    """add(text, priority, free=False): `text` cleaned onto `out`, nothing when it is blank or,
    for free text, when it holds one of the persona's names (name_re)."""
    def add(text: str, priority: int, free: bool = False) -> None:
        text = _clean(text)
        if not text or (free and name_re and name_re.search(text)):
            return
        out.append((text, priority))
    return add


def subject_phrase(p: Persona) -> tuple[str, int]:
    """Who is in the photo, at priority 0: age (called an adult under 25), nation and sex."""
    ident = p.identity
    adult = "adult " if ident.age < 25 else ""
    nation = "British" if ident.region == "UK" else "American"
    sex = "woman" if ident.player_gender == "female" else "man"
    return _clean(f"{ident.age}-year-old {adult}{nation} {sex}"), 0


def hair_skin(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """Hair and skin from the looks, at priority 1."""
    a, out = p.appearance, []
    add = phrase_adder(out, name_re)
    if a.hair_length == "shaved":
        add("shaved head", 1)
    else:
        hair = f"{_LENGTHS[a.hair_length]} {_clean(a.hair_colour)} hair"
        style = _clean(a.hair_style)
        add(f"{hair}, {style}" if style else hair, 1, True)
    if _clean(a.skin_tone):
        add(f"{_clean(a.skin_tone)} skin", 1, True)
    return out


def eyes_face(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """Eyes and face details from the looks, at priority 2."""
    a, out = p.appearance, []
    add = phrase_adder(out, name_re)
    if _clean(a.eye_colour):
        add(f"{_clean(a.eye_colour)} eyes", 2, True)
    for detail in a.face:
        add(detail, 2, True)
    return out


def looks_phrases(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """The face as the looks describe it: hair and skin (1), then eyes and face details (2).
    Free text holding a name in name_re is left out."""
    return hair_skin(p, name_re) + eyes_face(p, name_re)


def build_prompt(p: Persona, *, kind_style: str = PLAIN) -> PromptSpec:
    """The prompt for p's photo kind. `kind_style` is the style whose rules chose the kind
    (kind.chosen_style; PLAIN for a kind set by hand or with no record): the lust prompt for a
    kind the lust rules chose. ValueError when renderable says why not."""
    missing = renderable(p, kind_style)
    if missing:
        raise ValueError(missing)
    a, ph = p.appearance, p.photo
    lust = kind_style == LUST  # renderable has checked she still has the lust style
    name_re = names_re(p)

    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)

    headshot = ph.kind == "headshot"
    add(_KINDS[ph.kind], 0)
    add(_FRAMINGS[ph.framing], 0)
    add(*subject_phrase(p))
    if ph.kind != "snapshot":
        add("looking at the camera", 2)
    chosen = a.build != "average"
    if chosen:
        # A build other than average is chosen on purpose (often by the owner), so it must reach the
        # image: priority 0, never cut by the 75-token fit (the longest required set is about 55),
        # and right after who she is, where the text encoders weigh it more than at the end.
        add(BUILD_WORDS.get(a.build, f"{a.build} build"), 0)
    out += looks_phrases(p, name_re)
    if not chosen:
        add(BUILD_WORDS.get(a.build, f"{a.build} build"), 1)
    if lust:  # one phrase: the fit keeps both or neither, and priority 0 keeps both
        add(lust_phrase(ph), 0, True)  # renderable has checked no name filter drops it
    else:
        add(ph.outfit, 2, True)
        add(ph.setting, 2, True)
    add(ph.expression, 3, True)
    add(_HEADSHOT_TAIL if headshot else _PHONE_TAIL, 1)
    return PromptSpec(tuple(out), LUST_NEGATIVE if lust else NEGATIVE)
