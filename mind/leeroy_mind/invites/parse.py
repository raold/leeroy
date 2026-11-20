"""His invitation, read from one game line by patterns: who he asks, what to do and where.
Pure: no model, no game. A line the patterns cannot settle but that names an activity or a place
and reads as an ask is `Maybe` (the live model may class it, service._classify); anything else
is None, and his line is plain talk.

What (WHAT): come (to him), meet (at a named place), fish, sit (with him), follow (him), join
(his group). Who: `you` (the member he whispers), `named` (members he names, at most two),
`anyone` ("anyone up for ...", "who wants to ...", or an open "let's" in guild or say), `group`
(his group, in party or raid chat). Where: his spot (None), a spot (spots.resolve) or UNKNOWN
(a place none of ours: she asks where). `later`: he means some other time, so nothing moves."""
from dataclasses import dataclass
import re

from leeroy_mind.talk.select import mentioned

from . import spots as S

WHAT = ("come", "meet", "fish", "sit", "follow", "join")
MAX_NAMED = 2

_APOS = str.maketrans({"’": "'", "‘": "'", "`": "'"})

# He takes it back, or asks her not to.
_NEG = re.compile(r"\b(?:don'?t|dont|do not|no need to|never mind|nvm|not now)\b")
# Some other time: nothing moves now.
_LATER = re.compile(
    r"\b(?:later|later on|tonight|tomorrow|tmrw|tmr|tomorow|sometime|some ?time|another time|"
    r"next time|at some point|after (?:the |this |our )?(?:raid|run|dungeon|bg|this|that|work|dinner)|"
    r"in an? (?:hour|bit|while|few)|in \d+ ?(?:min|mins|minutes|hours?|hrs?)|"
    r"this (?:evening|weekend)|on the weekend|at \d{1,2}(?::\d\d)?\s*(?:am|pm)?)\b")
# What he asks, by its own words (the first that matches names it).
_FOLLOW = re.compile(r"\b(?:follow me|come with me|tag along|walk with me|ride with me|stick with me)\b")
_JOIN = re.compile(r"\b(?:join (?:my|me in my|our|the) (?:group|party|grp)|group up|party up|"
                   r"join up|get in (?:my|the) (?:group|party)|hop in (?:my|the) (?:group|party))\b")
# Fishing as something to do, not a fish he has or a fishing pole he wants.
_FISH = re.compile(r"\b(?:go(?:ing)? fishing|fishing(?! (?:pole|poles|rod|rods|skill|trainer|bait|lure|"
                   r"lures|supplies|gear|line|hat|daily|dailies|quest|quests))|fish with (?:me|us)|"
                   r"to fish|let'?s fish|wanna fish|come fish|fish together|catch (?:some )?fish)\b")
# Sitting or a drink together, not a mage's drinks.
_SIT = re.compile(r"\b(?:sit (?:down )?with (?:me|us)|come sit|sit down|grab a seat|take a seat|"
                  r"to sit (?:at|in|by|around|together|for a bit)|sit together|"
                  r"(?:have|grab|get|share|go for|come for|fancy|up for|want) (?:a |some )?"
                  r"(?:drink|drinks|pint|pints|beer|beers|ale|brew|cuppa))\b")
_COME = re.compile(
    r"\b(?:come (?:here|over|over here|to me|find me|see me|say hi|hang|hang out|chill|"
    r"keep me company|and hang|n hang|join me)|c'?mere|get over here|over here|"
    r"keep me company|hang out with me|hang with me|come hang|come chill)\b")
_MEET = re.compile(r"\bmeet(?: me| us| up)?\b")
# Going somewhere: an invitation only to a place, and only when asked ("let's go to the inn").
_GO = re.compile(
    r"\b(?:see you (?:at|in|by)|join me (?:at|in|by)|head (?:to|over to|down to)|heading (?:to|over to)|"
    r"go (?:to|over to)|let'?s go(?: to)?|let'?s hit|let'?s head|"
    r"come (?:to|down to|up to|over to|on over to|by)|stop by|swing by|pop by|drop by)\b")
_JOIN_ME = re.compile(r"\bjoin me\b")
# An ask: "let's", "wanna", "anyone", "come", "with me", "up for", ... (a statement is no invitation).
_CUE = re.compile(
    r"\b(?:let'?s|lets|wanna|want to|you want|u want|want 2|come|join|with me|with us|anyone|anybody|"
    r"any1|someone|somebody|who'?s up|who wants|who is up|who'?s in|up for|fancy|care to|feel like|"
    r"how about|we should|shall we|should we|would you like|do you want|d'?you want|you up for|"
    r"you in|u in|y'?all in|who'?s coming|you coming|u coming)\b")
# A question that asks about her ("do you fish?") is no ask; one that offers ("fishing tomorrow?") is.
_ABOUT_HER = re.compile(r"^(?:do|does|did|can|could|have|has|are|is|was|were|what|why|how|where|"
                        r"when|who|which|will|would|should)\b")
# Who: anyone, or everyone he is with.
_ANYONE = re.compile(r"\b(?:anyone|anybody|any1|someone|somebody|who'?s up|who wants|who is up|"
                     r"who'?s in|any takers|whoever)\b")
# Words that make a line worth the model's reading when no pattern settled it.
_HINT = re.compile(r"\b(?:fish|fishing|inn|tavern|pub|bar|drink|drinks|bank|fountain|eventide|"
                   r"krasus|landing|docks|hang out|company|meet)\b")


@dataclass(frozen=True, slots=True)
class Ask:
    """One invitation: `what` (WHAT), `who` (you, named, anyone, group), the members he named
    (guids, at most MAX_NAMED), `place` (a spots.Spot, None for his spot, spots.UNKNOWN) and
    `later` (nothing moves now)."""
    what: str
    who: str
    named: tuple[int, ...]
    place: object
    later: bool = False


@dataclass(frozen=True, slots=True)
class Maybe:
    """A line that may be an invitation: the model reads it (service._classify)."""
    who: str
    named: tuple[int, ...]


def normalize(text: str) -> str:
    return " ".join(text.translate(_APOS).lower().split())


def _named(text: str, names: dict[int, str]) -> tuple[int, ...]:
    """The members his line names, in the order he wrote them: talk's own reading (a whole name,
    or a prefix of four letters or more that only one name starts with)."""
    return tuple(mentioned(text, names))


def _who(text: str, channel: str, named: tuple[int, ...]) -> str:
    if channel == "whisper":
        return "you"
    if named:
        return "named"
    if channel in ("party", "raid") and not _ANYONE.search(text):
        return "group"
    return "anyone"


def _asks(text: str) -> bool:
    """An ask: a cue word, or an offer put as a question."""
    return bool(_CUE.search(text)) or (text.endswith("?") and not _ABOUT_HER.search(text))


def _what(text: str, channel: str, his_zone: int) -> str | None:
    if _FOLLOW.search(text):
        return "follow"
    if _JOIN.search(text):
        return "join"
    if _FISH.search(text) and _asks(text):
        return "fish"
    if _SIT.search(text) and _asks(text):
        return "sit"
    if _MEET.search(text):
        return "meet"
    if (_GO.search(text) and (channel == "whisper" or _CUE.search(text))
            and S.resolve(text, his_zone=his_zone) is not None):
        return "meet"
    if _COME.search(text):
        return "come"
    return None


def _where(what: str, text: str, his_zone: int) -> tuple[str, object]:
    place = S.resolve(text, his_zone=his_zone, fishing=what == "fish")
    if what == "come" and isinstance(place, S.Spot):
        what = "meet"  # "come to the inn" is where, not beside him
    if what == "meet" and place is None:
        if _COME.search(text) or _JOIN_ME.search(text):
            what = "come"  # "come join me" names no place: beside him
        else:
            place = S.UNKNOWN  # "meet me" where? she asks
    return what, place


def parse(text: str, *, channel: str, names: dict[int, str], his_zone: int) -> Ask | Maybe | None:
    """His line as an invitation, `Maybe` for the model, or None. `names`: the cast's names by
    guid (him left out)."""
    t = normalize(text)
    if not t or t.startswith((".", "!", "/")) or len(t) > 200:
        return None
    if _NEG.search(t):
        return None
    named = _named(t, names)[:MAX_NAMED]
    who = _who(t, channel, named)
    what = _what(t, channel, his_zone)
    if what is None:
        if _HINT.search(t) and (_CUE.search(t) or t.endswith("?")):
            return Maybe(who, named)
        return None
    what, place = _where(what, t, his_zone)
    return Ask(what, who, named, place, bool(_LATER.search(t)))


def ask_of(what: str, text: str, *, channel: str, names: dict[int, str],
           his_zone: int) -> Ask | None:
    """The model's class for a Maybe line made an Ask, with the patterns' who and where; None for
    "none". "later" keeps nothing moving."""
    if what == "none" or what not in (*WHAT, "later"):
        return None
    t = normalize(text)
    named = _named(t, names)[:MAX_NAMED]
    later = what == "later" or bool(_LATER.search(t))
    if what == "later":
        what = _what(t, channel, his_zone) or ("fish" if _FISH.search(t) else "meet")
    what, place = _where(what, t, his_zone)
    return Ask(what, _who(t, channel, named), named, place, later)
