import asyncio
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Callable

from leeroy_mind.clock import night_of
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.client import LLMClient, LLMError
from leeroy_mind.llm.presets import EXTRACT, JSONJobFailed, chat_json
from leeroy_mind.llm.scheduler import LLMScheduler, Priority, SchedulerError
from leeroy_mind.logs import log as log_event
from leeroy_mind.memory.recall import chat_ids, fts_query
from leeroy_mind.memory.threads import ClosedThread, worth_extracting
from leeroy_mind.memory.writer import MAX_TEXT, MemoryCandidate, MemoryWriter
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.screens import (
    ROMANCE_RE, check_romance, human_aliases, names_human, names_real_name, redact_real_name)
from leeroy_mind.storage.rows import REL_DIMS, ChatLineRow
from leeroy_mind.storage.store import Store
from leeroy_mind.text import (estimate_tokens, normalize_words, strip_wow_markup, truncate_words,
                              word_jaccard)

log = logging.getLogger("leeroy_mind")

PER_SPEAKER = 2
MAX_BYSTANDERS = 10
OVERHEARD_MIN = 5
_EVIDENCE = re.compile(r"\s*\[?\s*L(\d+)\s*\]?\s*", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine|myself|we|us|our|ours)\b", re.IGNORECASE)
# Small talk leaves no memory (prompts I11): a line of only these words, names aside.
SMALL_TALK = frozenset("""hi hey hello heya yo sup evening morning afternoon gz grats gratz
congrats gg gn night nite goodnight bye cya later laters lol lmao haha hehe rofl ok okay kk k np
brb wb back all everyone guys yall ya o7 sleep well boss gm""".split())
SMALL_TALK_WORDS = 6
_TALK_WORD = re.compile(r"[a-z0-9']+")

# Extraction records what happened and never canonises a claim (fix wave G2; dress rehearsal 2
# kept members' small talk, their own lines word for word and a member's mistake as the guild's
# truth, all at confidence 1.0, and they came back in prompts the same night).
WHAT = ("guild_master", "plan", "promise", "event", "claim")
KEEP_FROM = 4  # importance: a memory under it, with him not in it, is small talk between members
EXTRACT_CONFIDENCE = 0.9  # what happened, as the model read it from the chat: never certain
CLAIM_CONFIDENCE = 0.6  # what someone said about how things are: hearsay (recall: "(heard)")
COPY_RUN = 6  # this many of a line's words in a row make a memory a copy of it
COPY_JACCARD = 0.8  # and so do this many of the same words
KNOWN_MEMBERS = 8  # members whose cards and memories show under "What the guild already knows"
KNOWN_MEMORIES = 8
KNOWN_PER_MEMBER = 2
KNOWN_TERMS = 16  # words of the thread its memory search looks for
KNOWN_TOKENS = 350  # the whole block's budget, so a long thread's rules stay in the prompt
_REPORT = re.compile(
    r"(?:\s+[\w']+){0,2}?\s+(?:said|says|saying|told|tells|telling|asked|asks|asking|claims?"
    r"|claimed|claiming|thinks|thinking|thought|reckons?|reckoned|swears?|swore|insists?"
    r"|insisted|joked|jokes|joking|bragged|brags|boasted|boasts|mentioned|mentions"
    r"|announced|announces|promised|offered|wished|wishes|called|calls)(?!\w)", re.IGNORECASE)
_ACCORDING = re.compile(r"(?<!\w)according\s+to\s*$", re.IGNORECASE)
# A first-person frame that makes a memory a record of what the speaker said ("I told Testy
# ...", "we agreed ..."), not a bare statement of how things are (review G2, Important 2).
_FRAME = re.compile(
    r"(?<!\w)(?:I|we)(?:\s+[\w']+)?\s+(?:told|said|asked|answered|replied|promised|offered"
    r"|agreed|wished|joked|teased|thanked|invited|warned|admitted|explained|mentioned|suggested"
    r"|bet|swore|reminded|congratulated|apologi[sz]ed|whispered|called|greeted|welcomed"
    r"|complimented|flirted|challenged|dared|begged|volunteered|planned)(?!\w)", re.IGNORECASE)
# A wish, or a claim that a birthday is now (not talk about birthdays).
_BIRTHDAY_NOW = re.compile(
    r"(?<!\w)happy\s+(?:b-?day|birthday)(?!\w)"
    r"|(?<!\w)(?:b-?day|birthday)\s+(?:wish(?:es)?|cake|party|hugs?|today|tonight)(?!\w)"
    r"|(?<!\w)(?:it'?s|it\s+is)\s+(?:[\w']+\s+){0,2}(?:b-?day|birthday)(?!\w)"
    r"|(?<!\w)wish(?:ed|es|ing)?\s+(?:[\w']+\s+){1,3}(?:good|great|lovely)\s+(?:b-?day|birthday)",
    re.IGNORECASE)
_WHOSE = re.compile(r"(?<!\w)([\w]+)['\u2019]s\s+$")  # "Ebbo's " right before "birthday"
_POSSESSIVE = re.compile(r"['’]s(?!\w)")
_BIRTHDAY = re.compile(r"(?<!\w)(?:birthday|b-?day)s?(?!\w)", re.IGNORECASE)
_MY = re.compile(r"(?<!\w)my\s+$", re.IGNORECASE)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
_MONTH = re.compile(r"(?<!\w)(" + "|".join(MONTHS) + r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct"
                    r"|Nov|Dec)\.?(?!\w)")

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "speaker": {"type": "string"},
                "text": {"type": "string", "maxLength": MAX_TEXT},
                "what": {"type": "string", "enum": list(WHAT)},
                "disagrees": {"type": "boolean"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                "valence": {"type": "number", "minimum": -1, "maximum": 1},
                "about": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "nudges": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "dim": {"type": "string", "enum": list(REL_DIMS)},
                        "delta": {"type": "integer", "minimum": -3, "maximum": 3}},
                    "required": ["to", "dim", "delta"]}}},
            "required": ["speaker", "text", "what", "disagrees", "importance", "valence",
                         "about", "evidence"]}},
        "overheard": {"anyOf": [
            {"type": "null"},
            {"type": "object",
             "properties": {"text": {"type": "string", "maxLength": MAX_TEXT},
                            "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                            "disagrees": {"type": "boolean"}},
             "required": ["text", "importance", "disagrees"]}]},
    },
    "required": ["memories", "overheard"],
}


@dataclass(frozen=True)
class Nudge:
    a: int
    b: int
    dim: str
    delta: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Extraction:
    memories: list[MemoryCandidate]
    nudges: list[Nudge]
    overheard: tuple[str, int] | None
    # what the model labelled, what was kept and what each rule dropped: counts only, never text
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractReport:
    written: int
    merged: int
    nudges: int
    status: str


@dataclass(frozen=True)
class ThreadFacts:
    """What the cards say that a memory is checked against: each member's birthday (MM-DD, by
    guid), and the clock's zone, which decides the night a line was said on."""
    birthdays: Mapping[int, str]
    tz: tzinfo


def _line_text(ln: ChatLineRow, human: HumanIdentity) -> str:
    text = " ".join(strip_wow_markup(ln.text).split())
    return redact_real_name(text, human, human.called)


def _said_text(ln: ChatLineRow, said: Mapping[int, str] | None,
               human: HumanIdentity) -> tuple[str, ...]:
    """The model's own words for a line the mind said, as _line_text gives a line: () when
    `said` has none for it."""
    words = (said or {}).get(ln.id)
    if not words:
        return ()
    return (redact_real_name(" ".join(strip_wow_markup(words).split()), human, human.called),)


def build_extraction_messages(lines: list[ChatLineRow], names: dict[int, str], human_guid: int,
                              *, human: HumanIdentity, known: Iterable[str] = ()) -> list[dict]:
    """`names` maps guid to the name the model sees; the human's entry must be his nickname.
    The owner's real name never enters the prompt: it is redacted from the lines. `known` is
    what the guild already knows (Extractor._known), shown before the conversation."""
    body = "\n".join(
        f"[L{ln.id}] {names.get(ln.from_guid, ln.from_name)}: {_line_text(ln, human)}"
        for ln in lines)
    who = (f"The guild master is {human.describe(include_real_name=False)}. Always call "
           f"{human.forms[1]} {human.called}.\n")
    system = (
        "You read one short conversation in a World of Warcraft guild chat and note what the "
        "members would remember from it.\n" + who +
        "What the guild already knows, when there is any, comes before the conversation.\n"
        "Rules:\n"
        "- Write each memory in the first person, as the speaker it belongs to, in at most "
        f"{MAX_TEXT} characters, in your own words: never copy a line.\n"
        f"- At most {PER_SPEAKER} memories per speaker. Only speakers named in the lines may "
        "have memories; the guild master gets none.\n"
        "- Keep only what happened, and say which in what: guild_master for something the guild "
        "master said or asked, plan or promise for one somebody made, event for something that "
        "happened or was shared tonight.\n"
        "- What someone said about the past, about another member or about how things stand in "
        "the guild is a claim: note it only in another speaker's memory and as theirs (\"Arkon "
        "told me he soloed the dragon\"), never as a fact.\n"
        "- disagrees is true for a memory that goes against what the guild already knows; such "
        "a memory is not kept.\n"
        "- Note only what was actually said. Invent nothing: no facts, no feelings, no plans, "
        "and no \"always\" or \"keeps\" that the lines do not show.\n"
        "- No romance between members. Flirting is only ever aimed at the guild master.\n"
        "- Cite the lines a memory rests on in evidence, like L12.\n"
        "- Most short exchanges leave nothing to remember: greetings, gz, thanks, goodnights, "
        "laughs, banter and one-word replies get no memory, and an empty memories list is a "
        "good answer.\n"
        "- importance: 3 or less is small talk and banter, which is not kept, 5 is personal "
        "news, a plan or a promise, 8 is something they will bring up for weeks, 10 is "
        "life-changing.\n"
        "- valence runs from -1 (bad) to 1 (good).\n"
        "- about lists whom the speaker talked with: who the lines were said to, or who said "
        "them to the speaker; not whom the talk was about.\n"
        "- nudges (optional) say how the memory shifts the speaker's view of someone: to, a "
        f"dimension ({', '.join(REL_DIMS)}) and a delta from -3 to 3.\n"
        "- overheard is one first-person sentence for a guildmate who only saw the conversation "
        "(\"I saw Arkon and Belra arguing about loot.\"), or null when nothing in it is worth "
        "passing on; its disagrees works as a memory's.")
    known = [k for k in known if k]
    head = ("What the guild already knows:\n" + "\n".join(f"- {k}" for k in known) + "\n\n"
            if known else "")
    return [{"role": "system", "content": system},
            {"role": "user", "content": head + "Conversation:\n" + body}]


def _names_re(names: Iterable[str]) -> re.Pattern | None:
    """Whole-word names in any case: chat is typed in lower case too."""
    alts = sorted({n for n in names if n}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, alts)) + r")(?!\w)",
                      re.IGNORECASE)


class _People:
    """The cast's names as chat has them and the human's, to tell whom a line was said to and
    who said a claim."""

    def __init__(self, names: Mapping[int, str], human_guid: int, human: HumanIdentity):
        self.human, self.human_guid = human, human_guid
        self.names = names
        self._members = {n.casefold(): g for g, n in names.items() if g != human_guid and n}
        self._members_re = _names_re(self._members)
        his = {human.nickname, human.character_name, names.get(human_guid, "")} - {""}
        self._everyone = {**self._members, **{n.casefold(): human_guid for n in his}}
        self._everyone_re = _names_re(self._everyone)
        him = "|".join(map(re.escape, sorted(his, key=len, reverse=True)))
        # an "always" of his: "Testy keeps asking", "Testy always", "every time Testy"
        self._often = re.compile(
            rf"(?<!\w)(?:{him})(?!\w)\s+(?:[\w']+\s+)?(?:keeps|kept|always|constantly)(?!\w)"
            rf"|(?<!\w)(?:every\s+time|all\s+the\s+time)(?!\w)[^.!?;]*(?<!\w)(?:{him})(?!\w)"
            rf"|(?<!\w)(?:{him})(?!\w)[^.!?;]*(?<!\w)(?:every\s+time|all\s+the\s+time)(?!\w)",
            re.IGNORECASE) if him else None

    def often(self, text: str) -> bool:
        """The text says he does something again and again."""
        return self._often is not None and self._often.search(text) is not None

    def member(self, name: str) -> int | None:
        return self._members.get(name.casefold())

    def is_him(self, name: str) -> bool:
        return self._everyone.get(name.casefold()) == self.human_guid

    def named(self, text: str, *, possessive: bool = True) -> list[int]:
        """The members a text names, in order; without `possessive`, "ebbo's" (talk about
        him, not to him) does not count."""
        out: list[int] = []
        for m in self._members_re.finditer(text) if self._members_re else ():
            if not possessive and _POSSESSIVE.match(text, m.end()):
                continue
            g = self._members[m.group(0).casefold()]
            if g not in out:
                out.append(g)
        return out

    def names_him(self, text: str) -> bool:
        return names_human(text, self.human, any_case=True)

    def attributed(self, text: str, speaker: int) -> bool:
        """The text says whose words they are, and they are not the speaker's: "Arkon said",
        "Testy keeps asking", "according to Belra"."""
        for m in self._everyone_re.finditer(text) if self._everyone_re else ():
            if self._everyone[m.group(0).casefold()] == speaker:
                continue
            if _REPORT.match(text, m.end()) or _ACCORDING.search(text, 0, m.start()):
                return True
        return False



def _with(speaker: int, cited: list[ChatLineRow], lines: list[ChatLineRow], text: str,
          people: _People) -> tuple[int, ...]:
    """Whom a memory is with, the speaker first: who said the lines it rests on, the other
    side of a whisper, whom the speaker's own lines name (a possessive "ebbo's" is talk about
    him, not to him) or answer (the last line before hers that named her), and the human when
    the memory names him and he spoke in the thread. Never whom the talk was about: the
    model's `about` followed the topic, so a flirt at him became a memory with the member whose
    birthday it was (dress rehearsal 2)."""
    out = [speaker]

    def add(g: int | None) -> None:
        if g is not None and g in people.names and g not in out:
            out.append(g)
    for ln in cited:
        if ln.from_guid != speaker:
            add(ln.from_guid)
            continue
        if ln.channel == "whisper":
            add(ln.to_guid)
        if people.names_him(ln.text):
            add(people.human_guid)
        for g in people.named(ln.text, possessive=False):
            add(g)
        earlier = [x for x in lines if x.id < ln.id and x.from_guid != speaker]
        for x in reversed(earlier):
            if speaker in people.named(x.text):
                add(x.from_guid)
                break
    if names_human(text, people.human) and any(ln.from_guid == people.human_guid
                                                for ln in lines):
        add(people.human_guid)
    return tuple(out)


def _words(text: str) -> list[str]:
    return normalize_words(strip_wow_markup(text)).split()


def _longest_run(a: list[str], b: list[str]) -> int:
    best, prev = 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def copies(text: str, line: str) -> bool:
    """The memory says the line back: most of the same words (COPY_JACCARD), COPY_RUN of the
    line's words in a row outside an "I told / said / promised ..." frame (review G2, Minor 1:
    "I promised Testy I'd run him through ..." is her promise, not a copy), or all of a shorter
    line's (from three words up) when that is most of the memory; his question with her "sure,
    what time?" in it is no copy of hers."""
    b = _words(line)
    if len(b) < 3:
        return False
    a = _words(text)
    run = _longest_run(a, b)
    return (word_jaccard(text, strip_wow_markup(line)) >= COPY_JACCARD
            or (run >= COPY_RUN and not _FRAME.search(text))
            or (run == len(b) and len(a) <= 2 * len(b)))


def member_flirt(text: str, members: list[str]) -> bool:
    """A flirt at one of `members` that the romance screen's words miss: the seam for G1's
    member-flirt rule in talk/, which the controller points this at once both waves are merged.
    Until then it flags nothing."""
    return False


def _night(t: float, tz: tzinfo) -> set[str]:
    """MM-DD of the day and of the night (06:00 to 06:00) a time falls on."""
    at = datetime.fromtimestamp(t, tz)
    return {at.strftime("%m-%d"), night_of(at)[5:]}


def _wrong_birthday(text: str, speaker: int | None, t_event: float, facts: ThreadFacts,
                    people: _People, actors: Iterable[int] = ()) -> bool:
    """A birthday wished, said to be now, or put in a named month, that nobody it can be has
    on the cards (dress rehearsal 2: a birthday nobody had, kept as four members' memories).
    Its person is the speaker for "my birthday" and the member before "'s birthday"; otherwise
    anyone it can be: the speaker (none for an overheard line), every member the memory names
    and everyone it is with (`actors`, so "I wished him ..." can mean the member who said it
    was his), and it is wrong only when none of them has it, tonight or in the month named
    (review G2: on her birthday "Belra wished me a happy birthday" was read as Belra's, and
    many cards share a date). Talk about birthdays is not checked, nor anyone without a card
    birthday, him included."""
    months = {MONTHS.index(m) + 1 for m in
              (next(x for x in MONTHS if x.startswith(g[:3])) for g in _MONTH.findall(text))}
    if not months and not _BIRTHDAY_NOW.search(text):
        return False
    tonight = _night(t_event, facts.tz)
    for b in _BIRTHDAY.finditer(text):
        before = text[:b.start()]
        whose = _WHOSE.search(before)
        if _MY.search(before):
            who = [speaker]
        elif whose and people.is_him(whose.group(1)):
            continue
        elif whose and people.member(whose.group(1)) is not None:
            who = [people.member(whose.group(1))]
        elif names_human(text, people.human):
            continue  # it may be his
        else:
            who = list(dict.fromkeys(([speaker] if speaker is not None else [])
                                     + people.named(text) + list(actors)))
        cards = [facts.birthdays.get(g) if g is not None else None for g in who]
        if not cards or None in cards:
            continue  # someone it can be has no card birthday: nothing to check
        if months:
            if not any(int(c[:2]) in months for c in cards):
                return True
        elif not any(c in tonight for c in cards):
            return True
    return False


def small_talk(text: str, names: set[str]) -> bool:
    """A line that is only a greeting, a gz, a goodnight, a laugh or an ack (SMALL_TALK), with
    people's names (`names`, casefolded) left aside: "hey testy!", "gz", "night, sleep well".
    Thanks are not here: who thanked whom for what can matter."""
    words = [w for w in _TALK_WORD.findall(strip_wow_markup(text).casefold()) if w not in names]
    return len(words) <= SMALL_TALK_WORDS and all(w in SMALL_TALK for w in words)


def _evidence_ids(raw, valid: set[int]) -> list[int]:
    out: list[int] = []
    for e in raw if isinstance(raw, list) else []:
        m = _EVIDENCE.fullmatch(e) if isinstance(e, str) else None
        if m and int(m.group(1)) in valid and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def _int(v, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return max(lo, min(hi, int(round(v))))


def _took_part(cited: list[ChatLineRow], human_guid: int) -> bool:
    """He took part in what a memory rests on: a line of his, or a whisper between him and a
    member, is among its cited lines. Being named in a member's own line is not taking part."""
    return any(ln.from_guid == human_guid
               or (ln.channel == "whisper" and human_guid in (ln.from_guid, ln.to_guid))
               for ln in cited)


def validate_extraction(data, lines: list[ChatLineRow], names: dict[int, str], human_guid: int,
                        *, human: HumanIdentity, facts: ThreadFacts | None = None,
                        said: Mapping[int, str] | None = None) -> Extraction:
    """Keep only what the lines support. `names` maps every resolvable guild member (every
    persona, in the thread or not, plus the human) to the name the model sees. Raises
    ValueError on a reply with no `memories` list (so chat_json asks again); drops every
    invented or forbidden entry otherwise. Text that names the owner's real name is dropped,
    and so is a memory the model rates 1 (its small-talk anchor) or one resting only on small
    talk (small_talk). `said` holds the talk engine's words before styling for the lines the
    mind said (line id -> words), so their typos never hide a copy.

    Then extraction records and never canonises (fix wave G2):
    - whom a memory is with comes from the lines (_with), never from the model's `about`;
    - under KEEP_FROM on the model's own rating it is small talk, and goes, even when it names
      him (fix wave G3, rehearsal 3 check 8: a member's banter naming him was kept because it
      named him). What he took part in (_took_part: a line of his, a whisper between them) is
      worth 5 first, so a promise he asked for stays when the model rated it 3; any memory
      kept with him in it is worth 5;
    - a memory that says back one of the speaker's own lines (copies), as sent or in the
      model's words before styling (`said`), goes;
    - one the model says disagrees with what the guild already knows goes, and so does a
      birthday the cards disagree with (`facts`; None checks none);
    - an "always" or "keeps" of his with one line of his behind it goes;
    - a claim (`what`; anything unlabelled is one, and "guild_master" without a line of his)
      stays only as someone else's words (_People.attributed), at CLAIM_CONFIDENCE, and so does
      a memory resting only on the speaker's own lines with no "I told / said ..." frame,
      whatever its label; the rest is stored at EXTRACT_CONFIDENCE, never at full confidence;
    - romance at a member goes: the romance screen with the cast's names in any case, its
      pronoun rule over the model's `about` and the people it is with, and member_flirt.
    Only the hard drops (no such speaker, the real name, no evidence, romance) lose the
    memory's nudges. The overheard line goes through the romance screen, `disagrees` and the
    birthday check. `counts` says what was labelled, kept and dropped, by rule."""
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        raise ValueError("the reply needs a 'memories' list")
    by_name = {n.casefold(): g for g, n in names.items()}
    by_name.update({a: human_guid for a in human_aliases(human) if a not in by_name})
    by_id = {ln.id: ln for ln in lines}
    speakers_ok = {ln.from_guid for ln in lines} | {
        ln.to_guid for ln in lines if ln.channel == "whisper"}
    valid = set(by_id)
    member_names = [n for g, n in names.items() if g != human_guid]
    known = {n.casefold() for n in names.values()} | set(human_aliases(human))
    people = _People(names, human_guid, human)

    def guid_of(name) -> int | None:
        return by_name.get(name.strip().casefold()) if isinstance(name, str) else None

    def clean_text(raw) -> str:
        """Empty when the text uses the owner's real name in a form redaction takes (its
        configured, Title or UPPER form: screens.names_real_name). Such a text goes whole,
        never rewritten; the ordinary lower-case word the name may also be is kept."""
        if not isinstance(raw, str):
            return ""
        text = " ".join(strip_wow_markup(raw).split())
        if names_real_name(text, human):
            return ""
        return truncate_words(text, MAX_TEXT)

    def romantic(text: str) -> bool:
        """P1b's romance screen with the cast's names in any case, as typed chat has them
        (P1c ruling PR6)."""
        try:
            check_romance({"sample_lines": [text]}, human, (), characters=member_names)
        except ValueError:
            return True
        return False

    memories: list[MemoryCandidate] = []
    combined: dict[tuple[int, int, str], Nudge] = {}
    per_speaker: dict[int, int] = {}
    counts: Counter[str] = Counter()

    def drop(reason: str) -> None:
        counts[f"dropped:{reason}"] += 1
    for m in data["memories"]:
        if not isinstance(m, dict):
            continue
        counts[f"what:{m.get('what') if m.get('what') in WHAT else 'unlabelled'}"] += 1
        counts["disagrees"] += m.get("disagrees") is True
        # The hard drops take the memory's nudges along: nobody to give them to, the real name,
        # romance at a member, or nothing to cite.
        speaker = guid_of(m.get("speaker"))
        if speaker is None or speaker == human_guid or speaker not in speakers_ok:
            drop("speaker")
            continue
        text = clean_text(m.get("text"))
        ev_ids = _evidence_ids(m.get("evidence"), valid)
        if not text:
            drop("text")
            continue
        if not ev_ids:
            drop("evidence")
            continue
        cited = [by_id[i] for i in ev_ids]
        actors = _with(speaker, cited, lines, text, people)
        about = [g for a in (m.get("about") if isinstance(m.get("about"), list) else [])
                 if (g := guid_of(a)) is not None]
        members_with = [names[g] for g in actors if g not in (speaker, human_guid)]
        # Pronoun-only romance ("a crush on her") aimed at a member: one the model named in
        # `about`, or one the memory is with.
        if (romantic(text) or (ROMANCE_RE.search(text) and not names_human(text, human)
                               and any(g not in (human_guid, speaker) for g in (*about, *actors)))
                or (members_with and member_flirt(text, members_with))):
            drop("romance")
            continue
        # Any other drop keeps the nudges (review G2: a copied flirt lost its attraction +1).
        evidence = tuple(f"chat:{i}" for i in ev_ids)
        for n in (m.get("nudges") if isinstance(m.get("nudges"), list) else []):
            if not isinstance(n, dict):
                continue
            to, dim, delta = guid_of(n.get("to")), n.get("dim"), _int(n.get("delta"), -3, 3)
            if (to is None or to == speaker or dim not in REL_DIMS or not delta
                    or (dim == "attraction" and to != human_guid)):
                continue
            key = (speaker, to, dim)
            old = combined.get(key)
            combined[key] = Nudge(
                speaker, to, dim, _int((old.delta if old else 0) + delta, -3, 3),
                tuple(dict.fromkeys((old.evidence if old else ()) + evidence)))
        importance = _int(m.get("importance"), 1, 10)
        if per_speaker.get(speaker, 0) >= PER_SPEAKER:
            drop("cap")
            continue
        if (not _FIRST_PERSON.search(text)
                or text.split(" ", 1)[0].casefold() == names[speaker].casefold()):
            drop("first_person")
            continue
        if importance is None or importance <= 1:
            drop("importance")
            continue
        if all(small_talk(ln.text, known) for ln in cited):
            drop("small_talk_lines")
            continue
        if _took_part(cited, human_guid):
            importance = max(importance, 5)
        if importance < KEEP_FROM:
            drop("small_talk")
            continue
        if human_guid in actors:
            importance = max(importance, 5)
        if any(copies(text, said_line) for ln in lines if ln.from_guid == speaker
               for said_line in (_line_text(ln, human), *_said_text(ln, said, human))):
            drop("copy")
            continue
        if m.get("disagrees") is True:
            drop("disagrees")
            continue
        if people.often(text) and sum(ln.from_guid == human_guid for ln in cited) <= 1:
            drop("frequency")  # "keeps asking" after one ask (judge B)
            continue
        what = m.get("what") if m.get("what") in WHAT else "claim"
        if what == "guild_master" and not any(ln.from_guid == human_guid for ln in cited):
            what = "claim"
        if what == "claim" and not people.attributed(text, speaker):
            drop("claim")
            continue
        t_event = int(max(ln.ts for ln in cited))
        if facts is not None and _wrong_birthday(text, speaker, t_event, facts, people,
                                                 actors):
            drop("birthday")
            continue
        # Resting only on her own lines, a memory is what she said, not how things are, unless
        # it says so ("I told ..."): hearsay whatever `what` says (review G2, Important 2).
        own = all(ln.from_guid == speaker for ln in cited)
        hearsay = what == "claim" or (own and not _FRAME.search(text))
        valence = m.get("valence")
        valence = (max(-1.0, min(1.0, float(valence)))
                   if isinstance(valence, (int, float)) and not isinstance(valence, bool)
                   and valence == valence else 0.0)
        per_speaker[speaker] = per_speaker.get(speaker, 0) + 1
        counts[f"kept:{m.get('what') if m.get('what') in WHAT else 'unlabelled'}"] += 1
        counts["hearsay"] += hearsay
        memories.append(MemoryCandidate(
            owner=speaker, kind="conversation", text=text, t_event=t_event, actors=actors,
            importance=importance, valence=valence, source="extract", evidence=evidence,
            confidence=CLAIM_CONFIDENCE if hearsay else EXTRACT_CONFIDENCE))
    nudges = [n for n in combined.values() if n.delta]
    overheard = None
    o = data.get("overheard")
    if isinstance(o, dict):
        text, imp = clean_text(o.get("text")), _int(o.get("importance"), 1, 10)
        named = [names[g] for g in people.named(text)] if text else []
        if not text or imp is None:
            counts["overheard:text"] += 1
        elif romantic(text) or (named and member_flirt(text, named)):
            counts["overheard:romance"] += 1
        elif o.get("disagrees") is True:
            counts["overheard:disagrees"] += 1
        elif facts is not None and _wrong_birthday(text, None, max(ln.ts for ln in lines),
                                                   facts, people):
            counts["overheard:birthday"] += 1  # the rehearsal's false birthday, ten times over
        else:
            overheard = (text, imp)
            counts["overheard:kept"] += 1
    return Extraction(memories, nudges, overheard, {k: v for k, v in counts.items() if v})


class Extractor:
    """One extraction call per closed thread. Writes memories and pending rel_delta rows;
    never touches relationship scores beyond `interactions` and `last_seen`. `human` is
    required: it supplies the owner's identity for the prompt and the real-name and romance
    screens. `model` is a name, or a callable read at each extraction so a reloaded mind.toml
    moves extraction to the new live model along with the talk. `said(line_id)` gives the
    talk engine's words before styling for a line the mind said (TalkEngine.said_words), None
    for any other: the copy rule reads them as well as the line as sent."""

    def __init__(self, store: Store, scheduler: LLMScheduler, writer: MemoryWriter,
                 model: str | Callable[[], str], *,
                 human_guid: Callable[[], int],
                 bystanders: Callable[[ClosedThread, set[int]], list[int]],
                 human: Callable[[], HumanIdentity],
                 said: Callable[[int], str | None] | None = None):
        self._store = store
        self._scheduler = scheduler
        self._writer = writer
        self._model = model
        self._human_guid = human_guid
        self._bystanders = bystanders
        self._human = human
        self._said = said if said is not None else (lambda line_id: None)
        self._night: str | None = None  # the night _tally counts
        self._tally: Counter[str] = Counter()

    def _count(self, night: str, result: Extraction) -> None:
        """extraction_counts, after every extraction: the night's running totals of what the
        model labelled each memory, what was kept by label (and how many as hearsay), what each
        rule dropped, how many it said disagree, and what became of the overheard line. Counts
        only, for the confirmation run's look at the labels (review G2, Important 2)."""
        if night != self._night:
            self._night, self._tally = night, Counter()
        self._tally.update(result.counts)
        self._tally["threads"] += 1

        def part(prefix: str) -> dict[str, int]:
            return {k[len(prefix):]: v for k, v in sorted(self._tally.items())
                    if k.startswith(prefix)}
        log_event("extraction_counts", night=night, threads=self._tally["threads"],
                  what=part("what:"), kept=part("kept:"), hearsay=self._tally["hearsay"],
                  dropped=part("dropped:"), disagrees=self._tally["disagrees"],
                  overheard=part("overheard:"))

    def _names(self, human_guid: int, human: HumanIdentity) -> dict[int, str]:
        """Every persona-bearing member (not only those in the thread) plus the human."""
        cast = self._store.cast()
        names = {g: cast[g].name for g in self._store.persona_versions()
                 if g in cast and g != human_guid}
        names[human_guid] = human.called
        return names

    def _known(self, t: ClosedThread, lines: list[ChatLineRow], names: dict[int, str],
               human_guid: int, human: HumanIdentity) -> tuple[list[str], ThreadFacts]:
        """What the guild already knows, for the prompt, and the facts its memories are
        checked against: tonight's date; for each member in the thread (who spoke, then whom
        the lines name, at most KNOWN_MEMBERS) the birthday and job on their card; and their
        memories that share the thread's words, best first and at most KNOWN_PER_MEMBER each,
        none resting on a whisper (_public). The real name is redacted from all of it."""
        store = self._store
        tz = store.clock.local().tzinfo
        night = date.fromisoformat(night_of(datetime.fromtimestamp(max(ln.ts for ln in lines),
                                                                   tz)))
        people = _People(names, human_guid, human)
        members: list[int] = []
        for ln in lines:
            for g in (ln.from_guid, ln.to_guid if ln.channel == "whisper" else 0,
                      *people.named(ln.text)):
                if g in names and g != human_guid and g not in members:
                    members.append(g)
        members = members[:KNOWN_MEMBERS]
        out = [f"Tonight is {MONTHS[night.month - 1]} {night.day}."]
        birthdays: dict[int, str] = {}
        for g in members:
            try:
                p = load_persona(store, g)
            except ValueError:  # an unreadable persona: nothing known about them
                p = None
            if p is None:
                continue
            bits = []
            bday = p.real_life.birthday
            if len(bday) == 5 and bday[2] == "-" and (bday[:2] + bday[3:]).isdigit():
                birthdays[g] = bday
                bits.append(f"birthday {MONTHS[int(bday[:2]) - 1]} {int(bday[3:])}")
            if job := " ".join(p.real_life.job.split()).rstrip("."):
                bits.append(f"job: {job}")
            if bits:
                out.append(f"{names[g]}: {'; '.join(bits)}.")
        out += self._known_memories(t, lines, members, names, human)
        kept: list[str] = []
        for line in (redact_real_name(x, human, human.called) for x in out):
            if estimate_tokens("\n".join(f"- {x}" for x in (*kept, line))) > KNOWN_TOKENS:
                break  # the cards come first; memories past the budget are left out
            kept.append(line)
        return kept, ThreadFacts(birthdays, tz)

    def _known_memories(self, t: ClosedThread, lines: list[ChatLineRow], members: list[int],
                        names: dict[int, str], human: HumanIdentity) -> list[str]:
        """Each member's memories that share the thread's words, best first; one held as
        hearsay says so ("(heard)"), so the model does not take it for what the guild knows."""
        store = self._store
        q = fts_query(" ".join(_line_text(ln, human) for ln in lines), terms=KNOWN_TERMS)
        if not q:
            return []
        own = {f"chat:{i}" for i in t.line_ids}
        found = []
        for g in members:
            hits = store.fts(g, q, 3 * KNOWN_PER_MEMBER)
            rows = {m.id: m for m in store.get_memories([i for i, _ in hits])}
            found += [(score, g, rows[i]) for i, score in hits
                      if i in rows and not own & set(rows[i].evidence)]
        public = self._public([m for _, _, m in found])
        out, per = [], {}
        for _, g, m in sorted(found, key=lambda x: (x[0], x[2].id)):
            if m.id in public and per.get(g, 0) < KNOWN_PER_MEMBER and len(out) < KNOWN_MEMORIES:
                per[g] = per.get(g, 0) + 1
                out.append(f"{names[g]} remembers: {m.text}"
                           + (" (heard)" if m.confidence < 0.8 else ""))
        return out

    def _public(self, rows) -> set[int]:
        """The ids of the memories among rows that rest on no whisper: what the guild may know.
        A whisper's memory stays with its two people (recall's rule), and out of this prompt
        even for their own thread, whose overheard line goes to bystanders."""
        refs = {m.id: chat_ids(m.evidence) for m in rows}
        lines = self._store.chat_line_parties(i for ids in refs.values() for i in ids)
        return {m.id for m in rows
                if all(lines[i][0] != "whisper" for i in refs[m.id] if i in lines)}

    async def extract(self, t: ClosedThread) -> ExtractReport:
        store = self._store
        done = store.thread_status(t.thread_id)
        if done in ("extracted", "skipped"):
            return ExtractReport(0, 0, 0, done)
        lines = store.chat_lines(t.line_ids)
        human_guid = self._human_guid()
        if not worth_extracting(lines, human_guid):
            store.set_thread_status(t.thread_id, store.clock.now(), "skipped")
            return ExtractReport(0, 0, 0, "skipped")
        human = self._human()
        if human is None:
            raise ValueError("Extractor needs the owner's identity")
        names = self._names(human_guid, human)
        shown = {**{ln.from_guid: ln.from_name for ln in lines}, **names}
        known, facts = self._known(t, lines, names, human_guid, human)
        messages = build_extraction_messages(lines, shown, human_guid, human=human, known=known)
        model = self._model() if callable(self._model) else self._model
        said = {ln.id: words for ln in lines if (words := self._said(ln.id))}

        async def job(llm: LLMClient):
            return await chat_json(
                llm, model, messages, EXTRACTION_SCHEMA, EXTRACT,
                lambda d: validate_extraction(d, lines, names, human_guid, human=human,
                                              facts=facts, said=said))
        try:
            result = await self._scheduler.submit(Priority.EXTRACTION, job)
        except (JSONJobFailed, LLMError, SchedulerError) as e:
            log.warning("extraction of thread %d failed: %s", t.thread_id, e)
            store.set_thread_status(t.thread_id, store.clock.now(), "failed")
            return ExtractReport(0, 0, 0, "failed")
        self._count(night_of(datetime.fromtimestamp(max(ln.ts for ln in lines),
                                                    store.clock.local().tzinfo)), result)
        written = merged = 0

        async def put(c: MemoryCandidate) -> None:
            nonlocal written, merged
            r = await self._writer.write(c)
            if r.action in ("inserted", "updated"):
                written += 1
            elif r.action == "merged":
                merged += 1
        # Every await comes first: a cancel or crash here leaves no rel_delta rows and the
        # thread still pending, so a retry cannot store them twice.
        for c in result.memories:
            await put(c)
        speakers = {ln.from_guid for ln in lines}
        if result.overheard is not None and result.overheard[1] >= OVERHEARD_MIN:
            text, imp = result.overheard
            ev = tuple(f"chat:{ln.id}" for ln in lines)
            ts = int(max(ln.ts for ln in lines))
            ids = [g for g in self._bystanders(t, set(speakers)) if g not in speakers | {human_guid}]
            for b in ids[:MAX_BYSTANDERS]:
                await put(MemoryCandidate(
                    owner=b, kind="observation", text=text, t_event=ts,
                    actors=tuple(sorted(speakers)), importance=imp, valence=0.0, source="chat",
                    evidence=ev, confidence=EXTRACT_CONFIDENCE))
        now = store.clock.now()
        last = max(ln.ts for ln in lines)
        members = sorted(g for g in speakers if g in names and g != human_guid)
        with store.transaction():
            for n in result.nudges:
                store.add_rel_delta(n.a, n.b, n.dim, n.delta, "chat extraction", n.evidence, now,
                                    applied=False)
            # Relationship rows run from a member to anyone; the human is only ever `b`.
            others = members + ([human_guid] if human_guid in speakers else [])
            for a in members:
                for b in others:
                    if a != b:
                        store.note_interaction(a, b, last)
            store.set_thread_status(t.thread_id, now, "extracted")
        return ExtractReport(written, merged, len(result.nudges), "extracted")

    async def run(self, queue: "asyncio.Queue[ClosedThread]") -> None:
        while True:
            t = await queue.get()
            try:
                await self.extract(t)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("extraction of thread %d crashed", t.thread_id)
                try:
                    self._store.set_thread_status(t.thread_id, self._store.clock.now(), "failed")
                except Exception:
                    log.exception("could not mark thread %d failed", t.thread_id)
            finally:
                queue.task_done()
