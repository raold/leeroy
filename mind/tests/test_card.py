"""The core card as a prompt shows it, and the card's cosmetics (part B of the final fix wave;
the stored card's own tests are in test_personas_schema.py)."""
import itertools

from conftest import make_persona
from leeroy_mind.personas.card import (ATTACHMENT_TEXT, PROMPT_CARD_BUDGET, card_race_class,
                                       render_core_card, render_prompt_card)
from leeroy_mind.text import estimate_tokens

ATTACHMENTS = ("secure", "anxious", "avoidant")
HUMOURS = ("dry", "self-deprecating", "absurd", "teasing", "puns", "dark")
MOTIVATIONS = ("achiever", "social", "explorer", "competitor")
THINKS = ("theorycrafter", "lore", "social", "collector", "competitor")


def cards(**over):
    p = make_persona(101, "Arkon", **over)
    return (render_core_card(p, race="Orc", class_name="Warrior"),
            render_prompt_card(p, race="Orc", class_name="Warrior"))


def _rest(card: str) -> str:
    """The card below its identity and work lines (the prompt card's head is about_lines)."""
    return "\n".join(card.splitlines()[2:])


def test_the_prompt_card_reads_as_a_person_not_a_schema():
    """Prompts minor 13: "Attachment: anxious." and "thinks like a lore" reached the model."""
    for a, h, m, t in itertools.product(ATTACHMENTS, HUMOURS, MOTIVATIONS, THINKS):
        stored, prompt = cards(attachment=a, humour=h, motivation=m, thinks_as=t)
        for schema in ("Attachment", "Humour:", "thinks like", "In game:", f" {m},"):
            assert schema not in prompt, (a, h, m, t, schema)
        head = prompt.splitlines()[:2]
        assert ATTACHMENT_TEXT[a] in prompt
        assert estimate_tokens(prompt) - estimate_tokens("\n".join(head)) <= PROMPT_CARD_BUDGET
        # the same person: the play sentence is the stored card's
        assert stored.splitlines()[0].split(". ", 1)[1] in prompt.splitlines()[0]
    stored, prompt = cards(attachment="anxious", thinks_as="lore")
    assert "quiet on you" in prompt
    assert "In game you play for progress and care about the story." in prompt
    assert "Attachment: anxious." in stored  # the stored card keeps the schema's words


def test_empty_fields_leave_no_label_and_no_double_period():
    """Task 6 carry: empty wants gave "Wants: .", an empty real name "You are , 34", and a job
    that ends its own sentence "Work: nurse.."."""
    stored, prompt = cards(wants=[], real_name="", job="night nurse.", rhythm="nights, mostly!",
                           household="")
    for card in (stored, prompt):
        assert "Wants" not in card and "You want" not in card
        assert "Work: night nurse. Rhythm: nights, mostly!" in card and ".." not in card
    assert stored.startswith("You are 34, UK. You play Arkon")
    assert prompt.startswith("You are a 34-year-old man from the UK (he/him). You play Arkon")
    assert "At home" not in prompt  # an empty household leaves its label out
    for card in cards(wants=["clear the instance", ""]):
        assert "clear the instance." in card and ";" not in card


def test_the_prompt_card_says_who_she_is_at_home_first():
    """Identity audit, 2026-11-15: no card said whether she was a woman, and none carried her
    household, so a married woman said she was single and a woman took his body in explicit
    talk. The prompt card opens with her age and sex in words, her pronouns, her household
    and her job; the stored card keeps its own lines."""
    stored, prompt = cards(player_gender="female", real_name="Kim Vale", household=(
        "Lives with her husband, their twins (6) and a terrier"), job="dental receptionist")
    first, second = prompt.splitlines()[:2]
    assert first == ("You are Kim, a 34-year-old woman from the UK (she/her). You play Arkon, "
                     "an Orc Protection Warrior (tank, raider).")
    assert second == ("At home: Lives with her husband, their twins (6) and a terrier. "
                      "Work: dental receptionist. Rhythm: early shifts, free evenings.")
    assert "woman" not in stored and "husband" not in stored
    assert card_race_class(prompt) == ("Orc", "Warrior")


def test_the_head_is_never_cut_and_the_rest_is_what_it_was():
    """The head comes on top of the budget: a long household never pushes a section off the
    card that the stored card's shorter lines kept, nor is it cut itself."""
    long_home = "Lives with her husband " + "and a very loud parrot " * 4
    for over in ({}, {"household": long_home[:120], "player_gender": "female"},
                 {"opinions": ["x " * 70, "y " * 70, "z"]}):
        p = make_persona(101, "Arkon", **over)
        prompt = render_prompt_card(p, race="Orc", class_name="Warrior")
        assert " ".join(p.real_life.household.split()) in prompt
        before = _rest(render_card_without_head(p))
        assert _rest(prompt) == before


def render_card_without_head(p):
    """The prompt card as it was before its head: the same sections in the same budget."""
    from leeroy_mind.personas import card as C
    return C._render(p, "Orc", "Warrior", *C._prompt_mind(p), budget=PROMPT_CARD_BUDGET)


def test_race_and_class_survive_a_main_spec_with_brackets():
    """Task 6 carry: the race and class were parsed up to the first "(" on the card."""
    p = make_persona(101, "Arkon", main_spec="Fire (mostly)")
    for render in (render_core_card, render_prompt_card):
        assert card_race_class(render(p, race="Blood Elf", class_name="Mage")) == (
            "Blood Elf", "Mage")
