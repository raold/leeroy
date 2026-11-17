"""A member's voice sheet (Persona.speech, schema.Speech): how she talks, shown late in every
prompt of hers in place of the generic trait sentences and the canned attachment and humour
lines (talk/prompt.py PromptParts.voice; personas/card.py leaves those out for her).

- voice_block: the block a turn shows. Her register and sentence shape, the words she uses
  ("only now and then"), the words she never uses and what she brings up
  unasked, on every turn; how she is with him (`with_him`) only on a turn that is his; what her
  household means for it (`stance`) only where only he reads (a whisper to him, his DM); then
  one or two sample lines under SAMPLES_HEAD, which says they are how she sounds and never
  lines to reuse. The block without its samples never passes BLOCK_TOKENS estimated tokens: a
  sheet too long for a turn loses, in order (TRIMS), its fourth topic, its seventh and eighth
  words, its never-words beyond two (the guards hold her to all of them, shown or not), its
  third topic, the rest of its never-words, then words and a topic down to four and one
  (fitted says what a turn keeps); what is left is cut at a word.
- pick_samples: which samples a turn shows. The first by the turn's want (WANT_SAMPLE: the
  twelve wants of talk.intent onto the five kinds), the second a seeded draw among the others,
  so the same line is not shown every turn. A flirt sample is shown only on a turn that is
  his; one she already sent word for word is never shown again.
- speech_errors: the schema's checks (validate_persona): the sample kinds, flirt only for a
  woman and never beside warm, plain one-line text, no entry both said and never said, each
  sample within SAMPLE_TOKENS, and a block that fits BLOCK_TOKENS once trimmed (her register,
  shape, `with_him` and `stance` are never trimmed, so they must leave room).
- screen: what an import refuses beyond the persona screens (personas.io._screen: his real
  name, romance aimed at a member), each as a code: a later era's slang or year, a guild
  member's name in a sample, a never-word inside a sample, stock slop among her words, a
  never-word her own typing produces, a shape longer than her typing's lines.
- stance_current: her stance is shown only while her household is the one the sheet was
  stamped with at import; public_block: the part of the sheet the night's readers get.
- The samples a turn shows are also the pipeline's `Composed.samples`: a line that copies one
  word for word (3 words or more) is the existing fatal `sample` tell in generation, as it was
  for the generator's samples, and is regenerated. The guards' `voice_sample_copy` is the soft
  code for all five samples, shown or not.
- word_range: the line length her sentence shape names ("clipped, three to seven words"), for
  talk/shape.py's word draw.

Pure: no clock, no I/O, no logging. The seeded draw is the caller's own stream."""
import re

from ..text import estimate_tokens, has_meta, normalize_words, truncate_words
from .schema import SAMPLE_KINDS, Persona, Speech

BLOCK_TOKENS = 120   # the block without its samples, whatever the turn shows
SAMPLE_TOKENS = 55   # one sample line
# What a block over BLOCK_TOKENS keeps, tried in order: (her words, never-words, topics)
TRIMS = ((8, 4, 4), (8, 4, 3), (7, 4, 3), (6, 4, 3), (6, 2, 3), (6, 2, 2), (6, 0, 2), (5, 0, 2),
         (4, 0, 2), (4, 0, 1))
MAX_SAMPLES = 2      # shown on one turn
NOMINAL_HUMAN = "Guildmaster"  # the cap is checked with a name this long in "With <him>:"
RECIPE_TAG = "sheet-1"  # learn.recipe.voice_used: `voice.sheet-1` on lines whose prompt had one

VOICE_HEAD = "Your voice:"  # how the block opens
SAMPLES_HEAD = "How you sound, not lines to reuse (never copy one; say new things this way):"

# The twelve wants (talk.intent.Intent codes) onto the five sample kinds; "flirt" reads as
# "warm" for a member whose sheet has that instead (_kind_for).
WANT_SAMPLE = {
    "vent": "tired", "share": "answer", "ask": "answer", "tease": "tease", "flirt": "flirt",
    "reassure_seek": "flirt", "gossip": "tease", "deflect": "tired", "small_talk": "answer",
    "react": "answer", "boundary": "annoyed", "logistics": "answer",
}
DEFAULT_KIND = "answer"
ORDER = SAMPLE_KINDS  # a fixed order keeps a seeded draw stable

_NUMBER = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER) + r")"
_RANGE = re.compile(_NUM + r"\s*(?:to|-|–|or)\s*" + _NUM + r"\s+words?\b", re.IGNORECASE)
_LATER_YEAR = re.compile(r"\b20[1-9]\d\b")


def _one(s: str) -> str:
    return " ".join(s.split())


def _sentence(s: str) -> str:
    s = _one(s)
    return s if not s or s[-1] in ".!?" else s + "."


def _listed(items) -> str:
    return ", ".join(x for x in (_one(i) for i in items) if x)


def _body(speech: Speech, human: str, to_him: bool, private: bool,
          keep: tuple[int, int, int]) -> str:
    says, never, topics = keep
    lines = [_sentence(f"{VOICE_HEAD} {_one(speech.register)}; {_one(speech.shape)}")]
    words = f"You say, only now and then: {_listed(speech.says[:says])}."
    if speech.never[:never]:
        words += f" Never: {_listed(speech.never[:never])}."
    lines.append(words)
    brought = [x for x in (_one(b).rstrip(".") for b in speech.brings_up[:topics]) if x]
    if brought:
        lines.append("Unasked, you bring up: " + "; ".join(brought) + ".")
    him = [_sentence(x) for x in ((speech.with_him if to_him else ""),
                                  (speech.stance if to_him and private else "")) if _one(x)]
    if him:
        lines.append(f"With {human}: " + " ".join(him))
    return "\n".join(lines)


def fitted(speech: Speech, human: str = NOMINAL_HUMAN, *, to_him: bool = True,
           private: bool = True) -> tuple[tuple[int, int, int], int]:
    """(what the turn's block keeps: her words, never-words and topics; its estimated tokens):
    the first of TRIMS within BLOCK_TOKENS, else the last (which voice_block then cuts)."""
    for keep in TRIMS:
        size = estimate_tokens(_body(speech, human, to_him, private, keep))
        if size <= BLOCK_TOKENS:
            break
    return (min(keep[0], len(speech.says)), min(keep[1], len(speech.never)),
            min(keep[2], len(speech.brings_up))), size


def voice_block(speech: Speech, human: str, *, to_him: bool = False, private: bool = False,
                samples=()) -> str:
    """The block for one turn. `human`: what the guild calls him. `to_him`: the turn is his
    (her `with_him` line is shown); `private`: only he reads it (her `stance` too). `samples`:
    the lines pick_samples chose, after the block and outside its BLOCK_TOKENS."""
    keep, _ = fitted(speech, human, to_him=to_him, private=private)
    body = truncate_words(_body(speech, human, to_him, private, keep), BLOCK_TOKENS * 4)
    shown = [x for x in (_one(s) for s in samples) if x]
    return "\n".join([body, *([SAMPLES_HEAD] + ["- " + s for s in shown] if shown else [])])


def stance_current(p: Persona) -> bool:
    """Her stance may be shown: it was written for the household her card has now (the sheet's
    `household` stamp equals her real_life.household, spacing aside). After a household change
    (the night's arc, an owner edit) it is stale and stays out of every prompt until a sheet is
    imported again."""
    s = p.speech
    return (s is not None and s.household is not None
            and _one(s.household).casefold() == _one(p.real_life.household).casefold())


def public_block(speech: Speech) -> str:
    """The sheet's public part, as the night's readers get it (card.render_night_card): her
    register, shape, words, never-words and topics, as a turn that is not his shows them. Never
    `with_him`, `stance` or a sample."""
    return voice_block(speech, "", to_him=False, private=False)


def block_tokens(speech: Speech) -> int:
    """The block's estimated tokens at its fullest (a private turn of his) once trimmed,
    samples left out."""
    return fitted(speech)[1]


def _kind_for(want: str, speech: Speech) -> str:
    kind = WANT_SAMPLE.get(want or "", DEFAULT_KIND)
    if kind == "flirt" and "flirt" not in speech.samples:
        kind = "warm"
    return kind


def pick_samples(speech: Speech, want: str, rng, *, to_him: bool = False, sent=(),
                 n: int = MAX_SAMPLES) -> list[str]:
    """Up to `n` of her sample lines for a turn whose want is `want` ("" when it has none):
    the want's kind first, then a draw with `rng` (the caller's seeded stream, one draw) among
    the rest. A flirt sample is offered only when `to_him`; one whose words are in `sent` (her
    own lines, as sent) is never offered: a sample is how she sounds, not a line to say again.
    The want's kind falls back to "answer", then to the draw."""
    said = [normalize_words(x) for x in sent if x]

    def fresh(text: str) -> bool:
        words = normalize_words(text)
        return bool(words) and not any(words in s for s in said)

    offered = [k for k in ORDER if k in speech.samples and fresh(speech.samples[k])
               and (k != "flirt" or to_him)]
    if not offered or n <= 0:
        return []
    first = next((k for k in (_kind_for(want, speech), DEFAULT_KIND) if k in offered), None)
    rest = [k for k in offered if k != first]
    chosen = [first] if first else []
    if rest and len(chosen) < n:
        chosen.append(rest[rng.randrange(len(rest))])
    return [_one(speech.samples[k]) for k in chosen[:n]]


def word_range(shape: str) -> tuple[int, int] | None:
    """The line length a sentence shape names ("three to seven words", "3-8 words"), as (low,
    high) within 1 to 60; None when it names none."""
    m = _RANGE.search(shape or "")
    if not m:
        return None
    lo, hi = (int(x) if x.isdigit() else _NUMBER[x.lower()] for x in m.groups())
    lo, hi = max(1, min(lo, hi)), min(60, max(lo, hi))
    return (lo, hi) if lo <= hi else None


def strings(speech: Speech):
    """(path, text) for every string of the sheet."""
    yield "speech.register", speech.register
    yield "speech.shape", speech.shape
    for name in ("says", "never", "brings_up"):
        for i, x in enumerate(getattr(speech, name)):
            yield f"speech.{name}[{i}]", x
    yield "speech.with_him", speech.with_him
    yield "speech.stance", speech.stance
    for kind, text in speech.samples.items():
        yield f"speech.samples.{kind}", text


def speech_errors(p: Persona) -> list[str]:
    """validate_persona's checks of a voice sheet, as "path: problem"."""
    s, errs = p.speech, []
    for path, text in strings(s):
        optional = path in ("speech.with_him", "speech.stance")
        if not text.strip() and not optional:
            errs.append(f"{path}: must not be empty")
        if "\n" in text or "\r" in text or "|" in text or not text.isprintable():
            errs.append(f"{path}: one line of plain text (no line breaks, no |)")
    for kind in s.samples:
        if kind not in SAMPLE_KINDS:
            errs.append(f"speech.samples.{kind}: a sample kind is one of "
                        + ", ".join(SAMPLE_KINDS))
    if "answer" not in s.samples:
        errs.append("speech.samples: needs an answer sample")
    if "flirt" in s.samples and "warm" in s.samples:
        errs.append("speech.samples: flirt or warm, not both")
    if "flirt" in s.samples and p.identity.player_gender == "male":
        errs.append("speech.samples.flirt: men stay friends; use warm")
    for kind, text in s.samples.items():
        if estimate_tokens(text) > SAMPLE_TOKENS:
            errs.append(f"speech.samples.{kind}: at most {SAMPLE_TOKENS} estimated tokens "
                        f"({SAMPLE_TOKENS * 4} characters)")
    said = {normalize_words(x) for x in s.says}
    for i, x in enumerate(s.never):
        if normalize_words(x) and normalize_words(x) in said:
            errs.append(f"speech.never[{i}]: is also among the words she says")
    if len({normalize_words(x) or x for x in s.says}) != len(s.says):
        errs.append("speech.says: the same word twice")
    size = block_tokens(s)
    if size > BLOCK_TOKENS:
        errs.append(f"speech: the block is {size} estimated tokens with four words and one "
                    f"topic; at most {BLOCK_TOKENS}: shorten the register, shape, with_him or "
                    "stance")
    return errs


def _has_run(words: list[str], entry: list[str]) -> bool:
    n = len(entry)
    return bool(n) and any(words[i:i + n] == entry for i in range(len(words) - n + 1))


def typed_by_her(p: Persona) -> set[str]:
    """The words her own typing can put into a line after the guards have read it: the short
    forms of her abbreviations that shorten (talk.style._shortening) and, from her typing
    texture (talk.style.texture_of, by her character name), the shorthand she uses beyond her
    own list and her habitual spellings. Lower case."""
    from leeroy_mind.talk.style import SHORTHAND, _shortening, texture_of
    texture = texture_of(p.typing, p.character_name)
    words = {v for v in _shortening(p.typing.abbreviations).values()}
    words |= {SHORTHAND[i][1] for i in texture.extra if 0 <= i < len(SHORTHAND)}
    words |= {habit[1] for habit in texture.habits}
    return {" ".join(w.casefold().split()) for w in words if w.strip()}


def screen(p: Persona, *, characters=()) -> list[str]:
    """Codes for what a voice import refuses beyond the schema and the persona screens (never
    the text): `assistant_text` (reads like an assistant), `later_era` (slang or a year after
    2009), `member_name` (a guild member's name in a sample line: a sample is shown on any
    turn, whoever it is to), `never_in_sample` (a never-word inside one of her own samples),
    `says_slop` (one of her words holds, or sits inside, an entry of the stock list's slop
    lines: the guards rewrite those on every turn and never count one as her own word; the
    test is the guards' own, guards.voice.hers), `never_in_typing` (a never-word her own
    abbreviations or typing texture produce after the guards have passed the line:
    typed_by_her), `shape_over_typing` (her sentence shape names more words than her typing's
    longest line, which the styler would cut)."""
    from leeroy_mind.guards import phrases, voice
    from leeroy_mind.nightly.lexicon import era_ok
    from .schema import speech_to_dict
    from .screens import has_word
    s, codes = p.speech, []
    if s is None:
        return codes
    texts = [t for _, t in strings(s)]
    if any(has_meta(t) for t in texts):
        codes.append("assistant_text")
    if any(not era_ok(t) or _LATER_YEAR.search(t) for t in texts):
        codes.append("later_era")
    names = [n for n in characters if n and n.casefold() != p.character_name.casefold()]
    if any(has_word(t, n) for t in s.samples.values() for n in names):
        codes.append("member_name")
    samples = [normalize_words(t).split() for t in s.samples.values()]
    if any(_has_run(words, normalize_words(n).split()) for n in s.never for words in samples):
        codes.append("never_in_sample")
    sheet, slop = voice.sheet_of({"speech": speech_to_dict(s)}), phrases.slop()
    said = [phrases._words(entry) for entry in s.says]
    for name, alts in phrases.stock():
        if name not in slop:
            continue
        # her word inside the entry (the guards' own test), or the entry inside her word
        if any(voice.hers(alt, sheet) or any(phrases._has((words,), ((anchored, alt),))
                                             for words in said) for anchored, alt in alts):
            codes.append("says_slop")
            break
    made = typed_by_her(p)
    if any(" ".join(n.casefold().split()) in made for n in s.never):
        codes.append("never_in_typing")
    named = word_range(s.shape)
    if named is not None and named[1] > max(p.typing.words_per_line):
        codes.append("shape_over_typing")
    return codes


__all__ = ["BLOCK_TOKENS", "MAX_SAMPLES", "RECIPE_TAG", "SAMPLES_HEAD", "SAMPLE_TOKENS", "TRIMS",
           "VOICE_HEAD", "WANT_SAMPLE", "block_tokens", "fitted", "pick_samples", "public_block", "screen",
           "stance_current", "typed_by_her",
           "speech_errors", "strings", "voice_block", "word_range"]
