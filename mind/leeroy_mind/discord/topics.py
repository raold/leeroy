"""What each guild Discord channel is about, in words a prompt and a guard can use ([server]
channel_topics, default true; owner, 2026-11-21: "the discord channels like #memes are not topical
at all its just random prattling").

Each layout channel (discord/layout.py) carries its purpose: `about`, what it is about, and
`not_here`, what does not belong there; #general takes anything and has no `not_here`.

- `hold_text`: the one line a channel turn is held to ("Stay on what this channel is about
  (...): ..."), as talk/wowtopic.py's hold line words a WoW turn's; None for #general and a
  channel the layout lacks.
- `off_topic`: a draft clearly off its channel's purpose, as the guard code it earns (`CODE`,
  `MEME_CODE`) or None. Lexical and deliberately narrow: a draft holding any word of its
  channel's subject is on it; one is off only when it brings in OFF_MIN or more distinct words of
  another subject that the message it answers did not bring, or one in a message of LONG_WORDS
  words or more (a word the game and everyday English share, "fire", "kill", "party", never
  counts against a draft). #memes is stricter: an opener without a picture must be a text meme
  (`meme_format`), and a reply runs at most MEME_WORDS words and brings in no real-life, food or
  calendar talk of its own. Replayed read-only on the 3,001 member posts outside DMs of
  2026-11-12..21 it flags 18 of 32 in #memes, 21 of 206 in #food, 22 of 699 in #irl, 3 of 334 in
  #loot-and-dkp and none in #general (88 in all; server/README.md).
- `subjects`: the content channels a text's words fit (food, pets, professions and gold, PvP,
  achievements and mounts, groups and alts), for routing a conversation's subject
  (server/topics.py); `real_life` and `game` for the rest of it.

Pure: no clock, no I/O, no logging; never logs or stores text."""
from __future__ import annotations

import re

from leeroy_mind.talk.wowtopic import _LIFE

CODE = "channel_topic"
MEME_CODE = "channel_meme"
HINT = ("it strays from what this channel is about: keep to the channel's subject, as the "
        "instruction's \"Stay on\" line says")
MEME_HINT = ("this is #memes: post a meme in a meme format (\"POV: ...\", \"nobody: ... me: ...\", "
             "\"me when ...\", \"... be like\"), or answer the meme posted in a few words, never "
             "plain chat")
OFF_MIN = 2  # distinct words of another subject, none of its own: clearly off
LONG_WORDS = 12  # ...or one, in a message this long (more than a quick reaction)
MEME_WORDS = 20  # a reply in #memes is a reaction: at most this many words
HOLD = "Stay on what this channel is about ({about}): {not_here}."
GENERAL = "general"
# A hold keeps her life out of a channel turn but where her life is the subject
HER_LIFE = frozenset({"irl"})


def _words(pattern: str) -> str:
    return r"\b(?:" + pattern + r")\b"


def _any(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


# Real life: talk/wowtopic.py's (never a word the game uses too: "the boss", "cat form", "my
# pet"), and a few more of the same kind
LIFE_P = _LIFE.pattern + "|" + _words(
    r"my boss|manager|coworkers?|co-workers?|commute|hospital|doctor|dentist|appointment|"
    r"errands?|laundry|groceries|rent|bills|paycheck|overtime|nap|vacation|road trip|airport|"
    r"hangover|gym|workout|car|traffic|my (?:sister|brother|son|daughter|partner)")
FOOD_P = _words(
    r"food|foods|eat|eats|eating|ate|eaten|cook|cooks|cooking|cooked|bake|baked|baking|dinner|"
    r"lunch|breakfast|brunch|snacks?|recipes?|meals?|hungry|starving|pizza|pasta|burgers?|tacos?|"
    r"sushi|curry|soup|salad|sandwich(?:es)?|chili|steak|chicken|bbq|grilled|grilling|fries|"
    r"cheese|bread|cake|cookies?|pie|dessert|chocolate|ice cream|coffee|tea|wine|beer|takeout|"
    r"takeaway|restaurant|kitchen|oven|microwave|leftovers|spicy|delicious|tasty|yum|yummy|"
    r"craving|cravings|flavou?r|sauce|noodles?|ramen|rice|bacon|eggs?|pancakes?|waffles?|cereal|"
    r"feasts?|donuts?|doughnuts?|cupcakes?|brownies?|stew|casserole|lasagna|burritos?|nachos|"
    r"smoothie|latte|espresso|popcorn|candy|toast|jam|brew|butter|bagels?|muffins?|pastry|"
    r"porridge|oatmeal|yogh?urt|fruit|veggies|vegetables|potato(?:es)?|onions?|garlic|"
    r"tomato(?:es)?|milk|juice|soda|fridge|dishes|tea bags?|cuppa|groceries|takeouts?")
PETS_P = _words(
    r"pets?|dogs?|puppy|puppies|pup|cats?|kittens?|kitty|vet|leash|walkies|fur|furry|paws?|"
    r"barks?|barking|meow|purr|purring|birds?|parrot|hamsters?|rabbits?|bunny|bunnies|aquarium|"
    r"fish tank|goldfish|horses?|companions?|vanity pets?|wagging")
# The game's content channels' own words, none of them everyday English
GOLD_P = _words(
    r"gold|auctions?|auction house|crafting|crafted|professions?|farming|farmed|herbs|"
    r"herbalism|mining|ore|mats|enchanting|enchants?|alchemy|flasks?|potions?|elixirs?|"
    r"transmutes?|tailoring|jewelcrafting|inscription|glyphs?|blacksmithing|leatherworking|"
    r"engineering|gold cap|dailies|daily quests?|frost lotus|saronite|titanium|eternium|"
    r"frostweave")
PVP_P = _words(
    r"pvp|arenas?|bgs?|battlegrounds?|wintergrasp|wg|2v2|3v3|5v5|gank|ganked|ganking|warsong|"
    r"wsg|arathi basin|alterac|eye of the storm|eots|strand of the ancients|sota|"
    r"isle of conquest|duels?|dueling|resilience|call to arms")
COLLECT_P = _words(
    r"achievements?|achievs?|cheevos?|mounts?|drakes?|proto-drakes?|reins|mammoth|rare spawns?|"
    r"glory of the|loremaster|exalted|reputation|tabards?|kingslayer")
LFG_P = _words(
    r"lfg|lfm|alts?|toons?|levell?ing|levell?ed|dungeons?|heroics?|hcs?|5-?mans?|"
    r"dungeon finder|rdf|reroll")
# The guild's own game, by words no one uses outside it (never "fire", "kill", "party", "wow")
GAME_P = "|".join((_words(
    r"raids?|raiding|raided|wipes?|wiped|wiping|dkp|loot|looted|looting|bis|ilvl|item level|"
    r"talents?|rotations?|cooldowns?|cds|aggro|lockouts?|oom|brez|debuffs?|hit cap|addons?|dbm|"
    r"warcraft|azeroth|dalaran|orgrimmar|stormwind|northrend|horde|icc|naxx|naxxramas|ulduar|"
    r"toc|onyxia|sartharion|archavon|lich king|arthas|kel'?thuzad|sindragosa|putricide|"
    r"saurfang|marrowgar|deathwhisper|festergut|rotface|lana'?thel|valithria|gunship|"
    r"yogg-saron|yogg|mimiron|thorim|hodir|freya|algalon|ignis|kologarn|auriaya|razorscale|"
    r"anub'?arak|jaraxxus|gormok|heigan|loatheb|patchwerk|grobbulus|gluth|thaddius|sapphiron|"
    r"maexxna|paladins?|pally|pallys|death knights?|dks?|shamans?|shammys?|mages?|warlocks?|"
    r"druids?|boomkins?|healers?|tanking|dps|parses?|offspec|mainspec|raid night|signups?|"
    r"benched|roster|lineup|trash pulls?"),
    r"\{\{item:\d+\}\}", r"\[[^\[\]\n]{2,60}\]", GOLD_P, PVP_P, COLLECT_P, LFG_P))
# ...and the words it shares with everyday English, which count only for a draft (never against)
GAME_ANY_P = GAME_P + "|" + _words(
    r"pulls?|pulled|pulling|bosse?s?|tanks?|heals?|healing|gear|geared|specs?|threat|trash|"
    r"instance|reset|guild|officers?|mana|rez|buffs?|gems?|procs?|crit|haste|strats?|strategy|"
    r"mechanics?|phase|enrage|adds|kite|kiting|taunt|interrupt|logs?|bench|kills?|killed|downed|"
    r"progression|items?|drops?|dropped|tokens?|ms|os|roll|rolled|ret|prot|holy|disc|shadow|"
    r"frost|fire|arcane|unholy|blood|resto|enhance|elemental|affliction|destro|demo|arms|fury|"
    r"marks|bm|survival|combat|assassination|subtlety|ferals?|hunters?|rogues?|priests?|"
    r"warriors?|locks?|farm|rating|honor|titles?|rep|groups?|runs?|party|queues?|queued|"
    r"invites?|wow|class|classes|bids?|bidding")
OFFICER_P = _words(
    r"ranks?|promote|promoted|promotion|demote|demoted|trials?|applicants?|applications?|apps?|"
    r"recruit|recruiting|recruitment|attendance|kick|gkick|members?|guild bank|bank|requests?|"
    r"poll|vote|meeting|rules?|warning|loot council|policy")
CALENDAR_P = _words(
    r"calendar|signups?|signed up|sign up|schedule|scheduled|noted|reminder|rsvp|meeting|"
    r"appointment|next week|tomorrow at|tonight at")
PICS_P = _words(
    r"pics?|pictures?|photos?|photograph|shot|shots|screenshots?|selfies?|camera|view|views|"
    r"sunset|sunrise|lighting|light|colou?rs?|gorgeous|pretty|beautiful|stunning|cute|adorable|"
    r"look|looks|looking|wallpaper|scenery|landscape|beach|mountains?|sky|filter")

LIFE, FOOD, PETS = _any(LIFE_P), _any(FOOD_P), _any(PETS_P)
GOLD, PVP, COLLECT, LFG = _any(GOLD_P), _any(PVP_P), _any(COLLECT_P), _any(LFG_P)
GAME, GAME_ANY, CALENDAR = _any(GAME_P), _any(GAME_ANY_P), _any(CALENDAR_P)
REAL_LIFE = _any(LIFE_P, FOOD_P)

# A channel's own subject (any word of it: the draft is on it), and the words that clearly are
# another channel's (OFF_MIN of them, none of its own: off)
_OWN: dict[str, tuple[re.Pattern, re.Pattern]] = {
    "irl": (_any(LIFE_P, FOOD_P, PETS_P), GAME),
    "food": (FOOD, _any(GAME_P, LIFE_P)),
    "pets": (PETS, _any(GAME_P, FOOD_P)),
    "pics": (_any(PICS_P), _any(GAME_P, CALENDAR_P)),
}
OFFICERS = frozenset({"officer-chat", "applications", "bank-requests"})
_OFFICERS = (_any(GAME_ANY_P, OFFICER_P), REAL_LIFE)
_WOW = (GAME_ANY, _any(LIFE_P, FOOD_P, PETS_P))  # raiding, the classes, PvP, gold, mounts, alts
_MEMES_OFF = _any(LIFE_P, FOOD_P, CALENDAR_P)

# A text meme's frame: how it opens ("POV: ...", "nobody: / me: ...", "me when ...", "when ...",
# "tfw ...") or a format it uses anywhere ("... be like", "... has entered the chat")
_MEME_OPEN = re.compile(
    r"^\W{0,3}(?:pov\b|nobody\s*:|me\s*:|me (?:when|after|before|trying)\b|when\b|tfw\b|mfw\b|"
    r"that (?:moment|feeling) when\b|how it started\b|average\b|ah yes\b|plot twist\b|"
    r"breaking(?: news)?\s*:|day \d+\b|expectations?\s*:|reality\s*:|"
    r"the (?:raid|tank|tanks|healers?|guild|boss|dps)\s*:|\*[a-z])", re.IGNORECASE)
_MEME_ANY = re.compile(
    r"\b(?:be like|starter pack|has (?:entered|left) the (?:chat|raid|building)|also me\s*:|"
    r"nobody\s*:|how it'?s going|this is fine|top text|bottom text|we have .{1,40} at home|"
    r"(?:it'?s|its) free real estate|i'?m in this (?:photo|picture))|"
    r"\w+\s*:\s.{2,80}?\b(?:me|us|also me|the raid|the tank|the healers?)\s*:",
    re.IGNORECASE | re.DOTALL)


def _layout(cid: str):
    from leeroy_mind.discord import layout
    return layout.channel(cid)


def hold_text(cid: str) -> str | None:
    """The hold line of layout channel `cid` (its `about` and `not_here`); None for #general
    (anything goes) or a channel the layout lacks."""
    ch = _layout(cid)
    if ch is None or not ch.not_here or not ch.about:
        return None
    return HOLD.format(about=ch.about, not_here=ch.not_here)


def _terms(pattern: re.Pattern, text: str) -> set[str]:
    return {" ".join(m.group(0).casefold().split()) for m in pattern.finditer(text or "")}


def meme_format(text: str) -> bool:
    """`text` is a text meme: it opens with a meme's frame or uses a meme's format."""
    text = (text or "").strip()
    return bool(_MEME_OPEN.search(text) or _MEME_ANY.search(text))


def off_topic(cid: str, text: str, *, parent: str = "", opener: bool = False,
              photo: bool = False) -> str | None:
    """The guard code a draft for layout channel `cid` earns for straying from the channel's
    purpose (the module docstring), or None. `parent`: the message it answers ('' for an opener);
    `photo`: the draft goes out with a picture (a meme image, a photo), which is the post."""
    ch = _layout(cid)
    if ch is None or cid == GENERAL or ch.category == "info":
        return None
    if cid == "memes":
        if opener:
            return None if photo or meme_format(text) else MEME_CODE
        if len((text or "").split()) > MEME_WORDS:
            return MEME_CODE
        return MEME_CODE if _terms(_MEMES_OFF, text) - _terms(_MEMES_OFF, parent) else None
    own, other = _OWN.get(cid) or (_OFFICERS if cid in OFFICERS else _WOW)
    if own.search(text or ""):
        return None
    brought = len(_terms(other, text) - _terms(other, parent))
    long = len((text or "").split()) >= LONG_WORDS
    return CODE if brought >= OFF_MIN or brought and long else None


# The content channels a subject's words can take it to, in this order of preference
SUBJECT_CHANNELS = (("food", FOOD), ("pets", PETS), ("professions-and-gold", GOLD),
                    ("pvp-arena", PVP), ("achievements-and-mounts", COLLECT),
                    ("lfg-and-alts", LFG))


def subjects(text: str) -> tuple[str, ...]:
    """The content channels whose subject `text` names (SUBJECT_CHANNELS' order)."""
    return tuple(cid for cid, pattern in SUBJECT_CHANNELS if pattern.search(text or ""))


def real_life(text: str) -> bool:
    """`text` names her real life: her work, home, family, meals, sleep..."""
    return bool(REAL_LIFE.search(text or ""))


def game(text: str) -> bool:
    """`text` names the game by a word only the game uses (raids, loot, a boss, a class,
    professions, PvP, groups and alts)."""
    return bool(GAME.search(text or ""))


__all__ = ["CODE", "HER_LIFE", "HINT", "MEME_CODE", "MEME_HINT", "MEME_WORDS", "OFF_MIN", "game",
           "hold_text", "meme_format", "off_topic", "real_life", "subjects"]
