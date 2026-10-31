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


def test_the_prompt_card_reads_as_a_person_not_a_schema():
    """Prompts minor 13: "Attachment: anxious." and "thinks like a lore" reached the model."""
    for a, h, m, t in itertools.product(ATTACHMENTS, HUMOURS, MOTIVATIONS, THINKS):
        stored, prompt = cards(attachment=a, humour=h, motivation=m, thinks_as=t)
        for schema in ("Attachment", "Humour:", "thinks like", "In game:", f" {m},"):
            assert schema not in prompt, (a, h, m, t, schema)
        assert ATTACHMENT_TEXT[a] in prompt and estimate_tokens(prompt) <= PROMPT_CARD_BUDGET
        assert prompt.splitlines()[0] == stored.splitlines()[0]  # the same person
    stored, prompt = cards(attachment="anxious", thinks_as="lore")
    assert "quiet on you" in prompt
    assert "In game you play for progress and care about the story." in prompt
    assert "Attachment: anxious." in stored  # the stored card keeps the schema's words


def test_empty_fields_leave_no_label_and_no_double_period():
    """Task 6 carry: empty wants gave "Wants: .", an empty real name "You are , 34", and a job
    that ends its own sentence "Work: nurse.."."""
    for card in cards(wants=[], real_name="", job="night nurse.", rhythm="nights, mostly!"):
        assert "Wants" not in card and "You want" not in card
        assert card.startswith("You are 34, UK. You play Arkon")
        assert "Work: night nurse. Rhythm: nights, mostly!" in card and ".." not in card
    for card in cards(wants=["clear the instance", ""]):
        assert "clear the instance." in card and ";" not in card


def test_race_and_class_survive_a_main_spec_with_brackets():
    """Task 6 carry: the race and class were parsed up to the first "(" on the card."""
    p = make_persona(101, "Arkon", main_spec="Fire (mostly)")
    for render in (render_core_card, render_prompt_card):
        assert card_race_class(render(p, race="Blood Elf", class_name="Mage")) == (
            "Blood Elf", "Mage")
