"""The core card: the few lines of a persona that ride in every prompt. Deterministic, at most
CARD_BUDGET estimated tokens. Bits, sample lines, the secret and habits never appear here.

Two renderings of the same sections: the stored card (render_core_card: persona_core.card,
`personas card`) names the schema's own words ("Attachment: anxious."), and the prompt card
(render_prompt_card, rendered for every turn from the live persona) says them as behaviour,
since a schema word reaching the model reads as a form, not a person (prompts lens, minor 13).
An empty field leaves its label out, and a field that ends a sentence itself gets no second
period (Task 6 carry)."""
import re

from ..text import estimate_tokens, truncate_words
from ..wow import CLASSES, RACES
from .schema import Persona

CARD_BUDGET = 150
PROMPT_CARD_BUDGET = 165  # behaviour takes a few more words than the schema's labels

ATTACHMENT_TEXT = {
    "secure": "You trust people easily.",
    "anxious": "You worry when people go quiet on you.",
    "avoidant": "You keep people at arm's length.",
}
HUMOUR_TEXT = {
    "dry": "Your humour is dry.",
    "self-deprecating": "You joke at your own expense.",
    "absurd": "Your humour is absurd.",
    "teasing": "You tease people you like.",
    "puns": "You love a pun.",
    "dark": "Your humour is dark.",
}
MOTIVATION_TEXT = {
    "achiever": "play for progress",
    "social": "play for the people",
    "explorer": "play to explore",
    "competitor": "play to win",
}
THINKS_TEXT = {
    "theorycrafter": "think in numbers",
    "lore": "care about the story",
    "social": "think of people first",
    "collector": "collect everything",
    "competitor": "want to be the best",
}


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _sentence(s: str) -> str:
    s = _one_line(s)
    return s if not s or s[-1] in ".!?" else s + "."


def _field(label: str, value: str) -> str:
    """"Label: value." for a value, "" for an empty one."""
    v = _one_line(value)
    return _sentence(f"{label}: {v}") if v else ""


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _furthest_trait(p: Persona):
    traits = [("openness", p.big_five.openness), ("conscientiousness", p.big_five.conscientiousness),
              ("extraversion", p.big_five.extraversion), ("agreeableness", p.big_five.agreeableness),
              ("neuroticism", p.big_five.neuroticism)]
    return max(traits, key=lambda kv: abs(kv[1].score - 5))  # first wins a tie


def _identity_line(p: Persona, race: str, class_name: str) -> str:
    first = _one_line(p.identity.real_name).split(" ")[0]
    role = f"{p.play.raid_role}, {p.play.guild_role}"
    if _one_line(p.play.officer_title):
        role += f" {_one_line(p.play.officer_title)}"
    kind = " ".join(x for x in (race, _one_line(p.play.main_spec), class_name) if x)
    play = f"You play {p.character_name}"
    if kind:
        play += f", {_article(kind)} {kind}"
    who = ", ".join(x for x in (first, str(p.identity.age), _one_line(p.identity.region)) if x)
    return f"You are {who}. {play} ({role})."


def _wants(p: Persona) -> str:
    return "; ".join(w for w in (_one_line(x) for x in p.game.wants) if w)


def _stored_mind(p: Persona) -> tuple[str, str]:
    mind = f"Attachment: {p.attachment}. Humour: {p.humour}."
    view = " ".join(x for x in (f"In game: {p.game.motivation}, thinks like a "
                                f"{p.game.thinks_as}.", _field("Wants", _wants(p))) if x)
    return mind, view


def _prompt_mind(p: Persona) -> tuple[str, str]:
    mind = f"{ATTACHMENT_TEXT[p.attachment]} {HUMOUR_TEXT[p.humour]}"
    view = " ".join(x for x in (f"In game you {MOTIVATION_TEXT[p.game.motivation]} and "
                                f"{THINKS_TEXT[p.game.thinks_as]}.",
                                _field("Wants", _wants(p))) if x)
    return mind, view


def _render(p: Persona, race: str, class_name: str, mind: str, view: str, *,
            budget: int = CARD_BUDGET) -> str:
    ident = _identity_line(p, race, class_name)
    work = " ".join(x for x in (_field("Work", p.real_life.job),
                                _field("Rhythm", p.real_life.rhythm)) if x)

    ext = p.big_five.extraversion
    name, far = _furthest_trait(p)
    behaviours = [_sentence(ext.behaviour)]
    if name != "extraversion":
        behaviours.append(_sentence(far.behaviour))
    temper = " ".join(b for b in behaviours if b)

    opinions = "Opinions: " + " | ".join(_one_line(o) for o in p.opinions[:2])
    nevers = "Never: " + "; ".join(_one_line(n) for n in p.nevers[:2])

    # Sections in order; the optional ones are dropped in this order while over budget.
    sections = {"ident": ident, "work": work, "temper": temper, "mind": mind, "view": view,
                "opinions": opinions if p.opinions else "",
                "nevers": nevers if p.nevers else ""}
    for drop in (None, "opinions", "view", "mind"):
        if drop:
            sections[drop] = ""
        card = "\n".join(s for s in sections.values() if s)
        if estimate_tokens(card) <= budget:
            return card
    # Still over: the persona's own strings are long. Trim the tail, keeping the identity line.
    return truncate_words(card, budget * 4)


def render_core_card(p: Persona, *, race: str, class_name: str) -> str:
    """The stored card, in the schema's own words."""
    return _render(p, race, class_name, *_stored_mind(p))


def render_prompt_card(p: Persona, *, race: str, class_name: str) -> str:
    """The card a prompt shows: the same sections, attachment, humour and how they play said as
    behaviour, in at most PROMPT_CARD_BUDGET estimated tokens."""
    return _render(p, race, class_name, *_prompt_mind(p), budget=PROMPT_CARD_BUDGET)


# The identity line's "You play Name, a/an KIND (ROLE)." with the role the last parentheses,
# so a main spec with its own "(" still parses.
_PLAY = re.compile(r"You play [^,(\n]+(?:, an? (?P<kind>[^\n]*?))? \([^()\n]*\)\.")


def card_race_class(card: str) -> tuple[str, str]:
    """Recover the race and class a card was rendered with ("" for a part it does not name)."""
    m = _PLAY.search(card)
    kind = (m.group("kind") or "") if m else ""
    race = next((r for r in sorted(RACES.values(), key=len, reverse=True)
                 if kind == r or kind.startswith(r + " ")), "")
    cls = next((c for c in sorted(CLASSES.values(), key=len, reverse=True)
                if kind == c or kind.endswith(" " + c)), "")
    return race, cls
