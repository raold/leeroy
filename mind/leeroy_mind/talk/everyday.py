"""Everyday words: the ordinary vocabulary no guild coins, so never slang nor a running joke.

The slang stage (nightly/vocab.py) promotes none of them and the guard's gag ration
(guards/review.py) never rations one. Three sources, all lowercase:
- data/everyday_english.txt, about 45,000 common English words from SCOWL (levels 10 to 40:
  frequency classes and the core of 12 dictionaries; licence in data/SCOWL-Copyright.txt);
- talk.words.COMMON_WORDS, the typo step's own list;
- CHAT_WORDS below: what chat writes for ordinary words (contractions without the apostrophe
  and the stubs a word pattern leaves of them, "don" from "don't"), the usual chat shorthand
  and congratulations, and the stock MMO abbreviations every player already knows.
Coined words, game slang and invented words are in none of them: "zugzug", "gank", "noob",
"nerf", "kekw" all stay possible slang."""
from functools import cache
from importlib import resources

from leeroy_mind.talk.words import COMMON_WORDS

CHAT_WORDS = frozenset("""
aint arent cant couldnt didnt doesnt dont hadnt hasnt havent isnt mightnt mustnt neednt shouldnt
wasnt werent wont wouldnt ive youre youve youll youd theyre theyve theyll theyd thats whats whos
wheres theres heres hows yall
ain aren couldn didn doesn don hadn hasn isn mightn mustn needn shouldn wasn weren wouldn
abt aight alright anyways bbl bbs bc brb btw cmon cos coz cuz cya dunno gimme gonna gotta gtg
haha hahaha hehe heh hmm hmmm hmu idc idk ikr ily imo imho innit jst kinda lemme lmao lmfao lol
lul meh msg nah naw nope nvm ofc okay okey omg omfg omw outta pls plz ppl prob rdy rly rlly rofl
shld smh sorta sry stll sup tbf tbh tho thru tht thx tmr tmrw ttyl ugh wanna wknd wtf wth ya yah
yea yeah yep yup yw
congrats congratz grats gratz grtz
afk aoe bis boe bop cds dkp dps gcd hps ilvl lfd lfg lfm loot mats ooc oom pve pvp rez
""".split())


@cache
def _english() -> frozenset[str]:
    text = resources.files("leeroy_mind.talk").joinpath("data", "everyday_english.txt").read_text(
        encoding="ascii")
    return frozenset(w for w in text.split("\n") if w and not w.startswith("#"))


def is_everyday(word: str) -> bool:
    """Whether one word (any case) is everyday vocabulary rather than possible slang."""
    w = word.casefold()
    return w in COMMON_WORDS or w in CHAT_WORDS or w in _english()
