"""What the raid lead says: model-written words wrapped around facts the code built, and the
plain lines that need no model. Nothing here holds persona text, and nothing explicit.

`boss_brief` is the single hook for the raid playbooks (mind/leeroy_mind/raidplay/): a boss's
pre-pull plan built by code (briefs.py), said word for word, or None. Its callers ask it only
while `[raidlead] briefs` is on (guild/calls.RaidCalls before its pre-pull call, the service for
his questions about the plan); otherwise, or when it is None, `boss_plan`, the guild's standing
pre-pull call (guild/boss_calls.BOSS_CALLS), answers as before.
"""

from dataclasses import dataclass
import logging

from leeroy_mind.logs import log as log_event

from . import briefs as B
from .orders import MEANING


@dataclass(frozen=True, slots=True)
class BossContext:
    """The fight a brief is for: the core's encounter index on that map, its size (10 or 25; 0
    not known) and heroic, and `roles`: the playbook role (tank, healer, melee, ranged) of each
    member of his group whose role is known, empty when none is."""
    map_id: int
    boss_id: int
    boss_name: str = ""
    size: int = 0
    heroic: bool = False
    roles: tuple[str, ...] = ()

    @property
    def difficulty(self) -> int | None:
        return B.difficulty(self.size, self.heroic)

    @property
    def context_id(self) -> str:
        """MAP:BOSS:DIFFICULTY for the logs (the difficulty "-" when not known)."""
        diff = self.difficulty
        return f"{self.map_id}:{self.boss_id}:{'-' if diff is None else diff}"


_UNAVAILABLE = []


def boss_brief(context: BossContext) -> str | None:
    """The playbook's brief for this boss at its difficulty, one line per text line (at most
    briefs.MAX_LINES of at most briefs.LINE_BYTES bytes each), built by code and said as it is;
    None when the raid has no playbook for it, the difficulty is not known, no mechanic is for
    the roles given, or the playbook data cannot be read (logged once, then the standing call)."""
    try:
        brief = B.build(context.map_id, context.boss_id, context.difficulty, context.roles)
    except Exception as exc:
        if not _UNAVAILABLE:
            _UNAVAILABLE.append(True)
            log_event("raidlead_brief_unavailable", level=logging.WARNING, error=type(exc).__name__)
        return None
    return "\n".join(brief.lines) if brief is not None else None


def boss_plan(context: BossContext) -> str | None:
    """The guild's standing pre-pull call for that encounter (its heroic call on heroic), else
    None."""
    from leeroy_mind.guild.boss_calls import BOSS_CALLS, HEROIC_CALLS
    key = (context.map_id, context.boss_id)
    call = (HEROIC_CALLS.get(key) if context.heroic else None) or BOSS_CALLS.get(key)
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


def briefed(human: str, boss: str, facts: list[str]) -> str:
    """Her words after the boss plan she just posted word for word (the brief): the plan is not
    in the instruction, so the model can neither repeat nor change it."""
    return (f"{human} asked about the plan, and you just posted the plan for {boss} to the group, "
            f"word for word from the guild's playbook. Facts: {' '.join(facts)} Add one short line "
            f"for {human} in your own voice from these facts only; do not repeat the plan, and "
            f"name no mechanic or tactic.")


COUNTDOWN = "pulling in {seconds}"
GO = "go"
