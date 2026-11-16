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


def facts_from(*, gender: str = "", household: str = "", region: str = "", job: str = "",
               more: tuple[str, ...] = ()) -> Facts:
    """Facts from the persona's fields: `household` settles the partner (who lives there);
    kids and pets come from the household and `more` (her life thread and disclosures), so a
    child or a pet her own story names is hers."""
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
    pets = frozenset(k for k, rx in _PET_WORDS.items()
                     if any(rx.search(_fold(t)) for t in (household, *more)))
    return Facts(gender=gender, partner=partner, spouse=spouse | dating if partner in (
                     "married", "partnered") else frozenset(),
                 kids=kids, kid_kinds=kinds if kids == "some" else frozenset(), pets=pets,
                 region=region, employed=bool(_fold(job)), known=True)


def facts_of(p: Persona) -> Facts:
    """The facts of a stored persona (its own fields: a tuning or godmode lens never edits
    them)."""
    return facts_from(gender=p.identity.player_gender, household=p.real_life.household,
                      region=p.identity.region, job=p.real_life.job,
                      more=(p.real_life.life_thread, *(d.text for d in p.disclosures)))


def facts_of_json(data: dict) -> Facts:
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
                      job=get("real_life", "job"), more=(get("real_life", "life_thread"), *more))


# -- what she says about herself --------------------------------------------------------------

_I_AM = r"(?:\bi'm|\bi am|\bim)"
_I_HAVE = r"(?:\bi have|\bi've got|\bive got|\bi got|\bi've)"
_NUM = r"(?:a|an|one|two|three|four|five|\d+)"
_OWN = r"\b(?:my|our)"
# Single: no partner at all. Unmarried: no spouse, which a boyfriend or girlfriend allows.
_SINGLE_SAY = re.compile(
    _I_AM + r" (?:still |totally |completely |very )?single\b"
    r"|" + _I_HAVE + r" no (?:partner|boyfriend|girlfriend)\b"
    r"|\bi (?:don't|dont|do not) have an? (?:partner|boyfriend|girlfriend)\b"
    r"|(?:^|[.!?] *)single(?:,| and| with)? no (?:kids|children)\b")
_UNMARRIED_SAY = re.compile(
    _I_AM + r" (?:still )?(?:not married|unmarried|divorced|a widow(?:er)?|widowed)\b"
    r"|" + _I_HAVE + r" no (?:husband|wife)\b"
    r"|\bi (?:don't|dont|do not) have an? (?:husband|wife)\b"
    r"|\bi(?:'ve| have)? never (?:been )?married\b")
_MARRIED_SAY = re.compile(
    _OWN + r" (?<!ex-)(husband|hubby|wife|wifey|missus|missis)\b"
    r"|" + _I_AM + r" (?:happily |very )?(married)\b")
_LIVE_ALONE_SAY = re.compile(r"\bi live (?:all )?(?:alone|by myself|on my own)\b")
_KIDS_SAY = re.compile(
    r"\bmy (?:(?:two|three|four|\d+|little|youngest|eldest|oldest|older|younger|baby) )?"
    r"(sons?|daughters?|kids?|children|twins|toddlers?|newborn)\b"
    r"|\bmy (?:two|three|four|\d+|little) (girls|boys)\b"
    r"|" + _I_HAVE + r" " + _NUM + r" (?:little |grown )?(kids?|children|child|sons?|"
    r"daughters?|boys|girls|twins)\b"
    r"|" + _I_AM + r" (?:a )?(?:single )?(mum|mom|mother|dad|father|parent)\b")
_NO_KIDS_SAY = re.compile(
    _I_HAVE + r" no (?:kids|children)\b"
    r"|\bi (?:don't|dont|do not) have (?:any )?(?:kids|children)\b"
    r"|\bi(?:'ve| have)? never had (?:kids|children)\b"
    + r"|" + _I_AM + r" childless\b"
    r"|\b(?:single|married)(?:,| and| with)? no (?:kids|children)\b")
_PET_ADJ = (r"(?:(?:old|little|fat|lazy|stupid|dumb|silly|grumpy|new|rescue|big|tiny|sweet|"
            r"poor|own|ginger|black|grey|gray|orange|white|tabby) )?")
_PET_SAY = re.compile(r"\b" + _OWN + r" " + _PET_ADJ +
                      r"(cats?|kitten|kitty|dogs?|pup|puppy|doggo)\b")
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
    contradicts none or the persona does not settle what it says."""
    if not facts.known:
        return []
    t = first_person(text) if claim else _fold(text)
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
    pets = {"cat" if m.group(1).startswith(("cat", "kit")) else "dog"
            for m in _PET_SAY.finditer(t)}
    if pets and "any" not in facts.pets and not pets <= facts.pets:
        out.append("pets")
    if (facts.region == "UK" and _US_SAY.search(t)) or (facts.region == "US" and _UK_SAY.search(t)):
        out.append("region")
    if facts.employed and _JOBLESS_SAY.search(t):
        out.append("job")
    return out


__all__ = ["CODES", "Facts", "GENDER_WORD", "PRONOUNS", "conflicts", "facts_from", "facts_of",
           "facts_of_json", "first_person", "pronoun", "pronoun_of", "pronoun_pair"]
