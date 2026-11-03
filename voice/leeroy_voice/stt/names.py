"""The roster correction of his transcribed lines (draft section 6.4). onnx-asr has no hotwords,
so a member's name or a WoW word in what he says depends on this pass: rapidfuzz plus a phonetic
key (jellyfish's metaphone) against the full roster and the WoW lexicon (Lexicon.words()). It
puts right near-misses ("bellra" for Belra), and cannot put right a name heard as an ordinary
word ("Claire" as "clear"), except where he addresses someone.

A token is a word: letters and digits, apostrophes inside ("you're", "Kel'Thuzad"). Keys, what is
compared, are a word's letters and digits casefolded ("Kel'Thuzad" and "kel thuzad" are both
kelthuzad), and an entry's metaphone is its key's.
- A token in `common` is left alone (casefolded words: misaki's US dictionary in the service,
  common_words()), and so is a word in NOT_ADDRESS, a possessive whose stem is either, and a
  number.
- Any other token, and any run of up to MAX_RUN neighbouring tokens that holds one (the
  recognizer splits a long word: "kel thuzad"), is compared with every roster name and lexicon
  word, on its tokens' keys joined. It matches at rapidfuzz ratio >= DIRECT, or >= PHONETIC
  when both have the same metaphone key. At each token the best score wins, the longer run on a
  tie, and the run becomes the name or word as written. A name wins a tie with a lexicon word;
  a tie between two roster names is no match, since equal evidence picks no member.
  A run joins tokens across spaces and hyphens only, never across other punctuation, and holds
  no number (metaphone does not hear digits).
- A run is taken only when it scores higher than the best match that starts at its second
  token; otherwise its first token is taken alone, and a common one is left alone. A short word
  joined to a long name still scores DIRECT ("is" and Kel'Thuzad, 90), so without this the word
  would be lost (review C1); a word that helps still joins ("marrow gar" -> Marrowgar).
- A token ending in 's is tried twice: its stem, the 's put back after the name ("bellra's" ->
  "Belra's"), and as a whole, the 's dropped, since the recognizer also hears a name that ends
  in s as a possessive ("Caro's" for Coros, measured). The better wins, the stem on a tie.
- The first token, when a comma follows it, is the address: it is matched against the roster
  names alone at ratio >= PHONETIC, common or not ("Chorus, you're on adds" -> "Coros, ..."),
  unless it is in NOT_ADDRESS: the closed classes of words that open a line, so a member whose
  name is near one never takes it ("Dunno," stays; review I1). With `present`, the members in
  his room, it is matched against those names alone: he can address only someone who hears him
  (review-task9: 0.22% of dictionary words taken, against 2.5% for the whole roster). When no
  name matches, the rules above still apply to it.
- SOUNDALIKES are raid words the recognizer writes as an ordinary word (Parakeet wrote "ads"
  for every "adds" in Task 9's loopback): they are replaced common or not, in the token's case.

Everything else in the text (its spacing, punctuation and case) is kept as it came."""
import importlib.resources
import json
import re
from dataclasses import dataclass

import jellyfish
from rapidfuzz import fuzz, process

DIRECT = 85  # rapidfuzz ratio for a match on spelling alone
PHONETIC = 70  # the ratio for a match with the same metaphone key, and for the address
MAX_RUN = 3  # the most neighbouring tokens one name or word is heard as ("kel thu zad")
SOUNDALIKES = {"ads": "adds"}
# Words that open a line and are never the address (review I1): closed classes only, public
# English, no names, each as `common` holds it.
NOT_ADDRESS = frozenset(" ".join((
    # interjections
    "ah aw aww bah eh er erm ha hah haha heh hm hmm huh meh oh ooh oops ouch ow phew psst shh uh "
    "uhh ugh um umm whoa woah whoops wow yay yikes",
    # oaths
    "damn dang darn dammit gosh geez jeez god heck hell shoot crap shit fuck christ lord",
    # responses
    "yes yeah yea yep yup ya yah no nope nah naw ok okay okey okie kk k alright alrighty aight sure "
    "right fine true correct exactly absolutely definitely totally indeed agreed roger copy "
    "understood gotcha mhm dunno idk cool nice great good awesome perfect sweet excellent lol lmao "
    "omg wtf",
    # greetings
    "hi hello hey heya hiya howdy yo sup morning evening afternoon night goodnight bye goodbye "
    "later cheers welcome thanks thank thx ty sorry please congrats grats gratz gz gg",
    # discourse words
    "well so now then anyway anyways actually honestly seriously basically literally obviously "
    "apparently frankly clearly hopefully luckily sadly unfortunately fortunately personally maybe "
    "perhaps probably also plus besides however meanwhile otherwise still though first second "
    "third next finally lastly again instead look listen see wait like remember careful quick "
    "ready",
    # generic vocatives
    "guys folks everyone everybody people all team gang boys girls ladies gentlemen fellas friends "
    "buddy pal mate bro bruh dude man sis sir ma'am kid boss chief captain raid party group guild "
    "officers",
    # role vocatives
    "tank tanks healer healers heals dps melee ranged casters mage mages hunter hunters priest "
    "priests rogue rogues warrior warriors druid druids shaman shamans paladin paladins pally "
    "warlock warlocks lock dk",
    # endearments
    "babe baby honey hon sweetie sweetheart darling dear love beautiful gorgeous cutie",
    # raid calls, and the voice commands Task 15 maps (pull, go, hold, stop, follow, stay, attack)
    "adds ads add inc incoming pull go stop hold stay follow attack move run heal stack spread "
    "focus kill kick interrupt taunt",
)).split())
_TOKEN = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")
_RUN_GAP = re.compile(r"[\s-]+")  # what may stand between the tokens of a run
_COMMA = re.compile(r"\s*,")
_POSSESSIVE = re.compile(r"(.+)(['\u2019][sS])")
_MISAKI_US = ("us_gold.json", "us_silver.json")


def _key(word: str) -> str:
    return "".join(c for c in word.casefold() if c.isalnum())


def _norm(word: str) -> str:
    """A word as `common` holds it: casefolded, its apostrophes straight."""
    return word.replace("\u2019", "'").casefold()


def _cased(word: str, like: str) -> str:
    """`word` in the case of `like`: all capitals, a first capital, or as it is."""
    if len(like) > 1 and like.isupper():
        return word.upper()
    if like[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


@dataclass(frozen=True, slots=True)
class _Entry:
    word: str  # as written: what a match becomes
    key: str
    sound: str  # the key's metaphone
    name: bool  # a roster name: the address is matched against these alone


class RosterCorrector:
    """`names`: the roster; `words`: the lexicon's words; `common`: casefolded ordinary words,
    held as given (never copied, so the service can share one set). Cheap to make again when
    the roster changes: with_names() gives a new one, and one in use on another thread is left
    as it was."""

    def __init__(self, names, words, *, common: frozenset[str]):
        self._words = tuple(words)
        entries: list[_Entry] = []
        seen: set[str] = set()
        for word, is_name in [*((n, True) for n in names), *((w, False) for w in self._words)]:
            k = _key(word)
            if k and k not in seen:
                seen.add(k)
                entries.append(_Entry(word, k, jellyfish.metaphone(k), is_name))
        self._entries = entries
        self._keys = [e.key for e in entries]
        self._names = {i: e.key for i, e in enumerate(entries) if e.name}
        self._common = common

    def with_names(self, names) -> "RosterCorrector":
        return RosterCorrector(names, self._words, common=self._common)

    def _best(self, key: str, *, address: bool,
              present: frozenset[str] | None = None) -> tuple[float, int] | None:
        """The closest entry that matches `key`, as (score, index): the first listed among the
        closest, so a name before a lexicon word; None when two roster names are the closest.
        `present`, for the address: the keys of the names it may be."""
        choices = self._names if address else self._keys
        if present is not None:
            choices = {i: k for i, k in self._names.items() if k in present}
        sound = "" if address else jellyfish.metaphone(key)
        hits = [(score, i) for _, score, i in process.extract(key, choices, scorer=fuzz.ratio,
                                                              score_cutoff=PHONETIC, limit=None)
                if address or score >= DIRECT or (sound and sound == self._entries[i].sound)]
        if not hits:
            return None
        top = max(score for score, _ in hits)
        closest = sorted(i for score, i in hits if score == top)
        if sum(self._entries[i].name for i in closest) > 1:
            return None
        return top, closest[0]

    def _match(self, texts: list[str], *, address: bool = False,
               present: frozenset[str] | None = None) -> tuple[float, str] | None:
        """The best replacement for a run of tokens, as (score, replacement)."""
        head = "".join(_key(t) for t in texts[:-1])
        last = texts[-1]
        forms = []
        if m := _POSSESSIVE.fullmatch(last):
            forms.append((head + _key(m.group(1)), m.group(2)))  # first, so it wins a tie
        forms.append((head + _key(last), ""))
        best = None
        for key, suffix in forms:
            hit = self._best(key, address=address, present=present)
            if hit is not None and (best is None or hit[0] > best[0]):
                best = (hit[0], self._entries[hit[1]].word + suffix)
        return best

    def _is_common(self, token: str) -> bool:
        word = _norm(token)
        if word in self._common or word in NOT_ADDRESS:
            return True
        m = _POSSESSIVE.fullmatch(word)
        return m is not None and (m.group(1) in self._common or m.group(1) in NOT_ADDRESS)

    def _best_at(self, text: str, tokens: list[re.Match], i: int,
                 longest: int = MAX_RUN) -> tuple[float, int, str] | None:
        """The best run of up to `longest` tokens that starts at token `i`, as (score, run
        length, replacement)."""
        best = None
        for k in range(1, longest + 1):
            run = tokens[i:i + k]
            if len(run) < k or (k > 1 and not _RUN_GAP.fullmatch(text, run[-2].end(),
                                                                   run[-1].start())):
                break
            texts = [t.group() for t in run]
            if texts[-1].isdecimal():
                break
            if all(self._is_common(t) for t in texts):
                continue
            hit = self._match(texts)
            if hit is not None and (best is None or hit[0] >= best[0]):
                best = (hit[0], k, hit[1])
        return best

    def correct(self, text: str, *, present=None) -> str:
        """The text put right. `present`: the names of the members who can hear him; None: the
        whole roster."""
        room = None if present is None else frozenset(_key(n) for n in present)
        tokens = list(_TOKEN.finditer(text))
        fixes: list[tuple[int, int, str]] = []  # (start, end, replacement), in order
        i = 0
        if (tokens and _COMMA.match(text, tokens[0].end())
                and _norm(tokens[0].group()) not in NOT_ADDRESS):
            hit = self._match([tokens[0].group()], address=True, present=room)
            if hit is not None:
                fixes.append((tokens[0].start(), tokens[0].end(), hit[1]))
                i = 1
        while i < len(tokens):
            token = tokens[i].group()
            if (sound_alike := SOUNDALIKES.get(_norm(token))) is not None:
                fixes.append((tokens[i].start(), tokens[i].end(), _cased(sound_alike, token)))
                i += 1
                continue
            best = self._best_at(text, tokens, i)
            if best is not None and best[1] > 1:
                after = self._best_at(text, tokens, i + 1)
                if after is not None and after[0] >= best[0]:  # this token only lowers it
                    best = self._best_at(text, tokens, i, longest=1)  # alone, if at all
            if best is None:
                i += 1
                continue
            _, k, word = best
            fixes.append((tokens[i].start(), tokens[i + k - 1].end(), word))
            i += k
        out, last = [], 0
        for start, end, word in fixes:
            out += [text[last:start], word]
            last = end
        out.append(text[last:])
        return "".join(out)


def common_words() -> frozenset[str]:
    """Every word of misaki's US dictionaries (its gold and silver lists), as RosterCorrector's
    `common` takes them: about 178,000. Empty when misaki (the tts extra) is not installed.
    Imports nothing of misaki but its package (M9), and reads both files each call: the service
    reads them once."""
    try:
        data = importlib.resources.files("misaki") / "data"
    except ModuleNotFoundError:
        return frozenset()
    words: set[str] = set()
    for name in _MISAKI_US:
        words.update(json.loads((data / name).read_text(encoding="utf-8")))
    return frozenset(_norm(w) for w in words)
