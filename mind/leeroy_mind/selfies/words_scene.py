"""A requested photo read from his words (owner 2026-11-16, `[photos] scene_from_words`: "sneak
away from work and shoot me a selfie from the bathroom" should give her in her work clothes, in
the bathroom).

One structured call on the live model (request_scene.choose_words_scene, `format` = SCHEMA,
`think: false`) reads his ask into a scene: `place` (free text, at most PLACE_WORDS words),
`framing` (FRAMINGS), `wearing` (WEARING), `moment` (free text, at most MOMENT_WORDS words),
`time` (TIMES), `lighting` (LIGHTING) and `prop` (free text, at most PROP_WORDS words, or null).

His words drive this free text, so its screens are stricter than the night's (review of #339,
rulings 2026-11-16), and none of them is an allow-list:
- Hard, for the whole reply (age_hit): an age word anywhere in place, moment or prop (style.AGE_RE,
  scene.AGE_MORE_RE, an age under 25, and YOUNG_RE: young, youth, little, tiny, virgin, naive,
  innocent, class or classroom, campus, dorm, homework, a uniform skirt; petite with cute, small
  with girl) or a minor (scene.MINOR_RE, MINOR_MORE_RE) refuses all of it: the request takes the
  enum path's everyday choice without another model call (request_scene, log
  `scene_words_refused`).
- Per field: the text folded (curly apostrophes straight, accents dropped, whitespace single),
  then a charset (letters, spaces, hyphens, apostrophes; commas in the moment only, so place and
  prop are one phrase each; no digits, no prompt syntax), its word limit, the situational photo's
  screens (scene._text_codes: meta, his real name, a likeness, a member's name, an era, explicit
  wear or a sexual pose, a looks change, undressing or bathing, another person), clothing
  (CLOTHING_RE: what she wears is `wearing`'s, never free text), body parts and poses (BODY_RE),
  undressing states (STATE_RE), toilet use (TOILET_RE), other people (PEOPLE_RE), a likeness or
  a style (LIKE_RE), a proper name (a capital after the first word, or a first word that is no
  everyday word), her own names, and for a lust-styled woman a bed or bedroom as her place.
A refused field falls back on its own, never with a second model call: the place to a work place
when his ask speaks of work (WORK_ASK_RE: a staff bathroom or a break room at work), else to the
enum list's place his ask names, else to the everyday enum setting (fallback_place); the moment and
the prop to none. Props that are no nudity or undressing pass (the owner's realism rule: renders
stay clothed). Residual risk, accepted: a celebrity's name made of everyday words, written in
lowercase, passes as text (the negatives keep "celebrity, famous person" and her face reference).
`wearing` is resolved by code, never written by the model: work_uniform from her job
(UNIFORMS, else smart office clothes; none for a stay-at-home or student job), as_now from her
day plan's block when [life] availability is on (else what she wears at the computer), the rest
from the vetted outfit lists (scene_choices.OUTFITS for the plain style, kind.py's lists with a
drawn colour for the lust style). Pyjamas resolve to clothed loungewear from those lists, never
sleepwear; swimwear only for a lust-styled woman, at a water place, in a handheld selfie (framed
waist-up), else casual; gym wear or swimwear beside a bed, sofa or the indoors (scene.INDOOR_RE)
is casual for her too. Every resolved outfit meets her names and the age guard again, and what
she wears at the computer (her persona's own words) the scene screens.

The scene is stored in the request's snap in the situational shape (scene.Scene's fields, so
every existing check reads it) with recipe WORDS_RECIPE and its enum codes under `words`. Its
prompt (build_words_prompt) puts the framing, `wearing <outfit>` and the place at priority 0 beside
who she is, her body, hair and skin, so the worker's fit keeps them; the moment, prop, lighting,
time and the rest of her looks keep their usual priorities. Its negatives, the NSFW gate, the age
guard and the identity reference (her chosen photo's face) are the situational photo's; a photo
from behind takes the face at render.REAR_SCALE, as a varied back view does.

No logging, and no I/O but the owner's optional overlay of the call's prompt
(prompts/nightly_scene_words.txt, read by system). Only codes leave here for the logs."""
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.human import HumanIdentity
from leeroy_mind.life.day import Block, JobShape, job_shape
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import has_word
from leeroy_mind.talk.everyday import is_everyday

from .kind import LUST_COLOURS, VARIED_LOOKS, VARIED_OUTFITS, _enum, outfit_phrase
from .prompt import (_PHONE_TAIL, PromptSpec, _clean, body_phrases, eyes_face, fit_required,
                     glutes_words, hair_skin, identity_first, names_re, phrase_adder,
                     subject_phrase)
from .scene import (_NOBODY, AGE_MORE_RE, INDOOR_RE, MINOR_MORE_RE, MINOR_RE, SNAP_NEGATIVE,
                    SNAP_NEGATIVE_LUST, _text_codes, _young_number, limb_detail, scene_from,
                    scene_renderable)
from .scene_choices import OUTFITS, PLACES, PLAIN_PLACES
from .style import AGE_RE, LUST, style_of

WORDS_NAME = "scene_words"  # its overlay: prompts/nightly_scene_words.txt
WORDS_RECIPE = 2  # a scene read from his words (the enum scenes are scene_choices.RECIPE)

FRAMINGS = ("mirror_selfie", "handheld_selfie", "timer", "from_behind_over_shoulder")
BEHIND = "from_behind_over_shoulder"
WEARING = ("work_uniform", "gym", "casual", "going_out", "swimwear", "pyjamas", "as_now")
TIMES = ("now", "morning", "evening", "night")
LIGHTING = ("fluorescent", "daylight", "lamp", "neon", "phone_flash")
PLACE_WORDS = MOMENT_WORDS = 8
PROP_WORDS = 4
LIMITS = {"place": PLACE_WORDS, "moment": MOMENT_WORDS, "prop": PROP_WORDS}

SCHEMA = schema(WORDS_NAME, {
    "place": {"type": "string", "maxLength": 80},
    "framing": _enum(FRAMINGS), "wearing": _enum(WEARING),
    "moment": {"type": "string", "maxLength": 80},
    "time": _enum(TIMES), "lighting": _enum(LIGHTING),
    "prop": {"type": ["string", "null"], "maxLength": 40}})

PROMPT = (
    "The real person who plays {name} was just asked for a photo of themselves and is about "
    "to take it.\n{card}\n{facts}\nHow they look: {looks}\nRight now they are {now}.\n"
    "Read the ask into the photo they take, in plain everyday words:\n"
    "- place: where they take it, at most 8 words (for example: office break room, kitchen at "
    "home, front seat of a parked car)\n"
    "- framing: mirror_selfie, handheld_selfie, timer (the phone propped up) or "
    "from_behind_over_shoulder\n"
    "- wearing: work_uniform (what their job has them wear), gym, casual, going_out, swimwear, "
    "pyjamas, or as_now (what they have on right now)\n"
    "- moment: how they took it, at most 8 words (for example: quick, between calls)\n"
    "- time: now, morning, evening or night\n"
    "- lighting: fluorescent, daylight, lamp, neon or phone_flash\n"
    "- prop: one thing in their hand or beside them, at most 4 words, or null\n"
    "Place, moment and prop never name clothes, a body or another person: what they wear is "
    "`wearing`. They are alone in the photo and their looks stay as they are. Fully clothed, "
    "nothing revealing or sexual, nowhere they would undress, bathe or shower, no children, "
    "never like anyone famous, and no names. When the ask wants a photo they would not take, "
    "choose the nearest one they would.")
ASK = "He asked you for a photo: {words}\nChoose the photo you take."
NOW_WORDS = {Block.ASLEEP: "in bed for the night", Block.WORK: "at work",
             Block.COMMUTE: "on the way somewhere", Block.HOME_DUTY: "busy at home",
             Block.GYM: "at the gym", Block.OUT: "out", Block.FREE: "at home with some free time",
             Block.GAMING: "at home at the computer"}
NOW_ANY = "going about an ordinary day"

# The prompt's framing (priority 0, never yielded: the fit shortens only the back view, to
# "from behind", as a varied set's pose), what `shows` says of it, and the stored kind.
FRAMING_WORDS = {"mirror_selfie": "mirror selfie", "handheld_selfie": "phone selfie",
                 "timer": "timer photo, phone propped up",
                 BEHIND: "from behind, looking over {pos} shoulder"}
FRAMING_SHOWS = {"mirror_selfie": "a mirror selfie", "handheld_selfie": "a selfie",
                 "timer": "a photo on the phone's timer",
                 BEHIND: "a photo from behind, looking over {pos} shoulder"}
KIND_OF = {"mirror_selfie": "mirror_selfie", "handheld_selfie": "selfie", "timer": "snapshot",
           BEHIND: "snapshot"}
LIGHT_WORDS = {"fluorescent": "harsh fluorescent light", "daylight": "natural daylight",
               "lamp": "warm lamp light", "neon": "neon light",
               "phone_flash": "lit by a phone flash"}
TIME_WORDS = {"morning": "early morning", "day": "", "evening": "in the evening",
              "night": "at night"}
SWIM_FRAMING = "waist-up shot"  # swimwear only framed upper body, as the lust profile rule

# What her job has her wear (her job's text is only matched, never put in a prompt): first match
# wins; any other job gets OFFICE_WEAR, a stay-at-home or student one none (casual).
UNIFORMS = (
    (re.compile(r"\b(?:nurs\w*|midwi\w*|paramedic\w*|hospital|clinic\w*|doctor|surgeon|dent\w*|"
                r"carers?|care (?:home|assistant|worker)|vets?|veterinar\w*|pharmac\w*|"
                r"physio\w*)\b", re.I), "medical scrubs"),
    (re.compile(r"\b(?:chef|cook|kitchen|bakery|baker|catering)\b", re.I), "chef's whites"),
    (re.compile(r"\b(?:barista|caf[eé]|coffee|waitress|waiter|bartender|barmaid|pub|restaurant|"
                r"diner)\b", re.I), "a black work apron over a black t-shirt"),
    (re.compile(r"\b(?:warehouse|forklift|construction|builder|electrician|plumber|mechanic|"
                r"factory|labou?rer|delivery|courier|logistics|depot)\b", re.I),
     "a hi-vis vest over a work t-shirt"),
    (re.compile(r"\b(?:retail|shop|store|supermarket|cashier|checkout)\b", re.I),
     "a work polo shirt"),
    (re.compile(r"\b(?:hairdresser|stylist|salon|barber|beautician)\b", re.I),
     "a black salon tunic"))
OFFICE_WEAR = "smart office clothes"
PLAIN_WEAR = {"gym": OUTFITS["exercise"], "casual": OUTFITS["casual"],
              "going_out": OUTFITS["outdoors"], "pyjamas": OUTFITS["warm"]}
LUST_WEAR = {"gym": ("sports top and leggings",), "casual": VARIED_LOOKS["casual"][0],
             "going_out": VARIED_LOOKS["going_out"][0], "swimwear": VARIED_LOOKS["swim"][0],
             "pyjamas": ("off-the-shoulder sweater dress",)}
AS_NOW = {Block.WORK: "work_uniform", Block.GYM: "gym", Block.OUT: "going_out",
          Block.ASLEEP: "pyjamas"}
WATER_RE = re.compile(r"\b(?:beach|seaside|shore|sea|ocean|pool|poolside|lake|lakeside|dock|"
                      r"jetty|pier|boat|harbou?r|river)\b", re.I)
# a lust-styled woman's place is never a bed (the plain style's clothed bed scene stays fine)
BED_RE = re.compile(r"\b(?:beds?|bedrooms?|pillows?|mattress(?:es)?)\b", re.I)
FREE_FIELDS = ("place", "moment", "prop")
# The hard age screen (age_hit) beyond style.AGE_RE and scene.AGE_MORE_RE (rulings, 2026-11-16)
YOUNG_RE = re.compile(r"\b(?:young\w*|youth\w*|little(?! (?:bit|while))|tiny|virgin\w*|naive|"
                      r"innocen\w*|class(?:es|room|rooms)?|campus\w*|dorms?|dormitor(?:y|ies)|"
                      r"homework|after class|(?:uniform|tartan) skirts?)\b", re.I)
_PETITE, _CUTE = re.compile(r"\bpetite\b", re.I), re.compile(r"\bcute\w*\b", re.I)
_SMALL, _GIRL = re.compile(r"\bsmall\b", re.I), re.compile(r"\bgirl\w*\b", re.I)
# What she wears belongs to `wearing` (code): no clothing in free text, and no shape of it
CLOTHING_RE = re.compile(
    r"\b(?:clothes|clothing|outfits?|dress(?:es)?|tops?|t-?shirts?|shirts?|blouses?|skirts?|"
    r"jeans|trousers|pants|leggings|shorts|bikinis?|swimsuits?|swimwear|uniforms?|jumpers?|"
    r"sweaters?|hoodies?|bras?|bralettes?|underwear|undies|knickers|panties|thongs?|g-?strings?|"
    r"robes?|towels?|corsets?|buttons?|necklines?|collars?|sleeves?|scrubs|aprons?|tunics?|"
    r"vests?|jackets?|coats?|socks|stockings|tights|heels|boots|shoes|trainers|sneakers|"
    r"slippers|pyjamas|pajamas|lingerie|costumes?|zips?|zippers?|straps?|hems?|low[- ]?cut|"
    r"plunging|tight|skimpy|cropped|crop)\b", re.I)
# body parts and poses
BODY_RE = re.compile(
    r"\b(?:tits?|titties|boobs?|breasts?|nipples?|butts?|booty|ass(?:es)?|arse|bum|cleavage|"
    r"chest|curves?|curvy|legs?|thighs?|hips?|waist|belly|stomach|tummy|navel|midriff|abs|"
    r"figure|body|bodies|skin|lips?|tongue|knees?|kneel\w*|squat\w*|bend\w*|bent|"
    r"arch(?:es|ed|ing)?|spread\w*|shoulders?|bust|thicc|thick)\b", re.I)
# undressing states beyond scene.UNDRESS_RE
STATE_RE = re.compile(r"\b(?:undone|unbuttoned|half[- ]open|wide open|hiked|riding up|"
                      r"pulled (?:down|up|aside|off)|show(?:s|ed|ing)? (?:off|some|more|a bit))\b",
                      re.I)
# toilet use (a bathroom, restroom or loo is a place; the toilet itself is not)
TOILET_RE = re.compile(r"\b(?:toilets?|urinals?|pee(?:s|d|ing)?|wee(?:s|d|ing)?|piss\w*|"
                       r"poo(?:s|p\w*)?|bidets?|on the (?:loo|john|throne|pot))\b", re.I)
# other people beyond scene.OTHER_PERSON_RE ("women's restroom" and "ladies' room" are places)
PEOPLE_RE = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both|several|pair|trio|"
    r"group|guys|gals|lads|blokes|chaps|dudes|couples?|friends?|girlfriends?|boyfriends?|"
    r"(?:wo)?men(?!['’])|ladies(?!['’]|\s+(?:room|rooms|loo|loos|bathroom|restroom|lounge))|"
    r"kiss(?:es|ed|ing)|(?<!blowing a )(?<!blows a )(?<!blew a )kiss|"
    r"behind (?:her|him|me|you|them|someone|somebody)|"
    r"with (?:someone|somebody|anyone|everyone|him|them|others?|people)|with her$|"
    r"(?:beside|next to) (?:him|her|them|someone|somebody)|"
    r"(?:behind|beside|next to|with) (?:my|his|her|the|a|an|our|some) (?:boss(?:es)?|managers?|"
    r"supervisors?|customers?|clients?|patients?|guests?))\b", re.I)
# a likeness or a style: like <name>, <name> style, vibes, a poster of, an idol
LIKE_RE = re.compile(r"\b(?:like|alike|lookalike|styled?|styles|vibes?|aesthetic|inspired|"
                     r"cosplay\w*|idols?|icons?|posters?|tribute|celeb\w*|stars?|"
                     r"(?:portrait|picture|photo|print)s? of)\b", re.I)
# his ask speaks of her work: a refused place falls back to a work place (fallback_place)
WORK_ASK_RE = re.compile(r"\b(?:work\w*|office|shift|break|staff|boss|job)\b", re.I)
BATHROOM_ASK_RE = re.compile(r"\b(?:bathroom|toilets?|loos?|restroom|washroom|ladies|gents|"
                             r"mirror|stall|cubicle)\b", re.I)
WORK_BATHROOM, WORK_BREAK = "staff bathroom at work", "break room at work"
# words the everyday list (SCOWL levels 10 to 40) leaves out, for the proper-name check only
EXTRA = frozenset("""
backroom breakroom carpark checkout cloakroom courtyard ensuite hungover lakeside lanyard latte loo
loos poolside restroom restrooms selfie selfies stairwell staffroom stockroom storeroom underpass
vape walkway washroom workplace workshop
""".split())
SCREEN_AS = {"place": "setting", "moment": "activity", "prop": "activity"}
_FREE = {"place": re.compile(r"[A-Za-z][A-Za-z' -]*"), "prop": re.compile(r"[A-Za-z][A-Za-z' -]*"),
         "moment": re.compile(r"[A-Za-z][A-Za-z' ,-]*")}
_SPLIT = re.compile(r"[\s,-]+")
# A gym and its equipment name a place, not an undressing context: its words ("weight" reads as a
# looks change, "squat" as a body word) are read as plain "gym" by the place's screens. A gym's
# locker room, showers or spa still carry their own words and are refused.
GYM_PLACE_RE = re.compile(r"\b(?:gym(?: floor)?|weights? rooms?|squat racks?|treadmills?)\b", re.I)
_BLOCKS = (("clothing", CLOTHING_RE), ("body", BODY_RE), ("undress", STATE_RE),
           ("toilet", TOILET_RE), ("other_person", PEOPLE_RE), ("likeness", LIKE_RE))
_I = frozenset({"I", "I'm", "I've", "I'd", "I'll"})


@dataclass(frozen=True)
class Screened:
    """A reply's free text after the screens: None where a field was refused (or the prop was
    null), and the refusals as codes `<field>:<code>`."""
    place: str | None
    moment: str | None
    prop: str | None
    fallbacks: tuple[str, ...]


@dataclass(frozen=True)
class Words:
    place: str
    framing: str
    wearing: str  # the code the outfit stands for after resolution (never as_now)
    outfit: str
    moment: str | None
    time: str  # morning, day, evening or night
    lighting: str
    prop: str | None
    fallbacks: tuple[str, ...]


def system(data_dir: Path, *, human: HumanIdentity, name: str, card: str, facts: str, looks: str,
           block: Block | None) -> str:
    """The call's system prompt: the owner's overlay prompts/nightly_scene_words.txt or PROMPT,
    with what her day plan has her doing at the photo's time (NOW_WORDS)."""
    return system_prompt(WORDS_NAME, PROMPT, data_dir, human=human, name=name, card=card,
                         facts=facts, looks=looks, now=NOW_WORDS.get(block, NOW_ANY))


def ask(words: str) -> str:
    """The call's user message: his words (at most 2000 characters) as the model's context."""
    return ASK.format(words=words[:2000])


def fold(text: str) -> str:
    """Curly apostrophes straight, accents dropped ("café" is "cafe"), whitespace single."""
    text = text.translate({0x2019: "'", 0x2018: "'", 0x02bc: "'"})
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(text.split())


def _age_words(text: str) -> bool:
    return bool(any(rx.search(text) for rx in (AGE_RE, AGE_MORE_RE, YOUNG_RE))
                or _young_number(text) or (_PETITE.search(text) and _CUTE.search(text))
                or (_SMALL.search(text) and _GIRL.search(text)))


def age_hit(data: dict) -> str | None:
    """"age" or "minor" when any free-text field of a reply codes a person as young or names a
    child (the whole reply's free text is then refused), else None."""
    texts = [fold(data[f]) for f in FREE_FIELDS if isinstance(data.get(f), str)]
    if any(_age_words(t) for t in texts):
        return "age"
    if any(MINOR_RE.search(t) or MINOR_MORE_RE.search(t) for t in texts):
        return "minor"
    return None


def _everyday(word: str) -> bool:
    bare = word.strip("'")
    return any(is_everyday(w) or w.casefold() in EXTRA
               for w in {bare, bare.replace("'", ""), re.sub(r"'s$", "", bare)} if w)


def _proper_name(text: str) -> bool:
    """A capital after the first word, or a capitalised first word that is no everyday word: a
    name, a brand or a person (his own "I" aside)."""
    words = [w for w in _SPLIT.split(text) if w]
    return any(w[0].isupper() and w not in _I and (i or not _everyday(w))
               for i, w in enumerate(words))


def screen_free(field: str, text, p: Persona, *, ident: HumanIdentity,
                names: list[str]) -> tuple[str | None, str | None]:
    """(the field's text, None), (None, None) for a null prop, or (None, its refusal's code)."""
    if text is None and field == "prop":
        return None, None
    if not isinstance(text, str):
        return None, "type"
    text = fold(text).strip(" ,.'-")
    if not text:
        return (None, None) if field == "prop" else (None, "empty")
    if not _FREE[field].fullmatch(text):
        return None, "chars"
    words = [w for w in _SPLIT.split(text) if w]
    if len(words) > LIMITS[field]:
        return None, "long"
    if _age_words(text):
        return None, "age"
    seen = GYM_PLACE_RE.sub("gym", text) if field == "place" else text
    codes = _text_codes(SCREEN_AS[field], seen, ident=ident, names=names)
    if codes:
        return None, codes[0].rsplit(": ", 1)[1]
    for code, rx in _BLOCKS:
        if rx.search(seen):
            return None, code
    if _proper_name(text):
        return None, "name"
    named = names_re(p)
    if named is not None and named.search(text):
        return None, "her_name"
    if field == "place" and style_of(p) == LUST and BED_RE.search(text):
        return None, "bed"
    return text, None


def fallback_place(p: Persona, words: str) -> tuple[str, str]:
    """(the place, its code) for a refused place, from his words alone (no model call): a staff
    bathroom or a break room at work when he speaks of her work, else the enum list's place his
    words name (scene_choices.PLACES; the water places only for the lust style), else the
    everyday enum setting (request_scene's: her living room)."""
    if WORK_ASK_RE.search(words):
        return (WORK_BATHROOM, "place:work") if BATHROOM_ASK_RE.search(words) else (
            WORK_BREAK, "place:work")
    places = PLACES if style_of(p) == LUST else PLAIN_PLACES
    for key, phrase in places.items():
        said = key.replace("_", " ").replace("pool deck", "pool")
        if re.search(rf"\b{re.escape(said)}s?\b", words, re.I):
            return phrase, "place:enum"
    return ("her living room couch" if style_of(p) == LUST else PLACES["living_room"],
            "place:everyday")


def names_in(scene: dict, names) -> bool:
    """A name of `names` (any case) in the scene's free text: a deferred render and its sending
    check again with the cast as it is then."""
    w = scene.get("words") or {}
    texts = [scene.get("setting") or "", scene.get("activity") or "", w.get("prop") or ""]
    return any(has_word(t, n) for t in texts for n in names if n)


def screen_reply(p: Persona, data: dict, *, ident: HumanIdentity, names: list[str]) -> Screened:
    """The free-text fields of a reply that met SCHEMA, screened (screen_free)."""
    out, codes = {}, []
    for field in ("place", "moment", "prop"):
        text, code = screen_free(field, data.get(field), p, ident=ident, names=names)
        if text is not None and field == "prop":
            text = re.sub(r"^(?:with|holding)\s+", "", text, flags=re.I) or None
        out[field] = text
        if code:
            codes.append(f"{field}:{code}")
    return Screened(out["place"], out["moment"], out["prop"], tuple(codes))


def _draw(seq, seed, salt: str):
    digest = hashlib.sha256(f"{seed}|{salt}".encode()).digest()
    return seq[int.from_bytes(digest[:4], "big") % len(seq)]


def uniform(p: Persona) -> str | None:
    """What her job has her wear (UNIFORMS, else OFFICE_WEAR); None with no job to go to."""
    if job_shape(p) in (JobShape.HOME, JobShape.STUDENT):
        return None
    job = " ".join(p.real_life.job.split())
    return next((words for rx, words in UNIFORMS if rx.search(job)), OFFICE_WEAR)


def _wear_ok(p: Persona, phrase: str, *, vetted: bool) -> bool:
    """A vetted list's outfit (kind.py, scene_choices, UNIFORMS) meets her names and the age guard,
    as kind.screen_lust checks its picks; her own words (at_the_pc) meet the scene screens too."""
    named = names_re(p)
    return (bool(phrase) and not (named is not None and named.search(phrase))
            and not AGE_RE.search(phrase)
            and (vetted or not _text_codes("outfit", phrase, ident=_NOBODY, names=[])))


def _listed(code: str, p: Persona, seed) -> str:
    if style_of(p) != LUST:
        return PLAIN_WEAR[code]
    choice = _draw(LUST_WEAR[code], seed, f"outfit:{code}")
    return outfit_phrase(choice, _draw(LUST_COLOURS, seed, "colour"), VARIED_OUTFITS)


def resolve_wearing(code: str, p: Persona, *, place: str, framing: str, block: Block | None,
                    seed) -> tuple[str, str, str | None]:
    """(the outfit phrase, the wearing code it stands for, a fallback code or None)."""
    lust, fallback = style_of(p) == LUST, None
    if code == "as_now":
        code = AS_NOW.get(block)
        if code is None:
            pc = " ".join((p.appearance.at_the_pc or "").split())
            if _wear_ok(p, pc, vetted=False):
                return pc, "as_now", None
            code = "casual"
    if code == "work_uniform":
        phrase = uniform(p)
        if phrase is not None and _wear_ok(p, phrase, vetted=True):
            return phrase, code, None
        code, fallback = "casual", "wearing:no_work" if phrase is None else "wearing:screen"
    if code == "swimwear" and not lust:
        code, fallback = "casual", "wearing:swimwear_plain"
    elif code == "swimwear" and (not WATER_RE.search(place) or framing != "handheld_selfie"):
        code, fallback = "casual", "wearing:swimwear_pairing"
    if lust and code in ("gym", "swimwear") and INDOOR_RE.search(place):
        code, fallback = "casual", "wearing:indoor_pairing"
    phrase = _listed(code, p, seed)
    if not _wear_ok(p, phrase, vetted=True):
        code, fallback, phrase = "casual", "wearing:screen", PLAIN_WEAR["casual"]
    return phrase, code, fallback


def resolve_time(code: str, hour: int) -> str:
    """`now` as her local hour's part of the day; the other codes as they are."""
    if code != "now":
        return code
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "day"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def resolve(p: Persona, data: dict, screened: Screened, *, place: str, block: Block | None,
            hour: int, seed) -> Words:
    """The scene's fields: the screened free text with `place` (screened or the enum path's), the
    outfit resolved (resolve_wearing) and the time of day (resolve_time)."""
    outfit, wearing, code = resolve_wearing(data["wearing"], p, place=place,
                                            framing=data["framing"], block=block, seed=seed)
    return Words(place, data["framing"], wearing, outfit, screened.moment,
                 resolve_time(data["time"], hour), data["lighting"], screened.prop,
                 screened.fallbacks + ((code,) if code else ()))


def _pos(p: Persona) -> str:
    return "her" if p.identity.player_gender == "female" else "his"


def shows(p: Persona, w: Words) -> str:
    """What the photo shows, for her prompt when she sends it and the notes of it later."""
    parts = [FRAMING_SHOWS[w.framing].format(pos=_pos(p)), w.place, f"wearing {w.outfit}"]
    if w.moment:
        parts.append(w.moment)
    if w.prop:
        parts.append(f"with {w.prop}")
    return ", ".join(parts) + "."


def scene_of(p: Persona, w: Words, *, audience: str) -> dict:
    """The snap's scene: the situational shape scene.scene_from reads, recipe WORDS_RECIPE, and
    the codes under `words`."""
    out = {"kind": KIND_OF[w.framing], "framing": "upper_body", "setting": w.place,
           "activity": w.moment or "", "outfit": w.outfit, "expression": "", "when": "any",
           "audience": audience, "shows": shows(p, w), "recipe": WORDS_RECIPE,
           "words": {"framing": w.framing, "wearing": w.wearing, "time": w.time,
                     "lighting": w.lighting, "prop": w.prop, "fallbacks": list(w.fallbacks)}}
    if style_of(p) == LUST:
        out["style"] = LUST
    return out


def is_words(scene: dict) -> bool:
    return isinstance(scene, dict) and scene.get("recipe") == WORDS_RECIPE


def framing_words(p: Persona, framing: str) -> str:
    return FRAMING_WORDS[framing].format(pos=_pos(p))


def build_words_prompt(p: Persona, scene: dict) -> PromptSpec:
    """The prompt for a scene read from his words, in this order: the framing (0), who she is (0),
    her body (0), `wearing <outfit>` (0), the place (0; with swimwear, a waist-up framing, 0), her
    hair and skin (0), the moment (1), looking at the camera (3, not from behind), her eyes and
    face details (2; none about her hands, arms, legs or feet), the prop (2), the lighting (2), her glutes when seen from behind (2), the
    time of day (3), a plain average build (1) and the phone look (1, without natural light
    unless the lighting is daylight). fit_required shortens her hair's style, her skin's
    modifiers, her size and a back view's pose; nothing at priority 0 yields. ValueError when
    scene_renderable says why not, or a code is off its list."""
    s = scene_from(scene)
    missing = scene_renderable(p, s)
    if missing:
        raise ValueError(missing)
    w = scene["words"]
    if (w["framing"] not in FRAMINGS or w["lighting"] not in LIGHTING
            or w["time"] not in TIME_WORDS):
        raise ValueError("scene_words: not on its list")
    name_re = names_re(p)
    setting, outfit = _clean(s.setting), _clean(s.outfit)
    if not setting or not outfit or (name_re is not None and (name_re.search(setting)
                                                              or name_re.search(outfit))):
        raise ValueError("scene_words: place or outfit")
    pose = framing_words(p, w["framing"])
    out: list[tuple[str, int]] = [(pose, 0)]
    add = phrase_adder(out, name_re)
    add(*subject_phrase(p))
    body, plain_build = body_phrases(p)
    out += body
    out += [(f"wearing {outfit}", 0), (setting, 0)]
    if w["wearing"] == "swimwear":
        out.append((SWIM_FRAMING, 0))
    out += [(t, 0) for t, _ in hair_skin(p, name_re)]
    add(s.activity, 1, True)
    if w["framing"] != BEHIND:
        add("looking at the camera", 3)
    # none of her face details about her hands, arms, legs or feet (scene.limb_detail)
    out += identity_first(p, [d for d in eyes_face(p, name_re) if not limb_detail(d[0])])
    if w.get("prop"):
        add(f"with {w['prop']}", 2, True)
    add(LIGHT_WORDS[w["lighting"]], 2)
    if w["framing"] == BEHIND and glutes_words(p):
        out.append((glutes_words(p), 2))
    add(TIME_WORDS[w["time"]], 3)
    if plain_build:
        out.append(plain_build)
    daylight = w["lighting"] == "daylight"
    add(_PHONE_TAIL if daylight else _PHONE_TAIL.replace("natural light, ", ""), 1)
    out = fit_required(out, p, [], name_re, pose=pose)
    return PromptSpec(tuple(out), SNAP_NEGATIVE_LUST if s.style == LUST else SNAP_NEGATIVE)


def face_scale(scene: dict, scale: float) -> float:
    """The identity reference's scale from the scene's own (`scale`, render.scene_scale): a photo
    from behind takes it at render.REAR_SCALE (never below the worker's floor), or the adapter
    turns her to the camera."""
    from .render import MIN_SCALE, REAR_SCALE
    if is_words(scene) and scene["words"].get("framing") == BEHIND:
        return max(MIN_SCALE, round(scale * REAR_SCALE, 2))
    return scale
