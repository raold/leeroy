"""His game questions (fix wave part E): is his line one, what is it about and so which library
kinds answer it, the raider shorthand in it written out for the search ("icc" -> "Icecrown
Citadel"), the names it holds (the relevance gate's anchors, talk/facts.py), and who in the
guild knows the topic best. The names and the shorthand are talk/names.py's.

Shorthand that is also a word or slang (names.WORDLIKE: "hol up", "this boss is pos", "back up
the dbs") neither makes a line a game question nor is written out unless it is the line's game
term (_slang).

Chat is read by the shape of the whole line (fix round 4). A clause that asks about the guild's
people or its night ("who pulled marrowgar", "who ran icc yesterday", "where are you guys", "did
anyone get the bracers", "how did the attempts go") asks nothing of the game: the line is a game
question only when the rest of it is one. A "who" question asks for one of the guild unless it
asks for one of the game: who drops, sells, gives or teaches something, who someone of the lore
is, or who did a deed of the lore ("who killed arthas"); a time or a guild word ("tonight", "last
night", "in our guild") always makes it the guild's. An invitation or a call for people is a plan
("anyone up for ony later?", "icc 10 tonight, need a tank, anyone?") unless the line asks
something of the game besides. Why: a chat line taken for a game question keeps the facts of the
raid or boss it names, and the scholar answers it; a missed game question only goes without
facts. Every rule keeps tests/question_corpus.py green."""
import re
from dataclasses import dataclass
from typing import Sequence

from leeroy_mind.library.core import loot_target
from leeroy_mind.library.wiki import SEEDS
from leeroy_mind.personas.schema import Persona
from leeroy_mind.talk import names as N
from leeroy_mind.talk.names import ALIASES, CLASS_NAMES, bare, game_thing, names_in, words_re
from leeroy_mind.talk.select import is_quest_talk, quest_helper
from leeroy_mind.text import strip_wow_markup

__all__ = ["ALIASES", "DETECTOR_WORDS", "FIT_TIERS", "GameQuestion", "SCHOLAR_TITLE",
           "TOPIC_KINDS", "expand", "fit_tier", "game_question", "is_plan", "names_in"]


@dataclass(frozen=True, slots=True)
class GameQuestion:
    """topic: what the line asks about (TOPIC_KINDS' keys). kinds: the library chunk kinds
    that answer it, best first. query: his line with its markup gone (links as [Name]), its
    shorthand written out (expand) and its names spelt as the list spells them. names: the
    bosses, places, raids, items, events, people and factions it names (names.names_in)."""
    topic: str
    kinds: tuple[str, ...]
    query: str
    names: tuple[str, ...] = ()


TOPIC_KINDS: dict[str, tuple[str, ...]] = {
    "item": ("item", "loot"),
    "loot": ("loot", "item"),
    "mount": ("item", "event", "loot"),
    "reputation": ("loot", "lore", "quest"),
    "trainer": ("item", "zone", "lore", "quest"),
    "profession": ("item", "quest", "lore", "zone"),
    "quest": ("quest", "event", "zone", "lore"),
    "lore": ("lore", "event", "zone"),
    "strategy": ("strategy",),
    "class": ("class",),
    "achievement": ("event", "strategy", "lore"),
    "zone": ("zone", "lore", "event"),
}


def _plain(*phrases: str) -> str:
    """One alternation of whole words or phrases as written, longest first."""
    return "|".join(sorted({re.escape(p) for p in phrases}, key=len, reverse=True))


def _any(*phrases: str) -> re.Pattern:
    """The phrases as whole words in a lower-case line, in any of their spellings (names.flex:
    "lanathel", "valkyr twins", "jormungars")."""
    return re.compile(r"\b(?:" + words_re(*phrases) + r")\b")


# What a line may open with before its question ("lol", "ok so hey guys", "bruh").
_OPENERS = ("lol", "lmao", "omg", "haha", "ok", "okay", "so", "hey", "yo", "wait", "ugh", "and",
            "but", "also", "um", "uh", "hmm", "btw", "guys", "srsly", "seriously", "honestly",
            "dude", "bro", "bruh", "jeez", "man", "ffs", "welp", "well", "then", "quick q",
            "quick question", "qq")
_OPENER_WORDS = frozenset(w for o in _OPENERS for w in o.split())
# A question: a question mark, a question word first (after an opener), or asking for help or
# for someone who knows.
_ASK = re.compile(
    r"\?|^(?:(?:" + _plain(*_OPENERS) + r")\W+)*"
    r"(?:who|whos|who's|what|whats|what's|where|wheres|where's|when|why|how|hows|how's|"
    r"which|is|are|does|do|did|can|could|should|would|will|any|anyone|anybody|any1|someone|"
    r"some1|tips?)\b"
    r"|\b(?:anyone|anybody|any1|someone|some1|u|you|ya) (?:know|knows|got|have)\b"
    r"|\bhelp (?:me|with)\b|\bstuck (?:on|at|in|with)\b|\bneed (?:help|a hand|tips)\b"
    r"|\b(?:where|how|what|which|who|why) (?:do|does|did|can|is|are|was|were|should)\b")
_LINK = re.compile(r"\|H(?:item|spell|enchant):", re.IGNORECASE)
_QUEST_LINK = re.compile(r"\|Hquest:", re.IGNORECASE)
_BRACKETED = re.compile(r"\[[^\]\n]{2,60}\]")

_INSTANCE = _any(*N.INSTANCE_WORDS)
_BOSS = _any(*N.BOSS_WORDS)
_PLACES = _any(*N.PLACE_WORDS, *(bare(n).casefold() for n in SEEDS["zone"]))
_LORE_NAMES = _any(*N.LORE_WORDS, *(bare(n).casefold() for n in SEEDS["lore"]))
_FACTIONS = _any(*N.FACTION_WORDS)

# -- people and chat: the clauses that ask nothing of the game ----------------------------------

# A time or a guild word from the question's word on: the line is about the guild's night, not
# the game's story ("who ran icc yesterday", "who killed arthas first in our guild"); before it,
# it only says whom he asks ("do you guys know who killed arthas?"). Never "now" or "first"
# alone: "who is the lich king now?" and "who was the first lich king?" are the lore.
_CUE = re.compile(
    r"\b(?:yesterday|today|tonight|tonite|tomorrow|tmrw|earlier|later|yet|so far|"
    r"(?:this|last|next)\s+(?:night|week|weekend|morning|evening|time|reset|lockout|raid|run)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)days?|our|ours|we|us|guild|guildies|in here|"
    r"(?:on|in)\s+(?:this|the|our)\s+(?:server|realm)|you guys|u guys|y'?all|you all|of you)\b")
_NOW = re.compile(r"\b(?:tonight|tonite|today|tomorrow|tmrw|later|now|this\s+(?:week|weekend|"
                  r"reset|run))\b")
# An ask inside a people clause makes it a question of the game: "anyone who knows where the
# entrance is?", "did anyone figure out how to beat dbs?", "did anyone figure out the trick to
# putricide's ooze?".
_EMBEDDED_ASK = re.compile(r"\b(?:how|what|whats|what's|where|wheres|where's|which|why|"
                           r"explain|explaining|strats?|strategy|tactics?|tips|mechanics?|"
                           r"tricks?\s+(?:to|for))\b")
# In a "who" question a wh-word asks only after a verb of knowing or telling ("who knows how
# defile works?", "who can tell me where the entrance is?"); anywhere else it is about the raid's
# people ("who's tanking what on lk tonight?", "who pulled marrowgar and why?"), and so is "when"
# ("who knows when we're pulling lk?"). A strat or tips word asks wherever it is ("who has the
# marrowgar strat?", "who can explain the lich king fight?").
_KNOWS_ASK = re.compile(r"\b(?:know|knows|knew|tell|explain|explaining|show|figured?|find|found|"
                        r"remember|remembers|understand|understands|sure)\b"
                        r"[^?!.;,]*?\b(?:how|what|whats|what's|where|wheres|where's|which|why)\b"
                        r"|\b(?:strats?|strategy|tactics?|tips|mechanics?|explain\w*|"
                        r"tricks?\s+(?:to|for))\b")
# A clause's question opens it: at most _OPENING words besides openers come before its question
# word ("lol ok so hey guys who pulled", "it was arkon who pulled"), none of them a question word
# ("how did you get exalted?" asks how), or "know" does ("does anyone know who pulled?"). A
# "who" after a person it describes opens a relative clause instead ("anyone who can tank
# marrowgar?", "a mage who stacks haste"), and one after a creature of the game asks nothing of
# the guild ("the npc who gives the dailies", "the boss who stands at the end of icc").
_OPENING = 4
_QWORDS = frozenset("what whats what's where wheres where's how hows how's which why when".split())
_ANTECEDENT = frozenset("""anyone anybody any1 someone somebody some1 everyone everybody every1
people ppl folks those them one ones person player players guy girl else""".split())
_NPC_ANTECEDENT = frozenset("npc npcs boss bosses mob mobs vendor trainer quartermaster".split())
_KNOW = re.compile(r"\b(?:know|knows|knew|wonder|idea|tell me|ask|asking|guess)\s*$")
_WHO = re.compile(r"\b(?P<who>who)(?P<tail>(?:'s|s|se|'re)?\b[^?!.;,]*)")
_LEFT = re.compile(r"\bleft\b")  # "who has dailies left?", "who left the raid?"
# Who in the game: who drops, sells, gives, starts or teaches it, who has a quest or the best
# loot, whom do i talk to, turn it in to, buy it from (a tag may end it: "who do i turn this
# quest in to lol?").
_TAGS = r"(?:\s+(?:again|lol|lmao|haha|tho|though|pls|plz|please|guys|rn|now))*"
_WHO_ACT = re.compile(r"\s+(?:drops?|sells?|vendors?|gives?|starts?|begins?|offers?|teaches|"
                      r"trains|hands?\s+(?:out|in))\b")
_WHO_NPC = re.compile(
    _WHO_ACT.pattern
    + r"|(?:\s+(?:has|have|had|got|carries)|'s\s+got|s\s+got)\b[^?!.;]*\b(?:quests?|dailies|daily|"
    r"loot\s*tables?|best\s+loot)\b"
    r"|\s+do\s+(?:i|you|u|we|one)\s+(?:talk|speak)\s+(?:to|with)\b"
    r"|\s+do\s+(?:i|you|u|one)\s+(?:turn|hand|give|bring|take|show|deliver|return|sell)\b.*"
    r"\b(?:in|to)" + _TAGS + r"\s*$"
    r"|\s+do\s+(?:i|you|u|one)\s+(?:buy|get|learn|train)\b.*\bfrom" + _TAGS + r"\s*$")
# Words that change nothing of who is asked for ("who exactly was arthas?", "who actually killed
# him?"); "who just pulled" and "who else is on" are the guild's.
_WHO_SO = re.compile(r"\bwho\s+((?:(?:exactly|actually|really|originally|initially|first)\s+)+)")
# Who someone is: the lore ("who was arthas", "who is the old god in ulduar"), unless what
# follows is a gerund, a word of being around or a raid role ("who's tanking", "who's on",
# "who's the tank for marrowgar").
_WHO_IS = re.compile(r"(?:'s|s|'re|\s+is|\s+was|\s+were|\s+are|\s+r)\s+(?P<c>.*)$")
# "who's" before a participle is "who has" ("who's pulled marrowgar?", "who's done heroic lk?"):
# the guild's, unless it is a deed of the lore ("who's imprisoned yogg-saron?").
_WHO_HAS = re.compile(r"(?:'s|s)\s+(?:just\s+|already\s+|ever\s+|never\s+)?(?P<c>(?:\w{3,}ed|done|"
                      r"seen|been|had|won|gone|taken|stood|hit|beaten|slain|run|led|made|built)\b"
                      r".*)$")
_PEOPLE_IS = re.compile(
    r"\w{2,}ing\b|(?:on|up|in|here|around|free|ready|afk|online|offline|there|awake|back|alive|"
    r"home|out|down|off|gonna|got|still|keen|interested|willing|available|able)\b"
    r"|(?:(?:the|our|your|ur|a|an|my|their|his|her)\s+)?(?:(?:best|top|most|new|main|off|other|"
    r"next|last|second|third)\s+)?(?:tanks?|healers?|heals?|dps|rl|raid\s*leader|"
    r"leader(?!\s+of)|ml|master\s*looter|loot\s*master|mt|ot|main\s*tank|off\s*tank|offtank|gm|"
    r"guild\s*master|officers?|pugs?|mvp|caller|kiter|soaker|interrupter|backup)\b")
# A deed of the lore: "who killed arthas", "who made frostmourne", "who built ulduar". A closed
# list: a verb not on it ("who soaks defile on lich king", "who just pulled marrowgar") is the
# guild's.
_PLACE_VERBS = (
    "built", "builds", "constructed", "made", "makes", "created", "creates", "designed",
    "founded", "formed", "forged", "sealed", "ruled", "rules", "governs", "governed", "controls",
    "controlled", "guards", "guarded", "owns", "owned", "raised", "destroyed", "discovered",
    "found", "established")
_LORE_VERBS = _plain(
    *_PLACE_VERBS, "killed", "kills", "slew", "slays", "murdered", "defeated", "beat",
    "vanquished", "banished", "imprisoned", "jailed", "trapped", "captured", "freed", "saved",
    "rescued", "corrupted", "betrayed", "cursed", "resurrected", "crafted", "stole", "led",
    "leads", "runs", "ran", "commanded", "commands", "became", "becomes", "replaced",
    "succeeded", "wields", "wielded", "sacrificed")
_LORE_DEED = re.compile(rf"\s+(?:{_LORE_VERBS})\b")
_WHO_DEED = re.compile(rf"\bwho\s+(?:{_LORE_VERBS})\b")
# A raid is the lore's only as what was built or ruled ("who built ulduar?"); leading, running
# or killing one is the guild's ("who leads icc?", "who ran icc?").
_PLACE_DEED = re.compile(rf"\s*(?:who\s+)?(?:{_plain(*_PLACE_VERBS)})\b")
# Where people are or meet: "where are you guys", "where's everyone at", "where in dalaran are
# you tonight?", "where do we meet for icc?". Never "where do we stack on marrowgar?".
_WHEN = r"(?:\s+(?:at|now|tonight|today|later|tomorrow|again|right now|rn))*\s*(?=[,.!?;]|$)"
_WHERE_PEOPLE = re.compile(
    r"\bwhere(?:'s|s|\s+is|\s+are|\s+r|'re)?\s+(?:all\s+)?(?:you guys|u guys|you lot|you|u|ya|"
    r"yall|y'all|everyone|everybody|every1|ppl|people|we|they|the group|the rest)" + _WHEN
    + r"|\b(?:are|r)\s+(?:you guys|you|u|ya|yall|y'all|ppl|people|they|we|he|she)" + _WHEN
    + r"|\bwhere\s+(?:should|shall|do|will|are|r|'re|can|could)\s+(?:we|you guys|u guys|you|u|"
    r"y'all|yall|everyone|ppl|people)\s+(?:meet|meeting|gather|gathering|hang|summon|wait|"
    r"waiting)\b")
# What someone did: "did anyone get the bracers from marrowgar?", "did we just one shot it?".
_DID_PEOPLE = re.compile(r"\bdid\s+(?:anyone|anybody|any1|someone|somebody|some1|you guys|"
                         r"u guys|you|u|ya|yall|y'all|we|everyone|everybody|ppl|people)\b"
                         r"(?P<tail>[^?!.;,]*)")
# Asking for a summon, a port or an invite is chat wherever it is to ("anyone got a summon to
# marrowgar?", "can someone summon me to marrowgar?", "can i get a port to dal?"); where to get
# a portal is a place question ("where do i get a portal to dal?", "anyone know where i can get
# a portal to dal?").
_REQUEST = re.compile(r"\b(?:got|have|has|give|get|need|want|do)\s+(?:me\s+|us\s+)?(?:a|an|any)\s+"
                      r"(?:summon|summons|sum|port|portal|invite|inv)\b"
                      r"|\b(?:summon|port|invite|inv)\s+(?:me|us|him|her|them)\b")
_PORTAL = re.compile(r"\bport(?:al)?s?\b")
_WHERE_I = re.compile(r"\bwhere\s+(?:(?:do|can|could|would|should)\s+(?:i|you|u|one)|"
                      r"(?:i|you|u|one)\s+(?:can|could|would|should))\s+$")
# When the guild raids: "what time are we pulling marrowgar?", "when are we doing lk?"; never
# "what time does the lich king enrage?".
_SCHEDULE = re.compile(
    r"\bwhat\s+time\b[^?!.;,]*\b(?:we|us|tonight|tomorrow|raid|you guys)\b"
    r"|\bwhen\s+(?:are|r|will|do|shall|should)\s+(?:we|you guys|u guys|y'all|yall|everyone)\s+"
    r"(?:doing|going|raiding|running|starting|meeting|pulling|start|go|raid|run|meet|do|pull)\b")
# How a night went: "how was the gunship?", "how did the festergut attempts go?", "how did you
# guys do on putricide?"; never "how was arthas corrupted?" (a participle: the lore) or "how did
# you do the gunship?" (the way to do it).
_PARTICIPLE = (r"\b(?:\w{3,}(?:ed|en)|made|born|built|done|slain|won|lost|taken|given|chosen|"
               r"known|forged|broken|stolen|beaten|torn|sworn|brought|bought|caught|taught|fought|"
               r"sought|sent|led|held|kept|found|bound|struck|begun|drawn|thrown|flown|grown|"
               r"shown|worn|borne|sunk|cast|set|put|split|spread)\b")
# A raid's participle about no one of the lore is the night too ("how was icc cleared so
# fast?"), never "how was arthas killed?".
_RAID_PARTICIPLE = re.compile(r"\b(?:cleared|killed|downed|done|beaten|won|lost|pulled|tanked|"
                              r"healed|wiped|pugged|farmed|carried|run|one-?shot(?:ted)?|"
                              r"handled|dealt with)\b")
_HOW_WAS = re.compile(r"\bhow\s+(?:was|were)\s+(?P<subject>[^?!.,;]*)")
_HOW_WENT = re.compile(
    r"\bhow(?:'d|\s+did)\s+(?:[^?!.;,]*?\bgo\b(?P<went>[^?!.;,]*)"
    r"|(?:you guys|u guys|you|u|ya|yall|y'all|we|everyone|they|it)\s+do\b(?P<did>[^?!.;,]*))")
_WENT_TAIL = re.compile(r"\s*(?:(?:for\s+(?:you guys|u guys|you|u|ya|y'all|yall|everyone|the raid)"
                        r"|last\s+night|tonight|today|yesterday|earlier|later|now|then|again|"
                        r"so\s+far|this\s+week)\s*)*$")
_DID_TAIL = re.compile(r"\s*(?:$|(?:on|in|at|against|vs|with|last|tonight|today|yesterday|"
                       r"earlier|so\s+far)\b)")


def _before(low: str, i: int) -> str:
    """His line's clause before position i (from the last mark or comma)."""
    return low[max(low.rfind(c, 0, i) for c in ",.!?;:") + 1:i]


def _opens(low: str, i: int) -> bool:
    """Whether the word at position i opens its clause's question (_OPENING)."""
    words = [w for w in _before(low, i).split() if w not in _OPENER_WORDS]
    return len(words) <= _OPENING and not _QWORDS.intersection(words)


def _clause(low: str, i: int) -> tuple[int, int]:
    """The clause of `low` around position i: from the mark or comma before it to the one after
    it, that one included."""
    a = max(low.rfind(c, 0, i) for c in ",.!?;:") + 1
    ends = [j for j in (low.find(c, i) for c in ",.!?;") if j >= 0]
    return a, (min(ends) + 1 if ends else len(low))


def _who_people(tail: str, after: str) -> bool:
    """Whether a "who" question (its words after "who" in `tail`, his line from it on in
    `after`) asks for one of the guild (the module docstring)."""
    if _KNOWS_ASK.search(tail):
        return False
    if _CUE.search(after) or _LEFT.search(tail):
        return True
    if _WHO_NPC.match(tail):
        return False
    if m := _WHO_HAS.match(tail):
        deed = " " + m.group("c")
        return not (_LORE_DEED.match(deed) and _of_the_lore(deed))
    if m := _WHO_IS.match(tail):
        return bool(_PEOPLE_IS.match(m.group("c")))
    return not (_LORE_DEED.match(tail) and _of_the_lore(tail))


def _of_the_lore(deed: str) -> bool:
    """Whether a deed's words name someone or somewhere of the lore: a person, a people, a place,
    or a raid as what was built or ruled ("who built ulduar", never "who leads icc" or "who ran
    ulduar", also when the raid's name is a zone's)."""
    other = _INSTANCE.sub(" ", deed)
    return bool(_LORE_NAMES.search(other) or _PLACES.search(other)
                or (_INSTANCE.search(deed) and _PLACE_DEED.match(deed)))


def _chat_spans(low: str) -> list[tuple[int, int]]:
    """The clauses of his line that ask about people or a night, not the game: a "who" question
    for one of the guild, where people are, what someone did, how a night went."""
    out = []
    for m in _WHO.finditer(low):
        tail, before = m.group("tail"), _before(low, m.start())
        last = before.split()[-1] if before.split() else ""
        if last in _NPC_ANTECEDENT:
            continue
        after = low[m.start():]
        if last in _ANTECEDENT:  # a relative clause: only it goes, from its "who"
            if not _EMBEDDED_ASK.search(tail) and (_CUE.search(after) or not _WHO_ACT.match(tail)):
                out.append((m.start(), _clause(low, m.start())[1]))
        elif ((_opens(low, m.start()) or _KNOW.search(before)) and _who_people(tail, after)):
            out.append(_clause(low, m.start()))
    out += [_clause(low, m.start()) for m in _WHERE_PEOPLE.finditer(low)]
    out += [_clause(low, m.start()) for m in _REQUEST.finditer(low)
            if not (_PORTAL.search(m.group()) and _WHERE_I.search(low[:m.start()]))]
    out += [_clause(low, m.start()) for m in _SCHEDULE.finditer(low) if _opens(low, m.start())]
    out += [_clause(low, m.start()) for m in _DID_PEOPLE.finditer(low)
            if _opens(low, m.start()) and not _EMBEDDED_ASK.search(m.group("tail"))]
    out += [_clause(low, m.start()) for m in _HOW_WAS.finditer(low)
            if _opens(low, m.start()) and (
                not re.search(_PARTICIPLE, s := m.group("subject")) or _CUE.search(low, m.start())
                or (_RAID_PARTICIPLE.search(s) and not _LORE_NAMES.search(s)))]
    out += [_clause(low, m.start()) for m in _HOW_WENT.finditer(low)
            if _opens(low, m.start()) and (
                _WENT_TAIL.match(m.group("went")) if m.group("went") is not None
                else _DID_TAIL.match(m.group("did")))]
    return out


_QUEST_DROP = re.compile(r"\b(?:drop|abandon|share|skip|delete)\s+(?:this|that|the|my|a|your)"
                         r"\s+quests?\b")
# Plans: an invitation or a call for people ("anyone want to do lk tonight?", "anyone up for
# ony later?", "need a tank for icc hc?", "icc 10 tonight, need a tank, anyone?"). A line that
# asks something of the game besides is no plan ("anyone going to icc know where the entrance
# is?").
_RAID_TARGET = (r"(?:(?:a|an|the|some|quick|fast|couple|few|another|more|10|25|heroic|hc|normal|"
                r"man|player|of)\s+)*(?:" + words_re(*N.INSTANCE_WORDS, *N.BOSS_WORDS)
                + r"|raids?|runs?|heroics?|dungeons?|arena|bgs?|battlegrounds?|weekly|vault)\b")
_SUBJECT = (r"\b(?:anyone|anybody|any1|someone|some1|everyone|who|whos|who's|u|you guys|you|ya|"
            r"yall|y'all|we|ppl|people)\s+(?:(?:still|else)\s+)*")
# Wanting to do something: a run, a raid, a trade ("anyone want to swap their trinket for my
# ring?").
_INVITE_DO = re.compile(
    _SUBJECT + r"(?:wants?\s+to|wanna(?:\s+to)?|(?:would|'d)\s+like\s+to|gonna|tryna|going\s+to|"
    r"trying\s+to)\s+(?:run|do|go|join|come|farm|queue|raid|grab|pug|clear|kill|duo|group|heal|"
    r"tank|hit|try|start|swap|trade|sell|buy)\b")
_INVITE_FOR = re.compile(
    _SUBJECT + rf"(?:(?:wants?\s+to\s+go\s+to|going\s+to|up\s+for|down\s+for|down\s+to|keen\s+for|"
    rf"free\s+for|in\s+for|feel\s+like|running|doing|raiding|pugging|clearing|heading\s+to)\s+"
    rf"{_RAID_TARGET}"
    r"|feel\s+like\s+(?:running|doing|going|farming|raiding|queueing|pugging|clearing|healing|"
    r"tanking)\b)")
# Whether a run is on: "is lk on tonight?", "is marrowgar up?"; never "is hol on heroic hard?".
_RUN_ON = re.compile(rf"\b(?:is|are)\s+{_RAID_TARGET}\s+(?:still\s+)?(?:on(?!\s+(?:heroic|hc|"
                     r"normal|nm|10|25|easy|hard)\b)|up|down|dead|alive|happening|planned|a go|"
                     r"cancell?ed|canceled|done|cleared|saved|open)\b")
# "do we want to kill the adds first on deathwhisper?" asks how to fight; "do you guys want to
# do naxx later?" invites: asked, it is a plan only when the run itself or a time follows.
_ASKED = re.compile(r"\b(?:do|does|did|will|would|should|shall|can|could)\s+$")
_RUN_AFTER = re.compile(rf"\s+{_RAID_TARGET}|\s*(?:[?!.,;]|$)|\s+(?:it|this|that|one|some|"
                        r"tonight|later|today|tomorrow|now|again|together|with)\b")
_CALL = re.compile(
    r"\b(?:needs?|lf|lfm|lfg|looking\s+for)\s+(?:\d+\s+|a\s+|an\s+|one\s+|two\s+|three\s+|"
    r"four\s+|five\s+|more\s+|another\s+|some\s+)?(?:more\s+)?(?:tanks?|healers?|heals?|healz|"
    r"dps|ppl|people|players?)\b")
# What a fight needs, asked ("does sindragosa need 2 tanks?", "do we need 3 healers for the
# lich king?"), is no call, unless it is for now ("do you guys need a tank for icc tonight?").
_ASKED_NEED = re.compile(r"\b(?:do|does|did|will|would|should|could|can|shall|if|whether)\s+"
                         r"(?:\S+\s+){0,4}$")
# Nor is how many a boss needs, with its "does" dropped ("marrowgar needs 2 tanks?", "lk need 3
# heals on 25?"), when that is all the line says. One more, a time, anyone, or anything after it
# (a call for people: "who's in", "wanna join?", "add me") forms a raid, so it is a call ("lk
# needs 1 more heal?", "lk needs 2 tanks, anyone?", "sindy needs 2 tanks? who's in").
_BOSS_NEEDS = re.compile(rf"\b(?:{words_re(*N.BOSS_WORDS)})\s+$")
_COUNT_NEED = re.compile(r"needs?\s+(?:\d+|two|three|four|five)\s+(?!more\b)")
_NEED_END = re.compile(r"(?:\s+(?:on|for|in))?(?:\s+(?:the|10|25|10m|25m|10-man|25-man|heroic|hc|"
                       r"normal|nm|man|player|mode))*(?:\s+(?:right|yeah|yes|correct|really|too|"
                       r"then|lol|tho|though))*\s*[?!.]*\s*$")
_CALLING = re.compile(r"\b(?:anyone|anybody|any1|ne1|someone|some1|pst|pm|whisper|inv|invite|lf|"
                      r"lfm|lfg)\b")
_ASKS = re.compile(r"\b(?:what|whats|what's|how|hows|how's|which|why|know|knows|tips?|strats?|"
                   r"strategy|tactics?|explain|explaining|tell|telling|drops?|loot|mechanics?)\b")
# A "where" asks only about a place of the game, never where people meet ("anyone up for icc?
# where's the entrance again?" asks; "where should we meet?" and "where's everyone at?" do not).
_WHERE_ASK = re.compile(r"\bwhere(?:'s|s)?\b(?P<c>[^?!.;]*)")
_WHERE_US = re.compile(r"\s*(?:(?:should|shall|do|will|would|are|r|'re|can|could|is|'s|s)\s+)?"
                       r"(?:we|you guys|u guys|you|u|ya|yall|y'all|everyone|everybody|every1|ppl|"
                       r"people|they)\b")
_GAME_PLACE = re.compile(r"\b(?:entrance|entry|portal|summoning stone|meeting stone|flight ?master|"
                         r"flight path|quartermaster|vendor|trainer|bank|auction house|inn|"
                         r"innkeeper|zeppelin|zep|boat|tram|instance|dungeon)s?\b")


def _asks(rest: str) -> bool:
    """Whether the rest of a plan's line asks something of the game."""
    if _ASKS.search(rest):
        return True
    clauses = (m.group("c") for m in _WHERE_ASK.finditer(rest))
    return any((_GAME_PLACE.search(c) or _PLACES.search(c) or _INSTANCE.search(c))
               and not _WHERE_US.match(c) for c in clauses)


def _plan(low: str, cut: bool = False) -> bool:
    """Whether his line is a plan (an invitation, a run that is on, a call for people); `cut`: a
    people clause was cut out of it, so a boss's need is a call (_NEED_END)."""
    for m in _INVITE_DO.finditer(low):
        if _ASKED.search(low[:m.start()]) and not _RUN_AFTER.match(low, m.end()):
            continue
        if not _asks(low[:m.start()] + " " + low[m.end():]):
            return True
    for m in (*_INVITE_FOR.finditer(low), *_RUN_ON.finditer(low)):
        if not _asks(low[:m.start()] + " " + low[m.end():]):
            return True
    for m in _CALL.finditer(low):
        if _ASKED_NEED.search(low[:m.start()]) and not _NOW.search(low):
            continue
        if (not cut and _BOSS_NEEDS.search(low[:m.start()]) and _COUNT_NEED.match(m.group())
                and _NEED_END.match(low, m.end())
                and not (_NOW.search(low) or _CALLING.search(low))):
            continue
        if not _asks(low[:m.start()] + " " + low[m.end():]):
            return True
    return False


# Loot: only the shapes of a loot question, never a bare "drop" ("did that drop?"). A token is
# the game's when the line says so ("tier tokens", a raid, a name, a class) or asks what to do
# with it, except on the "where do i" path, where an everyday noun needs a second cue ("where
# do i get tokens for the arcade?").
_LOOT = re.compile(
    rf"\b(?:what|which|anything)\b[^?]*\bdrops?\b|\bdrops?\s+(?:from|off|in)\b"
    rf"|\b(?:does|do|did|will|can)\s+(?:the\s+)?(?:{words_re(*N.BOSS_WORDS)})\s+drop\b"
    rf"|\bwhere\b[^?]*\bdrops?\b|\bwho\s+drops?\b|\bloot\s+(?:from|of|off)\b|\bloot\s*tables?\b"
    rf"|\b(?:bis|best in slot|emblems?)\b")
_TOKENS = re.compile(r"\btokens?\b")
_TOKEN_GAME = re.compile(r"\b(?:tier|t(?:7|8|9|10)|vanquisher|protector|conqueror|"
                         r"sanctification|heroic|hc|emblems?|badges?|triumph)\b")
_WHAT_ABOUT = re.compile(r"\b(?:what|which|how)\b")
_WHERE_DO_I = re.compile(r"\bwhere do i\b")
_ACHIEVEMENT = re.compile(
    r"\b(?:achievements?|achievs?|cheevos?|glory of the|realm first)\b"
    r"|\bhow (?:do|can|did|does) (?:i|you|u|we|one) get (?:the |that |this )?[\w' -]{0,30}"
    r"\btitles?\b")
_META = re.compile(r"\bmeta\b")
_MOUNT = re.compile(r"\b(?:mounts?|reins|drakes?|proto[- ]?drakes?|wyrms?|mammoths?|choppers?|"
                    r"motorcycles?|gryphons?|wyverns?|hippogryphs?|dragonhawks?|warhorses?|"
                    r"chargers?)\b")
_OBTAIN = re.compile(r"\b(?:get|gets|got|getting|obtain|drops?|dropped|from|farm|farming|buy|"
                     r"bought|worth|earn|spawn|spawns|spawned|find)\b")
# The game in a mount question (the re-review: "that charger for your phone", "my motorcycle
# license"): a mount only the game has, or where one comes from.
_MOUNT_GAME = re.compile(
    r"\b(?:reins|proto[- ]?drakes?|wyrms?|gryphons?|wyverns?|hippogryphs?|dragonhawks?|"
    r"mechanostriders?|hawkstriders?|talbuks?|elekks?|nether ?rays?|zhevras?|invincible|"
    r"mimiron'?s head|ashes of al'?ar|mekgineer'?s chopper|"
    r"(?:tundra|ice|wooly|grand|traveler'?s) mammoths?|"
    r"(?:blue|azure|bronze|black|red|twilight|plagued|violet|green|ironbound|rusted|time-lost|"
    r"albino|onyxian) (?:proto[- ]?)?drakes?|"
    r"drops?|dropped|vendors?|sold|sells|achievements?|meta|quartermaster|rep|reputation|"
    r"exalted|revered)\b")
_PROFESSION = re.compile(r"\b(?:" + _plain(
    "alchemy", "alchemist", "blacksmith", "blacksmithing", "enchanting", "enchanter",
    "engineer", "engineering", "engi", "herbalism", "herbalist", "herbing", "inscription",
    "scribe", "jewelcrafting", "jewelcrafter", "jc", "leatherworking", "leatherworker", "lw",
    "mining", "miner", "skinning", "skinner", "tailor", "tailoring", "cooking", "fishing",
    "first aid", "schematic", "schematics") + r")\b")
# Profession words that are everyday words too ("the formula for this spreadsheet", "any good
# dal recipes?"): only with something of the game. Materials are the game's when asked about
# ("what mats do i need for the frostsavage boots?", "how many mats for lionheart?").
_PROFESSION_WORDS = re.compile(r"\b(?:recipes?|patterns?|formulas?|mats)\b")
_MATS_ASKED = re.compile(r"\b(?:what|which|how many|how much)\b[^?!.;]*\bmats\b"
                         r"|\bmats\s+(?:for|to make|needed|do i need|does it take)\b")
# The game's goods, by names only the game has ("frostweave", "eternal fire", "a glacial bag"),
# and everyday goods, which need a second cue ("how do i make a bag for my laptop?", "how do i
# get ink out of my shirt?").
_GAME_GOODS = re.compile(r"\b(?:" + _plain(
    "frostweave", "saronite", "titanium", "eternium", "cobalt", "borean leather", "arctic fur",
    "infinite dust", "dream shard", "dream shards", "abyss crystal", "abyss crystals", "flask",
    "flasks", "potion", "potions", "elixir", "elixirs", "glyph", "glyphs", "enchant", "enchants",
    "frost lotus", "lichbloom", "icethorn", "adder's tongue", "goldclover", "tiger lily",
    "talandra's rose", "deadnettle", "fire leaf", "eternal fire", "eternal water",
    "eternal earth", "eternal air", "eternal shadow", "eternal life", "eternals",
    "crystallized fire", "crystallized water", "crystallized earth", "crystallized air",
    "crystallized shadow", "crystallized life", "moonshroud", "ebonweave", "spellweave",
    "glacial", "fish feast", "fish feasts") + r")\b")
_GOODS = re.compile(r"\b(?:" + _plain(
    "cloth", "ore", "bar", "bars", "herb", "herbs", "leather", "bag", "bags", "gem", "gems",
    "scroll", "scrolls", "ink", "inks", "feast", "feasts") + r")\b")
_CRAFT = re.compile(r"\b(?:make|making|craft|crafting|cook|brew|smelt|smelting|transmute|"
                    r"prospect|mill)\b")
_GET_GOODS = re.compile(r"\bhow (?:do|can|would|should) (?:i|you|we|u) (?:get|farm|find)\b"
                        r"(?![^?!.,;]*\b(?:out|off|rid)\b)"
                        r"|\b(?:best|good|fastest|quickest) (?:place|spot|way) to (?:get|farm)\b")
_CLASSES = re.compile(r"\b(?:" + _plain(
    *CLASS_NAMES, "pally", "pallies", "pala", "dk", "dks", "shammy", "retribution", "ret",
    "protection", "prot", "beast mastery", "marksmanship", "assassination", "subtlety",
    "discipline", "disc", "unholy", "elemental", "ele", "enhancement", "enh", "resto",
    "restoration", "arcane", "affliction", "demonology", "destruction", "feral", "boomkin",
    "moonkin", "spriest", "shadow priest", "frost dk", "frost mage", "fire mage", "arms",
    "fury") + r")\b")
_TRAINER = re.compile(r"\bwhere (?:can|do|does|should|would) (?:i|you|we|u|one) (?:learn|train)"
                      r"\b|\btrainers?\b|\bweapon ?masters?\b|\bwho\s+(?:teaches|trains)\b")
# Someone's own trainer is a person ("my personal trainer taught me a new skill today").
_OWN_TRAINER = re.compile(r"\b(?:my|your|ur|his|her|our|their)\s+(?:personal\s+|own\s+|new\s+)?"
                          r"trainers?\b|\bpersonal\s+trainers?\b")
# Skills of the game (a question about one is a trainer question), and what else a trainer
# teaches: "my trainer at the gym" is none of the game's.
_SKILLS = re.compile(r"\b(?:" + _plain(
    "cwf", "cold weather flying", "dual spec", "dual talent specialization", "artisan riding",
    "expert riding", "journeyman riding", "apprentice riding", "riding skill",
    "flight form") + r")\b")
_TRAINS = re.compile(r"\b(?:" + _plain(
    "riding", "ride", "flying", "fly", "fish", "weapon", "weapons", "weapon skill",
    "weapon skills", "portal", "portals", "teleport", "teleports", "spell", "spells", "rank",
    "ranks", "ability", "abilities", "skill", "skills", "pet", "pets", "profession",
    "professions") + r")\b")
_REPUTATION = re.compile(r"\b(?:rep|reputation|exalted|revered|honou?red|friendly|"
                         r"quartermasters?|tabards?)\b")
# A standing needs whom it is with (fix round 2's brief: "with"; "honored for the invite" is
# none): a faction, or "rep / exalted ... with".
_REP_WITH = re.compile(r"\b(?:rep|reputation|exalted|revered|honou?red)\s+with\b")
# Buying something of the game: "where can i buy frostweave", "who sells cold weather flying".
_BUY = re.compile(r"\bwhere (?:can|do|does|should|would) (?:i|you|we|u|one) (?:buy|farm)\b"
                  r"|\bwho\s+(?:sells|vendors)\b")
_STRATEGY = re.compile(
    r"\b(?:strats?|strategy|tactics?|tips?|mechanics?|phases?|enrage|hard ?modes?|heroic|hc|"
    r"wipes?|wiped|wiping|positioning|kite|kiting|10m|25m|10 ?man|25 ?man|10-man|25-man)\b"
    r"|\bhow (?:do|does|did|can|should|to) (?:we|you|i|u|ya|people)? ?(?:do|beat|kill|down|"
    r"handle|survive|tank|heal|clear|get past)\b"
    r"|\bneeds?\s+(?:\d+|a|an|one|two|three|four|five|more|another)\s+(?:tanks?|healers?|heals|"
    r"dps)\b|\bhow many (?:tanks|healers|heals|dps)\b")
# Gear and goods: "what's a good tank trinket", "is t10 worth it", "is the black bruise worth it
# for my dk?".
_GEAR = re.compile(r"\b(?:trinkets?|weapons?|enchants?|gems?|t(?:7|8|9|10)|tier ?(?:7|8|9|10))\b")
_WORTH_FOR_CLASS = re.compile(r"\bworth\s+(?:it|getting|farming|buying|using|having)\s+(?:for|on)"
                              r"\s+(?:my|a|an|your|the)\s+(?:" + _plain(
                                  *CLASS_NAMES, "pally", "pala", "dk", "shammy", "lock", "hunt",
                                  "druid", "priest", "rogue", "mage", "tank", "healer") + r")\b")
# A dungeon or a raid worth running: its loot ("is pos worth running for the ring?").
_WORTH = re.compile(r"\bworth\s+(?:running|doing|farming|it|the time|going)\b")
_CLASS_PLAY = re.compile(r"\b(?:" + _plain(
    "talent", "talents", "spec", "specs", "specced", "speccing", "respec", "glyph", "glyphs",
    "rotation", "priority", "stat", "stats", "stat weights", "gem", "gems", "gemming", "gear",
    "gearing", "upgrade", "upgrades", "hit cap", "expertise", "haste", "crit", "armor pen",
    "arp", "spell power", "dps", "hps", "build", "dual spec") + r")\b")
_CLASS_STRONG = re.compile(r"\b(?:" + _plain(
    "talent", "talents", "spec", "specs", "specced", "respec", "glyph", "glyphs", "rotation",
    "stat weights", "hit cap", "armor pen", "dual spec") + r")\b")
# A choice of specs: "fire or arcane", "ret or prot" (names that are ordinary words alone).
_SPECS = _plain("arcane", "fire", "frost", "arms", "fury", "prot", "protection", "holy", "ret",
                "retribution", "disc", "discipline", "shadow", "bm", "beast mastery", "mm",
                "marks", "marksmanship", "survival", "assassination", "assa", "combat", "sub",
                "subtlety", "blood", "unholy", "ele", "elemental", "enh", "enhancement",
                "resto", "restoration", "affliction", "affli", "demo", "demonology", "destro",
                "destruction", "balance", "boomkin", "moonkin", "feral", "bear", "cat")
_SPEC_CHOICE = re.compile(rf"\b(?:{_SPECS}) or (?:{_SPECS})\b")
_WHERE = re.compile(r"\b(?:where(?:'s|s)?|located|location of|way to|directions? to)\b"
                    r"|\bhow (?:do|does|can|would) (?:i|you|we|u) get (?:to|into|in)\b")
# "who was arthas", "who's the lich king"; never "who's tanking sindragosa" (a gerund: people);
# "how was arthas corrupted" (a participle after the subject).
_LORE_ASK = re.compile(r"\bwho(?:'s|s|\s+is|\s+was|\s+were|\s+are|\s+r)\b(?!\s+\w+ing\b)"
                       r"|\bwhy (?:did|does|do|is|was|were|are)\b|\bwhat happened\b|\bstory\b"
                       r"|\bstories\b|\blore\b|"
                       r"\bhistory\b|\blegend\b|\bbackstory\b|\borigins?\b"
                       rf"|\bhow\s+(?:was|were)\s+[^?!.,;]*?{_PARTICIPLE}")
_LORE_WORDS = re.compile(r"\b(?:lore|story|stories|history|legend|backstory)\b")
# How a raid or a place came to be: "how was naxx brought to northrend?", "how was the sunwell
# restored?".
_HOW_MADE = re.compile(rf"\bhow\s+(?:was|were)\s+[^?!.,;]*?{_PARTICIPLE}")

# Every word list of the detector: each of ALIASES' keys is one of their words.
DETECTOR_WORDS = (_INSTANCE, _BOSS, _PLACES, _LORE_NAMES, _FACTIONS, _STRATEGY, _GEAR,
                  _SKILLS, _REPUTATION)

# -- shorthand that is also a word --------------------------------------------------------------

# "hol up", "hol on a sec", but "is hol on heroic hard?" is the dungeon.
_HOL_SLANG = re.compile(r"\b(hol)'?\s+(?:up|on)\b(?!\s+(?:heroic|hc|normal|nm|10|25|hard|easy)\b)",
                        re.IGNORECASE)
# "this boss is pos, any tips?": "pos" said of something, with the clause ending there; "is pos
# heroic hard?" is the dungeon.
_POS_SLANG = re.compile(r"\b(?:is|was|so|such|a|this|that|total|absolute|complete|real)\s+(pos)\b"
                        r"(?=\s*(?:[,.!?;]|$|(?:lol|lmao|af|tbh|imo|man|dude|honestly|mate|"
                        r"bro)\b))", re.IGNORECASE)
# "pos" as a position: "what pos do i stand at on sindragosa?", "my pos"; never before a gear or
# loot word ("what pos drops are good for a dk?", "best pos trinket for a tank?": the dungeon).
_POS_PLACE = re.compile(r"\b(?:what|which|my|your|ur|our|his|her|their|same|new|right|wrong|"
                        r"good|best|exact)\s+(pos)\b(?!\s+(?:drops?|boss(?:es)?|trinkets?|gear|"
                        r"loot|rings?|necks?|cloaks?|weapons?|items?|rewards?|runs?|heroic|hc|"
                        r"normal|entrance|achievements?|quests?|dailies|daily)\b)"
                        r"|\b(pos)\s+(?:do|should|am|are|is|was)\s+(?:i|we|you|u)\b", re.IGNORECASE)
_MODE = re.compile(r"(?<![\w'-])(?:10|25)m(?![\w'-])", re.IGNORECASE)
_TIME_BEFORE = re.compile(r"\b(?:brb|afk|in|within|after|about|bout|like|wait|gimme|give me|"
                          r"(?:afk|gone|away|out|back) for)\s*$", re.IGNORECASE)
_TIME_AFTER = re.compile(r"^\s*(?:ago|later|mins?|minutes?|tops|or so)\b", re.IGNORECASE)
_RAID = re.compile(r"\b(?:raids?|heroic|hc|normal)\b")
_DBS = re.compile(r"(?<![\w'-])dbs(?![\w'-])", re.IGNORECASE)
# "dbs" is the owner's databases with a word of databases in the line ("back up the dbs", "the
# dbs dump", "did you back the dbs up?"), or as "the / our / my dbs" with nothing of a raid
# around it ("are the dbs fine?"); else Deathbringer Saurfang ("what's dbs like?", "what's on
# the dbs loot table?").
_DB_WORD = re.compile(r"\b(?:back(?:ed|ing|s)?\s*-?\s*ups?|backups?|restor\w*|dump\w*|mysql|sql|"
                      r"databases?|quer(?:y|ies)|migrat\w*)\b", re.IGNORECASE)
_DB_SPLIT = re.compile(r"\bback(?:ed|ing|s)?\s+(?:(?:the|our|my|your|both|all\s+the)\s+)?dbs\s+"
                       r"up\b", re.IGNORECASE)
_DB_THE = re.compile(r"\b(?:the|our|my|your|both|all\s+the)\s+dbs\b(?!\s+(?:fight|encounter|kill|"
                     r"attempts?|pulls?|adds|loot|drops?|tank|heals?)\b)", re.IGNORECASE)
_REP = re.compile(r"(?<![\w'-])rep(?![\w'-])", re.IGNORECASE)
# "dal" is the city only where a place is meant: "in dal", "to dal", "the dal portal",
# "where's dal", "the dal ah"; "good dal around here" is food.
_DAL = re.compile(r"(?<![\w'-])dal(?![\w'-])", re.IGNORECASE)
_DAL_BEFORE = re.compile(r"\b(?:in|to|into|from|at|near|around|outside|inside|through|over|of|"
                         r"where(?:'s|s| is))\s*$", re.IGNORECASE)
_DAL_AFTER = re.compile(r"^\s*(?:portals?|port|sewers?|fountain|bank|inn|city|flight|landing|"
                        r"underbelly|crater|arena|vendors?|trainers?|quartermasters?|hearth|ah|"
                        r"auction(?:\s+house)?)\b", re.IGNORECASE)


def _blank(text: str, spans: Sequence[tuple[int, int]]) -> str:
    for a, b in spans:
        text = text[:a] + " " * (b - a) + text[b:]
    return text


def _slang(plain: str) -> list[tuple[int, int]]:
    """Where his line's WORDLIKE shorthand is a word, not the game: "hol up", "this boss is
    pos", "pos" as a position, "10m" as minutes ("brb 10m") or with no raid in the line, "dbs"
    as the owner's databases (_DB_WORD, _DB_THE: "should i back up the dbs"), "rep" with no
    faction and no "rep with" ("gotta rep my school"), "dal" that is not a place ("good dal
    around here")."""
    out = [m.span(1) for m in _HOL_SLANG.finditer(plain)]
    out += [m.span(1) for m in _POS_SLANG.finditer(plain)]
    out += [m.span(1) if m.group(1) else m.span(2) for m in _POS_PLACE.finditer(plain)]
    out += [m.span() for m in _DAL.finditer(plain)
            if not (_DAL_BEFORE.search(plain[:m.start()]) or _DAL_AFTER.match(plain[m.end():]))]
    raid = _blank(plain, out).casefold()
    raid = bool(_INSTANCE.search(raid) or _BOSS.search(raid) or _RAID.search(raid))
    out += [m.span() for m in _MODE.finditer(plain)
            if not raid or _TIME_BEFORE.search(plain[:m.start()])
            or _TIME_AFTER.search(plain[m.end():])]
    dbs = [m.span() for m in _DBS.finditer(plain)]
    rest = _blank(plain, out + dbs).casefold()
    if dbs and (_DB_WORD.search(rest) or _DB_SPLIT.search(plain) or (_DB_THE.search(plain) and not (
            _INSTANCE.search(rest) or _BOSS.search(rest) or _STRATEGY.search(rest)
            or _LOOT.search(rest)))):
        out += dbs
    if not (_FACTIONS.search(rest) or _REP_WITH.search(rest)):
        out += [m.span() for m in _REP.finditer(plain)]
    return out


def expand(text: str) -> str:
    """Raider shorthand written out (names.expand), but not where it is a word (_slang):
    "hol up, where is the icc entrance?" -> "hol up, where is the Icecrown Citadel entrance?"."""
    return N.expand(text, keep=_slang(text))


# -- the topic ---------------------------------------------------------------------------------

def _boss(low: str) -> bool:
    """A boss named in his line, not as a word of a place's name: "onyxia's lair" and "the vault
    of archavon" are places."""
    places = [m.span() for m in _INSTANCE.finditer(low)]
    return any(not any(a <= m.start() and m.end() <= b and b - a > m.end() - m.start()
                       for a, b in places) for m in _BOSS.finditer(low))


# What is left of his line after a people clause asks nothing when it holds only raids or
# bosses, times, modes and filler ("who's in? lk tonight?", "where are you, lk?", "who's tanking
# tonight, lk or sindy?").
_BARE_FILL = re.compile(r"\b(?:tonight|tonite|2nite|today|tomorrow|tmrw|later|now|next|first|"
                        r"again|week|weekend|already|at|\d+(?:m|man)?|man|hc|heroic|normal|nm|or|"
                        r"and|anyone|any1|ne1|anybody|someone|us|just|the|then|lol|pls|plz|ok|so|"
                        r"guys|yet|still|this|after|before|on|for|in|to)\b")


def _bare(rest: str) -> bool:
    left = _BARE_FILL.sub(" ", _BOSS.sub(" ", _INSTANCE.sub(" ", rest)))
    return not re.sub(r"[\s?!.,;:'-]+", "", left)


def _topic(text: str, plain: str, low: str, cut: bool = False) -> str | None:
    """`low`: his line in lower case with its slang blanked out (_slang); `cut`: the rest of it
    after a people clause was cut out (_plan)."""
    if _QUEST_LINK.search(text):
        return "quest"
    if _LINK.search(text) or (_BRACKETED.search(plain) and not _LORE_ASK.search(low)):
        return "item"
    if _QUEST_DROP.search(low):
        return None
    if spans := _chat_spans(low):  # people and nights: the rest of his line, if it asks
        rest = _blank(low, spans)
        if not _ASK.search(rest.strip(" ,;:!.")) or _bare(rest):
            return None
        rest_plain = _blank(plain, spans) if len(plain) == len(low) else rest
        return _topic(rest_plain, rest_plain, rest, True)  # its links were read above
    if _plan(low, cut):
        return None
    named = names_in(low)
    raid = bool(_BOSS.search(low) or _INSTANCE.search(low))
    where_do_i = bool(_WHERE_DO_I.search(low))
    if loot_target(plain) or _LOOT.search(low) or (_TOKENS.search(low) and (
            _TOKEN_GAME.search(low) or raid or named or _CLASSES.search(low)
            or (_WHAT_ABOUT.search(low) and not where_do_i))):
        return "loot"
    if _ACHIEVEMENT.search(low) or (_META.search(low) and raid):
        return "achievement"
    if _MOUNT.search(low) and _OBTAIN.search(low) and (_MOUNT_GAME.search(low) or raid or named):
        return "mount"
    goods = bool(_GAME_GOODS.search(low) or (_GOODS.search(low) and (named or game_thing(low))))
    if (_PROFESSION.search(low) or (_CRAFT.search(low) and goods)
            or (_MATS_ASKED.search(low) and not where_do_i)
            or (_PROFESSION_WORDS.search(low) and (named or _GAME_GOODS.search(low)
                                                   or game_thing(low)))):
        return "profession"
    trainer = _OWN_TRAINER.sub(" ", low)
    if _SKILLS.search(low) or (_TRAINER.search(trainer) and (
            _TRAINS.search(trainer) or _CLASSES.search(low) or named)):
        return "trainer"
    if _REPUTATION.search(low) and (_FACTIONS.search(low) or _REP_WITH.search(low)):
        return "reputation"
    if ((_BUY.search(low) and (game_thing(low) or _GAME_GOODS.search(low)))
            or (_GET_GOODS.search(low) and goods)):
        return "item"
    if is_quest_talk(text):
        return "quest"
    if _LORE_ASK.search(low) and (_LORE_NAMES.search(low) or _LORE_WORDS.search(low)):
        return "lore"
    if (m := _WHO_DEED.search(low)) and _of_the_lore(low[m.start():]):
        return "lore"
    if _HOW_MADE.search(low) and (_INSTANCE.search(low) or _PLACES.search(low)):
        return "lore"
    if raid and (_boss(low) or _STRATEGY.search(low)):
        return "strategy"
    if _GEAR.search(low) or _WORTH_FOR_CLASS.search(low):
        return "item"
    if _INSTANCE.search(low) and _WORTH.search(low) and not _NOW.search(low):
        return "loot"
    if (_CLASS_STRONG.search(low) or _SPEC_CHOICE.search(low)
            or (_CLASSES.search(low) and _CLASS_PLAY.search(low))):
        return "class"
    if _WHERE.search(low) and (_PLACES.search(low) or _INSTANCE.search(low)):
        return "zone"
    return None


def is_plan(text: str) -> bool:
    """His line is a plan (_plan): an invitation, a run that is on or a call for people
    ("anyone up for ony later?", "need a tank for icc hc?"). Fix wave G3 reads it so that an
    invitation is never taken for a question about the guild's past."""
    plain = " ".join(strip_wow_markup(text).split())
    return bool(plain) and _plan(_blank(plain, _slang(plain)).casefold())


def game_question(text: str) -> GameQuestion | None:
    """His line as a game question, or None for anything else: chat, banter that only names
    a class or the raid, a question about people or a night ("who's tanking", "who ran icc
    yesterday?", "where are you"), a plan ("anyone up for ony later?"). A question (a question
    mark, a question word first, asking for help) about an item link, loot (also "who drops
    X"), an achievement, a mount, a profession, a skill or a trainer, reputation with someone,
    buying something of the game, a quest (select's is_quest_talk), lore, a boss or a raid's
    tactics, gear, a class's spec, or where a place is. Its query has the shorthand written
    out and the names spelt as the list spells them (names.respell), and its names are read
    from that."""
    plain = " ".join(strip_wow_markup(text).split())
    if not plain:
        return None
    slang = _slang(plain)
    low = _blank(plain, slang).casefold()
    low = _blank(low, [m.span(1) for m in _WHO_SO.finditer(low)])
    if not _ASK.search(low):
        return None
    topic = _topic(text, plain, low)
    if topic is None:
        return None
    query = N.respell(N.expand(plain, keep=slang))
    return GameQuestion(topic, TOPIC_KINDS[topic], query, names_in(query))


# -- who knows ---------------------------------------------------------------------------------

# The guild's historian or scholar, by the officer title the persona carries.
SCHOLAR_TITLE = re.compile(r"\b(?:historian|scholar|lore\w*|archivist|librarian|chronicler)\b",
                           re.IGNORECASE)
# Who answers a topic best, best first. The controller's ruling (part E): the scholar (the
# officer title) first for lore and strategy, then the lore minds for lore and the
# theorycrafters for strategy and gear; quests part B's quest helpers. The other topics follow
# it (fix round 1; the controller accepted them in fix round 2): places like lore; loot,
# classes, professions and trainers like gear, with the scholar next; achievements and mounts
# the collectors, then the scholar; reputation the scholar, then the theorycrafters.
FIT_TIERS: dict[str, tuple[str, ...]] = {
    "lore": ("scholar", "lore"), "zone": ("scholar", "lore"),
    "strategy": ("scholar", "theorycrafter"),
    "item": ("theorycrafter", "scholar"), "loot": ("theorycrafter", "scholar"),
    "class": ("theorycrafter", "scholar"), "profession": ("theorycrafter", "scholar"),
    "trainer": ("theorycrafter", "scholar"),
    "achievement": ("collector", "scholar"), "mount": ("collector", "scholar"),
    "reputation": ("scholar", "theorycrafter"),
    "quest": ("quest_helper",),
}


def fit_tier(topic: str, p: Persona | None) -> int | None:
    """How well a member fits his question's topic (0 best, FIT_TIERS), from persona data
    only: the officer title (SCHOLAR_TITLE), game.thinks_as, select.quest_helper. None: no
    fit."""
    if p is None:
        return None
    for i, tag in enumerate(FIT_TIERS.get(topic, ())):
        if tag == "scholar":
            fits = bool(SCHOLAR_TITLE.search(p.play.officer_title))
        elif tag == "quest_helper":
            fits = quest_helper(p)
        else:
            fits = p.game.thinks_as == tag
        if fits:
            return i
    return None
