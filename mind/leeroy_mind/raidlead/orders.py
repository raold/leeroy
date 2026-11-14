"""His words as raid orders: a fixed list the bodies can carry out, never anything else.

An order needs an explicit addressee (everyone, all, raid, tanks, healers, dps, melee, ranged or
a member's name) or one of a few unambiguous phrases ("wait for my pull", "stack on me", "spread
out", "tanks swap", "lust now", "back off"); a single word or a short everyday phrase alone is
never an order. Deterministic patterns come first. An addressed line led by an order word that
no pattern resolves goes to the live model, whose JSON answer is restricted to this list plus
"none" by an enum (`schema`): it can never invent an action. Everything here is pure; nothing
logs his words.
"""

from dataclasses import dataclass
import re

# The fixed list, each mod-leeroy's bot_do "order" of that name (RaidLead.cpp, LeeroyRaidLeadPolicy.h
# kRaidOrders). Native's "lift" (undo a hold) is the raid lead's own, never his words.
ORDERS = ("follow", "come_back", "stay", "stack", "spread", "run_out", "back_off", "hold",
          "attack", "kill_skull", "taunt", "lust", "release")
# Who an order is for: the whole group, a role, the damage dealers by range, or one member.
WHO = ("everyone", "tanks", "healers", "damage", "melee", "ranged", "member")
MAX_CHARS = 200
MAX_WORDS = 12

# What each order is, in words: the model's list and the code-built facts the members speak from.
MEANING = {
    "follow": "follow him", "come_back": "come back to him", "stay": "stay where they are",
    "stack": "stack on one spot", "spread": "spread out", "run_out": "run out of the group",
    "back_off": "fall back to his side and fight on", "hold": "damage dealers hold their attacks until his pull",
    "attack": "attack his target", "kill_skull": "kill the skull-marked target",
    "taunt": "swap tanks: a tank not holding his target taunts it",
    "lust": "Bloodlust or Heroism now", "release": "the dead release and run back",
}


@dataclass(frozen=True, slots=True)
class Order:
    action: str
    who: str = "everyone"
    member: int | None = None  # who == "member"
    on: int | None = None  # stack: the member to stack on; None stacks on him
    by_model: bool = False


@dataclass(frozen=True, slots=True)
class Designate:
    guid: int | None  # None: he leads it himself and nobody calls


@dataclass(frozen=True, slots=True)
class AskPlan:
    member: int | None = None  # who he asked; None: the lead answers
    boss: tuple[int, int] | None = None  # the boss he named (map, encounter index); None: the one here


@dataclass(frozen=True, slots=True)
class Ready:
    member: int | None = None


@dataclass(frozen=True, slots=True)
class Countdown:
    seconds: int
    member: int | None = None


@dataclass(frozen=True, slots=True)
class Unclear:
    """An order attempt no pattern resolved: the model may map it, or the lead asks."""
    who: str = "everyone"
    member: int | None = None


Intent = Order | Designate | AskPlan | Ready | Countdown | Unclear

_GROUPS = {
    "everyone": "everyone", "everybody": "everyone", "all": "everyone", "guys": "everyone",
    "y'all": "everyone", "yall": "everyone", "raid": "everyone", "team": "everyone",
    "group": "everyone", "folks": "everyone", "people": "everyone", "ladies": "everyone",
    "tanks": "tanks", "tank": "tanks", "healers": "healers", "healer": "healers",
    "heals": "healers", "healz": "healers", "dps": "damage", "deeps": "damage",
    "melee": "melee", "ranged": "ranged", "range": "ranged", "casters": "ranged",
}
_LEAD_FILLER = re.compile(r"^(?:(?:ok|okay|alright|right|now|so|pls|please|and)\b[ ,]*)+")
_TAIL_FILLER = re.compile(r"(?:[ ,]+(?:now|please|pls|asap|quick|quickly|right now|go go|thanks|ty))+$")
_PUNCT = re.compile(r"[.!]+$")

_P = {
    "kill_skull": r"(?:(?:kill|nuke|focus|attack|dps|burn|hit|go|on) (?:the )?skull(?: first)?|skull(?: first| it)?|kill skull first)",
    "lust": r"(?:(?:pop|use|cast|hit) )?(?:blood ?lust|lust|hero(?:ism)?|bl|time warp)",
    "taunt": r"(?:(?:tanks? )?swap(?: tanks?)?|tank swap|taunt(?: (?:it|him|her|that|the boss|now|swap))?)",
    "release": r"(?:release(?:,? (?:and )?run (?:back|in))?|rel and run(?: back)?|run back(?: in)?)",
    "come_back": r"(?:come back(?: here| to me)?|get back here|back to me|regroup(?: on me)?|return to me)",
    "follow": r"(?:follow(?: me)?|come with me|with me|come here|come to me|move with me)",
    "stack": r"(?:(?:stack|collapse|group up|clump)(?: up)?(?: (?:on|at|to) (?P<on>[^\W\d_][\w'-]*))?|on (?P<on2>[^\W\d_][\w'-]*))",
    "spread": r"(?:spread(?: out| apart| up)?|split up)",
    "run_out": r"(?:run out|get out(?: of (?:the )?(?:raid|group|there|it))?|move out)",
    "back_off": r"(?:back off|fall back)",
    "stay": r"(?:stay(?: here| put| there| back)?|hold (?:position|here|this spot)|stand (?:here|still)|(?:don't|dont|do not) move|stop moving)",
    "hold": r"(?:hold(?: on| up| dps| fire| damage)?|wait(?: for (?:my|the) pull| for me| for it)?|stop(?: attacking| dps(?:ing)?| damage| hitting(?: it)?)?|(?:don't|dont|do not) (?:attack|pull|dps|touch it)(?: yet)?|no dps)",
    "attack": r"(?:attack(?: (?:my target|it|that|this|him|her|them))?|kill (?:it|that|this|my target|him|her|them)|nuke(?: it| that| him| her)?|burn(?: it| him| her| that)?|dps(?: it| my target)?|focus(?: my target)?|engage|go(?: go(?: go)?)?|charge)",
}
# Stay before hold ("hold position"), the rest as listed.
_ORDER_RE = tuple((name, re.compile(_P[name])) for name in (
    "kill_skull", "lust", "taunt", "release", "come_back", "follow", "stack", "spread",
    "run_out", "back_off", "stay", "hold", "attack"))
# Without an addressee only these whole phrases are orders (P2-2): every one is two words or more
# and has no everyday meaning in a raid's chat. "follow me" and "stay here" stay playerbots' own.
_PLAIN = {
    "hold": r"(?:wait for (?:my|the) pull|stop attacking|stop dps(?:ing)?|hold (?:your )?dps|hold (?:your )?damage|no dps until (?:my|the) pull)",
    "stack": r"(?:(?:stack|collapse|group)(?: up)? on (?P<on>[^\W\d_][\w'-]*))",
    "spread": r"(?:spread out|spread apart)",
    "taunt": r"(?:tanks? swap|swap tanks|taunt swap)",
    "lust": r"(?:(?:blood ?lust|lust|hero(?:ism)?|bl) now|pop (?:blood ?lust|lust|hero(?:ism)?|bl))",
    "back_off": r"(?:back off|fall back)",
    "kill_skull": r"(?:kill (?:the )?skull(?: first)?|focus (?:the )?skull|skull first)",
    "release": r"(?:release(?:,| and) run (?:back|in))",
    "come_back": r"(?:come back to me|regroup on me|get back here)",
    "stay": r"(?:hold (?:your )?position)",
    "attack": r"(?:(?:attack|kill|focus|nuke) my target)",
}
_PLAIN_RE = tuple((name, re.compile(_PLAIN[name])) for name in (
    "kill_skull", "lust", "taunt", "release", "come_back", "stay", "stack", "spread", "back_off",
    "hold", "attack"))
# Order words: a line led by one (after an addressee) is an order attempt the model may map.
_STRONG = frozenset({"stack", "spread", "taunt", "swap", "lust", "bloodlust", "heroism", "hero",
                     "bl", "release", "collapse", "regroup", "nuke", "focus", "kill", "attack",
                     "burn", "follow", "stay", "hold", "skull"})
_WEAK = frozenset({"go", "get", "come", "run", "move", "stop", "back", "fall", "range", "wait",
                   "stand", "group", "pop", "use", "dps", "don't", "dont", "pull", "kite"})

_READY = re.compile(r"(?:rc|ready check)")
_COUNT = re.compile(r"(?:pull(?:ing)?(?: in| on) (?P<n>\d{1,2})(?: ?s(?:ec(?:ond)?s?)?)?"
                    r"|(?P<n2>\d{1,2}) (?:sec(?:ond)?s? )?(?:to|till|until) pull)")
_PLAN = re.compile(r"(?:(?:what's|whats|what is) (?:the|our) (?:plan|strat(?:egy)?|tactics?)"
                   r"(?: (?:here|now|for (?:this|the) (?:one|boss|fight)))?\??"
                   r"|(?:plan|strat|tactics)\?|how do we do (?:this|the boss|this boss|this fight)\??"
                   r"|what do we do(?: here| now)?\?|how does (?:this|the) (?:boss|fight) work\??"
                   r"|explain (?:the )?(?:fight|boss|plan|strat))")
# A question about the plan for a boss he names ("what do we do on heigan"): only with the boss
# briefs on ([raidlead] briefs), and only when the name is exactly one of `bosses`.
_PLAN_FOR = re.compile(r"(?:(?:what's|whats|what is) (?:the|our) (?:plan|strat(?:egy)?|tactics?) (?:for|on|against)"
                       r"|what do we do (?:on|for|at|against|with|about)"
                       r"|how do we (?:do|kill|beat|handle|deal with)"
                       r"|explain) (?:the )?(?P<b>[^?]+?)\??"
                       r"|how does (?:the )?(?P<b2>[^?]+?) work\??"
                       r"|(?P<b3>[^?]+?) (?:plan|strat|strategy|tactics)\?")
# Only these explicit phrases take the lead back; "i'm calling it" (calling it a night) never does.
_HE_LEADS = re.compile(r"(?:i'?ll lead|i'?m leading|i lead)(?: (?:it|this|the raid))?(?: tonight| now)?")
_TIME = r"(?: tonight| now| this one| today| from now on)?"
_LEADS = (
    r"(?P<n>{name}),? (?:you're|you are|youre) (?:leading|the lead|raid lead|the raid lead|in charge)" + _TIME,
    r"(?P<n>{name}),? (?:you lead|you take (?:the )?lead|take (?:the )?lead|lead (?:us|the raid|this))" + _TIME,
    r"(?P<n>{name})(?:'s| is) (?:leading|the lead|raid lead|the raid lead|in charge)" + _TIME,
    r"(?P<n>{name}) (?:leads|takes (?:the )?lead|has (?:the )?lead)" + _TIME,
    r"(?:you lead|you're leading|take (?:the )?lead),? (?P<n>{name})" + _TIME,
    r"(?:new )?(?:raid )?lead(?: is|:)? (?P<n>{name})" + _TIME,
    r"let (?P<n>{name}) lead" + _TIME,
)
_NAME = r"[^\W\d_][\w'-]*"


def normalize(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_CHARS:
        return ""
    text = text.lower().replace("’", "'").replace("‘", "'").replace("ʼ", "'")
    return " ".join(text.split())


def resolve(word: str, names: dict[str, int]) -> int | None:
    """A member of his group by name: exactly, or by a unique prefix of three letters or more."""
    word = word.strip(" ,.:;!?'").removesuffix("'s")
    exact = {g for n, g in names.items() if n == word}
    if exact:
        return next(iter(exact)) if len(exact) == 1 else None
    if len(word) < 3:
        return None
    found = {g for n, g in names.items() if n.startswith(word)}
    return next(iter(found)) if len(found) == 1 else None


def _strip(text: str) -> str:
    text = _PUNCT.sub("", text.strip(" ,"))
    text = _LEAD_FILLER.sub("", text)
    return _TAIL_FILLER.sub("", text).strip(" ,")


def _address(text: str, names: dict[str, int]) -> list[tuple[str, int | None, str]]:
    """The ways the line can be read: (who, member, the rest), an explicit addressee first, the
    whole line for everyone last."""
    out = []
    head = re.match(r"^(?P<a>[^\s,:]+)[,:]? +(?P<rest>.+)$", text)
    if head:
        word, rest = head["a"], head["rest"]
        if word in _GROUPS:
            out.append((_GROUPS[word], None, rest))
        elif (guid := resolve(word, names)) is not None:
            out.append(("member", guid, rest))
    tail = re.match(r"^(?P<rest>.+?),? (?P<a>[^\s,]+)$", text)
    if tail:
        word, rest = tail["a"], tail["rest"]
        if word in _GROUPS:
            out.append((_GROUPS[word], None, rest))
        elif (guid := resolve(word, names)) is not None and word not in {"me", "it"}:
            out.append(("member", guid, rest))
    out.append(("everyone", None, text))
    return out


def _order(rest: str, who: str, member: int | None, names: dict[str, int], *,
           plain: bool = False) -> Order | None:
    """The order in `rest`: any pattern after an explicit addressee, only an unambiguous whole
    phrase (_PLAIN) without one."""
    whole = _PUNCT.sub("", _LEAD_FILLER.sub("", rest.strip(" ,"))).strip(" ,")  # "lust now" keeps its now
    rest = _strip(rest)
    for name, pattern in (_PLAIN_RE if plain else _ORDER_RE):
        match = pattern.fullmatch(rest) or pattern.fullmatch(whole)
        if match is None:
            continue
        on = None
        if name == "stack":
            target = match.groupdict().get("on") or match.groupdict().get("on2")
            if target and target not in {"me", "us", "him", "here"}:
                on = resolve(target, names)
                if on is None:
                    return None  # "on it", "on top": no member, no order
        return Order(name, who, member, on)
    return None


def _designation(text: str, names: dict[str, int]) -> Designate | None:
    bare = _strip(text)
    if _HE_LEADS.fullmatch(bare):
        return Designate(None)
    for pattern in _LEADS:
        match = re.fullmatch(pattern.format(name=_NAME), bare)
        if match is not None:
            guid = resolve(match["n"], names)
            if guid is not None:
                return Designate(guid)
    return None


def _boss_asked(bare: str, bosses) -> tuple[int, int] | None:
    match = _PLAN_FOR.fullmatch(bare)
    if match is None:
        return None
    name = " ".join((match["b"] or match["b2"] or match["b3"] or "").split())
    return bosses.get(name.removeprefix("the "))


def parse(text: str, names: dict[str, int], bosses=None) -> Intent | None:
    """What his line asks of the raid, or None for anything else (talk). `names` are the cast
    members of his group by lower-case name. A question is no order, but a ready check or a
    question about the plan is. `bosses` (raid bosses by what he may call them,
    briefs.boss_aliases) adds questions about the plan for a boss he names; None (the boss
    briefs off) adds nothing."""
    text = normalize(text)
    if not text or len(text.split()) > MAX_WORDS:
        return None
    if (found := _designation(text, names)) is not None:
        return found
    text = _LEAD_FILLER.sub("", text) or text  # "ok tanks, taunt": the addressee comes after
    readings = _address(text, names)
    for index, (who, member, rest) in enumerate(readings):
        plain = index == len(readings) - 1  # the whole line, with no addressee
        bare = _strip(rest)
        if who in ("everyone", "member"):
            if _READY.fullmatch(bare):
                return Ready(member)
            if _PLAN.fullmatch(bare):
                return AskPlan(member)
            if bosses is not None and (boss := _boss_asked(bare, bosses)) is not None:
                return AskPlan(member, boss)
            if (count := _COUNT.fullmatch(bare)) is not None:
                seconds = int(count["n"] or count["n2"])
                return Countdown(seconds, member) if 1 <= seconds <= 10 else None
        if "?" in rest:
            continue
        if (order := _order(rest, who, member, names, plain=plain)) is not None:
            return order
    if "?" in text:
        return None
    # Only a line addressed to the group, a role or a member and led by an order word is an
    # order attempt; without an addressee only the patterns above count ("stay safe" is talk).
    for who, member, rest in _address(text, names)[:-1]:
        words = _strip(rest).split()
        if words and words[0] in _STRONG | _WEAK:
            return Unclear(who, member)
    return None


def schema(names: dict[str, int]) -> dict:
    """The model's answer: an order from ORDERS or "none", who from WHO, the member's name from
    his group (or ""), and a confidence. The enums are the whole of what it may say."""
    return {
        "type": "object",
        "properties": {
            "order": {"type": "string", "enum": [*ORDERS, "none"]},
            "who": {"type": "string", "enum": list(WHO)},
            "member": {"type": "string", "enum": ["", *sorted(names)]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["order", "who", "member", "confidence"],
    }


SYSTEM = (
    "You read one line a World of Warcraft raid leader typed or said to his own group and decide "
    "which single order it gives, from a fixed list, or none. The orders: "
    + "; ".join(f"{name}: {MEANING[name]}" for name in ORDERS)
    + "; none: no order from this list. who is everyone, tanks, healers, damage, melee, ranged, "
    "or member when he speaks to one named member (member is then that name, else empty). "
    "confidence is how sure you are, 0 to 1. Answer with the JSON object only.")


def read_answer(content: str, names: dict[str, int]) -> tuple[Order | None, float] | None:
    """The model's answer as (order or None for "none", confidence); None when it is not a valid
    answer. Every field is checked again here: the schema is not trusted to have held."""
    import json
    try:
        value = json.loads(content)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    order, who, member, confidence = (value.get(k) for k in ("order", "who", "member", "confidence"))
    if (order not in (*ORDERS, "none") or who not in WHO or not isinstance(member, str)
            or type(confidence) not in (int, float) or not 0 <= confidence <= 1):
        return None
    guid = None
    if who == "member":
        guid = names.get(member)
        if guid is None:
            return None
    elif member:
        return None
    if order == "none":
        return None, float(confidence)
    return Order(order, who, guid, None, by_model=True), float(confidence)
