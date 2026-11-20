"""What her instruction tells her about his "you lead": what he asked and what really happened, so
her answer and the game agree. Never "I'm leading" unless she is; a refusal is said as her own
reason in plain words. Pure: text from codes."""
from leeroy_mind.invites import spots as S

FOLLOW_WHY = "source:lead"

# Native refusal codes and the mind's own, as her reason (second person; {he}/{him}/{his} are his
# pronouns, {place} where he asked to go).
REASONS = {
    "combat": "you're in the middle of a fight",
    "dead": "you're dead right now",
    "human_dead": "{he} is dead right now",
    "in_instance": "one of you is inside an instance",
    "in_bg": "one of you is in a battleground",
    "queued": "you're queued for something",
    "travelling": "one of you is on the move right now",
    "not_in_group": "you're not in {his} group",
    "not_leader": "{he} isn't the one leading {his} group",
    "lfg_group": "{his} group is a finder group",
    "too_far": "you're too far from {him} to lead {him} anywhere",
    "other_map": "{place} is too far to walk to from here",
    "bad_spot": "you don't know the way to {place}",
    "here": "you're already at {place}",
    "afk": "you weren't at your keyboard when {he} asked",
    "not_online": "you're not in the game right now",
    "raid_led": "the guild's raid is already being led",
    "no_tank": "there's no tank in {his} group to lead the way in here",
    "dc_refused": "the run in here won't start right now",
    "no_group": "{he} isn't in a group",
}
DEFAULT_REASON = "you can't take the lead right now"
BUSY_REASON = "you're tied up with something for the guild right now"

ACTIVITY = {
    "quest": "out questing: you choose where to go and which quests to do",
    "grind": "out grinding the mobs around you",
    "travel": "to {place}",
}


def _fill(text: str, forms: tuple[str, str, str], place: str = "") -> str:
    he, him, his = forms
    return text.format(he=he, him=him, his=his, place=place or "there")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def label(place) -> str:
    return place.label if isinstance(place, S.Spot) else ""


def reason(code: str) -> str:
    code = code or ""
    if code in REASONS:
        return REASONS[code]
    if code.startswith("busy"):
        return BUSY_REASON
    return DEFAULT_REASON


def started(activity: str, place, forms) -> str:
    where = ACTIVITY.get(activity, ACTIVITY["quest"])
    return _cap(_fill(
        "{he} asked you to take the lead, and you have: you're leading {his} group " + where
        + ", and {he} is following you. You wait for {him} whenever {he} falls behind. Say it in "
        "your own words; don't name a quest, a mob or a place you haven't been told.", forms,
        label(place)))


def refused(code: str, place, forms) -> str:
    if code == "unknown_place":
        return _cap(_fill("{he} asked you to lead {him} somewhere you don't know the way to. Ask "
                          "{him} where exactly; don't say you're leading.", forms))
    return _cap(_fill("{he} asked you to take the lead, but you can't right now: " + reason(code)
                      + ". Say so plainly; don't say you're leading.", forms, label(place)))


def pending(forms) -> str:
    return _cap(_fill("{he} asked you to take the lead; you're not sure yet whether you can. "
                      "Don't say you're leading.", forms))


def dc_started(tank_name: str | None, forms) -> str:
    who = ("you're leading {his} group from boss to boss now" if not tank_name
           else tank_name + " is leading {his} group from boss to boss now, and you're right behind")
    return _cap(_fill("{he} asked you to lead in here, and the run is on: " + who + "; {he} "
                      "follows. Say it in your own words; don't name a boss you haven't been told.",
                      forms))


def dc_refused(code: str, forms) -> str:
    return _cap(_fill("{he} asked you to lead in here, but the run can't start: " + reason(code)
                      + ". Say so plainly; don't say you're leading.", forms))


def stopped(forms, *, was_leading: bool = True) -> str:
    if not was_leading:
        return _cap(_fill("{he} said {he}'ll lead; you'd already stopped leading. Answer in a few "
                          "words.", forms))
    return _cap(_fill("{he} has taken the lead back: you've stopped leading and you're following "
                      "{him} again. Answer in a few words.", forms))


def not_asked(name: str, forms) -> str:
    return _cap(_fill("{he} asked " + (name or "someone else") + " to take the lead, not you. Don't "
                      "say you're leading.", forms))


# Her one short line when the lead ends by itself (native's lead job, ended).
ENDED = {
    "arrived": "You've led {him} to {place}: you're there now, so you've stopped leading and "
               "you're following {him} again.",
    "left": "{he} fell far behind and you waited a long while; you've stopped leading and you'll "
            "follow {him} again.",
    "timeout": "You've been leading for an hour; you hand the lead back to {him} and follow {him} "
               "again.",
    "no_path": "You couldn't find a way to {place} from here, so you've stopped leading and "
               "you'll follow {him} again.",
    "dead": "You died while leading, so you've stopped leading.",
}


def ended(why: str, place, forms) -> str | None:
    text = ENDED.get(why)
    if text is None:
        return None
    return _cap(_fill(text, forms, label(place))) + " One short line."


def handed_over(tank_name: str | None, code: str, forms) -> str:
    """Her line once they are inside an instance and dungeon-clear took over (or could not)."""
    if code:
        rest = "the run in here hasn't started: " + reason(code)
    elif tank_name:
        rest = tank_name + " leads the way from boss to boss now"
    else:
        rest = "you lead the way from boss to boss now"
    return _cap(_fill("You're all inside now, so you've stopped leading out there; " + rest
                      + ". One short line.", forms))
