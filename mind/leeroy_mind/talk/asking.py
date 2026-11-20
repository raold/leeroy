"""Whether his line asks her something (owner's live DM test, 2026-11-16: "not answering the
questions"). Two shapes, read from his words alone:

- a question: it ends with a question mark (an emoticon after it allowed), or it opens with a
  question word (what, when, where, why, how, who) or with an auxiliary followed by its subject
  ("do you", "are we", "is it", "can u", "have you"), so "have fun" and "do it" ask nothing (a
  "do" before "it", "that", "the" and the like is an imperative);
- a request: it opens with please or a verb asking her to do something ("tell me", "send me",
  "show me", "let me know", "pick one"), or says please anywhere.

A few chat openers before either ("so", "ok", "lol", "hey", "wait", "and", "but", "also", "btw")
are read past. Every answer to him on every channel (guild, party, raid, voice, whisper, Discord
channel, group DM and DM) reads the same rule; nothing here knows a member or a medium.

`buried` ([talk] coherence and the length rule; blind sandbox test, 2026-11-20: "...nightmare
day and i have not eaten yet so did you eat yet" and "you ever been out west" read as no
question) also finds a question clause anywhere in his line, with no question mark: his line is
split at punctuation, at line breaks (a Discord post's lines keep theirs) and at joining
words ("and", "but", "so", "lol", "btw"...), and a clause
after the first asks when it is a question by stricter rules than the line's start (a joined
"and did the dishes" or "and when i got home" is no question):

- a question word with a question's next word: "what about you", "hows work", "when does it
  start", "how hard was it" (never "when i got home", "what i said");
- an auxiliary with a person as its subject and more after it: "did you have lunch yet", "are
  you working tonight", "have you eaten" ("so do you" after "so", "neither" or "nor" is no
  question; a clause of only the two words is one only after punctuation: "i had pizza, did
  you");
- "you" with the auxiliary left out: "you ever been out west", "you eaten yet", "you free
  tonight", "you still at work", a "you ... yet" with no negation;
- "you", "u", "yourself" alone after the first clause ("im good, you"), wbu or hbu, and an offer
  or ask with its subject left out ("wanna raid later", "any plans tonight", "got a sec",
  "anyone up for heroics").

The first clause is also read by these rules, so his line may open with one ("you ever been out
west", "am i late"). Off (the default) every reading is exactly as before.

Pure: no I/O, no logging."""
import re

QUESTION_WORDS = frozenset({"what", "whats", "when", "whens", "where", "wheres", "why", "whys",
                            "how", "hows", "who", "whos", "which"})
AUXILIARIES = frozenset({"do", "does", "did", "can", "could", "will", "would", "are", "is", "have",
                         "was", "were", "has", "should", "shall"})
# what follows an auxiliary when it opens a question: its subject
SUBJECTS = frozenset({"i", "you", "u", "ya", "ye", "we", "they", "he", "she", "it", "that", "this",
                      "there", "these", "those", "anyone", "anybody", "someone", "somebody",
                      "everyone", "everybody", "yall", "ur", "your", "my", "our", "his", "her",
                      "their", "the", "a", "an", "any", "some", "ppl", "people"})
# what follows "do" in an imperative, never a question's subject: "do it", "do that again"
DO_OBJECTS = frozenset({"it", "that", "this", "the", "a", "an", "some", "any", "those", "these"})
REQUEST_VERBS = frozenset({"please", "pls", "plz", "tell", "send", "show", "give", "let",
                           "describe", "explain", "pick", "choose", "guess", "remind", "help",
                           "bring", "come", "meet", "join", "teach", "call", "text", "message",
                           "dm", "whisper", "say", "promise", "wish", "talk"})
OPENERS = frozenset({"so", "ok", "okay", "lol", "lmao", "haha", "hey", "hi", "wait", "and", "but",
                     "also", "btw", "um", "uh", "hmm", "omg", "oh", "well", "babe", "baby", "hun"})
_WORD = re.compile(r"[a-z]+(?:['’][a-z]+)*")
_ENDS_ASKING = re.compile(r"\?+\W*(?:[;:=xX8]['-]?[)(dDpP3/\\|]+\W*)*$")
_PLEASE = re.compile(r"\b(?:please|pls|plz)\b", re.IGNORECASE)

# -- a question buried in his line (`buried`, module docstring) --------------------------------
# where a clause ends: punctuation, a dash between words, a line break...
_CLAUSE_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)*")  # "eaten yet", "back at 9"
_BREAK = re.compile(r"[,.;:!?…()]+|\s[-–—]+\s|\n")
# ...and a joining word, which starts the next clause
JOINERS = frozenset({"and", "but", "so", "also", "anyway", "anyways", "btw", "lol", "lmao",
                     "haha", "hah", "tho", "though", "then", "plus", "or", "ok", "okay", "oh",
                     "well", "omg", "idk", "tbh"})
# ...but not "ok" or "well" right after "you", "still" or "doing": "you ok", "you doing well"
_SOFT_JOINERS = frozenset({"ok", "okay", "well"})
_SOFT_AFTER = frozenset({"you", "u", "ya", "yall", "ye", "still", "been", "doing", "feeling",
                         "going"})
# "so do you", "neither do i", "nor have we": the same again, no question
_INVERSION = frozenset({"so", "neither", "nor", "as", "than"})
_WH_CONTRACTED = frozenset({"whats", "whens", "wheres", "whys", "hows", "whos"})
# a question word's next word in a question ("what about you", "how long", "what time")
_WH_NEXT = frozenset({"about", "time", "else", "happened", "come", "if", "kind", "sort", "type",
                      "many", "much", "long", "far", "often", "old", "late", "early", "soon",
                      "up", "am"})
# an auxiliary opening a buried question; and the person it must ask about
_BURIED_AUX = AUXILIARIES | {"am", "may", "might"}
_PERSONS = frozenset({"i", "you", "u", "ya", "ye", "yall", "we", "they", "he", "she", "anyone",
                      "anybody", "someone", "somebody", "everyone", "everybody"})
# ...or a thing, after a form of "be" or a modal: "and is it worth it", "was that you"
_THINGS = frozenset({"it", "that", "this", "there"})
_BE_OR_MODAL = frozenset({"is", "was", "are", "were", "am", "will", "would", "can", "could",
                          "should", "shall", "may", "might"})
_OWNERS = SUBJECTS | {"yours", "urs", "mine", "ours", "theirs"}  # "how bad was yours"
_YOU = frozenset({"you", "u", "ya", "yall", "ye"})
# what follows "you" in a question that left its auxiliary out: "you ever been out west"
_YOU_ASKS = frozenset({"ever", "eaten", "free", "around", "up", "awake", "busy", "home",
                       "coming", "going", "working", "playing", "raiding", "online", "there",
                       "back", "ok", "okay", "alright", "good", "done", "ready", "doing", "seen",
                       "heard", "tried", "remember", "want", "wanna", "feeling", "sleeping",
                       "logging", "hungry", "tired", "sick", "out"})
# after "you still" or "you been": where or how she is, never "you still owe me"
_YOU_STILL = frozenset({"at", "in", "up", "there", "around", "awake", "busy", "coming",
                        "playing", "working", "raiding", "online", "mad", "upset", "ok", "okay",
                        "alright", "home", "out", "down", "free", "going", "doing", "on", "to",
                        "sleeping", "sick", "well", "good", "able"})
_MEALS = frozenset({"yet", "lunch", "dinner", "breakfast", "anything", "today", "tonight"})
_NEGATIONS = frozenset({"not", "no", "never", "dont", "didnt", "havent", "hasnt", "cant",
                        "wont", "isnt", "arent", "wasnt", "werent", "aint", "nothing"})
_ALONE = (("you",), ("u",), ("ya",), ("yourself",), ("urself",))
_WBU = frozenset({"wbu", "hbu", "wby", "hby"})
# an offer or ask with its "do you" left out: "wanna raid later", "any plans tonight"
_ANY = frozenset({"plans", "luck", "idea", "ideas", "news", "word", "update", "updates",
                  "chance", "tips", "thoughts"})
_GOT = frozenset({"time", "plans", "any"})
_GOT_A = frozenset({"sec", "second", "minute", "min", "moment", "mo"})
_ANYONE = frozenset({"anyone", "anybody"})
_ANYONE_ASKS = frozenset({"up", "around", "free", "want", "wanna", "know", "seen", "got",
                          "online", "going", "coming", "down", "need", "else"})


def _words(line: str) -> list[str]:
    words = [w.replace("’", "").replace("'", "") for w in _WORD.findall(line.casefold())]
    while words and words[0] in OPENERS and len(words) > 1:
        words.pop(0)
    return words


def _clauses(line: str) -> list[tuple[str, list[str]]]:
    """(the joining word before it, "" after punctuation or at the start; its words) for each
    clause of his line, in order."""
    out: list[tuple[str, list[str]]] = []
    for part in _BREAK.split(line.casefold()):
        before, clause = "", []
        for w in (x.replace("’", "").replace("'", "") for x in _CLAUSE_WORD.findall(part)):
            kept = w in _SOFT_JOINERS and bool(clause) and clause[-1] in _SOFT_AFTER
            if w in JOINERS and not kept:
                if clause:
                    out.append((before, clause))
                before, clause = w, []
            else:
                clause.append(w)
        if clause:
            out.append((before, clause))
    return out


def _clause_asks(before: str, w: list[str], first: bool) -> bool:
    """One clause of his line is a question by the buried rules (module docstring)."""
    head = w[0]
    if head in _WBU:
        return True
    if not first and tuple(w) in _ALONE:
        return True  # "im good, you", "im fine and u"
    if head in _WH_CONTRACTED:
        return True  # "hows work", "whats new with you"
    if head in QUESTION_WORDS:
        if len(w) > 1 and (w[1] in _WH_NEXT or w[1] in AUXILIARIES):
            return True
        return len(w) > 3 and w[2] in AUXILIARIES and w[3] in _OWNERS  # "how hard was it"
    if head in _BURIED_AUX and len(w) > 1:
        who = w[1]
        if not (who in _PERSONS or (head in _BE_OR_MODAL and who in _THINGS)):
            return False  # "and did the dishes", "and did it again", "have a good one"
        if before in _INVERSION and len(w) == 2:
            return False  # "so do you", "neither do i"
        return len(w) > 2 or not before  # "and is it" no; "i had pizza, did you" yes
    if head in _YOU and len(w) > 1:
        rest = w[2:] if w[1] == "guys" else w[1:]
        if not rest or _NEGATIONS & set(rest):
            return False
        if rest[0] in ("still", "been"):
            return len(rest) > 1 and rest[1] in _YOU_STILL
        if rest[0] == "eat":
            return bool(_MEALS & set(rest[1:]))
        return rest[0] in _YOU_ASKS or (len(rest) > 1 and rest[-1] == "yet")
    if head == "wanna" and len(w) > 1:
        return True
    if head == "want" and len(w) > 1 and w[1] in ("to", "me", "some", "any", "in"):
        return True
    if head == "any" and len(w) > 1:
        return w[1] in _ANY
    if head == "got" and len(w) > 1:
        return w[1] in _GOT or (w[1] == "a" and len(w) > 2 and w[2] in _GOT_A)
    return head in _ANYONE and len(w) > 1 and w[1] in _ANYONE_ASKS


def _lines(line: str) -> str:
    """His line with each of its lines' whitespace collapsed and its line breaks kept: a break
    ends a clause ("long day at work\ndid you eat yet")."""
    return "\n".join(" ".join(x.split()) for x in (line or "").splitlines() if x.strip())


def buried_question(line: str) -> bool:
    """His line holds a question clause the line-start reading misses, with or without a
    question mark (module docstring). Pass his text as it came: its line breaks are clause
    breaks."""
    clauses = _clauses(_lines(line))
    return any(_clause_asks(before, words, i == 0) for i, (before, words) in enumerate(clauses))


def is_question(line: str, *, buried: bool = False) -> bool:
    """His line is a question (module docstring); `buried`: also one with a question clause
    anywhere in it (buried_question)."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if _ENDS_ASKING.search(text):
        return True
    if buried and buried_question(line):  # his own text: its line breaks end clauses
        return True
    words = _words(text)
    if not words:
        return False
    if words[0] in QUESTION_WORDS:
        return True
    if words[0] == "do" and len(words) > 1 and words[1] in DO_OBJECTS:
        return False  # "do it", "do that again": an imperative
    return words[0] in AUXILIARIES and len(words) > 1 and words[1] in SUBJECTS


def is_request(line: str) -> bool:
    """His line asks her to do something (module docstring)."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if _PLEASE.search(text):
        return True
    words = _words(text)
    return bool(words) and words[0] in REQUEST_VERBS


def asks(line: str, *, buried: bool = False) -> bool:
    """A question or a request: the beat's wants then lead with answering ones (talk/beat.py,
    talk/intent.py). `buried`: a question clause anywhere in it counts too (buried_question)."""
    return is_question(line, buried=buried) or is_request(line)


# The sentence every answer to his question carries (owner's live DM test, 2026-11-16), and the
# repetition hint it replaces the "do something else" of (talk/moves.py question_first).
QUESTION_FIRST = "Answer {their} question first, plainly, then whatever else"


__all__ = ["QUESTION_FIRST", "asks", "buried_question", "is_question", "is_request"]
