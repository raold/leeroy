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


def _words(line: str) -> list[str]:
    words = [w.replace("’", "").replace("'", "") for w in _WORD.findall(line.casefold())]
    while words and words[0] in OPENERS and len(words) > 1:
        words.pop(0)
    return words


def is_question(line: str) -> bool:
    """His line is a question (module docstring)."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if _ENDS_ASKING.search(text):
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


def asks(line: str) -> bool:
    """A question or a request: the beat's wants then lead with answering ones (talk/beat.py,
    talk/intent.py)."""
    return is_question(line) or is_request(line)


# The sentence every answer to his question carries (owner's live DM test, 2026-11-16), and the
# repetition hint it replaces the "do something else" of (talk/moves.py question_first).
QUESTION_FIRST = "Answer {their} question first, plainly, then whatever else"


__all__ = ["QUESTION_FIRST", "asks", "is_question", "is_request"]
