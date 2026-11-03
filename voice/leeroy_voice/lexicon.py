"""Pronunciations: words misaki's G2P gets wrong, given as IPA in misaki's `[word](/IPA/)`
markup so Kokoro says them right (draft section 4.2).

The built-in data/wow_lexicon.toml holds public WoW nouns only. The local
voice-data/lexicon.toml (member names and his nickname, Task 19) has the same shape and wins
over it, word by word and in any case. In a file each top-level key is a word and its value
the IPA; the one key `check` lists words known without an entry (the nouns Task 4 Step 5
checks against misaki). words() is every word of both, for the recognizer's name correction
(Task 9).

An engine that reads letters takes no IPA, so a word it says wrong is respelled for it alone, in
a table per engine, [respell.<engine>] Word = "Spelling". Those stay out of words(): the
recognizer never hears a spelling. The word may be private, so such an entry belongs in the local
file, never the built-in one.

The IPA uses misaki's English phonemes (PHONEMES), all of them in Kokoro's vocabulary. Any
other character would be dropped without a word (an ASCII g for misaki's U+0261, say), so it
is refused."""
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

BUILTIN = Path(__file__).with_name("data") / "wow_lexicon.toml"
LOCAL_NAME = "lexicon.toml"  # in the data dir
CHECK_KEY = "check"
RESPELL_KEY = "respell"
# misaki 0.9.4's US and GB vocabularies (en.py US_VOCAB, GB_VOCAB), and the ɐ it gives "a"
PHONEMES = frozenset("AIOQWYabdfhijklmnpstuvwzæðŋɐɑɒɔəɛɜɡɪɹɾʃʊʌʒʤʧˈˌːθᵊᵻʔ")
_MARKUP_CHARS = frozenset("[]()/")
MARKUP = re.compile(r"\[[^\]]+\]\([^)]*\)")  # misaki's [word](...) span, as its LINK_REGEX reads it
_APOSTROPHE = "['\u2019]"
# a whole word: not inside a longer word or a hyphenated or apostrophised compound, but a
# possessive 's may follow ("Kel'Thuzad's")
_BEFORE = r"(?<!\w)(?<!\w['\u2019-])"
_AFTER = r"(?!\w)(?!-\w)(?!['\u2019](?!s(?!\w))\w)"


class LexiconError(ValueError):
    pass


def _key(word: str) -> str:
    return " ".join(word.replace("\u2019", "'").split()).casefold()


def _word(word) -> str:
    w = " ".join(word.split()) if isinstance(word, str) else ""
    if not w or not (w[0].isalnum() and w[-1].isalnum()) or set(w) & _MARKUP_CHARS:
        raise LexiconError(f"{word!r}: a word starts and ends with a letter or digit and holds "
                           "no [ ] ( ) or /")
    return w


def _ipa(word: str, ipa) -> str:
    if not isinstance(ipa, str) or not ipa.strip():
        raise LexiconError(f"{word}: expected its IPA as a string, got {ipa!r}")
    p = ipa.strip()
    bad = "".join(sorted(set(p) - PHONEMES - {" "}))
    if bad:
        hint = " (misaki's g is \u0261, U+0261)" if "g" in bad else ""
        raise LexiconError(f"{word}: IPA {ipa!r} holds {bad!r}, outside misaki's phonemes{hint}")
    return p


def _pattern(word: str) -> str:
    return re.escape(word.replace("\u2019", "'")).replace("'", _APOSTROPHE).replace(r"\ ", r"\s+")


def _alternatives(words: Iterable[str]) -> re.Pattern:
    """Whole words in any case, the longest first."""
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(_BEFORE + "(?:" + "|".join(map(_pattern, ordered)) + ")" + _AFTER,
                      re.IGNORECASE)


class Lexicon:
    """`entries`: word -> IPA, matched as whole words in any case (a later entry replaces an
    earlier one of the same word). `extra_words`: words known without an entry, for words().
    `respellings`: engine -> {word: its spelling for that engine}."""

    def __init__(self, entries: dict[str, str], *, extra_words: Iterable[str] = (),
                 respellings: dict[str, dict[str, str]] | None = None):
        self._respellings = {
            engine: ({_key(_word(w)): _word(s) for w, s in table.items()}, _alternatives(table))
            for engine, table in (respellings or {}).items() if table}
        self._entries: dict[str, tuple[str, str]] = {}
        for word, ipa in entries.items():
            w = _word(word)
            self._entries[_key(w)] = (w, _ipa(w, ipa))
        self._words = {k: w for k, (w, _) in self._entries.items()}
        for word in extra_words:
            w = _word(word)
            self._words.setdefault(_key(w), w)
        words = [w for w, _ in self._entries.values()]
        self._re = _alternatives(words) if words else None

    def respell(self, text: str, engine: str) -> str:
        """Each word `engine` has a spelling for, as that spelling, whatever its case. A
        possessive 's stays."""
        found = self._respellings.get(engine)
        if found is None:
            return text
        table, pattern = found
        return pattern.sub(lambda m: table.get(_key(m.group()), m.group()), text)

    def apply(self, text: str) -> str:
        """Each entry's word as misaki's [word](/IPA/), kept as written; markup already in the
        text is left alone."""
        if self._re is None:
            return text
        out, last = [], 0
        for m in MARKUP.finditer(text):
            out += [self._mark(text[last:m.start()]), m.group()]
            last = m.end()
        out.append(self._mark(text[last:]))
        return "".join(out)

    def _mark(self, part: str) -> str:
        def mark(m: re.Match) -> str:
            # the regex's case folding can match what casefold() spells otherwise (Turkish ı)
            entry = self._entries.get(_key(m.group()))
            return f"[{m.group()}](/{entry[1]}/)" if entry else m.group()
        return self._re.sub(mark, part)

    def words(self) -> list[str]:
        """Every word, the entries' and the extra ones, in alphabetical order."""
        return sorted(self._words.values(), key=str.casefold)


def _respellings(raw) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict) or not all(isinstance(t, dict) for t in raw.values()):
        raise LexiconError(f"{RESPELL_KEY}: expected a table per engine of words to spellings")
    out = {}
    for engine, table in raw.items():
        for word, spelling in table.items():
            if not isinstance(spelling, str):
                raise LexiconError(f"{RESPELL_KEY}.{engine}.{word}: expected a spelling")
            _word(word), _word(spelling)
        out[engine] = dict(table)
    return out


def _read(path: Path) -> tuple[dict[str, str], list[str], dict[str, dict[str, str]]]:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as e:
        raise LexiconError(f"{path}: cannot read it: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise LexiconError(f"{path}: not UTF-8 (byte {e.start}); save it as UTF-8") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LexiconError(f"{path}: {e}") from e
    check = data.pop(CHECK_KEY, [])
    respell = data.pop(RESPELL_KEY, {})
    try:
        if not isinstance(check, list):
            raise LexiconError(f"{CHECK_KEY}: expected a list of words, got {check!r}")
        words = [_word(w) for w in check]
        entries = {_word(word): _ipa(word, ipa) for word, ipa in data.items()}
        spellings = _respellings(respell)
    except LexiconError as e:
        raise LexiconError(f"{path}: {e}") from None
    return entries, words, spellings


def load_lexicon(data_dir: Path) -> Lexicon:
    """The built-in file, then <data_dir>/lexicon.toml over it when it exists. A file that
    cannot be read or parsed, or a bad entry, raises LexiconError naming the file and word."""
    entries, words, spellings = _read(BUILTIN)
    path = Path(data_dir) / LOCAL_NAME
    if path.exists():
        more, more_words, more_spellings = _read(path)
        entries = {**entries, **more}  # a local word in another case still comes after
        words += more_words
        for engine, table in more_spellings.items():
            spellings[engine] = {**spellings.get(engine, {}), **table}
    return Lexicon(entries, extra_words=words, respellings=spellings)
