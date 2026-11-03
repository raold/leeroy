"""Text for the ear: a member's line as the mind sends it (chat text: lowercase, WoW slang,
abbreviations, emoticons, item links) rewritten so a TTS engine can speak it (draft section
4.2). ear() goes in this order:

1. WoW's escape codes: a link |H...|h[Name]|h is read as its Name; colours (|cAARRGGBB, |r),
   textures (|T...|t) and |n go.
2. Unicode is folded (NFKC) and curly quotes made straight, so a typographic apostrophe
   (U+2019 in "I'm") is kept, not dropped as a symbol. A dash is a pause, ", " (as the mind's
   repair writes it); between two numbers it is "to".
3. *Actions* are not speech: they go, a laughing one counted as a laugh. **Bold** keeps its
   words.
4. Laughs (LAUGHS: lol, lmao, haha, :), <3, xD, the laughing emoji, and their drawn-out forms)
   are counted, and go. An engine that reads sound tags (SOUND_TAGS) gets a sound in their place,
   and in place of the cues ugh, omg and wtf: step 10. Other emoticons (:( D: o/) go uncounted,
   in step 6.
5. ABBREVIATIONS (e.g., i.e., etc., vs., w/, b/c) are written out, since misaki spells "e.g."
   letter by letter. % & + @ # are said as words, which step 6 would otherwise drop.
6. strip_markup's symbols: everything but letters, digits, spaces and .,!?'-:;… goes.
7. CHAT_SPEAK is expanded: whole words in any case, the punctuation around them kept, "r" only
   between two words. Initials such as "U.S." are left to misaki, which spells them.
8. Spaces and punctuation are tidied. A line left with no letter or digit is "", which the
   service drops as `empty` (Review Focus 4). Case is kept: misaki stresses an ALL-CAPS word.
9. A line over MAX_SPOKEN_CHARS is cut after the last sentence that fits, else at a word.
10. For Kokoro, the lexicon's IPA as misaki's [word](/IPA/) markup, which no other engine reads.
    For any other engine, the lexicon's respellings; for one that reads sound tags, first the
    sounds (cues_to_sounds, round 3's mapper) and a leading hum as "Mm..." or "Hmm...".

The text the mind sent is never changed: this is what the engine reads. EarText.plain is each
sentence of an engine that reads letters as words alone, no sound and no respelling, for Kokoro
to read if that engine fails on it (Kokoro never gets a tag).
"""
import re
import unicodedata
from dataclasses import dataclass

from .lexicon import Lexicon

MAX_SPOKEN_CHARS = 400  # the words; the lexicon's markup and a laugh cue come on top
KEPT = frozenset(".,!?'-:;…")  # the marks strip_markup keeps beside letters, digits and spaces

CHAT_SPEAK: dict[str, str] = {
    # the plan's words; afk and aoe as joined capitals, since misaki reads a lone "A" as "uh"
    # when it is tagged a determiner, and spells AFK and AOE the way it spells ICC
    "afk": "AFK", "aoe": "AOE", "brb": "be right back", "dps": "D P S", "gg": "good game",
    "grats": "grats", "gratz": "grats", "gz": "grats", "hp": "H P", "idk": "I don't know",
    "imo": "in my opinion", "inc": "incoming", "np": "no problem", "ofc": "of course",
    "omg": "oh my god", "oom": "out of mana", "pls": "please", "plz": "please", "r": "are",
    "rdy": "ready", "tbh": "to be honest", "thx": "thanks", "ty": "thanks", "u": "you",
    "ur": "your",
    # more chat: misaki knows none of these, or reads them wrong ("yea" as "yay", "ye" as
    # "yee", "k" as the letter; the plan's "ur" as "er")
    "atm": "at the moment", "btw": "by the way", "cya": "see ya", "gj": "good job",
    "gl": "good luck", "gn": "good night", "gtg": "got to go", "hf": "have fun",
    "idc": "I don't care", "irl": "in real life", "jk": "just kidding", "k": "okay",
    "kk": "okay", "lmk": "let me know", "nvm": "never mind", "omw": "on my way",
    "ppl": "people", "sry": "sorry", "ttyl": "talk to you later", "wb": "welcome back",
    "wp": "well played", "ye": "yeah", "yea": "yeah", "yw": "you're welcome",
    # raid words
    "bl": "bloodlust", "bres": "battle rez", "brez": "battle rez", "cds": "cooldowns",
    "gs": "gear score", "hc": "heroic", "ilvl": "item level", "lvl": "level",
    # acronyms: misaki spells an unknown word in capitals letter by letter ("ICC" and "DKP"
    # measured, draft 4.2) but has no reading for these in lower case
    "bg": "BG", "dk": "DK", "dkp": "DKP", "ffs": "FFS", "icc": "ICC", "lfg": "LFG",
    "lfm": "LFM", "toc": "TOC", "voa": "VOA", "wtf": "WTF", "xp": "XP",
    # contractions typed without the apostrophe; "wont" and the British "cant" are words that
    # misaki says otherwise
    "arent": "aren't", "cant": "can't", "couldnt": "couldn't", "didnt": "didn't",
    "doesnt": "doesn't", "dont": "don't", "hasnt": "hasn't", "havent": "haven't",
    "hes": "he's", "im": "I'm", "isnt": "isn't", "ive": "I've", "shes": "she's",
    "shouldnt": "shouldn't", "thats": "that's", "theyre": "they're", "wasnt": "wasn't",
    "whats": "what's", "wont": "won't", "wouldnt": "wouldn't", "youll": "you'll",
    "youre": "you're", "youve": "you've",
    # the pronoun: misaki gives a lone lowercase i primary stress, the pronoun I secondary
    "i": "I",
}
# dotted and slashed, matched in any case; a final dot may be left off ("etc", "vs")
ABBREVIATIONS: dict[str, str] = {
    "a.k.a.": "also known as", "aka": "also known as", "approx.": "approximately",
    "b/c": "because", "e.g.": "for example", "etc.": "et cetera", "i.e.": "that is",
    "vs.": "versus", "w/": "with", "w/e": "whatever", "w/o": "without",
}
# casefolded; drawn-out forms (lool, lmaooo, hahaha, xDD, :)))) count too
LAUGHS: frozenset[str] = frozenset({
    "lol", "lolz", "lul", "lel", "kek", "lmao", "lmfao", "rofl", "roflmao", "haha", "hehe", "xd",
    ":)", ":-)", ":d", ":-d", ";)", ";-)", ":p", ":-p", "=)", "(:", "^^", "^_^", "<3",
    # the faces grinning, smiley, smile, beaming, squinting, sweat smile, rolling on the floor,
    # tears of joy, slightly smiling, smiling eyes, smiling (U+263A), winking, the two laughing
    # cats, the three with a tongue, heart eyes, smiling with hearts, throwing a kiss, and the
    # hearts U+2764, U+2665 and two hearts
    "\U0001F600", "\U0001F603", "\U0001F604", "\U0001F601", "\U0001F606", "\U0001F605",
    "\U0001F923", "\U0001F602", "\U0001F642", "\U0001F60A", "\u263a", "\U0001F609",
    "\U0001F639", "\U0001F638", "\U0001F61B", "\U0001F61C", "\U0001F61D", "\U0001F60D",
    "\U0001F970", "\U0001F618", "\u2764", "\u2665", "\U0001F495",
})
# the bracket tags each engine's tokenizer lists (Chatterbox Turbo's added_tokens.json at the
# pinned revision, the listening kit). Kokoro reads none.
SOUND_TAGS: dict[str, frozenset[str]] = {"chatterbox-turbo": frozenset({
    "advertisement", "angry", "chuckle", "clear throat", "cough", "crying", "dramatic", "fear",
    "gasp", "groan", "happy", "laugh", "narration", "sarcastic", "shush", "sigh", "sniff",
    "surprised", "whispering"})}
MAX_SOUNDS = 2  # a line's; the owner's ruling after round 3


@dataclass(frozen=True, slots=True)
class EarText:
    text: str  # "" when nothing is left to say
    laughs: int
    sentences: tuple[str, ...]
    plain: tuple[str, ...] = ()  # one per sentence, for an engine that is not Kokoro


# 1. WoW's escape codes
_LINK = re.compile(r"\|H[^|]*\|h\[?([^\]|]*)\]?\|h")
_CODE = re.compile(r"\|c[0-9A-Fa-f]{0,8}|\|r|\|T[^|]*\|t")
_BREAK = re.compile(r"\|n|\|\|")

# 2. Unicode
_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2212": "-"})
_RANGE = re.compile(r"(?<=\d)\s*[\u2013\u2014\u2015]\s*(?=\d)")
_DASH = re.compile(r"\s*[\u2013\u2014\u2015]\s*")

# 3. actions
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ACTION = re.compile(r"\*([^*]{1,60})\*")
_LAUGHING = re.compile(r"\b(?:laughs?|laughing|giggles?|giggling|chuckles?|chuckling|"
                       r"snickers?|cackles?|smiles?|smiling|grins?|grinning)\b", re.IGNORECASE)

# 4. laughs and emoticons, on casefolded tokens
_LAUGH_WORD = re.compile(r"l(?:o+l+)+[sz]?|lu+l+[sz]?|le+l+|ke+k+w?|lm+f*a+o+|ro+f+l+(?:ma+o+)?"
                         r"|a?(?:ha+){2,}h?|(?:he+){2,}h?|x+d+")
_SMILE = r"[:;=][-'^o]?(?:\)+|\]+|d+|p+)|\(+[-'^o]?[:;=]|<3+|\^[_.-]?\^|x+d+"
_OTHER_FACE = (r"[:;=][-'^o]?(?:\(+|\[+|/+|\\+|\|+|o+|0+|s+|c+|x+|3+|\*+|@+|\$+)"
               r"|\)+[-']?[:;=]|d+[-']?[:;=]|</3+|-_+-|[o0][_.][o0]|;_+;|t_+t|>[_.]<"
               r"|\\o/|\\o|o/")
_HAPPY = re.compile(_SMILE)
_EMOTICON = re.compile(f"(?:{_SMILE}|{_OTHER_FACE})")
# a face glued to a word's end ("thanks:)", "ok:(", "love<3"), never after a digit (10:30)
_GLUED = re.compile(r"(?<=[^\W\d_])(?:[:;=][-'^o]?(?:\)+|\(+|\]+|d+|p+|/+|\|+|o+)|<3+)$",
                    re.IGNORECASE)
_NOT_LAUGHS = frozenset({"lull", "lulls", "loll", "lolls"})  # words the laugh patterns match
_TAIL = re.compile(r"[.,!?]+$")
_WRAP = "\"'()[]{}*_~"
_LAUGH_EMOJI = frozenset(x for x in LAUGHS if len(x) == 1)
# Private-use characters hold a sound's place until the text is done. A laugh has no words to
# say, so it is a mark alone; a cue with words (ugh, omg, wtf) is a mark, its words and CUE_END,
# so the words can stay when the sound does not. Round 3's table: a "lol" or a face is a chuckle,
# a "lmao" or a "haha" a laugh, an "ugh" a sigh, an "omg" or a "wtf" a gasp.
_SOUNDS = {"\ue000": "chuckle", "\ue001": "laugh", "\ue002": "sigh", "\ue003": "gasp"}
_CHUCKLE, _LAUGH, _SIGH, _GASP = _SOUNDS
_CUE_END = "\ue004"
_MARKS = "".join(_SOUNDS)
_KEPT_MARKS = _MARKS + _CUE_END
_CUE = re.compile(f"([{_MARKS}])(?:([^{_KEPT_MARKS}]*){_CUE_END})?([.,;:!?]*)")
_BIG_LAUGH = re.compile(r"lm+f*a+o+|ro+f+l+(?:ma+o+)?|a?(?:ha+){2,}h?")
_WORD_CUES = ((re.compile(r"ug+h+"), _SIGH, "ugh"), (re.compile(r"om+g+"), _GASP, "omg"),
              (re.compile(r"wtf+"), _GASP, "wtf"))
_SPOKEN_LAUGH = re.compile(r"(?<![\w'])ha(?:[ ,]+ha)+(?![\w'])", re.IGNORECASE)  # "Ha ha, no"
_HUM = re.compile(r"(h?)m{2,}(?![\w'])[\s.,;:!-]*", re.IGNORECASE)
_TAG = re.compile(r"\[[a-z ]+\]")


def _abbreviation(key: str) -> str:
    if key.endswith("."):
        return re.escape(key[:-1]) + r"\.?(?!\w)"
    return re.escape(key) + ("" if key.endswith("/") else r"(?!\w)")  # w/flasks


def _of(m: re.Match) -> str:
    """3/12 is a count, "3 of 12", when the second number is larger; 24/7 and 50/50 stay."""
    return f"{m[1]} of {m[2]}" if int(m[2]) > int(m[1]) else m[0]


def _to(m: re.Match) -> str:
    """10-15 is a range, "10 to 15", when the second number is larger; a 3-2 score stays."""
    return f"{m[1]} to {m[2]}" if int(m[2]) > int(m[1]) else m[0]


# 5. abbreviations and symbols said as words
_ABBREVIATION = re.compile(r"(?<![\w./])(?:" + "|".join(
    _abbreviation(k) for k in sorted(ABBREVIATIONS, key=len, reverse=True)) + ")", re.IGNORECASE)
_EXPANSION = {k.rstrip("."): v for k, v in ABBREVIATIONS.items()}
_SAID = ((re.compile(r"(?<=\d)\s*%"), " percent "), (re.compile(r"&"), " and "),
         (re.compile(r"(?<!\S)@(?!\S)"), " at "), (re.compile(r"\+"), " plus "),
         (re.compile(r"#(?=\d)"), " number "),
         # a lone pair, not part of a date (3/12/2026, 2026-10-31) or a countdown (3-2-1)
         (re.compile(r"(?<![\d/.])(\d{1,6})/(\d{1,6})(?![\d/])"), _of),
         (re.compile(r"(?<![\d.-])(\d{1,6}) ?- ?(\d{1,6})(?![\d-])"), _to))

# 7. chat speak, one token at a time: initials whole (so the u of "U.S." stays misaki's to
# spell, not "you"), or a word with its inner apostrophes and hyphens
_TOKEN = re.compile(r"[^\W\d_](?:\.[^\W\d_])+\.?|[^\W_]+(?:['-][^\W_]+)*")

# 8. tidying
_GAP = re.compile(r"\s+")
_BEFORE_MARK = re.compile(r" +(?=[,.!?;:])")
_SPACED_ENDS = re.compile(r"([.!?])(?: [.!?]+)+")  # "now. ." (a laugh went) -> "now."
_PAUSE_BEFORE_END = re.compile(r"[,;:]+ ?(?=[.!?])")
_RUN = re.compile(r"([!?,;:])\1+")
_DOTS = re.compile(r"\.{2,}")
_LONE_APOSTROPHE = re.compile(r"(?<!\S)'+(?!\S)")
_LEAD = re.compile(r"^[\s,;:.!?-]+")
_TRAIL = re.compile(r"[\s,;:-]+$")
_SAYABLE = re.compile(r"[^\W_]")

# 9. sentences: after . ! or ?, not after an ellipsis, initials (U.S.) or a title (Mr.)
_TITLES = ("Mr", "Mrs", "Ms", "Dr", "St", "Mt", "Prof", "Jr", "Sr")
_SENTENCE_GAP = re.compile(r"(?<=[!?])\s+|(?<=(?<!\.)\.)(?<![^\W\d_]\.[^\W\d_]\.)"
                           + "".join(rf"(?<!\b{t}\.)" for t in _TITLES) + r"\s+", re.IGNORECASE)


def _wow_codes(t: str) -> str:
    return _BREAK.sub(" ", _CODE.sub("", _LINK.sub(r"\1", t)))


def _normalize(t: str) -> str:
    t = unicodedata.normalize("NFKC", t.translate(_QUOTES))
    return _DASH.sub(", ", _RANGE.sub(" to ", t))


def _actions(t: str, mark: str) -> tuple[str, int]:
    laughs = 0

    def drop(m: re.Match) -> str:
        nonlocal laughs
        if _LAUGHING.search(m.group(1)):
            laughs += 1
            return f" {mark} "
        return " "
    return _ACTION.sub(drop, _BOLD.sub(r"\1", t)), laughs


def _split_face(tok: str) -> tuple[str, str, str]:
    """(word, face, tail) of a whitespace-separated token: an emoticon that is the whole token
    or is glued to a word's end, and the sentence marks after it."""
    if _EMOTICON.fullmatch(tok.casefold()):  # ;_; and (: end in marks of their own
        return "", tok, ""
    m = _TAIL.search(tok)
    core, tail = (tok[:m.start()], m.group()) if m and m.start() else (tok, "")
    if _EMOTICON.fullmatch(core.casefold()):
        return "", core, tail
    g = _GLUED.search(core)
    return (core[:g.start()], g.group(), tail) if g else (core, "", tail)


def _is_laugh_word(word: str) -> bool:
    low = word.strip(_WRAP).casefold()
    if not low or low in _NOT_LAUGHS:
        return False
    return low in LAUGHS or _LAUGH_WORD.fullmatch(low) is not None


def _laugh_mark(word: str, sounds: bool) -> str:
    if not sounds:
        return ""
    return _LAUGH if _BIG_LAUGH.fullmatch(word.strip(_WRAP).casefold()) else _CHUCKLE


def _word_cue(word: str) -> str:
    low = word.casefold()
    for pattern, mark, said in _WORD_CUES:
        if pattern.fullmatch(low):
            return f"{mark}{said}{_CUE_END}"
    return word


def _laughs(t: str, sounds: bool) -> tuple[str, int]:
    """The laughs counted, and with `sounds` every cue marked in its place, in one pass."""
    mark = _CHUCKLE if sounds else ""
    laughs = sum(c in _LAUGH_EMOJI for c in t)
    t = "".join(f" {mark} " if c in _LAUGH_EMOJI else c for c in t)
    out = []
    for tok in t.split():
        word, face, tail = _split_face(tok)
        if word and _is_laugh_word(word):
            laughs, word = laughs + 1, _laugh_mark(word, sounds)
        elif word and sounds:
            word = _word_cue(word)
        if face and _HAPPY.fullmatch(face.casefold()):
            laughs, face = laughs + 1, mark
        out.append(" ".join(p for p in (word, face) if p) + tail)
    return " ".join(out), laughs


def _char(c: str, keep: str) -> str:
    if c.isalpha() or c.isdecimal() or c in KEPT or c == " " or c in keep:
        return c
    return "" if unicodedata.category(c) in ("Mn", "Me", "Cf") else " "


def _drop_symbols(t: str, keep: str = "") -> str:
    words = []
    for tok in t.split():
        word, face, tail = _split_face(tok)
        words.append(word + tail if face else tok)
    return "".join(_char(c, keep) for c in " ".join(words))


def _spaces(t: str) -> str:
    return _BEFORE_MARK.sub("", _GAP.sub(" ", t).strip())


def strip_markup(text: str) -> str:
    """WoW's codes out (a link read as its name), then emoticons, emoji and every symbol but
    .,!?'-:;… (curly quotes made straight first, so a typographic apostrophe survives); spaces
    tidied."""
    return _spaces(_drop_symbols(_normalize(_wow_codes(text))))


def _between_words(t: str, i: int, j: int) -> bool:
    return (i >= 2 and t[i - 1] == " " and t[i - 2].isalnum()
            and t[j:j + 1] == " " and t[j + 1:j + 2].isalnum())


def _chat_speak(t: str) -> str:
    def swap(m: re.Match) -> str:
        tok, low = m.group(), m.group().casefold()
        if low not in CHAT_SPEAK or (low == "r" and not _between_words(t, m.start(), m.end())):
            return tok
        return CHAT_SPEAK[low]
    return _TOKEN.sub(swap, t)


def _tidy(t: str) -> str:
    t = _GAP.sub(" ", _LONE_APOSTROPHE.sub("", t)).strip()
    t = _PAUSE_BEFORE_END.sub("", _SPACED_ENDS.sub(r"\1", t))
    t = _DOTS.sub("...", _RUN.sub(r"\1", _BEFORE_MARK.sub("", t)))
    t = _TRAIL.sub("", _LEAD.sub("", t))
    return t if _SAYABLE.search(t) else ""


def sentences(text: str) -> list[str]:
    """The text cut after each . ! or ? (an ellipsis is a pause, not an end); [] for none."""
    return [s for s in _SENTENCE_GAP.split(text.strip()) if s]


def _cut(t: str) -> str:
    if len(t) <= MAX_SPOKEN_CHARS:
        return t
    kept = ""
    for s in sentences(t):
        longer = f"{kept} {s}" if kept else s
        if len(longer) > MAX_SPOKEN_CHARS:
            break
        kept = longer
    if kept:
        return kept
    head = t[:MAX_SPOKEN_CHARS - 1]  # one sentence too long: cut at a word, then a full stop
    if t[MAX_SPOKEN_CHARS - 1] != " " and " " in head:
        head = head[:head.rindex(" ")]
    return head.rstrip(" ,;:-'.") + "."


def cues_to_sounds(t: str, known: frozenset[str]) -> str:
    """Round 3's mapper, on the one line the service has: each cue marked in `t` becomes its
    sound, in its own place, and its word is not said. At most MAX_SOUNDS a line, and only tags
    the tokenizer lists (`known`): past those a cue keeps its word, and a laugh, which has none,
    goes. A run of one sound is one sound. A line left with no word to say keeps its cues' words,
    since a sound alone is no line. A cue's own punctuation is not said either: a pause (...)
    stays after the sound, and a sound that ends a sentence leaves the words before it closed,
    with its ? or ! or else a full stop. Pure."""
    out = _placed(t, known, keep_words=False)
    return out if _SAYABLE.search(_TAG.sub("", out)) else _placed(t, known, keep_words=True)


def _placed(t: str, known: frozenset[str], *, keep_words: bool) -> str:
    out, last, made, sound = "", 0, 0, ""
    for m in _CUE.finditer(t):
        mark, words, tail = m.groups()
        between, last = t[last:m.start()], m.end()
        out += between
        tag = f"[{_SOUNDS[mark]}]"
        over = made >= MAX_SOUNDS or _SOUNDS[mark] not in known
        if words is not None and (keep_words or over):
            out += words + tail
            continue
        if over:
            out += tail
            continue
        if tag == sound and not between.strip():
            continue
        made, sound = made + 1, tag
        ends = bool(set(tail) & set(".!?")) or not _SAYABLE.search(_CUE.sub("", t[last:]))
        if "..." in tail:
            out = f"{out.rstrip()} {tag}..."
        elif ends:
            head = out.rstrip(" ,;:-")
            if _SAYABLE.search(_TAG.sub("", head)) and not head.endswith((".", "!", "?", "]")):
                head += "".join(c for c in tail if c in "!?") or "."
            out = f"{head} {tag}"
        else:
            out = f"{out.rstrip()} {tag}"
    return _spaces(_BEFORE_MARK.sub("", out + t[last:]))


def _hum(t: str) -> str:
    """A leading mm or hmm stays a sound, with its pause: "mm. keep talking" -> "Mm... keep
    talking"."""
    m = _HUM.match(t)
    if m is None:
        return t
    return f"{'Hmm' if m.group(1) else 'Mm'}... {t[m.end():]}".rstrip()


def _with_sounds(t: str) -> list[str]:
    """The sentences, a sound with no words of its own staying with the sentence before it."""
    out: list[str] = []
    for s in sentences(t):
        if out and not _SAYABLE.search(_TAG.sub("", s)):
            out[-1] += f" {s}"
        else:
            out.append(s)
    return out


def ear(text: str, *, lexicon: Lexicon, engine: str) -> EarText:
    """The line rewritten for `engine` (a decision.ENGINES name), with its laughs counted."""
    known = SOUND_TAGS.get(engine)
    sounds = known is not None
    t = _normalize(_wow_codes(text))
    t, acted = _actions(t, _CHUCKLE if sounds else "")
    if sounds:
        t = _SPOKEN_LAUGH.sub("haha", t)
    t, laughed = _laughs(t, sounds)
    t = _ABBREVIATION.sub(lambda m: f" {_EXPANSION[m.group().casefold().rstrip('.')]} ", t)
    for pattern, said in _SAID:
        t = pattern.sub(said, t)
    t = _GAP.sub(" ", _drop_symbols(t, keep=_KEPT_MARKS if sounds else "")).strip()
    t = _cut(_tidy(_chat_speak(t)))
    laughs = acted + laughed
    if engine == "kokoro":
        t = lexicon.apply(t)
        return EarText(t, laughs, tuple(sentences(t)))
    if sounds and t:
        t = cues_to_sounds(_hum(t), known)
    said = _with_sounds(t)
    spoken = tuple(lexicon.respell(s, engine) for s in said)
    return EarText(" ".join(spoken), laughs, spoken, tuple(_tidy(_TAG.sub(" ", s)) for s in said))
