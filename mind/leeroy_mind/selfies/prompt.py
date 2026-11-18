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
from .safe import POSE_SHORT as POSE_WORDS_SHORT
from .safe import (BODY_ALONE, BODY_ANATOMY, BODY_GARMENT, BODY_NO_MIRROR, BODY_OPTIONAL,
                   HEAD_FRAMINGS, KIND_SHORT, KIND_WORDS, alone, kind_of, outfit_words, pose,
                   pose_priority, selfie_framing, setting_words, wearing)
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
# A profile photo framed head and shoulders or closer: NEGATIVE with the audit's words required
# (men rendered without a shirt; a phone or a mirror in a selfie, left out for a mirror selfie).
# A guild tile keeps NEGATIVE as it was (scene.TILE_NEGATIVE: its one-face line fills the window).
def _head(base, mirror: bool = False):
    line = ("shirtless" if mirror else "shirtless, mirror, phone", 0)
    at = next((i for i, (_, pr) in enumerate(base) if pr > 0), len(base))  # after the required
    return base[:at] + (line,) + base[at:]


HEAD_NEGATIVE: tuple[tuple[str, int], ...] = _head(NEGATIVE)
HEAD_NEGATIVE_MIRROR: tuple[tuple[str, int], ...] = _head(NEGATIVE, mirror=True)
HEAD_NEGATIVE_LUST: tuple[tuple[str, int], ...] = _head(LUST_NEGATIVE)


def body_negative(covered: str, *required: str, lust: bool = False, mirror: bool = False,
                  no_mirror: str = BODY_NO_MIRROR, optional: tuple[tuple[str, int], ...] = ()) -> tuple[tuple[str, int], ...]:
    """The negative of a photo that shows the body (waist-up or wider; every situational one).
    Required: what stays covered (`covered`), minors, likeness, the anatomy line, another or a
    second person, the missing garments, the mirror and the phone unless a mirror is rendered
    (`mirror`; `no_mirror` says them), and `required`. Then safe.BODY_OPTIONAL in its order (the realism words first)
    and `optional`: the worker's 75-token fit keeps what fits and drops the rest
    (test_safe_compositions pins both on the real tokenizers). The owner's "never" words go in
    ahead of the optional ones (negative_for)."""
    return (("cartoon, painting", 0),
            (covered, 0),
            ("child, teenager", 0),
            ("celebrity, famous person", 0),
            (BODY_ANATOMY, 0),
            (BODY_ALONE, 0),
            (BODY_GARMENT, 0),
            *(() if mirror else ((no_mirror, 0),)),
            *((text, 0) for text in required),
            *BODY_OPTIONAL,
            *optional)


BODY_NEGATIVE = body_negative(_COVERED)
BODY_NEGATIVE_MIRROR = body_negative(_COVERED, mirror=True)
BODY_NEGATIVE_LUST = body_negative(_COVERED_LUST, lust=True)

_KINDS = KIND_WORDS  # a selfie names no phone and no mirror (safe.py)
_FRAMINGS = {"face": "close-up of the face", "head_and_shoulders": "head and shoulders",
             "upper_body": "waist-up shot, upper body",
             # face, bust, waist and hips in frame (owner, 2026-11-08)
             "three_quarter": "three-quarter shot from mid-thigh up, waist and hips in frame",  # the waist in frame, so the figure shows
             "full_body": "full-body shot"}  # a varied set's candidate only (no photo kind has it)
# Their short forms, fit_required's first step (2026-11-15)
SHORT = {**KIND_SHORT, **POSE_WORDS_SHORT,
         "candid photo taken by a friend": "candid photo", "professional headshot": "headshot",
         "waist-up shot, upper body": "waist-up shot",
         "three-quarter shot from mid-thigh up, waist and hips in frame":
             "three-quarter shot, waist and hips in frame"}
_LENGTHS = {"short": "short", "chin": "chin-length", "shoulder": "shoulder-length",
            "long": "long", "very_long": "very long", "buzzed": "buzzed"}
# The owner's hair (Looks panel, 2026-11-17; schema owner_hair_*): each value set is said in
# place of the looks' own free text for it, which stays as stored. Plain hairdressing words,
# none of the age guard's (style.AGE_RE: no pigtails, no bunches).
HAIR_COLOUR_WORDS = {
    "black": "black", "dark_brown": "dark brown", "brown": "brown", "light_brown": "light brown",
    "auburn": "auburn", "red": "red", "copper": "copper",
    "strawberry_blonde": "strawberry blonde", "dark_blonde": "dark blonde", "blonde": "blonde",
    "platinum": "platinum blonde", "grey": "grey", "silver": "silver",
    "pastel_pink": "pastel pink", "blue": "blue", "purple": "purple"}
HAIR_STYLE_WORDS = {
    "straight": "straight", "wavy": "wavy", "curly": "curly", "coily": "coily",
    "ponytail": "in a ponytail", "high_bun": "in a high bun", "messy_bun": "in a messy bun",
    "braid": "in a single braid", "braids": "in braids", "bob": "bob cut", "pixie": "pixie cut",
    "bangs": "with bangs", "half_up": "half-up style", "slicked_back": "slicked back",
    "shaved_sides": "shaved sides", "crew_cut": "crew cut", "fade": "fade haircut",
    "quiff": "styled in a quiff"}
# Makeup, jewellery, glasses, tattoos and piercings as the owner set them (schema makeup,
# jewellery, glasses, tattoos, piercings), in the shortest words that say them (one or two
# tokens each, counted on the worker's tokenizers): accessory_phrases, at ACCESSORY priority
# right after her hair and skin, so the worker's fit offers them room before every other
# optional phrase, and fit_required makes that room by shortening (its `reserve`). "never" and
# "none" leave out the face details that name one (face_details) and name it in the negative
# where that has room (style_negative).
MAKEUP_WORDS = {"none": "no makeup", "natural": "natural makeup", "defined": "defined makeup",
                "glam": "glam makeup"}
JEWELLERY_WORDS = {"none": ("", ""), "minimal": ("delicate necklace", "chain necklace"),
                   "statement": ("statement jewellery", "chunky jewellery")}  # a woman's, a man's
GLASSES_WORDS = "glasses"
TATTOO_WORDS = {"none": "", "small": "small tattoo", "sleeve": "tattoo sleeve"}
PIERCING_WORDS = {"none": "", "ears": "pierced ears", "nose": "nose piercing",
                  "lip": "lip piercing"}
ACCESSORY = 1  # their priority: the first optional phrases the worker's fit offers room
_GLASSES = re.compile(r"\b(glasses|spectacles|specs)\b", re.IGNORECASE)
_TATTOO = re.compile(r"\b(tattoo\w*|inked)\b", re.IGNORECASE)
_PIERCING = re.compile(r"\b(pierc\w*|(?:nose|lip|eyebrow) (?:ring|stud)s?|septum)\b",
                       re.IGNORECASE)
# a face detail's clauses ("a faded tattoo and a scar on the chin"): only the clause that names
# the thing goes, the rest of the detail stays
_CLAUSES = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)
# one word each, a phrase apiece, so one never takes another down with it (style_negative)
STYLE_NEGATIVE = {"glasses": "glasses", "tattoos": "tattoo", "piercings": "piercing",
                  "jewellery": "jewellery"}
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
    """The one priority-0 phrase a lust kind renders as: its outfit and setting as the lists say
    them now (safe.outfit_words, safe.setting_words: a kind stored before the audit keeps its
    text and renders with the words that replaced a mirror, a car seat or bike shorts)."""
    return (f"wearing {outfit_words(_clean(photo.outfit))}, "
            f"{setting_words(_clean(photo.setting))}")


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
POSE_SHORT = {"from behind, looking over her shoulder": "from behind", **KIND_SHORT}
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
GIVEN = object()  # fit_required: a phrase that makes way for what the owner set her to wear


def fit_required(out: list[tuple[str, int]], p: Persona, yields: list[str],
                 name_re: re.Pattern | None = None, last: tuple[str, ...] = (),
                 pose: str | None = None, reserve: int = 0,
                 give: tuple[str, ...] = ()) -> list[tuple[str, int]]:
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
    (over_fit names it) for the worker to refuse.

    `reserve`: tokens the required set leaves free for optional phrases that must still show
    (what the owner set her to wear, accessory_phrases): the shortening steps, and only those,
    also run while the required set counts more than FIT_MAX less `reserve`; then the phrases
    of `give` (a profile's or a situation's kind of photo; a varied front or side view's framing)
    move to priority 2, behind what the reserve is for. Nothing else is yielded for it. With no
    reserve the steps are exactly what they were."""
    from .tokens import joined
    out = list(out)
    shorten = [(t, SHORT[t]) for t in yields if t in SHORT]
    shorten += hair_skin_short(p, name_re)
    shorten += [x for x in zip(body_words(p), body_words(p, short=True)) if x[0] != x[1]]
    steps = list(shorten)
    steps += [(SHORT.get(t, t), None) for t in yields] + [(t, None) for t in yields]
    steps += [(t, None) for t in last if t]
    steps += [(pose, POSE_SHORT[pose])] if pose in POSE_SHORT else []
    given = [(x, GIVEN) for t in give for x in dict.fromkeys((SHORT.get(t, t), t))]
    for limit, todo in ((FIT_MAX - reserve, shorten + given if reserve else []), (FIT_MAX, steps)):
        for old, new in todo:
            if joined(t for t, pr in out if pr == 0) <= limit:
                break
            if old == new or (old, 0) not in out:
                continue
            at = out.index((old, 0))
            out[at] = (old, 2) if new is GIVEN else (new, 0) if new is not None else (old, 1)
    return out


def fit_clothed(out: list[tuple[str, int]], p: Persona, yields: list[str],
                name_re: re.Pattern | None, clothes: str | None, framing: str, *,
                reserve: int = 0, give: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """fit_required (with its `reserve` and `give`) for a prompt whose clothing phrase `clothes`
    (safe.wearing: free text of up to safe.WEAR_MAX_WORDS words; None for a list outfit, which
    is fitted as it is) is required: when the required set still counts more than FIT_MAX after
    every step, the outfit gives way to the default garments (a few tokens), and if even those
    do not fit the clothing moves to priority 1, the first phrase the worker's fit keeps. Her
    body, hair and skin are never traded for it, and no long outfit makes the worker refuse the
    photo.

    The one fit order of a profile, varied or situational prompt (2026-11-17): required, and
    kept in this order of importance, who she is, her body and size, her hair's length and
    colour, her skin tone, what she wears, then the framing and a body shot's pose; then what
    the owner set her to wear (accessory_phrases, the first optional phrases, with room
    reserved); then her hair's style, her eye colour and the other details. To make the room:
    the short forms and her hair's style, skin's modifiers and size's last terms go first
    (fit_required); then, for the accessories' reserve, the kind of photo and the pose move
    behind them (`give`); then, only for the worker's limit, the kind, the pose and the framing
    yield in that order (`yields`)."""
    from .tokens import joined
    fitted = fit_required(out, p, yields, name_re, reserve=reserve, give=give)
    if clothes is None or (clothes, 0) not in out:
        return fitted
    at = out.index((clothes, 0))
    default = wearing("", gender=p.identity.player_gender, framing=framing)
    # the default garments also when her own outfit leaves the accessories no room (reserve)
    for phrase, priority, limit in ((default, 0, FIT_MAX - reserve), (default, 1, FIT_MAX)):
        if joined(t for t, pr in fitted if pr == 0) <= limit:
            continue
        fitted = fit_required(out[:at] + [(phrase, priority)] + out[at + 1:], p, yields, name_re,
                              reserve=reserve, give=give)
    return fitted


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


def hair_words(p: Persona) -> tuple[str, str] | None:
    """Her hair as the prompt says it: (its length and colour, its style or ""), each from the
    owner's value when one is set (HAIR_COLOUR_WORDS, HAIR_STYLE_WORDS, the owner's length),
    else from the looks' own text; None for a shaved head (the looks' length, with no owner
    length over it). Looks that say a shaved head have no colour or style of their own worth
    saying ("closely shaved"), so over them only the owner's values count: his length with his
    colour and style (looks_edit refuses a colour or style without a length there, and a length
    without a colour)."""
    a = p.appearance
    length = a.owner_hair_length or a.hair_length
    if length == "shaved":
        return None
    shaved = a.hair_length == "shaved"
    colour = (HAIR_COLOUR_WORDS[a.owner_hair_colour] if a.owner_hair_colour
              else "" if shaved else _clean(a.hair_colour))
    style = (HAIR_STYLE_WORDS[a.owner_hair_style] if a.owner_hair_style
             else "" if shaved else _clean(a.hair_style))
    return _clean(f"{_LENGTHS[length]} {colour} hair"), style


def hair_skin(p: Persona, name_re: re.Pattern | None) -> list[tuple[str, int]]:
    """Hair (hair_words) and skin from the looks, at priority 1."""
    a, out = p.appearance, []
    add = phrase_adder(out, name_re)
    words = hair_words(p)
    if words is None:
        add("shaved head", 1)
    else:
        hair, style = words
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
    words = hair_words(p)
    if words is not None:
        hair, style = words
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
    for detail in face_details(p):
        add(detail, 2, True)
    return out


def _without(detail: str, named: re.Pattern) -> str:
    """`detail` without its clauses that `named` matches ("freckles and a nose ring" ->
    "freckles"); "" when nothing is left."""
    kept = [c for c in _CLAUSES.split(detail) if c.strip() and not named.search(c)]
    if len(kept) == len([c for c in _CLAUSES.split(detail) if c.strip()]):
        return detail
    return " and ".join(kept)


def face_details(p: Persona) -> list[str]:
    """Her face details as the looks give them, less what the owner's glasses, tattoos and
    piercings stand in for: "never" or "none" leaves out the clauses that name one, a tattoo or
    piercing value is said in their place (accessory_phrases), and with glasses "always" the
    details that name glasses move to accessory_phrases, where the fit offers them room first.
    Everything else in a detail stays (a scar beside a tattoo, freckles beside a nose ring).
    Unset, the details as stored."""
    a = p.appearance
    details = list(a.face)
    for value, named in ((a.glasses, _GLASSES), (a.tattoos, _TATTOO), (a.piercings, _PIERCING)):
        if value is None:
            continue
        if named is _GLASSES and value == "always":
            details = [d for d in details if not named.search(d)]
        else:
            details = [d for d in (_without(d, named) for d in details) if d]
    return details


def accessory_phrases(p: Persona, name_re: re.Pattern | None = None) -> list[tuple[str, int]]:
    """What the owner set for her to wear, at ACCESSORY priority, in this order: glasses
    ("always": the looks' own detail that names them, else "glasses"), her tattoo, her
    piercing, her makeup (a woman's) and her jewellery. None when nothing is set."""
    a, out = p.appearance, []
    add = phrase_adder(out, name_re)
    woman = p.identity.player_gender == "female"
    if a.glasses == "always":
        own = [d for d in a.face if _GLASSES.search(d)]
        for d in own or [GLASSES_WORDS]:
            add(d, ACCESSORY, bool(own))
    if a.tattoos is not None:
        add(TATTOO_WORDS[a.tattoos], ACCESSORY)
    if a.piercings is not None:
        add(PIERCING_WORDS[a.piercings], ACCESSORY)
    if a.makeup is not None and woman:
        add(MAKEUP_WORDS[a.makeup], ACCESSORY)
    if a.jewellery is not None:
        add(JEWELLERY_WORDS[a.jewellery][0 if woman else 1], ACCESSORY)
    return out


def reserve(phrases) -> int:
    """The room `phrases` (optional ones the fit must still keep) take beside the required set,
    by the mind's count: their tokens and the commas that join them, the one before them too."""
    from .tokens import joined
    texts = [t for t, _ in phrases]
    return joined(texts) + 1 if texts else 0


def _kept(phrases, limit: int = 75) -> list[int]:
    """The indices the worker's fit keeps of `phrases` (selfie/leeroy_selfie/fit.py, by the
    mind's count): every priority-0 one, then by priority and position while they fit."""
    from .tokens import joined
    keep = {i for i, (_, pr) in enumerate(phrases) if pr == 0}
    for priority in sorted({pr for _, pr in phrases if pr > 0}):
        for i, (_, pr) in enumerate(phrases):
            if pr == priority and joined(phrases[j][0] for j in sorted(keep | {i})) <= limit:
                keep.add(i)
    return sorted(keep)


def style_negative(p: Persona, base: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    """What the owner's "never" and "none" keep out, each a one-word negative phrase of its own
    at priority 3, only where it has room beside what the worker's fit keeps of `base` (the
    prompt's fixed negatives): a word with no room is left out here, never sent to be dropped,
    and none displaces a fixed negative. Glasses first, then tattoos, piercings, jewellery."""
    from .tokens import joined
    a = p.appearance
    wanted = [STYLE_NEGATIVE[name] for name, off in (
        ("glasses", a.glasses == "never"), ("tattoos", a.tattoos == "none"),
        ("piercings", a.piercings == "none"), ("jewellery", a.jewellery == "none")) if off]
    texts = [base[i][0] for i in _kept(base)]
    out = []
    for word in wanted:
        if joined(texts + out + [word]) <= FIT_MAX:
            out.append(word)
    return tuple((w, 3) for w in out)


def negative_for(p: Persona, base: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    """`base` with what the owner's "never" and "none" keep out. A body negative's required set
    leaves little room, so there the words go in at priority 1 ahead of its optional phrases,
    and the worker's fit keeps as many as fit: all four in the plain, lust and situational body
    negatives, the first two (glasses, tattoo) in a varied set's, whose required set counts 71
    (main: none there). Any other negative takes them where they have room, as before
    (style_negative)."""
    if (BODY_ANATOMY, 0) not in base:
        return base + style_negative(p, base)
    a = p.appearance
    words = [STYLE_NEGATIVE[name] for name, off in (
        ("glasses", a.glasses == "never"), ("tattoos", a.tattoos == "none"),
        ("piercings", a.piercings == "none"), ("jewellery", a.jewellery == "none")) if off]
    first = next((i for i, (_, pr) in enumerate(base) if pr > 0), len(base))
    return base[:first] + tuple((w, 1) for w in words) + base[first:]


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
VARIED_FRAMING_WORDS = {"full_body": "full-body shot", "three_quarter": "three-quarter shot",
                        "upper_body": "waist-up shot"}
# a varied candidate is a snapshot (no phone to keep out): "mirror" alone, for the gym's
VARIED_NEGATIVE: tuple[tuple[str, int], ...] = body_negative(
    _COVERED_LUST, "thong, g-string", lust=True, no_mirror="mirror")


def varied_parts(look, outfit: str) -> dict[str, str]:
    """The phrases a varied look puts in a prompt beside her own (view, framing, the joined
    outfit and setting, expression), by field; `look` is a mapping or has the attributes.
    The screens (kind.screen_lust, varied) read exactly these."""
    get = (look.get if isinstance(look, dict) else lambda k: getattr(look, k))
    setting = setting_words(_clean(get("setting")))
    return {"view": VARIED_VIEW_WORDS[get("view")], "framing": VARIED_FRAMING_WORDS[get("framing")],
            "setting": setting, "outfit": _clean(outfit),
            "expression": _clean(get("expression")),
            "wearing": f"wearing {_clean(outfit)}, {setting}",
            "pose": pose(get("view"), get("framing"),
                         f"{get('view')}:{get('setting')}:{outfit}"),
            "alone": alone(setting)}


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
    acc = accessory_phrases(p, name_re)  # what the owner set: first among the optional
    out += acc
    add(parts["wearing"], 0, True)
    add(parts["pose"], pose_priority(look.framing))  # hands simple (safe.POSES)
    add(parts["expression"], 1, True)
    # from the side her glutes are said only where the framing shows them (not waist-up)
    side = (glutes, 2) if (glutes and look.view in GLUTES_SIDE
                           and look.framing in GLUTES_FRAMINGS) else None
    out += identity_first(p, eyes_face(p, name_re), then=side)  # from the side: after who she is
    add(_PHONE_TAIL, 2)
    if glutes and look.view not in GLUTES_VIEWS + GLUTES_SIDE:  # optional: last of all
        out.append((glutes, 2))
    add(parts["alone"], 3)  # the negative requires it already; said here when there is room
    # the pose is never yielded (controller ruling, 2026-11-15, after the GPU: a back view
    # without its pose phrase renders from the front): the framing yields, then her glutes,
    # and only then, for a look that could not fit at all, the pose takes its short form
    # the pose (safe.POSES) is required while it fits; it makes way for what the owner set her
    # to wear (give) and is the first to yield for the worker's limit, the framing after it
    stand = parts["pose"] if pose_priority(look.framing) == 0 else ""
    gives = tuple(t for t in (stand, "" if look.view in GLUTES_VIEWS else parts["framing"]) if t)
    out = fit_required(out, p, [t for t in (stand, parts["framing"]) if t], name_re,
                       last=(glutes,), pose=parts["view"], reserve=reserve(acc), give=gives)
    return PromptSpec(tuple(out), negative_for(p, VARIED_NEGATIVE))


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
    body_shot = ph.framing not in HEAD_FRAMINGS
    # a photo that shows the body says its kind in its short form from the start: the tokens go
    # to what she wears and how she stands (safe.py); its framing shortens only when it must
    shown = kind_of(ph.kind, lust)  # a lust kind's mirror selfie renders as a selfie
    mirror = shown == "mirror_selfie"
    # a lust selfie stored three-quarter renders waist-up (safe.selfie_framing)
    framing = selfie_framing(shown, ph.framing) if lust else ph.framing
    kind_words, framing_words = _KINDS[shown], _FRAMINGS[framing]
    if body_shot:
        kind_words = SHORT.get(kind_words, kind_words)
    add(kind_words, 0)
    add(framing_words, 0)
    add(*subject_phrase(p))
    # Her body right after who she is, then her hair and skin, all required: a build other than
    # average, a bust set by hand and her size are chosen on purpose (often by the owner), so they
    # come where the text encoders weigh them most (body_phrases), and her hair and skin are who
    # she is (controller ruling, 2026-11-15: never traded for body words).
    body, plain_build = body_phrases(p)
    out += body
    out += [(t, 0) for t, _ in hair_skin(p, name_re)]
    acc = accessory_phrases(p, name_re)  # what the owner set: first among the optional
    out += acc
    clothes = None
    out += identity_first(p, eyes_face(p, name_re))  # before the camera line (review of #316)
    if ph.kind != "snapshot":
        add("looking at the camera", 2)
    if plain_build:
        out.append(plain_build)
    if lust:  # one phrase: the fit keeps both or neither, and priority 0 keeps both
        add(lust_phrase(ph), 0, True)  # renderable has checked no name filter drops it
        add(alone(setting_words(_clean(ph.setting))), 3)
    else:
        # Clothed by construction (the audit: at priority 2 the outfit never fitted a waist-up
        # prompt and men rendered shirtless): what he or she wears is required, a top and for a
        # waist-up photo a bottom (safe.complete); an outfit holding one of the persona's names
        # gives way to the default garments.
        outfit = _clean(ph.outfit)
        if name_re is not None and name_re.search(outfit):
            outfit = ""
        clothes = wearing(outfit, gender=p.identity.player_gender, framing=ph.framing)
        # required wherever shoulders show; a close-up of the face keeps it where it was
        add(clothes, 2 if ph.framing == "face" else 0)
        add(ph.setting, 2, True)
    stand = pose("front", ph.framing, p.guid, kind=shown,
                 pos="her" if p.identity.player_gender == "female" else "his")
    add(stand, pose_priority(ph.framing))
    if glutes_words(p) and framing in GLUTES_FRAMINGS:  # optional, after who she is
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
    # the one fit order (fit_clothed): the kind, then a body shot's pose, make way for what the
    # owner set her to wear; for the worker's limit the kind, the pose and the framing yield
    held = stand if pose_priority(ph.framing) == 0 else ""
    out = fit_clothed(out, p, [t for t in (kind_words, held, framing_words) if t], name_re,
                      clothes, ph.framing, reserve=reserve(acc),
                      give=tuple(t for t in (kind_words, held) if t))
    if body_shot:  # hands, arms and a waist in frame: the anatomy and garment lines required
        negative = (BODY_NEGATIVE_LUST if lust else BODY_NEGATIVE_MIRROR if mirror
                    else BODY_NEGATIVE)
    else:
        negative = (HEAD_NEGATIVE_LUST if lust else HEAD_NEGATIVE_MIRROR if mirror
                    else HEAD_NEGATIVE)
    return PromptSpec(tuple(out), negative_for(p, negative))
