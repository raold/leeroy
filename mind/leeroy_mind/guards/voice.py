"""A member's voice sheet in the guards (Persona.speech; personas/speech.py). Three checks, all
string work on her stored sheet, none of them hard: an answer owed to him is rewritten once and
then goes by the owed rules (talk/pipeline.py: the best soft-only draft is kept).

- `voice_never`: the draft uses one of the words her sheet says she never uses. The rewrite
  hint names them (hint).
- `voice_sample_copy`: the draft holds one of her sample lines word for word. A sample is how
  she sounds, never a line to reuse.
- her own words (`says`) and the phrase cooldown (guards/phrases.py): a phrase that is one of
  her words is hers, so the conversation, day and per-member stock rules leave it alone, up to
  a ration (over_ration): at most about one in three of her lines in a conversation, and
  never fewer than SAY_FREE uses, counted over her last SAY_WINDOW lines there. Above it the
  cooldown names it as `phrase_conversation`, so a tic cannot take over.

Typing shortcuts (2026-11-17): the cooldown folds the cast's shortcuts back before it matches
("u" is "you", "tbh" is "to be honest": phrases.FOLD), and the draft the guards read is her
line before her typing is applied. So a never-word that is itself a shortcut ("u", "ur") is
matched only as typed, never as the word it stands for (a sheet that says she never types "u"
must not flag every "you"); and one of her own words that is a shortcut for a single function
word is no tic to ration.

Nothing here reads a database, logs or calls a model."""
from dataclasses import dataclass, field
from math import ceil

from leeroy_mind.text import strip_wow_markup

from . import phrases

SAY_WINDOW = 9   # her earlier lines in the conversation the ration counts
SAY_FREE = 2     # uses in the window (the draft's included) the ration never minds
SAY_SHARE = 3    # one line in this many
COPY_WORDS = 4   # a sample this long counts as copied when the draft holds it as a run
MAX_NAMED = 3
CODES = ("voice_never", "voice_sample_copy")


@dataclass(frozen=True)
class Sheet:
    says: tuple[tuple[str, ...], ...] = field(default=(), repr=False)   # folded words
    # (as written, its words, typed): typed words are matched unfolded (a shortcut such as "u")
    never: tuple[tuple[str, tuple[str, ...], bool], ...] = field(default=(), repr=False)
    samples: tuple[tuple[str, ...], ...] = field(default=(), repr=False)
    # her words the ration counts: `says` less a shortcut for one function word
    rationed: tuple[tuple[str, ...], ...] = field(default=(), repr=False)


NONE = Sheet()


def _strs(value) -> list[str]:
    return [x for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def sheet_of(stored) -> Sheet:
    """The sheet of a stored persona's JSON (persona_core.json as a dict); NONE without one,
    or when it is not shaped as one."""
    speech = stored.get("speech") if isinstance(stored, dict) else None
    if not isinstance(speech, dict):
        return NONE
    samples = speech.get("samples")
    lines = [x for x in samples.values() if isinstance(x, str)] if isinstance(samples, dict) else []
    says = [(phrases._words(x), typed(x)) for x in _strs(speech.get("says"))]
    return Sheet(
        tuple(w for w, _ in says if w),
        tuple((" ".join(x.split()), typed(x), True) if typed(x) != phrases._words(x)
              else (" ".join(x.split()), phrases._words(x), False)
              for x in _strs(speech.get("never"))),
        tuple(w for w in (phrases._words(strip_wow_markup(x)) for x in lines) if w),
        tuple(w for w, raw in says if w and not (w != raw and len(w) == 1
                                                 and not phrases._content(w[0]))))


def typed(text: str) -> tuple[str, ...]:
    """The words of `text` as typed: lower case, apostrophes dropped, no shortcut folded."""
    return tuple(m.group(0).casefold().replace("\u2019", "'").replace("'", "")
                 for m in phrases._WORD.finditer(text or ""))


def _run(words, entry) -> bool:
    n = len(entry)
    return bool(n) and any(tuple(words[i:i + n]) == tuple(entry) for i in range(len(words) - n + 1))


def never_used(text: str, sheet: Sheet) -> tuple[str, ...]:
    """The never-words the draft uses, as her sheet writes them: a run of whole words, or for
    an entry with no word in it (a mark such as "!!") the mark itself."""
    if not sheet.never:
        return ()
    plain = strip_wow_markup(text or "")
    words, raw, folded = phrases._words(plain), typed(plain), plain.casefold()
    return tuple(name for name, entry, as_typed in sheet.never
                 if (_run(raw if as_typed else words, entry) if entry
                     else name.casefold() in folded))[:MAX_NAMED]


def copies_sample(text: str, sheet: Sheet) -> bool:
    """The draft holds one of her sample lines word for word: as a run of words when the sample
    has COPY_WORDS or more, else when it is the whole draft."""
    if not sheet.samples:
        return False
    words = phrases._words(strip_wow_markup(text or ""))
    return any(_run(words, s) if len(s) >= COPY_WORDS else tuple(words) == s
               for s in sheet.samples)


def hers(words, sheet: Sheet) -> bool:
    """A phrase (folded words) is one of her words: it holds one whole and every other word in
    it is a function word, or it is a run inside one."""
    words = tuple(words)
    for entry in sheet.says:
        if _run(words, entry):
            if not any(phrases._content(w) for w in words if w not in entry):
                return True
        elif len(words) < len(entry) and _run(entry, words):
            return True
    return False


def over_ration(uses: int, lines: int) -> bool:
    """One of her words is over its ration when the draft would be its use number `uses` + 1
    within her last `lines` lines of this conversation and the draft: more than SAY_FREE and
    more than one in SAY_SHARE."""
    return uses + 1 > max(SAY_FREE, ceil((lines + 1) / SAY_SHARE))


def any_said(toks, sheet: Sheet) -> bool:
    """One of her own words is in the draft (phrases.tokens of it)."""
    norms = [t.norm for t in toks]
    return any(_run(norms, entry) for entry in sheet.rationed)


def rationed(toks, talk_words, sheet: Sheet) -> list[tuple[int, int]]:
    """The spans (first, last token index) of her own words in the draft that are over their
    ration. `toks`: phrases.tokens of the draft; `talk_words`: the folded words of each of her
    earlier lines in this conversation, newest first."""
    if not sheet.rationed:
        return []
    window = talk_words[:SAY_WINDOW]
    norms = [t.norm for t in toks]
    spans = []
    for entry in sheet.rationed:
        n = len(entry)
        at = next((i for i in range(len(norms) - n + 1) if tuple(norms[i:i + n]) == entry), None)
        if at is None:
            continue
        if over_ration(sum(_run(words, entry) for words in window), len(window)):
            spans.append((toks[at].i, toks[at + n - 1].i))
    return spans


def hint(never) -> str:
    """The retry's sentence naming the words she never uses; empty when there are none."""
    quoted = [f"'{w}'" for w in dict.fromkeys(never) if w][:MAX_NAMED]
    if not quoted:
        return ""
    said = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]
    return f"You never say {said}."
