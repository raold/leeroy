"""Generic guild prompts and deterministic raid callouts, without persona text."""

import re

from leeroy_mind.text import strip_wow_markup


INSTRUCTIONS = {
    "raid_call": "Ask {human} about a raid tonight. Suggest {suggest}. Available: {tanks} tanks, {healers} healers, {damage} damage. Keep it brief, in your own voice.",
    "pick_invalid": "The choice of {raid} cannot run: {reason}. Explain briefly and ask for another choice.",
    "raid_declined": "The guild master declined tonight's raid. Acknowledge it briefly, your way.",
    "call_lapsed": "The raid invitation went unanswered. Let the guild know the call has lapsed, briefly.",
    "raid_forming": "We're forming {raid} {size}. Role shortfalls: {short}. Say so briefly.",
    "bench_note": "These members have been benched repeatedly: {names}. Mention it discreetly to the officers, in a few words.",
    "raid_wrapup": "Wrap up {raid}: {kills} kills, {wipes} wipes, outcome {outcome}. Keep it brief and in your own voice.",
    "hangout_called": "{human} called a quiet night. Acknowledge it and suggest hanging out, your way.",
    "taking_lead": "{human} asked you to lead. Acknowledge it in a few words, your way.",
    "late_arrival": "You're late to the raid. Say so in a few words, your way.",
    "feast_down": "You put down {feast}. Let the raid know in a few words, your way.",
    "mvp_call": "The raid's done. Ask the guild who tonight's MVP was, in a few words.",
    "mvp_vote": "Vote for tonight's MVP: name one of {names}, not yourself, and say why in a few words.",
}

TEMPLATES = {
    "ready_check": "ready check",
    "dkp_kill": "+{amount:g} dkp to {count} for {boss}",
    "loot_call": "{item}: bids to me",
    "loot_award": "{item} -> {winner}, {paid:g} dkp",
    "loot_award_close": "{item} -> {winner}, {paid:g} dkp. close one, {runner_up} bid {runner_bid:g}",
    "loot_unclaimed": "{item}: no takers",
    "loot_give_failed": "{item}: couldn't hand that out ({error})",
    "dkp_standings": "dkp top 5: {top}",
    "dkp_decay": "dkp updated. decay {pct}% applied",
    "pull_countdown": "pulling in {seconds}",
    "pull_go": "go",
    "marks": "kill skull first, then x",
    "lust_now": "lust now",
    "brez": "brez on {name}",
    "ss_on": "ss on {name}",
    "drink_up": "drink up, pulling when everyone's full",
    "mvp_result": "mvp tonight: {name} ({votes} votes)",
}


def instruction(key, **facts) -> str:
    return INSTRUCTIONS[key].format(**facts)


def template(key, **facts) -> str:
    return TEMPLATES[key].format(**facts)


# A raid's outcome code in the words its wrap-up line gets: native offline stops, the mind's own and
# a called raid night's ends. A code not listed reads neutrally; no code reaches the prompt raw.
OUTCOME_WORDS = {
    "cleared": "cleared it",
    "cleared_reachable": "cleared every boss we could reach",
    "timeout": "ran out of time",
    "too_few": "too many people dropped out",
    "leader_lost": "lost our raid leader",
    "wipe_cap": "too many wipes",
    "boss_wipe_cap": "kept wiping on one boss",
    "dc_stalled": "kept getting stuck",
    "dc_door_blocked": "got stuck at a door we couldn't open",
    "dc_paused": "got stuck and couldn't go on",
    "stalled": "got stuck before the next boss",
    "stopped": "called it early",
    "operator_stopped": "called it early",
    "human_login": "called it early",
    "logout": "the raid leader logged off",
    "called": "called it for the night",
    "lapsed": "the call lapsed",
    "declined": "called it off",
    "cancelled": "called it off",
    "hangout": "turned it into a hangout",
    "disbanded": "the group broke up",
    "failed": "couldn't fill the raid",
    "assemble_timeout": "never got everyone together",
}
OUTCOME_NEUTRAL = "the raid is over"


def outcome_words(outcome) -> str:
    return OUTCOME_WORDS.get(str(outcome), OUTCOME_NEUTRAL)


def public_text(ports, text) -> str:
    """Clean game labels and configured identity before public callouts."""
    text = " ".join(strip_wow_markup(str(text)).split())
    ident = ports.human()
    if ident.real_name:
        text = re.sub(rf"(?<!\w){re.escape(ident.real_name)}(?!\w)",
                      lambda _: ident.called, text, flags=re.IGNORECASE)
    return text


def member_name(ports, guid) -> str:
    if guid == ports.human_guid:
        return public_text(ports, ports.human().called)
    member = ports.cast().get(guid)
    return public_text(ports, member.name if member else f"member {guid}")
