"""Fixed-list situational photos, legacy scene screens and identity-aware render prompts.

The reflection model only chooses enums. Code composes every phrase, then the existing
screens run unchanged. Stored prose remains readable; lust scenes keep their held-style rules.
"""
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly.enrich import LIKENESS_RE
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.text import has_meta

from .kind import (ERA_RE, EXPLICIT_RE, GYM_OUTFITS, LUST_COLOURS, LUST_EXPRESSIONS,
                   LUST_OUTFITS, PAIRINGS, SWIM_OUTFITS, WEAR_RE, _enum,
                   compose_lust, screen_lust)
from .prompt import (BUILD_WORDS, _FRAMINGS, _HEADSHOT_TAIL, _KINDS, _PHONE_TAIL, LUST_HELD, LUST_NAMED,
                     LUST_NEGATIVE, NEGATIVE, PromptSpec, eyes_face, hair_skin, looks_phrases,
                     lust_phrase, names_re, phrase_adder, renderable, subject_phrase)
from .style import AGE_RE, LUST, PLAIN, STYLES, SUGGESTIVE_MIN_AGE, style_of, suggestive
from .scene_choices import (ACTIVITIES, EXPRESSIONS, GAME_MONITOR, ONE_FACE_NEGATIVE,
                            OUTFITS, PLACES, PROPS, SCENE_LUST_SETTINGS, TILE_DESK,
                            TILE_FRAMING, compose_choices)

SCENE_NAME = "scene"             # its overlay: prompts/nightly_scene.txt
SCENE_LUST_NAME = "scene_lust"   # its overlay: prompts/nightly_scene_lust.txt

KINDS = ("selfie", "mirror_selfie", "snapshot")
FRAMINGS = ("face", "head_and_shoulders", "upper_body")
WHENS = ("morning", "day", "evening", "late", "any")
AUDIENCES = ("him", "anyone")


_COMMON = {"photo": {"type": "boolean"}, "kind": _enum(KINDS), "framing": _enum(FRAMINGS)}
_TAIL = {"when": _enum(WHENS), "audience": _enum(AUDIENCES), "prop": _enum(PROPS)}
SCENE_SCHEMA = schema(SCENE_NAME, {
    **_COMMON, "setting": _enum(PLACES), "activity": _enum(ACTIVITIES), "outfit": _enum(OUTFITS),
    "expression": _enum(EXPRESSIONS), **_TAIL})

# Reuse profile choices except the crowd, bathing and hallway-mirror settings.
SCENE_SCHEMA_LUST = schema(SCENE_LUST_NAME, {
    **_COMMON, "setting": _enum(SCENE_LUST_SETTINGS), "activity": _enum(ACTIVITIES),
    "outfit": _enum(LUST_OUTFITS), "colour": _enum(LUST_COLOURS),
    "expression": _enum(LUST_EXPRESSIONS), **_TAIL})

SCENE_PROMPT = (
    "Choose a photo the real person who plays {name} might take in the coming days and send to "
    "a friend from the guild.\n{card}\n{facts}\nHow they look: {looks}\n"
    "What is going on: {about}\nSay whether they would take a photo of it at all. "
    "Only choose the allowed enum values; never write descriptive text. Choose a setting, "
    "activity, outfit, expression, time, framing and audience. The optional prop uses none "
    "when absent. Keep activities in their natural places: desk games at a desk, cooking in "
    "a kitchen, exercise in a gym, dog walks outdoors. Props must fit the activity. "
    "They are alone and their looks stay unchanged. Fully clothed, nothing revealing or "
    "sexual, no children, never like anyone famous, and no one else's name.")
SCENE_PROMPT_LUST = (
    "Choose a photo the real person who plays {name} might take in the coming days.\n"
    "{card}\n{facts}\nHow she looks: {looks}\nWhat is going on: {about}\n"
    "Say whether she would take a photo. Only choose allowed enum values, never prose. "
    "Setting, outfit, colour and expression reuse the profile photo's vetted choices. "
    "Choose an activity that belongs in the setting, and a compatible prop or none. "
    "Swimwear only by the water and framed upper body; sports top and leggings only at "
    "the gym. She is alone, adult and fully clothed, her looks "
    "unchanged. No names or likenesses. Choose no photo when no suitable combination fits.")
SCENE_ASK = "Choose {name}'s photo."
ABOUT = {"life": "Tonight in their life away from the game: {beat}",
         "birthday": "It is their birthday tomorrow.",
         "everyday": "An ordinary day for them this week."}

SNAP_NEGATIVE = NEGATIVE + (("another person, crowd, group of people", 0),
                            (ONE_FACE_NEGATIVE, 0))
SNAP_NEGATIVE_LUST = LUST_NEGATIVE + (("another person, crowd, group of people", 0),
                                      (ONE_FACE_NEGATIVE, 0))

# A looks change: her looks stay exactly as described in a situational photo. "Weights" and
# "a glass of wine" are not one.
LOOKS_RE = re.compile(
    r"\b(hair ?cut|new (?:hair|hairstyle|look|glasses|specs)|dyed|dyes?|bleach(?:ed)?|"
    r"highlights|fringe|bangs|perm|buzz ?cut|shaved (?:head|off)|beard|moustache|mustache|"
    r"goatee|stubble|eyeglasses|spectacles|wearing glasses|contact lenses|tattoo(?:s|ed)?|"
    r"piercings?|pierced|weight|slimmer|thinner|makeover|braces|wig|extensions)\b", re.I)
# A child in the scene: nobody else is in it, and never a child.
MINOR_RE = re.compile(
    r"\b(child|children|kids?|kiddo|bab(?:y|ies)|toddlers?|infants?|sons?|daughters?|boys?|"
    r"girls?|teens?|teenagers?|nieces?|nephews?|grand(?:child|children|son|daughter)s?|"
    r"schoolkids?|pupils?)\b", re.I)

# The rest of the screen (controller rulings R12 to R20) reads every free-text field, whatever
# the style, with its whitespace folded. Whole words only, so a robin and a wardrobe are not a
# robe and a bathroom mirror is not a bath. A clothed scene in bed or a bedroom is fine (R12a),
# except beside a swimwear or gym pick (INDOOR_RE). Accepted over-refusals (R20): taking off a
# coat, kicking off shoes, old-school, a beach towel, a sports bra at the gym, a bathing suit.

# Undressing and bathing (R12, R16), code `undress`: #174's free-text lust screen (the
# yardstick OLD_REFUSED in tests/nightly/test_night_photo_lust.py) without its bed, bedroom and
# bathroom, and with the hot tub, since free text never names her place; then the bath, shower,
# bedding, robe, underwear and changing phrasings.
_CLOTHES = (r"(?:clothes|clothing|dress|dresses|top|tops|shirt|blouse|skirt|jeans|trousers|"
            r"pants|leggings|shorts|bikini|swimsuit|outfit|uniform|jumper|sweater|hoodie|"
            r"bra|underwear|robe|towel|corset)")
UNDRESS_RE = re.compile(
    r"\b(nude|nudes|naked|nudity|topless|shirtless|braless|undress\w*|unclothed|nipples?|"
    r"pasties|body[- ]?paint(?:ed)?|unbutton\w*|unzip\w*|wet|soaked|dripping|oiled|"
    r"nothing (?:but|else|underneath|on|at all)|wearing nothing|not wearing anything|"
    r"birthday suit|au naturel|(?:no|without) (?:a )?(?:top|bra|pants|shirt|clothes|clothing)|"
    r"(?:wearing |in )?(?:just|only|nothing but) (?:a|an|her|his|my|some)\b"
    r"(?! (?:little|bit|few|couple|touch|tad|moment|minute|second|while|slight)\b)|"
    r"(?:just|only|nothing but) (?:[\w'-]+ ){0,2}(?:socks?|stockings|tights|heels|boots|"
    r"shoes|apron|scarf|tie|gloves|ribbons?|bows?|jewell?ery|necklace)|"
    r"slip\b(?![\s-]*(?:dress|on|s on|ped|ping))|barely (?:dressed|there|covered|clothed|anything)|scantily|skimpy|"
    r"half[- ]dressed|partially dressed|covering (?:her|his|my) (?:chest|body)|"
    r"bare[- ]?(?:chested|breast\w*)|breasts?|\w*boob\w*|areolas?|lingerie|underwear|undies|"
    r"bra|bras|bralettes?|bralets?|brassieres?|bustiers?|panties|knickers|thong|g-string|"
    r"boyshorts|negligee|nightie|nightgown|nightdress|nightwear|sleepwear|pajamas?|pyjamas?|"
    r"pjs|chemise|camisole|bodysuit|babydolls?|baby-doll|teddy(?![- ]bears?)|teddies|corset|"
    r"garters?|body ?stockings?|stockings|suspenders|bandeau|catsuit|latex|harness|fishnets?|"
    r"mesh|robes?|bathrobes?|monokini|micro[- ]?bikini|see[- ]?thr(?:ough|u)|sheer|"
    r"transparent|shower\w*|baths?|bathtubs?|bubble bath|tubs?|sheets?|covers|blankets?|"
    r"duvets?|sauna|steam room|locker room|changing room|fitting room|nsfw|sex|sexual|"
    r"sexually|sexuali[sz]ed|seductive|erotic|erotica|explicit|porn\w*|fetish|boudoir|horny|"
    r"aroused|kinky|lewd|xxx|onlyfans|on all fours|bent over|straddl\w*|legs spread|"
    r"spread legs|nudist|naturist|clothing-optional|strip(?!e)\w*|spa|stage|jacuzzi|"
    # bath and shower
    r"towe?l\w*|bath(?:ing|ed|es)|bathe|bubble[\s-]*baths?|hot[\s-]*tubs?|hot[\s-]*springs?|"
    r"onsens?|skinny[\s-]*dip\w*|soak\w*|rins\w*|"
    r"wash(?:es|ed|ing)? (?:her|his|their|my) (?:hair|face|body)|glisten\w*|"
    # bedding, robes and underwear
    r"bed[\s-]*sheets?|quilts?|ready for bed|bed[\s-]*time|dressing[\s-]*gowns?|"
    r"house[\s-]*coats?|kimonos?|peignoirs?|boxers|briefs|leotards?|booty[\s-]*shorts|"
    r"hot[\s-]*pants|night[\s-]*shirts?|sleep[\s-]*shirts?|"
    # changing
    r"chang(?:e|es|ed|ing) (?:(?:her|his|their|my|your) )?(?:\w+ )?(?:clothes|outfits?)|"
    r"chang(?:e|es|ed|ing) (?:into|out of)|(?:get|gets|getting|got) changed|"
    r"(?:get|gets|getting|got) dressed(?! up)|dressing (?:for|in|into|herself|himself)|"
    r"(?:put|puts|putting|pull|pulls|pulled|pulling|slip|slips|slipped|slipping|try|tries|"
    r"tried|trying) on(?! (?:(?:her|his|their|my|the|a|an|some) )?(?:\w+ )?(?:shoes|boots|"
    r"sandals|trainers|sneakers|slippers|socks|coat|jacket|hat|gloves|scarf|heels)\b)|"
    # undressing: clothes off, on the floor, out of them, let fall, flashing, bare skin
    rf"{_CLOTHES} off|{_CLOTHES} (?:\w+ ){{0,2}}(?:on|to) the floor|"
    rf"out of (?:her|his|their|my|your|the) (?:\w+ )?{_CLOTHES}|"
    r"let(?:s|ting)? (?:her|his|their|my|the) (?:\w+ )?(?:fall|drop|slip|slide)|"
    rf"(?:loosen|lift|rais|hitch|hik|tug|lower|pull|undo|undid)\w* (?:her|his|their|my|your) "
    rf"(?:\w+ )?{_CLOTHES}|unhook\w*|unfasten\w*|unlac\w*|disrob\w*|"
    r"flash(?:es|ed|ing)|revealing|exposed|exposing|"
    # R23: only or just wearing, a clothing word then only, bare tokens, a euphemism
    rf"(?:only|just) wearing|{_CLOTHES} only|(?:only|just) (?:[\w'-]+ ){{0,2}}perfume|"
    r"bottomless|commando|in the buff|clothing[\s-]*optional|bare[\s-]*skinned|"
    r"wearing a smile|slip(?:s|ped|ping)? into something|"
    r"bare[\s-]*(?:shoulders?|backs?|skin|legs?|midriff|body|bottom|bum))\b", re.I)
# A verb within 3 words of off, down, out of or up (R16), and a strap near a verb: undress.
_PEEL = (r"(?:slip\w*|slid(?:e|es|ing)?|pull\w*|peel\w*|strip(?!e)\w*|lower\w*|shrug\w*|"
         r"wriggl\w*|wiggl\w*|shimm(?:y|ies|ied|ying)|tug\w*|yank\w*|lift\w*|hitch\w*|zip\w*|"
         r"kick\w*|rip(?:s|ped|ping)?|fall(?:s|ing|en)?|fell|loosen\w*)")
_POSS = r"(?:her|his|their|my|your)"
UNDRESS_VERB_RE = re.compile(
    rf"\b(?:{_PEEL}\W+(?:\w+\W+){{0,3}}?(?:off|down|out of|up)\b|"
    rf"(?:take|takes|taking|took|taken) (?:off|down) (?:{_POSS}|the|a|an|one|it|everything)\b|"
    rf"(?:take|takes|taking|took|taken) (?:{_POSS}|it|them|everything) "
    rf"(?!(?:day|days|night|nights|week|weekend|time|break|holiday|hair)\b)(?:\w+ ){{0,2}}"
    rf"(?:off|down)\b|"
    rf"(?:{_PEEL}|let\w*|drop\w*|push\w*|undo\w*|undid)\W+(?:\w+\W+){{0,3}}?straps?\b|"
    rf"\bstraps?\W+(?:\w+\W+){{0,3}}?(?:{_PEEL}|drop\w*|undone))", re.I)
# Sexual poses and tone (R16), code `explicit`. A bed is fine; these are not, in it or out.
SEXUAL_RE = re.compile(
    r"\b(on all fours|bent over|bending over|straddl\w*|legs (?:spread|apart|open|up|wide|"
    r"in the air)|spread(?:ing|s)? (?:her |his |their |my )?legs|"
    r"arch(?:es|ed|ing)? (?:her|his|their|my) back|on (?:her|his|their|my) (?:stomach|belly|"
    r"front)|bit(?:e|es|ing) (?:her|his|their|my) (?:lower )?lip|boudoir|erotic\w*|lewd\w*|"
    r"sultr\w*|steamy|racy|provocativ\w*|naughty|risqu[eé]|spicy (?:pics?|photos?|"
    r"selfies?|shots?|snaps?|pictures?|content)|bedroom eyes|come[\s-]*hither|"
    r"twerk\w*|lap[\s-]*danc\w*|pole[\s-]*danc\w*|suggestiv\w*|"
    r"gyrat\w*|ground (?:against|into)|body[\s-]*rolls?|booty[\s-]*shak\w*|"
    r"shak(?:e|es|ing) (?:her|his|their|my) (?:hips?|booty|bum|butt)|pole[\s-]*fitness|"
    r"bends? over|spread[\s-]*eagle\w*|arched back|jiggl\w*|teasingly|tantali[sz]\w*|"
    r"flirtatious\w*|"
    r"grind(?:s|ed|ing)?(?! (?:the |some |fresh )?(?:coffee|beans?|pepper|spices?|salt|herbs?|"
    r"flour|seeds?)\b)|"
    r"thirst[\s-]*traps?|horny|kinky|teas(?:e|es|ed|ing)|sexy|sensual\w*|seductiv\w*|"
    r"cover(?:s|ed|ing)? (?:her|his|their|my) (?:chest|body|breasts?|bust)|"
    r"covering (?:her|him|them|my)sel(?:f|ves)|implied|censor\w*)\b", re.I)
# Swimwear named in free text (R19), code `explicit`: her swimwear is only ever her pick, and
# a plain scene never wears it (as EXPLICIT_RE intends).
SWIM_RE = re.compile(
    r"\b(one[\s-]*pieces?|tankinis?|monokinis?|(?:swim(?:ming)?[\s-]*)?trunks|"
    r"swim(?:suit|wear|ming[\s-]*costume|ming[\s-]*suit|[\s-]*shorts)s?|bikinis?|"
    r"bathing[\s-]*suits?|board[\s-]*shorts)\b", re.I)
# Beside a swimwear or gym pick (R19), her free text never takes her indoors or into a bed:
# reported as that pick's pairing.
INDOOR_RE = re.compile(
    r"\b(beds?|bedrooms?|pillows?|mattress(?:es)?|sofas?|couch(?:es)?|duvets?|covers|"
    r"blankets?)\b", re.I)
# Another person in the photo (R18), code `other_person`; a pet is fine. A friend who took the
# snapshot is not in it.
OTHER_PERSON_RE = re.compile(
    r"\b(boy[\s-]*friends?|girl[\s-]*friends?|husbands?|wife|wives|spouses?|fianc[eé]e?s?|"
    r"partners?|(?:her|his|their|my|a|on a|dinner|first) dates?|mums?|moms?|mothers?|mam|"
    r"dads?|fathers?|parents?|sisters?|brothers?|siblings?|aunts?|aunties|uncles?|cousins?|"
    r"grand(?:ma|pa|mother|father|parent|mum|dad)s?|gran|nan|nana|in[\s-]*laws?|"
    r"famil(?:y|ies)|"
    r"(?<!by a )(?<!by my )(?<!by her )(?<!by his )(?<!by their )friends?"
    r"(?! (?:took|takes|snapped|shot|caught))|best[\s-]*friends?|bff|besties?|mates?|"
    r"co[\s-]*workers?|colleagues?|work[\s-]*mates?|room[\s-]*mates?|flat[\s-]*mates?|"
    r"house[\s-]*mates?|team[\s-]*mates?|(?:the|her|his|their|my|a) team|squad|crew|gang|"
    r"couples?|strangers?|crowds?|people|guests?|someone|somebody|"
    r"another (?:person|man|woman|guy|girl|lady)|(?:two|three|four|five|both|all) of "
    r"(?:them|us)|(?:man|woman|guy|lady|bloke|person) (?:beside|behind|next to|near|with)|"
    r"together with|concerts?)\b", re.I)
_WITH_NAME = re.compile(r"\b[Ww]ith [A-Z][a-z]+")  # with Sarah: someone named is in it
# A likeness beyond LIKENESS_RE (R18), code `likeness`; plus a name (_named).
LIKENESS_MORE_RE = re.compile(
    r"\b(dress(?:ed|es|ing)? (?:up )?as|cosplay\w*|channell?(?:ing|ed)|impressions? of|"
    r"impersonat\w*|doing (?:her|his|their|my|a|an) best|dead[\s-]*ringer|"
    r"like an? (?:[\w-]+ )?(?:star|supermodel|model|celebrity|actress|actor|singer|popstar|"
    r"idol|princess|influencer|icon)|influencers?|barbie|in the style of)\b", re.I)
# Minors beyond MINOR_RE (R17), code `minor`.
MINOR_MORE_RE = re.compile(
    r"\b(step[\s-]*(?:sons?|daughters?|kids?|child(?:ren)?)|grand[\s-]*kids?|kiddos|kiddies|"
    r"little ones?|twins|youngsters?|newborns?|tots?|tykes?|bairns?|"
    r"god[\s-]*(?:sons?|daughters?|child(?:ren)?|kids?)|playgrounds?|play[\s-]*dates?|"
    r"day[\s-]*cares?|creches?|nurser(?:y|ies)|kindergartens?|pre[\s-]*schools?|minors?|"
    r"juveniles?|\w+[\s-]graders?|cub[\s-]*scouts?|boy[\s-]*scouts?|girl[\s-]*scouts?|"
    r"girl[\s-]*guides?|school[\s-]*child(?:ren)?|little (?:boys?|girls?|kids?)|"
    r"pregnan(?:t|cy))\b", re.I)
# Coding a person as young beyond AGE_RE (R17), code `age`, beside the age-number rule.
AGE_MORE_RE = re.compile(
    r"\b(girly|girlie|girlish|pleated[\s-]*skirts?|plaid[\s-]*skirts?|"
    r"knee[\s-]*(?:high[\s-]*)?socks|thigh[\s-]*high[\s-]*socks|proms?|sweet[\s-]*sixteen|"
    r"innocent|doe[\s-]*eye[sd]|lollipops?|lollies|retainers?|freshm[ae]n|sophomores?|dorms?|"
    r"dormitor(?:y|ies)|petite|kawaii|doll[\s-]*like|dolly|baby[\s-]*doll)\b", re.I)
_TEEN_WORD = re.compile(r"\b(?:thir|four|fif|six|seven|eigh|nine)teen\b", re.I)
_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                 "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()
_AGE_NUMBER = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")(?:[\s-]*(?:years?|yrs?)[\s-]*old|"
    r"\s*(?:yo|y/o)\b|(?:st|nd|rd|th)[\s-]*birthday)|"
    r"\b(?:turning|turned|turns|aged|age) (\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\b", re.I)
# Looks changes beyond LOOKS_RE (R17), code `looks`.
LOOKS_MORE_RE = re.compile(
    r"\b((?:sun)?glasses|specs|shav(?:e|es|ed|ing) (?:her|his|their|my) (?:head|beard|hair)|"
    r"pixie[\s-]*cut|bob[\s-]*cut|bobs?|crew[\s-]*cut|undercut|mohawk|mullet|"
    r"cut (?:her|his|their|my) hair|chop(?:s|ped|ping)? (?:off )?(?:her|his|their|my) hair|"
    r"colou?r(?:s|ed|ing)? (?:her|his|their|my) hair|(?:went|gone|going) (?:blonde?|brunette|"
    r"red|ginger|grey|gray|platinum|pink|blue|purple|bald|silver|dark)|"
    r"new (?:\w+ ){0,2}(?:hair|hairstyle|haircut|hairdo|cut|bob|look|fringe|colou?r|"
    r"nose ring|lip ring|piercing|tattoo|glasses|specs)|clean[\s-]*shaven|"
    r"(?:fresh|new|spray|fake|deep|golden) tan|tanned|tanning|pregnan(?:t|cy)|"
    r"nose[\s-]*rings?|lip[\s-]*rings?|septum|"
    r"gr(?:ew|ow|ows|owing|own) (?:out )?(?:her|his|their|my) hair|fillers?|botox|"
    r"hair (?:dyed|coloured|colored|bleached))\b", re.I)
# Era beyond ERA_RE (R17), code `era`.
ERA_MORE = re.compile(
    r"\b(fifties|sixties|seventies|eighties|nineties|noughties|y2k|throwbacks?|"
    r"old[\s-]*school|discos?|vhs|cassettes?|walkman|boombox)\b", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_CAPITAL = re.compile(r"[A-Z][a-z]")


def _young_number(text: str) -> bool:
    """An age under SUGGESTIVE_MIN_AGE (R17): a teen number word anywhere, or a number with
    years old, a birthday, turning or aged."""
    if _TEEN_WORD.search(text):
        return True
    for m in _AGE_NUMBER.finditer(text):
        n = m.group(1) or m.group(2)
        value = int(n) if n.isdigit() else _NUMBER_WORDS.index(n.lower())
        if value < SUGGESTIVE_MIN_AGE:
            return True
    return False


def _named(field: str, text: str) -> bool:
    """A capitalised name after the first word (R18): any in activity, outfit and expression;
    in setting and shows, which may name a place, two in a row (a full name)."""
    caps = [_CAPITAL.match(w) is not None for w in _WORD.findall(text)[1:]]
    if field in ("activity", "outfit", "expression"):
        return any(caps)
    return any(a and b for a, b in zip(caps, caps[1:]))

_TEXT_FIELDS = ("setting", "activity", "outfit", "expression", "shows")
_LUST_TEXT_FIELDS = ("activity", "shows")  # the rest are her picks
_ENUMS = {"kind": KINDS, "framing": FRAMINGS, "when": WHENS, "audience": AUDIENCES}
OTHER_STYLE = "scene: picks for another style"

_LIGHT = {"morning": "soft morning light", "day": "daylight", "evening": "warm evening light",
          "late": "at night, lit by a lamp or a screen", "any": None}
_LATE_TAIL = _PHONE_TAIL.replace("natural light, ", "")
_CROPS = {"face": (0.0, 0.0, 1.0, 1.0), "head_and_shoulders": (0.125, 0.05, 0.875, 0.8),
          "upper_body": (0.25, 0.05, 0.75, 0.55)}

PHASES = {"morning": ("morning",), "day": ("day",), "evening": ("evening",),
          "late": ("late", "dead"), "any": ("morning", "day", "evening", "late", "dead")}
LIFE_DAYS = 3
EVERYDAY_DAYS = 5
GUILD_DAYS = 3
BIRTHDAY_S = 64800.0
_DAY_S = 86400.0
_LASTS = {"life": LIFE_DAYS * _DAY_S, "raid": GUILD_DAYS * _DAY_S,
          "everyday": EVERYDAY_DAYS * _DAY_S, "birthday": BIRTHDAY_S}


@dataclass(frozen=True)
class Scene:
    kind: str
    framing: str
    setting: str
    activity: str
    outfit: str
    expression: str
    when: str
    audience: str
    shows: str
    style: str = PLAIN  # LUST: setting, outfit and expression were her picks (R2)
    recipe: int = 0  # missing on pre-enum rows; snapstage holds them instead of rendering prose


def scene_from(data: dict) -> Scene:
    """The Scene a screened reply stands for, or the one scene_json stored. A lust reply (it has
    a colour) is composed as her profile photo is (kind.compose_lust: the colour put into the
    outfit, an article in front) and is of the lust style; a stored one says its style."""
    # Old rows store already composed prose. New replies have a prop key and enum picks.
    if "prop" in data:
        data = compose_choices(data)
    outfit, style = data["outfit"], data.get("style", PLAIN)
    if "colour" in data:
        outfit, style = compose_lust({f: data[f] for f in (
            "kind", "framing", "setting", "outfit", "colour", "expression")}).outfit, LUST
    if style not in STYLES:
        raise ValueError("scene: unknown style")
    return Scene(**{f.name: data[f.name] for f in fields(Scene)
                    if f.name not in ("outfit", "style", "recipe")}, outfit=outfit, style=style,
                 recipe=data.get("recipe", 0))


def scene_json(s: Scene) -> dict:
    """What a snap row stores: the scene's fields, and its style only when it is the lust one
    (a plain scene keeps the plain shape)."""
    out = asdict(s)
    if s.style == PLAIN:
        del out["style"]
    if not s.recipe:
        del out["recipe"]
    return out


def scene_schema(p: Persona | None) -> dict:
    """The reply schema for p's scene: only enum choices, under the member's style."""
    return SCENE_SCHEMA_LUST if style_of(p) == LUST else SCENE_SCHEMA


def scene_system(p: Persona | None, data_dir: Path, *, human: HumanIdentity, name: str,
                 card: str, facts: str, looks: str, about: str) -> str:
    """The scene request's system prompt for p under p's style: the owner's overlay
    prompts/nightly_scene_lust.txt or SCENE_PROMPT_LUST for a lust woman, else
    prompts/nightly_scene.txt or SCENE_PROMPT."""
    if style_of(p) == LUST:
        return system_prompt(SCENE_LUST_NAME, SCENE_PROMPT_LUST, data_dir, human=human,
                             name=name, card=card, facts=facts, looks=looks, about=about)
    return system_prompt(SCENE_NAME, SCENE_PROMPT, data_dir, human=human, name=name, card=card,
                         facts=facts, looks=looks, about=about)


def _any(text: str, *patterns: re.Pattern) -> bool:
    return any(p.search(text) is not None for p in patterns)


def _text_codes(field: str, text: str, *, ident: HumanIdentity, names: list[str]) -> list[str]:
    text = " ".join(text.split())  # R16: a run of spaces, tabs or newlines is one space
    found = [("meta", has_meta(text)), ("real_name", names_real_name(text, ident)),
             ("likeness", _any(text, LIKENESS_RE, LIKENESS_MORE_RE) or _named(field, text)),
             ("member_name", any(has_word(text, n) for n in names)),
             ("era", _any(text, ERA_RE, ERA_MORE)),
             ("explicit", _any(text, EXPLICIT_RE, WEAR_RE, SWIM_RE, SEXUAL_RE)),
             ("minor", _any(text, MINOR_RE, MINOR_MORE_RE)),
             ("looks", _any(text, LOOKS_RE, LOOKS_MORE_RE)),
             ("age", _any(text, AGE_RE, AGE_MORE_RE) or _young_number(text)),
             ("undress", _any(text, UNDRESS_RE, UNDRESS_VERB_RE)),
             ("other_person", _any(text, OTHER_PERSON_RE, _WITH_NAME))]
    return [f"scene.{field}: {code}" for code, hit in found if hit]


_NOBODY = HumanIdentity(guid=0, character_name="", nickname="", real_name="",
                        is_guild_master=False)


def screen_scene(p: Persona, data: dict, *, ident: HumanIdentity,
                 member_names) -> list[str]:
    """The problems with a scene reply for `p`, as codes `scene.<field>: <code>` only, never the
    text. Per free-text field, its whitespace folded, in order: meta, his real name
    (real_name), a likeness (a name after the first word too, _named), another member's name
    (member_name, any case), an era, explicit wear, swimwear or a sexual pose or tone
    (explicit), a minor, a looks change, anything that codes a person as young or an age under
    SUGGESTIVE_MIN_AGE (age), undressing or bathing (undress) and another person (other_person; a pet is fine).
    A clothed bed or bedroom scene is fine. The reply must be p's style's shape (OTHER_STYLE
    otherwise). A lust reply's free text is its activity and shows; beside a swimwear or gym
    pick it never names a bed or the indoors (INDOOR_RE, reported as that pick's pairing); its
    picks meet kind.screen_lust: on its list, the pairings, her name in a part, the age guard
    (its `photo.` codes reported as `scene.`)."""
    lust = style_of(p) == LUST
    if lust != ("colour" in data):
        return [OTHER_STYLE]
    names = [n for n in member_names if n]
    codes = [f"scene.{f}: not on its list" for f, values in _ENUMS.items()
             if data.get(f) not in values]
    for field in _LUST_TEXT_FIELDS if lust else _TEXT_FIELDS:
        codes += _text_codes(field, data.get(field) or "", ident=ident, names=names)
    if lust:
        pairing = (PAIRINGS[0] if data.get("outfit") in SWIM_OUTFITS else
                   PAIRINGS[1] if data.get("outfit") in GYM_OUTFITS else None)
        if pairing and any(INDOOR_RE.search(" ".join((data.get(f) or "").split()))
                           for f in _LUST_TEXT_FIELDS):
            codes.append("scene" + pairing[len("photo"):])
        if data.get("setting") not in SCENE_LUST_SETTINGS:
            codes.append("scene.setting: not on its list")
        picks = {f: data.get(f) for f in ("kind", "framing", "setting", "outfit", "colour",
                                          "expression")}
        codes += ["scene" + m[len("photo"):] if m.startswith("photo") else m
                  for m in screen_lust(p, picks)]
    return list(dict.fromkeys(codes))


def scene_renderable(p: Persona | None, s: Scene) -> str | None:
    """None when s can be rendered for p, else why not: renderable's reasons, then a lust scene
    while she no longer has the lust style (LUST_HELD: it is never rendered plainly) or while
    her name filter would drop its joined phrase (LUST_NAMED, a rename since)."""
    missing = renderable(p)
    if missing:
        return missing
    if s.style == LUST and not suggestive(p):
        return LUST_HELD
    if UNDRESS_RE.search(s.setting):
        return "scene.setting: undress"  # also hold older planned bathing-context rows
    named = names_re(p)
    if s.style == LUST and named is not None and named.search(lust_phrase(s)):
        return LUST_NAMED
    return None


def build_scene_prompt(p: Persona, s: Scene) -> PromptSpec:
    """The prompt for scene s with p's face, in this order: kind, framing, subject (0), the
    activity (1), the setting (1), hair and skin (1), looking at the camera (3, not a snapshot),
    eyes and face details (2), the outfit (2), the light of its time of day (2), the expression
    (3), the build as a figure (1, prompt.BUILD_WORDS) and the phone look (1, without natural light late at night). A lust
    scene's outfit and setting go in as one priority-0 phrase where the setting stands, with
    SNAP_NEGATIVE_LUST. Free text holding the persona's names is dropped. ValueError when
    scene_renderable says why not."""
    missing = scene_renderable(p, s)
    if missing:
        raise ValueError(missing)
    lust = s.style == LUST
    name_re = names_re(p)
    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)
    add(_KINDS[s.kind], 0)
    add(_FRAMINGS[s.framing], 0)
    add(*subject_phrase(p))
    add(s.activity, 1, True)
    if lust:  # one phrase: the fit keeps both or neither; scene_renderable checked the names
        add(lust_phrase(s), 0, True)
    else:
        add(s.setting, 1, True)
    out += hair_skin(p, name_re)
    if s.kind != "snapshot":
        add("looking at the camera", 3)
    out += eyes_face(p, name_re)
    if not lust:
        add(s.outfit, 2, True)
    if _LIGHT[s.when]:
        add(_LIGHT[s.when], 2)
    add(s.expression, 3, True)
    add(BUILD_WORDS.get(p.appearance.build, f"{p.appearance.build} build"), 1)
    add(_LATE_TAIL if s.when == "late" else _PHONE_TAIL, 1)
    return PromptSpec(tuple(out), SNAP_NEGATIVE_LUST if lust else SNAP_NEGATIVE)


def build_tile_prompt(p: Persona) -> PromptSpec:
    """A member's tile of the guild photo: alone at their desk at
    night with a headset, in what they wear at the computer. The same for every style.
    ValueError when renderable says why not."""
    missing = renderable(p)
    if missing:
        raise ValueError(missing)
    name_re = names_re(p)
    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)
    add(TILE_FRAMING, 0)
    add(*subject_phrase(p))
    # Do not let the fitter trade this member's features for generic desk decoration.
    # If all required words exceed the encoder window, refuse instead of dropping identity.
    out += [(text, 0) for text, _ in looks_phrases(p, name_re)]
    add(GAME_MONITOR, 0)
    add(TILE_DESK, 1)
    if not _text_codes("outfit", p.appearance.at_the_pc or "", ident=_NOBODY, names=[]):
        add(p.appearance.at_the_pc, 2, True)  # the scene screen's free-text checks (R22)
    add("looking at the screen, smiling", 3)
    add(_HEADSHOT_TAIL, 1)
    return PromptSpec(tuple(out), SNAP_NEGATIVE)


def ref_crop(framing: str) -> tuple[float, float, float, float]:
    """The square of the chosen photo the identity reference is cut from, as fractions (left,
    top, right, bottom): the whole of a face photo, the face of a wider one, top-aligned."""
    return _CROPS[framing]


def window(source: str, night_end: float) -> tuple[float, float]:
    """(not_before, expires) for a photo planned from `source` the night that ends at
    `night_end`: from then, for 3 days (life, raid), 5 days (everyday) or 18 hours (birthday)."""
    if source not in _LASTS:
        raise ValueError(f"unknown photo source {source!r}")
    return night_end, night_end + _LASTS[source]
