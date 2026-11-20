"""What her instruction tells her about his invitation: what he asked, what she decided and what
her body really did, so her answer and her body agree. Never "on my way" unless the move was
accepted; a refusal is said as her own reason in plain words. Pure: text from codes."""
from . import spots as S

# Native refusal codes and the mind's own, as her reason (second person: the instruction speaks
# to her; {he}/{him}/{his} are his pronouns).
REASONS = {
    "combat": "you're in the middle of a fight",
    "dead": "you're dead right now",
    "human_dead": "{he} is dead right now",
    "in_instance": "one of you is inside an instance",
    "his_instance": "{he} is inside an instance, and you can't get in there",
    "in_bg": "one of you is in a battleground",
    "queued": "you're queued for something",
    "travelling": "one of you is on the move right now",
    "grouped": "you're grouped up with other people",
    "bad_area": "you can't just turn up out where {he} is; somewhere in town would work",
    "group_full": "{his} group is full",
    "in_raid": "{his} group is a raid",
    "not_leader": "{he} isn't the one leading {his} group",
    "lfg_group": "{his} group is a finder group",
    "too_far": "you're not close enough to {him}",
    "no_fishing": "you never learned to fish",
    "no_water": "there's no water close enough to fish",
    "bad_spot": "you can't get there",
    "cant_reach": "you can't get there from where you are right now",
    "away": "you weren't at your keyboard when {he} asked",
    "busy": "you're busy with something away from the game right now",
    "dislike": "you don't feel like it, not with {him}",
    "not_now": "you just don't feel like it right now",
    "pending": "you're still sorting out what {he} asked a moment ago",
    "cap": "you can't get away right now",
    "unknown": "you're not sure yet whether you can make it",
}
DEFAULT_REASON = "you can't make it right now"
BUSY_REASON = "you're tied up with something for the guild right now"

# What each done step was, by native's done job (Guild/Strings.cpp outcomes).
DONE = {
    "came": "you've come over and you're beside {him} now",
    "joined": "you've joined {his} group and come to {him}",
    "joined_far": "you've joined {his} group and you're making your way to {him}",
    "following": "you're following {him} now",
    "sat": "you've sat down with {him}",
    "fishing": "your line is in the water",
}
NOTES = {
    "with_him": "You're with {him} in {his} group already, so you'll just go along together",
    "in_group": "You're in {his} group already",
    "no_fish": "you'll keep {him} company while {he} fishes",
}
FOLLOW_WHY = "source:invite"


def _fill(text: str, forms: tuple[str, str, str]) -> str:
    he, him, his = forms
    return text.format(he=he, him=him, his=his)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def label(place) -> str:
    return place.label if isinstance(place, S.Spot) else ""


def what_words(what: str, place) -> str:
    """What he invited her to, as "to <words>"."""
    where = label(place)
    return {
        "come": "come over to {him}",
        "meet": f"meet {{him}} at {where}" if where else "meet {him}",
        "fish": "go fishing with {him}" + (f" at {where}" if where else ""),
        "sit": "sit with {him}" + (f" at {where}" if where else ""),
        "follow": "come along with {him}",
        "join": "join {his} group",
    }.get(what, "come along with {him}")


def reason_words(code: str) -> str:
    if code in REASONS:
        return REASONS[code]
    if code.startswith("busy"):
        return BUSY_REASON
    return DEFAULT_REASON


def done_words(outcome: str, step: str, place) -> str:
    if step == "go_to":
        where = label(place) or "the place {he} named"
        return f"you've gone to {where} and you're waiting for {{him}} there"
    return DONE.get(outcome, "you've done what {he} asked")


def instruction(*, what: str, place, later: bool, accept: bool, reason: str, steps, outcomes,
                failed: str, done: bool, note: str, forms: tuple[str, str, str]) -> str:
    """Her instruction for an answer to his invitation, by its state now. `outcomes`: (step,
    outcome, code) of the steps carried out so far; `failed`: the code that stopped them."""
    asked = what_words(what, place)
    if later:
        mind = "You'd like that." if accept else "You're not keen."
        return _fill(f"{_cap(forms[0])} is suggesting you {asked} later, not right now. {mind} Say so in "
                     "your own words. Nothing happens now: don't say you're on your way or already "
                     "there.", forms)
    head = _cap(forms[0]) + " invited you to " + asked + "."
    if place is S.UNKNOWN:
        text = (f"{_cap(forms[0])} asked you to meet {{him}} somewhere, but you don't know which place "
                "{he} means. Ask {him} where exactly, briefly. Don't say you're on your way.")
        return _fill(text, forms)
    if not accept:
        text = (f"{head} You're not going: {reason_words(reason)}. Turn {{him}} down in your own words, "
                "briefly. Don't say you're on your way or coming.")
        return _fill(text, forms)
    if not steps:
        return _fill(f"{head} {NOTES.get(note, 'You said yes')}. Say yes in your own words.", forms)
    did = [done_words(o, s, place) for s, outcome, o in outcomes if outcome == "done"]
    if note == "no_fish" and did:
        did.append(NOTES["no_fish"])
    if failed and not did:
        if failed == "unknown":
            text = (f"{head} You said yes, but it isn't clear yet whether you can make it: say something "
                    "short and don't claim you're on your way or already there.")
        else:
            text = (f"{head} You'd like to, but you can't right now: {reason_words(failed)}. Say so "
                    "naturally, and maybe suggest later. Don't say you're on your way.")
        return _fill(text, forms)
    if failed:
        text = (f"{head} You said yes: {' and '.join(did)}, but {reason_words(failed)}. Say so in your "
                "own words.")
        return _fill(text, forms)
    if done or did:
        rest = "" if done else " More may follow; say only what's done."
        text = (f"{head} You said yes: {' and '.join(did)}.{rest} Answer {{him}} in your own words; no "
                "need to describe getting there.")
        return _fill(text, forms)
    text = (f"{head} You're trying to make it, but it isn't sorted yet: say something short and don't "
            "claim you're on your way or already there.")
    return _fill(text, forms)


def follow_up(*, what: str, place, steps, outcomes, failed: str, told_done: int,
              forms: tuple[str, str, str]) -> str | None:
    """A short line after her answer, when her body did more (or less) than her answer told:
    None when there is nothing new to say. `told_done`: how many steps were done when her answer
    was written."""
    did = [done_words(o, s, place) for s, outcome, o in outcomes if outcome == "done"]
    if failed:
        if failed == "unknown":
            return None  # nothing known to say: never a guess
        new = did[told_done:]
        if new:
            start = f"{_cap(' and '.join(new))}, but {reason_words(failed)}"
        elif did:
            start = f"It turns out {reason_words(failed)}"
        else:
            start = f"After all, {reason_words(failed)}, so it didn't work out"
        return _fill(f"A moment ago {{he}} invited you to {what_words(what, place)}. {start}. Tell "
                     "{him} in one short line, in your own words.", forms)
    if told_done == 0 and did and len(did) == len(steps):
        return _fill(f"A moment ago {{he}} invited you to {what_words(what, place)}, and now "
                     f"{' and '.join(did)}. Tell {{him}} in one short line, in your own words.", forms)
    return None


def later_dm(what: str, place, forms: tuple[str, str, str]) -> str:
    """His DM inviting her to something in game: nobody moves from Discord."""
    return _fill(f"{_cap(forms[0])} is asking you to {what_words(what, place)} in the game. From here you "
                 "can only agree to meet up in game later, or not: say so in your own words. Don't say "
                 "you're on your way or already there.", forms)


def not_asked(forms: tuple[str, str, str]) -> str:
    """A member answering his invitation that asked others: never a yes she did not give."""
    return _fill(f"{_cap(forms[0])} was asking others along, not you: don't say you're coming or on "
                 "your way.", forms)
