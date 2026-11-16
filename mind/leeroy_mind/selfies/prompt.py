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
with the situational photos' prompts (scene.py), as is the body (body_phrases): her size by its BMI
(size_words), then her build as a figure with her bust when the owner set one (build_words), said
right after who she is. In every profile, varied and situational prompt the required set is who
she is, her body, her hair and her skin; fit_required keeps it inside the worker's 75 tokens by
shortening the kind and framing, then her hair's style and her skin's modifiers, then her size to
two terms, then letting the kind or pose and the framing yield. Her body is never shortened
further and her hair's length and colour and her skin tone are never dropped (controller rulings,
2026-11-15, measured on the GPU). The body words are clothed; the negatives are unchanged."""
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
             "three_quarter": "three-quarter shot from mid-thigh up, waist and hips in frame",  # the waist in frame, so the figure shows
             "full_body": "full-body shot"}  # a varied set's candidate only (no photo kind has it)
# Their short forms, fit_required's first step (2026-11-15)
SHORT = {"candid photo taken by a friend": "candid photo", "professional headshot": "headshot",
         "waist-up shot, upper body": "waist-up shot",
         "three-quarter shot from mid-thigh up, waist and hips in frame":
             "three-quarter shot, waist and hips in frame"}
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
# Her bust, waist and hips as set in the Looks panel (owner, 2026-11-15): clothed fashion and
# body-shape words, each said in place of the build's own (_BUILD_PARTS) or after the build,
# women only. Never cleavage or a word of the negatives.
BUST_WORDS = {"small": "small bust", "medium": "medium bust", "large": "large full bust",
              "very_large": "very large full bust"}
WAIST_WORDS = {"very_narrow": "very narrow waist", "narrow": "narrow waist",
               "average": "average waist", "wide": "thick waist"}
HIPS_WORDS = {"narrow": "narrow hips", "average": "average hips", "wide": "wide hips",
              "very_wide": "very wide hips"}
PART_WORDS = {"bust": BUST_WORDS, "waist": WAIST_WORDS, "hips": HIPS_WORDS}  # in this order
_BUILD_PARTS = {"curvy": {"bust": "full bust", "hips": "wide hips", "waist": "narrow waist"},
                "voluptuous": {"bust": "large full bust", "hips": "wide hips"}}
# Her glutes, a phrase of their own (owner, 2026-11-15), a clothed fitness and body-shape word:
# required right after her build in a varied candidate seen from behind (her face mostly hidden);
# from the side, where her face shows, optional at priority 2 right after her eye colour and the
# details that tell her apart (controller ruling, 2026-11-15); elsewhere, where the framing shows
# her figure (a front view, three-quarter, full length), the last phrase of priority 2 (review of
# #318); not said in a closer photo.
GLUTES_WORDS = {"small": "slim backside", "medium": "rounded glutes",
                "large": "large round glutes", "very_large": "very large round glutes"}
GLUTES_FRAMINGS = ("three_quarter", "full_body")
GLUTES_VIEWS = ("behind", "rear_three_quarter")  # kind.BACK_VIEWS: required
GLUTES_SIDE = ("side",)  # her face shows: right after who she is, at priority 2
# The pose's short form, fit_required's very last step: still a view from behind
POSE_SHORT = {"from behind, looking over her shoulder": "from behind"}
# The weight said as a body size (owner 2026-11-15: curvy and voluptuous members rendered slim),
# by the body-mass index of weight_kg and height_cm: (below this BMI, a woman's words, a man's).
# The ordinary band says nothing, so the build alone stands there, as before. The heavier bands
# name what a face reference at full strength would otherwise slim down (controller, measured on
# the GPU 2026-11-15): arms and a full round face. Clothed, non-explicit words only, and a
# woman's lowest band is "slender", never "very thin" (a youthful cue). Size words never pull
# against the build (controller rulings, 2026-11-15): a FULL_BUILDS build below 25 and an
# athletic one from 25 to 28 (muscle) say nothing, and from 28 up the size replaces a slim,
# athletic or average build's own words (body_words).
SIZE_WORDS = ((18.5, "slender", "very thin"),
              (22.0, "slender", "lean"),
              (25.0, "", ""),
              (28.0, "slightly chubby", "slightly chubby"),
              (32.0, "chubby, thick arms", "chubby, thick arms"),
              (36.0, "plus-size, heavyset, thick arms, full round face",
               "overweight, heavyset, thick arms, full round face"),
              (float("inf"), "plus-size, heavyset, very full figure, full round face",
               "very overweight, heavyset, full round face"))
FULL_BUILDS = ("curvy", "voluptuous", "stocky", "heavy")  # only the bands above 25 add words
THIN_BUILDS = ("slim", "athletic", "average")  # from BMI 28 the size replaces their words
REPLACES_AT = 28.0


def bmi(a) -> float:
    """The looks' body-mass index: kg over metres squared."""
    return a.weight_kg / (a.height_cm / 100) ** 2


def size_words(p: Persona, short: bool = False) -> str:
    """The body size her weight and height say (SIZE_WORDS), or "": in the ordinary band, below
    it for a full build (FULL_BUILDS: a thin word never contradicts a curvy figure) and from 25
    to 28 for an athletic one (muscle). `short`: its first two terms ("plus-size, heavyset"), the
    least the fit ever keeps (controller ruling, 2026-11-15, measured on the GPU: "plus-size"
    alone rendered her slim)."""
    value, build = bmi(p.appearance), p.appearance.build
    if (value < 25.0 and build in FULL_BUILDS) or (25.0 <= value < 28.0 and build == "athletic"):
        return ""
    woman = p.identity.player_gender == "female"
    words = next(w if woman else m for below, w, m in SIZE_WORDS if value < below)
    return ", ".join(words.split(", ")[:2]) if short else words


def build_words(p: Persona) -> str:
    """The build as a figure (BUILD_WORDS), with her bust, waist and hips (part_words) each said
    in place of the build's own word for it, or after the build when it has none; a man's are
    never said. Never shortened (controller ruling, 2026-11-15: a short "voluptuous figure"
    rendered her slim)."""
    a = p.appearance
    words = BUILD_WORDS.get(a.build, f"{a.build} build")
    if a.build == "curvy" and a.waist == "wide" and p.identity.player_gender == "female":
        words = words.replace("curvy hourglass figure", "curvy figure", 1)  # no hourglass
    own, extra = _BUILD_PARTS.get(a.build, {}), []
    for name, said in part_words(p):
        if name in own:
            words = words.replace(own[name], said, 1)
        else:
            extra.append(said)
    return ", ".join([words, *extra])


def part_words(p: Persona) -> list[tuple[str, str]]:
    """Her bust, waist and hips as the owner set them, (part, words) in that order; none for a
    man or a part not set."""
    a = p.appearance
    if p.identity.player_gender != "female":
        return []
    return [(name, words[getattr(a, name)]) for name, words in PART_WORDS.items()
            if getattr(a, name) is not None]


def bust_said(p: Persona) -> bool:
    """She has a bust, waist or hips set that the prompt says."""
    return bool(part_words(p))


def glutes_words(p: Persona) -> str:
    """Her glutes as the owner set them (GLUTES_WORDS), or "" (not set, or a man)."""
    g = p.appearance.glutes
    return GLUTES_WORDS[g] if g is not None and p.identity.player_gender == "female" else ""


def body_words(p: Persona, short: bool = False) -> list[str]:
    """Her body, in order: her size (size_words), then her build with her bust, waist and hips
    (build_words). From BMI 28 a slim, athletic or average build's words are left out (the size
    says what she is; her bust, waist and hips, when set, stay as phrases of their own). `short`:
    the size in its two-term form, one to one with the long; the build and the parts never
    shorten. Her glutes are a phrase apart (glutes_words)."""
    size = size_words(p, short)
    if size and bmi(p.appearance) >= REPLACES_AT and p.appearance.build in THIN_BUILDS:
        return [size] + [said for _, said in part_words(p)]
    return ([size] if size else []) + [build_words(p)]


def body_phrases(p: Persona) -> tuple[list[tuple[str, int]], tuple[str, int] | None]:
    """Her body, said right after who she is (controller, 2026-11-15: later, the text encoders
    and her face reference outweighed it), each phrase at priority 0 (body_words). A plain
    average build with no bust and no size word is not chosen: it is returned apart, at
    priority 1, for its old place after her looks."""
    if p.appearance.build != "average" or bust_said(p) or size_words(p):
        return [(t, 0) for t in body_words(p)], None
    return [], (build_words(p), 1)


FIT_MAX = 75  # the worker's limit on the required phrases (selfie/leeroy_selfie/fit.py)


def fit_required(out: list[tuple[str, int]], p: Persona, yields: list[str],
                 name_re: re.Pattern | None = None, last: tuple[str, ...] = (),
                 pose: str | None = None) -> list[tuple[str, int]]:
    """`out` made to fit the worker (controller ruling, 2026-11-15, measured on the GPU): while
    the required (priority-0) set counts more than FIT_MAX by the mind's own count
    (tokens.estimate, never below the real one), one step at a time: the phrases of `yields`
    (the kind, then the framing) take their short forms (SHORT); her hair keeps its length and
    colour and drops its style (hair_skin_short); her skin keeps its tone word; her size keeps
    its first two terms; then the phrases of `yields` (a varied set's framing; a profile's kind,
    then its framing) move to priority 1, keeping their places; then those of `last` (a varied
    set's glutes); then a varied set's `pose` takes its short form (POSE_SHORT: "from behind",
    still from behind). Her body (the build and a set bust whole, at least two terms of the
    size), who she is, her hair's length and colour, her skin tone, a varied set's pose and an
    outfit with its setting never go: a prompt still too long after all that is left as it is
    (over_fit names it) for the worker to refuse."""
    from .tokens import joined
    out = list(out)
    steps = [(t, SHORT[t]) for t in yields if t in SHORT]
    steps += hair_skin_short(p, name_re)
    steps += [x for x in zip(body_words(p), body_words(p, short=True)) if x[0] != x[1]]
    steps += [(SHORT.get(t, t), None) for t in yields] + [(t, None) for t in yields]
    steps += [(t, None) for t in last if t]
    steps += [(pose, POSE_SHORT[pose])] if pose in POSE_SHORT else []
    for old, new in steps:
        if joined(t for t, pr in out if pr == 0) <= FIT_MAX:
            break
        if old == new or (old, 0) not in out:
            continue
        at = out.index((old, 0))
        out[at] = (new, 0) if new is not None else (old, 1)
    return out


def over_fit(spec: "PromptSpec") -> bool:
    """The required set still counts more than the worker's limit by the mind's count."""
    from .tokens import joined
    return joined(t for t, pr in spec.phrases if pr == 0) > FIT_MAX


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


def hair_skin_short(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, str]]:
    """(long, short) for her hair and her skin as hair_skin says them, for fit_required: the
    hair's length and colour without its style, the skin's tone without its modifiers
    (skin_tone_word). A phrase with no shorter form, or one the name filter left out, has no
    pair."""
    a, pairs = p.appearance, []
    kept = {t for t, _ in hair_skin(p, name_re)}
    if a.hair_length != "shaved":
        hair = f"{_LENGTHS[a.hair_length]} {_clean(a.hair_colour)} hair"
        style = _clean(a.hair_style)
        if style and f"{hair}, {style}" in kept:
            pairs.append((f"{hair}, {style}", hair))
    tone = _clean(a.skin_tone)
    short = skin_tone_word(tone)
    if short != tone and f"{tone} skin" in kept:
        pairs.append((f"{tone} skin", f"{short} skin"))
    return pairs


_CLAUSE = re.compile(r",|\s+with\s+", re.IGNORECASE)


def skin_tone_word(tone: str) -> str:
    """Her skin's tone without its modifiers: the first clause, before the first "," or " with "
    ("fair with faint freckles" -> "fair", "pale, with rosy cheeks" -> "pale", "light olive with
    a few freckles" -> "light olive"), where the tone comes in the looks; with no such separator
    its last word ("warm brown" -> "brown")."""
    first = _CLAUSE.split(tone, maxsplit=1)[0].strip()
    if first and first != tone:
        return first
    return tone.split(" ")[-1] if tone else tone


def eyes_face(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """Eyes and face details from the looks, at priority 2."""
    a, out = p.appearance, []
    add = phrase_adder(out, name_re)
    if _clean(a.eye_colour):
        add(f"{_clean(a.eye_colour)} eyes", 2, True)
    for detail in a.face:
        add(detail, 2, True)
    return out


# Face details that tell her apart, which the fit offers room first among her details, after her
# eye colour: glasses, facial hair, scars, tattoos and piercings (review of #316, round 3: with her
# hair, skin and body required, glasses and stubble lost their place to expressions and posture).
IDENTITY_DETAIL = re.compile(r"\b(glasses|spectacles|stubble|beard|moustache|mustache|goatee|"
                             r"sideburns|scars?|tattoos?|piercings?)\b", re.IGNORECASE)


def identity_first(p: Persona, details: list[tuple[str, int]],
                   then: tuple[str, int] | None = None) -> list[tuple[str, int]]:
    """`details` (eyes_face) in the order the fit should offer them room: her eye colour, then the
    details that tell her apart (IDENTITY_DETAIL), then `then` when given, then the rest, each
    group in its own order."""
    eye = f"{_clean(p.appearance.eye_colour)} eyes"
    first = [d for d in details if d[0] == eye]
    rest = [d for d in details if d[0] != eye]
    return (first + [d for d in rest if IDENTITY_DETAIL.search(d[0])] + ([then] if then else [])
            + [d for d in rest if not IDENTITY_DETAIL.search(d[0])])


def looks_phrases(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """The face as the looks describe it: hair and skin (1), then eyes and face details (2).
    Free text holding a name in name_re is left out."""
    return hair_skin(p, name_re) + eyes_face(p, name_re)


# A varied set's candidates (varied.py, owner 2026-11-14): each its own look and pose, said in
# short plain photography terms. The required (priority-0) set is the pose, the framing, who she
# is, her body (body_phrases: her size, then her build with her bust), her hair, her skin and the
# outfit with its setting (controller ruling, 2026-11-15: her hair and skin are never traded for
# body words). When it would count more than the worker's 75 (a curvy or voluptuous build with
# long looks can, measured on the real tokenizers), fit_required shortens her hair's style, her
# skin's modifiers and her size to two terms, then yields the framing and then her glutes; her
# build and bust stay whole and the pose is never yielded (a back view without it renders from
# the front). The thong and g-string negatives join the lust ones.
VARIED_VIEW_WORDS = {"behind": "from behind, looking over her shoulder",
                     "rear_three_quarter": "three-quarter rear view",
                     "side": "side profile",
                     "front": "front view"}
VARIED_FRAMING_WORDS = {"full_body": "full-body shot", "three_quarter": "three-quarter shot"}
VARIED_NEGATIVE: tuple[tuple[str, int], ...] = LUST_NEGATIVE + (("thong, g-string", 0),)


def varied_parts(look, outfit: str) -> dict[str, str]:
    """The phrases a varied look puts in a prompt beside her own (view, framing, the joined
    outfit and setting, expression), by field; `look` is a mapping or has the attributes.
    The screens (kind.screen_lust, varied) read exactly these."""
    get = (look.get if isinstance(look, dict) else lambda k: getattr(look, k))
    return {"view": VARIED_VIEW_WORDS[get("view")], "framing": VARIED_FRAMING_WORDS[get("framing")],
            "setting": _clean(get("setting")), "outfit": _clean(outfit),
            "expression": _clean(get("expression")),
            "wearing": f"wearing {_clean(outfit)}, {_clean(get('setting'))}"}


def build_varied_prompt(p: Persona, look) -> PromptSpec:
    """The prompt for one candidate of a varied set: `look` (varied.Look) in her body, face and
    build. Only for a woman who has the lust style now (style.suggestive: lust, 25 or older,
    looks that code nobody as young); ValueError otherwise, or when a phrase would hold one of
    her names (the caller renders her ordinary set instead)."""
    if p is None or p.appearance is None:
        raise ValueError("no looks yet")
    if p.identity.age < MIN_AGE or not suggestive(p):
        raise ValueError(LUST_HELD)
    parts = varied_parts(look, look.phrase)
    name_re = names_re(p)
    if name_re is not None and any(name_re.search(t) for t in parts.values()):
        raise ValueError(LUST_NAMED)
    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)
    add(parts["view"], 0)
    add(parts["framing"], 0)
    add(*subject_phrase(p))
    body, plain_build = body_phrases(p)
    out += body or [(plain_build[0], 0)]  # right after who she is, always required here
    glutes = glutes_words(p)
    if glutes and look.view in GLUTES_VIEWS:  # required from behind
        out.append((glutes, 0))
    out += [(t, 0) for t, _ in hair_skin(p, name_re)]  # her hair and skin, required
    add(parts["wearing"], 0, True)
    add(parts["expression"], 1, True)
    side = (glutes, 2) if glutes and look.view in GLUTES_SIDE else None
    out += identity_first(p, eyes_face(p, name_re), then=side)  # from the side: after who she is
    add(_PHONE_TAIL, 2)
    if glutes and look.view not in GLUTES_VIEWS + GLUTES_SIDE:  # optional: last of all
        out.append((glutes, 2))
    # the pose is never yielded (controller ruling, 2026-11-15, after the GPU: a back view
    # without its pose phrase renders from the front): the framing yields, then her glutes,
    # and only then, for a look that could not fit at all, the pose takes its short form
    out = fit_required(out, p, [parts["framing"]], name_re, last=(glutes,), pose=parts["view"])
    return PromptSpec(tuple(out), VARIED_NEGATIVE)


def build_prompt(p: Persona, *, kind_style: str = PLAIN, light: str = "") -> PromptSpec:
    """The prompt for p's photo kind. `kind_style` is the style whose rules chose the kind
    (kind.chosen_style; PLAIN for a kind set by hand or with no record): the lust prompt for a
    kind the lust rules chose. ValueError when renderable says why not."""
    missing = renderable(p, kind_style)
    if missing:
        raise ValueError(missing)
    ph = p.photo
    from .photo_extra import LIGHTS, fits
    light = light if light in LIGHTS and fits(LIGHTS.index(light), ph) else ""
    lust = kind_style == LUST  # renderable has checked she still has the lust style
    name_re = names_re(p)

    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)

    headshot = ph.kind == "headshot"
    add(_KINDS[ph.kind], 0)
    add(_FRAMINGS[ph.framing], 0)
    add(*subject_phrase(p))
    # Her body right after who she is, then her hair and skin, all required: a build other than
    # average, a bust set by hand and her size are chosen on purpose (often by the owner), so they
    # come where the text encoders weigh them most (body_phrases), and her hair and skin are who
    # she is (controller ruling, 2026-11-15: never traded for body words).
    body, plain_build = body_phrases(p)
    out += body
    out += [(t, 0) for t, _ in hair_skin(p, name_re)]
    out += identity_first(p, eyes_face(p, name_re))  # before the camera line (review of #316)
    if ph.kind != "snapshot":
        add("looking at the camera", 2)
    if plain_build:
        out.append(plain_build)
    if lust:  # one phrase: the fit keeps both or neither, and priority 0 keeps both
        add(lust_phrase(ph), 0, True)  # renderable has checked no name filter drops it
    else:
        add(ph.outfit, 2, True)
        add(ph.setting, 2, True)
    if glutes_words(p) and ph.framing in GLUTES_FRAMINGS:  # optional, after who she is
        out.append((glutes_words(p), 2))
    add(ph.expression, 3, True)
    tail = _HEADSHOT_TAIL if headshot else _PHONE_TAIL
    if light and light != LIGHTS[0]:
        tail = tail.replace(", natural light", "")
    add(tail, 1)
    if light:
        add(light, 9, True)  # first to go; never displaces identity, outfit or setting
    # the kind and framing, her hair's style, her skin's modifiers and her size's third and
    # fourth terms shorten first; then the kind and the framing yield (fit_required)
    out = fit_required(out, p, [_KINDS[ph.kind], _FRAMINGS[ph.framing]], name_re)
    return PromptSpec(tuple(out), LUST_NEGATIVE if lust else NEGATIVE)
