"""His "you lead", read from one game line by patterns: who he asks, what to do, where to. Pure:
no model, no game. Anything the patterns do not settle is None, and his line is plain talk.

Start: `activity` quest ("you lead", "take the lead", "lead the way", "take us questing": new
rpg's own day of quests, camps and grinding), grind ("go grind", "take us grinding") or travel
("lead us to the inn in orgrimmar": `place` a spots.Spot, or spots.UNKNOWN for a place none of
ours, where nothing moves and she says she doesn't know the way). Who: `you` (the member he
whispers), `named` (the member he names), `anyone` ("who wants to lead?") or `group` (his group,
in party or raid chat without a name). A line for some other time ("you lead tonight") is none
of the lead's: "Ana, you're leading tonight" is the raid lead's designation (raidlead/).

End: "i'll lead", "stop leading", "follow me", "stop", ... (the service honours one only while a
lead of his is in force: otherwise "i'll lead" stays the raid lead's, and the rest is talk or a
playerbots command). `command`: the line is a playerbots command too ("follow me"), which still
reaches playerbots and gets no answer."""
from dataclasses import dataclass
import re

from leeroy_mind.invites import spots as S
from leeroy_mind.talk.select import mentioned
from leeroy_mind.talk.world import looks_like_bot_command

ACTIVITIES = ("quest", "grind", "travel")
MAX_CHARS = 160
MAX_WORDS = 18

_APOS = str.maketrans({"’": "'", "‘": "'", "`": "'", "ʼ": "'"})

# He takes the lead himself, or ends hers.
_HE_LEADS = re.compile(
    r"\b(?:i'?ll (?:lead|take (?:the |it from |over )?(?:lead|here|over)|take over)|i'?m leading|i lead|"
    r"let me lead|my lead|i'?ll take it from here)\b")
_STOP = re.compile(
    r"^(?:ok(?:ay)? |alright |right |hey )?(?:stop(?: leading| there| here| now)?|that'?s enough|enough|"
    r"follow me(?: now| again)?|back to me|come back(?: to me)?|regroup(?: on me)?)[.!]*$")
# Not him taking it back: "don't let me lead", "i won't lead" ("never mind, i'll lead" is his).
_END_NEG = re.compile(r"\b(?:don'?t|dont|do not|never(?! mind)|no need to|shouldn'?t|wouldn'?t|won'?t)\b")
# Not now, or not that.
_NEG = re.compile(r"\b(?:don'?t|dont|do not|never|no need to|not now|nvm|never mind|shouldn'?t|wouldn'?t)\b")
_LATER = re.compile(
    r"\b(?:later|later on|tonight|tomorrow|tmrw|tmr|sometime|another time|next time|"
    r"after (?:the |this |our )?(?:raid|run|dungeon|bg|this|that|dinner)|in an? (?:hour|bit|while|few)|"
    r"in \d+ ?(?:min|mins|minutes|hours?|hrs?)|this (?:evening|weekend))\b")
# Taking the lead, in his words (a statement to her, or an ask).
_LEAD = re.compile(
    r"\b(?:(?:you |u )?take (?:the |over the )?lead|(?:you |u )?lead the way|lead on|"
    r"(?:you|u) lead(?: (?:us|me))?(?! (?:a|an|the|my|your|his|her|their|our)\b)|"
    r"lead (?:us|me)(?: (?:around|somewhere|anywhere|for a bit|for a while|on))?(?! (?:to|into)\b)|"
    r"show (?:us|me) (?:the way|around)|your lead|"
    r"(?:you|u) pick where (?:we|to) go)\b")
# An ask of hers: "can you lead?", "wanna lead?", "mind taking the lead?".
_ASK = re.compile(
    r"\b(?:(?:can|could|would|will) (?:you|u)(?: please)?|wanna|want to|mind|feel like) "
    r"(?:lead(?:ing)?(?: (?:us|me|the way))?|tak(?:e|ing) (?:the )?lead)\b")
# Anyone of them: "who wants to lead?", "someone take the lead".
_ANYONE = re.compile(
    r"\b(?:anyone|anybody|any1|someone|somebody|who wants to|who'?s gonna|whoever|one of you)"
    r"(?: (?:want|wants|wanna)(?: to)?)? (?:lead|take (?:the )?lead)\b")
_GRIND = re.compile(
    r"\b(?:go grind(?:ing)?(?: (?:some )?(?:mobs|stuff|xp))?|"
    r"(?:take|lead|bring) (?:us|me) (?:out )?(?:grinding|to grind)|"
    r"find (?:us|me) (?:something|some mobs|mobs) to (?:grind|kill))\b")
_GRIND_WORDS = re.compile(r"\b(?:grind|grinding|mobs|farm|farming)\b")
_QUEST = re.compile(
    r"\b(?:(?:take|lead|bring) (?:us|me) (?:out )?(?:questing|to (?:do )?quests?|on (?:some |a )?quests?)|"
    r"find (?:us|me) (?:some )?quests)\b")
_QUEST_WORDS = re.compile(r"\bquest(?:s|ing)?\b")
# Somewhere he names: "lead us to the inn", "take us to dalaran", "lead the way to the bank".
_TO = re.compile(
    r"\b(?:(?:lead|take|bring|guide|show) (?:us|me)(?: the way)?|lead the way|(?:you|u) lead us) "
    r"(?:to|into) (?P<place>[^,.;!?]+)")


@dataclass(frozen=True, slots=True)
class Start:
    """His ask that she lead: `activity` (ACTIVITIES), `who` (you, named, anyone, group), the
    member he named (at most one, a guid), `place` (travel: a spots.Spot or spots.UNKNOWN; else
    None)."""
    activity: str
    who: str
    named: tuple[int, ...]
    place: object = None


@dataclass(frozen=True, slots=True)
class End:
    """He takes the lead back. `command`: the line is a playerbots command as well. `his`: he says
    he leads ("i'll lead"), which also takes the raid's calling back from her (raidlead/)."""
    command: bool = False
    his: bool = False


def normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.translate(_APOS).lower().split())


def ends(text: str) -> End | None:
    """His line as the end of a lead in force, or None."""
    t = normalize(text)
    if not t or len(t) > MAX_CHARS or _END_NEG.search(t) or _LATER.search(t):
        return None  # "don't let me lead", "i'll lead tomorrow": not now
    if _HE_LEADS.search(t) or _STOP.match(t):
        return End(command=looks_like_bot_command(t), his=bool(_HE_LEADS.search(t)))
    return None


def _who(text: str, channel: str, named: tuple[int, ...]) -> str:
    if channel == "whisper":
        return "you"
    if named:
        return "named"
    if _ANYONE.search(text):
        return "anyone"
    return "group"


def _activity(t: str, his_zone: int) -> tuple[str, object] | None:
    """(activity, place) his line asks for, or None when it asks nobody to lead."""
    to = _TO.search(t)
    if to is not None:
        where = to["place"].strip()
        if _GRIND_WORDS.search(where):
            return "grind", None
        if _QUEST_WORDS.search(where):
            return "quest", None
        place = S.resolve(where, his_zone=his_zone)
        return "travel", place if isinstance(place, S.Spot) else S.UNKNOWN
    if _GRIND.search(t):
        return "grind", None
    if _QUEST.search(t):
        return "quest", None
    if _LEAD.search(t) or _ASK.search(t) or _ANYONE.search(t):
        if _GRIND_WORDS.search(t):
            return "grind", None
        return "quest", None
    return None


def parse(text: str, *, channel: str, names: dict[int, str], his_zone: int) -> Start | None:
    """His line as an ask that she lead, or None. `names`: the cast's names by guid (him left
    out). His own lead ("i'll lead") is never a start: see `ends`."""
    t = normalize(text)
    if not t or t.startswith((".", "!", "/")) or len(t) > MAX_CHARS or len(t.split()) > MAX_WORDS:
        return None
    if _HE_LEADS.search(t) or _NEG.search(t) or _LATER.search(t):
        return None
    found = _activity(t, his_zone)
    if found is None:
        return None
    activity, place = found
    named = tuple(mentioned(t, names))[:1]
    return Start(activity, _who(t, channel, named), named, place)
