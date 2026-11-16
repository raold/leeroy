"""Everyday words: the ordinary vocabulary no guild coins, so never slang nor a running joke.

The slang stage (nightly/vocab.py) promotes none of them, nor a typo of one, and the guard's gag
ration (guards/review.py) never rations a slang term that is one. Three sources, all lowercase:
- data/everyday_english.txt, about 45,000 common English words from SCOWL (levels 10 to 40:
  frequency classes and the core of 12 dictionaries, capitalised ones lowercased: weekdays,
  months, holidays, nationalities; licence in data/SCOWL-Copyright.txt);
- talk.words.COMMON_WORDS, the typo step's own list;
- CHAT_WORDS below: what chat writes for ordinary words (contractions without the apostrophe
  and the stubs a word pattern leaves of them, "don" from "don't"), the usual chat shorthand
  and congratulations, and the stock MMO abbreviations every player already knows.
A typo of one (everyday_reason "typo") is a word whose runs of three or more letters collapse
into one, or which one inserted letter, one swap of neighbours or one dropped doubled letter
turns into one of five letters or more: "conrats", "wehre", "reallly", "heyyyy". GAMER_WORDS are
gamer slang and game shorthand that would read as such a typo ("pwned" of "pawned", "gief" of
"grief", "mage" of "image", "spec" of "speck"); they stay possible slang.
Coined words, game slang and invented words are in none of them: "zugzug", "gank", "noob",
"nerf", "kekw" all stay possible slang."""
import re
from functools import cache, lru_cache
from importlib import resources
from string import ascii_lowercase

from leeroy_mind.talk.words import COMMON_WORDS

CHAT_WORDS = frozenset("""
aint arent cant couldnt didnt doesnt dont hadnt hasnt havent isnt mightnt mustnt neednt shouldnt
wasnt werent wont wouldnt ive youre youve youll youd theyre theyve theyll theyd thats whats whos
wheres theres heres hows yall
ain aren couldn didn doesn don hadn hasn isn mightn mustn needn shouldn wasn weren wouldn
abt aight alright anyways bbl bbs bc bro brb btw cmon cos coz cuz cya dunno gimme gonna gotta gtg
haha hahaha hehe heh hmm hmmm hmu idc idk ikr ily imo imho innit jst kinda lemme lmao lmfao lol
lul meh mins msg mvp nah naw nope nvm ofc okay okey omg omfg omw outta pls plz ppl prob rdy rly rlly
rofl shld smh sorta sry stll sup tbf tbh tho thru tht thx tmr tmrw ttyl ugh wanna wat wknd wtf
wth ya yah yea yeah yep yup yw
congrats congratz grats gratz grtz
afk aoe bis boe bop cds dkp dps gcd hps ilvl lfd lfg lfm loot mats ooc oom pve pvp rez
""".split())
GAMER_WORDS = frozenset("""
gief leet pwned pwnd pwnt pwnage pwns
achiev allys alts bois brah bres bruh destro ding ench homies hots mage mages mana nubs pugging
pugs regen resto respec ress shad shard sota spec twinking
""".split())
TYPO_MIN = 5  # the everyday word a near-miss lands on is at least this long
_RUN = re.compile(r"(.)\1{2,}")


@cache
def _english() -> frozenset[str]:
    text = resources.files("leeroy_mind.talk").joinpath("data", "everyday_english.txt").read_text(
        encoding="ascii")
    return frozenset(w for w in text.split("\n") if w and not w.startswith("#"))


def is_everyday(word: str) -> bool:
    """Whether one word (any case) is everyday vocabulary rather than possible slang."""
    w = word.casefold()
    return w in COMMON_WORDS or w in CHAT_WORDS or w in _english()


def _near(word):
    """The everyday-word candidates one slip away: one letter inserted, two neighbours swapped,
    one of a doubled letter dropped."""
    for i in range(len(word) + 1):
        for ch in ascii_lowercase:
            yield word[:i] + ch + word[i:]
    for i in range(len(word) - 1):
        if word[i] != word[i + 1]:
            yield word[:i] + word[i + 1] + word[i] + word[i + 2:]
        else:
            yield word[:i] + word[i + 1:]


@lru_cache(maxsize=8192)
def everyday_reason(word: str) -> str | None:
    """"common" for an everyday word, "typo" for a near-miss or elongation of one, else None."""
    w = word.casefold()
    if is_everyday(w):
        return "common"
    if w in GAMER_WORDS or not w.isalpha():
        return None
    if any(is_everyday(c) for c in {_RUN.sub(r"\1\1", w), _RUN.sub(r"\1", w)} - {w}):
        return "typo"
    if any(len(c) >= TYPO_MIN and is_everyday(c) for c in _near(w)):
        return "typo"
    return None
