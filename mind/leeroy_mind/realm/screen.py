"""The realm's screen (I15, the content invariants): the last check on a rival officer's or a
member's line before it goes to a public channel. A line that fails is dropped, never repaired
here, and the reason is all the caller may log.

It fails closed: `ready` is false, and every line is "not_ready", while the local blocklist
(`<data dir>/realm/blocklist.txt`, one word or phrase per line, never committed) is missing or
holds nothing, or while the guild's name is unknown.

The reasons, checked in this order:
- blocklist: a word or phrase of the blocklist, case-folded, as whole words (a plural or
  possessive "s" included);
- group_insult: a group of people (GROUP_NOUNS, neutral nouns only) within 4 words of an insult
  (INSULTS); mocking play ("typical huntard scrubs") is fine;
- guild_name: the guild's own name, anywhere, as whole words;
- leaves: someone said to leave the guild (P1c's LEAVES_RE);
- flirt: P1b's flirty (a wink, kiss or heart, a romance word or a flirt phrase) or its desire
  and pet-name patterns: rivals never flirt, and nobody flirts in a public channel;
- real_name: the owner's real name, by P1b's names_real_name;
- tells: what P1b's output filters find (find_tells) and silence (is_silence). The pipeline
  repairs the repairable tells (filters.repair) before it accepts a line, so a caller repairs
  first; a line that still needs a repair is dropped here;
- too_long: over MAX_CHARS characters."""
from bisect import bisect_left, bisect_right
from collections.abc import Callable
import logging
import os
from pathlib import Path
import re
import unicodedata

from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly.ground import LEAVES_RE
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.talk.filters import _DESIRE, _PET, _SUBJECT_DESIRE, find_tells, flirty, is_silence

log = logging.getLogger(__name__)

BLOCKLIST = Path("realm") / "blocklist.txt"
MAX_CHARS = 120
GROUP_WINDOW = 4  # words between a group noun and an insult, at most
SPEAKER_KINDS = frozenset({"officer", "member"})
REASONS = ("not_ready", "blocklist", "group_insult", "guild_name", "leaves", "flirt", "real_name",
           "tells", "too_long")

# Groups of people, as the neutral nouns and adjectives a line would use for them; a plural or
# possessive "s" is matched too. Words that are also ordinary game words are left out ("blind",
# "cripple", "man", "straight", "pole", "lady": Lady Deathwhisper).
GROUP_NOUNS = frozenset({
    # religions and beliefs
    "christian", "catholic", "protestant", "lutheran", "baptist", "methodist", "evangelical",
    "mormon", "orthodox", "jew", "jewish", "judaism", "muslim", "islam", "islamic", "moslem",
    "hindu", "buddhist", "sikh", "jain", "atheist", "agnostic", "amish", "quaker", "rastafarian",
    # ethnicities and peoples
    "asian", "african", "arab", "latino", "latina", "latinx", "hispanic", "black people",
    "white people", "brown people", "native american", "aboriginal", "indigenous", "gypsy",
    "gypsies", "roma", "romani", "slav", "slavic", "caucasian",
    # nationalities
    "american", "canadian", "mexican", "brazilian", "argentinian", "chilean", "colombian",
    "british", "brit", "english", "scottish", "scot", "welsh", "irish", "french", "frenchman",
    "frenchmen", "german", "dutch", "belgian", "swiss", "austrian", "italian", "spanish",
    "spaniard", "portuguese", "greek", "turk", "turkish", "polish", "czech", "hungarian",
    "romanian", "bulgarian", "serbian", "croatian", "ukrainian", "russian", "swede", "swedish",
    "norwegian", "dane", "danish", "finn", "finnish", "icelandic", "chinese", "japanese",
    "korean", "vietnamese", "thai", "filipino", "indonesian", "malaysian", "indian", "pakistani",
    "bangladeshi", "iranian", "iraqi", "israeli", "palestinian", "egyptian", "nigerian",
    "kenyan", "ethiopian", "australian", "aussie", "kiwi", "european",
    # genders
    "woman", "women", "girl", "girls", "female", "male", "men", "trans",
    "transgender", "nonbinary", "non-binary",
    # orientations
    "gay", "lesbian", "bisexual", "homosexual", "queer", "lgbt", "lgbtq",
    # disabilities
    "disabled", "handicapped", "autistic", "autism", "deaf", "dyslexic", "down syndrome",
    "special needs", "mentally ill", "wheelchair",
})
INSULTS = frozenset({
    "scrub", "noob", "newb", "n00b", "nub", "bad", "baddie", "trash", "garbage", "terrible",
    "awful", "worst", "suck", "sucks", "lame", "loser", "idiot", "moron", "stupid", "dumb",
    "worthless", "pathetic", "useless", "clown", "filth", "filthy", "dirty", "subhuman", "animal",
    "vermin", "rat", "scum", "disgusting", "gross", "hate", "inferior", "lazy", "smelly", "ugly",
    "weak", "weakling", "failure", "typical", "ruin", "ruins", "ruined", "plague", "parasite",
    "cancer", "freak",
})
_WORD = re.compile(r"[\w'-]+")


def _fold(text: str) -> str:
    """Case-folded, without accents, with "’" as "'"."""
    decomposed = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _phrases(words, *, plural: bool) -> re.Pattern | None:
    """Whole-word alternation of folded phrases, spaces as any whitespace."""
    forms = sorted({_fold(" ".join(w.split())) for w in words if w.strip()}, key=len, reverse=True)
    if not forms:
        return None
    alts = "|".join(r"\s+".join(map(re.escape, f.split(" "))) for f in forms)
    tail = r"(?:e?s)?(?:'s|s')?" if plural else r"(?:'s|s')?"
    return re.compile(rf"(?<![\w'])(?:{alts}){tail}(?![\w])")


_GROUP_RE = _phrases(GROUP_NOUNS, plural=True)
_INSULT_RE = _phrases(INSULTS, plural=True)


def _token_spans(text: str, pattern: re.Pattern) -> list[tuple[int, int]]:
    """For each match, the indexes of its first and last word among the text's words."""
    starts = [m.start() for m in _WORD.finditer(text)]
    spans = []
    for m in pattern.finditer(text):
        first = max(0, bisect_right(starts, m.start()) - 1)
        spans.append((first, max(first, bisect_left(starts, m.end()) - 1)))
    return spans


def insults_a_group(text: str) -> bool:
    """A group noun within GROUP_WINDOW words of an insult, either side."""
    folded = _fold(text)
    groups = _token_spans(folded, _GROUP_RE)
    insults = _token_spans(folded, _INSULT_RE) if groups else []
    return any((c - b if c > b else a - d if a > d else 0) <= GROUP_WINDOW
               for a, b in groups for c, d in insults)


def flirts(text: str) -> bool:
    return bool(flirty(text) or _DESIRE.search(text) or _SUBJECT_DESIRE.search(text)
                or _PET.search(text))


class Screen:
    def __init__(self, data_dir: Path, guild_name: Callable[[], str],
                 human: Callable[[], HumanIdentity]):
        self.path = Path(data_dir) / BLOCKLIST
        self._guild_name, self._human = guild_name, human
        self._loaded: tuple[tuple[int, int], re.Pattern | None] | None = None

    def _blocklist(self) -> re.Pattern | None:
        """The blocklist as one pattern, re-read when the file changes; None while it is
        missing, unreadable or empty."""
        try:
            st = os.stat(self.path)
        except OSError:
            self._loaded = None
            return None
        key = (st.st_mtime_ns, st.st_size)
        if self._loaded is None or self._loaded[0] != key:
            try:
                lines = self.path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeError):
                log.warning("realm_screen_blocklist_unreadable")
                self._loaded = None
                return None
            words = [s for line in lines if (s := line.strip()) and not s.startswith("#")]
            self._loaded = (key, _phrases(words, plural=True))
        return self._loaded[1]

    def _guild(self) -> re.Pattern | None:
        try:
            name = self._guild_name()
        except Exception:
            log.warning("realm_screen_guild_name_unavailable")
            return None
        if type(name) is not str or not name.strip():
            return None
        return _phrases([name], plural=False)

    @property
    def ready(self) -> bool:
        return self._blocklist() is not None and self._guild() is not None

    def check(self, text: str, *, speaker_kind: str) -> str | None:
        """The reason the line is dropped (REASONS), or None when it may be sent."""
        if speaker_kind not in SPEAKER_KINDS:
            raise ValueError("unknown realm speaker kind")
        blocked, guild = self._blocklist(), self._guild()
        if blocked is None or guild is None:
            return "not_ready"
        folded = _fold(text)
        if blocked.search(folded):
            return "blocklist"
        if insults_a_group(text):
            return "group_insult"
        if guild.search(folded):
            return "guild_name"
        if LEAVES_RE.search(text):
            return "leaves"
        if flirts(text):
            return "flirt"
        if names_real_name(text, self._human()):
            return "real_name"
        if is_silence(text) or find_tells(text):
            return "tells"
        if len(text) > MAX_CHARS:
            return "too_long"
        return None
