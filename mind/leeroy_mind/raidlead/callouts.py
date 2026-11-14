"""What the raid lead says: model-written words wrapped around facts the code built, and the
plain lines that need no model. Nothing here holds persona text, and nothing explicit.

`boss_brief` is the single hook for the raid playbooks (a later PR adds
mind/leeroy_mind/raidplay/): a boss's plan in a few code-built words, or None. It returns None
for now; `boss_plan` asks it first and falls back to the guild's standing pre-pull call
(guild/boss_calls.BOSS_CALLS), and guild/calls.RaidCalls asks it before its own call too.
"""

from dataclasses import dataclass

from .orders import MEANING


@dataclass(frozen=True, slots=True)
class BossContext:
    """The fight a brief is for: the core's encounter index on that map."""
    map_id: int
    boss_id: int
    boss_name: str = ""
    size: int = 0
    heroic: bool = False


def boss_brief(context: BossContext) -> str | None:
    """The raid playbooks' brief for this boss (mind/leeroy_mind/raidplay/, not yet here):
    None until they exist."""
    return None


def boss_plan(context: BossContext) -> str | None:
    """What the lead tells him about a boss: the playbook's brief, else the guild's standing
    pre-pull call for that encounter, else None."""
    from leeroy_mind.guild.boss_calls import BOSS_CALLS
    brief = boss_brief(context)
    if brief:
        return brief
    call = BOSS_CALLS.get((context.map_id, context.boss_id))
    return call.pre if call is not None else None


WHO_WORDS = {"everyone": "everyone", "tanks": "the tanks", "healers": "the healers",
             "damage": "the damage dealers", "melee": "the melee", "ranged": "the ranged"}

# A refusal in plain words (native's skip reasons and the mind's own). Anything not here is
# "they can't right now".
REASONS = {
    "dead": "they're dead", "alive": "nobody needs to release", "released": "they've released already",
    "elsewhere": "they're not here", "not_in_group": "they're not in your group",
    "no_target": "you've got nothing hostile targeted", "has_aggro": "the other tank already has it",
    "no_taunt": "no tank can taunt that", "not_leader": "you're not leading the group",
    "owner_offline": "you're not in game", "busy:realm_owner": "they're busy elsewhere",
    "busy:training_owner": "they're busy elsewhere", "busy:training_cleanup": "they're busy elsewhere",
    "busy:training_state": "they're still on another order", "offline": "they're offline",
    "nobody": "nobody it's for is here", "no_shaman": "nobody here has Bloodlust or Heroism",
    "busy": "they're busy", "failed": "it didn't take", "not_here": "they're not here",
    "not_damage": "tanks and healers don't hold",
}


def reason_words(code: str) -> str:
    return REASONS.get(code, "they can't right now")


def refusal(code: str, name: str = "") -> str:
    """A refused or impossible order: plain, never a fake "on it"."""
    if code == "not_here" and name:
        return f"can't do that one, {name} isn't here"
    return f"can't do that one, {reason_words(code)}"


def order_words(action: str, *, on: str = "", spell: str = "") -> str:
    if action == "stack":
        return f"stack on {on}" if on else MEANING[action]
    if action == "lust" and spell:
        return f"{spell} now"
    return MEANING[action]


def ack(human: str, words: str, who: str, done: list[str], missed: list[tuple[str, str]],
        *, by_model: bool = False) -> str:
    """Her acknowledgement, after the game confirmed it: only what was done, as facts."""
    facts = [f"{human} ordered {who}: {words}.",
             f"Done, confirmed by the game, for {', '.join(done)}." if done else ""]
    if missed:
        facts.append("Not for " + "; ".join(f"{n} ({why})" for n, why in missed) + ".")
    return " ".join(f for f in facts if f) + (
        " Acknowledge it in a few words, in your own voice: say what is being done, nothing that "
        "was not done, and no promise beyond it.")


def clarify(human: str) -> str:
    return (f"{human} gave the raid an order you couldn't make out. Ask {human} which order was "
            f"meant, in one short question.")


def designated(human: str) -> str:
    return (f"{human} asked you to lead the raid; the group stays {human}'s. Acknowledge it in a "
            f"few words, your way.")


def ready(human: str) -> str:
    return f"The ready check is up for {human}'s group. Tell the raid in a few words to click ready."


def plan(human: str, facts: list[str]) -> str:
    return (f"{human} asked about the plan. Facts: {' '.join(facts)} Answer {human} in one or two "
            f"short lines from these facts only; invent no mechanics.")


COUNTDOWN = "pulling in {seconds}"
GO = "go"
