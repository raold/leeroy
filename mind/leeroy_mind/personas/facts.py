"""What a member's persona says about who she is: her gender and pronouns, who she lives with,
her kids, her pets, her job and her country, read from the stored persona's own fields, and the
first-person statements that contradict them (identity audit, 2026-11-15: whatever was on the
card stayed consistent and whatever was off it got invented, so married women said they were
single and a cat owner talked about her dog).

The persona outranks anything said in chat. `conflicts` is deterministic and narrow: it reads
only first-person statements ("i'm single", "my son", "my dog", "here in the states"), never
what someone says about another member or about him, and a fact the persona does not settle is
never a conflict. Her household is taken as complete for a partner, and with her life thread
and disclosures for kids and pets: the generator and the owner write who lives there, so "my
cat" from a member whose persona names no pet is an invention (measured on the live chat,
2026-11-15: every such line was). `claim=True` reads a stored claim, which is written in the
third person without a name ("is single with no children"), as her own first-person words."""
import re
from dataclasses import dataclass

from .schema import Persona

PRONOUNS = {"female": ("she", "her", "her"), "male": ("he", "him", "his")}
GENDER_WORD = {"female": "woman", "male": "man"}
CODES = ("partner", "kids", "pets", "region", "job")


def pronoun(gender: str) -> str:
    """"she" or "he" for a player gender; "" for anything else."""
    return PRONOUNS.get(gender, ("",))[0]


def pronoun_of(p: Persona | None) -> str:
    """The subject pronoun of a member's persona (the player's gender, never the WoW
    character's: players talk about each other as people, owner ruling 2026-11-15); "" without
    a persona."""
    return pronoun(p.identity.player_gender) if p is not None else ""


def pronoun_pair(gender: str) -> str:
    """"she/her" or "he/him"; "" for anything else."""
    forms = PRONOUNS.get(gender)
    return f"{forms[0]}/{forms[1]}" if forms else ""


@dataclass(frozen=True)
class Facts:
    gender: str = ""
    partner: str = ""          # married, partnered, single, alone or "" (unknown)
    spouse: frozenset = frozenset()  # husband, wife, partner, boyfriend, girlfriend
    kids: str = ""             # none or some
    kid_kinds: frozenset = frozenset()  # son, daughter or any (kids not said which)
    pets: frozenset = frozenset()  # cat, dog, other, or any (a pet her story does not name)
    region: str = ""
    employed: bool = False
    known: bool = False        # read from a persona; Facts() (no persona) settles nothing


_EX = r"(?<!ex-)(?<!ex )(?<!ex)"
_SPOUSE = {"husband": re.compile(_EX + r"\bhusband\b"), "wife": re.compile(_EX + r"\bwife\b"),
           "partner": re.compile(r"\bcivil partner\b|\bspouse\b")}
_DATING = {"boyfriend": re.compile(_EX + r"\bboyfriend\b"),
           "girlfriend": re.compile(_EX + r"\bgirlfriend\b"),
           "partner": re.compile(r"(?<!no )(?<!civil )\b(?:partner|fianc[eé]e?)\b")}
_MARRIED = re.compile(r"(?<!un)\bmarried\b")
_SINGLE = re.compile(r"\b(?:single|no partner|divorced?|widow(?:ed|er)?|separated|split up|"
                     r"ex-?(?:husband|wife|boyfriend|girlfriend|partner))\b"
                     r"|\bsince (?:the |our |my )?(?:divorce|split)\b")
_ALONE = re.compile(r"\b(?:alone|solo|by herself|by himself)\b")
_NO_KIDS = re.compile(r"\bno (?:kids|children)\b|\bchild-?(?:less|free)\b")
# "girls" and "boys" in a household are as often flatmates as children: only the plain words.
_KID_WORDS = {
    "son": re.compile(r"\bsons?\b"),
    "daughter": re.compile(r"\bdaughters?\b"),
    "any": re.compile(r"\b(?:kids?|child|children|twins|bab(?:y|ies)|toddlers?|newborn|"
                      r"teenagers?|step-?(?:son|daughter|kids?))\b"),
}
_GRAND = re.compile(r"\bgrand[- ]?(?:kids?|child(?:ren)?|sons?|daughters?|bab(?:y|ies))\b")
_PET_WORDS = {
    "cat": re.compile(r"\b(?:cats?|kittens?|kitty|kitties|tabby|tabbies|tomcat|moggy|maine coon|"
                      r"siamese|(?:ginger|black|grey|gray|orange|white) tom)\b"),
    "dog": re.compile(r"\b(?:dogs?|pup|puppy|puppies|hound|mutt|terriers?|collies?|retrievers?|"
                      r"labradors?|beagles?|corgis?|greyhounds?|whippets?|spaniels?|poodles?|"
                      r"pugs?|husky|huskies|shepherds?|boxers?|dachshunds?|staff(?:ie|y)|"
                      r"bulldogs?|schnauzers?|chihuahuas?|rottweilers?|dobermanns?|dobermans?|"
                      r"pit ?bulls?|german shorthair|lurchers?|sheepdogs?|jack russell)\b"),
    "any": re.compile(r"\bpets?\b"),  # a pet her story does not name: any pet is hers
    "other": re.compile(r"\b(?:goldfish|fish|turtles?|tortoises?|rabbits?|bunny|bunnies|"
                        r"hamsters?|parrots?|budgies?|geckos?|snakes?|lizards?|ferrets?|"
                        r"guinea pigs?|chickens?|hens|horses?|ponies|pony|budgerigars?)\b"),
}


def _fold(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("’", "'").replace("ʼ", "'").split())


# What an owner's edit of her life talks about (godmode memory implants and life events): a topic
# it names is his, not the stored persona's, so it is never held against her.
_OWNER_TOPICS = {
    "partner": re.compile(r"\b(?:husband|wife|spouse|boyfriend|girlfriend|fianc[eé]e?|married|"
                          r"marry|marriage|wedding|divorc\w*|engaged|broke up|break-?up|split up|"
                          r"widow\w*|affair|dating)\b"),
    "kids": re.compile(r"\b(?:kids?|child|children|sons?|daughters?|pregnan\w*|twins|toddlers?|"
                       r"newborn|(?:a|the|new|her|had a|having a) baby|step-?(?:son|daughter|kids?))\b"),
    "pets": re.compile(r"\b(?:pets?|adopt\w*)\b"),
    "region": re.compile(r"\b(?:moved to|moving to|moved (?:back|over|across|abroad)|relocat\w*|"
                         r"emigrat\w*|lives? in (?:the )?(?:us|usa|states|america|uk|britain|"
                         r"england|scotland|wales))\b"),
    "job": re.compile(r"\b(?:new job|(?:lost|quit|left) (?:her|my|his|the) job|fired|laid off|"
                      r"redundan\w*|promot\w*|got hired|unemploy\w*|career change|"
                      r"changed jobs?|between jobs)\b"),
}
# The game's own words an edit may hold, never her life: the holiday, not her children.
_GAME_EVENTS = re.compile(r"\bchildren'?s week\b")


def facts_from(*, gender: str = "", household: str = "", region: str = "", job: str = "",
               more: tuple[str, ...] = (), owner: tuple[str, ...] = ()) -> Facts:
    """Facts from the persona's fields: `household` settles the partner (who lives there);
    kids and pets come from the household and `more` (her life thread and disclosures), so a
    child or a pet her own story names is hers. `owner` is the owner's own edits of her life
    (owner_texts: godmode memory implants and life events): a topic one names (a partner, kids,
    pets, where she lives, her job) is left unsettled, so his edits always win."""
    h = _fold(household)
    spouse = frozenset(k for k, rx in _SPOUSE.items() if rx.search(h))
    dating = frozenset(k for k, rx in _DATING.items() if rx.search(h))
    if spouse or _MARRIED.search(h) and not re.search(r"\bnot married\b", h):
        partner = "married"
    elif dating:
        partner = "partnered"
    elif _SINGLE.search(h):
        partner = "single"
    elif _ALONE.search(h):
        partner = "alone"
    else:
        partner = ""
    # Kids her own story names count too ("her daughter is sixteen" in a life thread beside a
    # household that only says who lives there); "no kids" itself names none.
    plain = [_NO_KIDS.sub(" ", _GRAND.sub(" ", _fold(t))) for t in (household, *more)]
    kinds = frozenset(k for k, rx in _KID_WORDS.items() if any(rx.search(t) for t in plain))
    kids = "some" if kinds else "none"
    texts = (household, *more, *owner)
    pets = frozenset(k for k, rx in _PET_WORDS.items() if any(rx.search(_fold(t)) for t in texts))
    edits = _GAME_EVENTS.sub(" ", " ".join(_fold(t) for t in owner))
    edited = {topic for topic, rx in _OWNER_TOPICS.items() if rx.search(edits)}
    if "partner" in edited:
        partner = ""
    if "kids" in edited:
        kids, kinds = "", frozenset()
    if "pets" in edited:
        pets |= {"any"}
    return Facts(gender=gender, partner=partner, spouse=spouse | dating if partner in (
                     "married", "partnered") else frozenset(),
                 kids=kids, kid_kinds=kinds if kids == "some" else frozenset(), pets=pets,
                 region="" if "region" in edited else region,
                 employed=bool(_fold(job)) and "job" not in edited, known=True)


def facts_of(p: Persona, *, owner: tuple[str, ...] = ()) -> Facts:
    """The facts of a stored persona (its own fields: a tuning lens never edits them), with the
    owner's own edits of her life (`owner`, owner_texts) winning over them."""
    return facts_from(gender=p.identity.player_gender, household=p.real_life.household,
                      region=p.identity.region, job=p.real_life.job,
                      more=(p.real_life.life_thread, *(d.text for d in p.disclosures)),
                      owner=owner)


def facts_of_json(data: dict, *, owner: tuple[str, ...] = ()) -> Facts:
    """The same facts from a persona's stored JSON (persona_core.json), for readers that never
    parse a whole persona; a missing field settles nothing."""
    def get(*path):
        v = data
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        return v if isinstance(v, str) else ""
    disclosures = data.get("disclosures") if isinstance(data, dict) else None
    more = tuple(d.get("text", "") for d in disclosures or () if isinstance(d, dict)
                 and isinstance(d.get("text"), str))
    return facts_from(gender=get("identity", "player_gender"),
                      household=get("real_life", "household"), region=get("identity", "region"),
                      job=get("real_life", "job"), more=(get("real_life", "life_thread"), *more),
                      owner=owner)


def owner_texts(conn, guid: int) -> tuple[str, ...]:
    """The owner's own edits of a member's life: her active memories of source `owner` (godmode
    implants, and the owner's seeded history) and the titles of his godmode life events still
    in force. Missing tables read as none."""
    import sqlite3
    out = []
    try:
        out += [r[0] for r in conn.execute("SELECT text FROM memory WHERE owner=? AND "
                                           "source='owner' AND status='active'", (guid,))]
    except sqlite3.OperationalError:
        pass
    try:
        from leeroy_mind.tuning.edits import live, values
        out += [str(v["args"].get("title", "")) for v in values(conn).values()
                if live(v) and v.get("kind") == "life_event"
                and isinstance(v.get("args"), dict) and v["args"].get("member") == guid]
    except (sqlite3.OperationalError, KeyError, TypeError):
        pass
    return tuple(t for t in out if isinstance(t, str) and t)


# -- what she says about herself --------------------------------------------------------------

_I_AM = r"(?:\bi'm|\bi am|\bim)"
_I_HAVE = r"(?:\bi have|\bi've got|\bive got|\bi got|\bi've)"
_NUM = r"(?:a|an|one|two|three|four|five|\d+)"
# Single: no partner at all. Unmarried: no spouse, which a boyfriend or girlfriend allows.
_SINGLE_SAY = re.compile(
    _I_AM + r" (?:still |totally |completely |very )?single\b"
    r"|" + _I_HAVE + r" no (?:partner|boyfriend|girlfriend)\b"
    r"|\bi (?:don't|dont|do not) have an? (?:partner|boyfriend|girlfriend)\b"
    r"|^\s*single(?:,| and| with)? no (?:kids|children)\b")
# "married to this spec" is a gamer's idiom, never her household.
_IDIOM = r"(?! to (?:this|that|the|my|wow|warcraft|raiding|work|it)\b)"
_UNMARRIED_SAY = re.compile(
    _I_AM + r" (?:still )?(?:not married" + _IDIOM + r"|unmarried|divorced|a widow(?:er)?|widowed)\b"
    r"|" + _I_HAVE + r" no (?:husband|wife)\b"
    r"|\bi (?:don't|dont|do not) have an? (?:husband|wife)\b"
    r"|\bi(?:'ve| have)? never (?:been )?married\b(?! (?:to|the|this|that|it|my)\b)")
_MARRIED_SAY = re.compile(
    r"\bmy (husband|hubby|wife|wifey|missus|missis)\b"
    r"|" + _I_AM + r" (?:happily |very )?(married)\b" + _IDIOM)
_LIVE_ALONE_SAY = re.compile(r"\bi live (?:all )?(?:alone|by myself|on my own)\b")
_KIDS_SAY = re.compile(
    r"\bmy (?:(?:two|three|four|\d+|little|youngest|eldest|oldest|older|younger|baby) )?"
    r"(sons?|daughters?|kids?|children|twins|toddlers?|newborn)\b"
    r"|\bmy (?:two|three|four|\d+|little) (girls|boys)\b"
    r"|" + _I_HAVE + r" " + _NUM + r" (?:little |grown )?(kids?|children|child|sons?|"
    r"daughters?|boys|girls|twins)\b"
    r"|" + _I_AM + r" a (?:single )?(mum|mom|mother|dad|father|parent)\b"
    r"(?!\s+(?:jokes?|friends?|energy|mode|vibes?|bod))")
_NO_KIDS_SAY = re.compile(
    _I_HAVE + r" no (?:kids|children)\b"
    r"|\bi (?:don't|dont|do not) have (?:any )?(?:kids|children)\b"
    r"|\bi(?:'ve| have)? never had (?:kids|children)\b"
    + r"|" + _I_AM + r" childless\b"
    r"|\b(?:single|married)(?:,| and| with)? no (?:kids|children)\b")
_PET_ADJ = (r"(?:(?:old|little|fat|lazy|stupid|dumb|silly|grumpy|new|rescue|big|tiny|sweet|"
            r"poor|own|ginger|black|grey|gray|orange|white|tabby) )?")
# "my cat form", and "kitty" (a druid's cat form as often as a pet), are the game's.
_PET_SAY = re.compile(r"\bmy " + _PET_ADJ + r"(cats?|kitten|dogs?|pup|puppy|doggo)\b(?! form)")
_US_SAY = re.compile(
    _I_AM + r" (?:an )?american\b"
    r"|\b(?:here|over here|back home|where i live) in (?:the )?(?:us|usa|states|america)\b"
    r"|\bi live in (?:the )?(?:us|usa|states|america)\b")
_UK_SAY = re.compile(
    _I_AM + r" (?:british|english|scottish|welsh)\b"
    r"|\b(?:here|over here|back home|where i live) in (?:the )?(?:uk|britain|england|scotland|"
    r"wales|blighty)\b"
    r"|\bi live in (?:the )?(?:uk|britain|england|scotland|wales)\b")
_JOBLESS_SAY = re.compile(
    _I_AM + r" (?:currently |still )?(?:unemployed|jobless|out of work|between jobs)\b"
    r"|\bi (?:don't|dont|do not) (?:have|work) a job\b")
# Never her own life: a joke, a hedge ("i'd say"), what follows "if i", someone named as her
# wife or kids in fun ("she's my wife now"), the guild called her kids ("listen up my
# children", "my kids in this guild"), a pet in the game ("my cat pulled the boss").
_SENTENCES = re.compile(r"[.!?;]+")
_UNREAL = re.compile(r"\b(?:jk|j/k|just kidding|kidding|imagine|pretend(?:ing)?|hypothetically|"
                     r"i'?d say|i would say)\b")
# A hypothetical of hers: what follows "if i ..." is not her life ("if i had kids my kids...");
# "my cat would wreck it if she got out" still is.
_IF_I = re.compile(r"\bif i(?:'d|'m)?\b.*$")
_DESIGNATE = re.compile(r"\b(?:she|he|you|u|they|ur|youre|you're|shes|she's|hes|he's)"
                        r"(?:'s|'re| is| are)?\s+(?:now\s+|basically\s+|officially\s+)?my\s+"
                        r"(?:wife|wifey|husband|hubby|missus|girlfriend|boyfriend|kids?|children|"
                        r"sons?|daughters?|cats?|dogs?)\b")
_VOCATIVE = re.compile(r"\b(?:listen up|gather round|gather around|hey|hi|hello|ok|okay|alright|"
                       r"morning|night|goodnight|come on|behave),? my (?:kids|children)\b")
_GUILD_KIDS = re.compile(r"\bmy (?:kids|children) (?:in|of) (?:this|the|our|my) "
                         r"(?:guild|raid|group|team)\b")
_GAME_PET = re.compile(r"\b(?:form|pull(?:ed|s|ing)?|aggro|boss(?:es)?|mobs?|growl(?:ed|ing)?|"
                       r"taunt(?:ed|ing)?|tank(?:ed|ing)?|dps|pet bar|stable(?:d)?|feral|prowl|"
                       r"dash|hunter|druid|feign|mend pet|revive pet|spirit beast|tamed?|"
                       r"dismiss(?:ed)?)\b")


def first_person(claim: str) -> str:
    """A stored claim ("is single with no children", "has two daughters", "her husband ...")
    as the speaker's own words, for `conflicts`."""
    t = "i " + _fold(claim)
    for a, b in ((r"^i is\b", "i'm"), (r"^i has\b", "i have"), (r"^i doesn't\b", "i don't"),
                 (r"^i does not\b", "i do not"), (r"^i lives\b", "i live"),
                 (r"^i was\b", "i was"), (r"^i (\w+)s\b", r"i \1")):
        t, n = re.subn(a, b, t)
        if n:
            break
    return re.sub(r"\b(?:her|his)\b", "my", t)


def conflicts(text: str, facts: Facts, *, claim: bool = False) -> list[str]:
    """The codes (CODES) of the persona facts a first-person `text` contradicts; [] when it
    contradicts none or the persona does not settle what it says. Read sentence by sentence:
    a joke or a hypothetical ("lol jk", "if i had kids...") says nothing about her life, nor
    does naming someone her wife or kids in fun, the guild's "my children", or a pet in the
    game ("my cat pulled the boss", "my cat form")."""
    if not facts.known:
        return []
    t = first_person(text) if claim else _fold(text)
    t = _GUILD_KIDS.sub(" ", _VOCATIVE.sub(" ", _DESIGNATE.sub(" ", t)))
    out = []
    for sentence in _SENTENCES.split(t):
        sentence = _IF_I.sub(" ", sentence).strip()
        if sentence and not _UNREAL.search(sentence):
            out += [c for c in _said(sentence, facts) if c not in out]
    return out


def _said(t: str, facts: Facts) -> list[str]:
    out = []
    said = [m.group(1) or m.group(2) for m in _MARRIED_SAY.finditer(t)]
    if facts.partner == "married" and (_SINGLE_SAY.search(t) or _UNMARRIED_SAY.search(t)
                                       or _LIVE_ALONE_SAY.search(t)):
        out.append("partner")
    elif facts.partner == "partnered" and _SINGLE_SAY.search(t):
        out.append("partner")
    elif said and facts.partner in ("single", "alone"):
        out.append("partner")
    elif said and facts.partner == "partnered" and not facts.spouse & {"partner"}:
        out.append("partner")
    elif said and facts.partner == "married" and facts.spouse & {"husband", "wife"}:
        kinds = {"husband" if w in ("husband", "hubby") else "wife" if w in ("wife", "wifey",
                 "missus", "missis") else "" for w in said} - {""}
        if kinds and not kinds & facts.spouse:
            out.append("partner")
    kids = [next(g for g in m.groups() if g) for m in _KIDS_SAY.finditer(t)]
    if facts.kids == "none" and kids:
        out.append("kids")
    elif facts.kids == "some" and _NO_KIDS_SAY.search(t):
        out.append("kids")
    elif kids and facts.kid_kinds and "any" not in facts.kid_kinds:
        said_kinds = {"son" if k.startswith(("son", "boy")) else
                      "daughter" if k.startswith(("daughter", "girl")) else "" for k in kids}
        if said_kinds - {""} and not (said_kinds - {""}) <= facts.kid_kinds:
            out.append("kids")
    pets = set() if _GAME_PET.search(t) else {
        "cat" if m.group(1).startswith(("cat", "kit")) else "dog" for m in _PET_SAY.finditer(t)}
    if pets and "any" not in facts.pets and not pets <= facts.pets:
        out.append("pets")
    if (facts.region == "UK" and _US_SAY.search(t)) or (facts.region == "US" and _UK_SAY.search(t)):
        out.append("region")
    if facts.employed and _JOBLESS_SAY.search(t):
        out.append("job")
    return out


__all__ = ["CODES", "Facts", "GENDER_WORD", "PRONOUNS", "conflicts", "facts_from", "facts_of",
           "facts_of_json", "first_person", "owner_texts", "pronoun", "pronoun_of", "pronoun_pair"]
