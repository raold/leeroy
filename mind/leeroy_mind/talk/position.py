"""Her position inside one conversation ([talk] coherence; self-play audit, 2026-11-17: 23 of
100 whisper turns contradicted or ignored the conversation they were in, with a new want drawn
every turn whatever was open between them, three different tallies in three turns, and an
officer arguing against a rule his own card calls law).

Read from words alone, the same on every channel and for every member:

- `disagrees`: his line pushes back on what she said or doubts it (select.pushback_kind's cues
  and the plain ones: "you said", "which is it", "are you sure", "idk, it looked legit");
- `bad_news`: his line carries bad news of his own (a hospital, a death, a vet, a lost job, a
  rough day): she neither teases nor flirts on it;
- `Hold` and `held`: while his question or disagreement is open her want is the one she had
  (held for HOLD_S of quiet, at most HOLD_TURNS answers or HOLD_MAX_S from when it was first
  set, until he changes the subject or settles it with a "fair enough");
- `claims_so_far`: what she has said so far in this conversation, as a few short claims
  (numbers and statements about herself first); `relevant_claims`, the ones his line is about,
  within CLAIM_TOKENS; and `said_line`, the sentence that shows them to the beat with the rule
  that she does not contradict herself (the line's prompt has one "so far" block, and it is
  talk/sofar.py's);
- `opinion_for`, `strong` and `view_line`: the opinion on her card his line touches, and
  whether she holds it as a rule ("not optional", "never", "the law"), which she then does not
  argue against unless he, as guild leader, overrules it outright.

Pure: no clock, no I/O, no logging. Nothing here is ever logged; the texts go only into her own
prompt, which already holds the lines they come from."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from leeroy_mind.talk.select import _words as topic_words_of
from leeroy_mind.talk.select import pushback_kind
from leeroy_mind.text import estimate_tokens

HOLD_S = 600.0  # a held want lets go this long after her last answer on it
HOLD_MAX_S = 900.0  # ...and this long after it was first set, whatever came since
HOLD_TURNS = 6  # ...or once she has answered on it this many times
CLAIM_TOKENS = 50  # the claims shown to the beat, at most (estimated tokens)
CLAIM_WINDOW_S = 1800.0  # her lines this old are no longer "so far in this conversation"
MAX_CLAIMS = 3
CLAIM_WORDS = 12
SAID = ("So far in this conversation you've said: {claims}. Stay consistent with that: don't "
        "contradict yourself, and if you change your mind, say that you do.")
VIEW = ('You hold this as a rule: "{opinion}" You don\'t argue against it; if {human}, as '
        "guild leader, overrules it outright you can give way, and you say that you do.")

_DISAGREES = re.compile(
    r"\b(?:i don'?t think|not sure about that|are (?:you|u) sure|(?:you|u) sure\b|no way\b|"
    r"thought (?:you|u)\b|(?:you|u) said\b|(?:you|u) told me|but (?:you|u)\b|which is it|"
    r"that'?s not|wasn'?t it|isn'?t it|looked? (?:pretty |kinda |quite )?legit|i disagree|"
    r"(?:you|u) were the one|(?:you|u) wrote|nah\b|wait (?:what|you|u)\b|since when|"
    r"how come|make up your mind)", re.IGNORECASE)
_DOUBT_OPEN = re.compile(r"^\W*(?:idk|hmm+|eh|really)\b", re.IGNORECASE)
_BAD_NEWS = re.compile(
    r"\b(?:hospital|icu|emergency room|in the er|passed away|died|dying|funeral|cancer|"
    r"diagnos\w+|heart (?:attack|thing|problem)|stroke|surgery|at the vet|to the vet|"
    r"put (?:him|her|them|it) (?:down|to sleep)|laid off|got fired|lost my job|made redundant|"
    r"broke up|dumped me|divorce|car accident|miscarr\w+|"
    r"(?:rough|bad|terrible|awful|shit|shitty|horrible|worst) (?:day|night|week|news)|"
    r"not doing (?:well|good|great|so good)|worried sick|scared (?:for|about)|"
    r"my (?:dad|mom|mum|mother|father|brother|sister|wife|kid|son|daughter|dog|cat) "
    r"(?:is|was|got|has|had) (?:sick|ill|hurt|in|not)|want to think about something else)\b",
    re.IGNORECASE)
_ACK = frozenset("""ok okay k kk fair enough true right yeah yea yep yup lol lmao haha hah nice
cool thanks thx ty cheers got it gotcha np sure alright aight good point makes sense i see
oh ah fine then so""".split())
_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
# a number she could later contradict: digits beside a word ("7 wins", "at 8"), or a number
# word but never "one" ("that one", "no one": review of #370, 14% of claims ranked for it)
_NUMBER = re.compile(r"\b\d+(?:[.,:]\d+)?%?\s+[a-z]{2,}|\b[a-z]{2,}\s+\d+(?:[.,:]\d+)?\b"
                     r"|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                     r"twenty|thirty|forty|fifty|hundred|thousand|half|twice|double)\b",
                     re.IGNORECASE)
# what she says of herself that she could later contradict: where she is, what she has, did,
# will or never does ("im at work", "i have two cats", "i never pug"); not every "i" or "my"
_SELF = re.compile(r"\b(?:i'?m|i am|i was|i have|i'?ve|i had|i got|i will|i'?ll|i won'?t|"
                   r"i never|i always|i did|i didn'?t|i can'?t|i don'?t|i went|i work|"
                   r"i play|i live|i need|my \w+ (?:is|was|are|were|has|starts?|ends?))\b",
                   re.IGNORECASE)
_STRONG = re.compile(
    r"\b(?:never|always|not optional|isn'?t optional|non-?negotiable|the law|is law|no "
    r"exceptions?|period|must|hill to die on|refuse|won'?t|will not|hate|can'?t stand|"
    r"every time|no excuses?)\b", re.IGNORECASE)
OPINION_MIN = 4  # a shared word this long ties his line to an opinion


def disagrees(line: str) -> bool:
    """His line pushes back on her or doubts what she said (module docstring)."""
    text = " ".join((line or "").split())
    if not text:
        return False
    return (pushback_kind(text) is not None or bool(_DISAGREES.search(text))
            or bool(_DOUBT_OPEN.search(text)))


def bad_news(line: str) -> bool:
    """His line carries bad news of his own (module docstring). One reader: talk/hisline.py's
    (the DM memory work) once it is there, this module's own pattern until then."""
    try:
        from leeroy_mind.talk.hisline import bad_news as theirs
    except ImportError:
        return bool(_BAD_NEWS.search(" ".join((line or "").split())))
    return theirs(line or "")


def settled(line: str) -> bool:
    """His line closes the matter: a few words of acknowledgement and no question."""
    words = _WORD.findall((line or "").casefold())
    return bool(words) and len(words) <= 4 and "?" not in line and all(w in _ACK for w in words)


def words_of(*lines: str) -> frozenset[str]:
    """The content words of `lines` (select's stems, stop words left out)."""
    return frozenset(w for line in lines for w in topic_words_of(line or ""))


@dataclass(frozen=True)
class Hold:
    """Her want while his question or disagreement is open: the want's code, when she last
    answered on it, and the content words of the exchange so far."""
    want: str
    at: float
    words: frozenset[str]
    first: float = 0.0  # when the hold was first set (0: `at`)
    turns: int = 1  # her answers on it so far


def held(hold: Hold | None, his_line: str, now: float) -> bool:
    """`hold` still stands for an answer to `his_line` now: within HOLD_S of her last answer,
    HOLD_MAX_S and HOLD_TURNS answers of when it was first set, not settled by him, and not a
    new subject (its words are the first exchange's and do not grow) (his line has two content words or more and
    shares none with the exchange; a short "which is it", "?" or "brb" stays on it)."""
    if hold is None or not 0 <= now - hold.at <= HOLD_S or settled(his_line):
        return False
    if hold.turns >= HOLD_TURNS or now - (hold.first or hold.at) > HOLD_MAX_S:
        return False  # bounded from its first turn: it never rides a whole evening
    his = words_of(his_line)
    return len(his) < 2 or bool(his & hold.words)


def _sentences(line: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(line or "") if s.strip()]


def claims_so_far(lines: Sequence[str], limit: int = MAX_CLAIMS) -> list[str]:
    """What she has said so far, from her own `lines` in this conversation (oldest first): at
    most `limit` short claims in the order she made them, each a sentence of hers cut to
    CLAIM_WORDS words. Only a sentence with a number in it or one that says something of
    herself ("im at work", "i have", "i never", "my shift starts") is a claim, numbers first,
    the newest winning a tie; questions, banter and sentences under three words are none. Any "so far in this conversation" block can read the same helper."""
    found: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for sentence in (s for line in lines for s in _sentences(line)):
        words = sentence.split()
        key = " ".join(_WORD.findall(sentence.casefold()))
        if len(words) < 3 or sentence.rstrip().endswith("?") or key in seen:
            continue
        seen.add(key)
        score = 2 if _NUMBER.search(sentence) else 1 if _SELF.search(sentence) else 0
        if not score:
            continue  # banter: nothing she could later contradict
        text = " ".join(words[:CLAIM_WORDS]).rstrip(".,;:!")
        found.append((score, len(found), text))
    best = sorted(found, key=lambda c: (c[0], c[1]), reverse=True)[:max(0, limit)]
    return [text for _, _, text in sorted(best, key=lambda c: c[1])]


def relevant_claims(lines: Sequence[str], his_line: str, *,
                    tokens: int = CLAIM_TOKENS) -> list[str]:
    """claims_so_far for a line of his: only the claims it is about (one shares a content word
    with it; a short follow-up such as "which is it", "?" or "you sure" is about her newest
    claim), the newest kept within `tokens` estimated tokens. [] when she has claimed nothing
    his line touches."""
    claims = claims_so_far(lines, limit=len(lines) * 4 + 4)
    his = words_of(his_line)
    picked = [c for c in claims if his & words_of(c)]
    if not picked and len(his) < 2 and (disagrees(his_line) or "?" in (his_line or "")):
        picked = claims[-1:]
    out: list[str] = []
    for claim in reversed(picked[-MAX_CLAIMS:]):
        if estimate_tokens(said_line([claim, *out])) > tokens:
            break
        out.insert(0, claim)
    return out


def said_line(claims: Sequence[str]) -> str:
    """The sentence that shows her claims ("" without any)."""
    if not claims:
        return ""
    return SAID.format(claims="; ".join(f'"{c}"' for c in claims))


def opinion_for(opinions: Sequence[str], line: str) -> str | None:
    """The opinion of hers his line touches: the one sharing the most content words with it
    (at least one of OPINION_MIN letters or more), the first on a tie; None when none does."""
    his = {w for w in words_of(line) if len(w) >= OPINION_MIN}
    if not his:
        return None
    best, most = None, 0
    for opinion in opinions:
        shared = len(his & words_of(opinion))
        if shared > most:
            best, most = opinion, shared
    return best


def strong(opinion: str) -> bool:
    """She holds it as a rule: its own words say never, always, not optional, the law..."""
    return bool(_STRONG.search(opinion or ""))


def view_line(opinion: str, human: str = "he") -> str:
    return VIEW.format(opinion=" ".join(opinion.split()), human=human)


__all__ = ["CLAIM_TOKENS", "CLAIM_WINDOW_S", "HOLD_MAX_S", "HOLD_S", "HOLD_TURNS", "Hold",
           "bad_news", "claims_so_far", "disagrees", "held", "opinion_for", "relevant_claims",
           "said_line", "settled", "strong", "view_line", "words_of"]
